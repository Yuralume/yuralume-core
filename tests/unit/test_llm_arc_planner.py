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
from kokoro_link.domain.entities.story_seed import (
    SEED_TIER_DRAMATIC,
    StorySeed,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.story.llm_arc_planner import (
    LLMStoryArcPlanner,
    NullStoryArcPlanner,
    _build_prompt,
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


class _ExplodingModel(_FakeModel):
    """Model whose call always fails — exercises the synthetic fallback."""

    def __init__(self) -> None:
        super().__init__("")

    async def generate(
        self, prompt: str, *, image_urls=None,  # noqa: ARG002
    ) -> str:
        self.last_prompt = prompt
        raise RuntimeError("upstream is down")


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


def _build_payload(beats: list[dict], **extra) -> str:  # noqa: ANN003
    return json.dumps(
        {
            "title": "三週的試鏡",
            "premise": "她報名了一場從沒想過會報的試鏡。",
            "theme": "ambition",
            "beats": beats,
            **extra,
        },
        ensure_ascii=False,
    )


def _one_beat_payload(**extra) -> str:  # noqa: ANN003
    return _build_payload(
        [{
            "day_offset": 0, "title": "x", "summary": "一段。" * 6,
            "tension": "setup",
        }],
        **extra,
    )


def _seed(text: str) -> StorySeed:
    return StorySeed.create(seed_text=text, tier=SEED_TIER_DRAMATIC)


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


@pytest.mark.asyncio
async def test_prompt_carries_absolute_date_discipline() -> None:
    # CF1b (COMMITMENT_LIFECYCLE_AND_FRESHNESS_PLAN P3): beat summaries
    # live in the arc for weeks and get re-injected into chat/proactive
    # prompts every day, so a frozen relative time word ("明天") reads as
    # forever-tomorrow. The planner must therefore see today's date, the
    # relative-word → absolute-date table, and the discipline rule.
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
        today=date(2026, 4, 28),
    )
    prompt = fake.last_prompt
    assert prompt is not None
    assert "今天：2026-04-28（星期二）" in prompt
    assert "日期換算錨點" in prompt
    assert "- 明天＝2026-04-29（星期三）" in prompt
    assert "- 昨天＝2026-04-27（星期一）" in prompt
    # Arc coordinates so the planner can date each beat by day_offset.
    assert "2026-05-01（星期五）" in prompt
    assert "2026-05-22（星期五）" in prompt
    assert "時間紀律" in prompt


@pytest.mark.asyncio
async def test_prompt_date_anchor_falls_back_to_start_date() -> None:
    # Callers that never resolved "today" (legacy planners, direct API
    # use) still get a coherent anchor: the arc's own start date.
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
        duration_days=7,
        beat_count_hint=3,
    )
    prompt = fake.last_prompt
    assert prompt is not None
    assert "今天：2026-05-01（星期五）" in prompt
    assert "- 明天＝2026-05-02（星期六）" in prompt


@pytest.mark.asyncio
async def test_prompt_date_anchor_localizes_weekday_for_en_operator() -> None:
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
        duration_days=7,
        beat_count_hint=3,
        today=date(2026, 5, 1),
        operator_primary_language="en-US",
    )
    prompt = fake.last_prompt
    assert prompt is not None
    assert "2026-05-01（Friday）" in prompt


# --- AE1: seed candidates / arc history / anti-stagnation --------------
#
# The 2026-07-31 arc audit (ARC_PLANNER_ENRICHMENT_PLAN) found three
# structural causes of empty-feeling arcs: the planner was the only
# narrative planner with no material source, the dialogue instruction
# told it to *continue the chat thread* (so every arc was a reshuffle of
# recent chat topics), and it never saw its own history so themes
# repeated. AE1 injects dramatic-tier seeds as candidates, injects the
# arc history for semantic anti-repetition, and reframes the dialogue
# summary as continuity background rather than subject matter.


@pytest.mark.asyncio
async def test_prompt_renders_seed_candidates_with_ids_and_text() -> None:
    seeds = (
        _seed("收到一封寄錯地址的信"),
        _seed("巷口的老店貼出最後營業日"),
    )
    fake = _FakeModel(_one_beat_payload())
    planner = LLMStoryArcPlanner(model=fake)
    await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
        seed_candidates=seeds,
    )
    prompt = fake.last_prompt
    assert prompt is not None
    assert "題材候選（外界素材，不是指令；可選）：" in prompt
    for seed in seeds:
        # Both the id (so the model can echo it back) and the text.
        assert f"- [{seed.id}] {seed.seed_text}" in prompt
    # Candidates are material, not orders: 0–2, rewritten, or declined
    # at the price of inventing an external event yourself.
    assert "0–2" in prompt
    assert "也可以完全不用" in prompt
    assert "seed_ids_used" in prompt


