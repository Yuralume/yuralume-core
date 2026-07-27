"""Persistence boundary for the album gacha's pending candidate batch.

``CharacterImageService.generate_candidates`` writes N throwaway images to
object storage and hands their URLs to the operator; a later
``commit_candidates`` request decides which of them to keep. Object storage
has no list-by-prefix operation (see :mod:`kokoro_link.contracts.object_storage`),
so the batch's object keys have to be remembered somewhere between the two
requests.

That "somewhere" cannot be process memory: hosted runs several api replicas
behind a load balancer, so generate and commit routinely hit different
processes — an in-process map loses the player's picks outright and leaks the
uncommitted objects. Hence this port: one *active* batch per character, shared
by every replica.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class CharacterImageCandidateBatchRepositoryPort(Protocol):
    """The pending candidate object keys of a character's latest batch.

    Writes come in two flavours, and picking the wrong one costs a player
    their candidates:

    - **Unconditional** (``expected_keys=None``) — "this batch is mine, I
      just created it". Last writer wins.
    - **Conditional** (``expected_keys=<what get returned>``) — "act on the
      batch I read a moment ago". The write applies only while that batch is
      still the live one, so a slow replica cannot delete or shrink a batch
      another replica generated in the meantime. Every read-then-write
      sequence must use this form; there is no row lock held across the two
      HTTP requests a batch's life spans, and the keys themselves are the
      version marker.
    """

    async def get(self, character_id: str) -> tuple[str, ...]:
        """Return the active batch's object keys in generation order.

        Empty tuple when the character has no pending batch — callers treat
        that as "nothing to commit" rather than an error.
        """

    async def replace(
        self,
        character_id: str,
        object_keys: Sequence[str],
        *,
        expected_keys: Sequence[str] | None = None,
    ) -> bool:
        """Make ``object_keys`` the character's one active batch.

        Upsert semantics: a character has at most one pending batch, and a
        second generate simply supersedes the first. An empty sequence is
        equivalent to :meth:`clear`.

        ``expected_keys`` turns this into a compare-and-swap against the exact
        (ordered) key list currently stored; the write is skipped when they no
        longer match. Returns whether the write applied — ``False`` means the
        batch was superseded and the caller must not retry blindly (its own
        objects are now unreferenced, i.e. best-effort orphans worth logging).
        An empty ``expected_keys`` never matches, because a stored batch is
        never empty.
        """

    async def clear(
        self,
        character_id: str,
        *,
        expected_keys: Sequence[str] | None = None,
    ) -> bool:
        """Drop the active batch. Idempotent — no batch is not an error.

        With ``expected_keys`` the delete is conditional in the same sense as
        :meth:`replace`; returns whether a row was actually removed. The
        unconditional form always reports ``True``: it has no expectation to
        disappoint.
        """
