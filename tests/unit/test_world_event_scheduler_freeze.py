"""World-event curation must skip frozen characters (CHARACTER_FREEZE_PLAN).

The curate pass is a second background per-character loop, independent
of ProactiveScheduler. A frozen character must not incur its
embedding-cost curation — this locks that guard so a future refactor of
``_safe_curate`` can't silently re-introduce the cost leak.
"""

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.rss_ingestion_service import IngestionReport
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


class _ReportingIngestion:
    async def ingest_all(self) -> IngestionReport:
        return IngestionReport(
            sources_attempted=4,
            sources_succeeded=3,
            events_persisted=2,
            events_skipped_dedup=1,
            events_skipped_embed=1,
            errors=("source: timeout",),
            events_failed_persist=2,
            embed_batches_failed=3,
        )

    async def gc(self) -> int:
        return 0


class _CountingIngestion:
    def __init__(self) -> None:
        self.ingest_calls = 0
        self.gc_calls = 0

    async def ingest_all(self) -> IngestionReport:
        self.ingest_calls += 1
        return IngestionReport()

    async def gc(self) -> int:
        self.gc_calls += 1
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


@pytest.mark.asyncio
async def test_safe_ingest_logs_additive_failure_counters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = WorldEventScheduler(
        ingestion_service=_ReportingIngestion(),  # type: ignore[arg-type]
        curator_service=object(),  # type: ignore[arg-type]
        character_repository=object(),  # type: ignore[arg-type]
    )

    with caplog.at_level("INFO"):
        await scheduler._safe_ingest()

    assert "persist_failed=2" in caplog.text
    assert "embed_batches_failed=3" in caplog.text


@pytest.mark.asyncio
async def test_scheduled_passes_fail_closed_without_live_leadership() -> None:
    repo = InMemoryCharacterRepository()
    await repo.save(_character("Active"))
    curator = _RecordingCurator()
    ingestion = _CountingIngestion()
    scheduler = WorldEventScheduler(
        ingestion_service=ingestion,  # type: ignore[arg-type]
        curator_service=curator,  # type: ignore[arg-type]
        character_repository=repo,
        leadership_guard=_false,
    )

    await scheduler._safe_ingest()
    await scheduler._safe_curate()

    assert ingestion.ingest_calls == 0
    assert ingestion.gc_calls == 0
    assert curator.curated == []


async def _false() -> bool:
    return False
