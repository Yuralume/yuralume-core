"""Activity aftermath port.

When a schedule activity completes, the memorialiser writes an episodic
memory describing what happened ("14:00-15:30 在咖啡廳寫劇本"). That
alone is dry — it captures the fact but not the *feeling*. Real people
walk out of a meeting still annoyed, or out of a date still buzzing.

This port asks the LLM to read the persona + the activity + companions
+ busy_score and produce a short emotional residue (e.g. "早上被鄰居
大媽追問感情狀況，很煩躁"), which the memorialiser then folds into the
memory's content and tags as ``aftermath``. The prompt builder later
surfaces fresh aftermath memories prominently so the character can
naturally bring them up in the next chat ("早上那個大媽超煩的——").

Per the project's top directive: judgment is the LLM's — we don't
enumerate "activity X → emotion Y" rules. Persona + activity context
go in, a short residue summary comes out, and the same activity affects
different personas differently because the LLM sees their persona axes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.services.shared_activity_evidence import (
    SharedActivityEvidence,
)


@dataclass(frozen=True, slots=True)
class ActivityAftermath:
    """LLM-judged emotional residue from a completed activity.

    ``residue_summary`` is a short fragment in the operator's content
    language that reads as the character's internal note: "早上被大媽追問
    感情很煩", "still buzzing after lunch with a coworker". Appended to
    the episodic memory content so the next chat's prompt naturally picks
    it up via the existing memory recall path — no separate storage table
    required.

    ``emotion_tag`` is an optional one-word mood label ("煩躁" / "annoyed"
    / "雀躍") — written into the memory tags so future ranking can prefer
    aftermath memories whose mood matches the user's current message.

    Empty values mean "no notable residue" — the memorialiser falls
    back to the bare activity description.
    """

    residue_summary: str = ""
    emotion_tag: str = ""
    factual_summary: str = ""
    """LLM-written factual framing of what actually happened, requested
    only for activities carrying an operator commitment (CF4).

    For those, the planner-written ``description`` ("與木木一起前往先前約定
    的刨冰店") is a *plan*, not a record — folding it into a memory verbatim
    is what manufactured the "we already went together" narrative the user
    never lived. When the memorialiser supplies
    :class:`SharedActivityEvidence`, the model rewrites the semantic body
    under that evidence ("本來想約他一起去，最後自己去了") and the
    memorialiser uses *this* line as the memory body instead of the
    description. Blank means the model declined or was unavailable — the
    memorialiser then writes no memory at all rather than falling back to
    the unproven plan text.
    """

    @property
    def is_empty(self) -> bool:
        return (
            not self.residue_summary.strip()
            and not self.emotion_tag.strip()
            and not self.factual_summary.strip()
        )


class ActivityAftermathPort(Protocol):
    async def judge(
        self,
        *,
        character: Character,
        activity: ScheduleActivity,
        operator_primary_language: str = "zh-TW",
        evidence: SharedActivityEvidence | None = None,
    ) -> ActivityAftermath:
        """Return the emotional residue this activity left on the character.

        ``evidence`` carries the structured verdict on whether the operator
        was really part of this activity (CF4). ``None`` / a
        ``NOT_APPLICABLE`` policy means "ordinary solo activity" and the
        adapter behaves exactly as before. Anything else obliges the
        adapter to put the facts *and* the claim rule in the prompt and to
        ask for a ``factual_summary``; the judgement of how to word it is
        the model's, never a keyword rewrite on our side.

        ``operator_primary_language`` (BCP 47) is the content language for
        the player-visible residue: the memorialiser folds
        ``residue_summary`` verbatim into episodic memory content shown in
        MemoryBrowserPanel, so a non-Chinese operator must not see Chinese
        residue sentences inside their character's memories. Defaults to
        ``zh-TW`` (ship-first) so legacy callers keep working.

        Must be fail-soft: any internal error (model timeout, parse fail,
        empty response) should return :class:`ActivityAftermath` with
        blank fields rather than raise. The memorialiser treats a blank
        result as "no residue to fold in" and keeps the bare-activity
        memory it would have written without the port.
        """
        ...
