"""Memory repository port.

Storage-agnostic interface for ``MemoryItem`` persistence. Callers build
prompts by querying structured items; they should not format or flatten
items before handing them to the prompt builder.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, Union

from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kokoro_link.domain.value_objects.actor import ParticipantRef


WorldScope = Union[str, None, Literal["all"]]
"""How a memory query should treat the legacy ``world_id`` column:

- ``"all"`` (default) — no filtering. Used by maintenance flows that
  need every stored memory.
- ``None`` — only memories with ``world_id IS NULL``. Normal chat uses
  this after the old world system was removed, so stale world-scoped rows
  do not leak into standalone prompts.
- ``"<world-id>"`` — retained for compatibility with historical rows and
  tests; no current production path creates new world-scoped memories."""


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    """A memory item plus the cosine similarity to the query vector.

    ``similarity`` is in ``[-1.0, 1.0]`` — we map pgvector's cosine
    *distance* (``0..2``) to ``1 - distance`` at the repository
    boundary so callers always see higher-is-better scores.
    """

    item: MemoryItem
    similarity: float


@dataclass(frozen=True, slots=True)
class MemorySummary:
    """Read-model for the memory-browsing UI — every field it renders,
    and deliberately nothing else.

    Why this exists instead of reusing ``MemoryItem``: the entity carries
    two ``Vector(1024)`` columns, and the browse response emits neither.
    It reports only *whether* the content vector exists. Hydrating the
    entity to answer that meant Postgres shipping — and Python
    ``float()``-coercing, element by element — millions of floats per
    listing that were then dropped on the floor. ``has_embedding`` is
    resolved by the storage engine instead, so the vectors never leave
    the database (plan D9).

    A projection type rather than a flag on ``list_all_for_character``:
    the seven consolidation / reflection / coherence callers of that
    method genuinely need the vectors, and a boolean parameter that
    silently changes the return type is how those callers would break.
    """

    id: str
    character_id: str
    conversation_id: str | None
    kind: MemoryKind
    content: str
    salience: float
    tags: tuple[str, ...]
    created_at: datetime
    last_accessed_at: datetime | None
    access_count: int
    has_embedding: bool
    """``True`` when the *content* vector is present. The auxiliary tag
    vector does not count — it is a recall booster, not the thing whose
    absence makes semantic search skip the row."""

    @classmethod
    def from_item(cls, item: MemoryItem) -> "MemorySummary":
        """Project an already-hydrated entity.

        For adapters that hold ``MemoryItem`` objects anyway (the
        in-process repository). The SQL adapter never takes this path —
        materialising the entity is precisely the cost it avoids — but
        both must agree on what ``has_embedding`` means, so the rule
        lives here once.
        """
        return cls(
            id=item.id,
            character_id=item.character_id,
            conversation_id=item.conversation_id,
            kind=item.kind,
            content=item.content,
            salience=item.salience,
            tags=tuple(item.tags),
            created_at=item.created_at,
            last_accessed_at=item.last_accessed_at,
            access_count=item.access_count,
            has_embedding=item.embedding is not None,
        )


class MemoryRepositoryPort(Protocol):
    async def add(self, item: MemoryItem) -> MemoryItem:
        """Persist a new memory item and return the stored representation."""

    async def add_many(self, items: Sequence[MemoryItem]) -> list[MemoryItem]:
        """Persist multiple memory items atomically where possible."""

    async def query(
        self,
        character_id: str,
        *,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 20,
        min_salience: float = 0.0,
        world_scope: WorldScope = "all",
    ) -> list[MemoryItem]:
        """Return recent memory items ordered by recency (newest first).

        Implementations should filter by ``kinds`` and ``min_salience``
        before applying ``limit``. Ranking for prompt use happens in a
        dedicated ranker, not here.

        ``world_scope`` lets callers honour the world-system isolation
        rules — see :data:`WorldScope`.
        """

    async def query_semantic(
        self,
        character_id: str,
        query_embedding: Sequence[float],
        *,
        kinds: Sequence[MemoryKind] | None = None,
        limit: int = 20,
        min_salience: float = 0.0,
        world_scope: WorldScope = "all",
    ) -> list[ScoredMemory]:
        """Return memories nearest ``query_embedding`` in vector space.

        Rows without an embedding are skipped. The result is ordered by
        descending similarity; callers typically feed it to the hybrid
        ranker which blends similarity with salience × recency.

        ``world_scope`` honours world isolation — see :data:`WorldScope`.
        """

    async def list_all_for_character(
        self,
        character_id: str,
        *,
        kinds: Sequence[MemoryKind] | None = None,
        world_scope: WorldScope = "all",
    ) -> list[MemoryItem]:
        """Return every memory item for a character (newest first).

        Intended for consolidation / decay passes that need to reason
        about the whole pool at once. Not for use in the hot chat path.

        Unbounded by design — every caller here needs the full pool.
        The operator-facing browse path does **not**; it uses
        :meth:`list_page_for_character`.
        """

    async def list_page_for_character(
        self,
        character_id: str,
        *,
        kinds: Sequence[MemoryKind] | None = None,
        world_scope: WorldScope = "all",
        limit: int = 50,
        before: datetime | None = None,
    ) -> list["MemorySummary"]:
        """Return one keyset page of the browse projection (newest first).

        Same filtering and ordering rules as
        :meth:`list_all_for_character` — only the bound and the returned
        shape differ. ``before`` is the ``created_at`` of the last item
        the caller already has; rows are matched strictly older than it,
        matching the feed's cursor convention (plan D8).

        Implementations must resolve ``has_embedding`` without
        materialising the vector columns — see :class:`MemorySummary`.
        """

    async def count_for_character(self, character_id: str) -> int:
        """Return the total number of memories a character owns."""

    async def delete_many(self, item_ids: Sequence[str]) -> int:
        """Delete a batch of memories by id. Returns the number removed."""

    async def delete_created_since(
        self, conversation_id: str, since,
    ) -> int:
        """Delete memories tied to ``conversation_id`` whose ``created_at``
        is at or after ``since``. Returns the number removed.

        Used by the turn-undo path to reverse post-turn memory extraction
        without needing to thread the extracted ids back through the
        pipeline. Rows lacking a ``conversation_id`` are not touched —
        undo only reverses memories unambiguously attributed to this thread.
        """

    async def items_without_embedding(
        self,
        *,
        limit: int = 100,
        character_id: str | None = None,
    ) -> list[MemoryItem]:
        """Return memories that still need an embedding (backfill support).

        ``character_id=None`` means *all characters* — useful for the
        global backfill CLI. Implementations should order by
        ``created_at`` ascending so old items get caught up first.
        """

    async def update_embedding(
        self,
        item_id: str,
        embedding: Sequence[float],
    ) -> None:
        """Persist an embedding vector for an existing row.

        No-op when the row is missing so concurrent deletes do not
        crash the backfill worker.
        """

    async def update_tags_embedding(
        self,
        item_id: str,
        embedding: Sequence[float],
    ) -> None:
        """Persist the auxiliary tag embedding for an existing row.

        Same fail-soft semantics as ``update_embedding`` — used by
        the backfill CLI to populate the tags-embedding column for
        memories created before the column existed."""

    async def items_pending_tag_embedding(
        self,
        *,
        limit: int = 100,
        character_id: str | None = None,
    ) -> list[MemoryItem]:
        """Return memories that have at least one tag but no
        ``tags_embedding`` yet — backfill targets.

        Ordered oldest-first so a multi-pass backfill catches up
        chronologically, mirroring ``items_without_embedding``."""

    async def touch(self, item_id: str) -> None:
        """Record that an item was used in a prompt (updates access stats)."""

    async def delete_for_character(self, character_id: str) -> int:
        """Remove every memory item belonging to a character. Returns count."""

    async def get(self, item_id: str) -> MemoryItem | None:
        """Fetch a single memory by id, or ``None`` when missing.

        Intended for the memory-browsing UI; the hot chat path uses
        ``query`` / ``query_semantic`` instead.
        """

    async def update_fields(
        self,
        item_id: str,
        *,
        content: str | None = None,
        salience: float | None = None,
        tags: Sequence[str] | None = None,
        participants: Sequence["ParticipantRef"] | None = None,
    ) -> MemoryItem | None:
        """Patch a memory's mutable fields.

        Leaves ``kind`` alone — the kind is tied to prompt grouping and
        tag-oriented retrieval, so editing it would invalidate other
        invariants (ranker weights, consolidation cluster boundaries).
        Embedding is cleared on content change so the next post-turn
        /backfill pass re-embeds with the new text.

        ``participants`` replaces the structured participant refs (used by
        the relationship-coherence self-heal to reconcile an operator's
        display name when an old/contaminated name pointed at the same
        person). It is a **structural** edit only — the free-text
        ``content`` is never rewritten here. ``None`` leaves participants
        untouched.

        Returns the updated item, or ``None`` when the id does not
        exist.
        """

    async def mark_disclosed(
        self,
        character_id: str,
        item_ids: Sequence[str],
    ) -> tuple[str, ...]:
        """Flip ``player_knowledge`` from ``private`` to ``disclosed`` (KB8).

        Deliberately **not** a parameter on :meth:`update_fields`. That
        method is the memory-editing surface — it takes any value the
        caller hands it — whereas this one implements a single legal
        transition and nothing else. The narrower signature is what makes
        the ledger trustworthy: there is no call shape that walks a
        ``disclosed`` row back to ``private``, downgrades a ``shared``
        row, or mints a verdict on a row nobody classified (``""``), so
        the "she told him, and telling cannot be untold" invariant is a
        property of the port rather than a discipline every call site has
        to remember.

        Idempotent and replay-safe: an id already ``disclosed`` is a
        no-op, and so is one whose value is anything other than
        ``private``. Ids that do not exist, or belong to a different
        character, are ignored — ``character_id`` scopes the write so a
        caller can only flip rows for the character it already resolved
        ownership of.

        Returns the ids that were actually flipped by *this* call (never
        the ones that were already disclosed), so the caller can log a
        real transition instead of an attempt.
        """
