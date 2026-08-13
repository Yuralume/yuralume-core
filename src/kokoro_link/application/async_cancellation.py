"""asyncio cancellation helpers shared by the long-lived workers.

Deliberately *not* under ``services/`` and not in any domain package:
there is nothing about messaging, accounts or gateways in here. It is a
pure asyncio idiom that any worker with a "cancel a child task inside a
``finally``" shape needs, so it sits next to the other layer-neutral
application modules (``exceptions``, ``media_content_types``) rather
than being owned by whichever service happened to need it first.
"""

from __future__ import annotations

import asyncio

__all__ = ["reraise_own_cancellation"]


def reraise_own_cancellation(task: asyncio.Task[None] | None) -> None:
    """Re-raise a cancellation ``contextlib.suppress`` may have eaten.

    The shape this exists for::

        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher
            await release_the_lock()
            reraise_own_cancellation(current_task)

    ``refresher.cancel()`` followed immediately by ``await refresher`` is
    a genuine suspension point — the refresher has not been scheduled
    yet, so awaiting it yields to the event loop. A ``stop()`` landing in
    that window cancels *this* task, and the resulting ``CancelledError``
    is indistinguishable from the child's, so the ``suppress`` swallows
    it and the outer loop runs on: ``stop()`` never returns and the
    worker keeps dialling the platform after shutdown. Measured on the
    Discord loop, the cancelled task went on to run ~66k more iterations;
    on WhatsApp it re-dialled the sidecar ~8.7k times. In tests the same
    bug is not a failure but a hang.

    **Call it after the cleanup that must not be skipped** (releasing a
    lock, closing a client). Raising earlier converts "cancellation was
    swallowed" into "cleanup was skipped", which for a gateway lock means
    the account is stranded until its TTL expires.

    ``Task.cancelling()`` counts cancellation requests not yet undone by
    ``uncancel()``, so it is exactly the "was that error mine?" question,
    and it stays set as long as nothing calls ``uncancel``.
    """
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
