"""Deterministic evidence objects for reply quality gates."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ReplyDiversityEvidence:
    assistant_line_count: int = 0
    max_self_similarity: float | None = None
    mean_self_similarity: float | None = None
    self_repetition_hint: str = ""
    phrase_frequency_lines: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def has_frequency_evidence(self) -> bool:
        return bool(self.self_repetition_hint.strip() or self.phrase_frequency_lines)

    @property
    def highest_similarity(self) -> float:
        return float(self.max_self_similarity or 0.0)
