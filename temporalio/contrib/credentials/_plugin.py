"""The credentials plugin."""

from __future__ import annotations

import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import temporalio.client
import temporalio.service
import temporalio.worker
import temporalio.workflow
from temporalio.contrib.credentials import _api
from temporalio.contrib.credentials._interceptor import (
    Interceptor,
    _Writer,
    nexus_specs,
)


def _check_workflows(workflows: Sequence[type]) -> None:
    for cls in workflows:
        defn = temporalio.workflow._Definition.from_class(cls)
        if defn is None:
            continue
        spec = _api.spec_of(defn.run_fn)
        if spec is not None and (spec.retain or spec.revokes):
            raise ValueError(
                f"credentials: Workflow {defn.name}: retain and revokes apply to Signal and Update handlers, not the run method"
            )


def _check_activities(activities: Sequence[Callable[..., Any]]) -> None:
    for fn in activities:
        spec = _api.spec_of(fn)
        if spec is not None and (spec.retain or spec.revokes):
            name = getattr(fn, "__name__", repr(fn))
            raise ValueError(
                f"credentials: Activity {name}: retain and revokes apply to Signal and Update handlers"
            )


class _PluginBase(temporalio.client.Plugin, temporalio.worker.Plugin):
    """The credentials plugin, writing credentials with writer."""

    def __init__(self, writer: _Writer) -> None:
        super().__init__()
        self._writer = writer
        self._clients: weakref.WeakSet[temporalio.service.ServiceClient] = (
            weakref.WeakSet()
        )
        self._nexus: dict[tuple[str, str], _api._Spec] = {}

    def name(self) -> str:
        """See base class."""
        return f"temporalio.contrib.credentials.{type(self).__qualname__}"

    def configure_client(
        self, config: temporalio.client.ClientConfig
    ) -> temporalio.client.ClientConfig:
        """See base class."""
        self._clients.add(config["service_client"])
        rest = [
            i for i in config.get("interceptors", []) if not isinstance(i, Interceptor)
        ]
        config["interceptors"] = [Interceptor(self._writer, self._nexus), *rest]
        return config

    async def connect_service_client(
        self,
        config: temporalio.service.ConnectConfig,
        next: Callable[
            [temporalio.service.ConnectConfig],
            Awaitable[temporalio.service.ServiceClient],
        ],
    ) -> temporalio.service.ServiceClient:
        """See base class."""
        return await next(config)

    def configure_worker(
        self, config: temporalio.worker.WorkerConfig
    ) -> temporalio.worker.WorkerConfig:
        """See base class."""
        client = config.get("client")
        if client is None or client.service_client not in self._clients:
            raise ValueError(
                "credentials: set the credentials plugin on the client (Client.connect(plugins=...)), not only on the Worker; Workers inherit it from the client"
            )
        _check_workflows(config.get("workflows") or [])
        _check_activities(config.get("activities") or [])
        self._nexus.update(
            nexus_specs(list(config.get("nexus_service_handlers") or []))
        )
        return config

    async def run_worker(
        self,
        worker: temporalio.worker.Worker,
        next: Callable[[temporalio.worker.Worker], Awaitable[None]],
    ) -> None:
        """See base class."""
        await next(worker)

    def configure_replayer(
        self, config: temporalio.worker.ReplayerConfig
    ) -> temporalio.worker.ReplayerConfig:
        """See base class."""
        _check_workflows(config.get("workflows") or [])
        rest = [
            i for i in config.get("interceptors", []) if not isinstance(i, Interceptor)
        ]
        config["interceptors"] = [Interceptor(self._writer, {}), *rest]
        return config

    def run_replayer(
        self,
        replayer: temporalio.worker.Replayer,
        histories: AsyncIterator[temporalio.client.WorkflowHistory],
        next: Callable[
            [
                temporalio.worker.Replayer,
                AsyncIterator[temporalio.client.WorkflowHistory],
            ],
            AbstractAsyncContextManager[
                AsyncIterator[temporalio.worker.WorkflowReplayResult]
            ],
        ],
    ) -> AbstractAsyncContextManager[
        AsyncIterator[temporalio.worker.WorkflowReplayResult]
    ]:
        """See base class."""
        return next(replayer, histories)


class Plugin(_PluginBase):
    """Secure credential propagation for Clients and Workers.

    Python applications run the binding layer in Temporal Proxy, so create the
    plugin with ``proxy=True``. The application sends credentials to the proxy
    in plaintext and can't check that the server is one: connected straight to
    a Temporal Service, it stores them in plaintext in Workflow history.

    Set the plugin on the Client. Workers inherit it::

        client = await Client.connect("proxy:7233", plugins=[credentials.Plugin(proxy=True)])
        worker = Worker(client, task_queue="agents", workflows=[Agent], activities=[call_tool])

    The plugin puts its interceptor first, so other interceptors can call
    :py:func:`get`, and its Workflow outbound interceptor runs last.
    """

    def __init__(self, *, proxy: bool = False) -> None:
        """Create the plugin.

        Args:
            proxy: Must be ``True``: Temporal Proxy runs the binding layer.
                Running it in the application needs support in SDK Core.
        """
        if not proxy:
            raise ValueError(
                "credentials: create the plugin with Plugin(proxy=True); Python applications run the binding layer in Temporal Proxy"
            )
        super().__init__(_Writer())
