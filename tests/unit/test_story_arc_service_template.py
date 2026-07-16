"""StoryArcService template-vs-LLM selection (Phase 2 of SCENE_BEAT_PLAN).

Verifies the routing in ``start_new_arc``:

- character with ``arc_template_id`` + working template repo → template path,
  LLM planner is NOT called
- no template id → LLM planner is called as before
- unknown template id → fall back to LLM (don't crash)
- template repo not wired → LLM as before (Phase-2 opt-in)
- template materialise crash → fall back to LLM (defence in depth)
"""

from __future__ import annotations

from datetime import date

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.domain.entities.arc_template import (
    ArcTemplate,
    ArcTemplateBeat,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)


class _RecordingPlanner(StoryArcPlannerPort):
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
    ) -> StoryArc:
        self.calls += 1
        arc = StoryArc.create(
            character_id=character.id,
            title="LLM-planned",
            premise="LLM 規劃的 arc。",
            theme="custom",
            start_date=start_date,
            end_date=date(start_date.year, start_date.month, start_date.day),
        )
        beat = StoryArcBeat.create(
            arc_id=arc.id, sequence=0, scheduled_date=start_date,
            title="LLM beat", summary="LLM 規劃的第一個 beat。",
        )
        return arc.with_beats([beat])


class _StubTemplateRepo(ArcTemplateRepositoryPort):
    def __init__(self, templates: dict[str, ArcTemplate]) -> None:
        self._templates = templates
        # Track (template_id, user_id) pairs so tests can assert the
        # service forwarded the character's owner — important for the
        # per-(template, owner) visibility rule.
        self.lookups: list[tuple[str, str | None]] = []

    async def get_for_user(
        self, template_id: str, *, user_id: str | None,
    ) -> ArcTemplate | None:
        self.lookups.append((template_id, user_id))
        return self._templates.get(template_id)

    async def list_for_user(
        self, user_id: str | None,
    ) -> list[ArcTemplate]:
        return list(self._templates.values())

    async def list_packs(self) -> list[ArcTemplate]:
        return list(self._templates.values())

    async def save_for_user(
        self, template, *, user_id, overwrite=False,
    ) -> str:
        raise NotImplementedError

    async def delete_for_user(
        self, template_id: str, *, user_id: str,
    ) -> bool:
        raise NotImplementedError

    async def upsert_pack(
        self, template, *, pack_id: str, external_id: str | None = None,
    ) -> str:
        raise NotImplementedError


class _CrashingTemplateRepo(ArcTemplateRepositoryPort):
    async def get_for_user(
        self, template_id: str, *, user_id: str | None,
    ) -> ArcTemplate | None:
        raise RuntimeError("disk on fire")

    async def list_for_user(
        self, user_id: str | None,
    ) -> list[ArcTemplate]:
        return []

    async def list_packs(self) -> list[ArcTemplate]:
        return []

    async def save_for_user(
        self, template, *, user_id, overwrite=False,
    ) -> str:
        raise NotImplementedError

    async def delete_for_user(
        self, template_id: str, *, user_id: str,
    ) -> bool:
        raise NotImplementedError

    async def upsert_pack(
        self, template, *, pack_id: str, external_id: str | None = None,
    ) -> str:
        raise NotImplementedError


def _character(*, arc_template_id: str | None = None) -> Character:
    return Character.create(
        name="Aki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        arc_template_id=arc_template_id,
    )


def _template(template_id: str = "cafe_idol_audition") -> ArcTemplate:
    return ArcTemplate.create(
        id=template_id,
        title="三週的試鏡",
        premise="她偷偷報名了一場試鏡。",
        theme="ambition",
        duration_days=14,
        beats=[
            ArcTemplateBeat.create(
                sequence=0, day_offset=0, title="公告張貼",
                summary="她看見公告欄的海報。",
                location="學校公告欄",
                scene_characters=["凜"],
                dramatic_question="她敢報名嗎？",
            ),
            ArcTemplateBeat.create(
                sequence=1, day_offset=5, title="撞牆",
                summary="鏡子裡的自己呼吸不穩。",
                tension="rising", scene_type="conflict",
                location="音樂教室",
                scene_characters=["指導老師"],
                dramatic_question="她要承認嗎？",
            ),
        ],
    )


