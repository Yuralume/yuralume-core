"""Materialise-time arc-template localization (SHIPPED_CONTENT_LOCALIZATION).

Verifies the StoryArcService integration:

- an en operator binding a zh-TW template materialises an English arc
  (translator was consulted);
- the same-language operator never triggers a translation;
- no translator wired → authored prose flows through unchanged;
- a fail-soft translator (returns original) lands the authored prose;
- the per-(template_id + lang) cache means a second bind doesn't re-call
  the translator.
"""

from __future__ import annotations

from datetime import date

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.arc_template_translator import (
    ArcTemplateTranslatorPort,
)
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


class _NeverPlanner(StoryArcPlannerPort):
    async def plan_arc(self, *, character, start_date, duration_days=21,
                       beat_count_hint=5, hint=None, recent_dialogue_summary=""):
        raise AssertionError("LLM planner must not run on the template path")


class _StubTemplateRepo(ArcTemplateRepositoryPort):
    def __init__(self, templates: dict[str, ArcTemplate]) -> None:
        self._templates = templates

    async def get_for_user(self, template_id, *, user_id):
        return self._templates.get(template_id)

    async def list_for_user(self, user_id):
        return list(self._templates.values())

    async def list_packs(self):
        return list(self._templates.values())

    async def save_for_user(self, template, *, user_id, overwrite=False):
        raise NotImplementedError

    async def delete_for_user(self, template_id, *, user_id):
        raise NotImplementedError

    async def upsert_pack(self, template, *, pack_id, external_id=None):
        raise NotImplementedError


class _CountingTranslator(ArcTemplateTranslatorPort):
    """Turns the whole template English-ish and counts invocations."""

    def __init__(self) -> None:
        self.calls = 0

    async def translate_template(self, template, *, target_language):
        self.calls += 1
        beats = [
            b.__class__.create(
                sequence=b.sequence, day_offset=b.day_offset,
                title=f"EN::{b.title}", summary=f"EN::{b.summary}",
                tension=b.tension, scene_type=b.scene_type,
                location=(f"EN::{b.location}" if b.location else None),
                scene_characters=[f"EN::{c}" for c in b.scene_characters],
                dramatic_question=(
                    f"EN::{b.dramatic_question}" if b.dramatic_question else None
                ),
                required=b.required,
            )
            for b in template.beats
        ]
        return template.__class__.create(
            id=template.id,
            title=f"EN::{template.title}",
            premise=f"EN::{template.premise}",
            theme=template.theme,
            tone=template.tone,
            language=target_language,
            duration_days=template.duration_days,
            beats=beats,
        ).with_language(target_language)


class _FailSoftTranslator(ArcTemplateTranslatorPort):
    def __init__(self) -> None:
        self.calls = 0

    async def translate_template(self, template, *, target_language):
        self.calls += 1
        return template  # declined / failed


class _OperatorProfileService:
    def __init__(self, language: str) -> None:
        self._language = language

    async def get_for_user(self, user_id):
        return type("Op", (), {"primary_language": self._language})()


def _character(arc_template_id="quiet_breakup") -> Character:
    return Character.create(
        name="Aki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        arc_template_id=arc_template_id,
    )


def _template() -> ArcTemplate:
    return ArcTemplate.create(
        id="quiet_breakup",
        title="沒有吵架的告別",
        premise="一段沒有溫度的關係。",
        theme="loss", tone="dark", language="zh-TW", duration_days=10,
        beats=[
            ArcTemplateBeat.create(
                sequence=0, day_offset=0, title="週日的早餐",
                summary="兩個人一起吃早餐。", location="共同的家",
                scene_characters=["伴侶"], dramatic_question="這算還在一起嗎？",
            ),
        ],
    )


def _service(*, translator, operator_language) -> tuple[StoryArcService, object]:
    arc_repo = InMemoryStoryArcRepository()
    service = StoryArcService(
        repository=arc_repo,
        planner=_NeverPlanner(),
        template_repository=_StubTemplateRepo({"quiet_breakup": _template()}),
        operator_profile_service=_OperatorProfileService(operator_language),
        template_translator=translator,
    )
    return service, arc_repo


@pytest.mark.asyncio
async def test_en_operator_gets_translated_arc() -> None:
    translator = _CountingTranslator()
    service, _ = _service(translator=translator, operator_language="en-US")
    arc = await service.start_new_arc(_character(), today=date(2026, 5, 1))
    assert arc.title == "EN::沒有吵架的告別"
    assert arc.beats[0].title == "EN::週日的早餐"
    assert arc.beats[0].location == "EN::共同的家"
    assert arc.beats[0].scene_characters == ("EN::伴侶",)
    # Structural fields survive the round-trip.
    assert arc.tone == "dark"
    assert arc.beats[0].scene_type == "encounter"
    assert translator.calls == 1


@pytest.mark.asyncio
async def test_same_language_operator_skips_translation() -> None:
    translator = _CountingTranslator()
    service, _ = _service(translator=translator, operator_language="zh-TW")
    arc = await service.start_new_arc(_character(), today=date(2026, 5, 1))
    assert arc.title == "沒有吵架的告別"
    assert translator.calls == 0


@pytest.mark.asyncio
async def test_no_translator_uses_authored_prose() -> None:
    service, _ = _service(translator=None, operator_language="en-US")
    arc = await service.start_new_arc(_character(), today=date(2026, 5, 1))
    assert arc.title == "沒有吵架的告別"


@pytest.mark.asyncio
async def test_failsoft_translator_lands_original() -> None:
    translator = _FailSoftTranslator()
    service, _ = _service(translator=translator, operator_language="ja-JP")
    arc = await service.start_new_arc(_character(), today=date(2026, 5, 1))
    assert arc.title == "沒有吵架的告別"
    assert translator.calls == 1


@pytest.mark.asyncio
async def test_translation_is_cached_per_template_and_lang() -> None:
    translator = _CountingTranslator()
    service, _ = _service(translator=translator, operator_language="en-US")
    await service.start_new_arc(_character(), today=date(2026, 5, 1))
    # Second bind of the same template + same operator language hits the
    # cache — the translator is not called again.
    await service.start_new_arc(_character(), today=date(2026, 6, 1))
    assert translator.calls == 1
