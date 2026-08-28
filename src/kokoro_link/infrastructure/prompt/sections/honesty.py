"""The always-on honesty discipline, and the "you cannot browse" line (HV2).

Why this is a section of its own instead of more text in the tool rail
--------------------------------------------------------------------
``tools_block`` already carries a strong honesty rule — but it opens with
``if not tools: return []``, so it exists only on turns that were offered
a tool. Two very ordinary situations therefore render it away at exactly
the moment it is needed:

* **The final hop of a tool turn.** ``ChatService._generate_reply_with_tools``
  hides the tool catalogue on the last hop on purpose (``tools_for_hop =
  []``) so the model must stop chaining and write to the player. That last
  hop is the one that produces the user-visible prose — and it is the only
  hop with no honesty rule in front of it.
* **A character with no tools at all.** ``allowed_tools`` empty, or a
  deployment with no orchestrator wired. Nothing can be called, so the
  model has no way to make "我剛查了一下" true — and nothing in the prompt
  says so.

The discipline below is therefore rendered on **every** chat turn, and its
text does not vary with the per-hop tool list. That constancy is also what
lets it sit in the cacheable prefix (DH5): a block whose bytes flip
between hop 0 and hop 1 would break the prefix for everything behind it.

Positive and negative, deliberately paired (FB rule 7)
------------------------------------------------------
The knowledge-boundary work of 2026-08-20 landed a rule this module obeys
literally: a section made only of prohibitions teaches over-avoidance. A
model told nothing but "don't claim things" starts refusing to narrate the
fiction it exists to narrate — it stops writing 「我走過去把窗簾拉上」
because that too is an action it did not "really" perform. So every
prohibition here is followed by the honest move that replaces it, and the
three admissible shapes (a promise about later, an action inside the
fiction, the player's own material read back) are named as *allowed*
rather than left for the model to infer.

The browsing line, and why it is conditional on a positive fact
---------------------------------------------------------------
:data:`BROWSING_TOOL_NAMES` are the two tools that let a character reach
the open web. On a deployment where neither is available to this
character, "我幫你點進去看看" is a promise that can never be kept, and the
follow-up ("我看過了，裡面說…") is invention. So the line is worth saying —
but only when we positively know the capability is absent.

``ToolsContext.character_tool_names`` is that fact, and it is a
**tri-state**: ``None`` means the caller never told us (a legacy call
site, a non-chat surface), and unknown must render nothing. The fail
direction matters more than it looks — telling a character that *does*
have ``web_search`` that it cannot browse would suppress every search it
should have run, which is the same over-avoidance failure in a different
costume. The per-hop ``available_tools`` list is unusable for this: it is
emptied on the final hop, so deriving absence from it would print the
line on every tool-enabled character's last hop.
"""

from typing import Final

from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    PromptSection,
    section,
)

BROWSING_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {"web_search", "web_fetch"},
)
"""The tools that constitute "this character can reach the open web".

``web_fetch`` counts as much as ``web_search``: the claim the line exists
to prevent is 「我點進去看過了」, and opening a link the player pasted is
``web_fetch``'s whole job. A deployment that mounted only one of the two
still has a character that can honestly say it looked something up, so
absence means *neither*.
"""

_BASELINE_LINES: Final[tuple[str, ...]] = (
    "",
    "誠實界線（不管這一輪有沒有工具可用，這段都成立）：",
    "- 一件對外的事，只有在你這一輪真的送出了工具呼叫、"
    "而且下一輪收到了工具結果之後，才算真的發生過。"
    "沒有工具結果就寫「我查到了」「圖傳給你了」「我點進去看過」"
    "「我幫你把行程改好了」，是拿對方對你的信任去換一句好聽的話。",
    "- ✅ 想做但此刻做不到，就照實說：「我等等幫你看看」「這個我不確定，你別當真」"
    "「我沒辦法自己去查，你貼給我好不好」——說「還沒」不會扣分，說謊才會。",
    "- ✅ 故事、想像、比喻裡的動作照寫不誤"
    "（「我走過去把窗簾拉上」「在夢裡牽著你的手」）："
    "那些發生在你和對方共享的虛構層，不是對外部系統的操作，不受這條限制。"
    "這條界線要擋的是「謊稱做過一件現實中可查證的事」，不是要你別演。",
    "- ✅ 對方自己傳來的圖、自己講過的事、自己貼上來的內容，"
    "你複述、引用或評論都不需要任何工具，那本來就在你眼前。",
)
"""Constant on every chat turn — see the module docstring on why."""

_NO_BROWSING_LINES: Final[tuple[str, ...]] = (
    "- 這個環境沒有給你上網的能力：你打不開網頁、點不了連結、查不到即時資訊。"
    "所以不要答應「我去看一下那個連結」「我幫你查最新的」，"
    "事後更不要說你看過了、查過了。",
    "- ✅ 對方貼連結或問到你可能不知道的近況時，"
    "可以請對方把重點貼上來給你看，或就你既有的知識聊——"
    "但要講清楚你的知識有時效，不保證是現在的狀況。",
)
"""Appended only when the caller positively declared a capability set with
no browsing tool in it."""


def browsing_unavailable(character_tool_names: tuple[str, ...] | None) -> bool:
    """Does this character positively have no way to reach the web?

    ``None`` is *unknown*, not *absent* — see the module docstring's fail
    direction. Exposed (rather than inlined) because the tri-state is the
    part of this section that is easy to get subtly wrong, and a named
    predicate is what a test can pin.
    """
    if character_tool_names is None:
        return False
    return not BROWSING_TOOL_NAMES.intersection(character_tool_names)


def _render_honesty_discipline_block(
    character_tool_names: tuple[str, ...] | None,
) -> list[str]:
    lines = list(_BASELINE_LINES)
    if browsing_unavailable(character_tool_names):
        lines.extend(_NO_BROWSING_LINES)
    return lines


def _honesty_discipline(ctx: PromptSectionContext) -> list[str]:
    return _render_honesty_discipline_block(ctx.tools.character_tool_names)


SECTIONS: tuple[PromptSection, ...] = (
    section("honesty_discipline", _honesty_discipline),
)
