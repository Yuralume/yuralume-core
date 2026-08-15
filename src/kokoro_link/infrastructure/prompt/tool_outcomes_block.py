"""The prompt section that reports what the tools just returned.

Extracted from ``prompt.default`` because chat is no longer the only
surface that runs a tool and then asks the model to write about the
result: the promise-fulfilment composers (scheduled promise today,
busy-defer follow-up next) run the same compose → tool → compose loop
(:mod:`kokoro_link.application.services.composer_tool_loop`) and need
the *same* wording — including the failure branch.

The failure branch is the reason this block is shared rather than
re-invented per surface. A character who promised to look something up
and whose search failed must say so ("查不到，等等再幫你看"); a
character whose camera (ComfyUI) is down must say the camera is down.
Dropping the failure and letting the model write as if nothing was
attempted is how a promise turns into a silent broken promise.
"""

from __future__ import annotations

from kokoro_link.contracts.prompt import ToolOutcomeMessage


def render_tool_outcomes_block(outcomes: list[ToolOutcomeMessage]) -> list[str]:
    """Tell the model what the tools it called just returned.

    We keep the payload human-readable rather than echoing the full
    JSON — the model is about to write a reply to the *user*, so it
    needs enough context ("I just generated an image of …") without
    getting distracted by URL structure.
    """
    if not outcomes:
        return []
    lines: list[str] = ["工具回傳結果（已執行，請在回覆中自然地交代結果）："]
    for outcome in outcomes:
        if outcome.ok:
            lines.append(
                f"- {outcome.tool_name} 成功：{outcome.output_text or '（無文字輸出）'}"
            )
            if outcome.attachment_urls:
                lines.append(
                    f"  產出檔案：{len(outcome.attachment_urls)} 個（已附在這則回覆一起送給使用者）"
                )
        else:
            lines.append(
                f"- {outcome.tool_name} 失敗：{outcome.error or '未知錯誤'}"
                "（請以角色語氣向使用者簡短致歉，不要重試）"
            )
    lines.append(
        "只能根據上面實際回傳的內容作答。回傳的資訊不足以回答時，"
        "就照實說沒查到，不要自行補上工具沒給你的細節。"
    )
    return lines


TOOL_PASS_CLOSING_HINT = (
    "（如果你需要先用上面的工具查證或製作東西才能真的履行這個承諾，"
    "這一輪就只輸出工具 JSON，不要寫任何訊息內容；工具結果會在下一輪交給你。）"
)
"""Closing directive for the *first* pass of a two-pass compose.

Lives next to the tool blocks rather than inside the prompt template
because it is conditional: the template's closing line ("直接寫出你要
傳的訊息") is correct on every single-pass call, and only a call that
actually offers tools may say "or emit JSON instead". Same reasoning
as :mod:`kokoro_link.infrastructure.prompt.tools_block`.
"""


__all__ = ["TOOL_PASS_CLOSING_HINT", "render_tool_outcomes_block"]
