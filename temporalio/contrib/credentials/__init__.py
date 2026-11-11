"""Secure credential propagation.

Carries credentials, such as a user's token, through Temporal calls: from a
Client into a Workflow, and from the Workflow to the Activities, child
Workflows, and Nexus operations that act on the user's behalf. Each credential
travels sealed to the receiver's key and bound to the exact call it travels
with. Python applications run the binding layer, which seals and opens
credentials, in Temporal Proxy.

.. warning::
    This module is experimental. Its API and wire format can change.

Usage::

    from temporalio.contrib import credentials

    client = await Client.connect("proxy:7233", plugins=[credentials.Plugin(proxy=True)])

    with credentials.attach(user_token=token):
        await client.execute_workflow(Agent.run, input, id=..., task_queue=...)

    @workflow.defn
    class Agent:
        @workflow.run
        @credentials.requires("user_token")
        async def run(self, input: Input) -> None:
            with credentials.propagate("user_token"):
                await workflow.execute_activity(call_tool, input, ...)

    @activity.defn
    async def call_tool(input: Input) -> None:
        token = credentials.get("user_token")
        await call_service(input, token.reveal())
"""

from temporalio.contrib.credentials import jwt
from temporalio.contrib.credentials._api import (
    ERROR_TYPE_CREDENTIAL_MISSING,
    Credential,
    RetainPolicy,
    accepts,
    attach,
    attach_with_key,
    discard,
    get,
    if_absent,
    lookup,
    propagate,
    propagate_all,
    propagate_with_key,
    requires,
    revokes,
)
from temporalio.contrib.credentials._link import is_credentials_payload
from temporalio.contrib.credentials._plugin import Plugin

__all__ = [
    "ERROR_TYPE_CREDENTIAL_MISSING",
    "Credential",
    "Plugin",
    "RetainPolicy",
    "accepts",
    "attach",
    "attach_with_key",
    "discard",
    "get",
    "if_absent",
    "is_credentials_payload",
    "jwt",
    "lookup",
    "propagate",
    "propagate_all",
    "propagate_with_key",
    "requires",
    "revokes",
]
