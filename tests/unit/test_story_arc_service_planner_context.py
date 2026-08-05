"""AE0 / OP1-A — what ``StoryArcService`` feeds the arc planner.

Three inputs reach the LLM planning path:

- ``seed_candidates`` — ``dramatic``-tier story seeds rolled as
  subject-matter candidates, minus whatever the last few arcs already
  consumed, so a new arc has somewhere to come from other than the last
  few chat turns.
- ``arc_history`` — one-line digests of earlier arcs, oldest first, as
  the semantic anti-repetition input.
- ``operator_relationship_lines`` (OP1-A) — who the *player* is to this
  character: how they are addressed, the relationship, the distance.
  Without it the planner could not name the player, and so had no basis
  on which to decide whether a beat is about them.

The invariants under test are mostly about what happens when those
inputs are *not* available: no gacha wired, an empty pool, a raising
repository, no confirmed relationship seed, or a planner still pinned to
the pre-AE0 signature must all produce an arc exactly as before.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.contracts.story_arc import (
    StoryArcPlannerPort,
    StoryArcSeasonContext,
    StoryArcSeasonDecision,
    StoryArcSeasonDeciderPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.story_arc import (
    ARC_ABANDONED,
    ARC_COMPLETED,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_seed import (
    SEED_TIER_DAILY,
    SEED_TIER_DRAMATIC,
    StorySeed,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStorySeedRepository,
    InMemoryStoryEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)


# ---- fakes ------------------------------------------------------------


def _plan_stub_arc(
    *, character_id: str, start_date: date, duration_days: int,
) -> StoryArc:
    arc = StoryArc.create(
        character_id=character_id,
        title="planned",
        premise="planned premise",
        theme="custom",
        start_date=start_date,
        end_date=start_date + timedelta(days=duration_days),
    )
    return arc.with_beats(
        [
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=0,
                scheduled_date=start_date,
                title="beat 0",
                summary="summary 0",
                tension=TENSION_SETUP,
            ),
        ],
    )


class _RecordingPlanner(StoryArcPlannerPort):
    """Declares the AE0 / OP1-A kwargs and records every call's context."""

    def __init__(self) -> None:
        self.seed_candidates: list[tuple[StorySeed, ...]] = []
        self.arc_histories: list[tuple[str, ...]] = []
        self.relationship_lines: list[tuple[str, ...]] = []

    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
        operator_primary_language: str = "zh-TW",
        today: date | None = None,
        seed_candidates: tuple[StorySeed, ...] = (),
        arc_history: tuple[str, ...] = (),
        operator_relationship_lines: tuple[str, ...] = (),
    ) -> StoryArc:
        self.seed_candidates.append(seed_candidates)
        self.arc_histories.append(arc_history)
        self.relationship_lines.append(operator_relationship_lines)
        return _plan_stub_arc(
            character_id=character.id,
            start_date=start_date,
            duration_days=duration_days,
        )


class _LegacyPlanner(StoryArcPlannerPort):
    """Pre-AE0 signature — knows nothing about seeds or history."""

    def __init__(self) -> None:
        self.calls = 0

    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
        operator_primary_language: str = "zh-TW",
    ) -> StoryArc:
        self.calls += 1
        return _plan_stub_arc(
            character_id=character.id,
            start_date=start_date,
            duration_days=duration_days,
        )


class _ExplodingGacha:
    """Stands in for a gacha whose seed repository is down."""

    async def roll(self, **_kwargs: object) -> object:
        raise RuntimeError("seed pool unreachable")


class _RecordingSeasonDecider(StoryArcSeasonDeciderPort):
    def __init__(self) -> None:
        self.contexts: list[StoryArcSeasonContext] = []

    async def decide(
        self, context: StoryArcSeasonContext,
    ) -> StoryArcSeasonDecision:
        self.contexts.append(context)
        return StoryArcSeasonDecision(
            should_start=False, reason="test: stay dormant",
        )


# ---- builders ---------------------------------------------------------


