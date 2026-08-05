"""OP2-B — the same beat, two paths, opposite answers.

``ARC_PLAYER_POSITION_PLAN`` §5.1 OP2-B splits the two consumers that
read the same pending beats:

* the **unattended scan** (``BeatDueChecker`` → ``StoryBeatSceneService``)
  plays beats with nobody in the room, so it passes over a beat whose
  ``operator_position`` is ``central`` — that scene is *about* the
  player and has no content without them;
* the **player pull** (起幕 layer 1) is the player walking in, so the
  very same beat is exactly what it should raise the curtain on.

Red line 4 is the part that is easy to get subtly wrong: passing over is
a **pass, not a failure**. It must cost the beat no attempt, no failure
count, no pushed-out backoff and no retirement — the beat is not broken,
it is waiting. Every assertion about "nothing happened" below is that red
line, not defensive padding.

The three other positions (``absent`` / ``present`` / ``None``) are
characterized here as *unchanged*: this ticket is control-flow routing
only, and the framing those beats get is OP2-A/-C/-D's job.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.beat_due_checker import BeatDueChecker
from kokoro_link.application.services.beat_retry_policy import (
    AUTONOMOUS_SCENE_RETRY_POLICY,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_beat_scene_service import (
    StoryBeatSceneService,
)
from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.story_scene_material import (
    PendingBeatSceneMaterialProvider,
)
from kokoro_link.application.services.story_scene_service import StorySceneService
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.contracts.story_arc import (
    StoryArcPlannerPort,
    StoryBeatSceneDraft,
    StoryBeatSceneWriterPort,
)
from kokoro_link.contracts.story_scene import (
    StorySceneOpeningDraft,
    StorySceneOpenerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    BEAT_PENDING,
    BEAT_REALIZED,
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    PLAY_RESULT_FAILED,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_scene_session import SCENE_LAYER_BEAT
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)


UTC = timezone.utc
TODAY = date(2026, 6, 1)
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


# ── doubles ──────────────────────────────────────────────────────────


class _NeverPlanner(StoryArcPlannerPort):
    """Arcs here are hand-built; planning one would hide the setup."""

    async def plan_arc(self, **kwargs) -> StoryArc:  # noqa: ANN003
        raise AssertionError("operator-position routing tests never plan")


class _UnusedExpander(StoryEventExpanderPort):
    async def expand(self, **kwargs):  # noqa: ANN003
        raise AssertionError("the scene path must not use the diary expander")


class _RecordingWriter(StoryBeatSceneWriterPort):
    """The unattended scene writer. Its call log *is* the assertion."""

    def __init__(self) -> None:
        self.contexts: list = []

    async def write_scene(self, context):  # noqa: ANN001
        self.contexts.append(context)
        return StoryBeatSceneDraft(
            narrative="她把譜架收起來，沒有等任何人。",
            emotional_tone="quiet",
            cast_strategy="solo",
            participation_note="user not required",
        )

    @property
    def played_beat_ids(self) -> list[str]:
        return [context.beat.id for context in self.contexts]


class _StubOpener(StorySceneOpenerPort):
    def __init__(self) -> None:
        self.contexts: list = []

    async def write_opening(self, context):  # noqa: ANN001
        self.contexts.append(context)
        return StorySceneOpeningDraft(
            narration="門推開的時候，她剛好抬頭。",
            character_line="……你來了。",
            title="頂樓",
            location="頂樓天台",
            mood="欲言又止",
        )


# ── fixture ──────────────────────────────────────────────────────────


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="a violinist",
        personality=[],
        interests=[],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        user_id="user-a",
        proactive_enabled=True,
    )


class _BothPaths:
    """One repository, one arc, both consumers of its beats.

    The point of sharing the repository is that the two paths cannot be
    argued to be looking at different data: whatever the unattended scan
    refuses is byte-for-byte the beat the player pull is handed.
    """

    def __init__(self, character: Character) -> None:
        self.character = character
        self.arcs_repo = InMemoryStoryArcRepository()
        self.events_repo = InMemoryStoryEventRepository()
        self.memories = InMemoryMemoryRepository()

        self.arc_service = StoryArcService(
            repository=self.arcs_repo,
            planner=_NeverPlanner(),
            local_tz=UTC,
        )
        self.event_service = StoryEventService(
            gacha=StoryGachaService(
                seed_repository=InMemoryStorySeedRepository(),
                event_repository=self.events_repo,
            ),
            expander=_UnusedExpander(),
            event_repository=self.events_repo,
            memory_repository=self.memories,
            local_tz=UTC,
            arc_service=self.arc_service,
        )

        # unattended path
        self.writer = _RecordingWriter()
        self.checker = BeatDueChecker(
            story_event_service=self.event_service,
            story_arc_service=self.arc_service,
            story_beat_scene_service=StoryBeatSceneService(
                story_arc_service=self.arc_service,
                story_event_service=self.event_service,
                writer=self.writer,
                local_tz=UTC,
            ),
            local_tz=UTC,
        )

        # player-pull path (起幕 layer 1)
        self.opener = _StubOpener()
        self.curtain = StorySceneService(
            sessions=InMemoryStorySceneSessionRepository(),
            conversations=InMemoryConversationRepository(),
            opener=self.opener,
            material_providers=(
                PendingBeatSceneMaterialProvider(
                    story_arc_service=self.arc_service,
                ),
            ),
            story_arc_service=self.arc_service,
            local_tz=UTC,
        )

    async def stage(self, *positions: str | None) -> StoryArc:
        """One arc, one beat per position, all due today, in order."""
        arc = StoryArc.create(
            character_id=self.character.id,
            title="夏日的獨奏會",
            premise="她要準備一場獨奏會。",
            theme="custom",
            start_date=TODAY,
            end_date=TODAY + timedelta(days=21),
        )
        arc = arc.with_beats([
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=index,
                scheduled_date=TODAY,
                title=f"第 {index} 顆",
                summary=f"第 {index} 顆的內容。",
                tension=TENSION_SETUP,
                operator_position=position,
            )
            for index, position in enumerate(positions)
        ])
        await self.arcs_repo.add(arc)
        return arc

    async def reload(self, beat_id: str) -> StoryArcBeat:
        beat = await self.arc_service.find_beat(beat_id)
        assert beat is not None
        return beat


@pytest.fixture()
def character() -> Character:
    return _character()


async def _both_paths(character: Character) -> _BothPaths:
    return _BothPaths(character)


# ── the core acceptance: one beat, two paths, opposite answers ───────


@pytest.mark.asyncio
async def test_a_central_beat_is_passed_over_unattended_and_played_when_pulled(
    character: Character,
) -> None:
    """The whole ticket in one test.

    Same beat, same repository, same moment — the unattended scan will
    not touch it and the player's 起幕 opens on it.
    """
    fx = await _both_paths(character)
    arc = await fx.stage(OPERATOR_POSITION_CENTRAL)
    central = arc.beats[0]

    scan = await fx.checker.scan(character, now=NOW)

    assert scan.attempted_beat_id is None
    assert scan.realized_event_id is None
    assert scan.should_notify is False
    assert fx.writer.played_beat_ids == []

    opening = await fx.curtain.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_BEAT
    assert opening.session.beat_id == central.id
    assert fx.opener.contexts[-1].material.beat_id == central.id


@pytest.mark.asyncio
async def test_passing_over_a_central_beat_costs_it_nothing(
    character: Character,
) -> None:
    """Red line 4, stated as the four things that must not move.

    A skip that quietly recorded an attempt would be indistinguishable
    from a failure at the next tick: the retry budget would drain, the
    backoff would slide out, and the beat the player is meant to walk
    into would eventually be retired for a failure that never happened.
    """
    fx = await _both_paths(character)
    arc = await fx.stage(OPERATOR_POSITION_CENTRAL)
    central = arc.beats[0]

    # A full budget's worth of ticks, spread past every backoff window.
    for tick in range(AUTONOMOUS_SCENE_RETRY_POLICY.max_attempts + 3):
        await fx.checker.scan(character, now=NOW + timedelta(days=tick))

    after = await fx.reload(central.id)
    assert after.play_attempt_count == 0
    assert after.play_failure_count == 0
    assert after.last_play_failure_at is None
    assert after.last_play_attempt_result is None
    # …and therefore never exhausted, so the retirement sweep that runs
    # before every scan leaves it pending and pullable.
    assert not AUTONOMOUS_SCENE_RETRY_POLICY.is_exhausted(after)
    assert after.status == BEAT_PENDING


@pytest.mark.asyncio
async def test_a_passed_over_beat_does_not_block_the_rest_of_the_arc(
    character: Character,
) -> None:
    """SC0's walk-down carries the pass, rather than a new mechanism."""
    fx = await _both_paths(character)
    arc = await fx.stage(OPERATOR_POSITION_CENTRAL, OPERATOR_POSITION_ABSENT)
    central, standalone = arc.beats

    scan = await fx.checker.scan(character, now=NOW)

    assert scan.attempted_beat_id == standalone.id
    assert fx.writer.played_beat_ids == [standalone.id]
    assert (await fx.reload(standalone.id)).status == BEAT_REALIZED
    assert (await fx.reload(central.id)).status == BEAT_PENDING


