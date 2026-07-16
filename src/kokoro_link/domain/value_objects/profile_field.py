"""ProfileField — one structured fact the character knows about the
operator, layered by the five-tier interpersonal model.

Three closely-related types live here because they share validation
shape and never travel separately:

- ``EvidenceRef``: the receipt — which user message a fact was lifted
  from, with the exact quote. The LLM-first guard relies on this to
  refuse hallucinations (the quote must appear verbatim in a real user
  message).
- ``ProfileField``: a confirmed (or pending) attribute about the
  operator, plus the confidence and evidence backing it.
- ``CandidateField``: the staging shape produced by the extraction
  pass before the dream job decides whether to promote it.

Layers (mirrors :class:`OperatorPersona`):

1. identity (name, age, occupation, ...)
2. life context (interests, routine, ...)
3. emotional depth (anxieties, values, ...)
4. interaction strength — computed, **not** stored as ProfileField
5. trust & dependence (borrowed money, family introduced, ...)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar

from kokoro_link.domain.entities.conversation import MessageContentMode


_VALID_SOURCES: frozenset[str] = frozenset(
    {"extraction", "dream", "dream_inference", "user_explicit", "manual"},
)
"""Where the field came from. ``dream_inference`` is special: the dream
job synthesised it across other fields, so prompt rendering may want to
soften the wording ("seems to" instead of "is")."""

_VALID_CANDIDATE_STATES: frozenset[str] = frozenset(
    {"pending", "promoted", "rejected", "stale", "conflict", "superseded"},
)

_VALID_LAYERS: frozenset[int] = frozenset({1, 2, 3, 5})
"""Layer 4 (interaction strength) is computed, never stored as a
ProfileField — keep it out of the validation set so a stray write
fails loudly."""

_MAX_QUOTE_LEN = 240
"""Hard cap on stored quotes. The prompt asked the LLM for ≤80 chars
but we allow some slack for short messages that quote-in-full; 240 is
still tight enough that one bad row doesn't bloat the table."""


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A pointer back to the user message that justifies a fact.

    The quote is stored verbatim — that's what the substring guard
    checks against. Trimming or "normalising" it here would silently
    defeat the guard.
    """

    turn_id: str
    conversation_id: str
    quote: str
    extracted_at: datetime

    def __post_init__(self) -> None:
        turn_id = (self.turn_id or "").strip()
        if not turn_id:
            raise ValueError("EvidenceRef.turn_id must be non-empty")
        object.__setattr__(self, "turn_id", turn_id)
        conv_id = (self.conversation_id or "").strip()
        if not conv_id:
            raise ValueError("EvidenceRef.conversation_id must be non-empty")
        object.__setattr__(self, "conversation_id", conv_id)
        quote = (self.quote or "").strip()
        if not quote:
            raise ValueError("EvidenceRef.quote must be non-empty")
        if len(quote) > _MAX_QUOTE_LEN:
            quote = quote[:_MAX_QUOTE_LEN]
        object.__setattr__(self, "quote", quote)

    def to_dict(self) -> dict[str, str]:
        return {
            "turn_id": self.turn_id,
            "conversation_id": self.conversation_id,
            "quote": self.quote,
            "extracted_at": self.extracted_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "EvidenceRef | None":
        """Inverse of :meth:`to_dict`; tolerate malformed rows by
        returning ``None`` so a single bad evidence entry doesn't
        kill the whole field read."""
        turn_id = str(payload.get("turn_id") or "").strip()
        conv_id = str(payload.get("conversation_id") or "").strip()
        quote = str(payload.get("quote") or "").strip()
        ts_raw = payload.get("extracted_at")
        if not (turn_id and conv_id and quote and isinstance(ts_raw, str)):
            return None
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            return None
        try:
            return cls(
                turn_id=turn_id,
                conversation_id=conv_id,
                quote=quote,
                extracted_at=ts,
            )
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class ProfileField:
    """A single attribute one character has come to know about the
    operator. Frozen so it can be safely shared across services.

    Per-character: ``character_id`` is part of the identity tuple
    ``(character_id, operator_id, layer, field_key)``. A different
    character builds their own ``ProfileField`` for the same fact
    based on what they personally observed — no cross-character
    inheritance. This matches the "stranger → acquaintance" arc the
    feature exists to model.

    ``confidence`` semantics (Layer-specific thresholds live in the
    dream-job rules, not here):

    - 0.0–0.5: barely above noise; should never be promoted
    - 0.5–0.7: pending, one observation
    - 0.7–0.9: confirmed (Layer 1/2 inject threshold)
    - 0.8+:   Layer 3/5 inject threshold

    ``update_count`` tracks **observations**, not **edits** — every
    time the same value is seen again it bumps. The dream job uses
    this for the "seen N times → promote" rule.
    """

    field_key: str
    layer: int
    value: str
    confidence: float
    evidence_refs: tuple[EvidenceRef, ...]
    last_updated: datetime
    update_count: int
    source: str
    content_mode: MessageContentMode | str = MessageContentMode.NORMAL
    """Write-time content mode of the evidence that produced this fact.

    NSFW-mode persona facts are treated as sensitive even when they
    live in otherwise low-risk layers.  Renderers must not expose them
    to frontier prompts or player-visible projection surfaces.
    """
    character_id: str = ""
    """The character who observed this fact. Required for new writes;
    defaulted to empty string only for backwards-compat with code
    paths that haven't been threaded yet (validation in
    :meth:`__post_init__` raises if it stays empty)."""

    field_id: str | None = None
    """DB row id if loaded from storage; ``None`` for freshly built
    instances. Repository assigns one when persisting."""

    def __post_init__(self) -> None:
        char_id = (self.character_id or "").strip()
        if not char_id:
            raise ValueError("ProfileField.character_id must be non-empty")
        object.__setattr__(self, "character_id", char_id)
        key = (self.field_key or "").strip()
        if not key:
            raise ValueError("ProfileField.field_key must be non-empty")
        object.__setattr__(self, "field_key", key)
        if self.layer not in _VALID_LAYERS:
            raise ValueError(
                f"ProfileField.layer must be one of {sorted(_VALID_LAYERS)}, "
                f"got {self.layer!r}",
            )
        value = (self.value or "").strip()
        if not value:
            raise ValueError("ProfileField.value must be non-empty")
        object.__setattr__(self, "value", value)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"ProfileField.confidence must be in [0,1], got {self.confidence!r}",
            )
        if self.update_count < 0:
            raise ValueError("ProfileField.update_count must be >= 0")
        src = (self.source or "").strip().lower()
        if src not in _VALID_SOURCES:
            raise ValueError(
                f"ProfileField.source must be one of {sorted(_VALID_SOURCES)}, "
                f"got {self.source!r}",
            )
        object.__setattr__(self, "source", src)
        object.__setattr__(
            self,
            "content_mode",
            _coerce_content_mode(self.content_mode),
        )
        if self.field_id is not None:
            trimmed = self.field_id.strip()
            object.__setattr__(self, "field_id", trimmed or None)
        if not self.evidence_refs:
            raise ValueError("ProfileField must carry at least one EvidenceRef")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    def with_confidence(self, new_confidence: float) -> "ProfileField":
        from dataclasses import replace

        return replace(self, confidence=new_confidence)


@dataclass(frozen=True, slots=True)
class CandidateField:
    """Staging shape produced by the extraction pass. Cheaper than
    ``ProfileField``: only one evidence ref, no merged update_count,
    and a state that the dream job sets as it makes decisions.

    Per-character — ``character_id`` records which character observed
    this candidate. A different character's extractor running the
    same turn produces its own row; nothing is shared across
    characters.
    """

    field_key: str
    layer: int
    proposed_value: str
    evidence_ref: EvidenceRef
    raw_extractor_confidence: float
    state: str = "pending"
    source: str = "extraction"
    content_mode: MessageContentMode | str = MessageContentMode.NORMAL
    """Write-time content mode of this candidate's evidence."""
    candidate_id: str | None = None
    extracted_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    explicit: bool = False
    """Layer-5 promotion gate: only ``True`` when the extractor judged
    the operator made an explicit, unambiguous statement (e.g. "I'll
    pay you back the 5000 next week"). Layers 1-3 ignore this."""
    character_id: str = ""
    """The character whose extractor produced this candidate. Required
    for writes; validation in :meth:`__post_init__` raises if empty."""

    _ALLOWED_STATES: ClassVar[frozenset[str]] = _VALID_CANDIDATE_STATES
    _ALLOWED_SOURCES: ClassVar[frozenset[str]] = _VALID_SOURCES
    _ALLOWED_LAYERS: ClassVar[frozenset[int]] = _VALID_LAYERS

    def __post_init__(self) -> None:
        char_id = (self.character_id or "").strip()
        if not char_id:
            raise ValueError("CandidateField.character_id must be non-empty")
        object.__setattr__(self, "character_id", char_id)
        key = (self.field_key or "").strip()
        if not key:
            raise ValueError("CandidateField.field_key must be non-empty")
        object.__setattr__(self, "field_key", key)
        if self.layer not in self._ALLOWED_LAYERS:
            raise ValueError(
                f"CandidateField.layer must be one of "
                f"{sorted(self._ALLOWED_LAYERS)}, got {self.layer!r}",
            )
        value = (self.proposed_value or "").strip()
        if not value:
            raise ValueError("CandidateField.proposed_value must be non-empty")
        object.__setattr__(self, "proposed_value", value)
        if not 0.0 <= self.raw_extractor_confidence <= 1.0:
            raise ValueError(
                "CandidateField.raw_extractor_confidence must be in [0,1]",
            )
        state = (self.state or "").strip().lower()
        if state not in self._ALLOWED_STATES:
            raise ValueError(
                f"CandidateField.state must be one of "
                f"{sorted(self._ALLOWED_STATES)}, got {self.state!r}",
            )
        object.__setattr__(self, "state", state)
        src = (self.source or "").strip().lower()
        if src not in self._ALLOWED_SOURCES:
            raise ValueError(
                f"CandidateField.source must be one of "
                f"{sorted(self._ALLOWED_SOURCES)}, got {self.source!r}",
            )
        object.__setattr__(self, "source", src)
        object.__setattr__(
            self,
            "content_mode",
            _coerce_content_mode(self.content_mode),
        )
        if self.candidate_id is not None:
            trimmed = self.candidate_id.strip()
            object.__setattr__(self, "candidate_id", trimmed or None)


def _coerce_content_mode(
    value: MessageContentMode | str | None,
) -> MessageContentMode:
    if isinstance(value, MessageContentMode):
        return value
    try:
        return MessageContentMode(str(value or "").strip().lower())
    except ValueError:
        return MessageContentMode.NORMAL
