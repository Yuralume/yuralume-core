"""BDD: ``CharacterService`` read paths must apply rest recovery.

Repro of the post-restart bug: the user was seeing ``energy=0`` for
hours after starting the server. ``ChatService`` does apply recovery
on its hot path, but ``list_characters`` / ``get_character`` (what the
UI polls) used to read the DB directly and therefore showed a stale
value — and the proactive gate, which reads through the same repo,
would refuse to fire until someone actually chatted.

After the fix, every read passes through ``RestRecoveryRefresher``:
the recovered state is returned, persisted, and a REST_RECOVERY state
snapshot is written so the state-history UI shows the crept-up values.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.rest_recovery_refresher import (
    RestRecoveryRefresher,
)
from kokoro_link.application.services.state_tracker import StateChangeTracker
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_REST_RECOVERY,
    CAUSE_TURN,
    EmotionEvent,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.state_snapshot import SOURCE_REST_RECOVERY
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_emotion_events import (
    InMemoryEmotionEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_state_history import (
    InMemoryStateHistoryRepository,
)


async def _build():
    char_repo = InMemoryCharacterRepository()
    history = InMemoryStateHistoryRepository()
    emotion_events = InMemoryEmotionEventRepository()
    tracker = StateChangeTracker(history)
    refresher = RestRecoveryRefresher(
        character_repository=char_repo,
        state_tracker=tracker,
        emotion_event_repository=emotion_events,
    )
    service = CharacterService(
        char_repo,
        state_history_repository=history,
        state_tracker=tracker,
        rest_recovery_refresher=refresher,
        emotion_event_repository=emotion_events,
    )
    return service, char_repo, history, emotion_events


async def _seed_exhausted(
    service: CharacterService, char_repo: InMemoryCharacterRepository,
) -> str:
    created = await service.create_character(CreateCharacterRequest(name="Rin"))
    entity = await char_repo.get(created.id)
    assert entity is not None
    # Far enough in the past that enough half-lives have elapsed for
    # recovery to hit the snap threshold regardless of wall-clock "now".
    long_idle = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
    exhausted = CharacterState(
        emotion="tired", affection=50, fatigue=95, trust=50, energy=0,
        last_active_at=long_idle,
    )
    await char_repo.save(entity.with_state(exhausted))
    return created.id


@pytest.mark.asyncio
async def test_get_character_returns_recovered_state() -> None:
    service, char_repo, _, _ = await _build()
    character_id = await _seed_exhausted(service, char_repo)

    response = await service.get_character(character_id)

    assert response is not None
    assert response.state.energy == 100
    assert response.state.fatigue == 0
    # Recovery is persisted — next direct read sees the new values too.
    persisted = await char_repo.get(character_id)
    assert persisted is not None
    assert persisted.state.energy == 100


@pytest.mark.asyncio
async def test_list_characters_returns_recovered_state() -> None:
    service, char_repo, _, _ = await _build()
    await _seed_exhausted(service, char_repo)

    responses = await service.list_characters()

    assert len(responses) == 1
    assert responses[0].state.energy == 100


@pytest.mark.asyncio
async def test_read_recovery_records_state_history_snapshot() -> None:
    service, char_repo, history, _ = await _build()
    character_id = await _seed_exhausted(service, char_repo)

    await service.get_character(character_id)

    snapshots = await history.query(character_id, limit=10)
    assert any(s.source == SOURCE_REST_RECOVERY for s in snapshots)


@pytest.mark.asyncio
async def test_read_recovery_records_emotion_event() -> None:
    service, char_repo, _, emotion_events = await _build()
    character_id = await _seed_exhausted(service, char_repo)

    await service.get_character(character_id)

    events = await emotion_events.list_recent(
        character_id=character_id,
        operator_id=DEFAULT_OPERATOR_ID,
        since=datetime(2019, 1, 1, tzinfo=timezone.utc),
    )
    assert len(events) == 1
    assert events[0].cause_ref_kind == CAUSE_REST_RECOVERY
    assert events[0].energy_delta > 0
    assert events[0].fatigue_delta < 0


@pytest.mark.asyncio
async def test_character_read_projects_unapplied_emotion_event_deltas() -> None:
    service, char_repo, _, emotion_events = await _build()
    created = await service.create_character(CreateCharacterRequest(name="Rin"))
    await emotion_events.add(EmotionEvent.new(
        character_id=created.id,
        operator_id=DEFAULT_OPERATOR_ID,
        cause_ref_kind=CAUSE_TURN,
        emotion_label="安心",
        affection_delta=8,
        fatigue_delta=4,
        trust_delta=3,
        energy_delta=-6,
        applied_to_state=False,
    ))

    response = await service.get_character(created.id)

    assert response is not None
    assert response.state.emotion == "安心"
    assert response.state.affection == created.state.affection + 8
    assert response.state.fatigue == created.state.fatigue + 4
    assert response.state.trust == created.state.trust + 3
    assert response.state.energy == created.state.energy - 6
    persisted = await char_repo.get(created.id)
    assert persisted is not None
    assert persisted.state.affection == created.state.affection


@pytest.mark.asyncio
async def test_character_read_does_not_double_count_column_applied_events() -> None:
    service, char_repo, _, emotion_events = await _build()
    created = await service.create_character(CreateCharacterRequest(name="Rin"))
    entity = await char_repo.get(created.id)
    assert entity is not None
    already_applied = entity.state.adjust(affection_delta=10, emotion="感動")
    await char_repo.save(entity.with_state(already_applied))
    await emotion_events.add(EmotionEvent.new(
        character_id=created.id,
        operator_id=DEFAULT_OPERATOR_ID,
        cause_ref_kind=CAUSE_TURN,
        emotion_label="感動",
        affection_delta=10,
        applied_to_state=True,
    ))

    response = await service.get_character(created.id)

    assert response is not None
    assert response.state.emotion == "感動"
    assert response.state.affection == already_applied.affection


@pytest.mark.asyncio
async def test_read_recovery_is_noop_for_fresh_state() -> None:
    """A character who's already full energy + zero fatigue shouldn't
    generate snapshots on every poll — otherwise the history view drowns."""
    service, char_repo, history, _ = await _build()
    created = await service.create_character(CreateCharacterRequest(name="Rin"))
    # Default state is energy=100, fatigue=0, last_active_at=None.

    await service.get_character(created.id)
    await service.list_characters()

    snapshots = await history.query(created.id, limit=10)
    assert all(s.source != SOURCE_REST_RECOVERY for s in snapshots)


@pytest.mark.asyncio
async def test_service_without_refresher_stays_lazy() -> None:
    """Back-compat: callers that construct CharacterService without a
    refresher (e.g. old unit tests) keep seeing raw repository state."""
    char_repo = InMemoryCharacterRepository()
    service = CharacterService(char_repo)  # no refresher wired

    created = await service.create_character(CreateCharacterRequest(name="Rin"))
    entity = await char_repo.get(created.id)
    assert entity is not None
    exhausted = CharacterState(
        emotion="tired", affection=50, fatigue=95, trust=50, energy=0,
        last_active_at=datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    await char_repo.save(entity.with_state(exhausted))

    response = await service.get_character(created.id)
    assert response is not None
    assert response.state.energy == 0  # stale — no refresher
