from temporal_sdk_core_bridge import (
    core_new,
    register_worker,
    poll_workflow_activation,
    complete_workflow_activation,
)
import asyncio
from temporalio.proto.temporal.sdk.core.workflow_activation.workflow_activation_pb2 import (
    WorkflowActivation,
)
from temporalio.proto.temporal.sdk.core.workflow_completion.workflow_completion_pb2 import (
    WorkflowActivationCompletion,
    Success,
)
from temporalio.proto.temporal.sdk.core.workflow_commands.workflow_commands_pb2 import (
    WorkflowCommand,
    CompleteWorkflowExecution,
)


async def main():
    core = await core_new()
    worker = await register_worker(core, "py")
    buf = await poll_workflow_activation(worker)
    activation = WorkflowActivation()
    activation.ParseFromString(buf)
    print(f"<< {activation=}")
    completion = WorkflowActivationCompletion(
        run_id=activation.run_id,
        task_queue="py",
        successful=Success(
            commands=[
                WorkflowCommand(complete_workflow_execution=CompleteWorkflowExecution())
            ]
        ),
    )
    print(f">> {completion=}")
    buf = completion.SerializeToString()
    await complete_workflow_activation(worker, buf)


asyncio.run(main())
