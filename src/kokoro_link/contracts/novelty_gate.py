"""Ports and DTOs for post-generation chat novelty gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.domain.entities.character import Character


@dataclass(frozen=True, slots=True)
class NoveltyGateContext:
    character_id: str
    operator_id: str
    response_text: str
    known_material: tuple[str, ...] = ()
    recent_self_lines: tuple[str, ...] = ()
    self_repetition_hint: str = ""
    latest_user_message: str = ""
    content_tolerance: str = "frontier"
    register_profile: RegisterProfile | None = None
    diversity_evidence: ReplyDiversityEvidence | None = None
    persona_context: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NoveltyVerdict:
    passes: bool
    lacks_novelty: bool = False
    imagery_relapse: bool = False
    register_mismatch: bool = False
    over_warm: bool = False
    formulaic: bool = False
    feedback: str = ""
    gate_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected = not any((
            self.lacks_novelty,
            self.imagery_relapse,
            self.register_mismatch,
            self.over_warm,
            self.formulaic,
        ))
        if self.passes != expected:
            object.__setattr__(self, "passes", expected)

    @classmethod
    def pass_open(cls, reason: str = "") -> "NoveltyVerdict":
        return cls(
            passes=True,
            feedback=reason,
            gate_metadata={"error": reason} if reason else {},
        )


class NoveltyGatePort(Protocol):
    async def evaluate(
        self,
        context: NoveltyGateContext,
        *,
        character: Character | None = None,
    ) -> NoveltyVerdict:
        """Evaluate one candidate reply. Implementations must fail open."""
