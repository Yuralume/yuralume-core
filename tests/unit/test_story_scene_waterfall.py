"""The §3.1 waterfall end to end — layers 2 and 3 (SC1-B).

SC1-A proved layer 1 in ``test_story_scene_service.py``. What is under
test here is the *order* and the *degradation*: which layer answers when,
and that a layer which cannot answer hands down rather than failing the
button. The bar the plan sets for the bottom layer is high — pressing
起幕 should practically always raise a curtain — so most of these cases
are about material being thin, not about material being absent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.story_scene_material import (
    ForcedSeasonSceneMaterialProvider,
    PendingBeatSceneMaterialProvider,
)
from kokoro_link.application.services.story_scene_service import (
    SceneMaterialUnavailable,
    StorySceneService,
)
from kokoro_link.application.services.story_scene_side_story import (
    SideStorySceneMaterialProvider,
)
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.contracts.story_scene import (
    StorySceneOpeningDraft,
    StorySceneOpenerPort,
)
from kokoro_link.domain.entities.arc_series import ArcSeries
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.conversation import (
    SOURCE_WEB,
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.story_arc import (
    ARC_COMPLETED,
    BEAT_REALIZED,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_BEAT,
    SCENE_LAYER_NEW_SEASON,
    SCENE_LAYER_SIDE_STORY,
)
from kokoro_link.domain.entities.story_seed import (
    SEED_TIER_DAILY,
    SEED_TIER_DRAMATIC,
    StorySeed,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_arc_series import (
    InMemoryArcSeriesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
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


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 6, 1)

_DRAFT = StorySceneOpeningDraft(
    narration="雨停了，她把窗推開一條縫。",
    character_line="……你要不要過來看一下。",
    title="窗縫",
    location="房間",
    mood="安靜",
)


# ── doubles ──────────────────────────────────────────────────────────


class _StubOpener(StorySceneOpenerPort):
    def __init__(self) -> None:
        self.contexts: list = []

    async def write_opening(self, context):  # noqa: ANN001
        self.contexts.append(context)
        return _DRAFT


class _SeasonPlanner(StoryArcPlannerPort):
    """Plans a real three-beat season, or fails in a chosen way."""

    def __init__(
        self,
        *,
        beats: int = 3,
        raises: Exception | None = None,
        first_beat_operator_position: str | None = None,
    ) -> None:
        self.calls = 0
        self._beats = beats
        self._raises = raises
        self._first_beat_operator_position = first_beat_operator_position

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
        if self._raises is not None:
            raise self._raises
        arc = StoryArc.create(
            character_id=character.id,
            title="第二季",
            premise="她答應了一件她其實沒把握的事",
            theme="custom",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        return arc.with_beats([
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=index,
                scheduled_date=start_date + timedelta(days=index * 5),
                title=f"新的第 {index} 顆",
                summary=f"新的第 {index} 顆的內容",
                tension=TENSION_SETUP,
                operator_position=(
                    self._first_beat_operator_position if index == 0 else None
                ),
            )
            for index in range(self._beats)
        ])


class _StubSummarizer:
    def __init__(self, summary: str = "他們昨晚在講那把還沒修好的琴。") -> None:
        self._summary = summary
        self.calls = 0

    async def summarize(self, *, character, messages, now=None, local_tz=None):  # noqa: ANN001
        self.calls += 1
        return self._summary


class _StubOperatorProfiles:
    def __init__(self, language: str = "zh-TW") -> None:
        self._language = language

    async def get_for_user(self, user_id: str):  # noqa: ANN201
        return type("_Profile", (), {"primary_language": self._language})()


def _completed_season(character_id: str) -> StoryArc:
    """A finished season, built without going near the planner.

    Setting the dormant state up directly keeps every ``planner.calls``
    assertion attributable to the button and lets a test wire a planner
    that only ever fails.
    """
    arc = StoryArc.create(
        character_id=character_id,
        title="第一季",
        premise="她把一場獨奏會撐完了",
        theme="custom",
        start_date=TODAY - timedelta(days=21),
        end_date=TODAY - timedelta(days=1),
    )
    beat = StoryArcBeat.create(
        arc_id=arc.id,
        sequence=0,
        scheduled_date=TODAY - timedelta(days=14),
        title="上台",
        summary="她上了台。",
        tension=TENSION_SETUP,
    )
    return arc.with_beats(
        [beat.with_status(BEAT_REALIZED)],
    ).with_status(ARC_COMPLETED)


def _character(**kwargs) -> Character:  # noqa: ANN003
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
        **kwargs,
    )


def _template(template_id: str, title: str) -> ArcTemplate:
    return ArcTemplate.create(
        id=template_id,
        title=title,
        premise=f"{title} 的劇情。",
        theme="growth",
        duration_days=7,
        beats=[
            ArcTemplateBeat.create(
                sequence=0, day_offset=0,
                title=f"{title} 開場", summary="第一場戲。",
            ),
        ],
    )


# ── fixture ──────────────────────────────────────────────────────────


class _Waterfall:
    """All three layers wired the way the container wires them."""

    def __init__(
        self,
        *,
        planner: _SeasonPlanner | None = None,
        series: bool = False,
        seeds: list[StorySeed] | None = None,
        summarizer: _StubSummarizer | None = None,
    ) -> None:
        self.arcs_repo = InMemoryStoryArcRepository()
        self.series_repo = InMemoryArcSeriesRepository()
        self.template_repo = InMemoryArcTemplateRepository()
        self.memories = InMemoryMemoryRepository()
        self.relationship_seeds = (
            InMemoryCharacterOperatorRelationshipSeedRepository()
        )
        self.conversations = InMemoryConversationRepository()
        self.sessions = InMemoryStorySceneSessionRepository()
        self.seed_repo = InMemoryStorySeedRepository()
        self.events = InMemoryStoryEventRepository()
        self.planner = planner or _SeasonPlanner()
        self.summarizer = summarizer or _StubSummarizer()
        self._seeds = seeds or []
        self._series = series
        self.opener = _StubOpener()

        self.arc_service = StoryArcService(
            repository=self.arcs_repo,
            planner=self.planner,
            template_repository=self.template_repo,
            series_repository=self.series_repo,
        )
        self.side_story = SideStorySceneMaterialProvider(
            memory_repository=self.memories,
            relationship_seed_repository=self.relationship_seeds,
            conversation_repository=self.conversations,
            dialogue_summarizer=self.summarizer,
            gacha_service=StoryGachaService(
                seed_repository=self.seed_repo,
                event_repository=self.events,
            ),
            operator_profile_service=_StubOperatorProfiles(),
        )
        self.service = StorySceneService(
            sessions=self.sessions,
            conversations=self.conversations,
            opener=self.opener,
            material_providers=(
                PendingBeatSceneMaterialProvider(
                    story_arc_service=self.arc_service,
                ),
                ForcedSeasonSceneMaterialProvider(
                    story_arc_service=self.arc_service,
                ),
                self.side_story,
            ),
            story_arc_service=self.arc_service,
        )

    async def setup(self) -> None:
        for seed in self._seeds:
            await self.seed_repo.add(seed)
        if self._series:
            await self.template_repo.save_for_user(
                _template("book-one", "第一本"), user_id="user-a",
            )
            await self.series_repo.save_for_user(
                ArcSeries.create(
                    id="series-a",
                    title="連載篇",
                    premise="只有一本。",
                    template_ids=["book-one"],
                    user_id="user-a",
                ),
                user_id="user-a",
            )

    @property
    def material(self):  # noqa: ANN201
        return self.opener.contexts[-1].material


async def _waterfall(**kwargs) -> _Waterfall:  # noqa: ANN003
    fixture = _Waterfall(**kwargs)
    await fixture.setup()
    return fixture


# ── layer order ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_running_season_is_played_not_replaced() -> None:
    fx = await _waterfall()
    character = _character()
    live = await fx.arc_service.start_new_arc(character, today=TODAY)
    calls_before = fx.planner.calls

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_BEAT
    assert opening.session.arc_id == live.id
    # layer 2 was never reached, so no season was planned over the top
    assert fx.planner.calls == calls_before


@pytest.mark.asyncio
async def test_a_dormant_character_gets_a_forced_season_and_its_first_beat(
) -> None:
    fx = await _waterfall()
    character = _character()
    first = _completed_season(character.id)
    await fx.arcs_repo.add(first)

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_NEW_SEASON
    fresh = await fx.arcs_repo.get_active_for_character(character.id)
    assert fresh is not None and fresh.id != first.id
    assert opening.session.arc_id == fresh.id
    assert opening.session.beat_id == fresh.beats[0].id
    # one tap also plants the rest of the season as future scheduled beats
    assert len(fresh.beats) == 3
    assert [b.scheduled_date for b in fresh.beats[1:]] == [
        TODAY + timedelta(days=5), TODAY + timedelta(days=10),
    ]


@pytest.mark.asyncio
async def test_the_forced_seasons_first_beat_carries_its_player_position(
) -> None:
    """Layer 2 differs from layer 1 only in *how it got the arc* (module
    docstring) — the planner's own beat position must reach the material
    exactly the same way a pre-existing beat's does."""
    fx = await _waterfall(
        planner=_SeasonPlanner(first_beat_operator_position="present"),
    )
    character = _character()
    await fx.arcs_repo.add(_completed_season(character.id))

    await fx.service.open_scene(character, now=NOW)

    assert fx.material.layer == SCENE_LAYER_NEW_SEASON
    assert fx.material.operator_position == "present"


