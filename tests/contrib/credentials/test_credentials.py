"""Credentials through real Workers, with the loopback plugin standing in for
the binding layer."""

import json
import uuid
from datetime import timedelta

import nexusrpc
import pytest
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation

from temporalio import activity, workflow
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowFailureError,
    WorkflowUpdateFailedError,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib import credentials
from temporalio.contrib.credentials.testing import LoopbackPlugin, activity_credentials
from temporalio.exceptions import ActivityError, ApplicationError, NexusOperationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from tests.contrib.credentials.test_api import jwt_for

TIMEOUT = timedelta(seconds=10)


@activity.defn
async def token_of(name: str) -> str:
    cred = credentials.lookup(name)
    return cred.reveal() if cred else "missing"


@activity.defn
@credentials.requires("user_token")
async def requires_token() -> str:
    return credentials.get("user_token").reveal()


@nexusrpc.service
class Tools:
    token_of: nexusrpc.Operation[str, str]


@service_handler(service=Tools)
class ToolsHandler:
    @sync_operation
    @credentials.requires("user_token")
    async def token_of(self, _ctx: StartOperationContext, name: str) -> str:
        return credentials.get(name).reveal()


@workflow.defn
class ChildToken:
    @workflow.run
    async def run(self) -> str:
        cred = credentials.lookup("user_token")
        return cred.reveal() if cred else "missing"


@workflow.defn
class Chain:
    @workflow.run
    @credentials.requires("user_token")
    async def run(self, iteration: int) -> str:
        if iteration == 0:
            with credentials.propagate_all():
                workflow.continue_as_new(1)
        with credentials.propagate("user_token"):
            return await workflow.execute_child_workflow(
                ChildToken.run, id=f"{workflow.info().workflow_id}-child"
            )


@workflow.defn
class Agent:
    @workflow.run
    @credentials.requires("user_token")
    async def run(self, endpoint: str) -> dict[str, str]:
        out: dict[str, str] = {}
        tools = workflow.create_nexus_client(service=Tools, endpoint=endpoint)
        with credentials.propagate("user_token"):
            out["activity"] = await workflow.execute_activity(
                token_of, "user_token", start_to_close_timeout=TIMEOUT
            )
            out["local"] = await workflow.execute_local_activity(
                token_of, "user_token", start_to_close_timeout=TIMEOUT
            )
            out["child"] = await workflow.execute_child_workflow(
                ChildToken.run, id=f"{workflow.info().workflow_id}-child"
            )
            out["nexus"] = await tools.execute_operation(Tools.token_of, "user_token")
        out["outside"] = await workflow.execute_activity(
            token_of, "user_token", start_to_close_timeout=TIMEOUT
        )
        with credentials.propagate("not_held"):
            out["not_held"] = await workflow.execute_activity(
                token_of, "user_token", start_to_close_timeout=TIMEOUT
            )
        with credentials.propagate_all():
            out["all"] = await workflow.execute_activity(
                token_of, "user_token", start_to_close_timeout=TIMEOUT
            )
        try:
            await workflow.execute_activity(
                requires_token, start_to_close_timeout=TIMEOUT
            )
            out["requires"] = "ran"
        except ActivityError as err:
            assert isinstance(err.cause, ApplicationError)
            out["requires"] = err.cause.type or ""
        try:
            await tools.execute_operation(Tools.token_of, "user_token")
            out["nexus_outside"] = "ran"
        except NexusOperationError:
            out["nexus_outside"] = "failed"
        return out


