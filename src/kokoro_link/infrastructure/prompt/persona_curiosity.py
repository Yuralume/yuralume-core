"""Prompt rendering helpers for conversational persona discovery."""

from __future__ import annotations

from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan


def render_persona_curiosity_plan_lines(
    plan: PersonaCuriosityPlan | None,
    *,
    surface: str,
) -> list[str]:
    if plan is None or not plan.should_ask:
        return []
    lines = [
        "自然認識對方的候選意圖：",
        "- 這只是一個候選動機；是否值得使用仍要依當下互動、額度、未回覆狀態與角色心境判斷。",
        "- 不要說你在蒐集資料、完善畫像、做問卷或填表單。",
        f"- 目標層級：Layer {plan.target_layer}",
        f"- 目標主題：{_clip(plan.target_topic, 80)}",
        f"- 語氣策略：{_clip(plan.tone_strategy, 160)}",
        f"- 問題意圖：{_clip(plan.question_intent, 260)}",
        f"- 安全理由：{_clip(plan.safety_reason, 220)}",
        "- 探索不必用問句收尾；也可以先分享你自己的相關經驗或反應，讓對方自然接話。",
    ]
    if surface == "proactive":
        lines.extend(
            [
                "- 主動訊息要比聊天更克制：只有當這個探索本身像一個自然、可接話的關心，才值得消耗今日主動訊息額度。",
                "- 若上方顯示連續未回覆或最近已送出類似探索，先處理關係/情緒反應或保持沉默，不要追問個人資訊。",
                "- 一則 proactive 最多一個輕問題；不要連續追資料，不要把對方逼進回答義務。",
            ],
        )
    else:
        lines.append("- 一則回覆最多一個自然問題；若對方正在求助或交辦任務，先處理當下。")
    if plan.avoid:
        lines.append("- 避開：")
        for item in plan.avoid[:5]:
            cleaned = item.strip()
            if cleaned:
                lines.append(f"  · {_clip(cleaned, 120)}")
    return lines


def _clip(value: str, limit: int) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"
