"""Tests for ``workflow.speculative()``.

Every test runs against servers with and without the ``optimistic_start``
namespace capability. Without it the SDK never starts anything optimistically,
so workflows must behave the same while history stays free of optimistic
flags.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pytest

from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType, WorkflowTaskFailedCause
from temporalio.api.history.v1 import HistoryEvent
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import (
    Client,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowHistory,
)
from temporalio.common import RetryPolicy
from temporalio.converter import PayloadConverter
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.worker import Replayer
from tests.helpers import (
    assert_eq_eventually,
    assert_eventually,
    assert_task_fail_eventually,
    new_worker,
)


async def _optimistic_start_supported(client: Client) -> bool:
    resp = await client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace=client.namespace)
    )
    return resp.namespace_info.capabilities.optimistic_start


def _optimistic_flags(event: HistoryEvent) -> list[str]:
    attrs_name = event.WhichOneof("attributes")
    if not attrs_name:
        return []
    attrs = getattr(event, attrs_name)
    flags = []
    fields = attrs.DESCRIPTOR.fields_by_name
    if "optimistic_start" in fields and attrs.HasField("optimistic_start"):
        flags.append("optimistic_start")
    if "optimistic_resolution" in fields:
        # A bool on ActivityTaskStarted, a message on resolution events
        if fields["optimistic_resolution"].message_type is None:
            if attrs.optimistic_resolution:
                flags.append("optimistic_resolution")
        elif attrs.HasField("optimistic_resolution"):
            flags.append("optimistic_resolution")
    return flags


def _summarize(events: list[HistoryEvent]) -> list[str]:
    return [
        " ".join(
            [EventType.Name(e.event_type).removeprefix("EVENT_TYPE_")]
            + _optimistic_flags(e)
        )
        for e in events
    ]


async def _history(handle: WorkflowHandle) -> list[HistoryEvent]:
    return [e async for e in handle.fetch_history_events()]


async def _assert_history(
    handle: WorkflowHandle, supported: bool, expected_if_supported: list[str]
) -> list[HistoryEvent]:
    events = await _history(handle)
    summary = _summarize(events)
    if supported:
        assert summary == expected_if_supported
    else:
        assert not any(_optimistic_flags(e) for e in events), summary
    return events


async def _replay(handle: WorkflowHandle, *workflows: type) -> None:
    await Replayer(workflows=workflows).replay_workflow(await handle.fetch_history())


# Lets tests coordinate with activities, which run in the test's event loop
_events: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
_executions: defaultdict[str, list[int]] = defaultdict(list)


def _event(key: str) -> asyncio.Event:
    return _events[key]


@activity.defn
async def sleep_and_return(name: str, seconds: float) -> str:
    await asyncio.sleep(seconds)
    return name


@workflow.defn
class ExampleWorkflow:
    @workflow.run
    async def run(self, last_activity_seconds: float) -> list[str]:
        order: list[str] = []

        async def run_activity(name: str, seconds: float) -> None:
            order.append(
                await workflow.execute_activity(
                    sleep_and_return,
                    args=[name, seconds],
                    start_to_close_timeout=timedelta(seconds=30),
                )
            )

        async with workflow.speculative():
            a1 = asyncio.create_task(run_activity("a1", 0.5))
            a2 = asyncio.create_task(run_activity("a2", 0.05))
            await workflow.wait([a1, a2])
            await workflow.sleep(0.3)
            await run_activity("a3", last_activity_seconds)
        return order


async def test_speculative_example(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, ExampleWorkflow, activities=[sleep_and_return]
    ) as worker:
        handle = await client.start_workflow(
            ExampleWorkflow.run,
            # Still running at 80% of the task timeout, so it's handed off
            2.5,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )
        assert await handle.result() == ["a2", "a1", "a3"]

    events = await _assert_history(
        handle,
        supported,
        [
            "WORKFLOW_EXECUTION_STARTED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "ACTIVITY_TASK_STARTED optimistic_resolution",
            "ACTIVITY_TASK_COMPLETED optimistic_resolution",
            "ACTIVITY_TASK_STARTED optimistic_resolution",
            "ACTIVITY_TASK_COMPLETED optimistic_resolution",
            "TIMER_STARTED optimistic_start",
            "TIMER_FIRED optimistic_resolution",
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            # a3 was handed off and completed through the normal RPC
            "ACTIVITY_TASK_STARTED",
            "ACTIVITY_TASK_COMPLETED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "WORKFLOW_EXECUTION_COMPLETED",
        ],
    )
    if supported:
        # Resolutions are recorded in the order the workflow observed them
        assert (
            events[7].activity_task_completed_event_attributes.scheduled_event_id == 6
        )
        assert (
            events[9].activity_task_completed_event_attributes.scheduled_event_id == 5
        )
        started = events[10].timer_started_event_attributes.optimistic_start.start_time
        fired = events[11].timer_fired_event_attributes.optimistic_resolution
        assert fired.workflow_time.ToDatetime() - started.ToDatetime() >= timedelta(
            seconds=0.3
        )
    await _replay(handle, ExampleWorkflow)


@pytest.mark.parametrize("optimistic", [True, False])
async def test_speculative_replay_with_and_without_optimistic_start(optimistic: bool):
    # Recorded from test_speculative_example against servers with and without
    # the capability. The same code replays both.
    name = "optimistic" if optimistic else "normal"
    history = WorkflowHistory.from_json(
        "fake",
        Path(__file__)
        .with_name(f"test_speculative_example_{name}_history.json")
        .read_text(),
    )
    assert optimistic == any(_optimistic_flags(e) for e in history.events)
    await Replayer(workflows=[ExampleWorkflow]).replay_workflow(history)


@workflow.defn
class WorkflowTimeWorkflow:
    @workflow.run
    async def run(self) -> list[float]:
        async with workflow.speculative():
            start = workflow.now()
            await workflow.sleep(0.5)
            after_sleep = workflow.now()
            # Without workflow time moving forward during a window this would
            # spin until the next checkpoint
            deadline = after_sleep + timedelta(seconds=0.5)
            while workflow.now() < deadline:
                await asyncio.sleep(0.1)
            end = workflow.now()
        return [
            (after_sleep - start).total_seconds(),
            (end - after_sleep).total_seconds(),
        ]


async def test_speculative_workflow_time_advances(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(client, WorkflowTimeWorkflow) as worker:
        handle = await client.start_workflow(
            WorkflowTimeWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        slept, looped = await handle.result()
    assert slept >= 0.5
    assert looped >= 0.5
    events = await _history(handle)
    if supported:
        # Everything ran in the first workflow task
        assert _summarize(events).count("WORKFLOW_TASK_COMPLETED") == 1
        assert all(
            "optimistic_start" in _optimistic_flags(e)
            for e in events
            if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
        )
    await _replay(handle, WorkflowTimeWorkflow)


@activity.defn
async def fail_first_attempt() -> int:
    if activity.info().attempt == 1:
        raise ApplicationError("Intentional failure")
    return activity.info().attempt


@workflow.defn
class FailureRetriedWorkflow:
    @workflow.run
    async def run(self) -> int:
        async with workflow.speculative():
            return await workflow.execute_activity(
                fail_first_attempt,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=50)),
            )


async def test_speculative_activity_failure_retried_by_server(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, FailureRetriedWorkflow, activities=[fail_first_attempt]
    ) as worker:
        handle = await client.start_workflow(
            FailureRetriedWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == 2
    events = await _assert_history(
        handle,
        supported,
        [
            "WORKFLOW_EXECUTION_STARTED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "ACTIVITY_TASK_STARTED",
            "ACTIVITY_TASK_COMPLETED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "WORKFLOW_EXECUTION_COMPLETED",
        ],
    )
    if supported:
        # The optimistic attempt's failure went through the normal RPC
        started = next(
            e
            for e in events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
        )
        attrs = started.activity_task_started_event_attributes
        assert attrs.attempt == 2
        assert attrs.last_failure.message == "Intentional failure"
    await _replay(handle, FailureRetriedWorkflow)


@activity.defn
async def wait_for_test(key: str) -> str:
    _executions[key].append(activity.info().attempt)
    if len(_executions[key]) == 1:
        _event(f"{key}-started").set()
        await _event(f"{key}-continue").wait()
    return "activity"


@workflow.defn
class SignalDuringWindowWorkflow:
    def __init__(self) -> None:
        self.observed: list[str] = []

    @workflow.run
    async def run(self, key: str) -> list[str]:
        async with workflow.speculative():
            self.observed.append(
                await workflow.execute_activity(
                    wait_for_test, key, start_to_close_timeout=timedelta(seconds=10)
                )
            )
        await workflow.wait_condition(lambda: "signal" in self.observed)
        return self.observed

    @workflow.signal
    def signal(self) -> None:
        self.observed.append("signal")


async def test_speculative_signal_delivered_after_checkpoint(client: Client):
    supported = await _optimistic_start_supported(client)
    key = str(uuid.uuid4())
    async with new_worker(
        client, SignalDuringWindowWorkflow, activities=[wait_for_test]
    ) as worker:
        handle = await client.start_workflow(
            SignalDuringWindowWorkflow.run,
            key,
            id=f"wf-{key}",
            task_queue=worker.task_queue,
        )
        await _event(f"{key}-started").wait()
        await handle.signal(SignalDuringWindowWorkflow.signal)
        if not supported:
            # The signal gets its own workflow task while the activity runs
            await assert_eventually(lambda: _assert_signaled_and_task_completed(handle))
        _event(f"{key}-continue").set()
        observed = await handle.result()

    if supported:
        # The signal arrived during the window, so the workflow observed it
        # only after the activity result it had already seen
        assert observed == ["activity", "signal"]
        await _assert_history(
            handle,
            supported,
            [
                "WORKFLOW_EXECUTION_STARTED",
                "WORKFLOW_TASK_SCHEDULED",
                "WORKFLOW_TASK_STARTED",
                "WORKFLOW_TASK_COMPLETED",
                "ACTIVITY_TASK_SCHEDULED optimistic_start",
                "ACTIVITY_TASK_STARTED optimistic_resolution",
                "ACTIVITY_TASK_COMPLETED optimistic_resolution",
                "WORKFLOW_EXECUTION_SIGNALED",
                "WORKFLOW_TASK_SCHEDULED",
                "WORKFLOW_TASK_STARTED",
                "WORKFLOW_TASK_COMPLETED",
                "WORKFLOW_EXECUTION_COMPLETED",
            ],
        )
    else:
        assert observed == ["signal", "activity"]
    await _replay(handle, SignalDuringWindowWorkflow)


async def _assert_signaled_and_task_completed(handle: WorkflowHandle) -> None:
    summary = _summarize(await _history(handle))
    assert "WORKFLOW_EXECUTION_SIGNALED" in summary
    assert summary[-1] == "WORKFLOW_TASK_COMPLETED"


@workflow.defn
class CompleteWithSignalDuringWindowWorkflow:
    def __init__(self) -> None:
        self.signals: list[str] = []

    @workflow.run
    async def run(self, key: str) -> list[str]:
        async with workflow.speculative():
            result = await workflow.execute_activity(
                wait_for_test, key, start_to_close_timeout=timedelta(seconds=10)
            )
        return [result, *self.signals]

    @workflow.signal
    def signal(self) -> None:
        self.signals.append("signal")


async def test_speculative_completion_rejected_by_unhandled_signal(client: Client):
    supported = await _optimistic_start_supported(client)
    key = str(uuid.uuid4())
    async with new_worker(
        client, CompleteWithSignalDuringWindowWorkflow, activities=[wait_for_test]
    ) as worker:
        handle = await client.start_workflow(
            CompleteWithSignalDuringWindowWorkflow.run,
            key,
            id=f"wf-{key}",
            task_queue=worker.task_queue,
        )
        await _event(f"{key}-started").wait()
        await handle.signal(CompleteWithSignalDuringWindowWorkflow.signal)
        if not supported:
            await assert_eventually(lambda: _assert_signaled_and_task_completed(handle))
        _event(f"{key}-continue").set()
        assert await handle.result() == ["activity", "signal"]

    if supported:
        # The server rejected the checkpoint that also completed the workflow,
        # so the window ran again from scratch, without a new attempt
        assert _executions[key] == [1, 1]
        events = await _history(handle)
        failed = next(
            e
            for e in events
            if e.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED
        )
        assert (
            failed.workflow_task_failed_event_attributes.cause
            == WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_COMMAND
        )
        assert any("optimistic_resolution" in _optimistic_flags(e) for e in events)
    else:
        assert _executions[key] == [1]
    await _replay(handle, CompleteWithSignalDuringWindowWorkflow)


@workflow.defn
class LocalActivityInBlockWorkflow:
    @workflow.run
    async def run(self) -> str:
        async with workflow.speculative():
            try:
                return await workflow.execute_local_activity(
                    sleep_and_return,
                    args=["local", 0],
                    start_to_close_timeout=timedelta(seconds=10),
                )
            except RuntimeError:
                # Catching the error must not let the workflow carry on
                return "caught"


async def test_speculative_local_activity_in_block_fails_task(client: Client):
    async with new_worker(
        client, LocalActivityInBlockWorkflow, activities=[sleep_and_return]
    ) as worker:
        handle = await client.start_workflow(
            LocalActivityInBlockWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await assert_task_fail_eventually(
            handle,
            message_contains="cannot be scheduled inside a speculative block",
        )
        await handle.terminate()


@workflow.defn
class LocalActivityDuringWindowWorkflow:
    def __init__(self) -> None:
        self.first_done = False

    @workflow.run
    async def run(self) -> list[str]:
        async def speculative_part() -> list[str]:
            async with workflow.speculative():
                first = await workflow.execute_activity(
                    sleep_and_return,
                    args=["first", 0.05],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                self.first_done = True
                second = await workflow.execute_activity(
                    sleep_and_return,
                    args=["second", 0.3],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                return [first, second]

        task = asyncio.create_task(speculative_part())
        await workflow.wait_condition(lambda: self.first_done)
        # Scheduled outside the block while the window is open
        local = await workflow.execute_local_activity(
            sleep_and_return,
            args=["local", 0.05],
            start_to_close_timeout=timedelta(seconds=10),
        )
        return [*await task, local]


async def test_speculative_local_activity_outside_block_during_window(
    client: Client,
):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, LocalActivityDuringWindowWorkflow, activities=[sleep_and_return]
    ) as worker:
        handle = await client.start_workflow(
            LocalActivityDuringWindowWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == ["first", "second", "local"]
    summary = _summarize(await _history(handle))
    if supported:
        assert summary[4:7] == [
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "ACTIVITY_TASK_STARTED optimistic_resolution",
            "ACTIVITY_TASK_COMPLETED optimistic_resolution",
        ]
    assert "MARKER_RECORDED" in summary
    await _replay(handle, LocalActivityDuringWindowWorkflow)


@activity.defn
async def wait_for_cancel(key: str) -> str:
    _event(f"{key}-started").set()
    try:
        while True:
            await asyncio.sleep(0.05)
            activity.heartbeat()
    except asyncio.CancelledError:
        _executions[key].append(activity.info().attempt)
        raise


@workflow.defn
class CancelWorkflow:
    @workflow.run
    async def run(
        self, key: str, cancellation_type: workflow.ActivityCancellationType
    ) -> str:
        async with workflow.speculative():
            handle = workflow.start_activity(
                wait_for_cancel,
                key,
                start_to_close_timeout=timedelta(seconds=30),
                heartbeat_timeout=timedelta(seconds=5),
                cancellation_type=cancellation_type,
            )
            await workflow.sleep(0.2)
            handle.cancel()
            try:
                await handle
            except ActivityError as err:
                assert isinstance(err.cause, CancelledError)
                return "cancelled"
        return "not cancelled"


@pytest.mark.parametrize(
    "cancellation_type",
    [
        workflow.ActivityCancellationType.TRY_CANCEL,
        workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    ],
)
async def test_speculative_activity_cancellation(
    client: Client, cancellation_type: workflow.ActivityCancellationType
):
    supported = await _optimistic_start_supported(client)
    key = str(uuid.uuid4())
    async with new_worker(
        client, CancelWorkflow, activities=[wait_for_cancel]
    ) as worker:
        handle = await client.start_workflow(
            CancelWorkflow.run,
            args=[key, cancellation_type],
            id=f"wf-{key}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == "cancelled"
        # The activity itself learns about the cancellation either way
        await assert_eq_eventually([1], lambda: _executions_of(key))

    summary = _summarize(await _history(handle))
    if supported:
        prefix = [
            "WORKFLOW_EXECUTION_STARTED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "TIMER_STARTED optimistic_start",
            "TIMER_FIRED optimistic_resolution",
            "ACTIVITY_TASK_CANCEL_REQUESTED",
        ]
        if cancellation_type == workflow.ActivityCancellationType.TRY_CANCEL:
            # The workflow completed without waiting, so the activity's
            # cancellation reported after the handoff never makes it to history
            assert summary == prefix + ["WORKFLOW_EXECUTION_COMPLETED"]
        else:
            assert summary == prefix + [
                "ACTIVITY_TASK_STARTED optimistic_resolution",
                "ACTIVITY_TASK_CANCELED optimistic_resolution",
                "WORKFLOW_EXECUTION_COMPLETED",
            ]
    else:
        assert "ACTIVITY_TASK_CANCEL_REQUESTED" in summary
    await _replay(handle, CancelWorkflow)


async def _executions_of(key: str) -> list[int]:
    return list(_executions[key])


@activity.defn
async def heartbeat_through_handoff(key: str) -> list[str]:
    activity.heartbeat("early")
    # Stay quiet until well past the handoff at 80% of the task timeout
    await _event(f"{key}-handed-off").wait()
    i = 0
    while not _event(f"{key}-continue").is_set():
        i += 1
        activity.heartbeat(f"late-{i}")
        await asyncio.sleep(0.1)
    return [str(activity.info().attempt), *activity.info().heartbeat_details]


@workflow.defn
class HeartbeatWorkflow:
    @workflow.run
    async def run(self, key: str) -> list[str]:
        async with workflow.speculative():
            return await workflow.execute_activity(
                heartbeat_through_handoff,
                key,
                start_to_close_timeout=timedelta(seconds=30),
                heartbeat_timeout=timedelta(seconds=3),
            )


async def test_speculative_heartbeats_through_handoff(client: Client):
    key = str(uuid.uuid4())
    async with new_worker(
        client, HeartbeatWorkflow, activities=[heartbeat_through_handoff]
    ) as worker:
        handle = await client.start_workflow(
            HeartbeatWorkflow.run,
            key,
            id=f"wf-{key}",
            task_queue=worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )

        async def heartbeat_details() -> list[str]:
            desc = await handle.describe()
            pending = desc.raw_description.pending_activities
            assert len(pending) == 1
            assert pending[0].HasField("heartbeat_details")
            return PayloadConverter.default.from_payloads(
                pending[0].heartbeat_details.payloads
            )

        # The server learns about the heartbeat sent before the handoff
        await assert_eq_eventually(
            ["early"], heartbeat_details, timeout=timedelta(seconds=5)
        )
        _event(f"{key}-handed-off").set()

        # Heartbeats after the handoff reach the server too
        async def late_heartbeat() -> None:
            details = await heartbeat_details()
            assert details[0].startswith("late-")

        await assert_eventually(late_heartbeat)
        _event(f"{key}-continue").set()
        # No heartbeat timeout, so this is still the first attempt
        assert await handle.result() == ["1"]
    await _replay(handle, HeartbeatWorkflow)


@activity.defn
async def complete_async(key: str) -> str:
    _executions[key].append(activity.info().attempt)
    if activity.info().attempt == 1:
        _event(f"{key}-token").set()
        _tokens[key] = activity.info().task_token
        activity.raise_complete_async()
    return "completed normally"


_tokens: dict[str, bytes] = {}


@workflow.defn
class AsyncCompletionWorkflow:
    @workflow.run
    async def run(self, key: str) -> str:
        async with workflow.speculative():
            return await workflow.execute_activity(
                complete_async,
                key,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=50)),
            )


async def test_speculative_async_completion_unsupported(client: Client):
    supported = await _optimistic_start_supported(client)
    key = str(uuid.uuid4())
    async with new_worker(
        client, AsyncCompletionWorkflow, activities=[complete_async]
    ) as worker:
        handle = await client.start_workflow(
            AsyncCompletionWorkflow.run,
            key,
            id=f"wf-{key}",
            task_queue=worker.task_queue,
        )
        if supported:
            # The optimistic attempt can't complete asynchronously, so it fails
            # and the server's retry runs normally
            assert await handle.result() == "completed normally"
            assert _executions[key] == [1, 2]
            events = await _history(handle)
            started = next(
                e
                for e in events
                if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
            )
            failure = started.activity_task_started_event_attributes.last_failure
            assert failure.application_failure_info.type == (
                "OptimisticAsyncCompletionUnsupported"
            )
        else:
            await _event(f"{key}-token").wait()
            await client.get_async_activity_handle(task_token=_tokens[key]).complete(
                "completed asynchronously"
            )
            assert await handle.result() == "completed asynchronously"
    await _replay(handle, AsyncCompletionWorkflow)


@workflow.defn
class ScopeWorkflow:
    @workflow.run
    async def run(self) -> None:
        proceed = asyncio.Event()

        async def started_inside_block() -> None:
            await proceed.wait()
            await workflow.sleep(0.05)

        async with workflow.speculative():
            task = asyncio.create_task(started_inside_block())
            await workflow.sleep(0.05)
        # The task was created inside the block, but this timer starts after
        # the block exited
        proceed.set()
        await task


async def test_speculative_scope_ends_for_spawned_tasks(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(client, ScopeWorkflow) as worker:
        handle = await client.start_workflow(
            ScopeWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
    await _assert_history(
        handle,
        supported,
        [
            "WORKFLOW_EXECUTION_STARTED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "TIMER_STARTED optimistic_start",
            "TIMER_FIRED optimistic_resolution",
            "TIMER_STARTED",
            "TIMER_FIRED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "WORKFLOW_EXECUTION_COMPLETED",
        ],
    )
    await _replay(handle, ScopeWorkflow)


@workflow.defn
class SequentialBlocksWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        results = []
        for i in range(3):
            async with workflow.speculative():
                async with workflow.speculative():
                    results.append(
                        await workflow.execute_activity(
                            sleep_and_return,
                            args=[str(i), 0],
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                    )
        return results


async def test_speculative_sequential_blocks_share_window(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, SequentialBlocksWorkflow, activities=[sleep_and_return]
    ) as worker:
        handle = await client.start_workflow(
            SequentialBlocksWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == ["0", "1", "2"]
    await _assert_history(
        handle,
        supported,
        [
            "WORKFLOW_EXECUTION_STARTED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
        ]
        + [
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "ACTIVITY_TASK_STARTED optimistic_resolution",
            "ACTIVITY_TASK_COMPLETED optimistic_resolution",
        ]
        * 3
        + ["WORKFLOW_EXECUTION_COMPLETED"],
    )
    await _replay(handle, SequentialBlocksWorkflow)


@workflow.defn(name="SpeculativePatchWorkflow")
class DeprecatingPatchWorkflow:
    @workflow.run
    async def run(self) -> str:
        async with workflow.speculative():
            workflow.deprecate_patch("first")
            result = await workflow.execute_activity(
                sleep_and_return,
                args=["done", 0],
                start_to_close_timeout=timedelta(seconds=10),
            )
            workflow.deprecate_patch("second")
            await workflow.sleep(0.05)
        return result


@workflow.defn(name="SpeculativePatchWorkflow")
class PatchFreeWorkflow:
    @workflow.run
    async def run(self) -> str:
        async with workflow.speculative():
            result = await workflow.execute_activity(
                sleep_and_return,
                args=["done", 0],
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.sleep(0.05)
        return result


async def test_speculative_deprecated_patches_replay(client: Client):
    supported = await _optimistic_start_supported(client)
    histories = {}
    for wf in [DeprecatingPatchWorkflow, PatchFreeWorkflow]:
        async with new_worker(client, wf, activities=[sleep_and_return]) as worker:
            handle = await client.start_workflow(
                "SpeculativePatchWorkflow",
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert await handle.result() == "done"
        events = await _history(handle)
        assert supported == any(_optimistic_flags(e) for e in events)
        assert (wf is DeprecatingPatchWorkflow) == any(
            e.event_type == EventType.EVENT_TYPE_MARKER_RECORDED for e in events
        )
        histories[wf] = await handle.fetch_history()
    # Deprecated patches may be added to or removed from code that runs in a
    # window, both ways
    for wf in [DeprecatingPatchWorkflow, PatchFreeWorkflow]:
        for history in histories.values():
            await Replayer(workflows=[wf]).replay_workflow(history)


@workflow.defn
class TimerCancelWorkflow:
    @workflow.run
    async def run(self) -> str:
        async with workflow.speculative():
            try:
                await asyncio.wait_for(workflow.sleep(10), timeout=0.1)
                result = "slept"
            except asyncio.TimeoutError:
                result = "timed out"
            await workflow.sleep(0.05)
        return result


async def test_speculative_timer_cancellation(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(client, TimerCancelWorkflow) as worker:
        handle = await client.start_workflow(
            TimerCancelWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == "timed out"
    await _assert_history(
        handle,
        supported,
        [
            "WORKFLOW_EXECUTION_STARTED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "TIMER_STARTED optimistic_start",
            "TIMER_STARTED optimistic_start",
            "TIMER_FIRED optimistic_resolution",
            "TIMER_CANCELED",
            "TIMER_STARTED optimistic_start",
            "TIMER_FIRED optimistic_resolution",
            "WORKFLOW_EXECUTION_COMPLETED",
        ],
    )
    await _replay(handle, TimerCancelWorkflow)


@workflow.defn
class CancelBeforeSentWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        async with workflow.speculative():
            handle = workflow.start_activity(
                sleep_and_return,
                args=["never", 10],
                start_to_close_timeout=timedelta(seconds=30),
            )
            handle.cancel()
            try:
                first = await handle
            except asyncio.CancelledError:
                first = "cancelled"
            second = await workflow.execute_activity(
                sleep_and_return,
                args=["second", 0],
                start_to_close_timeout=timedelta(seconds=10),
            )
        return [first, second]


async def test_speculative_activity_cancelled_before_sent(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, CancelBeforeSentWorkflow, activities=[sleep_and_return]
    ) as worker:
        handle = await client.start_workflow(
            CancelBeforeSentWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == ["cancelled", "second"]
    await _assert_history(
        handle,
        supported,
        [
            "WORKFLOW_EXECUTION_STARTED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "ACTIVITY_TASK_STARTED optimistic_resolution",
            "ACTIVITY_TASK_COMPLETED optimistic_resolution",
            "WORKFLOW_EXECUTION_COMPLETED",
        ],
    )
    await _replay(handle, CancelBeforeSentWorkflow)


@workflow.defn
class QueryDuringWindowWorkflow:
    def __init__(self) -> None:
        self.progress: list[str] = []

    @workflow.run
    async def run(self, key: str) -> list[str]:
        async with workflow.speculative():
            self.progress.append(
                await workflow.execute_activity(
                    sleep_and_return,
                    args=["first", 0],
                    start_to_close_timeout=timedelta(seconds=10),
                )
            )
            self.progress.append(
                await workflow.execute_activity(
                    wait_for_test, key, start_to_close_timeout=timedelta(seconds=10)
                )
            )
        return self.progress

    @workflow.query
    def progress_so_far(self) -> list[str]:
        return self.progress


async def test_speculative_query_sees_progress_in_window(client: Client):
    key = str(uuid.uuid4())
    async with new_worker(
        client,
        QueryDuringWindowWorkflow,
        activities=[sleep_and_return, wait_for_test],
    ) as worker:
        handle = await client.start_workflow(
            QueryDuringWindowWorkflow.run,
            key,
            id=f"wf-{key}",
            task_queue=worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )
        await _event(f"{key}-started").wait()
        assert await handle.query(QueryDuringWindowWorkflow.progress_so_far) == [
            "first"
        ]
        _event(f"{key}-continue").set()
        assert await handle.result() == ["first", "activity"]
    await _replay(handle, QueryDuringWindowWorkflow)


@workflow.defn
class ShutdownDuringWindowWorkflow:
    @workflow.run
    async def run(self, key: str) -> str:
        async with workflow.speculative():
            return await workflow.execute_activity(
                wait_for_test, key, start_to_close_timeout=timedelta(seconds=10)
            )


# The activity completes during shutdown either inside the window, or after it
# was handed off at 80% of the task timeout
@pytest.mark.parametrize("complete_after", [0.2, 2.5])
async def test_speculative_worker_shutdown_during_window(
    client: Client, complete_after: float
):
    key = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    worker = new_worker(
        client,
        ShutdownDuringWindowWorkflow,
        activities=[wait_for_test],
        task_queue=task_queue,
        graceful_shutdown_timeout=timedelta(seconds=10),
        # Keeps the next workflow task off this worker's sticky queue, which
        # not every server reroutes after shutdown
        max_cached_workflows=0,
    )
    run_task = asyncio.create_task(worker.run())
    handle = await client.start_workflow(
        ShutdownDuringWindowWorkflow.run,
        key,
        id=f"wf-{key}",
        task_queue=task_queue,
        task_timeout=timedelta(seconds=2),
    )
    await _event(f"{key}-started").wait()
    shutdown = asyncio.create_task(worker.shutdown())
    await asyncio.sleep(complete_after)
    # Core holds an optimistic activity's completion until the workflow observed
    # it or it was handed off, which must not hold up shutdown
    _event(f"{key}-continue").set()
    await asyncio.wait_for(shutdown, timeout=10)
    await run_task

    async with new_worker(
        client,
        ShutdownDuringWindowWorkflow,
        activities=[wait_for_test],
        task_queue=task_queue,
    ):
        assert await handle.result() == "activity"
    assert _executions[key] == [1]
    await _replay(handle, ShutdownDuringWindowWorkflow)


@workflow.defn
class UpdateDuringWindowWorkflow:
    def __init__(self) -> None:
        self.observed: list[str] = []

    @workflow.run
    async def run(self, key: str) -> list[str]:
        async with workflow.speculative():
            self.observed.append(
                await workflow.execute_activity(
                    wait_for_test, key, start_to_close_timeout=timedelta(seconds=10)
                )
            )
        await workflow.wait_condition(lambda: "update" in self.observed)
        return self.observed

    @workflow.update
    def update(self) -> list[str]:
        self.observed.append("update")
        return list(self.observed)


async def test_speculative_update_delivered_after_checkpoint(client: Client):
    supported = await _optimistic_start_supported(client)
    key = str(uuid.uuid4())
    async with new_worker(
        client, UpdateDuringWindowWorkflow, activities=[wait_for_test]
    ) as worker:
        handle = await client.start_workflow(
            UpdateDuringWindowWorkflow.run,
            key,
            id=f"wf-{key}",
            task_queue=worker.task_queue,
        )
        await _event(f"{key}-started").wait()
        update = asyncio.create_task(
            handle.execute_update(UpdateDuringWindowWorkflow.update)
        )
        if not supported:
            assert await update == ["update"]
        else:
            # Delivered only after the checkpoint, which waits for the activity
            await asyncio.sleep(0.5)
            assert not update.done()
        _event(f"{key}-continue").set()
        if supported:
            assert await update == ["activity", "update"]
        assert await handle.result() == (
            ["activity", "update"] if supported else ["update", "activity"]
        )
    await _replay(handle, UpdateDuringWindowWorkflow)


@workflow.defn
class CancelledDuringWindowWorkflow:
    @workflow.run
    async def run(self, key: str) -> None:
        async with workflow.speculative():
            await workflow.execute_activity(
                wait_for_test, key, start_to_close_timeout=timedelta(seconds=10)
            )
            await workflow.sleep(60)


async def test_speculative_workflow_cancelled_during_window(client: Client):
    supported = await _optimistic_start_supported(client)
    key = str(uuid.uuid4())
    async with new_worker(
        client, CancelledDuringWindowWorkflow, activities=[wait_for_test]
    ) as worker:
        handle = await client.start_workflow(
            CancelledDuringWindowWorkflow.run,
            key,
            id=f"wf-{key}",
            task_queue=worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )
        await _event(f"{key}-started").wait()
        await handle.cancel()
        _event(f"{key}-continue").set()
        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()
        assert isinstance(err.value.cause, CancelledError)
    summary = _summarize(await _history(handle))
    assert summary[-1] == "WORKFLOW_EXECUTION_CANCELED"
    if supported:
        # The cancellation waited for the checkpoint, at the latest when the
        # window's sleep was handed off
        cancel_requested = summary.index("WORKFLOW_EXECUTION_CANCEL_REQUESTED")
        assert summary[4:cancel_requested] == [
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "ACTIVITY_TASK_STARTED optimistic_resolution",
            "ACTIVITY_TASK_COMPLETED optimistic_resolution",
            "TIMER_STARTED optimistic_start",
        ]
    await _replay(handle, CancelledDuringWindowWorkflow)


@workflow.defn
class ContinueAsNewInBlockWorkflow:
    @workflow.run
    async def run(self, iteration: int) -> int:
        async with workflow.speculative():
            await workflow.execute_activity(
                sleep_and_return,
                args=["x", 0],
                start_to_close_timeout=timedelta(seconds=10),
            )
            if iteration < 2:
                workflow.continue_as_new(iteration + 1)
        return iteration


async def test_speculative_continue_as_new(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, ContinueAsNewInBlockWorkflow, activities=[sleep_and_return]
    ) as worker:
        handle = await client.start_workflow(
            ContinueAsNewInBlockWorkflow.run,
            0,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == 2
    first_run = client.get_workflow_handle(
        handle.id, run_id=handle.first_execution_run_id
    )
    await _assert_history(
        first_run,
        supported,
        [
            "WORKFLOW_EXECUTION_STARTED",
            "WORKFLOW_TASK_SCHEDULED",
            "WORKFLOW_TASK_STARTED",
            "WORKFLOW_TASK_COMPLETED",
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "ACTIVITY_TASK_STARTED optimistic_resolution",
            "ACTIVITY_TASK_COMPLETED optimistic_resolution",
            "WORKFLOW_EXECUTION_CONTINUED_AS_NEW",
        ],
    )
    await _replay(first_run, ContinueAsNewInBlockWorkflow)
    await _replay(handle, ContinueAsNewInBlockWorkflow)


@workflow.defn
class ChildWorkflow:
    @workflow.run
    async def run(self) -> str:
        return "child"


@workflow.defn
class ChildInBlockWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        async with workflow.speculative():
            before = await workflow.execute_activity(
                sleep_and_return,
                args=["before", 0],
                start_to_close_timeout=timedelta(seconds=10),
            )
            child = await workflow.execute_child_workflow(
                ChildWorkflow.run, id=f"{workflow.info().workflow_id}-child"
            )
            after = await workflow.execute_activity(
                sleep_and_return,
                args=["after", 0],
                start_to_close_timeout=timedelta(seconds=10),
            )
        return [before, child, after]


async def test_speculative_child_workflow_in_block(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client,
        ChildInBlockWorkflow,
        ChildWorkflow,
        activities=[sleep_and_return],
    ) as worker:
        handle = await client.start_workflow(
            ChildInBlockWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == ["before", "child", "after"]
    events = await _history(handle)
    summary = _summarize(events)
    if supported:
        # The child can't be deferred, so it checkpoints the window, and the
        # block's next activity opens a new one
        assert summary[4:8] == [
            "ACTIVITY_TASK_SCHEDULED optimistic_start",
            "ACTIVITY_TASK_STARTED optimistic_resolution",
            "ACTIVITY_TASK_COMPLETED optimistic_resolution",
            "START_CHILD_WORKFLOW_EXECUTION_INITIATED",
        ]
        assert summary.count("ACTIVITY_TASK_SCHEDULED optimistic_start") == 2
    else:
        assert not any(_optimistic_flags(e) for e in events)
    await _replay(handle, ChildInBlockWorkflow, ChildWorkflow)


@workflow.defn
class OtherTaskQueueWorkflow:
    @workflow.run
    async def run(self, other_task_queue: str) -> list[str]:
        def run_activity(name: str, task_queue: str | None = None):
            return workflow.execute_activity(
                sleep_and_return,
                args=[name, 0],
                task_queue=task_queue,
                start_to_close_timeout=timedelta(seconds=10),
            )

        async with workflow.speculative():
            other = await run_activity("other queue", other_task_queue)
            own = await run_activity("own queue")
        return [other, own]


async def test_speculative_other_task_queue_runs_normally(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, OtherTaskQueueWorkflow, activities=[sleep_and_return]
    ) as worker:
        async with new_worker(client, activities=[sleep_and_return]) as other:
            handle = await client.start_workflow(
                OtherTaskQueueWorkflow.run,
                other.task_queue,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert await handle.result() == ["other queue", "own queue"]
    flags = [
        _optimistic_flags(e)
        for e in await _history(handle)
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    assert flags == [[], ["optimistic_start"] if supported else []]
    await _replay(handle, OtherTaskQueueWorkflow)


async def test_speculative_without_free_activity_slots(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client,
        ExampleWorkflow,
        activities=[sleep_and_return],
        # The poller holds the only slot, as with eager activities
        max_concurrent_activities=1,
    ) as worker:
        handle = await client.start_workflow(
            ExampleWorkflow.run,
            0.1,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # With one slot the activities run one after the other, in whatever
        # order they're dispatched
        result = await handle.result()
        assert sorted(result[:2]) == ["a1", "a2"] and result[2] == "a3"
    events = await _history(handle)
    assert not any(
        _optimistic_flags(e)
        for e in events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    )
    # The timer doesn't need a slot
    assert supported == any(
        _optimistic_flags(e)
        for e in events
        if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
    )
    await _replay(handle, ExampleWorkflow)


@activity.defn
async def slow_first_attempt() -> int:
    if activity.info().attempt == 1:
        await asyncio.sleep(3)
    return activity.info().attempt


@workflow.defn
class StartToCloseTimeoutWorkflow:
    @workflow.run
    async def run(self) -> int:
        async with workflow.speculative():
            return await workflow.execute_activity(
                slow_first_attempt,
                start_to_close_timeout=timedelta(seconds=0.5),
                retry_policy=RetryPolicy(initial_interval=timedelta(milliseconds=50)),
            )


async def test_speculative_activity_start_to_close_timeout(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, StartToCloseTimeoutWorkflow, activities=[slow_first_attempt]
    ) as worker:
        handle = await client.start_workflow(
            StartToCloseTimeoutWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == 2
    events = await _history(handle)
    if supported:
        assert "optimistic_start" in _optimistic_flags(
            next(
                e
                for e in events
                if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            )
        )
        # Timeouts stay server-generated, also for an optimistic attempt
        started = next(
            e
            for e in events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
        )
        assert started.activity_task_started_event_attributes.last_failure.HasField(
            "timeout_failure_info"
        )
    await _replay(handle, StartToCloseTimeoutWorkflow)


async def test_speculative_example_without_cache(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client,
        ExampleWorkflow,
        activities=[sleep_and_return],
        max_cached_workflows=0,
    ) as worker:
        handle = await client.start_workflow(
            ExampleWorkflow.run,
            0.1,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # The workflow is replayed from history before each task
        assert await handle.result() == ["a2", "a1", "a3"]
    events = await _history(handle)
    assert supported == any(_optimistic_flags(e) for e in events)
    await _replay(handle, ExampleWorkflow)


@workflow.defn
class PatchedAndUpsertWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        async with workflow.speculative():
            first = await workflow.execute_activity(
                sleep_and_return,
                args=["first", 0],
                start_to_close_timeout=timedelta(seconds=10),
            )
            patched = workflow.patched("in-window")
            workflow.upsert_memo({"memo": "set in window"})
            second = await workflow.execute_activity(
                sleep_and_return,
                args=["second", 0],
                start_to_close_timeout=timedelta(seconds=10),
            )
        return [first, str(patched), second]


async def test_speculative_patched_and_upsert_in_window(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, PatchedAndUpsertWorkflow, activities=[sleep_and_return]
    ) as worker:
        handle = await client.start_workflow(
            PatchedAndUpsertWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == ["first", "True", "second"]
    summary = _summarize(await _history(handle))
    if supported:
        # Markers and workflow property changes don't end the window
        assert summary.count("WORKFLOW_TASK_COMPLETED") == 1
    assert await (await handle.describe()).memo_value("memo") == "set in window"
    await _replay(handle, PatchedAndUpsertWorkflow)


@activity.defn
async def report_timeouts() -> list[float | None]:
    info = activity.info()
    return [
        t.total_seconds() if t is not None else None
        for t in [
            info.schedule_to_close_timeout,
            info.start_to_close_timeout,
            info.heartbeat_timeout,
        ]
    ]


@workflow.defn
class ActivityTimeoutsWorkflow:
    @workflow.run
    async def run(self) -> list[list[float | None]]:
        results = []
        for heartbeat_timeout in [None, timedelta(seconds=20)]:
            normal = await workflow.execute_activity(
                report_timeouts,
                start_to_close_timeout=timedelta(seconds=10),
                heartbeat_timeout=heartbeat_timeout,
            )
            async with workflow.speculative():
                optimistic = await workflow.execute_activity(
                    report_timeouts,
                    start_to_close_timeout=timedelta(seconds=10),
                    heartbeat_timeout=heartbeat_timeout,
                )
            results += [normal, optimistic]
        return results


async def test_speculative_activity_info_timeouts(client: Client):
    supported = await _optimistic_start_supported(client)
    async with new_worker(
        client, ActivityTimeoutsWorkflow, activities=[report_timeouts]
    ) as worker:
        handle = await client.start_workflow(
            ActivityTimeoutsWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            run_timeout=timedelta(seconds=60),
        )
        results = await handle.result()
    normal, optimistic, normal_heartbeat, optimistic_heartbeat = results
    # Optimistic tasks get the timeouts the server fills in on the tasks it
    # dispatches, ex: schedule-to-close from the run timeout
    assert optimistic == normal == [60.0, 10.0, None]
    assert optimistic_heartbeat == normal_heartbeat
    if supported:
        # The server caps the heartbeat timeout at start-to-close
        assert optimistic_heartbeat == [60.0, 10.0, 10.0]
    flags = [
        _optimistic_flags(e)
        for e in await _history(handle)
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    expected = ["optimistic_start"] if supported else []
    assert flags == [[], expected, [], expected]
    await _replay(handle, ActivityTimeoutsWorkflow)