@workflow.defn
class Session:
    def __init__(self) -> None:
        self.done = False

    @workflow.run
    @credentials.requires("user_token")
    async def run(self) -> dict[str, str]:
        await workflow.wait_condition(lambda: self.done)
        with credentials.propagate("user_token", "approver_token"):
            user = await workflow.execute_activity(
                token_of, "user_token", start_to_close_timeout=TIMEOUT
            )
            approver = await workflow.execute_activity(
                token_of, "approver_token", start_to_close_timeout=TIMEOUT
            )
        return {"user": user, "approver": approver}

    @workflow.update
    @credentials.requires("approver_token", retain=credentials.if_absent)
    async def approve(self) -> None:
        pass

    @workflow.update
    @credentials.requires("user_token", retain=credentials.jwt.same_subject)
    async def refresh(self) -> None:
        pass

    @workflow.update
    async def forward(self) -> str:
        # An Update forwards its own credentials without keeping them.
        with credentials.propagate("caller_token"):
            return await workflow.execute_activity(
                token_of, "caller_token", start_to_close_timeout=TIMEOUT
            )

    @workflow.signal
    @credentials.requires("signal_token", retain=credentials.if_absent)
    def keep(self) -> None:
        pass

    @workflow.signal
    @credentials.revokes("user_token")
    def logout(self) -> None:
        pass

    @workflow.signal
    def finish(self) -> None:
        self.done = True

    @workflow.query
    def holds(self, name: str) -> bool:
        return credentials.lookup(name) is not None


def loopback_client(client: Client) -> Client:
    config = client.config()
    config["plugins"] = [LoopbackPlugin()]
    return Client(**config)


def failure_type(err: BaseException) -> str | None:
    while err.__cause__ is not None and not isinstance(err, ApplicationError):
        err = err.__cause__
    return err.type if isinstance(err, ApplicationError) else None


CACHE_SIZES = pytest.mark.parametrize("max_cached_workflows", [1000, 0])


@pytest.mark.requires_local_server
@CACHE_SIZES
async def test_forwarding(env: WorkflowEnvironment, max_cached_workflows: int) -> None:
    client = loopback_client(env.client)
    task_queue = f"tq-{uuid.uuid4()}"
    endpoint = f"credentials-{uuid.uuid4()}"
    await env.create_nexus_endpoint(endpoint, task_queue)
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[Agent, Chain, ChildToken],
        activities=[token_of, requires_token],
        nexus_service_handlers=[ToolsHandler()],
        max_cached_workflows=max_cached_workflows,
    ):
        with credentials.attach(user_token="alice"):
            handle = await client.start_workflow(
                Agent.run, endpoint, id=f"agent-{uuid.uuid4()}", task_queue=task_queue
            )
        result = await handle.result()
        assert result == {
            "activity": "alice",
            "local": "alice",
            "child": "alice",
            "nexus": "alice",
            "outside": "missing",
            "not_held": "missing",
            "all": "alice",
            "requires": credentials.ERROR_TYPE_CREDENTIAL_MISSING,
            "nexus_outside": "failed",
        }

        with credentials.attach(user_token="alice"):
            assert (
                await client.execute_workflow(
                    Chain.run, 0, id=f"chain-{uuid.uuid4()}", task_queue=task_queue
                )
                == "alice"
            )

        # The run method requires the start to carry the token.
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                Agent.run, endpoint, id=f"agent-{uuid.uuid4()}", task_queue=task_queue
            )
        assert failure_type(err.value) == credentials.ERROR_TYPE_CREDENTIAL_MISSING

        # Replay sees the same credentials.
        await Replayer(
            workflows=[Agent, ChildToken], plugins=[LoopbackPlugin()]
        ).replay_workflow(await handle.fetch_history())


