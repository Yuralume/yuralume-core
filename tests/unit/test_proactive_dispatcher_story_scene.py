"""Proactive delivery pauses while a 起幕 scene is running (SC1-E).

STORY_SCENE_PLAN §3.2: a character cannot be inside a framed scene and
also messaging the player from outside it. The pause is *only* a pause —
nothing about it is remembered, so the tick after the scene closes
evaluates exactly as it would have if no scene had ever happened.

These are characterization tests as much as feature tests: the point is
that a character with no scene (every deployment that never wires the
repository, and every character who never pressed 起幕) reaches the
decider on the identical path it did before.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_TIMEOUT,
    SCENE_LAYER_BEAT,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)
from tests.unit._messaging_harness import build_messaging_harness, create_character

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 18, 14, 30, tzinfo=timezone.utc)


class _FixedDecider(ProactiveDeciderPort):
    def __init__(self) -> None:
        self.calls: list[ProactiveContext] = []

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.calls.append(context)
        return ProactiveDecision(True, "ok", "在幹嘛？")


class _ExplodingSessions(InMemoryStorySceneSessionRepository):
    async def get_open_for_character(self, character_id: str):  # noqa: ANN201
        raise RuntimeError("session table unreadable")


async def _character(harness):  # noqa: ANN001
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=None,
        ),
        proactive_enabled=True,
        accepts_web_proactive=True,
    )
    await harness.character_repository.save(updated)
    return updated


async def _seed_repository(character_id: str):  # noqa: ANN201
    """Permission to speak first, so a clean run reaches the decider."""
    repository = InMemoryCharacterOperatorRelationshipSeedRepository()
    await repository.save(
        CharacterOperatorRelationshipSeed(
            character_id=character_id,
            operator_id=DEFAULT_OPERATOR_ID,
            relationship_label="剛認識",
            proactive_permission=True,
            proactive_cadence_hint="一天最多一次，下午較好",
        ),
    )
    return repository


def _dispatcher(harness, *, decider, seeds, sessions=None):  # noqa: ANN001
    return ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        relationship_seed_repository=seeds,
        story_scene_sessions=sessions,
    )


async def _open_scene(sessions, character_id: str) -> StorySceneSession:
    session = StorySceneSession.open_scene(
        character_id=character_id,
        conversation_id="conv-1",
        source_layer=SCENE_LAYER_BEAT,
        beat_id="b1",
        title="頂樓的那次相遇",
        opened_at=NOW - timedelta(minutes=10),
    )
    await sessions.add(session)
    return session


async def test_a_character_in_a_scene_does_not_message_from_outside_it() -> None:
    harness = build_messaging_harness()
    character = await _character(harness)
    sessions = InMemoryStorySceneSessionRepository()
    await _open_scene(sessions, character.id)
    decider = _FixedDecider()
    dispatcher = _dispatcher(
        harness,
        decider=decider,
        seeds=await _seed_repository(character.id),
        sessions=sessions,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=NOW,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert attempt.reason == "story scene in progress"
    # Cheap band: no model call, and nothing was delivered anywhere.
    assert decider.calls == []
    assert harness.telegram_adapter.sent == []


async def test_delivery_resumes_the_moment_the_scene_closes() -> None:
    """Nothing about the pause survives the close — no cooldown, no flag."""
    harness = build_messaging_harness()
    character = await _character(harness)
    sessions = InMemoryStorySceneSessionRepository()
    session = await _open_scene(sessions, character.id)
    decider = _FixedDecider()
    dispatcher = _dispatcher(
        harness,
        decider=decider,
        seeds=await _seed_repository(character.id),
        sessions=sessions,
    )

    blocked = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=NOW,
    )
    assert blocked.outcome == ProactiveOutcome.GATE_BLOCKED

    await sessions.close(session.id, reason=SCENE_CLOSE_TIMEOUT, at=NOW)

    resumed = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        # A minute later: the blocked attempt must not have consumed the
        # cooldown budget, which only advances on attempts that passed the gate.
        now=NOW + timedelta(minutes=1),
    )
    assert resumed.outcome == ProactiveOutcome.SENT
    assert len(decider.calls) == 1


async def test_a_character_not_in_a_scene_is_evaluated_exactly_as_before() -> None:
    harness = build_messaging_harness()
    character = await _character(harness)
    decider = _FixedDecider()
    dispatcher = _dispatcher(
        harness,
        decider=decider,
        seeds=await _seed_repository(character.id),
        sessions=InMemoryStorySceneSessionRepository(),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert len(decider.calls) == 1


async def test_an_unwired_session_repository_changes_nothing() -> None:
    """Every deployment that predates SC1-E keeps the path it had."""
    harness = build_messaging_harness()
    character = await _character(harness)
    decider = _FixedDecider()
    dispatcher = _dispatcher(
        harness,
        decider=decider,
        seeds=await _seed_repository(character.id),
        sessions=None,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT


async def test_an_unreadable_session_table_fails_open() -> None:
    """The pause is narrative polish, not a wall.

    Failing closed here would silence every proactive message on the
    deployment for as long as the read kept failing; failing open costs at
    most one message that lands during a scene."""
    harness = build_messaging_harness()
    character = await _character(harness)
    decider = _FixedDecider()
    dispatcher = _dispatcher(
        harness,
        decider=decider,
        seeds=await _seed_repository(character.id),
        sessions=_ExplodingSessions(),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT


async def test_a_pre_composed_release_is_not_blocked_by_a_scene() -> None:
    """A busy-defer / scheduled-promise release is a message the player is
    already owed, and its caller turns any non-``sent`` outcome into a
    terminal ``failed`` row. Blocking here would not postpone the message,
    it would delete it — a broken promise traded for a framing break.
    Deferring instead of dropping belongs in the follow-up dispatcher's own
    release decision, where "not now" already has a meaning."""
    harness = build_messaging_harness()
    character = await _character(harness)
    sessions = InMemoryStorySceneSessionRepository()
    await _open_scene(sessions, character.id)
    dispatcher = _dispatcher(
        harness,
        decider=_FixedDecider(),
        seeds=await _seed_repository(character.id),
        sessions=sessions,
    )

    attempt = await dispatcher.deliver_pre_composed(
        character_id=character.id,
        text="剛剛在忙，你說的那件事我查到了。",
        trigger=ProactiveTrigger.SCHEDULED_PROMISE,
        now=NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
