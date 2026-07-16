from __future__ import annotations

from dataclasses import dataclass

import pytest

from kokoro_link.application.services.arc_template_intake_service import (
    BeatDraft,
    TemplateDraft,
)
from kokoro_link.application.services.fusion_to_arc_service import (
    FusionToArcDraftService,
)
from kokoro_link.contracts.fusion_to_arc import FusionToArcContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import FusionStory
from kokoro_link.domain.value_objects.character_state import CharacterState


def _character(character_id: str, *, user_id: str = "alice") -> Character:
    character = Character.create(
        name=f"Char {character_id}",
        summary="A character summary.",
        personality=[],
        interests=[],
        speaking_style="plain",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
        user_id=user_id,
    )
    object.__setattr__(character, "id", character_id)
    return character


def _story(*, ready: bool = True) -> FusionStory:
    story = FusionStory.create_pending(
        id="fusion-1",
        character_ids=["c-a", "c-b"],
        prompt="A promise is re-opened.",
    )
    if ready:
        return story.with_full_text("A finished fusion story.")
    return story


def _draft() -> TemplateDraft:
    return TemplateDraft(
        id="promise_arc",
        title="Promise Arc",
        premise="A playable arc about trust returning through small scenes.",
        theme="friendship",
        tone="daily",
        duration_days=7,
        beats=(
            BeatDraft(
                sequence=0,
                day_offset=0,
                title="First Step",
                summary="The character chooses whether to ask about the promise.",
            ),
        ),
    )


@dataclass
class _FusionStoryServiceStub:
    story: FusionStory | None

    async def get(self, story_id: str) -> FusionStory | None:
        assert story_id == "fusion-1"
        return self.story


@dataclass
class _CharacterServiceStub:
    characters: dict[str, Character]

    async def get_character_entity(
        self,
        character_id: str,
        *,
        user_id: str | None = None,
    ) -> Character | None:
        character = self.characters.get(character_id)
        if user_id and character and character.user_id != user_id:
            return None
        return character


class _AdapterStub:
    def __init__(self, draft: TemplateDraft | None) -> None:
        self.draft = draft
        self.contexts: list[FusionToArcContext] = []

    async def adapt(self, context: FusionToArcContext) -> TemplateDraft | None:
        self.contexts.append(context)
        return self.draft


def _service(
    *,
    story: FusionStory | None,
    adapter: _AdapterStub | None = None,
) -> tuple[FusionToArcDraftService, _AdapterStub]:
    adapter = adapter or _AdapterStub(_draft())
    service = FusionToArcDraftService(
        fusion_story_service=_FusionStoryServiceStub(story),  # type: ignore[arg-type]
        character_service=_CharacterServiceStub({  # type: ignore[arg-type]
            "c-a": _character("c-a"),
            "c-b": _character("c-b"),
        }),
        adapter=adapter,
    )
    return service, adapter


@pytest.mark.asyncio
async def test_service_adapts_ready_story_without_saving_anything() -> None:
    service, adapter = _service(story=_story())

    draft = await service.adapt(
        "fusion-1",
        user_id="alice",
        operator_primary_language="zh-TW",
        instruction="Make the beats quieter.",
    )

    assert draft == _draft()
    assert len(adapter.contexts) == 1
    assert adapter.contexts[0].story.status == "ready"
    assert [c.id for c in adapter.contexts[0].characters] == ["c-a", "c-b"]
    assert adapter.contexts[0].instruction == "Make the beats quieter."


@pytest.mark.asyncio
async def test_service_rejects_not_ready_story() -> None:
    service, adapter = _service(story=_story(ready=False))

    with pytest.raises(ValueError, match="not ready"):
        await service.adapt("fusion-1", user_id="alice")

    assert adapter.contexts == []


@pytest.mark.asyncio
async def test_service_propagates_fail_soft_none_from_adapter() -> None:
    service, _ = _service(story=_story(), adapter=_AdapterStub(None))

    draft = await service.adapt("fusion-1", user_id="alice")

    assert draft is None
