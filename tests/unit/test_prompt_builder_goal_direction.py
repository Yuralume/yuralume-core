"""Chat 「角色目標」 block: cap + 「N 天前立下」 anchor (CF6b / plan §2 P2d).

The 7/28 芊璃 dump injected all 28 active goals verbatim — near-duplicates
included — and one of them was a zombie (「陪使用者明早一起出門吃刨冰」)
whose 「明早」 had expired days earlier. Two rendering-side defences:

1. a hard cap so the section can never drown the prompt again, ordered by
   priority then recency so the cut is principled rather than arbitrary;
2. a program-stamped 「（N 天前立下）」 anchor on every goal, counted in
   civil days like the proactive twin (`proactive_dispatcher._goal_age_tag`),
   so a frozen relative word inside the goal text can be read against how
   many midnights have passed since it was written.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import (
    _DIRECTION_GOALS_MAX,
    DefaultPromptContextBuilder,
    _render_direction_block,
)

NOW = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)


def _char() -> Character:
    return Character.create(
        name="Aki",
        summary="插畫家",
        personality=["溫柔"],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _goal(
    content: str, *, priority: int = 3, days_ago: float = 1.0,
) -> CharacterGoal:
    return CharacterGoal.create(
        character_id="char-1",
        content=content,
        priority=priority,
        created_at=NOW - timedelta(days=days_ago),
    )


def _goal_lines(lines: list[str]) -> list[str]:
    """Bullet lines rendered under 中期目標 (excludes 長期追求 bullets)."""
    start = lines.index("中期目標：")
    return [line for line in lines[start + 1:] if line.startswith("- [")]


def _twenty_eight_goals() -> list[CharacterGoal]:
    """Reproduce the 芊璃 shape: a long tail of low-priority near-dupes."""
    goals = [
        _goal("陪使用者明早一起出門吃刨冰，順利確認八點半出發與路線",
              priority=2, days_ago=3.0),
    ]
    for index in range(27):
        goals.append(
            _goal(
                f"和使用者維持每天的閒聊節奏 #{index}",
                # Priority walks 1→5 so the cap has something to rank on;
                # older goals get the higher index (i.e. staler).
                priority=1 + (index % 5),
                days_ago=1.0 + index,
            ),
        )
    return goals


def test_direction_block_caps_medium_term_goals() -> None:
    lines = _render_direction_block(
        aspirations=["成為能靠畫養活自己的人"],
        goals=_twenty_eight_goals(),
        current_intent=None,
        now=NOW,
    )

    rendered = _goal_lines(lines)
    assert len(rendered) == _DIRECTION_GOALS_MAX == 10
    body = "\n".join(lines)
    # The overflow is disclosed rather than silently dropped, so the model
    # knows the list is a selection.
    assert "另有 18 條" in body


def test_direction_block_orders_by_priority_then_recency() -> None:
    goals = [
        _goal("低優先但最新", priority=1, days_ago=0.5),
        _goal("高優先較舊", priority=5, days_ago=20.0),
        _goal("高優先較新", priority=5, days_ago=2.0),
        _goal("中優先", priority=3, days_ago=1.0),
    ]

    rendered = _goal_lines(
        _render_direction_block(
            aspirations=[], goals=goals, current_intent=None, now=NOW,
        ),
    )

    ordered = [line for line in rendered]
    assert "高優先較新" in ordered[0]
    assert "高優先較舊" in ordered[1]
    assert "中優先" in ordered[2]
    assert "低優先但最新" in ordered[3]


def test_direction_block_keeps_lowest_priority_stale_goal_out_of_cap() -> None:
    goals = [_goal(f"高優先 #{i}", priority=5, days_ago=i + 1) for i in range(10)]
    goals.append(_goal("最舊最低優先的殭屍目標", priority=1, days_ago=90.0))

    body = "\n".join(
        _render_direction_block(
            aspirations=[], goals=goals, current_intent=None, now=NOW,
        ),
    )

    assert "最舊最低優先的殭屍目標" not in body


def test_direction_block_stamps_civil_days_created_anchor() -> None:
    """Civil days, not elapsed duration — same unit as the proactive side.

    Expectation changed from 「（約 3 天前立下）」 with cause: the chat tag
    used ``format_relative_past_label`` (duration buckets) while proactive
    used ``format_civil_days_ago_label``; plan §2 P2d specifies 「N 天前立下」
    and cross-day awareness is the whole point, so the two paths now share
    the calendar-day unit.
    """
    body = "\n".join(
        _render_direction_block(
            aspirations=[],
            goals=[_goal("陪使用者明早一起出門吃刨冰", days_ago=3.0)],
            current_intent=None,
            now=NOW,
        ),
    )

    assert "（3 天前立下）" in body


def test_direction_block_counts_midnights_not_elapsed_hours() -> None:
    """A goal written at 23:50 last night is 「1 天前」, not 「約 8 小時前」.

    The zombie case: 「陪使用者**明早**一起出門」 written just before
    midnight is already spent by breakfast, and only the calendar boundary
    says so.
    """
    last_night = datetime(2026, 7, 28, 23, 50, tzinfo=timezone.utc)
    body = "\n".join(
        _render_direction_block(
            aspirations=[],
            goals=[
                CharacterGoal.create(
                    character_id="char-1",
                    content="陪使用者明早一起出門吃刨冰",
                    priority=3,
                    created_at=last_night,
                ),
            ],
            current_intent=None,
            now=NOW,
            local_tz=timezone.utc,
        ),
    )

    assert "（1 天前立下）" in body
    assert "小時前" not in body


def test_direction_block_without_clock_omits_anchor() -> None:
    """Legacy/replay callers pass no clock — the line renders as before."""
    body = "\n".join(
        _render_direction_block(
            aspirations=[],
            goals=[_goal("和使用者維持每天的閒聊節奏")],
            current_intent=None,
            now=None,
        ),
    )

    assert "立下）" not in body
    assert "和使用者維持每天的閒聊節奏" in body


def test_long_term_aspirations_are_not_capped() -> None:
    aspirations = [f"長期追求 #{i}" for i in range(15)]
    body = "\n".join(
        _render_direction_block(
            aspirations=aspirations, goals=[], current_intent=None, now=NOW,
        ),
    )

    for item in aspirations:
        assert item in body


def test_build_threads_capped_goals_with_anchor_into_prompt() -> None:
    builder = DefaultPromptContextBuilder()
    character = _char()

    prompt = builder.build(
        character=character,
        conversation=Conversation.start(character_id=character.id),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
        active_goals=_twenty_eight_goals(),
        now=NOW,
    )

    goal_lines = [
        line for line in prompt.splitlines()
        if line.startswith("- [") and "優先" in line
    ]
    assert len(goal_lines) == _DIRECTION_GOALS_MAX
    assert "立下）" in prompt
