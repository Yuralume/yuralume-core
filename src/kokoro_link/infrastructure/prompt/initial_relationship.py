"""Prompt renderer for initial relationship seed."""

from __future__ import annotations

from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)


def render_initial_relationship_seed_lines(
    seed: CharacterOperatorRelationshipSeed | None,
    *,
    include_address: bool = True,
) -> list[str]:
    """Render the relationship-seed context block.

    ``include_address`` controls the two address lines (稱呼使用者 /
    使用者怎麼稱呼你). The main chat prompt sets it ``False`` because the
    bidirectional address resolver now owns those terms (so the seed,
    learned persona, and global profile don't render three competing
    名字). Auxiliary prompts and the player-facing persona-snapshot API
    keep the default ``True`` so their behaviour and contract are
    unchanged.
    """
    if seed is None or seed.is_empty:
        return []
    lines = [
        "使用者創角時確認的起始關係設定：",
        "- 這些內容只能用於稱謂、語氣距離與互動邊界；不可改寫成已發生過的系統內記憶。",
        "- 未提供的共同經歷不得補完；資訊不足時請自然詢問，而不是假裝知道。",
    ]
    _append(lines, "關係", seed.relationship_label)
    _append(lines, "可知道的背景", seed.known_context)
    _append(lines, "居住安排", seed.living_arrangement)
    if include_address:
        _append(lines, "稱呼使用者", seed.user_address_name)
        _append(lines, "使用者怎麼稱呼你", seed.character_address_name)
    _append(lines, "語氣距離", seed.tone_distance)
    _append(lines, "熟悉度邊界", seed.familiarity_boundary)
    _append(lines, "行程中的使用者參與程度", _schedule_policy_text(seed))
    if seed.proactive_permission:
        _append(lines, "主動訊息授權", "使用者允許創角後主動找她／他，但必須遵守頻率與邊界。")
        _append(lines, "主動訊息頻率或時機", seed.proactive_cadence_hint)
    else:
        lines.append("- 主動訊息授權：沒有明確授權；不要把起始關係解讀成可直接打擾。")
    _append(lines, "使用者補充", seed.user_profile_notes)
    return lines


def _append(lines: list[str], label: str, value: str) -> None:
    text = (value or "").strip()
    if text:
        lines.append(f"- {label}：{text}")


def _schedule_policy_text(seed: CharacterOperatorRelationshipSeed) -> str:
    policy = seed.schedule_involvement_policy
    if policy == "mention_only":
        return "可準備話題或想起使用者偏好，但不可說成已約好共同活動。"
    if policy == "invite_required":
        return "可以安排想邀請使用者的活動，但必須說成邀請或詢問，不可當成已約定。"
    if policy == "shared_allowed":
        return "可以安排使用者明確允許的共同日常，但仍不可杜撰未提供的共同往事。"
    return "不把使用者排進角色行程。"