@pytest.mark.requires_local_server
@CACHE_SIZES
async def test_session(client: Client, max_cached_workflows: int) -> None:
    client = loopback_client(client)
    task_queue = f"tq-{uuid.uuid4()}"
    alice1, alice2 = jwt_for("idp", "alice", "1"), jwt_for("idp", "alice", "2")
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[Session],
        activities=[token_of],
        max_cached_workflows=max_cached_workflows,
    ):
        with credentials.attach(user_token=alice1):
            handle = await client.start_workflow(
                Session.run, id=f"session-{uuid.uuid4()}", task_queue=task_queue
            )

        # Updates are rejected in the validator when the credential is missing
        # or the retain policy declines it.
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update(Session.approve)
        assert failure_type(err.value) == credentials.ERROR_TYPE_CREDENTIAL_MISSING
        with credentials.attach(approver_token="bob"):
            await handle.execute_update(Session.approve)
        with (
            credentials.attach(approver_token="mallory"),
            pytest.raises(WorkflowUpdateFailedError),
        ):
            await handle.execute_update(Session.approve)

        with (
            credentials.attach(user_token=jwt_for("idp", "mallory")),
            pytest.raises(WorkflowUpdateFailedError),
        ):
            await handle.execute_update(Session.refresh)
        with credentials.attach(user_token=alice2):
            await handle.execute_update(Session.refresh)

        with credentials.attach(caller_token="carol"):
            assert await handle.execute_update(Session.forward) == "carol"
        assert not await handle.query(Session.holds, "caller_token")

        # A Signal missing a required credential is dropped.
        await handle.signal(Session.keep)
        assert not await handle.query(Session.holds, "signal_token")
        with credentials.attach(signal_token="dave"):
            await handle.signal(Session.keep)
        assert await handle.query(Session.holds, "signal_token")

        await handle.signal(Session.finish)
        assert await handle.result() == {"user": alice2, "approver": "bob"}

        # After logout, a refresh has nothing to compare with.
        with credentials.attach(user_token=alice1):
            handle = await client.start_workflow(
                Session.run, id=f"session-{uuid.uuid4()}", task_queue=task_queue
            )
        await handle.signal(Session.logout)
        assert not await handle.query(Session.holds, "user_token")
        with (
            credentials.attach(user_token=alice2),
            pytest.raises(WorkflowUpdateFailedError),
        ):
            await handle.execute_update(Session.refresh)
        await handle.terminate()


@workflow.defn
class WithStart:
    def __init__(self) -> None:
        self.done = False

    @workflow.run
    @credentials.requires("user_token")
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self.done)
        return credentials.get("user_token").reveal()

    @workflow.update
    @credentials.requires("user_token")
    async def begin(self) -> str:
        self.done = True
        return credentials.get("user_token").reveal()


@pytest.mark.requires_local_server
async def test_update_with_start(client: Client) -> None:
    client = loopback_client(client)
    task_queue = f"tq-{uuid.uuid4()}"
    async with Worker(client, task_queue=task_queue, workflows=[WithStart]):
        start = WithStartWorkflowOperation(
            WithStart.run,
            id=f"with-start-{uuid.uuid4()}",
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        with credentials.attach(user_token="alice"):
            assert (
                await client.execute_update_with_start_workflow(
                    WithStart.begin, start_workflow_operation=start
                )
                == "alice"
            )
        assert await (await start.workflow_handle()).result() == "alice"


async def test_activity_environment() -> None:
    env = ActivityEnvironment()
    with activity_credentials(user_token="alice"):
        assert await env.run(requires_token) == "alice"
    assert await env.run(token_of, "user_token") == "missing"


@workflow.defn
class Noop:
    @workflow.run
    async def run(self) -> None:
        pass


async def test_proxy_mode_without_proxy(client: Client) -> None:
    # The dev server isn't a proxy, so it stores what the plugin writes for the
    # proxy to seal.
    config = client.config()
    config["plugins"] = [credentials.Plugin(proxy=True)]
    proxied = Client(**config)
    with credentials.attach(user_token="alice"):
        handle = await proxied.start_workflow(
            Noop.run, id=f"noop-{uuid.uuid4()}", task_queue=f"tq-{uuid.uuid4()}"
        )
    await handle.terminate()
    history = await handle.fetch_history()
    started = history.events[0].workflow_execution_started_event_attributes
    payload = started.header.fields["credentials"]
    assert payload.metadata["encoding"] == b"json/credentials-pending"
    assert json.loads(payload.data) == {"user_token": "alice"}


async def test_plugin_on_worker_only(client: Client) -> None:
    with pytest.raises(ValueError, match="set the credentials plugin on the client"):
        Worker(
            client, task_queue="unused", workflows=[Noop], plugins=[LoopbackPlugin()]
        )


@workflow.defn
class RetainOnRun:
    @workflow.run
    @credentials.requires("user_token", retain=credentials.if_absent)
    async def run(self) -> None:
        pass


async def test_retain_on_run_method(client: Client) -> None:
    with pytest.raises(ValueError, match="not the run method"):
        Worker(loopback_client(client), task_queue="unused", workflows=[RetainOnRun])
