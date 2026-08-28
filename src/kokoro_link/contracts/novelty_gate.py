"""Ports and DTOs for post-generation chat novelty gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.domain.entities.character import Character

UNROUTED_METADATA_KEY = "unrouted"
"""Marks a verdict produced without a routable judge — see
:meth:`NoveltyVerdict.pass_unrouted`."""

SOFT_AXES: tuple[str, ...] = (
    "lacks_novelty",
    "imagery_relapse",
    "register_mismatch",
    "over_warm",
    "formulaic",
)
"""Quality opinions. A surviving soft failure still ships (best-effort)."""

HARD_AXES: tuple[str, ...] = (
    "structural_leak",
    "language_mismatch",
    "visible_truncation",
    "tool_prompt_defect",
    "temporal_inconsistency",
)
"""Defects whose cost is asymmetric enough to withhold a whole background
message. Order is the order an operator reads them in."""

ALL_AXES: tuple[str, ...] = (*SOFT_AXES, *HARD_AXES)
"""The judge's full vocabulary — the single source of truth.

These tuples exist because the axis list used to be hand-copied into five
places (this module's own ``hard_fail``, the LLM adapter, the disposal
orchestrator, and the chat / proactive metadata rows). Four of those are
silent on omission: a tenth axis missing from a metadata row simply never
appears in the audit trail, and one missing from the orchestrator's hard
list downgrades a hard failure to a soft one. Everything that enumerates
axes imports from here.
"""


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
    operator_primary_language: str = ""
    """Player-facing primary language label (e.g. ``"繁體中文"``).

    Sole reference for the ``language_mismatch`` axis. Empty means the
    caller could not determine it — the rubric then leaves that axis
    false rather than guessing.
    """
    tool_prompt_lines: tuple[str, ...] = ()
    """Accompanying tool prompts, each line already labelled with its
    source (e.g. ``"image_prompt: 1girl, cafe, rain"``). These are
    legitimately English and must never drive ``language_mismatch``;
    they exist so the judge can see ``tool_prompt_defect``."""
    mechanical_evidence_lines: tuple[str, ...] = ()
    """Deterministic structural signals computed by the caller, e.g.
    「正文長度 612 超過上限 280，疑似混入非正文內容」. Evidence only —
    the verdict itself stays with the judge (LLM-first)."""
    temporal_context_lines: tuple[str, ...] = ()
    """When *now* is, and when the material this message answers happened.

    Sole reference for the ``temporal_inconsistency`` axis, and the reason
    it is a separate field rather than more ``mechanical_evidence_lines``:
    empty means the caller cannot date this message, and the rubric then
    pins that axis false rather than guessing — the same fail-safe
    ``operator_primary_language`` gives ``language_mismatch``. A surface
    that supplies nothing here is provably unaffected by the axis.

    Lines are already rendered by the caller (``timing_utils`` builds
    them) so this module never learns how a time is spelled, e.g.
    「現在：2026-08-27（週四）09:12」/「玩家說要回家：2026-08-26 17:30
    （約 16 小時前）」.
    """


@dataclass(frozen=True, slots=True)
class NoveltyVerdict:
    passes: bool
    lacks_novelty: bool = False
    imagery_relapse: bool = False
    register_mismatch: bool = False
    over_warm: bool = False
    formulaic: bool = False
    structural_leak: bool = False
    language_mismatch: bool = False
    visible_truncation: bool = False
    tool_prompt_defect: bool = False
    temporal_inconsistency: bool = False
    """The message is incoherent with *when it is being sent* — it treats
    a stale event as if it just happened, re-asks something already
    answered long ago, or misdates what the player said. Hard, because a
    character who does not know what time it is breaks the illusion the
    same way a leaked schema tag does, and because 「不發」 is the right
    answer for a concern that expired: an unsendable stale follow-up is
    better withheld than sent. Fires only against
    ``NoveltyGateContext.temporal_context_lines``."""
    feedback: str = ""
    gate_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        expected = not any(getattr(self, axis) for axis in ALL_AXES)
        if self.passes != expected:
            object.__setattr__(self, "passes", expected)

    @property
    def hard_fail(self) -> bool:
        """True when any *hard* axis fired.

        Hard axes carry asymmetric cost (structure leaked, wrong
        language, visible truncation, broken tool prompt, message sent at
        a time that makes no sense) and drive fail-closed handling on
        background surfaces, unlike the five soft quality axes which stay
        best-effort.
        """
        return any(getattr(self, axis) for axis in HARD_AXES)

    @property
    def fired_axes(self) -> tuple[str, ...]:
        """Names of the axes that fired, hard ones first — the order an
        operator reads them in: hard explains the disposal, soft explains
        the feedback."""
        return tuple(
            axis for axis in (*HARD_AXES, *SOFT_AXES) if getattr(self, axis)
        )

    @classmethod
    def pass_open(cls, reason: str = "") -> "NoveltyVerdict":
        return cls(
            passes=True,
            feedback=reason,
            gate_metadata={"error": reason} if reason else {},
        )

    @classmethod
    def pass_unrouted(cls) -> "NoveltyVerdict":
        """No judge is routable for this call — the resolved provider is
        the built-in fake one.

        Distinct from :meth:`pass_open`, which spells "a judge exists and
        broke": the orchestrator counts that as ``gate_error_failopen``
        and alarms on a streak. An unrouted call is not a review at all
        and must stay off the scrape entirely, exactly like an orchestrator
        built with ``gate=None``. Provider routing is DB-backed and can
        change at runtime, so this is a per-call fact, not a bootstrap one.
        """
        return cls(passes=True, gate_metadata={UNROUTED_METADATA_KEY: True})

    @property
    def unrouted(self) -> bool:
        return bool(self.gate_metadata.get(UNROUTED_METADATA_KEY))


class NoveltyGatePort(Protocol):
    async def evaluate(
        self,
        context: NoveltyGateContext,
        *,
        character: Character | None = None,
    ) -> NoveltyVerdict:
        """Evaluate one candidate reply. Implementations must fail open."""