@pytest.mark.asyncio
async def test_the_forced_beat_is_staged_like_any_other_played_beat() -> None:
    fx = await _waterfall()
    character = _character()
    await fx.arcs_repo.add(_completed_season(character.id))

    opening = await fx.service.open_scene(character, now=NOW)

    played = await fx.arc_service.find_beat(opening.session.beat_id)
    assert played is not None
    assert played.play_attempt_count == 1
    assert played.play_failure_count == 0


@pytest.mark.asyncio
async def test_a_crashing_planner_falls_through_to_the_side_story() -> None:
    fx = await _waterfall(
        planner=_SeasonPlanner(raises=RuntimeError("planner exploded")),
    )
    character = _character()
    await fx.arcs_repo.add(_completed_season(character.id))

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_SIDE_STORY
    assert opening.session.arc_id is None
    assert opening.session.beat_id is None


@pytest.mark.asyncio
async def test_a_beatless_season_falls_through_to_the_side_story() -> None:
    """The arc still stands; tonight's scene comes from the layer below."""
    fx = await _waterfall(planner=_SeasonPlanner(beats=0))
    character = _character()
    await fx.arcs_repo.add(_completed_season(character.id))

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_SIDE_STORY


@pytest.mark.asyncio
async def test_a_concluded_series_falls_through_to_the_side_story() -> None:
    """§3.1's named hand-off: the author's books ran out, the story does not."""
    fx = await _waterfall(series=True)
    character = _character(arc_series_id="series-a")
    only_book = await fx.arc_service.ensure_active_arc(character, today=TODAY)
    assert only_book is not None
    await fx.arcs_repo.save(only_book.with_status(ARC_COMPLETED))

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_SIDE_STORY
    assert fx.planner.calls == 0


