"""Interceptors that move credentials between application code and the binding layer.

Outbound, they write the selected credentials into headers in the link
format. Inbound, they read the binding layer's opened result and enforce each
handler's declarations before the handler runs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, NoReturn

import nexusrpc
import nexusrpc._util

import temporalio.activity
import temporalio.client
import temporalio.nexus
import temporalio.worker
import temporalio.workflow
from temporalio.api.common.v1 import Payload
from temporalio.contrib.credentials import _api, _link
from temporalio.contrib.credentials._api import Credential, _Kind, _Run, _Scope, _Spec


class _Writer:
    """Writes credentials onto outgoing headers for the binding layer to seal."""

    def payloads(
        self, values: Mapping[str, str], key: str | None
    ) -> dict[str, Payload]:
        """Temporal header entries carrying values."""
        return _link.pending_payloads(values, key)

    def nexus(self, values: Mapping[str, str], key: str | None) -> dict[str, str]:
        """Nexus header entries carrying values."""
        return _link.pending_nexus(values, key)


class LoopbackWriter(_Writer):
    """Writes credentials already opened, for tests with no binding layer."""

    def payloads(
        self, values: Mapping[str, str], key: str | None
    ) -> dict[str, Payload]:
        """See base class."""
        opened = _link.Opened(ok=True, credentials=dict(values)).marshal()
        return {
            _link.HEADER: Payload(
                metadata={
                    "encoding": _link.ENCODING_SEALED,
                    _link.HEADER_OPENED: opened,
                },
                data=b"v1.loopback",
            )
        }

    def nexus(self, values: Mapping[str, str], key: str | None) -> dict[str, str]:
        """See base class."""
        return {
            _link.HEADER_OPENED: _link.Opened(ok=True, credentials=dict(values))
            .marshal()
            .decode()
        }


def _credentials(
    opened: _link.Opened | None, what: str, log: Callable[[str], None]
) -> dict[str, Credential]:
    """The credentials a call carried. A failure marker counts as missing."""
    if opened is None:
        return {}
    if not opened.ok:
        log(
            f"credentials: the {what} credentials header didn't open; treating its credentials as missing (reason: {opened.reason})"
        )
        return {}
    return {name: Credential(name, value) for name, value in opened.credentials.items()}


# ---------------------------------------------------------------------------
# Client


class _ClientOutbound(temporalio.client.OutboundInterceptor):
    def __init__(
        self, next: temporalio.client.OutboundInterceptor, writer: _Writer
    ) -> None:
        super().__init__(next)
        self._writer = writer

    def _headers(self, headers: Mapping[str, Payload]) -> dict[str, Payload]:
        out = _link.strip_payloads(headers)
        att = _api._attached.get()
        if att is not None and att.values:
            out.update(self._writer.payloads(att.values, att.key))
        return out

    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        # With signal-with-start, the binding layer binds them to the Signal.
        input.headers = self._headers(input.headers)
        return await super().start_workflow(input)

    async def signal_workflow(
        self, input: temporalio.client.SignalWorkflowInput
    ) -> None:
        input.headers = self._headers(input.headers)
        return await super().signal_workflow(input)

    async def start_workflow_update(
        self, input: temporalio.client.StartWorkflowUpdateInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        input.headers = self._headers(input.headers)
        return await super().start_workflow_update(input)

    async def start_update_with_start_workflow(
        self, input: temporalio.client.StartWorkflowUpdateWithStartInput
    ) -> temporalio.client.WorkflowUpdateHandle[Any]:
        # The start and the Update are each sealed to their own call.
        input.start_workflow_input.headers = self._headers(
            input.start_workflow_input.headers
        )
        input.update_workflow_input.headers = self._headers(
            input.update_workflow_input.headers
        )
        return await super().start_update_with_start_workflow(input)

    async def start_activity(
        self, input: temporalio.client.StartActivityInput
    ) -> temporalio.client.ActivityHandle[Any]:
        input.headers = self._headers(input.headers)
        return await super().start_activity(input)

    async def start_nexus_operation(
        self, input: temporalio.client.StartNexusOperationInput
    ) -> temporalio.client.NexusOperationHandle[Any]:
        headers = _link.strip_nexus(input.headers)
        att = _api._attached.get()
        if att is not None and att.values:
            headers.update(self._writer.nexus(att.values, att.key))
        input.headers = headers
        return await super().start_nexus_operation(input)


# ---------------------------------------------------------------------------
# Workflow


def _workflow_log(message: str) -> None:
    temporalio.workflow.logger.warning(message)


def _handler_spec(kind: str, name: str) -> _Spec | None:
    """The declarations on the Signal or Update handler that will run for name,
    including handlers set at runtime and dynamic ones.
    """
    runtime = temporalio.workflow._Runtime.current()
    defns = getattr(runtime, "_signals" if kind == "Signal" else "_updates", None)
    if defns is None:
        cls_defn = temporalio.workflow._Definition.from_class(
            type(temporalio.workflow.instance())
        )
        defns = (
            (cls_defn.signals if kind == "Signal" else cls_defn.updates)
            if cls_defn
            else {}
        )
    defn = defns.get(name) or defns.get(None)
    return _api.spec_of(defn.fn) if defn is not None else None


def _accept(*_args: Any, **_kwargs: Any) -> None:
    pass


def _add_validators() -> None:
    """Give a validator to each Update handler that requires a credential.

    Python calls interceptors at the validation step only for Update handlers
    with a validator. Without one, a missing credential would fail the
    Workflow task instead of rejecting the Update. This runs in the Workflow
    sandbox, on the definitions the run uses, before any Update arrives.
    Handlers set at runtime need a validator of their own.
    """
    runtime = temporalio.workflow._Runtime.current()
    for defn in getattr(runtime, "_updates", {}).values():
        spec = _api.spec_of(defn.fn)
        if spec is not None and spec.required and defn.validator is None:
            defn.set_validator(_accept)


def _check(
    spec: _Spec, arrived: Mapping[str, Credential], run: _Run, what: str
) -> tuple[str | None, dict[str, Credential]]:
    """Check a Signal or Update against its handler's declarations. Returns the
    first required credential that's missing or declined, and what to keep.
    """
    keep: dict[str, Credential] = {}
    for name in spec.required:
        cred = arrived.get(name)
        if cred is None:
            _workflow_log(
                f'credentials: the {what} didn\'t carry the credential "{name}"'
            )
            return name, {}
        policy = spec.retain.get(name)
        if policy is not None:
            reason = _keep(policy, run, name, cred)
            if reason is not None:
                _workflow_log(
                    f'credentials: the retain policy declined the credential "{name}" the {what} carried: {reason}'
                )
                return name, {}
            keep[name] = cred
    for name in spec.accepted:
        cred, policy = arrived.get(name), spec.retain.get(name)
        if cred is None or policy is None or name in keep:
            continue
        reason = _keep(policy, run, name, cred)
        if reason is not None:
            _workflow_log(
                f'credentials: the retain policy declined the credential "{name}" the {what} carried: {reason}'
            )
            continue
        keep[name] = cred
    return None, keep


def _keep(
    policy: _api.RetainPolicy, run: _Run, name: str, offered: Credential
) -> str | None:
    held = run.held.get(name)
    return policy._keep(held.reveal() if held else None, offered.reveal())


class _WorkflowInbound(temporalio.worker.WorkflowInboundInterceptor):
    """One per Workflow run. Holds what the run holds."""

    _writer: _Writer = _Writer()

    def __init__(self, next: temporalio.worker.WorkflowInboundInterceptor) -> None:
        super().__init__(next)
        self._state: _Run | None = None
        self._start: dict[str, Credential] = {}

    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        _add_validators()
        super().init(_WorkflowOutbound(outbound, self._writer))

    def _run(self) -> _Run:
        # Signal and Update handlers in the first Workflow task can run before
        # the run method, and must see the start's credentials too.
        if self._state is None:
            self._start = _credentials(
                _link.opened_from_header(temporalio.workflow.info().headers),
                "Workflow start's",
                _workflow_log,
            )
            self._state = _Run(held=dict(self._start))
        return self._state

    async def execute_workflow(
        self, input: temporalio.worker.ExecuteWorkflowInput
    ) -> Any:
        run = self._run()
        spec = _api.spec_of(input.run_fn)
        for name in spec.required if spec else ():
            if name not in self._start:
                _workflow_log(
                    f'credentials: the Workflow start didn\'t carry the credential "{name}"'
                )
                raise _api.missing(name)
        _api._scope.set(_Scope(_Kind.START, dict(self._start), run))
        return await super().execute_workflow(input)

    async def handle_signal(self, input: temporalio.worker.HandleSignalInput) -> None:
        run = self._run()
        what = f"Signal {input.signal!r}"
        arrived = _credentials(
            _link.opened_from_header(input.headers), f"{what}'s", _workflow_log
        )
        spec = _handler_spec("Signal", input.signal)
        if spec is not None:
            failed, keep = _check(spec, arrived, run, what)
            if failed is not None:
                # It's already in history, so the sender gets no error.
                _workflow_log(f"credentials: dropping {what}")
                return
            run.held.update(keep)
            for name in spec.revokes:
                run.held.pop(name, None)
        _api._scope.set(_Scope(_Kind.SIGNAL, arrived, run))
        await super().handle_signal(input)

    def handle_update_validator(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> None:
        run = self._run()
        what = f"Update {input.update!r}"
        arrived = _credentials(
            _link.opened_from_header(input.headers), f"{what}'s", _workflow_log
        )
        spec = _handler_spec("Update", input.update)
        if spec is not None:
            failed, _ = _check(spec, arrived, run, what)
            if failed is not None:
                raise _api.missing(failed)
        token = _api._scope.set(_Scope(_Kind.UPDATE, arrived, run))
        try:
            super().handle_update_validator(input)
        finally:
            _api._scope.reset(token)

    async def handle_update_handler(
        self, input: temporalio.worker.HandleUpdateInput
    ) -> Any:
        run = self._run()
        what = f"Update {input.update!r}"
        arrived = _credentials(
            _link.opened_from_header(input.headers), f"{what}'s", _workflow_log
        )
        spec = _handler_spec("Update", input.update)
        if spec is not None:
            failed, keep = _check(spec, arrived, run, what)
            if failed is not None:
                # The validator accepted the Update, or it isn't running for
                # the first time. Either history was altered, or the
                # declarations changed without versioning.
                raise RuntimeError(
                    f'credentials: the {what} was accepted, but its handler\'s check of the credential "{failed}" failed'
                )
            run.held.update(keep)
            for name in spec.revokes:
                run.held.pop(name, None)
        _api._scope.set(_Scope(_Kind.UPDATE, arrived, run))
        return await super().handle_update_handler(input)

    async def handle_query(self, input: temporalio.worker.HandleQueryInput) -> Any:
        token = _api._scope.set(_Scope(_Kind.QUERY, {}, self._run()))
        try:
            return await super().handle_query(input)
        finally:
            _api._scope.reset(token)


class _WorkflowOutbound(temporalio.worker.WorkflowOutboundInterceptor):
    def __init__(
        self, next: temporalio.worker.WorkflowOutboundInterceptor, writer: _Writer
    ) -> None:
        super().__init__(next)
        self._writer = writer

    def _selected(self) -> tuple[dict[str, str], str | None] | None:
        selection = _api._selection.get()
        if selection is None:
            return None
        scope = _api._scope.get()
        if scope is None:
            _workflow_log(
                "credentials: no credentials state on this context; is the credentials plugin installed?"
            )
            return None
        available = scope.available()
        if selection.all:
            values = {name: cred.reveal() for name, cred in available.items()}
        else:
            values = {}
            for name in selection.names:
                if name in available:
                    values[name] = available[name].reveal()
                else:
                    _workflow_log(
                        f'credentials: not forwarding the credential "{name}": the Workflow doesn\'t have it'
                    )
        return (values, selection.key) if values else None

    def _headers(
        self, headers: Mapping[str, Payload], *, local: bool = False
    ) -> dict[str, Payload]:
        out = _link.strip_payloads(headers)
        if (selected := self._selected()) is not None:
            values, key = selected
            out.update(
                _link.local_payloads(values)
                if local
                else self._writer.payloads(values, key)
            )
        return out

    def start_activity(
        self, input: temporalio.worker.StartActivityInput
    ) -> temporalio.workflow.ActivityHandle[Any]:
        input.headers = self._headers(input.headers)
        return super().start_activity(input)

    def start_local_activity(
        self, input: temporalio.worker.StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle[Any]:
        input.headers = self._headers(input.headers, local=True)
        return super().start_local_activity(input)

    async def start_child_workflow(
        self, input: temporalio.worker.StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle[Any, Any]:
        input.headers = self._headers(input.headers)
        return await super().start_child_workflow(input)

    def continue_as_new(self, input: temporalio.worker.ContinueAsNewInput) -> NoReturn:
        input.headers = self._headers(input.headers)
        if _link.HEADER in input.headers and not input.workflow:
            # The binding layer binds the header to the Workflow type in the
            # command, which is empty unless named.
            input.workflow = temporalio.workflow.info().workflow_type
        super().continue_as_new(input)

    async def signal_child_workflow(
        self, input: temporalio.worker.SignalChildWorkflowInput
    ) -> None:
        input.headers = self._headers(input.headers)
        return await super().signal_child_workflow(input)

    async def signal_external_workflow(
        self, input: temporalio.worker.SignalExternalWorkflowInput
    ) -> None:
        input.headers = self._headers(input.headers)
        return await super().signal_external_workflow(input)

    async def start_nexus_operation(
        self, input: temporalio.worker.StartNexusOperationInput[Any, Any]
    ) -> temporalio.workflow.NexusOperationHandle[Any]:
        headers = _link.strip_nexus(input.headers)
        if (selected := self._selected()) is not None:
            headers.update(self._writer.nexus(*selected))
        input.headers = headers
        return await super().start_nexus_operation(input)


# ---------------------------------------------------------------------------
# Activities and Nexus


class _ActivityInbound(temporalio.worker.ActivityInboundInterceptor):
    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> Any:
        if temporalio.activity.info().is_local:
            values = _link.local_from_header(input.headers) or {}
            arrived = {name: Credential(name, value) for name, value in values.items()}
        else:
            arrived = _credentials(
                _link.opened_from_header(input.headers),
                "Activity's",
                temporalio.activity.logger.warning,
            )
        token = _api._scope.set(_Scope(_Kind.ACTIVITY, arrived))
        try:
            spec = _api.spec_of(input.fn)
            for name in spec.required if spec else ():
                if name not in arrived:
                    temporalio.activity.logger.warning(
                        f'credentials: the Activity didn\'t carry the credential "{name}"'
                    )
                    raise _api.missing(name)
            return await super().execute_activity(input)
        finally:
            _api._scope.reset(token)


class _NexusInbound(temporalio.worker.NexusOperationInboundInterceptor):
    def __init__(
        self,
        next: temporalio.worker.NexusOperationInboundInterceptor,
        specs: Mapping[tuple[str, str], _Spec],
    ) -> None:
        super().__init__(next)
        self._specs = specs

    async def execute_nexus_operation_start(
        self, input: temporalio.worker.ExecuteNexusOperationStartInput
    ) -> Any:
        ctx = input.ctx
        arrived = _credentials(
            _link.opened_from_nexus(ctx.headers),
            "Nexus request's",
            temporalio.nexus.logger.warning,
        )
        token = _api._scope.set(_Scope(_Kind.NEXUS, arrived))
        try:
            spec = self._specs.get((ctx.service, ctx.operation))
            for name in spec.required if spec else ():
                if name not in arrived:
                    temporalio.nexus.logger.warning(
                        f'credentials: the Nexus request didn\'t carry the credential "{name}"'
                    )
                    raise _api.missing(name)
            return await super().execute_nexus_operation_start(input)
        finally:
            _api._scope.reset(token)


def nexus_specs(handlers: list[Any]) -> dict[tuple[str, str], _Spec]:
    """Map (service, operation) to the declarations on each operation's method."""
    out: dict[tuple[str, str], _Spec] = {}
    for handler in handlers:
        service = nexusrpc.get_service_definition(type(handler))
        if service is None:
            continue
        for attr in dir(type(handler)):
            method = getattr(type(handler), attr, None)
            factory = (
                nexusrpc._util.get_operation_factory(method)
                if callable(method)
                else None
            )
            spec = _api.spec_of(method)
            if factory is None or spec is None:
                continue
            op = nexusrpc._util.get_operation(factory)
            out[(service.name, op.name if op is not None else attr)] = spec
    return out


class Interceptor(temporalio.client.Interceptor, temporalio.worker.Interceptor):
    """The credentials client and Worker interceptor. The plugin installs it
    first, so it reads credentials before other interceptors run and writes
    them after.
    """

    def __init__(self, writer: _Writer, nexus: Mapping[tuple[str, str], _Spec]) -> None:
        """Create the interceptor. The plugin creates it for each client."""
        super().__init__()
        self._writer = writer
        self._nexus = nexus

        class _Inbound(_WorkflowInbound):
            _writer = writer

        self._workflow_inbound = _Inbound

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        """See base class."""
        return _ClientOutbound(next, self._writer)

    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        """See base class."""
        return _ActivityInbound(next)

    def workflow_interceptor_class(
        self, input: temporalio.worker.WorkflowInterceptorClassInput
    ) -> type[temporalio.worker.WorkflowInboundInterceptor]:
        """See base class."""
        return self._workflow_inbound

    def intercept_nexus_operation(
        self, next: temporalio.worker.NexusOperationInboundInterceptor
    ) -> temporalio.worker.NexusOperationInboundInterceptor:
        """See base class."""
        return _NexusInbound(next, self._nexus)
