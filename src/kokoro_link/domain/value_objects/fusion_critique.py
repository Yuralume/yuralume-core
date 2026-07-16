"""Value objects for the fusion-story critic stage.

The critic is an LLM pass that reads a polished draft and points out
what's still wrong — repetition, abstract / vague descriptions, soft
transitions, anything else its semantic judgement flags. The
orchestrator then feeds the critique back to the polisher for another
round, repeating until the critic signals "good enough" or the
configured round cap kicks in.

Why a value object (vs free-form LLM text):

- The polisher prompt has to *act on* findings, not just be inspired
  by them. Structure makes it possible to say "rewrite each quoted
  passage in your output" rather than hoping the polisher reads a
  paragraph of prose criticism and acts.
- The orchestrator decides whether to loop or stop based on
  ``severity`` — that needs to be machine-readable, not buried in
  prose.
- Logging / debugging benefits: an operator looking at a story that
  went through 3 polish rounds can see exactly what each round was
  asked to fix.

Findings are intentionally schema-light: ``kind`` is a free-form
string the LLM picks (重複 / 抽象 / 銜接 / 節奏 / …), not an enum.
Forcing the LLM into a closed taxonomy is exactly the keyword-style
hard-coding CLAUDE.md forbids.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


SEVERITY_CLEAN = 0
"""Nothing material to fix — orchestrator should stop the polish loop."""

SEVERITY_MINOR = 1
"""Cosmetic issues. Optional round."""

SEVERITY_MAJOR = 2
"""Real problems present. Another polish round is justified."""

SEVERITY_SEVERE = 3
"""Draft is rough. Multiple more rounds likely needed."""


_VALID_SEVERITIES = (
    SEVERITY_CLEAN, SEVERITY_MINOR, SEVERITY_MAJOR, SEVERITY_SEVERE,
)


@dataclass(frozen=True, slots=True)
class FusionCritiqueFinding:
    """One observation the critic flagged for the polisher to address.

    - ``kind`` — the critic's own label (重複 / 抽象 / 銜接 / 描寫薄
      / 節奏 / 視角 / …). Free-form so the LLM isn't squeezed into a
      fixed vocabulary.
    - ``paragraph_index`` — 0-based index of the paragraph this finding
      points at (paragraphs split on ``\\n\\n``). When set, the spot
      polisher can rewrite *only* that paragraph instead of the whole
      text. ``None`` means whole-story / cross-paragraph observation,
      which requires the global polish path.
    - ``quote`` — the *exact* prose fragment the critic is pointing at.
      Both human-readable anchor and a sanity check on the index.
      Empty is allowed for whole-story observations.
    - ``issue`` — why it's a problem. Concrete, not "improve this".
    - ``suggestion`` — direction (not a rewrite). The polisher does
      the actual rewriting in its own voice.
    """

    kind: str
    quote: str
    issue: str
    suggestion: str
    paragraph_index: int | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("finding.kind must be non-empty")
        if not self.issue.strip():
            raise ValueError("finding.issue must be non-empty")
        if self.paragraph_index is not None and self.paragraph_index < 0:
            raise ValueError(
                "finding.paragraph_index must be >= 0 or None; got "
                f"{self.paragraph_index}",
            )

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        quote: str = "",
        issue: str,
        suggestion: str = "",
        paragraph_index: int | None = None,
    ) -> "FusionCritiqueFinding":
        return cls(
            kind=kind.strip()[:40],
            quote=quote.strip()[:400],
            issue=issue.strip()[:400],
            suggestion=suggestion.strip()[:400],
            paragraph_index=paragraph_index,
        )

    def has_anchor(self) -> bool:
        """``True`` when the finding can be addressed via spot polish.

        Whole-story observations (no paragraph_index) need the global
        polish path because they're about cross-paragraph structure or
        rhythm rather than a single span."""
        return self.paragraph_index is not None


@dataclass(frozen=True, slots=True)
class FusionStoryCritique:
    """The critic's overall verdict on a draft.

    ``severity`` drives the polish-loop decision: ``SEVERITY_CLEAN``
    stops the loop, anything higher justifies another round. The
    polisher consumes ``findings`` directly.

    ``should_continue`` is a soft override the critic can set when it
    thinks the issues are real (severity > 0) but no longer worth
    spending another polish round on (diminishing returns). The
    orchestrator respects it.
    """

    severity: int
    summary: str
    findings: tuple[FusionCritiqueFinding, ...] = field(default_factory=tuple)
    should_continue: bool = True

    def __post_init__(self) -> None:
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {_VALID_SEVERITIES}; "
                f"got {self.severity}",
            )

    @classmethod
    def create(
        cls,
        *,
        severity: int,
        summary: str = "",
        findings: Iterable[FusionCritiqueFinding] = (),
        should_continue: bool = True,
    ) -> "FusionStoryCritique":
        return cls(
            severity=int(severity),
            summary=summary.strip()[:600],
            findings=tuple(findings),
            should_continue=bool(should_continue),
        )

    @classmethod
    def clean(cls) -> "FusionStoryCritique":
        """Convenience constructor for the "nothing to fix" verdict.

        Used by the fake LLM path and by the orchestrator when it
        needs to short-circuit without running the critic.
        """
        return cls.create(
            severity=SEVERITY_CLEAN,
            summary="無重大問題",
            findings=(),
            should_continue=False,
        )

    def has_issues(self) -> bool:
        return self.severity > SEVERITY_CLEAN and bool(self.findings)
