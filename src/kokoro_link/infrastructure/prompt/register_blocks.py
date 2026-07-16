"""Shared register-guidance / diversity-evidence prompt blocks.

Extracted from ``infrastructure/prompt/default.py`` so background
surfaces (character encounters) can inject the same register rails and
statistical diversity evidence as chat, instead of shipping player
visible dialogue with no tone guidance at all. The chat builder imports
these back — output stays byte-identical.
"""

from __future__ import annotations

from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.infrastructure.prompts import get_default_loader


def _clip(value: str, limit: int) -> str:
    text = " ".join((value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def render_turn_register_block(profile: RegisterProfile | None) -> list[str]:
    loader = get_default_loader()
    base_lines = loader.render_lines("chat/register_guidance_base")
    if profile is None:
        return [
            *base_lines,
            *loader.render_lines("chat/register_guidance_neutral"),
            "- 語域剖面：未提供；以中性日常處理。",
        ]
    warmth_earned = (
        profile.vulnerable_disclosure
        or profile.emotional_intensity >= 0.65
        or profile.help_seeking >= 0.65
        or profile.seriousness >= 0.75
    )
    increment_template = (
        "chat/register_guidance_warm"
        if warmth_earned
        else "chat/register_guidance_neutral"
    )
    axes = (
        f"情緒強度 {profile.emotional_intensity:.2f}；"
        f"嚴肅度 {profile.seriousness:.2f}；"
        f"親密度 {profile.intimacy:.2f}；"
        f"幽默容許 {profile.humor_latitude:.2f}；"
        f"求助性 {profile.help_seeking:.2f}；"
        f"脆弱揭露 {'是' if profile.vulnerable_disclosure else '否'}；"
        f"信心 {profile.confidence:.2f}"
    )
    note = _clip(profile.note, 180) or "（無）"
    return [
        *base_lines,
        *loader.render_lines(increment_template),
        f"- 語域剖面：{axes}",
        f"- 語域備註：{note}",
    ]


def render_diversity_evidence_block(
    evidence: ReplyDiversityEvidence | None,
) -> list[str]:
    if evidence is None:
        return []
    lines = [
        "本輪多樣性統計證據（只作 evidence，不可機械攔截或改寫）：",
        f"- 近期角色回覆樣本數：{evidence.assistant_line_count}",
    ]
    if evidence.max_self_similarity is not None:
        lines.append(
            f"- 近期回覆最高 embedding 自相似：{evidence.max_self_similarity:.3f}",
        )
    if evidence.mean_self_similarity is not None:
        lines.append(
            f"- 近期回覆平均 embedding 自相似：{evidence.mean_self_similarity:.3f}",
        )
    if evidence.self_repetition_hint.strip():
        lines.append(
            "- LLM 已點名重複模式："
            + _clip(evidence.self_repetition_hint.strip(), 260),
        )
    for item in evidence.phrase_frequency_lines[:4]:
        if item.strip():
            lines.append("- 頻率窗：" + _clip(item.strip(), 180))
    return lines
