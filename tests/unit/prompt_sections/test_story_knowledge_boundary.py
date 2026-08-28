"""KB3 / KB4 pins for the three story sections that hand over beat material.

The incident these prevent (2026-08-25, hosted): a beat the player was
never in got staged, memorialised and then re-injected as today's scene,
and the character opened with 「你是不是又去了山區」 — a place the player
had never heard of, spoken as shared history. Two separate omissions made
that sentence possible, and both are asserted here per surface:

* **KB3** — nothing in any of these three blocks said the material is a
  director's note the player has not read. The discipline existed only in
  the proactive decider's rule 7, whose wording sits on a Cloud
  tuned-overlay comparison chain and must not move; these blocks get
  freshly written text from ``player_knowledge_lines`` instead.
* **KB4** — ``delay_beat`` moves ``scheduled_date`` and leaves the prose
  alone, so the summary can name a day the beat is no longer on. The
  stamp is unconditional, so it is pinned on an *undelayed* beat too.

Rendered through the section entries rather than the private helpers, so
a section that stops being reachable from the registry fails here.
"""

from __future__ import annotations

from datetime import date

import pytest

from kokoro_link.domain.entities.story_arc import (
    BEAT_PENDING,
    BEAT_REALIZED,
    SCENE_CONFLICT,
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.infrastructure.prompt.player_knowledge_lines import (
    BEAT_LIST_DATE_DISCIPLINE_LINE,
    PLAYER_KNOWLEDGE_HEADER,
    render_arc_forward_feed_knowledge_line,
    render_arc_history_knowledge_line,
    render_arc_history_solo_heading,
    render_player_knowledge_lines,
)
from kokoro_link.infrastructure.prompt.sections.context import StoryContext
from kokoro_link.infrastructure.prompt.sections.story import SECTIONS

TODAY = date(2026, 8, 25)
SCHEDULED = date(2026, 8, 23)
"""A beat written for 8/23 and pushed to 8/25 — the incident's shape."""

_KNOWLEDGE_MARK = "當成他完全不知道"
_ASSUMED_KNOWLEDGE_BAN = "你也知道那個"


class _TimeOnly:
    def __init__(self, today: date | None) -> None:
        self.today_local = today


class _StoryOnlyContext:
    """Enough context for the story sections, deliberately nothing more."""

    def __init__(self, story: StoryContext, today: date | None) -> None:
        self.story = story
        self.time = _TimeOnly(today)


def _beat(
    *,
    scheduled_date: date,
    status: str = BEAT_PENDING,
    sequence: int = 0,
    operator_position: str | None = "central",
    title: str = "銀環裂開以前",
) -> StoryArcBeat:
    return StoryArcBeat(
        id=f"b-{sequence}",
        arc_id="arc-1",
        sequence=sequence,
        scheduled_date=scheduled_date,
        title=title,
        summary="8/23 那天她一個人走進林道，回來時手上全是擦傷。",
        tension=TENSION_RISING,
        status=status,
        scene_characters=("巡山員",),
        location="林道入口",
        dramatic_question="她會承認自己一個人去了嗎？",
        scene_type=SCENE_CONFLICT,
        required=True,
        operator_position=operator_position,
    )


def _arc(beats: tuple[StoryArcBeat, ...]) -> StoryArc:
    arc = StoryArc.create(
        character_id="c1",
        title="銀環",
        premise="她答應過不再一個人上山。",
        theme="promise",
        start_date=date(2026, 8, 20),
        end_date=date(2026, 9, 10),
    )
    return arc.with_beats(list(beats))


def _render(name: str, story: StoryContext, today: date | None = TODAY) -> str:
    (entry,) = [s for s in SECTIONS if s.name == name]
    return "\n".join(entry.render(_StoryOnlyContext(story, today)))


def _story(
    *,
    arc: StoryArc | None,
    upcoming: tuple[StoryArcBeat, ...] = (),
) -> StoryContext:
    return StoryContext(
        story_events=(),
        story_arc=arc,
        upcoming_arc_beats=upcoming,
        story_scene=None,
    )


# --- KB3: the boundary reaches all three surfaces --------------------


def test_today_scene_directive_carries_the_full_boundary_block() -> None:
    arc = _arc((_beat(scheduled_date=SCHEDULED),))

    text = _render("today_scene", _story(arc=arc))

    for line in render_player_knowledge_lines():
        assert line in text
    assert _ASSUMED_KNOWLEDGE_BAN in text


def test_arc_forward_feed_carries_the_short_boundary_line() -> None:
    beat = _beat(scheduled_date=date(2026, 8, 28), sequence=1)
    arc = _arc((beat,))

    text = _render("story_arc", _story(arc=arc, upcoming=(beat,)))

    assert render_arc_forward_feed_knowledge_line() in text


def test_arc_forward_feed_keeps_the_boundary_even_without_upcoming_beats() -> None:
    """Premise and title are already material the player has not read —
    the rider must not be gated on the beat list being non-empty."""
    arc = _arc(())

    text = _render("story_arc", _story(arc=arc))

    assert render_arc_forward_feed_knowledge_line() in text
    assert BEAT_LIST_DATE_DISCIPLINE_LINE not in text


def test_arc_history_corrects_its_heading_for_an_unjudged_beat() -> None:
    """The heading claims 「你們已經一起經歷過」 for every beat under it. KB7
    files the ``absent`` ones elsewhere, but a legacy beat with no
    ``operator_position`` cannot be filed either way — so it stays under
    the heading and the rider has to sit in the same block as the claim."""
    arc = _arc((
        _beat(
            scheduled_date=SCHEDULED,
            status=BEAT_REALIZED,
            operator_position=None,
        ),
    ))

    text = _render("arc_history", _story(arc=arc))

    assert "你們已經一起經歷過" in text
    assert render_arc_history_knowledge_line() in text
    assert "有些他不在場" in text


def test_the_two_arc_riders_are_not_the_same_paragraph_twice() -> None:
    """``story_arc`` and ``arc_history`` render back to back, so a single
    shared sentence would print identical prose in consecutive blocks."""
    assert (
        render_arc_forward_feed_knowledge_line()
        != render_arc_history_knowledge_line()
    )


@pytest.mark.parametrize("name", ["today_scene", "story_arc", "arc_history"])
def test_no_boundary_line_when_the_section_has_nothing_to_say(name: str) -> None:
    """An empty section stays empty — a lone rider with no material under
    it would be noise in every arc-less character's prompt."""
    assert _render(name, _story(arc=None)) == ""


# --- KB7: the history block is filed by who was actually there --------


def test_a_beat_the_player_missed_is_filed_as_hers_alone() -> None:
    """The incident's shape: a solo chapter listed under 「你們已經一起經歷
    過」 is how a mountain rescue he never joined became something the
    character interrogated him about."""
    arc = _arc((
        _beat(
            scheduled_date=SCHEDULED,
            status=BEAT_REALIZED,
            operator_position="absent",
        ),
    ))

    text = _render("arc_history", _story(arc=arc))

    assert render_arc_history_solo_heading() in text
    assert "你們已經一起經歷過" not in text
    assert "銀環裂開以前" in text


def test_shared_and_solo_beats_are_listed_under_their_own_headings() -> None:
    arc = _arc((
        _beat(
            scheduled_date=SCHEDULED,
            status=BEAT_REALIZED,
            sequence=0,
            operator_position="present",
            title="一起等雨停",
        ),
        _beat(
            scheduled_date=SCHEDULED,
            status=BEAT_REALIZED,
            sequence=1,
            operator_position="absent",
            title="一個人的林道",
        ),
    ))

    lines = _render("arc_history", _story(arc=arc)).splitlines()

    def _at(needle: str) -> int:
        return next(i for i, line in enumerate(lines) if needle in line)

    # Each title sits under its own heading, not merely somewhere in the
    # block — the split is the whole point.
    assert _at("你們已經一起經歷過") < _at("一起等雨停") < _at("玩家當時不在場")
    assert _at("玩家當時不在場") < _at("一個人的林道")


def test_an_all_solo_history_needs_no_shared_heading_rider() -> None:
    """The rider exists to walk the shared heading back; with no shared
    heading and no unjudged beat there is nothing to correct, and printing
    it anyway would be the renderer arguing with itself."""
    arc = _arc((
        _beat(
            scheduled_date=SCHEDULED,
            status=BEAT_REALIZED,
            operator_position="absent",
        ),
    ))

    text = _render("arc_history", _story(arc=arc))

    assert render_arc_history_knowledge_line() not in text


def test_a_history_the_player_provably_lived_is_not_second_guessed() -> None:
    """Every beat ``central``/``present`` — telling her these 「不一定是共同
    經歷」 would have her re-introduce his own memories to him, the error
    direction the plan calls visible-to-the-player (D7)."""
    arc = _arc((
        _beat(
            scheduled_date=SCHEDULED,
            status=BEAT_REALIZED,
            operator_position="central",
        ),
    ))

    text = _render("arc_history", _story(arc=arc))

    assert "你們已經一起經歷過" in text
    assert render_arc_history_knowledge_line() not in text
    assert render_arc_history_solo_heading() not in text


# --- KB4: the scheduled-date stamp ------------------------------------


def test_today_scene_stamps_the_scheduled_date_over_the_prose() -> None:
    arc = _arc((_beat(scheduled_date=SCHEDULED),))

    text = _render("today_scene", _story(arc=arc))

    # The authoritative day, with the same relative wording the rest of
    # the block uses.
    assert "- 本場戲排定日：2026-08-23（2 天前）" in text
    assert "不要照唸" in text


def test_the_stamp_is_unconditional_for_an_undelayed_beat() -> None:
    """R-KB-2: a beat whose prose already agrees with its schedule is
    stamped too — there is no way to detect the disagreement without
    sniffing the prose, which the LLM-first rail forbids."""
    arc = _arc((_beat(scheduled_date=TODAY),))

    text = _render("today_scene", _story(arc=arc))

    assert "- 本場戲排定日：2026-08-25（今天）" in text


def test_arc_forward_feed_states_the_date_discipline_once() -> None:
    """Per-beat relative labels are already rendered by the loop, so the
    list gets one rider rather than one stamp per beat."""
    first = _beat(scheduled_date=date(2026, 8, 27), sequence=1)
    second = _beat(scheduled_date=date(2026, 8, 29), sequence=2)
    arc = _arc((first, second))

    text = _render("story_arc", _story(arc=arc, upcoming=(first, second)))

    assert text.count(BEAT_LIST_DATE_DISCIPLINE_LINE) == 1


# --- the shared module is the single source ---------------------------


def test_the_boundary_text_is_not_a_copy_of_the_decider_rule() -> None:
    """Rule 7 in ``proactive/decider_instructions.txt`` is pinned by a
    Cloud tuned-overlay comparison chain and must stay where it is; this
    module is new text serving the same discipline, not a relocation."""
    rendered = "\n".join(render_player_knowledge_lines())
    assert PLAYER_KNOWLEDGE_HEADER in rendered
    assert _KNOWLEDGE_MARK in rendered
    # Rule 7's own sentence, verbatim — must not have been moved here.
    verbatim = "就上次跟你說的那個 X 啊"
    assert verbatim not in rendered
    assert verbatim not in render_arc_forward_feed_knowledge_line()
    assert verbatim not in render_arc_history_knowledge_line()
