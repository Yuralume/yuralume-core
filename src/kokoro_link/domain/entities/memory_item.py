"""Structured memory entity.

A ``MemoryItem`` is the unit of long-term memory attached to a character.
It is the domain-level view — storage schemas and extractor outputs map
onto this shape.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4

from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import MemoryKind


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clamp_salience(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


MEMORY_AUDIENCE_PRIVATE = "private"
MEMORY_AUDIENCE_SHAREABLE = "shareable"
_VALID_AUDIENCES = frozenset({MEMORY_AUDIENCE_PRIVATE, MEMORY_AUDIENCE_SHAREABLE})


def _normalize_audience(value: str) -> str:
    """Coerce to ``private`` / ``shareable`` / ``""``. Unknown or absent
    values stay ``""`` (the legacy "no judgement" state) so the feed
    collector only suppresses memories the model *affirmatively* marked
    private — it never silences the back catalogue."""
    text = (value or "").strip().lower()
    return text if text in _VALID_AUDIENCES else ""


PLAYER_KNOWLEDGE_SHARED = "shared"
PLAYER_KNOWLEDGE_PRIVATE = "private"
PLAYER_KNOWLEDGE_DISCLOSED = "disclosed"
_VALID_PLAYER_KNOWLEDGE = frozenset(
    {PLAYER_KNOWLEDGE_SHARED, PLAYER_KNOWLEDGE_PRIVATE, PLAYER_KNOWLEDGE_DISCLOSED},
)


def _normalize_player_knowledge(value: str) -> str:
    """Coerce to ``shared`` / ``private`` / ``disclosed`` / ``""``.

    Unknown or absent values stay ``""`` (legacy / unjudged) — same
    dispatch shape as :func:`_normalize_audience`. A value outside the
    domain never raises; it degrades to "no judgement" so a malformed
    write-station payload can't crash the write path, and a row
    restored from an older schema version never becomes unreadable."""
    text = (value or "").strip().lower()
    return text if text in _VALID_PLAYER_KNOWLEDGE else ""


def merge_player_knowledge(values: Iterable[str]) -> str:
    """Propagate ``player_knowledge`` across a merged cluster (KB6).

    Consolidation rewrites several memories into one line of text, so
    the merged row inherits the *most protective* verdict any source
    carried — it never mints a new one:

    1. any ``private`` → ``private``. Absolute priority. The merged text
       can restate that private fact, so it stays unknown to the player.
    2. else any ``""`` (unjudged) → ``""``. One unknown source means the
       merged text may restate something nobody classified; minting a
       verdict for it would be guessing.
    3. else any ``disclosed`` → ``disclosed``. Told once, still worth
       marking as previously-told rather than as lived-together.
    4. else → ``shared``.

    **Why ``private`` outranks ``""`` and not the other way round.**
    ``""`` is *not* the conservative end of the scale — at the only place
    the ledger is read (``prompt/memory_lines.memory_knowledge_frame``)
    ``""`` and ``shared`` both map to *no frame*, so they are rendering-
    equivalent. Letting an unjudged source win would therefore strip a
    ``private`` sibling of the one instruction that protects it, which is
    the arc-leak incident this plan exists to stop — and it would fire on
    essentially every merge, because the entire pre-KB5 back catalogue is
    ``""`` while ``private`` rows are only now starting to be written.
    "Don't invent a verdict" still holds for steps 2–4, where the worst
    fallout is the character re-introducing something the player already
    knew (mild); it does not get to outrank the actual boundary.

    An empty ``values`` yields ``""``: no sources, no verdict.
    """
    seen = {_normalize_player_knowledge(value) for value in values}
    if PLAYER_KNOWLEDGE_PRIVATE in seen:
        return PLAYER_KNOWLEDGE_PRIVATE
    if not seen or "" in seen:
        return ""
    if PLAYER_KNOWLEDGE_DISCLOSED in seen:
        return PLAYER_KNOWLEDGE_DISCLOSED
    return PLAYER_KNOWLEDGE_SHARED


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: str
    character_id: str
    conversation_id: str | None
    kind: MemoryKind
    content: str
    salience: float
    tags: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=_utcnow)
    last_accessed_at: datetime | None = None
    access_count: int = 0
    embedding: tuple[float, ...] | None = None
    """Semantic embedding vector (Phase B). ``None`` when the embedder is
    unavailable or the item predates the embedding column — callers must
    fall back to salience-only ranking in that case."""
    tags_embedding: tuple[float, ...] | None = None
    """Auxiliary embedding of the joined tag string (e.g. ``"travel
    location coffee"``). Stored alongside the primary content embedding
    so ``query_semantic`` can score against both — useful when the user
    query phrases a topic the content doesn't echo verbatim but the
    tags capture (``"旅行" → travel`` etc.). ``None`` when the item has
    no tags or pre-dates the column; the repo falls back to the content
    score alone in that case."""
    participants: tuple[ParticipantRef, ...] = field(default_factory=tuple)
    """Who this memory is *about*, beyond the character themselves.

    Phase 2 of the world-system roadmap: the post-turn extractor records every named person that appeared
    in the memory's content as a structured reference. This unblocks
    cross-character disambiguation later — A's memory of "B took the
    operator to ramen" carries actor_ids for both B (character) and
    the operator (operator), so the prompt builder can render
    ``[與 B 一起]`` tags and the eventual "god agent" can filter who
    sees what. Empty tuple is the default; pre-Phase-2 rows arrive
    from the DB with no participants and behave exactly as before."""
    world_id: str | None = None
    """Reserved for the world system. Always ``None`` today; future
    multi-world deployments will populate this so memories can be
    filtered to the world they happened in. Carrying the column now
    avoids a migration when the world system lands."""
    location: str | None = None
    """Free-form location string ("咖啡廳", "我家"). Reserved seam for
    the world system, where this will normalise into a ``Place``
    entity. The extractor populates it best-effort when the dialogue
    mentions a clear setting; callers shouldn't rely on it being set."""
    audience: str = ""
    """Whether this memory is fit to be shared publicly. The post-turn
    extractor classifies it semantically: ``private`` for relationship
    book-keeping (how the two address each other, naming preferences,
    secrets, vulnerabilities, contact details) that the character would
    never broadcast, ``shareable`` for ordinary life moments. ``""`` =
    no judgement (legacy rows). The LumeGram feed collector skips
    ``private`` memories so a private preference never becomes a public
    post; recall in chat is unaffected — salience measures *recall*
    importance, not *shareability*."""
    player_knowledge: str = ""
    """The disclosure ledger (KB5 of the player-knowledge-boundary plan):
    what the *character* believes the player knows about this memory —
    a different axis from ``audience`` (which asks whether the memory is
    fit to broadcast publicly; a memory can be ``player_knowledge=private``
    and ``audience=shareable`` at once, e.g. a solo errand the character
    is happy to post about but hasn't mentioned to the player yet).

    Four values:
    - ``""`` = legacy / unjudged. No write station recorded a verdict
      (either it predates this column, or the station hasn't been
      updated yet). Rendering treats this exactly like today — the
      back catalogue is never silently reinterpreted as private.
    - ``shared`` = the player lived this or was told about it live —
      the character can reference it as a common memory.
    - ``private`` = the player did not witness this; it was written by
      a background process (an autonomous story beat, encounter
      hearsay, schedule memorialization of an unattended activity...).
      The character should not treat it as something the player already
      knows.
    - ``disclosed`` = originally ``private``, but the character has
      since told the player about it in-fiction (chat, a feed post the
      player read, a proactive message) — structurally promoted so the
      character stops re-introducing it as news.

    Set once at write time by the station that created the memory
    (deterministic from the write pathway, not an LLM guess — see the
    plan's KB6) and later flipped ``private`` → ``disclosed`` by the
    disclosure-ledger flip (KB8). ``""`` never gets guessed into one of
    the other three after the fact."""

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        kind: MemoryKind,
        content: str,
        salience: float = 0.5,
        conversation_id: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        created_at: datetime | None = None,
        embedding: tuple[float, ...] | None = None,
        tags_embedding: tuple[float, ...] | None = None,
        participants: tuple[ParticipantRef, ...] | list[ParticipantRef] | None = None,
        world_id: str | None = None,
        location: str | None = None,
        audience: str = "",
        player_knowledge: str = "",
    ) -> "MemoryItem":
        trimmed_content = content.strip()
        if not trimmed_content:
            raise ValueError("MemoryItem content must be non-empty")
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            conversation_id=conversation_id,
            kind=kind,
            content=trimmed_content,
            salience=_clamp_salience(salience),
            tags=tuple(tags or ()),
            created_at=created_at or _utcnow(),
            embedding=embedding,
            tags_embedding=tags_embedding,
            participants=tuple(participants or ()),
            world_id=(world_id.strip() or None) if world_id else None,
            location=(location.strip() or None) if location else None,
            audience=_normalize_audience(audience),
            player_knowledge=_normalize_player_knowledge(player_knowledge),
        )

    @property
    def is_shareable_to_feed(self) -> bool:
        """``False`` only when the extractor affirmatively marked this
        memory ``private``. Legacy / unjudged memories (``""``) stay
        feed-eligible so the back catalogue isn't silenced."""
        return self.audience != MEMORY_AUDIENCE_PRIVATE

    def with_embedding(self, embedding: tuple[float, ...] | None) -> "MemoryItem":
        """Return a copy with ``embedding`` attached. Idempotent."""
        return replace(self, embedding=embedding)

    def with_tags_embedding(
        self, tags_embedding: tuple[float, ...] | None,
    ) -> "MemoryItem":
        """Return a copy with ``tags_embedding`` attached. Idempotent."""
        return replace(self, tags_embedding=tags_embedding)

    def touched(self, at: datetime | None = None) -> "MemoryItem":
        """Return a new item with access bookkeeping incremented."""
        return replace(
            self,
            last_accessed_at=at or _utcnow(),
            access_count=self.access_count + 1,
        )

    def with_salience(self, salience: float) -> "MemoryItem":
        return replace(self, salience=_clamp_salience(salience))
