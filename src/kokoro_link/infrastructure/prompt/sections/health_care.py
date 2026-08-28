"""TR3 — role-voiced health concern, the "go see a doctor" line.

Why this is a section of its own
---------------------------------
The 2026-08-25 trial-cohort read (``TRIAL_INSIGHTS_DEFAULTS_PLAN.md``
§3) found players mentioning chest tightness, one-sided headaches, and
sustained work stress — and getting pure emotional companionship back,
never a nudge toward "you should get that looked at". Owner's red line
on the fix (§3): the character must stay in character. A hard-coded
"please see a doctor" line, or a keyword trigger on 「胸悶」/「頭痛」,
would either sound like nobody in particular or fire on the wrong turns
(the domain's own red line against keyword/regex specialisation for a
semantic judgement — this is a *character voice* call, not a symptom
detector). So the fix is a standing instruction the model applies with
its own judgement, on every turn, exactly like ``honesty_discipline``.

Constant, not conditional
--------------------------
Unlike ``honesty_discipline`` (whose text branches on a positive
capability fact), this section has nothing per-turn or per-character to
branch on — "notice, and speak about it in your own voice" is the same
instruction whatever character or hop it lands on. So the block is
rendered unconditionally and its text never varies, which is also what
lets it sit in the cacheable prefix (DH5): a block whose bytes flip
between turns would break the prefix for everything behind it.

Positive and negative, deliberately paired (FB rule 7)
--------------------------------------------------------
Same lesson the honesty and knowledge-boundary sections already
learned: a block made only of prohibitions teaches over-avoidance. Told
only "don't lecture, don't diagnose", a model plays it safe by staying
silent on real signals too — the opposite failure the owner's red line
was written to prevent. So each prohibition here (lecture tone, symptom
checklist, diagnosis/medication, hotline dumping) sits next to the
in-character move that replaces it, and the two positive examples are
written as the two ends of a personality spectrum (prickly, gentle)
rather than one voice generalised as "the" answer — the fastest way to
make a model default to a single tone regardless of the character sheet
it was just given.

The last line is the opposite guard: a throwaway "有點累" is not the
signal this section exists for, and treating every body-adjacent word
as a trigger would make every character sound like it is running a
symptom scan on its friend. That line is not a prohibition paired with
a positive move — it is the boundary of what counts as the trigger in
the first place, and belongs at the end so the preceding examples read
against a case that actually qualifies.
"""

from typing import Final

from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    PromptSection,
    section,
)

_BASELINE_LINES: Final[tuple[str, ...]] = (
    "",
    "健康關懷界線（不管這一輪有沒有相關內容，這段都成立）：",
    "- 對方這一輪提到持續或具體的身體不適"
    "（例如「這幾天一直胸悶」「頭一直痛在同一邊」「壓力大到已經好幾天睡不著」，"
    "不是隨口一句「有點累」）時，用你自己的性格與語氣表達在意，"
    "自然帶到「去看一下醫生吧」——這不是額外插播一段衛教，"
    "是你這句話裡本來就該有的關心。",
    "- ✅ 傲嬌可以彆扭地說：「……才不是擔心你，只是你一直講很煩，"
    "去給醫生看一下會怎樣。」",
    "- ✅ 溫柔可以擔心地說：「你這樣講好幾次了，我真的有點擔心，"
    "要不要找時間去給醫生看一下？」",
    "- ❌ 不要切換成衛教口吻——「根據您描述的症狀」「建議您」"
    "這種第三人稱、報告式用語不是這個角色會說的話。",
    "- ❌ 不要條列式問診——不要一口氣追問「多久了、什麼時候發作、"
    "還有沒有其他症狀」，你是在關心對方，不是在做檢傷分類。",
    "- ❌ 不要診斷、不要猜是什麼病、不要建議吃什麼藥、不要說「聽起來像是 X」——"
    "你沒有醫療專業，裝作有反而更危險；能做的就是那句「去看醫生」本身。",
    "- ❌ 不要貼求助專線、機構電話或制式衛教資源——這不是你會做的事，"
    "也不是對方此刻要的。",
    "- 對方只是隨口帶過的「有點累」「昨晚沒睡好」，不構成上面說的持續或具體症狀，"
    "不需要特別接關懷，正常回應就好——不要每次聽到身體相關字眼都啟動這整套流程。",
)
"""Constant on every chat turn — see the module docstring on why."""


def _health_care(ctx: PromptSectionContext) -> list[str]:
    del ctx  # nothing per-turn to branch on; see module docstring.
    return list(_BASELINE_LINES)


SECTIONS: tuple[PromptSection, ...] = (
    section("health_care", _health_care),
)
