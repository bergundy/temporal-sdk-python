"""Attaching, forwarding, reading, and declaring credentials."""

from __future__ import annotations

import contextvars
import enum
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, NoReturn, TypeVar

import nexusrpc

import temporalio.exceptions

ERROR_TYPE_CREDENTIAL_MISSING = "CredentialMissing"
"""The :py:class:`temporalio.exceptions.ApplicationError` type raised when a
credential is missing or a retain policy declines it. The error names the
credential, never the cause: that goes only to Worker logs.
"""

_REDACTED = "[REDACTED]"

CallableT = TypeVar("CallableT", bound=Callable[..., Any])


class Credential:
    """A credential the current call carried or the Workflow holds.

    ``str()``, ``repr()``, and formatting show ``[REDACTED]``. Call
    :py:meth:`reveal` only to hand the value to the service that verifies it.
    A Credential can't be serialized as a Temporal payload or pickled.
    """

    __slots__ = ("_name", "_value")

    def __init__(self, name: str, value: str) -> None:
        """Create a credential. Applications receive them from :py:func:`get`."""
        self._name = name
        self._value = value

    @property
    def name(self) -> str:
        """The credential's name, such as ``user_token``."""
        return self._name

    def reveal(self) -> str:
        """Return the raw credential, for example to call a downstream service."""
        return self._value

    def __str__(self) -> str:
        """Return ``[REDACTED]``."""
        return _REDACTED

    def __repr__(self) -> str:
        """Show the name, with the value redacted."""
        return f"Credential(name={self._name!r}, value={_REDACTED})"

    def __format__(self, format_spec: str) -> str:
        """Format ``[REDACTED]``."""
        return format(_REDACTED, format_spec)

    def __copy__(self) -> Credential:
        """Return self: a Credential is immutable."""
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Credential:
        """Return self: a Credential is immutable."""
        return self

    def __reduce__(self) -> NoReturn:
        """Refuse to pickle, so a credential never leaves the process that way."""
        raise TypeError("a credentials.Credential can't be pickled")


class RetainPolicy:
    """Decides whether a Workflow keeps a credential a call carried in place
    of the one it holds under the same name.

    Only :py:data:`if_absent` and :py:data:`jwt.same_subject` exist: custom
    and unconditional policies wait until credentials can be validated.
    """

    __slots__ = ("_name", "_keep")

    def __init__(
        self,
        name: str,
        keep: Callable[[str | None, str], str | None],
        *,
        _sealed: object = None,
    ) -> None:
        """Policies are sealed: use :py:data:`if_absent` or :py:data:`jwt.same_subject`."""
        if _sealed is not _SEAL:
            raise TypeError(
                "credentials: retain policies are sealed; use credentials.if_absent or credentials.jwt.same_subject"
            )
        self._name = name
        # Returns why it declines, for Worker logs only, or None to keep.
        self._keep = keep

    def __repr__(self) -> str:
        """Show the policy's name."""
        return f"credentials.{self._name}"


_SEAL = object()


def _keep_if_absent(held: str | None, _offered: str) -> str | None:
    return "the Workflow already holds this credential" if held is not None else None


if_absent = RetainPolicy("if_absent", _keep_if_absent, _sealed=_SEAL)
"""Keeps a credential only when the Workflow holds none under that name. The
first one to arrive is kept until the Workflow discards it.
"""


# ---------------------------------------------------------------------------
# Client: attaching


@dataclass(frozen=True)
class _Attachment:
    values: Mapping[str, str]
    key: str | None


_attached: contextvars.ContextVar[_Attachment | None] = contextvars.ContextVar(
    "temporalio_credentials_attached", default=None
)


def _values(
    values: Mapping[str, str] | None, kwargs: Mapping[str, str]
) -> dict[str, str]:
    merged = {**(values or {}), **kwargs}
    for name, value in merged.items():
        if not isinstance(value, str):
            raise TypeError(
                f"credentials: the value of {name!r} must be a str, not {type(value).__name__}"
            )
    return merged


@contextmanager
def attach(values: Mapping[str, str] | None = None, /, **kwargs: str) -> Iterator[None]:
    """Attach credentials to Client calls made in this block.

    They go on starting and signaling Workflows, signal-with-start, Updates,
    update-with-start, and starting standalone Activities and Nexus
    operations. Queries never carry credentials. The binding layer seals them
    to the namespace's default key.

    A call carries the values from the innermost ``attach``: they don't merge
    with outer ones.

    Usage::

        with credentials.attach(user_token=token):
            await client.execute_workflow(Agent.run, input, id=..., task_queue=...)
    """
    with _attach(None, _values(values, kwargs)):
        yield


