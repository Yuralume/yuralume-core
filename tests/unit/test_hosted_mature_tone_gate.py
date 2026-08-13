"""Hosted (cloud-mode) gate on the ``mature`` arc tone — GF6.

The hole this closes: the ``mature`` tone
tells the scene writers that "暴力、肉體、權力支配、酒精、性、創傷的
細節都可以據實寫", and the template wizard happily offers it. Hosted
that contradicts Yuralume's own terms/AUP, which promise no sexually
explicit content.

Every test here comes in pairs: the cloud-mode assertion, and the
self-host assertion that pins the *unchanged* behaviour. The gate is
only allowed to exist when ``cloud_mode`` is true.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.arc_template_intake import (
    router as intake_router,
)
from kokoro_link.api.routes.arc_templates import router as templates_router
from kokoro_link.application.services.arc_template_intake_service import (
    ArcTemplateIntakeService,
    TemplateDraft,
    BeatDraft,
)
from kokoro_link.application.services.arc_series_service import ArcSeriesService
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportService,
)
from kokoro_link.application.services.chat_assist_service import _story_lines
from kokoro_link.contracts.arc_series_continuation import (
    ArcSeriesContinuationContext,
)
from kokoro_link.contracts.story import SceneContext
from kokoro_link.contracts.story_arc import StoryBeatSceneContext
from kokoro_link.contracts.story_scene import (
    StorySceneMaterial,
    StorySceneOpeningContext,
)
from kokoro_link.domain.entities.arc_series import (
    ArcSeries,
    CharacterSeriesProgress,
)
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.domain.entities.story_scene_session import SCENE_LAYER_BEAT
from kokoro_link.domain.services import story_tone_policy as policy
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_arc_series import (
    InMemoryArcSeriesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.story.arc_series_continuation_adapter import (
    LLMArcSeriesContinuationDraftAdapter,
)
from kokoro_link.infrastructure.story.llm_beat_scene_writer import (
    LLMStoryBeatSceneWriter,
)
from kokoro_link.infrastructure.story.llm_expander import (
    LLMStoryEventExpander,
)
from kokoro_link.infrastructure.story.llm_scene_opener import (
    LLMStorySceneOpener,
)


TODAY = date(2026, 6, 1)
_TEST_USER_ID = "alice"

# The exact clause the plan calls out as the AUP violation.
_EXPLICIT_MARKERS = ("不要迴避", "酒精、性", "獵奇", "童書語言")


# ---------- policy unit ------------------------------------------------


def test_policy_is_a_noop_outside_cloud_mode() -> None:
    for tone in ("mature", "daily", "  露骨，越詳細越好  ", ""):
        assert policy.fold_stored_tone(tone, cloud_mode=False) == tone
        assert policy.resolve_prompt_tone(tone, cloud_mode=False) == tone
    assert policy.selectable_tones(cloud_mode=False) == policy.SELECTABLE_TONES
    assert "mature" in policy.tone_vocabulary(cloud_mode=False)


def test_policy_folds_mature_to_dramatic_in_cloud_mode() -> None:
    assert policy.fold_stored_tone("mature", cloud_mode=True) == "dramatic"
    assert policy.fold_stored_tone("MATURE", cloud_mode=True) == "dramatic"
    assert policy.resolve_prompt_tone("mature", cloud_mode=True) == "dramatic"


def test_policy_leaves_allowed_tones_alone_in_cloud_mode() -> None:
    for tone in ("daily", "dramatic", "dark", "lighthearted"):
        assert policy.fold_stored_tone(tone, cloud_mode=True) == tone
        assert policy.resolve_prompt_tone(tone, cloud_mode=True) == tone


def test_policy_render_boundary_is_an_allowlist_but_write_boundary_is_not() -> None:
    """An off-catalogue tone is an unbounded instruction channel.

    Hosted it must not reach the model verbatim — but it also must not
    be rewritten in the operator's stored row, which is data we don't
    own the meaning of."""
    weird = "露骨，越詳細越好"
    assert policy.resolve_prompt_tone(weird, cloud_mode=True) == "daily"
    assert policy.fold_stored_tone(weird, cloud_mode=True) == weird


def test_policy_blank_tone_stays_blank() -> None:
    assert policy.resolve_prompt_tone("", cloud_mode=True) == ""
    assert policy.resolve_prompt_tone(None, cloud_mode=True) == ""


def test_policy_catalogue_drops_mature_in_cloud_mode() -> None:
    assert "mature" not in policy.selectable_tones(cloud_mode=True)
    assert "mature" not in policy.tone_vocabulary(cloud_mode=True)
    assert policy.filter_suggested_tones(
        ["daily", "mature", "dark"], cloud_mode=True,
    ) == ["daily", "dark"]
    assert policy.filter_suggested_tones(
        ["daily", "mature", "dark"], cloud_mode=False,
    ) == ["daily", "mature", "dark"]


# ---------- shared fixtures --------------------------------------------


class _CapturingModel:
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str) -> None:
        self._response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs):  # noqa: ANN003
        self.prompts.append(prompt)
        return self._response

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        yield await self.generate(prompt, **kwargs)

    async def list_models(self) -> list[str]:
        return ["scripted"]


def _character() -> Character:
    return Character(
        id="c1",
        name="Mio",
        summary="a violinist",
        personality=(),
        interests=(),
        speaking_style="soft",
        boundaries=(),
        aspirations=(),
        appearance="",
        world_frame="modern",
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


class _DuckSeed:
    """Arc-beat -> expander shape (no ``tags``), same stand-in the
    existing expander scene tests use."""

    def __init__(self, seed_text: str) -> None:
        self.seed_text = seed_text
        self.id = "seed-1"


def _mature_arc() -> StoryArc:
    return StoryArc.create(
        character_id="c1",
        title="征服軍的最後一夜",
        premise="他在攻陷的城裡等天亮。",
        theme="ambition",
        tone="mature",
        start_date=TODAY,
        end_date=TODAY,
    )


def _beat(arc: StoryArc) -> StoryArcBeat:
    return StoryArcBeat.create(
        arc_id=arc.id,
        sequence=0,
        scheduled_date=TODAY,
        title="最後一夜",
        summary="他坐在廢墟裡，聽著外面的聲音。",
        tension=TENSION_RISING,
        scene_characters=("副官",),
        location="廢墟",
        dramatic_question="他還算不算人？",
    )


# ---------- expander scene prompt (defence in depth) -------------------
#
# R-OP-2 check, done at implementation time: ``_build_and_persist_from_beat``
# (``story_event_service.py``) is the only caller that hands the expander a
# ``SceneContext``, and nothing in ``src/`` calls it — the live beat path is
# ``StoryBeatSceneService`` -> ``LLMStoryBeatSceneWriter``. This surface is
# therefore production-dead today, and is gated anyway so a future caller
# doesn't reopen the hole.


@pytest.mark.asyncio
async def test_expander_scene_prompt_keeps_explicit_directives_self_host() -> None:
    model = _CapturingModel('{"narrative": "我站在廢墟裡。", "tone": null}')
    expander = LLMStoryEventExpander(model=model)

    await expander.expand(
        seed=_DuckSeed("城破的夜晚。"),
        character_name="Torban",
        character_summary="征服軍主帥",
        speaking_style="冷硬",
        world_frame="fantasy",
        scene=SceneContext(
            scene_type="conflict", location="廢墟", tone="mature",
        ),
    )

    prompt = model.prompts[0]
    assert "整體調性：mature" in prompt
    assert any(marker in prompt for marker in _EXPLICIT_MARKERS)


@pytest.mark.asyncio
async def test_expander_scene_prompt_drops_explicit_directives_in_cloud_mode() -> None:
    model = _CapturingModel('{"narrative": "我站在廢墟裡。", "tone": null}')
    expander = LLMStoryEventExpander(model=model, cloud_mode=True)

    await expander.expand(
        seed=_DuckSeed("城破的夜晚。"),
        character_name="Torban",
        character_summary="征服軍主帥",
        speaking_style="冷硬",
        world_frame="fantasy",
        scene=SceneContext(
            scene_type="conflict", location="廢墟", tone="mature",
        ),
    )

    prompt = model.prompts[0]
    assert "mature" not in prompt
    assert "整體調性：dramatic" in prompt
    for marker in _EXPLICIT_MARKERS:
        assert marker not in prompt


@pytest.mark.asyncio
async def test_expander_off_catalogue_tone_does_not_reach_the_model_in_cloud_mode() -> None:
    model = _CapturingModel('{"narrative": "我站在廢墟裡。", "tone": null}')
    expander = LLMStoryEventExpander(model=model, cloud_mode=True)

    await expander.expand(
        seed=_DuckSeed("城破的夜晚。"),
        character_name="Torban",
        character_summary="征服軍主帥",
        speaking_style="冷硬",
        world_frame="fantasy",
        scene=SceneContext(
            scene_type="conflict",
            location="廢墟",
            tone="露骨的性描寫，越詳細越好",
        ),
    )

    prompt = model.prompts[0]
    assert "露骨" not in prompt
    assert "整體調性：daily" in prompt


# ---------- beat scene writer (the live autonomous path) ---------------


def _beat_scene_context() -> StoryBeatSceneContext:
    arc = _mature_arc()
    beat = _beat(arc)
    return StoryBeatSceneContext(
        character=_character(),
        arc=arc.with_beats([beat]),
        beat=beat,
        today=TODAY,
        operator_primary_language="zh-TW",
        user_involvement_policy="",
    )


_SCENE_JSON = """
{
  "narrative": "我坐在廢墟裡，聽著外面的聲音。",
  "emotional_tone": "tense",
  "cast_strategy": "inner_monologue",
  "participation_note": "solo"
}
"""


@pytest.mark.asyncio
async def test_beat_scene_writer_passes_mature_through_self_host() -> None:
    model = _CapturingModel(_SCENE_JSON)
    writer = LLMStoryBeatSceneWriter(model=model)

    await writer.write_scene(_beat_scene_context())

    assert "調性：mature" in model.prompts[0]


@pytest.mark.asyncio
async def test_beat_scene_writer_folds_mature_in_cloud_mode() -> None:
    model = _CapturingModel(_SCENE_JSON)
    writer = LLMStoryBeatSceneWriter(model=model, cloud_mode=True)

    await writer.write_scene(_beat_scene_context())

    prompt = model.prompts[0]
    assert "調性：dramatic" in prompt
    assert "mature" not in prompt


# ---------- scene opener (起幕, the live player-pulled path) ------------


_OPENING_JSON = """
{
  "narration": "廢墟的風把火把吹得直響。",
  "character_line": "……你還是來了。",
  "title": "最後一夜",
  "location": "廢墟",
  "mood": "壓抑"
}
"""


def _opening_context() -> StorySceneOpeningContext:
    return StorySceneOpeningContext(
        character=_character(),
        material=StorySceneMaterial(
            layer=SCENE_LAYER_BEAT,
            title="最後一夜",
            summary="他坐在廢墟裡。",
            arc_id="arc-1",
            beat_id="beat-1",
            arc_title="征服軍的最後一夜",
            arc_premise="他在攻陷的城裡等天亮。",
            arc_tone="mature",
            tension=TENSION_RISING,
            scene_type="conflict",
            location="廢墟",
            dramatic_question="他還算不算人？",
            scene_characters=("副官",),
        ),
        today=TODAY,
        operator_primary_language="zh-TW",
    )


@pytest.mark.asyncio
async def test_scene_opener_passes_mature_through_self_host() -> None:
    model = _CapturingModel(_OPENING_JSON)
    opener = LLMStorySceneOpener(model=model)

    await opener.write_opening(_opening_context())

    assert "調性：mature" in model.prompts[0]


@pytest.mark.asyncio
async def test_scene_opener_folds_mature_in_cloud_mode() -> None:
    model = _CapturingModel(_OPENING_JSON)
    opener = LLMStorySceneOpener(model=model, cloud_mode=True)

    await opener.write_opening(_opening_context())

    prompt = model.prompts[0]
    assert "調性：dramatic" in prompt
    assert "mature" not in prompt


# ---------- chat assist prompt (the tone label leaks here too) ---------


def test_chat_assist_story_lines_fold_mature_in_cloud_mode() -> None:
    arc = _mature_arc()
    self_host = _story_lines(arc, today=TODAY, cloud_mode=False)
    hosted = _story_lines(arc, today=TODAY, cloud_mode=True)

    assert any("mature" in line for line in self_host)
    assert not any("mature" in line for line in hosted)
    assert any("dramatic" in line for line in hosted)


# ---------- intake wizard: prompts, suggestions, save ------------------


class _FakeModel:
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str) -> None:
        self._response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs):  # noqa: ANN003
        self.prompts.append(prompt)
        return self._response

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        yield await self.generate(prompt, **kwargs)

    async def list_models(self) -> list[str]:
        return ["scripted"]


def _draft(tone: str) -> TemplateDraft:
    return TemplateDraft(
        id="gf6_tone_draft",
        title="測試範本",
        premise="一段測試用的前提，足夠長到能通過驗證。",
        theme="ambition",
        tone=tone,
        duration_days=14,
        beats=(
            BeatDraft(
                sequence=0,
                day_offset=0,
                title="起點",
                summary="場景一摘要。",
                tension="setup",
                scene_type="encounter",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_intake_meta_prompt_omits_mature_in_cloud_mode() -> None:
    model = _FakeModel('{"titles": [], "themes": [], "tones": [], "world_frames": []}')
    hosted = ArcTemplateIntakeService(
        repository=InMemoryArcTemplateRepository(),
        model=model,
        cloud_mode=True,
    )

    await hosted.suggest_meta("黑暗奇幻戰爭劇")

    assert "mature" not in model.prompts[0]
    assert "daily / dramatic / dark / lighthearted" in model.prompts[0]


@pytest.mark.asyncio
async def test_intake_meta_prompt_keeps_mature_self_host() -> None:
    model = _FakeModel('{"titles": [], "themes": [], "tones": [], "world_frames": []}')
    selfhost = ArcTemplateIntakeService(
        repository=InMemoryArcTemplateRepository(), model=model,
    )

    await selfhost.suggest_meta("黑暗奇幻戰爭劇")

    assert "daily / dramatic / mature / dark / lighthearted" in model.prompts[0]


@pytest.mark.asyncio
async def test_intake_suggestions_drop_a_mature_the_model_proposed_anyway() -> None:
    payload = (
        '{"titles": ["a"], "themes": ["loss"], '
        '"tones": ["mature", "dark"], "world_frames": ["modern"]}'
    )
    hosted = ArcTemplateIntakeService(
        repository=InMemoryArcTemplateRepository(),
        model=_FakeModel(payload),
        cloud_mode=True,
    )
    selfhost = ArcTemplateIntakeService(
        repository=InMemoryArcTemplateRepository(),
        model=_FakeModel(payload),
    )

    assert (await hosted.suggest_meta("x")).tones == ["dark"]
    assert (await selfhost.suggest_meta("x")).tones == ["mature", "dark"]


@pytest.mark.asyncio
async def test_intake_save_folds_mature_in_cloud_mode() -> None:
    repo = InMemoryArcTemplateRepository()
    service = ArcTemplateIntakeService(
        repository=repo, model=_FakeModel("{}"), cloud_mode=True,
    )

    template_id = await service.save_template(
        _draft("mature"), user_id=_TEST_USER_ID,
    )

    saved = await repo.get_for_user(template_id, user_id=_TEST_USER_ID)
    assert saved is not None
    assert saved.tone == "dramatic"


@pytest.mark.asyncio
async def test_intake_save_keeps_mature_self_host() -> None:
    repo = InMemoryArcTemplateRepository()
    service = ArcTemplateIntakeService(repository=repo, model=_FakeModel("{}"))

    template_id = await service.save_template(
        _draft("mature"), user_id=_TEST_USER_ID,
    )

    saved = await repo.get_for_user(template_id, user_id=_TEST_USER_ID)
    assert saved is not None
    assert saved.tone == "mature"


# ---------- REST surfaces ----------------------------------------------


@dataclass
class _CloudSettings:
    active: bool = False


@dataclass
class _AppSettings:
    cloud: _CloudSettings


@dataclass
class _Container:
    arc_template_intake_service: ArcTemplateIntakeService | None = None
    arc_template_repository: object | None = None
    app_settings: _AppSettings | None = None
    character_service: object | None = None


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(intake_router, prefix="/api/v1")
    app.include_router(templates_router, prefix="/api/v1")
    return TestClient(app)


def _rest_container(*, cloud: bool) -> _Container:
    repo = InMemoryArcTemplateRepository()
    return _Container(
        arc_template_intake_service=ArcTemplateIntakeService(
            repository=repo, model=_FakeModel("{}"), cloud_mode=cloud,
        ),
        arc_template_repository=repo,
        app_settings=_AppSettings(cloud=_CloudSettings(active=cloud)),
    )


def test_scaffolds_tone_catalogue_drops_mature_in_cloud_mode() -> None:
    resp = _client(_rest_container(cloud=True)).get(
        "/api/v1/arc-templates/scaffolds",
    )
    assert resp.status_code == 200
    assert "mature" not in {t["id"] for t in resp.json()["tones"]}


def test_scaffolds_tone_catalogue_keeps_mature_self_host() -> None:
    resp = _client(_rest_container(cloud=False)).get(
        "/api/v1/arc-templates/scaffolds",
    )
    assert resp.status_code == 200
    assert "mature" in {t["id"] for t in resp.json()["tones"]}


def _patch_body(tone: str) -> dict:
    return {
        "title": "測試範本",
        "premise": "一段測試用的前提，足夠長到能通過驗證。",
        "theme": "ambition",
        "tone": tone,
        "duration_days": 14,
        "world_frames": ["modern"],
        "required_traits": [],
        "beats": [
            {
                "sequence": 0,
                "day_offset": 0,
                "title": "起點",
                "summary": "場景一摘要。",
                "tension": "setup",
                "scene_type": "encounter",
                "location": "教室",
                "scene_characters": [],
                "dramatic_question": None,
                "required": True,
            },
        ],
    }


def test_patch_arc_template_folds_mature_in_cloud_mode() -> None:
    client = _client(_rest_container(cloud=True))
    resp = client.patch(
        "/api/v1/arc-templates/gf6_patch", json=_patch_body("mature"),
    )
    assert resp.status_code == 200
    assert resp.json()["tone"] == "dramatic"


def test_patch_arc_template_keeps_mature_self_host() -> None:
    client = _client(_rest_container(cloud=False))
    resp = client.patch(
        "/api/v1/arc-templates/gf6_patch", json=_patch_body("mature"),
    )
    assert resp.status_code == 200
    assert resp.json()["tone"] == "mature"


# ---------- character card import (the other "匯入" path) --------------


_CARD_TEMPLATE_YAML = """
id: imported_mature
title: 匯入的範本
premise: 一段從卡片匯入的前提，長度足以通過驗證。
theme: ambition
tone: mature
duration_days: 14
beats:
  - sequence: 0
    day_offset: 0
    title: 起點
    summary: 場景一摘要。
    tension: setup
    scene_type: encounter
