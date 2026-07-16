"""Tests for shared state tone labels used across prompt surfaces."""

from kokoro_link.infrastructure.prompt.state_tone import (
    affection_tone,
    energy_tone,
    fatigue_tone,
    trust_tone,
)


def test_relationship_tone_boundaries_are_shared_prompt_labels() -> None:
    assert affection_tone(80) == "非常親近，可以主動撒嬌、分享、開玩笑"
    assert affection_tone(39) == "偏低，回應簡短、語氣平淡、不主動示好"
    assert trust_tone(19) == "很低，明顯不信任、懷疑對方動機、可以直接質問或拒絕回答"


def test_vitality_tone_boundaries_are_shared_prompt_labels() -> None:
    assert fatigue_tone(80) == "非常疲憊，語氣可帶倦意、句子精簡、希望早點休息"
    assert fatigue_tone(49) == "狀態輕鬆，不需表現疲態"
    assert energy_tone(39) == "低能量，語氣偏慢、回覆節奏放緩"
