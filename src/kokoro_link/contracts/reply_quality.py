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
    language_mix_lines: tuple[str, ...] = ()
    """QG5 — ``script_mix_lines`` over this character's recent outputs.

    Descriptive only: it says what scripts the last few replies were
    written in, never that anything is wrong with them. Two readers, one
    computation — the gate prompt shows it to the judge as evidence for
    the ``language_mismatch`` axis, and
    :func:`~kokoro_link.application.services.chat_service._reply_quality_risk_score`
    reads :attr:`has_script_mix_evidence` off it to decide whether the
    *next* turn is worth buffering. Empty when nothing was countable.
    """
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def has_frequency_evidence(self) -> bool:
        return bool(self.self_repetition_hint.strip() or self.phrase_frequency_lines)

    @property
    def has_script_mix_evidence(self) -> bool:
        """Did the mix summary have to name individual mixed lines?

        ``script_mix_lines`` always opens with one composition summary and
        appends a second line *only* when at least one recent output
        crossed its per-line **foreign**-script reporting threshold —
        foreign relative to the operator's own language, which is why the
        caller has to hand that language in. So "longer than the summary
        alone" is exactly "this character has started writing in a script
        this operator does not read", and reading the length keeps that
        judgement in the one helper that computes it instead of re-deriving
        a threshold here.

        Deliberately a routing signal and nothing else — it decides which
        *path* the next turn takes (buffered and gated, or streamed
        straight through), never whether any text ships. That is the same
        standing the embedding self-similarity number already has, and it
        is what keeps this side of the gate free of content rules.
        """
        return len(self.language_mix_lines) > 1

    @property
    def highest_similarity(self) -> float:
        return float(self.max_self_similarity or 0.0)
