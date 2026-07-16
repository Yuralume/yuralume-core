"""OperatorPersona — the character's evolving understanding of the
operator, organised by the five-tier interpersonal model.

Sits alongside :class:`OperatorProfile` (which keeps the operator's
chosen display name / aliases / pronouns and is updated through the
settings UI). ``OperatorPersona`` is what the *characters* infer from
conversation; it grows over time through extraction + dream-job
consolidation, and is rendered into the chat prompt so characters can
behave as if they remember.

Layer 4 (interaction strength) is special: it's derived from system
state (message counts, story-arc progress), not extracted from text.
It lives on this aggregate as a value object so the prompt builder
can render all five layers from one source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    ProfileField,
)


@dataclass(frozen=True, slots=True)
class InteractionStrength:
    """Layer 4 snapshot — per ``(character_id, operator_id)`` pair.

    Computed by :class:`InteractionStrengthCalculator`; do not write
    directly to storage — recompute on demand (cheap, and avoids
    stale rows).

    Per-character because conversations are per-character: a character
    can have ``total_user_messages == 0`` even when an initial relationship
    seed says the pair already knows each other. The band remains a
    statistical interaction-volume bucket; seed data is the relationship
    authority.

    Numeric fields stay numeric here; the prompt builder is responsible
    for translating them to qualitative phrases so the LLM never sees
    raw counts (which encourage arithmetic-reasoning).
    """

    character_id: str
    operator_id: str
    first_message_at: datetime | None
    total_user_messages: int
    days_since_first_contact: int
    messages_last_7_days: int
    messages_last_30_days: int
    longest_session_minutes: int
    shared_arc_realized_count: int
    shared_drama_count: int
    familiarity_band: Familiarity
    computed_at: datetime

    def __post_init__(self) -> None:
        char_id = (self.character_id or "").strip()
        if not char_id:
            raise ValueError("InteractionStrength.character_id must be non-empty")
        object.__setattr__(self, "character_id", char_id)
        op_id = (self.operator_id or "").strip()
        if not op_id:
            raise ValueError("InteractionStrength.operator_id must be non-empty")
        object.__setattr__(self, "operator_id", op_id)
        for attr in (
            "total_user_messages",
            "days_since_first_contact",
            "messages_last_7_days",
            "messages_last_30_days",
            "longest_session_minutes",
            "shared_arc_realized_count",
            "shared_drama_count",
        ):
            value = getattr(self, attr)
            if value < 0:
                raise ValueError(f"InteractionStrength.{attr} must be >= 0")

    @classmethod
    def empty(
        cls, character_id: str, operator_id: str, *, now: datetime,
    ) -> "InteractionStrength":
        """Sentinel for a brand-new ``(character, operator)`` pair —
        the character has never received a message from the operator."""
        return cls(
            character_id=character_id,
            operator_id=operator_id,
            first_message_at=None,
            total_user_messages=0,
            days_since_first_contact=0,
            messages_last_7_days=0,
            messages_last_30_days=0,
            longest_session_minutes=0,
            shared_arc_realized_count=0,
            shared_drama_count=0,
            familiarity_band=Familiarity.STRANGER,
            computed_at=now,
        )


@dataclass(frozen=True, slots=True)
class OperatorPersona:
    """One character's aggregated knowledge of the operator.

    Per ``(character_id, operator_id)`` — each character owns a
    separate aggregate so meeting a new character resets the slate.
    Sharing facts across characters would collapse the "stranger →
    acquaintance" arc the feature exists to model.

    Layer dicts hold **confirmed** fields only (those that passed the
    dream job's promotion threshold and are eligible for prompt
    injection). ``pending_candidates`` carries staged observations
    that the dream job will weigh on its next pass — repositories
    expose them via a separate query so casual reads (e.g. prompt
    rendering) don't pull staging noise.
    """

    character_id: str
    operator_id: str
    layer1_identity: Mapping[str, ProfileField] = field(default_factory=dict)
    layer2_life: Mapping[str, ProfileField] = field(default_factory=dict)
    layer3_emotional: Mapping[str, ProfileField] = field(default_factory=dict)
    layer5_trust: Mapping[str, ProfileField] = field(default_factory=dict)
    layer4_interaction: InteractionStrength | None = None
    pending_candidates: tuple[CandidateField, ...] = ()

    def __post_init__(self) -> None:
        char_id = (self.character_id or "").strip()
        if not char_id:
            raise ValueError("OperatorPersona.character_id must be non-empty")
        object.__setattr__(self, "character_id", char_id)
        op_id = (self.operator_id or "").strip()
        if not op_id:
            raise ValueError("OperatorPersona.operator_id must be non-empty")
        object.__setattr__(self, "operator_id", op_id)
        for attr, expected_layer in (
            ("layer1_identity", 1),
            ("layer2_life", 2),
            ("layer3_emotional", 3),
            ("layer5_trust", 5),
        ):
            mapping = getattr(self, attr)
            for key, fld in mapping.items():
                if fld.layer != expected_layer:
                    raise ValueError(
                        f"OperatorPersona.{attr}[{key!r}] must carry layer "
                        f"{expected_layer}, got {fld.layer}",
                    )
            object.__setattr__(self, attr, dict(mapping))
        object.__setattr__(
            self, "pending_candidates", tuple(self.pending_candidates),
        )

    @classmethod
    def empty(cls, character_id: str, operator_id: str) -> "OperatorPersona":
        return cls(character_id=character_id, operator_id=operator_id)

    def is_empty(self) -> bool:
        """``True`` when no layer has any confirmed field and Layer 4
        is either missing or still at the stranger band — prompt
        builder uses this to decide whether to emit the persona block
        at all."""
        if any(
            (
                self.layer1_identity,
                self.layer2_life,
                self.layer3_emotional,
                self.layer5_trust,
            ),
        ):
            return False
        if self.layer4_interaction is None:
            return True
        return self.layer4_interaction.total_user_messages == 0

    def fields_by_layer(self, layer: int) -> Mapping[str, ProfileField]:
        """Convenience accessor for code that walks layers
        generically (e.g. the prompt renderer)."""
        if layer == 1:
            return self.layer1_identity
        if layer == 2:
            return self.layer2_life
        if layer == 3:
            return self.layer3_emotional
        if layer == 5:
            return self.layer5_trust
        raise ValueError(f"Unknown persona layer: {layer}")
