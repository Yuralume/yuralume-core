from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from typing import Sequence

import pytest

from kokoro_link.application.services.feature_keys import FEATURE_MEMOIR_LOCALIZE
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.memoir import (
    ENTRY_EMOTION,
    ENTRY_MEMORY,
    MemoirChapter,
    MemoirEntry,
    MemoirView,
)
from kokoro_link.infrastructure.memoir.llm_localizer import (
    LLMMemoirLocalizer,
    NullMemoirLocalizer,
)


class _ScriptedModel(ChatModelPort):
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str | None]] = []

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.calls.append((prompt, model))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        yield await self.generate(prompt, image_urls=image_urls, model=model)

    async def list_models(self) -> list[str]:
        return ["scripted-model"]


class _ActiveProvider:
    def __init__(self, model: _ScriptedModel, *, fake: bool = False) -> None:
        self.model = model
        self.fake = fake
        self.resolve_calls: list[str | None] = []
        self.model_id_calls: list[str | None] = []
        self.fake_calls: list[str | None] = []

    async def resolve(self, feature_key=None, *, character=None):
        self.resolve_calls.append(feature_key)
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        self.model_id_calls.append(feature_key)
        return "scripted-model"

    async def is_fake(self, feature_key=None, *, character=None):
        self.fake_calls.append(feature_key)
        return self.fake


def _view() -> MemoirView:
    return MemoirView(
        chapters=(
            MemoirChapter(
                period="week",
                period_start=date(2026, 5, 18),
                period_end=date(2026, 5, 25),
                narrative="這週想起了咖啡店那天。",
                dominant_themes=("咖啡",),
                evidence_quotes=("我今天很開心",),
            ),
        ),
        timeline=(
            MemoirEntry(
                kind=ENTRY_MEMORY,
                entry_id="m1",
                occurred_at=datetime(2026, 5, 25, tzinfo=timezone.utc),
                summary="一起聊了咖啡。",
                score=0.9,
                pinned=True,
                extras={
                    "memory_kind": "episodic",
                    "tags": "咖啡,約定",
                },
            ),
            MemoirEntry(
                kind=ENTRY_EMOTION,
                entry_id="e1",
                occurred_at=datetime(2026, 5, 24, tzinfo=timezone.utc),
                summary="被理解了",
                score=0.8,
                pinned=False,
                extras={
                    "cause_ref_kind": "turn",
                    "valence": "0.70",
                    "emotion_label": "被理解了",
                },
            ),
        ),
        pin_count=1,
        pin_limit=32,
    )


@pytest.mark.asyncio
async def test_llm_memoir_localizer_translates_visible_fields_only() -> None:
    response = json.dumps(
        {
            "chapters": [
                {
                    "index": 0,
                    "narrative": "This week I remembered that day at the cafe.",
                    "dominant_themes": ["coffee"],
                    "evidence_quotes": ["I was really happy today"],
                },
            ],
            "timeline": [
                {
                    "index": 0,
                    "summary": "We talked about coffee.",
                    "extras": {"tags": "coffee,promise"},
                },
                {
                    "index": 1,
                    "summary": "I felt understood.",
                    "extras": {"emotion_label": "understood"},
                },
            ],
        },
        ensure_ascii=False,
    )
    model = _ScriptedModel(response)
    provider = _ActiveProvider(model)
    localizer = LLMMemoirLocalizer(
        provider=provider,
        feature_key=FEATURE_MEMOIR_LOCALIZE,
    )

    result = await localizer.localize_view(_view(), target_language="en-US")

    assert result.chapters[0].narrative == (
        "This week I remembered that day at the cafe."
    )
    assert result.chapters[0].dominant_themes == ("coffee",)
    assert result.chapters[0].evidence_quotes == (
        "I was really happy today",
    )
    assert result.timeline[0].entry_id == "m1"
    assert result.timeline[0].summary == "We talked about coffee."
    assert result.timeline[0].extras["tags"] == "coffee,promise"
    assert result.timeline[0].extras["memory_kind"] == "episodic"
    assert result.timeline[0].pinned is True
    assert result.timeline[1].extras["emotion_label"] == "understood"
    assert result.timeline[1].extras["cause_ref_kind"] == "turn"
    assert result.timeline[1].extras["valence"] == "0.70"
    assert result.pin_count == 1
    assert result.pin_limit == 32
    assert provider.fake_calls == [FEATURE_MEMOIR_LOCALIZE]
    assert provider.resolve_calls == [FEATURE_MEMOIR_LOCALIZE]
    assert provider.model_id_calls == [FEATURE_MEMOIR_LOCALIZE]
    assert model.calls[0][1] == "scripted-model"


@pytest.mark.asyncio
async def test_llm_memoir_localizer_keeps_original_on_model_error() -> None:
    view = _view()
    localizer = LLMMemoirLocalizer(
        model=_ScriptedModel(RuntimeError("boom")),
    )

    result = await localizer.localize_view(view, target_language="en-US")

    assert result == view


@pytest.mark.asyncio
async def test_llm_memoir_localizer_fake_provider_returns_original() -> None:
    view = _view()
    model = _ScriptedModel('{"chapters":[],"timeline":[]}')
    localizer = LLMMemoirLocalizer(
        provider=_ActiveProvider(model, fake=True),
        feature_key=FEATURE_MEMOIR_LOCALIZE,
    )

    result = await localizer.localize_view(view, target_language="en-US")

    assert result == view
    assert model.calls == []


@pytest.mark.asyncio
async def test_null_memoir_localizer_is_noop() -> None:
    view = _view()
    localizer = NullMemoirLocalizer()

    result = await localizer.localize_view(view, target_language="en-US")

    assert result == view
