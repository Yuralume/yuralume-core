"""The updater is actually reached — from the real post-turn body.

Wiring a collaborator and never calling it is the failure mode a unit
test of the updater alone cannot see, and DH3's whole cost model rests
on the merge running *here*: on the background post-turn, behind the
player's reply, rather than in front of it.

So this drives ``ChatService._do_post_turn`` itself rather than a
stand-in, and pins the two properties that make the placement correct:
the updater runs, and it runs on the far side of the undo gate.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.undone_turn_gate import UndoneTurnGate
from kokoro_link.contracts.post_turn import PostTurnResult
from kokoro_link.domain.entities.undone_turn import UndoneTurn
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_undone_turns import (
    InMemoryUndoneTurnRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine
from tests.unit.dialogue_checkpoint.builders import (
    OPERATOR_ID,
    character,
    conversation_of,
)

pytestmark = pytest.mark.asyncio


class _QuietProcessor:
    async def process(self, **kwargs):  # noqa: ANN003
        return PostTurnResult(memories=[], state_suggestion=None)


class _StubModel:
    provider_id = "stub"
    supports_vision = False

    async def generate(self, prompt, *, image_urls=(), model=None):  # noqa: ANN001
        return "ok"

    async def list_models(self) -> list[str]:
        return ["stub"]


class _RecordingUpdater:
    def __init__(self) -> None:
        self.runs: list[tuple[str, str]] = []

    async def run(self, *, character, operator_id, now):  # noqa: ANN001
        self.runs.append((character.id, operator_id))
        from kokoro_link.application.services.dialogue_checkpoint import (
            CheckpointUpdateOutcome,
            CheckpointUpdateReport,
        )
        return CheckpointUpdateReport(CheckpointUpdateOutcome.WRITTEN)


def _service(updater, *, undone_turns=None):
    registry = InMemoryChatModelRegistry(default_provider_id="stub")
    registry.register(_StubModel())
    service = ChatService(
        character_repository=InMemoryCharacterRepository(),
        conversation_repository=InMemoryConversationRepository(),
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=_QuietProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        dialogue_checkpoint_updater=updater,
    )
    if undone_turns is not None:
        # The gate is injected after construction in production too
        # (``set_undone_turn_gate``) — the container builds it once and
        # shares it with the undo service.
        service.set_undone_turn_gate(UndoneTurnGate(undone_turns))
    return service


async def _run_post_turn(service, *, turn_record_id="turn-1"):
    return await service._do_post_turn(
        character=character(),
        conversation_id="conv-1",
        turn_record_id=turn_record_id,
        user_text="今天過得如何？",
        assistant_text="還不錯。",
        prior_messages=conversation_of(6),
    )


async def test_the_post_turn_advances_the_checkpoint() -> None:
    updater = _RecordingUpdater()
    await _run_post_turn(_service(updater))
    assert updater.runs == [(character().id, OPERATOR_ID)]


async def test_no_updater_wired_means_the_post_turn_is_unchanged() -> None:
    """Flag off: an absent collaborator, not a boolean on the path."""
    result = await _run_post_turn(_service(None))
    assert "post_turn_error" not in result


async def test_an_undone_turn_never_reaches_the_updater() -> None:
    """The gate exists so a reversed turn cannot write. The checkpoint
    is the one write that could never be taken back, which is why the
    hook sits at the end of the body rather than anywhere above it."""
    undone = InMemoryUndoneTurnRepository()
    updater = _RecordingUpdater()
    service = _service(updater, undone_turns=undone)
    await undone.record(UndoneTurn.new(
        turn_record_id="turn-1", conversation_id="conv-1",
    ))

    result = await _run_post_turn(service)

    assert updater.runs == []
    assert "post_turn_skipped" in result