def _character() -> Character:
    return Character.create(
        name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _dramatic_seed(text: str) -> StorySeed:
    return StorySeed.create(
        seed_text=text, tier=SEED_TIER_DRAMATIC, cooldown_days=0,
    )


async def _gacha(*seeds: StorySeed) -> StoryGachaService:
    seed_repository = InMemoryStorySeedRepository()
    for seed in seeds:
        await seed_repository.add(seed)
    return StoryGachaService(
        seed_repository=seed_repository,
        event_repository=InMemoryStoryEventRepository(),
        rng=random.Random(7),
    )


async def _store_past_arc(
    repository: InMemoryStoryArcRepository,
    *,
    character_id: str,
    title: str,
    premise: str,
    theme: str = "growth",
    status: str = ARC_COMPLETED,
    updated_at: datetime,
    source_seed_ids: tuple[str, ...] = (),
) -> StoryArc:
    """Persist a finished arc with an explicit ``updated_at`` ordering key."""
    arc = StoryArc.create(
        character_id=character_id,
        title=title,
        premise=premise,
        theme=theme,
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 20),
        status=status,
        source_seed_ids=source_seed_ids,
    )
    stamped = replace(arc, updated_at=updated_at)
    await repository.add(stamped)
    return stamped


def _at(minute: int) -> datetime:
    return datetime(2026, 4, 30, 12, minute, tzinfo=timezone.utc)


# ---- seed candidates --------------------------------------------------


@pytest.mark.asyncio
async def test_start_new_arc_offers_dramatic_seeds_to_the_planner() -> None:
    planner = _RecordingPlanner()
    service = StoryArcService(
        repository=InMemoryStoryArcRepository(),
        planner=planner,
        gacha_service=await _gacha(
            _dramatic_seed("她收到一封沒有署名的信。"),
            _dramatic_seed("排練廳被臨時收回。"),
        ),
    )

    await service.start_new_arc(_character(), today=date(2026, 5, 1))

    offered = planner.seed_candidates[0]
    assert {seed.seed_text for seed in offered} == {
        "她收到一封沒有署名的信。",
        "排練廳被臨時收回。",
    }
    assert all(seed.tier == SEED_TIER_DRAMATIC for seed in offered)


@pytest.mark.asyncio
async def test_daily_tier_seeds_never_reach_the_arc_planner() -> None:
    planner = _RecordingPlanner()
    daily = StorySeed.create(seed_text="她買了新的耳機。", tier=SEED_TIER_DAILY)
    service = StoryArcService(
        repository=InMemoryStoryArcRepository(),
        planner=planner,
        gacha_service=await _gacha(daily),
    )

    await service.start_new_arc(_character(), today=date(2026, 5, 1))

    assert planner.seed_candidates[0] == ()


@pytest.mark.asyncio
async def test_no_gacha_wired_plans_with_empty_candidates() -> None:
    planner = _RecordingPlanner()
    service = StoryArcService(
        repository=InMemoryStoryArcRepository(), planner=planner,
    )

    arc = await service.start_new_arc(_character(), today=date(2026, 5, 1))

    assert arc.beats
    assert planner.seed_candidates == [()]


@pytest.mark.asyncio
async def test_gacha_failure_never_blocks_arc_creation() -> None:
    planner = _RecordingPlanner()
    service = StoryArcService(
        repository=InMemoryStoryArcRepository(),
        planner=planner,
        gacha_service=_ExplodingGacha(),
    )

    arc = await service.start_new_arc(_character(), today=date(2026, 5, 1))

    assert arc.beats
    assert planner.seed_candidates == [()]


@pytest.mark.asyncio
async def test_seeds_consumed_by_the_last_three_arcs_are_filtered_out() -> None:
    repository = InMemoryStoryArcRepository()
    planner = _RecordingPlanner()
    character = _character()
    fresh = _dramatic_seed("久未謀面的人捎來消息。")
    spent = [_dramatic_seed(f"已經用過的題材 {index}") for index in range(3)]
    stale = _dramatic_seed("四段之前用過的題材。")
    # Newest three arcs consumed ``spent``; the fourth-newest consumed
    # ``stale``, which is far enough back to come round again.
    for index, seed in enumerate(spent):
        await _store_past_arc(
            repository,
            character_id=character.id,
            title=f"past-{index}",
            premise="premise",
            updated_at=_at(30 + index),
            source_seed_ids=(seed.id,),
        )
    await _store_past_arc(
        repository,
        character_id=character.id,
        title="oldest",
        premise="premise",
        updated_at=_at(1),
        source_seed_ids=(stale.id,),
    )
    service = StoryArcService(
        repository=repository,
        planner=planner,
        gacha_service=await _gacha(fresh, stale, *spent),
    )

    await service.start_new_arc(character, today=date(2026, 5, 1))

    offered = {seed.id for seed in planner.seed_candidates[0]}
    assert offered == {fresh.id, stale.id}


