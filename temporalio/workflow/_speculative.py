"""Speculative blocks, in which activities and timers may start optimistically.

.. warning::
    Speculative blocks are an experimental API and may change without notice.
"""

from __future__ import annotations

import contextvars
from contextlib import AbstractAsyncContextManager
from types import TracebackType

from ._context import _Runtime

__all__ = [
    "speculative",
    "_in_speculative_scope",
]


class _SpeculativeScope:
    """State shared by a speculative block and every task created inside it.

    Tasks copy the context when they are created, so they keep referencing this
    object after the block exits. Clearing ``active`` instead of only resetting
    the context variable is what ends the block for those tasks too.
    """

    __slots__ = ("active", "parent")

    def __init__(self, parent: _SpeculativeScope | None) -> None:
        self.active = True
        self.parent = parent


_current_speculative_scope: contextvars.ContextVar[_SpeculativeScope | None] = (
    contextvars.ContextVar("__temporal_speculative_scope", default=None)
)


def _in_speculative_scope() -> bool:
    """Whether commands issued from the current context are speculative."""
    scope = _current_speculative_scope.get()
    # A task created inside an inner block that already exited is still inside
    # any enclosing block that hasn't.
    while scope is not None:
        if scope.active:
            return True
        scope = scope.parent
    return False


class _SpeculativeBlock(AbstractAsyncContextManager[None]):
    def __init__(self) -> None:
        self._runtime: _Runtime | None = None
        self._scope: _SpeculativeScope | None = None
        self._token: contextvars.Token[_SpeculativeScope | None] | None = None

    async def __aenter__(self) -> None:
        if self._scope is not None:
            raise RuntimeError("A speculative block cannot be entered more than once")
        runtime = _Runtime.current()
        runtime.workflow_enter_speculative_block()
        self._runtime = runtime
        self._scope = _SpeculativeScope(_current_speculative_scope.get())
        self._token = _current_speculative_scope.set(self._scope)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Must not await: code after the block has to run in the same
        # activation as the exit, which is what lets a loop of sequential blocks
        # keep one speculation window going.
        assert self._scope and self._token and self._runtime
        if not self._scope.active:
            return
        self._scope.active = False
        try:
            _current_speculative_scope.reset(self._token)
        except ValueError:
            # Unwinding from a context other than the one the block was entered
            # in, which happens when a coroutine suspended inside the block is
            # closed rather than resumed. That context is being discarded.
            pass
        self._runtime.workflow_exit_speculative_block()


def speculative() -> AbstractAsyncContextManager[None]:
    """Run part of a workflow in a speculative block.

    Activities on the workflow's own task queue and timers (including
    :py:func:`sleep`, :py:func:`asyncio.sleep` and other event loop timers)
    started inside a speculative block may start optimistically: the worker
    begins executing them right away, before the server knows about them, and
    can deliver their results to the workflow within the same workflow task.
    This saves a server round trip per activity or timer, while history still
    records ordinary activity and timer events. The SDK reports them to the
    server at the next checkpoint, at the latest at about 80% of the workflow
    task timeout, and anything still running then continues as a normal
    server-owned activity or timer.

    Whether a command actually starts optimistically is up to the SDK (the
    server must support it, an activity slot must be free, and so on), and
    workflow code behaves the same either way. Child workflows, Nexus
    operations and activities on other task queues are never started
    optimistically.

    The block follows the current context, so tasks created inside it are
    covered too, until the block exits.

    Progress inside a block is not durable until the next checkpoint. If the
    worker crashes, activities since the last checkpoint run again without
    their attempt increasing, as with local activities. Signals, updates and
    other events that arrive in the meantime are delivered after the
    checkpoint.

    Local activities can't be scheduled inside a speculative block. Doing so
    fails the workflow task. Optimistically started activities can't complete
    asynchronously: such an attempt fails and the server retries it.

    Example::

        async with workflow.speculative():
            await workflow.execute_activity(
                activity1, start_to_close_timeout=timedelta(seconds=5)
            )
            await workflow.sleep(3)
            await workflow.execute_activity(
                activity2, start_to_close_timeout=timedelta(seconds=5)
            )

    Returns:
        An async context manager for the block. It can be entered once.

    .. warning::
        Speculative blocks are an experimental API and may change without
        notice.
    """
    return _SpeculativeBlock()
