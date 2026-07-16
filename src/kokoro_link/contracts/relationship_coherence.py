"""Relationship-coherence detector port + structured repair DTOs.

The dream pass accumulates learned identity facts about the operator
across several stores (persona name/nickname, observed salutation, seed
address names, memory participants). A recurring contamination pattern
inverts the two address directions: a *direction-B* term the player uses
to address the **character** (兄妹喊「哥哥」、情侶喊「老公」、或直接喊角色名)
leaks into a *direction-A* slot that is meant to record how the
**character** should address the **player**.

Write-side guards stop *new* contamination. This port is the offline
second layer: during the dream pass, a high-reasoning model inspects the
authoritative facts (seed, rename-log, character name, operator profile)
against the suspect stores and returns a structured repair plan. The
model must cite which authoritative source each repair contradicts and
only propose high-confidence repairs; an empty plan is the correct
answer when the data is already consistent.

The port stays pure — it takes structured facts + suspects and returns a
plan. Applying the plan (respecting the invariants: never rewrite the
global profile, never rewrite memory free-text, never delete memories,
never touch a ``confirmed_by_user`` seed) is the service's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


# ---- Structured input --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoherenceTranscriptTurn:
    """One recent raw dialogue turn, first-hand evidence for adjudication.

    Distinct from a memory: memory is the *derived* layer that gets
    contaminated. The raw transcript is ground truth the model uses to
    decide **which derived value is dirty** — it must never be mined to
    invent a new name (that would re-run the extraction that erred).
    """

    role: str  # "user" | "assistant"
    content: str


@dataclass(frozen=True, slots=True)
class CoherenceFacts:
    """Authoritative direction facts the detector reasons against.

    Direction A = how the character addresses the player.
    Direction B = how the player addresses the character.

    These are the "truth" sources. ``seed_confirmed_by_user`` marks the
    seed as user-confirmed truth that must never be mutated. The seed and
    rename-log stay the repair anchor; ``recent_transcript`` is first-hand
    evidence used **only** to adjudicate which derived value is
    contaminated (and to tell a legitimate recent re-address apart from
    contamination), never to fabricate new names.
    """

    # Direction A truth (how the character addresses the player).
    seed_user_address_name: str = ""
    # Direction B truth (how the player addresses the character).
    seed_character_address_name: str = ""
    seed_confirmed_by_user: bool = True
    character_name: str = ""
    operator_display_name: str = ""
    operator_aliases: tuple[str, ...] = ()
    # Most recent rename-log value per direction, if any (for continuity).
    latest_rename_player_direction: str = ""
    latest_rename_character_direction: str = ""
    # Recent raw user+assistant turns for the pair (windowed, may be empty).
    recent_transcript: tuple[CoherenceTranscriptTurn, ...] = ()


@dataclass(frozen=True, slots=True)
class SuspectPersonaField:
    """A confirmed persona identity row that might be contaminated."""

    field_id: str
    field_key: str  # "name" | "nickname"
    value: str
    source: str  # "extraction" | "user_explicit" | ...
    confidence: float


@dataclass(frozen=True, slots=True)
class SuspectMemory:
    """A recent memory whose participant attribution might be wrong."""

    memory_id: str
    content: str
    salience: float
    # display names of the operator-kind participants on this memory.
    operator_participant_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CoherenceSuspects:
    """The mutable stores the detector is allowed to propose repairs on."""

    persona_fields: tuple[SuspectPersonaField, ...] = ()
    observed_salutation: str = ""
    memories: tuple[SuspectMemory, ...] = ()


# ---- Structured output (repair actions) -------------------------------------

# Each action names the authoritative source it contradicts so the audit
# trail is explicit and the service can double-check the model's claim
# structurally before applying.

ContradictionSource = Literal[
    "seed_user_address_name",       # direction A truth
    "seed_character_address_name",  # direction B truth
    "character_name",
    "operator_display_name",
    "operator_alias",
]


@dataclass(frozen=True, slots=True)
class PersonaFieldRepair:
    """Retire a persona name/nickname row whose value is a direction-B
    term (the player addressing the character), not the player's own name.

    Applied via the persona service's supersede/reject path — never writes
    back to the global operator profile.
    """

    field_id: str
    contradicts: ContradictionSource
    reason: str


@dataclass(frozen=True, slots=True)
class SalutationRepair:
    """Fix a contaminated observed ``salutation`` (direction B).

    ``align_to_seed`` clears the observed value so the seed's
    ``character_address_name`` (or character name) resolves cleanly; there
    is no free-text rewrite — the observation is simply dropped.
    """

    contradicts: ContradictionSource
    reason: str


@dataclass(frozen=True, slots=True)
class MemoryRepair:
    """Down-rank a contaminated memory and reconcile its operator
    participant display name.

    Never rewrites ``content`` free-text and never deletes the row —
    deletion stays a human decision. ``reconcile_participant_to`` is the
    corrected operator display name (typically the resolved direction-A
    address); empty string means "down-rank salience only".
    """

    memory_id: str
    lower_salience_to: float
    reconcile_participant_to: str
    reason: str


@dataclass(frozen=True, slots=True)
class CoherenceRepairPlan:
    """The detector's structured verdict. Empty = data already coherent."""

    persona_field_repairs: tuple[PersonaFieldRepair, ...] = field(
        default_factory=tuple,
    )
    salutation_repair: SalutationRepair | None = None
    memory_repairs: tuple[MemoryRepair, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        return not (
            self.persona_field_repairs
            or self.salutation_repair
            or self.memory_repairs
        )


class RelationshipCoherenceDetectorPort(Protocol):
    async def detect(
        self,
        *,
        facts: CoherenceFacts,
        suspects: CoherenceSuspects,
    ) -> CoherenceRepairPlan:
        """Inspect authoritative facts vs suspect stores and return a
        repair plan.

        Failure modes — the implementation MUST NOT raise. On any internal
        error (LLM failure, unparseable output) return an empty
        :class:`CoherenceRepairPlan` so the dream pass falls through
        untouched. An empty plan is also the correct answer whenever the
        data is already coherent or the model is not confident.
        """
