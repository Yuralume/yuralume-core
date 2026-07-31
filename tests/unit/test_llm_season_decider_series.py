from __future__ import annotations

from datetime import date

from kokoro_link.contracts.story_arc import StoryArcSeasonContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import StoryArc
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.story.llm_season_decider import _build_prompt


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="想成為舞台上的人。",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _completed_arc() -> StoryArc:
    return StoryArc.create(
        character_id="character-a",
        title="第一本",
        premise="她完成第一次試鏡。",
        theme="growth",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 7),
        source_template_id="book-one",
    )


def test_series_context_prompt_names_next_template_and_forbids_replanning() -> None:
    prompt = _build_prompt(
        StoryArcSeasonContext(
            character=_character(),
            today=date(2026, 6, 9),
            completed_arc=_completed_arc(),
            days_since_completed=2,
            continuation_summary="上一段已收束。",
            recent_dialogue_summary="最近她提到想繼續。",
            series_id="series-a",
            series_title="連載篇",
            next_template_id="book-two",
            next_template_title="第二本",
        ),
    )

    assert "series-bound: true" in prompt
    assert "series_id: series-a" in prompt
    assert "series_title: 連載篇" in prompt
    assert "next_template_id: book-two" in prompt
    assert "next_template_title: 第二本" in prompt
    assert "你只判斷現在是否適合接上下一本" in prompt
    assert "不要改寫、替換或另創下一季" in prompt


def test_non_series_prompt_keeps_llm_planner_contract() -> None:
    prompt = _build_prompt(
        StoryArcSeasonContext(
            character=_character(),
            today=date(2026, 6, 9),
            completed_arc=_completed_arc(),
            days_since_completed=2,
            continuation_summary="上一段已收束。",
            recent_dialogue_summary="最近她提到想繼續。",
        ),
    )

    assert "非 series-bound" in prompt
    assert "LLM planner 規劃內容" in prompt


# ---- AE0: arc history + anti-echo instructions ------------------------


def _context(**overrides: object) -> StoryArcSeasonContext:
    base: dict[str, object] = dict(
        character=_character(),
        today=date(2026, 6, 9),
        completed_arc=_completed_arc(),
        days_since_completed=2,
        continuation_summary="上一段已收束。",
        recent_dialogue_summary="最近她提到想繼續。",
    )
    base.update(overrides)
    return StoryArcSeasonContext(**base)  # type: ignore[arg-type]


def test_arc_history_block_lists_every_entry_in_order() -> None:
    prompt = _build_prompt(
        _context(
            arc_history=(
                "第一季｜growth｜她第一次站上舞台。",
                "第二季｜conflict｜她和搭檔拆夥。",
            ),
        ),
    )

    assert "歷史 story arc（由舊到新）：" in prompt
    assert "- 第一季｜growth｜她第一次站上舞台。" in prompt
    assert "- 第二季｜conflict｜她和搭檔拆夥。" in prompt
    assert prompt.index("第一季") < prompt.index("第二季")


def test_empty_arc_history_omits_the_block() -> None:
    prompt = _build_prompt(_context())

    # The instruction line still mentions the list; only the data block
    # (its heading and bullets) disappears.
    assert "歷史 story arc（由舊到新）：" not in prompt
    # The placeholder collapses back to the blank separator line the
    # template always had — no orphan heading, no double blank line.
    assert "source_template_id: book-one\n\nSeries / 下一本資訊：" in prompt


def test_prompt_forbids_echoing_chat_and_replaying_history() -> None:
    prompt = _build_prompt(_context(arc_history=("第一季｜growth｜舊題材",)))

    assert "hint 不得只複述近期聊天話題" in prompt
    assert "不得重跑「歷史 story arc」清單中任何一條的核心題材" in prompt
    # The continuity instruction survives, re-scoped to consequences.
    assert "承接的是「後果與狀態」，不是重跑同一個題材" in prompt