@pytest.mark.asyncio
async def test_an_all_central_arc_reports_nothing_due_rather_than_failing(
    character: Character,
) -> None:
    """Nothing to play unattended is not an error and not a notification.

    Until OP3 lands the invitation, the honest answer for an arc that is
    entirely waiting on the player is silence — not a proactive ping
    generated from a beat the character cannot perform alone.
    """
    fx = await _both_paths(character)
    await fx.stage(OPERATOR_POSITION_CENTRAL, OPERATOR_POSITION_CENTRAL)

    scan = await fx.checker.scan(character, now=NOW)

    assert scan.attempted_beat_id is None
    assert scan.should_notify is False
    assert fx.writer.played_beat_ids == []


# ── characterization: the other three positions do not move ──────────


@pytest.mark.parametrize(
    "position",
    [OPERATOR_POSITION_ABSENT, OPERATOR_POSITION_PRESENT, None],
    ids=["absent", "present", "unjudged"],
)
@pytest.mark.asyncio
async def test_every_other_position_is_played_unattended_exactly_as_before(
    character: Character, position: str | None,
) -> None:
    fx = await _both_paths(character)
    arc = await fx.stage(position)
    beat = arc.beats[0]

    scan = await fx.checker.scan(character, now=NOW)

    assert fx.writer.played_beat_ids == [beat.id]
    assert scan.attempted_beat_id == beat.id
    assert scan.realized_event_id is not None
    assert (await fx.reload(beat.id)).status == BEAT_REALIZED