# ---- arc history ------------------------------------------------------


@pytest.mark.asyncio
async def test_arc_history_is_formatted_oldest_first_and_capped() -> None:
    repository = InMemoryStoryArcRepository()
    planner = _RecordingPlanner()
    character = _character()
    for index in range(8):
        await _store_past_arc(
            repository,
            character_id=character.id,
            title=f"第{index}季",
            premise=f"premise-{index}",
            theme=f"theme-{index}",
            status=ARC_COMPLETED if index % 2 else ARC_ABANDONED,
            updated_at=_at(index),
        )
    service = StoryArcService(repository=repository, planner=planner)

    await service.start_new_arc(character, today=date(2026, 5, 1))

    # Six newest arcs (index 2..7), oldest first, whatever their status.
    assert planner.arc_histories[0] == tuple(
        f"第{index}季｜theme-{index}｜premise-{index}"
        for index in range(2, 8)
    )


@pytest.mark.asyncio
async def test_arc_history_truncates_a_long_premise() -> None:
    repository = InMemoryStoryArcRepository()
    planner = _RecordingPlanner()
    character = _character()
    await _store_past_arc(
        repository,
        character_id=character.id,
        title="長篇",
        premise="長" * 200,
        theme="growth",
        updated_at=_at(5),
    )
    service = StoryArcService(repository=repository, planner=planner)

    await service.start_new_arc(character, today=date(2026, 5, 1))

    assert planner.arc_histories[0] == ("長篇｜growth｜" + "長" * 80,)


@pytest.mark.asyncio
async def test_arc_history_load_failure_degrades_to_empty() -> None:
    repository = InMemoryStoryArcRepository()
    planner = _RecordingPlanner()

    async def _boom(_character_id: str) -> list[StoryArc]:
        raise RuntimeError("arc table unreachable")

    repository.list_for_character = _boom  # type: ignore[method-assign]
    service = StoryArcService(repository=repository, planner=planner)

    arc = await service.start_new_arc(_character(), today=date(2026, 5, 1))

    assert arc.beats
    assert planner.arc_histories == [()]


# ---- per-caller shaping -----------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_beats_gets_history_but_no_seed_candidates() -> None:
    repository = InMemoryStoryArcRepository()
    planner = _RecordingPlanner()
    character = _character()
    await _store_past_arc(
        repository,
        character_id=character.id,
        title="上一季",
        premise="舊的題材",
        theme="growth",
        updated_at=_at(5),
    )
    service = StoryArcService(
        repository=repository,
        planner=planner,
        gacha_service=await _gacha(_dramatic_seed("新的外部事件。")),
    )
    current = await service.start_new_arc(character, today=date(2026, 5, 1))

    await service.regenerate_beats(current.id, character=character)

    # Second planner call is the replan.
    assert planner.seed_candidates[1] == ()
    assert planner.arc_histories[1] == ("上一季｜growth｜舊的題材",)


@pytest.mark.asyncio
async def test_legacy_planner_signature_still_plans_an_arc() -> None:
    planner = _LegacyPlanner()
    service = StoryArcService(
        repository=InMemoryStoryArcRepository(),
        planner=planner,
        gacha_service=await _gacha(_dramatic_seed("她收到一封沒有署名的信。")),
    )

    arc = await service.start_new_arc(_character(), today=date(2026, 5, 1))

    assert planner.calls == 1
    assert arc.beats