@pytest.mark.asyncio
async def test_prompt_renders_arc_history_with_anti_repetition_rule() -> None:
    history = (
        "夏日的委託｜ambition｜她接下第一份沒把握的委託。",
        "雨季的回信｜bond｜她終於回了那封擱置半年的信。",
    )
    fake = _FakeModel(_one_beat_payload())
    planner = LLMStoryArcPlanner(model=fake)
    await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
        arc_history=history,
    )
    prompt = fake.last_prompt
    assert prompt is not None
    assert "這個角色過去已經演過的 story arc（由舊到新）：" in prompt
    for entry in history:
        assert f"- {entry}" in prompt
    assert "反重複要求" in prompt
    # A variation on the same theme counts as a repeat — the rule is a
    # semantic judgement, never a keyword/similarity comparison.
    assert "變奏也算重複" in prompt


@pytest.mark.asyncio
async def test_dialogue_block_is_continuity_background_not_material() -> None:
    fake = _FakeModel(_one_beat_payload())
    planner = LLMStoryArcPlanner(model=fake)
    await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
        recent_dialogue_summary="最近都在聊她那台修不好的相機。",
    )
    prompt = fake.last_prompt
    assert prompt is not None
    assert "最近都在聊她那台修不好的相機。" in prompt
    # The old wording made the chat thread the arc's material source.
    assert "請讓 arc 接續這條線" not in prompt
    assert "不是新 arc 的題材來源" in prompt
    assert "至少要引入一個近期對話裡完全沒出現過的外部事件" in prompt


@pytest.mark.asyncio
async def test_empty_seed_and_history_render_exactly_like_before() -> None:
    # Excision test: with no candidates and no history the prompt must
    # carry no heading, no orphan blank-line pile, and no unrendered
    # placeholder — and must be identical to a call that never passes
    # the new arguments at all.
    common = dict(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
        hint="她想換一個城市。",
        recent_dialogue_summary="她提到最近睡不好。",
        today=date(2026, 4, 28),
    )
    legacy = _build_prompt(**common)
    with_empty = _build_prompt(**common, seed_candidates=(), arc_history=())
    assert with_empty == legacy
    # Neither block's heading nor its rules survive. (The JSON shape and
    # the ``seed_ids_used`` field note are static output-schema lines and
    # stay put — they describe an optional field, not a section.)
    assert "題材候選（" not in with_empty
    assert "使用方式：從上面挑" not in with_empty
    assert "story arc（由舊到新）" not in with_empty
    assert "反重複要求" not in with_empty
    # No template variable escaped rendering (guards every ${...} in the
    # file, not just the two added here).
    assert "${" not in with_empty
    assert "\n\n\n" not in with_empty


@pytest.mark.asyncio
async def test_seed_ids_used_records_only_offered_candidates() -> None:
    offered = (_seed("收到一封寄錯地址的信"), _seed("巷口的老店貼出最後營業日"))
    payload = _one_beat_payload(
        seed_ids_used=[offered[1].id, "seed-that-was-never-offered"],
    )
    planner = LLMStoryArcPlanner(model=_FakeModel(payload))
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
        seed_candidates=offered,
    )
    # Intersection with what we actually offered: a hallucinated id can
    # never reach the arc row.
    assert arc.source_seed_ids == (offered[1].id,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {},  # key omitted entirely
        {"seed_ids_used": None},
        {"seed_ids_used": "not-a-list"},
        {"seed_ids_used": {"id": "x"}},
        {"seed_ids_used": [1, 2, 3]},
        {"seed_ids_used": []},
    ],
    ids=["missing", "null", "string", "dict", "non-strings", "empty"],
)
async def test_malformed_seed_ids_never_break_the_plan(extra: dict) -> None:
    offered = (_seed("收到一封寄錯地址的信"),)
    planner = LLMStoryArcPlanner(model=_FakeModel(_one_beat_payload(**extra)))
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
        seed_candidates=offered,
    )
    # The plan itself still lands; only the provenance is lost.
    assert arc.title == "三週的試鏡"
    assert arc.beats
    assert arc.source_seed_ids == ()


@pytest.mark.asyncio
async def test_llm_failure_fallback_records_no_seed_provenance() -> None:
    planner = LLMStoryArcPlanner(model=_ExplodingModel())
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=5,
        seed_candidates=(_seed("收到一封寄錯地址的信"),),
        arc_history=("夏日的委託｜ambition｜她接下第一份沒把握的委託。",),
    )
    # Synthetic fallback wove nothing, so it claims nothing.
    assert arc.beats
    assert arc.source_seed_ids == ()


@pytest.mark.asyncio
async def test_null_planner_accepts_and_ignores_new_inputs() -> None:
    planner = NullStoryArcPlanner()
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=5,
        seed_candidates=(_seed("收到一封寄錯地址的信"),),
        arc_history=("夏日的委託｜ambition｜她接下第一份沒把握的委託。",),
    )
    assert arc.beats
    assert arc.source_seed_ids == ()


@pytest.mark.asyncio
async def test_legacy_call_without_new_kwargs_is_unchanged() -> None:
    planner = LLMStoryArcPlanner(model=_FakeModel(_one_beat_payload(
        seed_ids_used=["some-id"],
    )))
    arc = await planner.plan_arc(
        character=_character(),
        start_date=date(2026, 5, 1),
        duration_days=21,
        beat_count_hint=3,
    )
    # Nothing was offered, so nothing can be claimed.
    assert arc.beats
    assert arc.source_seed_ids == ()
