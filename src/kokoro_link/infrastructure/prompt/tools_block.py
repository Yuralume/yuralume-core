"""The prompt section that tells a character what tools it has.

Split out of ``prompt.default`` because the block grew a second job:
it no longer just lists tool names, it has to teach *when each tool is
the right move* — and those judgements differ per tool far more than
they overlap.

**Why per-tool guidance instead of one shared rulebook.** The block
used to carry a single set of triggers, written when ``generate_image``
was the only tool that mattered, and rendered whether or not that tool
was on offer. Every judgement in it was visual ("is there a concrete
scene to show?"), so a model reading it about ``web_search`` concluded
that looking something up was *discouraged*: a question about last
week's news is exactly "抽象討論、沒有具體畫面可給". The instruction
that was meant to stop gratuitous selfies was suppressing search.

So each tool now brings its own triggers, and they are rendered only
when that tool is actually offered to this character. What stays shared
is the mechanical contract — the JSON shape, one call per reply, and
the honesty rule below.

**The honesty rule.** A model that decides against calling a tool
sometimes narrates the outcome anyway ("我剛查了一下…", "*（傳一張自拍）*"),
which is worse than not calling it: the player is told something
happened that did not. Both known shapes of this — fabricated search
results and parenthetical stage directions in place of a call — are
called out explicitly, for every tool, on every turn.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from kokoro_link.contracts.prompt import PromptToolDescriptor


@dataclass(frozen=True, slots=True)
class ToolUsageGuidance:
    """When one specific tool is, and is not, the right move.

    Rendered only alongside the tool it belongs to. Tools without an
    entry still work — they get their name, description and schema, and
    the model judges from those plus the shared rules — but a tool the
    model keeps declining to use is a sign its entry is missing or its
    triggers are too narrow.
    """

    call_when: tuple[str, ...]
    """Situations that should trigger a call. Phrased as recognisable
    conversational moments, not keyword lists — the model has to
    generalise to phrasings nobody enumerated."""
    skip_when: tuple[str, ...] = ()
    """Situations that look like a trigger but are not. Scoped to this
    tool: a reason to skip image generation must never read as a reason
    to skip search."""
    anti_patterns: tuple[str, ...] = ()
    """Failure modes observed for this tool specifically."""


_IMAGE_GUIDANCE: Final = ToolUsageGuidance(
    call_when=(
        "當你此刻要描述『自己正身處一個明確場景、做著一個明確動作或擺出明確姿態／表情』時，"
        "優先用這個工具把畫面傳給對方，再配一句自然、簡短的角色台詞，而不是只用文字長篇描寫。"
        "例如：你正在廚房煮東西、窩在沙發讀書、穿新衣服轉圈、剛洗完頭、趴在桌上偷看使用者。",
        "當使用者明確或委婉表達想看你的樣子／現在的場景"
        "（「現在在幹嘛」「拍給我看」「長什麼樣子」）時，必叫。",
        "當你的穿著、所在地、情緒、動作發生明顯變化、值得『給一眼』時，也可以主動生圖。",
    ),
    skip_when=(
        "純聊心情、抽象討論、此刻沒有具體畫面可給。",
        "剛剛已經發過類似的圖，畫面還沒推進到新的場景。",
    ),
    anti_patterns=(
        "不要用「*（生成一張圖片：小櫻坐在公園…）*」「*（傳一張自拍…）*」"
        "「*（拍一張照片…）*」這類括號旁白代替真正的工具呼叫——"
        "那只是一段文字，工具不會被執行，使用者看到的是一句沒有圖的敘述。",
    ),
)

_WEB_SEARCH_GUIDANCE: Final = ToolUsageGuidance(
    call_when=(
        "當對話需要一個你無法從自身知識確定的事實——最近發生的事、"
        "現在的狀態、會過期的數字（新聞、賽果、票價、上映與發售、版本、排行）——"
        "先查再說，而不是憑印象回答。",
        "**當你發現自己正要寫出「我記得好像」「應該是」「我不太確定」「可能是」這類措辭時，"
        "那就是該搜尋的訊號**：那份猶豫本身代表你缺一個事實，查完再回答。",
        "當使用者提到一個你不認得的作品、人物、團體、產品、地點或術語時，"
        "查清楚它是什麼，再決定角色怎麼反應——不要含糊帶過或猜測。",
        "當使用者問「你知道⋯嗎」「聽說⋯」「最近⋯怎麼樣了」而你的知識可能已經過時時。",
    ),
    skip_when=(
        "問題是關於你自己、關於你和使用者之間的事、或關於你的記憶與設定——"
        "那些在你腦中，不在網路上。",
        "這一輪已經查過同一件事，或使用者只是閒聊、抒發情緒、需要的是陪伴而不是資訊。",
    ),
    anti_patterns=(
        "**絕對不要在沒有實際呼叫工具的情況下，寫出「我剛查了一下」「我看到新聞說」"
        "「網路上寫」這類句子**——那是憑空捏造消息給信任你的人，"
        "比誠實說「我不確定，我查查看」糟糕得多。",
        "不要因為「查資料很不像角色會做的事」而放棄搜尋。"
        "查詢過程對使用者是隱形的，使用者只會看到你最後用角色語氣講出來的內容；"
        "知道得準確不會讓你不像你自己。",
    ),
)

_TOOL_GUIDANCE: Final[dict[str, ToolUsageGuidance]] = {
    "generate_image": _IMAGE_GUIDANCE,
    "web_search": _WEB_SEARCH_GUIDANCE,
}


def render_tools_block(
    tools: list[PromptToolDescriptor],
    *,
    forced_tool_name: str | None = None,
) -> list[str]:
    """Instruct the model about available tools + the JSON call format.

    The format is deliberately single-call-per-reply — we don't try to
    support multi-call parallel dispatch yet. The model either replies
    in natural language to the user, or emits a fenced JSON block that
    parses to ``{"tool": name, "args": {...}}``. The chat orchestrator
    catches the JSON, runs the tool, and re-prompts with the result
    injected, so the final user-visible reply can reference it.

    When ``forced_tool_name`` is set, the fixed command trigger
    fired — the user explicitly asked for this tool's output. We inject
    a hard directive that overrides the normal "judge whether a tool
    fits" framing: this turn must emit a JSON call to that tool, with
    arguments the model picks from conversation context (so the image
    still reflects the scene being discussed, not the raw command).
    """
    if not tools:
        return []
    lines: list[str] = []
    if forced_tool_name:
        lines.extend(_render_forced_call(forced_tool_name))
    lines.append(
        "可用工具：這些工具能讓你的回覆更有臨場感、更可信，"
        "符合下列情境時請主動使用，不要等使用者開口："
    )
    for tool in tools:
        lines.append("")
        lines.extend(_render_tool_entry(tool))
    lines.extend(_render_call_contract())
    lines.extend(_render_shared_anti_patterns(tools))
    return lines


def _render_forced_call(forced_tool_name: str) -> list[str]:
    return [
        f"⚡ 本回合強制工具呼叫：使用者訊息命中了操作者設定的觸發規則，"
        f"**這回合必須呼叫 `{forced_tool_name}` 工具**，禁止純文字回覆。",
        "- 參數（例如 `generate_image` 的 `positive`）請依當前對話情境與角色處境自行決定，"
        "不要照抄使用者的觸發指令字面（例如 `/pic`、`幫我畫` 這類命令前綴不要當成畫面內容）。",
        "- 若使用者觸發指令後方有補充描述（例如「/pic 咖啡廳窗邊的側臉」的「咖啡廳窗邊的側臉」），"
        "該補充是使用者對畫面的偏好提示，請優先融入參數；"
        "沒有補充時就完全依據近期對話與當下場景自行構圖。",
        "- 下一輪你會收到工具結果，再以角色身份寫一句自然的收尾台詞即可。",
        "",
    ]


def _render_tool_entry(tool: PromptToolDescriptor) -> list[str]:
    """One tool: identity, schema, and its own call / skip judgements."""
    lines = [f"### {tool.name}", f"- 用途：{tool.description}"]
    try:
        schema_text = json.dumps(
            tool.parameters_schema, ensure_ascii=False, indent=None,
        )
    except (TypeError, ValueError):
        schema_text = "{}"
    lines.append(f"- 參數 schema：{schema_text}")
    guidance = _TOOL_GUIDANCE.get(tool.name)
    if guidance is None:
        return lines
    if guidance.call_when:
        lines.append(f"- 該用 {tool.name} 的時機：")
        lines.extend(f"  - {item}" for item in guidance.call_when)
    if guidance.skip_when:
        lines.append(f"- 不必用 {tool.name} 的時機：")
        lines.extend(f"  - {item}" for item in guidance.skip_when)
    return lines


def _render_call_contract() -> list[str]:
    return [
        "",
        "呼叫方式：想呼叫工具時，這回合就**只**輸出以下 JSON，"
        "不要任何前後文、不要對話、不要旁白、不要表情符號：",
        "```json",
        '{"tool": "工具名稱", "args": {...}}',
        "```",
        "一回合只能呼叫一個工具。工具執行完後你會收到結果，"
        "下一輪再以角色身份自然地回覆使用者。",
    ]


def _render_shared_anti_patterns(tools: list[PromptToolDescriptor]) -> list[str]:
    """Mechanical mistakes + the honesty rule, plus per-tool failures.

    The honesty rule leads because it is the one that hurts the player:
    a fabricated tool outcome is misinformation delivered in a trusted
    voice, while a malformed JSON blob is merely ugly.
    """
    lines = [
        "",
        "嚴禁的錯誤模式（非常重要、常見錯誤）：",
        "❌ **沒有實際輸出工具 JSON，就不准在回覆裡表現得好像用過工具。**"
        "只有這回合輸出了 JSON、下一輪收到工具結果，才算真的用過。"
        "想做某件事就真的呼叫工具；不想呼叫就別提它，或誠實說你不確定。",
        "❌ 不要把工具呼叫寫成旁白（`*（…）*` 這類括號敘述）——工具不會被執行。",
        "❌ 不要在自然語言回覆中夾 JSON；也不要在 JSON 前後補「好的我來查」這類解釋。",
        "✅ 正確：要用工具 → 這回合整個輸出就是一段 JSON；"
        "要聊天 → 完全不出現 JSON，也不出現任何暗示你剛用過工具的說法。",
    ]
    for tool in tools:
        guidance = _TOOL_GUIDANCE.get(tool.name)
        if guidance is None:
            continue
        lines.extend(f"❌ {item}" for item in guidance.anti_patterns)
    lines.extend([
        "",
        "若這回合沒有任何工具符合上述時機，就直接用角色的語氣以自然語言回覆，"
        "不要輸出任何 JSON——但也不要在文字裡假裝你使用過任何工具。",
    ])
    return lines
