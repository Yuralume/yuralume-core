"""DIGEST_OFFPATH — forget the digest the reversed turn budgeted.

The material digest is no longer computed on the chat turn: each
post-turn distils that turn's emotion events, reflections, story material
and feed posts into bullets and leaves them in a row for the *next*
turn's prompt to read.

Which means a reversed turn can leave a trace nothing else in this
rollback touches. The post-turn's own writes are deleted by the steps
above — but the bullets it distilled from them live in their own row, and
without this step the next prompt would still be told about the memories,
moods and beats of a turn the player just took back.

The step deletes nothing recoverable: the row it drops is rebuilt by the
next post-turn, and until then the prompt renders the source blocks it
always could. So it forgets by character, not by (character, operator) —
the undo journal names a character and nothing else, and over-forgetting
costs one turn of source-block rendering while under-forgetting is the bug
this file exists to prevent.

That the digest lives in a table rather than in process memory is what
makes this step work at all on hosted: the undo runs in the API process
and the post-turn that wrote the row ran on a worker, so a memory-local
clear here would be clearing something the writer never touched.

It runs **last**: an in-flight post-turn that slipped past the tombstone
between the gate and its final write would otherwise re-write the row
after an earlier invalidation had cleared it.
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)


class MaterialDigestCacheInvalidateStep(UndoStep):
    name = "material_digest_cache"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        cache = context.deps.material_digest_cache
        if cache is None:
            return
        await cache.invalidate(context.journal.character_id)
