"""World-event curation must skip frozen characters (CHARACTER_FREEZE_PLAN).

The curate pass is a second background per-character loop, independent
of ProactiveScheduler. A frozen character must not incur its
embedding-cost curation — this locks that guard so a future refactor of
``_safe_curate`` can't silently re-introduce the cost leak.
"""

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.world_event_scheduler import (
    WorldEventScheduler,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


class _RecordingCurator:
    def __init__(self) -> None:
        self.curated: list[str] = []

    async def curate(self, character) -> int:  # noqa: ANN001
        self.curated.append(character.id)
        return 0


def _character(name: str) -> Character:
    return Character.create(
        name=name,
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


@pytest.mark.asyncio
async def test_safe_curate_skips_frozen_characters() -> None:
    repo = InMemoryCharacterRepository()
    active = _character("Active")
    dormant = _character("Dormant")
    await repo.save(active)
    await repo.save(dormant)
    await repo.set_frozen(
        dormant.id, frozen=True,
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
    )

    curator = _RecordingCurator()
    scheduler = WorldEventScheduler(
        ingestion_service=object(),  # type: ignore[arg-type]  # unused by curate pass
        curator_service=curator,  # type: ignore[arg-type]
        character_repository=repo,
    )

    await scheduler._safe_curate()

    assert active.id in curator.curated
    assert dormant.id not in curator.curated
