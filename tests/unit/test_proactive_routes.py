"""Route tests for proactive attempts log + evaluate-now endpoint."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.proactive import router as proactive_router
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.proactive import ProactiveDecision
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.proactive.null_decider import NullProactiveDecider
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    build_service_container,
    create_character,
    create_telegram_account,
)


def _client(container) -> TestClient:
    app = FastAPI()
    app.state.container = container
    app.include_router(proactive_router, prefix="/api/v1")
    return TestClient(app)


async def _build_wired_container(
    *, decision: ProactiveDecision | None = None,
) -> tuple[object, object]:
    harness = build_messaging_harness()
    container = build_service_container(harness)
    attempts = InMemoryProactiveAttemptRepository()
    container.proactive_attempt_repository = attempts

    decider = (
        NullProactiveDecider() if decision is None
        else _StaticDecider(decision)
    )
    container.proactive_dispatcher = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=attempts,
        gate=HeuristicProactiveGate(local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
    )
    return harness, container


class _StaticDecider:
    def __init__(self, decision: ProactiveDecision) -> None:
        self._decision = decision

    async def decide(self, context):  # noqa: ANN001
        return self._decision


@pytest.mark.asyncio
async def test_list_attempts_returns_404_for_unknown_character() -> None:
    _, container = await _build_wired_container()
    client = _client(container)
    response = client.get("/api/v1/characters/ghost/proactive/attempts")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_attempts_returns_log_newest_first() -> None:
    harness, container = await _build_wired_container()
    character = await create_character(harness)
    attempts = container.proactive_attempt_repository
    base = datetime.now(tz=timezone.utc)
    for idx in range(3):
        await attempts.add(
            ProactiveAttempt.record(
                character_id=character.id,
                trigger=ProactiveTrigger.TICK,
                outcome=ProactiveOutcome.GATE_BLOCKED,
                reason=f"attempt-{idx}",
                metadata={"persona_curiosity": {"should_ask": idx == 2}},
                now=base + timedelta(seconds=idx),
            ),
        )
    client = _client(container)

    response = client.get(
        f"/api/v1/characters/{character.id}/proactive/attempts",
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["reason"] for row in body] == ["attempt-2", "attempt-1", "attempt-0"]
    assert body[0]["metadata"]["persona_curiosity"]["should_ask"] is True


@pytest.mark.asyncio
async def test_evaluate_now_records_attempt() -> None:
    harness, container = await _build_wired_container()
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    # Enable proactive and configure a binding that accepts it so the
    # pipeline runs the full gate → decider (null) → skipped flow.
    enabled = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
        ),
        aspirations=None, appearance=None, proactive_enabled=True,
    )
    await harness.character_repository.save(enabled)
    account = await create_telegram_account(
        harness, character_id=character.id,
    )
    await harness.binding_repository.save(
        ChannelBinding.create(
            account_id=account.id, chat_ref="c1", accepts_proactive=True,
        ),
    )
    client = _client(container)

    response = client.post(
        f"/api/v1/characters/{dto.id}/proactive/evaluate",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    # null decider always declines
    assert body["attempt"]["outcome"] == "decider_skipped"


@pytest.mark.asyncio
async def test_evaluate_missing_character_returns_404() -> None:
    _, container = await _build_wired_container()
    client = _client(container)

    response = client.post("/api/v1/characters/ghost/proactive/evaluate")
    assert response.status_code == 404
