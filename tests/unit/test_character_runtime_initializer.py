import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_runtime_initializer import (
    CharacterRuntimeInitializer,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


class _RecordingScheduleService:
    def __init__(self, *, crash: bool = False) -> None:
        self.calls: list[str] = []
        self._crash = crash

    async def ensure_window(self, character):
        self.calls.append(character.id)
        if self._crash:
            raise RuntimeError("planner unavailable")
        return [object(), object(), object()]


class _RecordingStoryArcService:
    def __init__(self, *, crash: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._crash = crash

    async def ensure_active_arc(
        self,
        character,
        *,
        auto_start=True,
        open_new_season=True,
    ):
        self.calls.append(
            {
                "character_id": character.id,
                "auto_start": auto_start,
                "open_new_season": open_new_season,
            }
        )
        if self._crash:
            raise RuntimeError("arc planner unavailable")
        return object()


class _RecordingStoryEventService:
    def __init__(self, *, crash: bool = False) -> None:
        self.calls: list[str] = []
        self._crash = crash

    async def ensure_today(self, character):
        self.calls.append(character.id)
        if self._crash:
            raise RuntimeError("story expander unavailable")
        return type("Report", (), {"newly_rolled": 1})()


@pytest.mark.asyncio
async def test_prepare_after_create_pre_generates_runtime_context() -> None:
    character_repository = InMemoryCharacterRepository()
    character_service = CharacterService(character_repository)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi"),
    )
    schedule_service = _RecordingScheduleService()
    story_arc_service = _RecordingStoryArcService()
    story_event_service = _RecordingStoryEventService()
    initializer = CharacterRuntimeInitializer(
        character_service=character_service,
        schedule_service=schedule_service,  # type: ignore[arg-type]
        story_arc_service=story_arc_service,  # type: ignore[arg-type]
        story_event_service=story_event_service,  # type: ignore[arg-type]
    )

    result = await initializer.prepare_after_create(created.id)

    assert result.character_id == created.id
    assert result.schedule_days_prepared == 3
    assert result.story_arc_prepared is True
    assert result.story_events_prepared == 1
    assert schedule_service.calls == [created.id]
    assert story_arc_service.calls == [
        {
            "character_id": created.id,
            "auto_start": True,
            "open_new_season": False,
        }
    ]
    assert story_event_service.calls == [created.id]


@pytest.mark.asyncio
async def test_prepare_after_create_ignores_missing_character() -> None:
    character_service = CharacterService(InMemoryCharacterRepository())
    schedule_service = _RecordingScheduleService()
    initializer = CharacterRuntimeInitializer(
        character_service=character_service,
        schedule_service=schedule_service,  # type: ignore[arg-type]
        story_arc_service=_RecordingStoryArcService(),  # type: ignore[arg-type]
        story_event_service=_RecordingStoryEventService(),  # type: ignore[arg-type]
    )

    result = await initializer.prepare_after_create("missing")

    assert result.character_id == "missing"
    assert result.schedule_days_prepared == 0
    assert result.story_arc_prepared is False
    assert result.story_events_prepared == 0
    assert schedule_service.calls == []


@pytest.mark.asyncio
async def test_prepare_after_create_is_fail_soft_per_warmup_step() -> None:
    character_repository = InMemoryCharacterRepository()
    character_service = CharacterService(character_repository)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi"),
    )
    story_arc_service = _RecordingStoryArcService(crash=True)
    story_event_service = _RecordingStoryEventService()
    initializer = CharacterRuntimeInitializer(
        character_service=character_service,
        schedule_service=_RecordingScheduleService(crash=True),  # type: ignore[arg-type]
        story_arc_service=story_arc_service,  # type: ignore[arg-type]
        story_event_service=story_event_service,  # type: ignore[arg-type]
    )

    result = await initializer.prepare_after_create(created.id)

    assert result.character_id == created.id
    assert result.schedule_days_prepared == 0
    assert result.story_arc_prepared is False
    assert result.story_events_prepared == 1
    assert story_arc_service.calls == [
        {
            "character_id": created.id,
            "auto_start": True,
            "open_new_season": False,
        }
    ]
    assert story_event_service.calls == [created.id]
