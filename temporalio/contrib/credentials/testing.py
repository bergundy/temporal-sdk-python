"""Helpers for testing code that uses credentials, with no binding layer.

:py:class:`LoopbackPlugin` works with
:py:class:`temporalio.testing.WorkflowEnvironment`, and
:py:func:`activity_credentials` with
:py:class:`temporalio.testing.ActivityEnvironment`, which runs no
interceptors.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from temporalio.contrib.credentials import _api
from temporalio.contrib.credentials._interceptor import LoopbackWriter
from temporalio.contrib.credentials._plugin import _PluginBase


class LoopbackPlugin(_PluginBase):
    """A credentials plugin for tests, with no binding layer.

    Calls carry credentials as if a binding layer had sealed and opened them,
    so Workflows, Activities, and Nexus operations receive what the caller
    attached or the Workflow forwarded, and tests can assert on it. Nothing is
    sealed: credentials are stored in plaintext in the test server's history.
    Never use it outside tests.
    """

    def __init__(self) -> None:
        """Create the plugin."""
        super().__init__(LoopbackWriter())


@contextmanager
def activity_credentials(
    values: Mapping[str, str] | None = None, /, **kwargs: str
) -> Iterator[None]:
    """Make :py:func:`temporalio.contrib.credentials.get` see these credentials
    in an Activity run by :py:class:`temporalio.testing.ActivityEnvironment`
    in this block.
    """
    values = _api._values(values, kwargs)
    call = {name: _api.Credential(name, value) for name, value in values.items()}
    token = _api._scope.set(_api._Scope(_api._Kind.ACTIVITY, call))
    try:
        yield
    finally:
        _api._scope.reset(token)