@pytest.mark.asyncio
async def test_template_path_skips_llm_planner() -> None:
    planner = _RecordingPlanner()
    template_repo = _StubTemplateRepo({"cafe_idol_audition": _template()})
    arc_repo = InMemoryStoryArcRepository()
    service = StoryArcService(
        repository=arc_repo,
        planner=planner,
        template_repository=template_repo,
    )
    character = _character(arc_template_id="cafe_idol_audition")

    arc = await service.start_new_arc(
        character, today=date(2026, 5, 1),
    )

    # Came from the template — title + scene structure prove it.
    assert arc.title == "三週的試鏡"
    assert len(arc.beats) == 2
    assert arc.beats[0].location == "學校公告欄"
    assert arc.beats[1].scene_characters == ("指導老師",)
    # Planner was never asked.
    assert planner.calls == 0
    # Lookup carried the character's owner so user-authored templates
    # stay invisible to other users.
    assert template_repo.lookups == [("cafe_idol_audition", "default")]
    # Persisted in the arc repository.
    saved = await arc_repo.get_active_for_character(character.id)
    assert saved is not None
    assert saved.id == arc.id


@pytest.mark.asyncio
async def test_no_template_id_falls_back_to_llm() -> None:
    planner = _RecordingPlanner()
    template_repo = _StubTemplateRepo({})
    arc_repo = InMemoryStoryArcRepository()
    service = StoryArcService(
        repository=arc_repo,
        planner=planner,
        template_repository=template_repo,
    )
    character = _character()  # arc_template_id=None

    arc = await service.start_new_arc(
        character, today=date(2026, 5, 1),
    )

    assert arc.title == "LLM-planned"
    assert planner.calls == 1
    # Template repo not consulted for an unbound character.
    assert template_repo.lookups == []


@pytest.mark.asyncio
async def test_unknown_template_id_falls_back_to_llm(
    caplog: pytest.LogCaptureFixture,
) -> None:
    planner = _RecordingPlanner()
    template_repo = _StubTemplateRepo({})  # no templates loaded
    arc_repo = InMemoryStoryArcRepository()
    service = StoryArcService(
        repository=arc_repo,
        planner=planner,
        template_repository=template_repo,
    )
    character = _character(arc_template_id="ghost_template")

    arc = await service.start_new_arc(
        character, today=date(2026, 5, 1),
    )

    # Unknown id → planner runs.
    assert arc.title == "LLM-planned"
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_no_template_repository_keeps_legacy_behaviour() -> None:
    # Phase 2 is opt-in — services constructed without a template
    # repository (legacy bootstrap path) must keep planner-only flow
    # so deploys can roll forward gradually.
    planner = _RecordingPlanner()
    arc_repo = InMemoryStoryArcRepository()
    service = StoryArcService(
        repository=arc_repo,
        planner=planner,
        template_repository=None,
    )
    character = _character(arc_template_id="cafe_idol_audition")

    arc = await service.start_new_arc(
        character, today=date(2026, 5, 1),
    )

    assert arc.title == "LLM-planned"
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_template_lookup_crash_falls_back_to_llm() -> None:
    planner = _RecordingPlanner()
    arc_repo = InMemoryStoryArcRepository()
    service = StoryArcService(
        repository=arc_repo,
        planner=planner,
        template_repository=_CrashingTemplateRepo(),
    )
    character = _character(arc_template_id="cafe_idol_audition")

    arc = await service.start_new_arc(
        character, today=date(2026, 5, 1),
    )

    assert arc.title == "LLM-planned"
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_template_path_abandons_existing_active_arc() -> None:
    # Same lifecycle invariant as the LLM path: at most one ACTIVE arc
    # per character. Switching templates should retire the old arc.
    planner = _RecordingPlanner()
    template_repo = _StubTemplateRepo({"cafe_idol_audition": _template()})
    arc_repo = InMemoryStoryArcRepository()
    service = StoryArcService(
        repository=arc_repo,
        planner=planner,
        template_repository=template_repo,
    )
    character = _character(arc_template_id="cafe_idol_audition")

    first = await service.start_new_arc(character, today=date(2026, 5, 1))
    assert first.status == "active"

    # Force a second start (e.g. operator clicked "重來").
    second = await service.start_new_arc(character, today=date(2026, 5, 10))
    assert second.id != first.id

    actives = [
        a for a in await arc_repo.list_for_character(character.id)
        if a.status == "active"
    ]
    assert len(actives) == 1
    assert actives[0].id == second.id
