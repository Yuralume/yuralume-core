from __future__ import annotations

from datetime import datetime

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEMO_ACCOUNT_RUNTIME_PROFILE,
)
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ProactiveTrigger]] = []

    async def evaluate(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        now: datetime | None = None,  # noqa: ARG002
    ) -> None:
        self.calls.append((character_id, trigger))


class _DemoProfileResolver:
    async def resolve_for_operator(self, operator_id: str):
        return DEMO_ACCOUNT_RUNTIME_PROFILE


@pytest.mark.asyncio
async def test_demo_profile_throttles_background_proactive_ticks() -> None:
    repository = InMemoryCharacterRepository()
    service = CharacterService(repository)
    character = await service.create_character(
        CreateCharacterRequest(name="Airi"),
        user_id="cloud:acct",
    )
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=repository,
        startup_grace_seconds=0.0,
        account_runtime_profile_resolver=_DemoProfileResolver(),
    )

    for _ in range(DEMO_ACCOUNT_RUNTIME_PROFILE.proactive_tick_multiplier - 1):
        await scheduler._tick_all()  # noqa: SLF001 - focused scheduler policy.

    assert dispatcher.calls == []

    await scheduler._tick_all()  # noqa: SLF001 - focused scheduler policy.

    assert dispatcher.calls == [(character.id, ProactiveTrigger.TICK)]