@pytest.mark.asyncio
async def test_the_chat_read_path_still_sees_a_central_beat(
    character: Character,
) -> None:
    """``next_beat_due`` without the flag is unchanged.

    The chat path stages today's beat in the prompt for a player who is
    right there — the one condition under which a ``central`` beat is
    playable. Filtering it here would have hidden the beat from the
    player as well as from the scanner.
    """
    fx = await _both_paths(character)
    arc = await fx.stage(OPERATOR_POSITION_CENTRAL)

    due = await fx.arc_service.next_beat_due(character.id, today=TODAY)

    assert due is not None
    assert due[1].id == arc.beats[0].id


# ── the read path's own contract ─────────────────────────────────────


@pytest.mark.asyncio
async def test_unattended_walks_past_central_even_with_no_retry_policy(
    character: Character,
) -> None:
    """The two filters are independent, and so is the walk-down.

    The walk-down used to be reachable only through a retry policy. A
    caller that wants unattended candidates without one must still get a
    walk, or the flag would silently degrade to "return nothing".
    """
    fx = await _both_paths(character)
    arc = await fx.stage(OPERATOR_POSITION_CENTRAL, OPERATOR_POSITION_PRESENT)

    due = await fx.arc_service.next_beat_due(
        character.id, today=TODAY, unattended=True,
    )

    assert due is not None
    assert due[1].id == arc.beats[1].id


@pytest.mark.asyncio
async def test_unattended_and_the_retry_policy_both_apply_to_the_walk(
    character: Character,
) -> None:
    fx = await _both_paths(character)
    arc = await fx.stage(
        OPERATOR_POSITION_CENTRAL,  # waiting for the player
        OPERATOR_POSITION_ABSENT,  # inside its failure backoff
        OPERATOR_POSITION_ABSENT,  # the one that should be handed back
    )
    await fx.arc_service.mark_beat_play_attempted(
        beat_id=arc.beats[1].id,
        attempted_at=NOW,
        source="scene_simulation",
        result=PLAY_RESULT_FAILED,
        push_intensity="autonomous_scene",
    )

    due = await fx.arc_service.next_beat_due(
        character.id,
        today=TODAY,
        retry_policy=AUTONOMOUS_SCENE_RETRY_POLICY,
        retry_at=NOW + timedelta(minutes=1),
        unattended=True,
    )

    assert due is not None
    assert due[1].id == arc.beats[2].id


@pytest.mark.asyncio
async def test_an_explicit_simulate_still_plays_a_central_beat(
    character: Character,
) -> None:
    """The routing lives in the scanner, not in the scene service.

    ``POST /story-arc-beats/{id}/simulate`` is an operator asking for
    this beat by id. That is a pull, not unattended play, so putting the
    filter inside ``play_beat`` would have broken a deliberate action.
    """
    fx = await _both_paths(character)
    arc = await fx.stage(OPERATOR_POSITION_CENTRAL)
    central = arc.beats[0]

    event = await fx.checker._scene_service.play_beat(  # noqa: SLF001
        character, beat_id=central.id, now=NOW,
    )

    assert event is not None
    assert (await fx.reload(central.id)).status == BEAT_REALIZED
