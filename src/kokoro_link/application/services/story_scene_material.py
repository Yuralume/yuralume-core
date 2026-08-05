"""Material layers for 起幕 — the §3.1 waterfall, one provider per layer.

:class:`StorySceneService` walks an ordered list of providers and opens
on the first one that answers, so a layer is an appended object rather
than a branch: the opening path, the message write and the session row
never learn that new layers exist.

Both arc-shaped layers live here — layer 1 (:class:`PendingBeatScene\
MaterialProvider`, the active arc's next beat) and layer 2
(:class:`ForcedSeasonSceneMaterialProvider`, force-start the next
season). Layer 3, the ad-hoc side story, draws on relationship,
memory, dialogue and seeds instead of arcs and lives in
``story_scene_side_story.py``.
"""

from __future__ import annotations

import logging
from datetime import date as date_type

from kokoro_link.application.services.beat_retry_policy import (
    AUTONOMOUS_SCENE_RETRY_POLICY,
    BeatRetryPolicy,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.contracts.story_scene import (
    StorySceneMaterial,
    StorySceneMaterialProviderPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    BEAT_PENDING,
    StoryArc,
    StoryArcBeat,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_BEAT,
    SCENE_LAYER_NEW_SEASON,
)


_LOGGER = logging.getLogger(__name__)


class PendingBeatSceneMaterialProvider(StorySceneMaterialProviderPort):
    """Layer 1 — the active arc's next playable pending beat.

    Two rules separate this from the autonomous scanner that reads the
    same beats:

    * **``scheduled_date`` is ignored.** The button means "我現在就要劇
      情"; a beat scheduled for next Tuesday is still the next thing that
      happens in this story, and making the player wait for a date is the
      exact friction 起幕 exists to remove.
    * **Backoff does not apply.** ``BeatRetryPolicy.allows`` throttles
      *autonomous retries* of a writer that keeps failing — it is a cost
      guard on unattended work. A player pulling the story is not a
      retry, so only ``is_exhausted`` (budget permanently spent) skips a
      beat here, and the walk-down continues to the next candidate.

    Exhausted beats are skipped, never written to: retiring them to
    ``skipped`` belongs to the background sweep that owns that write
    (SC0's ``retire_exhausted_beats``), and a read path that mutated arc
    state as a side effect of a player tap would be a second, racing
    writer for the same rows.
    """

    layer = SCENE_LAYER_BEAT

    def __init__(
        self,
        *,
        story_arc_service: StoryArcService,
        retry_policy: BeatRetryPolicy = AUTONOMOUS_SCENE_RETRY_POLICY,
    ) -> None:
        self._arcs = story_arc_service
        self._retry_policy = retry_policy

    async def resolve(
        self, character: Character, *, today: date_type,
    ) -> StorySceneMaterial | None:
        arc = await self._arcs.get_active(character.id)
        if arc is None:
            return None
        beat = _next_playable_beat(arc.beats, self._retry_policy)
        if beat is None:
            _LOGGER.info(
                "story scene layer=beat found no playable beat "
                "character=%s arc=%s",
                character.id,
                arc.id,
            )
            return None
        return _material_from_beat(arc, beat, layer=self.layer)


class ForcedSeasonSceneMaterialProvider(StorySceneMaterialProviderPort):
    """Layer 2 — the character is dormant, so open the next season now.

    Layer 1 has already declined, which means either there is no active
    arc or the one there is has nothing playable left. Pressing 起幕 is
    the player saying the 留白期 is over, and the season decider's entire
    job is judging *when* that should happen — so this layer asks
    ``StoryArcService.force_open_season`` for the same season the
    scheduled path would eventually have planned, minus that one gate
    (STORY_SCENE_PLAN §3.1).

    Two consequences are the point rather than side effects:

    * **The rest of the season comes with it.** The planner writes a full
      arc, so one tap also plants the next few weeks of scheduled beats —
      the player is not buying a single scene, they are starting a story.
    * **Series-bound characters advance their series.** ``force_open_season``
      routes through the same series bookkeeping ``ensure_active_arc``
      uses, so the next authored book is materialised and
      ``CharacterSeriesProgress`` moves with it. ``start_new_arc`` would
      have free-planned an LLM season straight over the series.

    Answers ``None`` for a concluded series, a failed planner, or a season
    that is still live — all three fall through to the side story.
    """

    layer = SCENE_LAYER_NEW_SEASON

    def __init__(
        self,
        *,
        story_arc_service: StoryArcService,
        retry_policy: BeatRetryPolicy = AUTONOMOUS_SCENE_RETRY_POLICY,
    ) -> None:
        self._arcs = story_arc_service
        self._retry_policy = retry_policy

    async def resolve(
        self, character: Character, *, today: date_type,
    ) -> StorySceneMaterial | None:
        arc = await self._arcs.force_open_season(character, today=today)
        if arc is None:
            _LOGGER.info(
                "story scene layer=new_season could not open a season "
                "character=%s",
                character.id,
            )
            return None
        beat = _next_playable_beat(arc.beats, self._retry_policy)
        if beat is None:
            # A season with no playable opening beat is a planner that
            # produced an empty (or already-terminal) arc. The arc still
            # stands — future beats can be scheduled onto it — but tonight's
            # scene has to come from the layer below.
            _LOGGER.warning(
                "story scene layer=new_season opened an arc with no playable "
                "beat character=%s arc=%s",
                character.id,
                arc.id,
            )
            return None
        return _material_from_beat(arc, beat, layer=self.layer)


def _next_playable_beat(
    beats: tuple[StoryArcBeat, ...] | list[StoryArcBeat],
    retry_policy: BeatRetryPolicy,
) -> StoryArcBeat | None:
    candidates = sorted(
        (b for b in beats if b.status == BEAT_PENDING),
        key=lambda b: (b.scheduled_date, b.sequence),
    )
    for beat in candidates:
        if not retry_policy.is_exhausted(beat):
            return beat
    return None


def _material_from_beat(
    arc: StoryArc, beat: StoryArcBeat, *, layer: str,
) -> StorySceneMaterial:
    """The one shape both arc-backed layers produce.

    Layers 1 and 2 differ only in *how they got the arc*; what the opener
    is told about it is identical, and the layer label is what the session
    row and the prompt use to tell the two apart.
    """
    return StorySceneMaterial(
        layer=layer,
        title=beat.title,
        summary=beat.summary,
        arc_id=arc.id,
        beat_id=beat.id,
        arc_title=arc.title,
        arc_premise=arc.premise,
        arc_tone=arc.tone,
        tension=beat.tension,
        scene_type=beat.scene_type,
        location=beat.location,
        dramatic_question=beat.dramatic_question,
        scene_characters=tuple(beat.scene_characters),
        operator_position=beat.operator_position,
        operator_note=beat.operator_note,
    )
