# Secure credential propagation

`temporalio.contrib.credentials` carries credentials, such as a user's token, through Temporal calls: from a client into a Workflow, and from the Workflow to the Activities, child Workflows, and Nexus operations that act on the user's behalf.

Each credential travels in a header sealed to the receiver's public key and bound to the exact call it travels with: the namespace, the Workflow, Update, Activity, or Nexus operation it addresses, and a digest of the call's payload bytes. Temporal never sees a credential, and a header moved to another call, or paired with other input, doesn't open. Nothing is forwarded unless the Workflow asks for it.

Python applications run the binding layer, which seals and opens credentials, in [Temporal Proxy](https://github.com/temporalio/temporal-proxy). The proxy holds the keys; the application holds none.

> [!NOTE]
> This module is experimental. Its API and wire format can change.

## Using it

### Configure the client

```python
from temporalio.client import Client
from temporalio.contrib import credentials
from temporalio.worker import Worker

client = await Client.connect("proxy:7233", plugins=[credentials.Plugin(proxy=True)])
worker = Worker(client, task_queue="agents", workflows=[Agent], activities=[call_tool])  # inherits the plugin
```

Set the plugin on the client, not only on a Worker: a Worker whose client doesn't have it raises `ValueError`.

> [!WARNING]
> The application sends credentials to the proxy in plaintext and can't check that the server is one. Temporal Proxy identifies itself with a `credentials-binding: v1` response header, but the Python SDK doesn't expose response headers. Connected straight to a Temporal Service, the application stores credentials in plaintext in Workflow history.

### Attach credentials to a call

```python
with credentials.attach(user_token=token):
    await client.execute_workflow(Agent.run, input, id=agent_id, task_queue="agents")
```

`attach` works for starting and signaling Workflows, signal-with-start, Updates, update-with-start, and starting standalone Activities and Nexus operations. Queries never carry credentials. A call carries the values from the innermost `attach`; they don't merge with outer ones. `attach_with_key("payments", user_token=token)` seals them to a named key instead of the default.

### Forward credentials from a Workflow

```python
@workflow.defn
class Agent:
    @workflow.run
    @credentials.requires("user_token")  # a start without the token fails the Workflow
    async def run(self, input: Input) -> None:
        with credentials.propagate("user_token"):
            await workflow.execute_activity(call_tool, input, start_to_close_timeout=timedelta(minutes=1))
```

- Calls made inside `propagate(*names)` forward exactly the named credentials. Calls made outside forward nothing, and `propagate()` with no names forwards nothing.
- `propagate_all()` forwards everything available. `propagate_with_key(key, *names)` seals to a named key.
- They work for Activities, Local Activities, child Workflows, continue-as-new, Signals to other Workflows, and Nexus operations.

### Use a credential in an Activity or Nexus handler

```python
@activity.defn
async def call_tool(input: Input) -> None:
    token = credentials.get("user_token")  # non-retryable error if the call didn't carry it
    await call_service(input, token.reveal())
```

A `Credential` shows `[REDACTED]` under `str`, `repr`, and formatting, and can't be serialized as a payload or pickled. In a Nexus handler, `get` raises a non-retryable `nexusrpc.HandlerError`. Credentials live in context variables, so Activities that run in a process pool can't read them.

### Keep credentials in a Workflow

```python
@workflow.defn
class Agent:
    # Keeps the approver's credential after the Update, so the run method can forward it.
    @workflow.update
    @credentials.requires("approver_token", retain=credentials.if_absent)
    async def approve(self) -> None: ...

    # Replaces the session's token with a refreshed one for the same user.
    @workflow.update
    @credentials.requires("user_token", retain=credentials.jwt.same_subject)
    async def refresh(self) -> None: ...

    # Drops the user's token from the Workflow.
    @workflow.signal
    @credentials.revokes("user_token")
    def logout(self) -> None: ...
```

| Declaration or call | Where | What it does |
| --- | --- | --- |
| `@requires(*names)` | run method, Signal and Update handlers, Activities, Nexus handlers | rejects the call unless it carried each name |
| `@requires(*names, retain=policy)` | Signal and Update handlers | also rejects it if `policy` declines; otherwise keeps it for the run |
| `@accepts(*names, retain=policy)` | Signal and Update handlers | keeps each one `policy` allows; never rejects |
| `@revokes(*names)` | Signal and Update handlers | drops them from what the Workflow holds |
| `get`, `lookup` | anywhere | the current call, then what the Workflow holds (in the run method, only what it holds) |
| `discard(*names)` | Workflow code | drops them from what the Workflow holds |

Put these decorators below the Temporal decorator. The run method keeps every credential the start carried. Retain policies are `credentials.if_absent` and `credentials.jwt.same_subject`, which replaces a held JWT with one for the same `iss` and `sub`, without verifying either token.

When a check fails:

- **Workflow start:** the Workflow fails with a non-retryable `ApplicationError` of type `CredentialMissing` on its first Workflow task.
- **Update:** it's rejected in its validator. The plugin adds a validator to Update handlers declared on the Workflow class that have none, because Python runs interceptors at the validation step only for handlers with a validator. Give handlers you set with `workflow.set_update_handler` a validator yourself. The check runs again when the handler starts, because validators don't run on replay: if it fails there, history was altered or the declarations changed without versioning, and the Workflow task fails.
- **Signal:** it's dropped before the handler runs, and logged. The sender gets no error, so prefer Updates when the sender needs to know.
- **Activity or Nexus operation:** it fails with a non-retryable error before the handler runs.

Errors name the credential, never the cause; the cause goes to Worker logs. Declarations are Workflow logic: change them with Worker Versioning or a new handler name.

## What stays plaintext inside an application

The link between the application and the proxy is plaintext, and must be trusted: local, or mutually authenticated TLS. Inside the application, credentials sit in Temporal headers:

- Client and Worker interceptors see them. Don't log headers, or whole requests and responses.
- History read through the proxy carries opened credentials, including `fetch_history` and histories saved from it. Treat those histories as secrets. The `Replayer` uses the opened credentials in them as they are.

## Things to know

- **Reserved headers.** `credentials`, `credentials-key`, and `credentials-opened` belong to this module. The plugin replaces values under these names on outgoing calls.
- **Header codecs.** With `header_codec_behavior` other than `NO_CODEC`, a payload codec visits header payloads. It must pass through payloads for which `credentials.is_credentials_payload` is true, or credentials won't reach the proxy in a form it seals.
- **Core-based Workers.** The proxy seals credentials a Workflow forwards using the `resource_id` that Workflow task completions carry. That needs an SDK Core change that isn't released yet. Without it, a Workflow task that forwards credentials fails and keeps retrying.
- **Plugin ordering.** The plugin puts its interceptor first, so other interceptors can call `get`, and its Workflow outbound interceptor runs last.
- **System Nexus operations**, on the `__temporal_system` endpoint, don't forward credentials.

## Testing

`temporalio.contrib.credentials.testing` helps with `WorkflowEnvironment` and `ActivityEnvironment`, which have no binding layer:

```python
from temporalio.contrib.credentials.testing import LoopbackPlugin, activity_credentials

config = env.client.config()
config["plugins"] = [LoopbackPlugin()]
client = Client(**config)

with activity_credentials(user_token="alice"):
    await ActivityEnvironment().run(call_tool, input)
```

With `LoopbackPlugin`, calls carry credentials as if a binding layer had sealed and opened them, so tests can assert what each Activity, child Workflow, or Nexus operation receives. Nothing is sealed: credentials are stored in plaintext in the test server's history.