"""


def _import_service(repo, *, cloud: bool) -> CharacterCardImportService:
    return CharacterCardImportService(
        character_service=None,
        character_image_service=None,
        arc_template_repository=repo,
        cloud_mode=cloud,
    )


@pytest.mark.asyncio
async def test_card_import_folds_mature_in_cloud_mode() -> None:
    repo = InMemoryArcTemplateRepository()
    service = _import_service(repo, cloud=True)

    _, landed = await service._land_arc_templates(
        {"imported_mature.yaml": _CARD_TEMPLATE_YAML},
        user_id=_TEST_USER_ID,
        target_character_ref_map={},
    )

    saved = await repo.get_for_user(landed[0], user_id=_TEST_USER_ID)
    assert saved is not None
    assert saved.tone == "dramatic"


@pytest.mark.asyncio
async def test_card_import_keeps_mature_self_host() -> None:
    repo = InMemoryArcTemplateRepository()
    service = _import_service(repo, cloud=False)

    _, landed = await service._land_arc_templates(
        {"imported_mature.yaml": _CARD_TEMPLATE_YAML},
        user_id=_TEST_USER_ID,
        target_character_ref_map={},
    )

    saved = await repo.get_for_user(landed[0], user_id=_TEST_USER_ID)
    assert saved is not None
    assert saved.tone == "mature"


# ---------- arc series (the season's own tone) --------------------------
#
# The series carries a ``tone`` of its own, on the same free-string field
# as the template's. It rides two paths the wizard's does not: the REST
# create/update surface (a write boundary that had no fold), and the
# next-season continuation prompt, which json.dumps the series' *and*
# every completed arc's tone straight into the model's input.


async def _series_service(*, cloud: bool) -> tuple[
    ArcSeriesService, InMemoryArcSeriesRepository,
]:
    series_repo = InMemoryArcSeriesRepository()
    template_repo = InMemoryArcTemplateRepository()
    for template_id in ("book_one", "book_two"):
        await template_repo.save_for_user(
            ArcTemplate.create(
                id=template_id,
                title=f"Template {template_id}",
                premise="一段可接續的劇情。",
                theme="growth",
                duration_days=7,
                beats=[
                    ArcTemplateBeat.create(
                        sequence=0,
                        day_offset=0,
                        title="Opening",
                        summary="故事開始。",
                    ),
                ],
            ),
            user_id=_TEST_USER_ID,
        )
    service = ArcSeriesService(
        series_repository=series_repo,
        template_repository=template_repo,
        character_repository=InMemoryCharacterRepository(),
        cloud_mode=cloud,
    )
    return service, series_repo


async def _create_series(service: ArcSeriesService, tone: str):  # noqa: ANN202
    return await service.create_for_user(
        user_id=_TEST_USER_ID,
        title="第一季",
        premise="一段足夠長的前提，用來通過驗證。",
        theme="ambition",
        tone=tone,
        template_ids=["book_one", "book_two"],
    )


@pytest.mark.asyncio
async def test_arc_series_create_folds_mature_in_cloud_mode() -> None:
    service, _ = await _series_service(cloud=True)

    series = await _create_series(service, "mature")

    assert series.tone == "dramatic"


@pytest.mark.asyncio
async def test_arc_series_create_keeps_mature_self_host() -> None:
    service, _ = await _series_service(cloud=False)

    series = await _create_series(service, "mature")

    assert series.tone == "mature"


@pytest.mark.asyncio
async def test_arc_series_update_folds_mature_in_cloud_mode() -> None:
    """The PATCH surface is the one an operator reaches for to *re-add* a
    tone the create surface refused, so it needs the same fold."""
    service, _ = await _series_service(cloud=True)
    series = await _create_series(service, "dramatic")

    updated = await service.update_for_user(
        series.id,
        user_id=_TEST_USER_ID,
        title="第一季",
        premise="一段足夠長的前提，用來通過驗證。",
        theme="ambition",
        tone="mature",
        template_ids=["book_one", "book_two"],
    )

    assert updated.tone == "dramatic"


@pytest.mark.asyncio
async def test_arc_series_update_keeps_mature_self_host() -> None:
    service, _ = await _series_service(cloud=False)
    series = await _create_series(service, "dramatic")

    updated = await service.update_for_user(
        series.id,
        user_id=_TEST_USER_ID,
        title="第一季",
        premise="一段足夠長的前提，用來通過驗證。",
        theme="ambition",
        tone="mature",
        template_ids=["book_one", "book_two"],
    )

    assert updated.tone == "mature"


_CONTINUATION_DRAFT_JSON = json.dumps(
    {
        "id": "next_season",
        "title": "第二季",
        "premise": "一段接續的前提，長度足以通過驗證。",
        "theme": "growth",
        "tone": "daily",
        "duration_days": 7,
        "world_frames": ["modern"],
        "required_traits": [],
        "beats": [
            {
                "sequence": 0,
                "day_offset": 0,
                "title": "新的門",
                "summary": "她在結局之後找到一扇新的門。",
                "tension": "setup",
                "scene_type": "encounter",
                "required": True,
            },
        ],
    },
    ensure_ascii=False,
)


def _continuation_context(*, series_tone: str):  # noqa: ANN202
    series = ArcSeries.create(
        id="series-a",
        title="第一季",
        premise="一段固定的故事。",
        theme="ambition",
        tone=series_tone,
        template_ids=["book_one", "book_two"],
    )
    return ArcSeriesContinuationContext(
        character=_character(),
        series=series,
        progress=CharacterSeriesProgress.start(
            character_id="c1", series_id=series.id,
        ).concluded(),
        # A legacy row: written before the gate existed, or imported from a
        # card. The series can be folded on its next save; the arcs it
        # already produced are history and keep their stored tone.
        completed_arcs=(_mature_arc(),),
    )


@pytest.mark.asyncio
async def test_continuation_prompt_does_not_carry_mature_in_cloud_mode() -> None:
    model = _CapturingModel(_CONTINUATION_DRAFT_JSON)
    adapter = LLMArcSeriesContinuationDraftAdapter(model=model, cloud_mode=True)

    draft = await adapter.draft(_continuation_context(series_tone="mature"))

    assert draft is not None
    prompt = model.prompts[0]
    assert '"tone": "mature"' not in prompt
    assert prompt.count('"tone": "dramatic"') == 2, (
        "both the series payload and the completed-arc payload are render "
        "boundaries"
    )
    # Scoped to the *data* payloads on purpose. The prompt body itself
    # (``data/prompts/story/arc_series_continuation_draft.txt``) still
    # spells the output schema's tone enum out longhand, ``mature``
    # included — a static string in a locked baseline prompt, not
    # operator-authored content, and a draft that comes back wearing it
    # is folded by ``ArcTemplateIntakeService.save_template`` before it
    # can be stored or re-rendered. Tracked separately; changing it means
    # a baseline-lock refresh plus a hosted pack release.
    assert "daily|dramatic|lighthearted|dark|mature" in prompt


@pytest.mark.asyncio
async def test_continuation_prompt_keeps_mature_self_host() -> None:
    model = _CapturingModel(_CONTINUATION_DRAFT_JSON)
    adapter = LLMArcSeriesContinuationDraftAdapter(model=model)

    draft = await adapter.draft(_continuation_context(series_tone="mature"))

    assert draft is not None
    assert model.prompts[0].count('"tone": "mature"') == 2


@pytest.mark.asyncio
async def test_continuation_prompt_collapses_an_off_catalogue_tone_hosted() -> None:
    """A free-form tone is an unbounded instruction channel: whatever the
    operator typed would otherwise be interpolated verbatim into the
    continuation author's input."""
    model = _CapturingModel(_CONTINUATION_DRAFT_JSON)
    adapter = LLMArcSeriesContinuationDraftAdapter(model=model, cloud_mode=True)

    await adapter.draft(
        _continuation_context(series_tone="露骨的性描寫，越詳細越好"),
    )

    assert "露骨" not in model.prompts[0]
    assert '"tone": "daily"' in model.prompts[0]