@contextmanager
def attach_with_key(
    key: str, values: Mapping[str, str] | None = None, /, **kwargs: str
) -> Iterator[None]:
    """Like :py:func:`attach`, sealing to the named key of the keyset instead of the default."""
    with _attach(key, _values(values, kwargs)):
        yield


@contextmanager
def _attach(key: str | None, values: dict[str, str]) -> Iterator[None]:
    token = _attached.set(_Attachment(values, key))
    try:
        yield
    finally:
        _attached.reset(token)


# ---------------------------------------------------------------------------
# Workflow: forwarding


@dataclass(frozen=True)
class _Selection:
    names: tuple[str, ...]
    all: bool
    key: str | None


_selection: contextvars.ContextVar[_Selection | None] = contextvars.ContextVar(
    "temporalio_credentials_selection", default=None
)


@contextmanager
def propagate(*names: str) -> Iterator[None]:
    """Forward exactly the named credentials on Workflow calls made in this block.

    It covers Activities, Local Activities, child Workflows, continue-as-new,
    Signals to other Workflows, and Nexus operations. Calls made outside a
    ``propagate`` block forward nothing.

    With no names it forwards nothing, so ``propagate(*selected)`` with an
    empty ``selected`` never forwards everything by accident. Use
    :py:func:`propagate_all` for that.

    In the run method, the values come from what the Workflow holds. In a
    Signal or Update handler, the call's own credentials come first. A name
    that isn't there is logged and skipped, and the receiver fails closed.
    """
    with _select(_Selection(names, False, None)):
        yield


@contextmanager
def propagate_with_key(key: str, *names: str) -> Iterator[None]:
    """Like :py:func:`propagate`, sealing to the named key of the keyset instead
    of the default or a Nexus endpoint override.
    """
    with _select(_Selection(names, False, key)):
        yield


@contextmanager
def propagate_all() -> Iterator[None]:
    """Forward every credential available, like :py:func:`propagate`."""
    with _select(_Selection((), True, None)):
        yield


@contextmanager
def _select(selection: _Selection) -> Iterator[None]:
    token = _selection.set(selection)
    try:
        yield
    finally:
        _selection.reset(token)


# ---------------------------------------------------------------------------
# Reading


class _Kind(enum.Enum):
    START = enum.auto()
    SIGNAL = enum.auto()
    UPDATE = enum.auto()
    QUERY = enum.auto()
    ACTIVITY = enum.auto()
    NEXUS = enum.auto()


@dataclass
class _Run:
    """What one Workflow run holds."""

    held: dict[str, Credential] = field(default_factory=dict)


@dataclass
class _Scope:
    """The credentials the running code sees: those the current call carried,
    and in a Workflow, what the run holds.
    """

    kind: _Kind
    call: dict[str, Credential]
    run: _Run | None = None

    def lookup(self, name: str) -> Credential | None:
        # The run method reads only what the Workflow holds, so discard takes
        # effect there.
        if self.kind is not _Kind.START and name in self.call:
            return self.call[name]
        return self.run.held.get(name) if self.run else None

    def available(self) -> dict[str, Credential]:
        held = dict(self.run.held) if self.run else {}
        if self.kind is _Kind.START:
            return held
        return {**held, **self.call}


_scope: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "temporalio_credentials_scope", default=None
)


def missing(name: str) -> Exception:
    """The error for a missing credential, for the kind of call running."""
    message = f'credentials: missing credential "{name}"'
    scope = _scope.get()
    if scope is not None and scope.kind is _Kind.NEXUS:
        return nexusrpc.HandlerError(
            message,
            type=nexusrpc.HandlerErrorType.UNAUTHENTICATED,
            retryable_override=False,
        )
    return temporalio.exceptions.ApplicationError(
        message, type=ERROR_TYPE_CREDENTIAL_MISSING, non_retryable=True
    )


def lookup(name: str) -> Credential | None:
    """Like :py:func:`get`, returning None when the credential is missing."""
    scope = _scope.get()
    return scope.lookup(name) if scope else None


def get(name: str) -> Credential:
    """Return the named credential.

    It comes from the credentials the current call carried, then from what the
    Workflow holds. In the run method, only what the Workflow holds counts.

    Raises:
        temporalio.exceptions.ApplicationError: Non-retryable, of type
            ``CredentialMissing``, when it's missing in a Workflow or an
            Activity.
        nexusrpc.HandlerError: Non-retryable, when it's missing in a Nexus
            handler.
    """
    cred = lookup(name)
    if cred is None:
        raise missing(name)
    return cred


