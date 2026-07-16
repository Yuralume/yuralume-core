"""Value objects for the branching-drama critic stage.

Same shape as ``fusion_critique`` but kept as its own type so the two
features can evolve independently — drama narrations are short scene
beats (300–500 chars), not multi-act prose, so the meaning of
``severity`` and the kinds of issues a critic should flag are subtly
different from fusion's. Duplicated VO > forcing both into a shared
shape that satisfies neither.

Findings are intentionally schema-light: ``kind`` is a free-form
string the LLM picks (重複前情 / 重複措辭 / 抽象 / 銜接 / 語感失調 / …)
rather than an enum. Pinning the LLM to a closed taxonomy is exactly
the keyword-style hard-coding CLAUDE.md forbids.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


SEVERITY_CLEAN = 0
"""Nothing material to fix — orchestrator should skip the polish pass."""

SEVERITY_MINOR = 1
"""Cosmetic issues. Polish optional."""

SEVERITY_MAJOR = 2
"""Real problems present. Polish should run."""

SEVERITY_SEVERE = 3
"""Draft is rough; whole rewrite warranted."""


_VALID_SEVERITIES = (
    SEVERITY_CLEAN, SEVERITY_MINOR, SEVERITY_MAJOR, SEVERITY_SEVERE,
)


@dataclass(frozen=True, slots=True)
class DramaCritiqueFinding:
    """One observation the critic flagged for the polisher to address.

    - ``kind`` — the critic's own label (重複前情 / 重複措辭 / 抽象 /
      銜接 / 語感失調 / 視角 / …). Free-form so the LLM isn't squeezed
      into a fixed vocabulary.
    - ``paragraph_index`` — 0-based paragraph index (split on
      ``\\n\\n``). Drama narrations are short — many will have only one
      paragraph and findings will pin to index 0; cross-paragraph
      observations leave it ``None`` for the whole-text polish path.
    - ``quote`` — exact prose fragment being pointed at. Human-readable
      anchor + sanity check on the index. Empty allowed for whole-text
      observations.
    - ``issue`` — why it's a problem. Concrete, not "improve this".
    - ``suggestion`` — direction (not a rewrite).
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
    ) -> "DramaCritiqueFinding":
        return cls(
            kind=kind.strip()[:40],
            quote=quote.strip()[:400],
            issue=issue.strip()[:400],
            suggestion=suggestion.strip()[:400],
            paragraph_index=paragraph_index,
        )

    def has_anchor(self) -> bool:
        return self.paragraph_index is not None


@dataclass(frozen=True, slots=True)
class DramaCritique:
    """The critic's overall verdict on a narration draft.

    ``severity`` drives the polish decision: ``SEVERITY_CLEAN`` skips
    polish, anything higher fires a single polish pass. Drama runs a
    single round (not a multi-round loop like fusion) because the
    narration is short and the gameplay latency budget is tight —
    one critic + one polish doubles the LLM cost; looping would
    triple it for marginal gain.
    """

    severity: int
    summary: str
    findings: tuple[DramaCritiqueFinding, ...] = field(default_factory=tuple)

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
        findings: Iterable[DramaCritiqueFinding] = (),
    ) -> "DramaCritique":
        return cls(
            severity=int(severity),
            summary=summary.strip()[:400],
            findings=tuple(findings),
        )

    @classmethod
    def clean(cls) -> "DramaCritique":
        return cls.create(
            severity=SEVERITY_CLEAN,
            summary="無重大問題",
            findings=(),
        )

    def has_issues(self) -> bool:
        return self.severity > SEVERITY_CLEAN and bool(self.findings)