# ---- OP1-A: the player facts the planner never had --------------------
#
# ARC_PLAYER_POSITION_PLAN §1: the arc planner's only input about the
# player was a language tag. It could not name them, did not know how
# close they stand, and therefore had no basis on which to decide whether
# a beat is *about* them. The seed the user confirmed at creation time is
# the material that already exists for exactly this question, so the
# service now renders it into the planner call — on every LLM planning
# path, not just the first arc.


class _Operator:
    def __init__(self, operator_id: str, language: str = "zh-TW") -> None:
        self.id = operator_id
        self.primary_language = language


class _OperatorProfiles:
    def __init__(self, operator: _Operator | None) -> None:
        self._operator = operator

    async def get_for_user(self, user_id: str) -> _Operator | None:  # noqa: ARG002
        return self._operator


class _ExplodingSeedRepository:
    async def get(self, character_id: str, operator_id: str) -> object:  # noqa: ARG002
        raise RuntimeError("relationship seed table unreachable")


async def _seed_repository(
    *, character_id: str, operator_id: str, **fields: object,
) -> InMemoryCharacterOperatorRelationshipSeedRepository:
    repository = InMemoryCharacterOperatorRelationshipSeedRepository()
    await repository.save(
        CharacterOperatorRelationshipSeed(
            character_id=character_id, operator_id=operator_id, **fields,
        ),
    )
    return repository


def _service_with_relationship(
    *,
    character: Character,
    planner: _RecordingPlanner,
    repository: InMemoryStoryArcRepository | None = None,
    seed_repository: object | None = None,
    operator: _Operator | None = None,
    season_decider: StoryArcSeasonDeciderPort | None = None,
) -> StoryArcService:
    return StoryArcService(
        repository=repository or InMemoryStoryArcRepository(),
        planner=planner,
        operator_profile_service=_OperatorProfiles(operator),
        relationship_seed_repository=seed_repository,
        season_decider=season_decider,
    )


@pytest.mark.asyncio
async def test_planner_learns_how_the_player_is_addressed() -> None:
    character = _character()
    planner = _RecordingPlanner()
    operator = _Operator("op-1")
    service = _service_with_relationship(
        character=character,
        planner=planner,
        operator=operator,
        seed_repository=await _seed_repository(
            character_id=character.id,
            operator_id=operator.id,
            user_address_name="小北",
            character_address_name="唯醬",
            relationship_label="住在一起兩年的戀人",
            tone_distance="親暱但不黏",
        ),
    )

    await service.start_new_arc(character, today=date(2026, 5, 1))

    lines = planner.relationship_lines[0]
    assert "- 角色怎麼稱呼玩家：小北" in lines
    assert "- 玩家怎麼稱呼角色：唯醬" in lines
    assert "- 關係：住在一起兩年的戀人" in lines
    assert "- 語氣距離：親暱但不黏" in lines


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["no-repository", "no-operator", "no-profile-service", "empty-seed"],
)
async def test_absent_relationship_material_plans_exactly_as_before(
    kind: str,
) -> None:
    # Four ways to have nothing to say about the player. All of them must
    # hand the planner an empty tuple — which renders the pre-OP1-A
    # prompt — rather than a half-filled block the model feels obliged
    # to complete.
    character = _character()
    planner = _RecordingPlanner()
    operator = _Operator("op-1")
    if kind == "no-repository":
        service = _service_with_relationship(
            character=character, planner=planner, operator=operator,
        )
    elif kind == "no-operator":
        service = _service_with_relationship(
            character=character,
            planner=planner,
            operator=None,
            seed_repository=await _seed_repository(
                character_id=character.id,
                operator_id="op-1",
                relationship_label="戀人",
            ),
        )
    elif kind == "no-profile-service":
        service = StoryArcService(
            repository=InMemoryStoryArcRepository(),
            planner=planner,
            relationship_seed_repository=await _seed_repository(
                character_id=character.id,
                operator_id="op-1",
                relationship_label="戀人",
            ),
        )
    else:
        service = _service_with_relationship(
            character=character,
            planner=planner,
            operator=operator,
            # Confirmed, but the user filled nothing in.
            seed_repository=await _seed_repository(
                character_id=character.id, operator_id=operator.id,
            ),
        )

    arc = await service.start_new_arc(character, today=date(2026, 5, 1))

    assert arc.beats
    assert planner.relationship_lines == [()]