def discard(*names: str) -> None:
    """Drop the named credentials from what the Workflow holds."""
    scope = _scope.get()
    if scope is None or scope.run is None:
        return
    for name in names:
        scope.run.held.pop(name, None)


# ---------------------------------------------------------------------------
# Declarations


@dataclass
class _Spec:
    required: list[str] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)
    retain: dict[str, RetainPolicy] = field(default_factory=dict)
    revokes: list[str] = field(default_factory=list)


_SPEC_ATTR = "__temporal_credentials_spec"


def spec_of(fn: Any) -> _Spec | None:
    """The declarations on a handler, looking through bound methods and wrappers."""
    seen: set[int] = set()
    while fn is not None and id(fn) not in seen:
        seen.add(id(fn))
        spec = getattr(fn, _SPEC_ATTR, None)
        if isinstance(spec, _Spec):
            return spec
        defn = getattr(fn, "_defn", None)
        fn = (
            getattr(fn, "__func__", None)
            or getattr(fn, "__wrapped__", None)
            or getattr(fn, "func", None)
            or getattr(defn, "fn", None)
        )
    return None


def _target(fn: Any) -> Any:
    # Applied above @workflow.update, the decorator sees the definition wrapper.
    defn = getattr(fn, "_defn", None)
    return getattr(defn, "fn", fn)


def _spec_for(fn: Any) -> _Spec:
    target = _target(fn)
    spec = getattr(target, _SPEC_ATTR, None)
    if not isinstance(spec, _Spec):
        spec = _Spec()
        setattr(target, _SPEC_ATTR, spec)
    return spec


def _check_names(decorator: str, names: tuple[str, ...]) -> None:
    if not names:
        raise TypeError(f"credentials.{decorator} needs at least one credential name")
    for name in names:
        if not isinstance(name, str) or not name:
            raise TypeError(
                f"credentials.{decorator}: credential names must be non-empty strings, not {name!r}"
            )


def _check_policy(decorator: str, retain: object) -> None:
    if retain is not None and not isinstance(retain, RetainPolicy):
        raise TypeError(
            f"credentials.{decorator}: retain must be credentials.if_absent or credentials.jwt.same_subject, not {retain!r}"
        )


def _add(names: list[str], new: tuple[str, ...]) -> None:
    names.extend(n for n in new if n not in names)


def requires(
    *names: str, retain: RetainPolicy | None = None
) -> Callable[[CallableT], CallableT]:
    """Reject the call unless it carried every named credential.

    On the run method, an Activity, or a Nexus handler, put it below the
    Temporal decorator. A missing credential fails the Workflow with a
    non-retryable error on its first Workflow task, fails the Activity before
    it runs, or fails the Nexus operation with a non-retryable handler error.

    On an Update handler, the Update is rejected in its validator, and checked
    again when the handler starts, because validators don't run on replay. The
    plugin adds a validator to Update handlers declared on the Workflow class
    that have none. On a Signal handler, the Signal is dropped and logged.

    On Update and Signal handlers, ``retain`` keeps the credential for the rest
    of the run, so any coroutine can forward it, and the call is also rejected
    if the policy declines it.
    """
    _check_names("requires", names)
    _check_policy("requires", retain)

    def decorator(fn: CallableT) -> CallableT:
        spec = _spec_for(fn)
        _add(spec.required, names)
        if retain is not None:
            spec.retain.update(dict.fromkeys(names, retain))
        return fn

    return decorator


def accepts(
    *names: str, retain: RetainPolicy | None = None
) -> Callable[[CallableT], CallableT]:
    """Use the named credentials if the call carried them; never reject.

    On Update and Signal handlers, ``retain`` keeps each one the policy allows
    for the rest of the run. Without ``retain``, this only documents intent:
    the handler can read and forward any credential its call carried.
    """
    _check_names("accepts", names)
    _check_policy("accepts", retain)

    def decorator(fn: CallableT) -> CallableT:
        spec = _spec_for(fn)
        _add(spec.accepted, names)
        if retain is not None:
            spec.retain.update(dict.fromkeys(names, retain))
        return fn

    return decorator


def revokes(*names: str) -> Callable[[CallableT], CallableT]:
    """On an Update or Signal handler, drop the named credentials from what the
    Workflow holds when the handler starts.
    """
    _check_names("revokes", names)

    def decorator(fn: CallableT) -> CallableT:
        _add(_spec_for(fn).revokes, names)
        return fn

    return decorator