@pytest.mark.asyncio
async def test_side_story_records_no_play_attempt() -> None:
    """No beat, no attempt — an impromptu scene owes no arc bookkeeping."""
    fx = await _waterfall(planner=_SeasonPlanner(raises=RuntimeError("nope")))
    character = _character()
    first = _completed_season(character.id)
    await fx.arcs_repo.add(first)
    attempts_before = [b.play_attempt_count for b in first.beats]

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.beat_id is None
    reloaded = await fx.arcs_repo.get(first.id)
    assert [b.play_attempt_count for b in reloaded.beats] == attempts_before


# ── side story material ──────────────────────────────────────────────


async def _dormant_side_story_fixture(**kwargs) -> tuple[_Waterfall, Character]:
    """A character whose only remaining layer is the side story."""
    fx = await _waterfall(
        planner=_SeasonPlanner(raises=RuntimeError("planner exploded")),
        **kwargs,
    )
    character = _character()
    await fx.arcs_repo.add(_completed_season(character.id))
    return fx, character


@pytest.mark.asyncio
async def test_side_story_material_carries_relationship_memory_and_dialogue(
) -> None:
    fx, character = await _dormant_side_story_fixture(
        seeds=[
            StorySeed.create(
                seed_text="一封沒有署名的信被塞進門縫。",
                tier=SEED_TIER_DRAMATIC,
            ),
        ],
    )
    await fx.relationship_seeds.save(
        CharacterOperatorRelationshipSeed(
            character_id=character.id,
            operator_id="user-a",
            relationship_label="住在對門的鄰居",
            user_address_name="小遊",
            character_address_name="Mio",
        ),
    )
    await fx.memories.add(
        MemoryItem.create(
            character_id=character.id,
            kind=MemoryKind.RELATIONSHIP,
            content="他答應過會來聽她的獨奏，然後沒有出現。",
            salience=0.9,
        ),
    )
    conversation = Conversation.start(
        character_id=character.id, source=SOURCE_WEB,
    ).append(Message(role=MessageRole.USER, content="那把琴修好了嗎"))
    await fx.conversations.save(conversation)

    await fx.service.open_scene(character, now=NOW)

    summary = fx.material.summary
    assert "住在對門的鄰居" in summary
    # the address terms keep their direction: 稱呼使用者 is what SHE calls HIM
    assert "稱呼使用者：小遊" in summary
    assert "他答應過會來聽她的獨奏" in summary
    assert "他們昨晚在講那把還沒修好的琴。" in summary
    assert "一封沒有署名的信被塞進門縫。" in summary
    # and it hands the framing job to the director rather than faking one
    assert fx.material.dramatic_question is None
    assert "即席番外" in summary


