"""LLMStoryArcPlanner tests — scene-structure parsing & robustness.

Covers Phase 1 of ``docs/SCENE_BEAT_PLAN.md``:
- Beats now carry ``scene_type`` / ``location`` / ``scene_characters`` /
  ``dramatic_question`` / ``required`` and a planner regression must not
  silently drop them.
- Planner output is fuzzy: lists may be returned as comma-strings,
  scene_type may be off-list, ``required`` may be missing entirely.
  Each fallback is verified explicitly so a smaller / older LLM still
  produces usable beats instead of validation crashes.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    SCENE_CONFLICT,
    SCENE_ENCOUNTER,
    SCENE_REVELATION,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.story.llm_arc_planner import (
    LLMStoryArcPlanner,
    NullStoryArcPlanner,
)


class _FakeModel:
    """Returns a fixed payload — used for deterministic parser tests."""

    supports_vision = False

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_prompt: str | None = None

    async def generate(
        self, prompt: str, *, image_urls=None,  # noqa: ARG002
    ) -> str:
        self.last_prompt = prompt
        return self._response

    def generate_stream(
        self, prompt: str, *, image_urls=None,  # noqa: ARG002
    ):
        async def _empty():
            if False:
                yield ""
        return _empty()


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="插畫家",
        personality=["內向"],
        interests=["音樂"],
        speaking_style="溫柔",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _build_payload(beats: list[dict]) -> str:
    return json.dumps(
        {
            "title": "三週的試鏡",
            "premise": "她報名了一場從沒想過會報的試鏡。",
            "theme": "ambition",
            "beats": beats,
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_parses_full_scene_structure() -> None:
    payload = _build_payload([
        {
            "day_offset": 0,
            "title": "公告張貼",
            "summary": "週一早上她在公告欄看見海報。" * 4,
            "tension": "setup",
            "scene_type": "encounter",
            "location": "學校公告欄",
            "scene_characters": ["凜"],
            "dramatic_question": "她敢報名嗎？",
            "required": True,
        },
        {
            "day_offset": 6,
            "title": "撞牆",
            "summary": "音樂教室的鏡子裡只剩自己。" * 4,
            "tension": "rising",
            "scene_type": "conflict",
            "location": "音樂教室",
            "scene_characters": ["指導老師"],
            "dramatic_question": "她要承認嗎？",
            "required": True,
        },
        {
            "day_offset": 12,
            "title": "頓悟",
            "summary": "雨後的長椅讓她終於想清楚。" * 4,
            "tension": "rising",
            "scene_type": "revelation",
            "location": "校園長椅",
            "scene_characters": ["指導老師"],
            "dramatic_question": "她願意調整方向嗎？",
            "required": False,
        },
    ])
    planner = LLMStoryArcPlanner(model=_FakeModel(payload))
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
    )
    assert len(arc.beats) == 3
    first, second, third = arc.beats
    assert first.scene_type == SCENE_ENCOUNTER
    assert first.location == "學校公告欄"
    assert first.scene_characters == ("凜",)
    assert first.dramatic_question == "她敢報名嗎？"
    assert first.required is True
    assert second.scene_type == SCENE_CONFLICT
    assert second.scene_characters == ("指導老師",)
    assert third.scene_type == SCENE_REVELATION
    # `required=False` survives the round trip.
    assert third.required is False


@pytest.mark.asyncio
async def test_missing_scene_fields_fall_back_to_safe_defaults() -> None:
    # Older planner output / pre-Phase-1 prompts return only the
    # original keys. The new pipeline must still produce valid beats.
    payload = _build_payload([
        {
            "day_offset": 0,
            "title": "公告",
            "summary": "她看見公告欄的海報。" * 5,
            "tension": "setup",
        },
        {
            "day_offset": 7,
            "title": "練習",
            "summary": "練到深夜的鋼琴室。" * 5,
            "tension": "rising",
        },
        {
            "day_offset": 14,
            "title": "結果",
            "summary": "走出試鏡廳的那一刻。" * 5,
            "tension": "climax",
        },
    ])
    planner = LLMStoryArcPlanner(model=_FakeModel(payload))
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
    )
    for beat in arc.beats:
        # Defaults: scene_type=encounter, no location/question, empty
        # NPC list, required=True (legacy main-line semantics).
        assert beat.scene_type == SCENE_ENCOUNTER
        assert beat.location is None
        assert beat.dramatic_question is None
        assert beat.scene_characters == ()
        assert beat.required is True


@pytest.mark.asyncio
async def test_unknown_scene_type_falls_back_to_encounter() -> None:
    payload = _build_payload([
        {
            "day_offset": 0,
            "title": "起點",
            "summary": "這是起點。" * 6,
            "tension": "setup",
            "scene_type": "inner_monologue",  # not in canonical list
        },
        {
            "day_offset": 7,
            "title": "中段",
            "summary": "中段的生活。" * 6,
            "tension": "rising",
            "scene_type": "REVELATION",  # case-insensitive accepted
        },
        {
            "day_offset": 14,
            "title": "結束",
            "summary": "走向結束。" * 6,
            "tension": "resolution",
        },
    ])
    planner = LLMStoryArcPlanner(model=_FakeModel(payload))
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
    )
    assert arc.beats[0].scene_type == SCENE_ENCOUNTER
    assert arc.beats[1].scene_type == SCENE_REVELATION


@pytest.mark.asyncio
async def test_scene_characters_accepts_comma_string() -> None:
    # Smaller LLMs sometimes return "A, B" instead of ["A", "B"].
    payload = _build_payload([
        {
            "day_offset": 0,
            "title": "起點",
            "summary": "這是起點。" * 6,
            "tension": "setup",
            "scene_characters": "夏目, 凜, ",  # trailing empty piece
        },
        {
            "day_offset": 7,
            "title": "中段",
            "summary": "中段的生活。" * 6,
            "tension": "rising",
            "scene_characters": "",  # empty string → empty tuple
        },
        {
            "day_offset": 14,
            "title": "結束",
            "summary": "走向結束。" * 6,
            "tension": "resolution",
            "scene_characters": ["佐藤", 123, "凜", "凜"],  # mixed types + dupes
        },
    ])
    planner = LLMStoryArcPlanner(model=_FakeModel(payload))
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
    )
    assert arc.beats[0].scene_characters == ("夏目", "凜")
    assert arc.beats[1].scene_characters == ()
    # Non-string entries dropped, dupes deduped.
    assert arc.beats[2].scene_characters == ("佐藤", "凜")


@pytest.mark.asyncio
async def test_required_field_string_coercion() -> None:
    payload = _build_payload([
        {
            "day_offset": 0,
            "title": "a",
            "summary": "一段內容。" * 6,
            "tension": "setup",
            "required": "false",  # JSON-stringy false
        },
        {
            "day_offset": 7,
            "title": "b",
            "summary": "中段內容。" * 6,
            "tension": "rising",
            "required": 0,  # int 0 → False
        },
        {
            "day_offset": 14,
            "title": "c",
            "summary": "結束內容。" * 6,
            "tension": "resolution",
            "required": "yes",  # truthy string → True
        },
    ])
    planner = LLMStoryArcPlanner(model=_FakeModel(payload))
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
    )
    assert arc.beats[0].required is False
    assert arc.beats[1].required is False
    assert arc.beats[2].required is True


@pytest.mark.asyncio
async def test_synthetic_arc_carries_scene_structure() -> None:
    # NullStoryArcPlanner / LLM failure fallback both go through
    # `_synthetic_arc` — its beats must satisfy the new prompt builder.
    planner = NullStoryArcPlanner()
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=5,
    )
    assert arc.beats, "synthetic arc must have beats"
    for beat in arc.beats:
        assert beat.scene_type
        assert beat.dramatic_question is not None
        assert beat.required is True


@pytest.mark.asyncio
async def test_prompt_mentions_new_schema() -> None:
    # Smoke: planner prompt actually instructs the LLM about the new
    # fields. Catches regressions where someone reverts the prompt
    # but leaves the parser expecting the new schema.
    fake = _FakeModel(_build_payload([
        {
            "day_offset": 0, "title": "x", "summary": "一段。" * 6,
            "tension": "setup",
        },
    ]))
    planner = LLMStoryArcPlanner(model=fake)
    await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
    )
    assert fake.last_prompt is not None
    for token in (
        "scene_type",
        "scene_characters",
        "dramatic_question",
        "required",
        "encounter",
    ):
        assert token in fake.last_prompt, f"missing {token!r} from prompt"
