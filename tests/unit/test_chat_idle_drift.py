"""Integration tests for the idle-drift hook in :class:`ChatService`.

The judge port is stubbed so we can assert *contract*: drift gets
folded into ``pending_state`` (and persisted as the post-turn state)
when the absence crosses the threshold, the judge is not called for
short gaps, and an LLM crash doesn't break chat.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.contracts.idle_drift import IdleDrift, IdleDriftPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


UTC = timezone.utc


class _RecordingJudge(IdleDriftPort):
    def __init__(self, drift: IdleDrift) -> None:
        self.drift = drift
        self.calls: list[float] = []

    async def judge(
        self, *, character: Character, idle_minutes: float,
    ) -> IdleDrift:
        self.calls.append(idle_minutes)
        return self.drift


class _CrashingJudge(IdleDriftPort):
    def __init__(self) -> None:
        self.calls = 0

    async def judge(
        self, *, character: Character, idle_minutes: float,
    ) -> IdleDrift:
        self.calls += 1
        raise RuntimeError("backend down")


class _LanguageRecordingJudge(IdleDriftPort):
    """Records the operator language passed to ``judge`` so the test can
    assert ChatService threaded the operator's primary_language through
    (bug B2: the idle-drift current_intent was Chinese for non-Chinese
    operators because this fact was never passed)."""

    def __init__(self, drift: IdleDrift) -> None:
        self.drift = drift
        self.languages: list[str] = []

    async def judge(
        self,
        *,
        character: Character,
        idle_minutes: float,
        operator_primary_language: str = "zh-TW",
    ) -> IdleDrift:
        self.languages.append(operator_primary_language)
        return self.drift


class _EnglishOperatorProfileService:
    async def get_for_user(self, user_id: str):  # noqa: ANN001, ARG002
        from kokoro_link.domain.entities.operator_profile import OperatorProfile

        return OperatorProfile(
            id="default", display_name="Alex", primary_language="en-US",
        )

    async def get_current(self):  # noqa: ANN001
        return await self.get_for_user("default")


class _RecordingPromptBuilder:
    """Captures ``pending_state`` so tests can assert the drift actually
    landed in the data the prompt builder sees — not just the post-turn
    state, which the state engine can mutate further on the assistant
    reply."""

    def __init__(self) -> None:
        self.pending_states: list[CharacterState] = []

    def build(self, **kwargs: object) -> str:
        self.pending_states.append(kwargs["pending_state"])  # type: ignore[arg-type]
        return f"prompt:{kwargs.get('latest_user_message', '')}"


def _build(
    *,
    judge: IdleDriftPort | None,
    threshold: float = 120.0,
    prompt_builder: object | None = None,
    operator_profile_service: object | None = None,
) -> tuple[ChatService, CharacterService, InMemoryCharacterRepository]:
    char_repo = InMemoryCharacterRepository()
    conv_repo = InMemoryConversationRepository()
    mem_repo = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    chat = ChatService(
        character_repository=char_repo,
        conversation_repository=conv_repo,
        memory_repository=mem_repo,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder or DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        idle_drift_judge=judge,
        idle_drift_threshold_minutes=threshold,
        operator_profile_service=operator_profile_service,
    )
    char_svc = CharacterService(
        char_repo, conversation_repository=conv_repo, memory_repository=mem_repo,
    )
    return chat, char_svc, char_repo


async def _seed_state(
    char_repo: InMemoryCharacterRepository,
    character_id: str,
    *,
    minutes_ago: float | None,
    affection: int = 60,
) -> None:
    """Backdate ``last_active_at`` and seed a non-zero affection so
    negative drift deltas are observable (clamp floor is 0)."""
    character = await char_repo.get(character_id)
    assert character is not None
    state = character.state
    delta = affection - state.affection
    if delta:
        state = state.adjust(affection_delta=delta)
    if minutes_ago is not None:
        state = state.with_active_now(datetime.now(UTC) - timedelta(minutes=minutes_ago))
    await char_repo.save(character.with_state(state))


@pytest.mark.asyncio
async def test_long_idle_triggers_judge_and_drifts_state() -> None:
    """4 hours idle + drift = -3 affection / 鬧彆扭 → prompt sees the
    drifted mood, and the affection delta survives into the persisted
    state. (Emotion may shift again during the assistant reply via the
    state engine's heuristic; that's expected and out of scope here.)"""
    judge = _RecordingJudge(IdleDrift(emotion="鬧彆扭", affection_delta=-3))
    recorder = _RecordingPromptBuilder()
    chat, char_svc, char_repo = _build(judge=judge, prompt_builder=recorder)
    created = await char_svc.create_character(
        CreateCharacterRequest(name="Airi", personality=["傲嬌"], interests=[]),
    )
    await _seed_state(char_repo, created.id, minutes_ago=240, affection=60)  # 4h
    baseline = 60

    reply = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="嗨"),
    )

    # Judge ran exactly once, idle_minutes was passed through correctly.
    assert len(judge.calls) == 1
    assert judge.calls[0] >= 240
    # Prompt builder saw the drifted mood — this is the contract that
    # matters for character behaviour (the LLM reads pending_state).
    assert recorder.pending_states, "prompt builder must have been called"
    drifted = recorder.pending_states[0]
    assert drifted.emotion == "鬧彆扭"
    # Affection delta lands in the persisted state (state engine's
    # on_assistant_reply doesn't touch affection in SimpleStateEngine).
    assert reply.state.affection == baseline - 3


@pytest.mark.asyncio
async def test_short_idle_skips_judge() -> None:
    """Sub-threshold idle = no LLM spend, no drift, normal chat."""
    judge = _RecordingJudge(IdleDrift(emotion="失落", affection_delta=-5))
    chat, char_svc, char_repo = _build(judge=judge, threshold=120.0)
    created = await char_svc.create_character(
        CreateCharacterRequest(name="Airi", personality=["黏人"], interests=[]),
    )
    await _seed_state(char_repo, created.id, minutes_ago=30)  # under 120

    await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="嗨"),
    )

    assert judge.calls == [], "judge must not run when idle < threshold"


@pytest.mark.asyncio
async def test_first_ever_chat_skips_judge() -> None:
    """``last_active_at`` is ``None`` on a freshly-created character →
    idle_minutes is unknown → no drift attempt. Guards against feeding
    a NULL into the LLM."""
    judge = _RecordingJudge(IdleDrift(emotion="失落"))
    chat, char_svc, _ = _build(judge=judge)
    created = await char_svc.create_character(
        CreateCharacterRequest(name="Airi", personality=["焦慮"], interests=[]),
    )

    await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="第一次見面"),
    )

    assert judge.calls == []


@pytest.mark.asyncio
async def test_empty_drift_leaves_state_unchanged() -> None:
    """LLM judged 'nothing notable' → state engine acts alone."""
    judge = _RecordingJudge(IdleDrift())  # empty
    chat, char_svc, char_repo = _build(judge=judge)
    created = await char_svc.create_character(
        CreateCharacterRequest(name="Airi", personality=["冷淡"], interests=[]),
    )
    await _seed_state(char_repo, created.id, minutes_ago=600, affection=60)  # 10h

    reply = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="嗨"),
    )

    # Judge was consulted but returned nothing — affection unchanged
    # by drift; SimpleStateEngine's heuristic doesn't move it on "嗨".
    assert len(judge.calls) == 1
    assert reply.state.affection == 60


@pytest.mark.asyncio
async def test_judge_crash_is_fail_soft() -> None:
    """A flaky judge must never block the chat reply."""
    judge = _CrashingJudge()
    chat, char_svc, char_repo = _build(judge=judge)
    created = await char_svc.create_character(
        CreateCharacterRequest(name="Airi", personality=["傲嬌"], interests=[]),
    )
    await _seed_state(char_repo, created.id, minutes_ago=300)

    reply = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="嗨"),
    )

    assert judge.calls == 1
    assert reply.assistant_message.content  # chat still produced a reply


@pytest.mark.asyncio
async def test_no_judge_wired_is_legacy_passthrough() -> None:
    """Containers that omit the judge (test harnesses, fake-provider
    deployments) must still run end-to-end without errors."""
    chat, char_svc, char_repo = _build(judge=None)
    created = await char_svc.create_character(
        CreateCharacterRequest(name="Airi", personality=["平靜"], interests=[]),
    )
    await _seed_state(char_repo, created.id, minutes_ago=300)

    reply = await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="嗨"),
    )
    assert reply.assistant_message.content


@pytest.mark.asyncio
async def test_operator_language_is_threaded_to_judge() -> None:
    """Bug B2: idle-drift ``current_intent`` is player-visible, so the
    operator's primary_language must reach the judge. With an en-US
    operator wired, the judge sees en-US instead of the zh-TW default."""
    judge = _LanguageRecordingJudge(IdleDrift(emotion="wistful"))
    chat, char_svc, char_repo = _build(
        judge=judge,
        operator_profile_service=_EnglishOperatorProfileService(),
    )
    created = await char_svc.create_character(
        CreateCharacterRequest(name="Airi", personality=["clingy"], interests=[]),
    )
    await _seed_state(char_repo, created.id, minutes_ago=300)

    await chat.send_message(
        SendChatMessageRequest(character_id=created.id, message="hi"),
    )

    assert judge.languages == ["en-US"]