@pytest.mark.asyncio
async def test_relationship_lookup_failure_never_blocks_arc_creation() -> None:
    character = _character()
    planner = _RecordingPlanner()
    service = _service_with_relationship(
        character=character,
        planner=planner,
        operator=_Operator("op-1"),
        seed_repository=_ExplodingSeedRepository(),
    )

    arc = await service.start_new_arc(character, today=date(2026, 5, 1))

    # A missing relationship costs a nuance in one arc, never the arc.
    assert arc.beats
    assert planner.relationship_lines == [()]


@pytest.mark.asyncio
async def test_next_season_inherits_the_player_facts() -> None:
    # Season continuation is the same planner chain, so it must pick the
    # material up without its own plumbing — pinned here because "it
    # inherits naturally" is exactly the kind of claim that quietly stops
    # being true.
    repository = InMemoryStoryArcRepository()
    character = _character()
    planner = _RecordingPlanner()
    operator = _Operator("op-1")
    await _store_past_arc(
        repository,
        character_id=character.id,
        title="第一季",
        premise="第一段題材",
        updated_at=_at(1),
    )

    class _AlwaysOpen(StoryArcSeasonDeciderPort):
        async def decide(
            self, context: StoryArcSeasonContext,  # noqa: ARG002
        ) -> StoryArcSeasonDecision:
            return StoryArcSeasonDecision(should_start=True, reason="test")

    service = _service_with_relationship(
        character=character,
        planner=planner,
        repository=repository,
        operator=operator,
        season_decider=_AlwaysOpen(),
        seed_repository=await _seed_repository(
            character_id=character.id,
            operator_id=operator.id,
            relationship_label="還在試探彼此的同事",
        ),
    )

    arc = await service.ensure_active_arc(character, today=date(2026, 5, 1))

    assert arc is not None
    assert planner.relationship_lines[0] == ("- 關係：還在試探彼此的同事",)


@pytest.mark.asyncio
async def test_force_open_season_inherits_the_player_facts() -> None:
    # 起幕's forced open skips the season decider, not the planner
    # context: the player pulled this season into existence, so the
    # planner had better know who they are.
    repository = InMemoryStoryArcRepository()
    character = _character()
    planner = _RecordingPlanner()
    operator = _Operator("op-1")
    await _store_past_arc(
        repository,
        character_id=character.id,
        title="第一季",
        premise="第一段題材",
        updated_at=_at(1),
    )
    service = _service_with_relationship(
        character=character,
        planner=planner,
        repository=repository,
        operator=operator,
        # No decider wired at all — force_open_season must not need one.
        seed_repository=await _seed_repository(
            character_id=character.id,
            operator_id=operator.id,
            relationship_label="還在試探彼此的同事",
        ),
    )

    arc = await service.force_open_season(character, today=date(2026, 5, 1))

    assert arc is not None
    assert planner.relationship_lines[0] == ("- 關係：還在試探彼此的同事",)


@pytest.mark.asyncio
async def test_season_context_carries_history_without_the_completed_arc() -> None:
    repository = InMemoryStoryArcRepository()
    decider = _RecordingSeasonDecider()
    character = _character()
    await _store_past_arc(
        repository,
        character_id=character.id,
        title="第一季",
        premise="第一段題材",
        theme="growth",
        updated_at=_at(1),
    )
    just_finished = await _store_past_arc(
        repository,
        character_id=character.id,
        title="第二季",
        premise="剛演完的題材",
        theme="conflict",
        updated_at=_at(9),
    )
    service = StoryArcService(
        repository=repository,
        planner=_RecordingPlanner(),
        season_decider=decider,
    )

    await service.ensure_active_arc(
        character, today=date(2026, 5, 1), auto_start=True,
    )

    assert len(decider.contexts) == 1
    context = decider.contexts[0]
    assert context.completed_arc is not None
    assert context.completed_arc.id == just_finished.id
    assert context.arc_history == ("第一季｜growth｜第一段題材",)
