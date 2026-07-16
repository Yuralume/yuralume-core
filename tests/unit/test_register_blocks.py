"""Unit tests for the shared register/diversity prompt blocks.

Extracted from ``infrastructure/prompt/default.py``; these assertions
lock the neutral/warm selection rule and the evidence rendering so the
encounter surface inherits exactly the chat behaviour.
"""

from __future__ import annotations

from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.infrastructure.prompt.register_blocks import (
    render_diversity_evidence_block,
    render_turn_register_block,
)


def _profile(**axes: float) -> RegisterProfile:
    vulnerable = bool(axes.pop("vulnerable_disclosure", False))
    return RegisterProfile(
        axes={
            "emotional_intensity": axes.get("emotional_intensity", 0.1),
            "seriousness": axes.get("seriousness", 0.1),
            "intimacy": axes.get("intimacy", 0.1),
            "humor_latitude": axes.get("humor_latitude", 0.5),
            "help_seeking": axes.get("help_seeking", 0.1),
        },
        confidence=0.8,
        note="平常日常閒聊",
        vulnerable_disclosure=vulnerable,
    )


def test_none_profile_falls_back_to_neutral_with_base_rail() -> None:
    lines = render_turn_register_block(None)
    text = "\n".join(lines)
    assert "語域剖面：未提供" in text
    # Base rail must be present regardless of profile availability.
    assert lines[0]


def test_low_intensity_profile_selects_neutral_increment() -> None:
    neutral = "\n".join(render_turn_register_block(_profile()))
    warm = "\n".join(
        render_turn_register_block(_profile(emotional_intensity=0.9)),
    )
    assert neutral != warm
    assert "語域剖面：" in neutral


def test_vulnerable_disclosure_earns_warm_increment() -> None:
    warm_by_flag = "\n".join(
        render_turn_register_block(_profile(vulnerable_disclosure=True)),
    )
    warm_by_axis = "\n".join(
        render_turn_register_block(_profile(help_seeking=0.7)),
    )
    assert warm_by_flag.splitlines()[:-2] == warm_by_axis.splitlines()[:-2]


def test_diversity_block_none_renders_empty() -> None:
    assert render_diversity_evidence_block(None) == []


def test_diversity_block_renders_evidence_lines() -> None:
    evidence = ReplyDiversityEvidence(
        assistant_line_count=6,
        max_self_similarity=0.912,
        mean_self_similarity=0.71,
        self_repetition_hint="最近三句都以「嗯，」開頭",
        phrase_frequency_lines=("『亮亮的東西』近 3 場出現 5 次",),
    )
    text = "\n".join(render_diversity_evidence_block(evidence))
    assert "只作 evidence" in text
    assert "0.912" in text
    assert "頻率窗" in text
    assert "亮亮的東西" in text


def test_default_builder_uses_shared_helpers() -> None:
    from kokoro_link.infrastructure.prompt import default as default_mod

    assert default_mod._render_turn_register_block is render_turn_register_block
    assert (
        default_mod._render_diversity_evidence_block
        is render_diversity_evidence_block
    )