@pytest.mark.asyncio
async def test_side_story_opens_on_an_empty_seed_pool() -> None:
    """SE3 not imported is normal operation, not a degraded mode (§6)."""
    fx, character = await _dormant_side_story_fixture()
    await fx.memories.add(
        MemoryItem.create(
            character_id=character.id,
            kind=MemoryKind.EPISODIC,
            content="上週她一個人把譜架搬回家。",
            salience=0.8,
        ),
    )

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_SIDE_STORY
    summary = fx.material.summary
    assert "上週她一個人把譜架搬回家。" in summary
    assert "可用的即席素材" not in summary


@pytest.mark.asyncio
async def test_side_story_does_not_draw_daily_seeds() -> None:
    """Everyday micro-events are the gacha's; this layer wants drama."""
    fx, character = await _dormant_side_story_fixture(
        seeds=[
            StorySeed.create(
                seed_text="今天的咖啡買一送一。", tier=SEED_TIER_DAILY,
            ),
        ],
    )

    await fx.service.open_scene(character, now=NOW)

    assert "今天的咖啡買一送一。" not in fx.material.summary


@pytest.mark.asyncio
async def test_side_story_opens_for_a_character_with_no_history_at_all(
) -> None:
    """Nothing in the pair's past is a precondition for a scene."""
    fx, character = await _dormant_side_story_fixture()

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_SIDE_STORY
    assert fx.material.summary.strip()


@pytest.mark.asyncio
async def test_side_story_never_forces_a_player_position() -> None:
    """OP2-D §2 #4: there is no beat here to have judged one, and the
    player is already the addressee of the relationship/memory/dialogue
    material — forcing a value would be inventing a judgment nobody made.
    """
    fx, character = await _dormant_side_story_fixture()

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_SIDE_STORY
    assert fx.material.operator_position is None
    assert fx.material.operator_note is None
    assert opening.session.operator_position is None
    assert opening.session.operator_note is None


@pytest.mark.asyncio
async def test_low_salience_memories_stay_out_of_the_material() -> None:
    fx, character = await _dormant_side_story_fixture()
    await fx.memories.add(
        MemoryItem.create(
            character_id=character.id,
            kind=MemoryKind.EPISODIC,
            content="她今天喝了溫開水。",
            salience=0.1,
        ),
    )

    await fx.service.open_scene(character, now=NOW)

    assert "她今天喝了溫開水。" not in fx.material.summary


@pytest.mark.asyncio
async def test_the_material_keeps_the_memories_with_the_most_pull() -> None:
    """A side story is built on what weighs on her, not on the last N rows."""
    fx, character = await _dormant_side_story_fixture()
    for index in range(9):
        await fx.memories.add(
            MemoryItem.create(
                character_id=character.id,
                kind=MemoryKind.EPISODIC,
                content=f"瑣事 {index}",
                salience=0.55,
            ),
        )
    await fx.memories.add(
        MemoryItem.create(
            character_id=character.id,
            kind=MemoryKind.EPISODIC,
            content="她把那張沒送出去的票留到現在。",
            salience=0.95,
        ),
    )

    await fx.service.open_scene(character, now=NOW)

    summary = fx.material.summary
    assert "她把那張沒送出去的票留到現在。" in summary
    assert sum(f"瑣事 {i}" in summary for i in range(9)) == 5


@pytest.mark.asyncio
async def test_a_broken_summarizer_does_not_cost_the_player_the_scene(
) -> None:
    class _ExplodingSummarizer(_StubSummarizer):
        async def summarize(self, *, character, messages, now=None, local_tz=None):  # noqa: ANN001
            raise RuntimeError("summarizer is down")

    fx, character = await _dormant_side_story_fixture(
        summarizer=_ExplodingSummarizer(),
    )
    conversation = Conversation.start(
        character_id=character.id, source=SOURCE_WEB,
    ).append(Message(role=MessageRole.USER, content="嗨"))
    await fx.conversations.save(conversation)

    opening = await fx.service.open_scene(character, now=NOW)

    assert opening.session.source_layer == SCENE_LAYER_SIDE_STORY
    assert "最近的對話" not in fx.material.summary


@pytest.mark.asyncio
async def test_every_layer_declining_still_reports_no_material() -> None:
    """The bottom of the waterfall is allowed to fail — loudly, not silently."""
    fx, character = await _dormant_side_story_fixture()

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("material assembly is broken")

    fx.side_story._seed_block = _boom  # noqa: SLF001 - the layer under test

    with pytest.raises(SceneMaterialUnavailable) as caught:
        await fx.service.open_scene(character, now=NOW)

    assert caught.value.code == "no_material"
    assert await fx.sessions.get_open_for_character(character.id) is None
