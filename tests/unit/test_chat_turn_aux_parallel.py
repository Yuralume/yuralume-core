"""CHAT_AUX_PARALLEL-1 — the per-turn aux context loads in two parallel waves.

The concurrency proofs here are deterministic: no sleeps, no wall-clock
comparisons. Every participant of a wave signs in at a rendezvous and then
blocks until the whole wave has signed in. A serial implementation deadlocks
on the first participant, which ``asyncio.wait_for`` turns into a failure; a
parallel one releases everybody and returns.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import (
    ChatService,
    _with_script_mix_evidence,
)
from kokoro_link.application.services.chat_turn_aux import load_turn_aux_context
from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict
from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigest,
    PromptMaterialDigestContext,
)
from kokoro_link.contracts.register_profile import RegisterProfile, RegisterProfileContext
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

_NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
_TIMEOUT = 5.0

_WAVE_ONE_LOADERS = (
    "recognition",
    "operator_persona",
    "player_persona_note",
    "peer_roster",
    "initial_relationship",
    "emotion_events",
    "self_reflections",
    "phrase_habits",
    "material_digest_read",
    "embedding",
)
"""Ten, including the material-digest read: DIGEST_OFFPATH moved the digest
off the turn's LLM budget entirely — the post-turn produces it and this is
the primary-key SELECT that picks it up, which belongs in wave 1 because
its inputs are ready the moment the turn starts."""

_WAVE_TWO_LOADERS = ("curiosity", "register_profile")
"""Two, not three — the digest left for wave 1 (see above)."""


class _Rendezvous:
    """Release every arrival only once *party* of them have arrived."""

    def __init__(self, party: int) -> None:
        self._party = party
        self._released = asyncio.Event()
        self.arrived: list[str] = []
        self.cancelled: list[str] = []

    async def arrive(self, name: str) -> None:
        self.arrived.append(name)
        if len(self.arrived) >= self._party:
            self._released.set()
        try:
            await self._released.wait()
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise


class _StubLoaders:
    """A ``TurnAuxLoaders`` stand-in with a hook on every awaited step."""

    def __init__(
        self,
        *,
        rendezvous: _Rendezvous | None = None,
        blocking: tuple[str, ...] = (),
        raises: dict[str, Exception] | None = None,
        curiosity_plan: PersonaCuriosityPlan | None = None,
        material_digest: PromptMaterialDigest | None = None,
        register_profile: RegisterProfile | None = None,
    ) -> None:
        self._rendezvous = rendezvous
        self._blocking = set(blocking)
        self._raises = dict(raises or {})
        self.calls: list[str] = []
        self.seen: dict[str, dict] = {}
        self.curiosity_plan = curiosity_plan
        self.material_digest = material_digest
        self.register_profile = register_profile

    async def _step(self, name: str, result):  # noqa: ANN001
        self.calls.append(name)
        boom = self._raises.get(name)
        if boom is not None:
            raise boom
        if self._rendezvous is not None and name in self._blocking:
            await self._rendezvous.arrive(name)
        return result

    async def _build_image_recognition_context(
        self, *, character, main_model, attachment_urls, content_tolerance,  # noqa: ANN001
    ) -> str:
        self.seen["recognition"] = {
            "attachment_urls": tuple(attachment_urls),
            "content_tolerance": content_tolerance,
        }
        return await self._step("recognition", "看到一張照片")

    async def _load_operator_persona(self, character_id, operator):  # noqa: ANN001
        self.seen["operator_persona"] = {"character_id": character_id}
        return await self._step("operator_persona", SimpleNamespace(tag="persona"))

    def _render_operator_persona_lines(self, persona) -> list[str]:  # noqa: ANN001
        self.calls.append("render_persona_lines")
        return [] if persona is None else ["關於對方：喜歡貓"]

    async def _load_player_persona_note(
        self, character_id, operator, *, enabled,  # noqa: ANN001
    ) -> str:
        self.seen["player_persona_note"] = {"enabled": enabled}
        return await self._step("player_persona_note", "我是研究生")

    async def _load_peer_roster_lines(self, character_id) -> list[str]:  # noqa: ANN001
        return await self._step("peer_roster", ["同伴：小葵"])

    async def _load_initial_relationship_lines(
        self, character_id, operator,  # noqa: ANN001
    ) -> list[str]:
        return await self._step("initial_relationship", ["初識於書店"])

    async def _load_persona_curiosity_plan(
        self,
        *,
        character,  # noqa: ANN001
        operator,  # noqa: ANN001
        enabled,  # noqa: ANN001
        conversation_id,  # noqa: ANN001
        recent_dialogue_summary,  # noqa: ANN001
        initial_relationship_lines,  # noqa: ANN001
        now,  # noqa: ANN001
    ):
        self.seen["curiosity"] = {
            "enabled": enabled,
            "conversation_id": conversation_id,
            "recent_dialogue_summary": recent_dialogue_summary,
            "initial_relationship_lines": list(initial_relationship_lines),
            "now": now,
        }
        return await self._step("curiosity", self.curiosity_plan)

    async def _load_recent_emotion_events(self, *, character_id, operator, now):  # noqa: ANN001
        self.seen["emotion_events"] = {"now": now}
        return await self._step("emotion_events", ["emotion-event"])

    async def _load_self_reflections(self, *, character_id, operator):  # noqa: ANN001
        return await self._step("self_reflections", ["reflection"])

    async def _load_cached_prompt_material_digest(
        self,
        *,
        character,  # noqa: ANN001
        operator,  # noqa: ANN001
        content_tolerance,  # noqa: ANN001
        now,  # noqa: ANN001
    ):
        # A store read, never an upstream call.
        self.seen["material_digest"] = {
            "content_tolerance": content_tolerance, "now": now,
        }
        return await self._step("material_digest_read", self.material_digest)

    async def _load_phrase_habit_lines(self, character_id) -> list[str]:  # noqa: ANN001
        return await self._step("phrase_habits", ["句尾偶爾帶一個「欸」"])

    async def _load_register_profile(
        self,
        *,
        character,  # noqa: ANN001
        operator,  # noqa: ANN001
        latest_user_message,  # noqa: ANN001
        recent_dialogue_summary,  # noqa: ANN001
        relationship_context,  # noqa: ANN001
        content_tolerance,  # noqa: ANN001
    ):
        self.seen["register_profile"] = {
            "latest_user_message": latest_user_message,
            "relationship_context": tuple(relationship_context),
            "content_tolerance": content_tolerance,
        }
        return await self._step("register_profile", self.register_profile)


class _BlockingEmbedder:
    """Wave-1 participant standing in for the diversity embedding pass."""

    is_operational = True

    def __init__(self, rendezvous: _Rendezvous | None) -> None:
        self._rendezvous = rendezvous
        self.calls = 0

    async def embed_many(self, texts):  # noqa: ANN001
        self.calls += 1
        if self._rendezvous is not None:
            await self._rendezvous.arrive("embedding")
        return [None for _ in texts]

    async def embed(self, text: str):  # noqa: ANN001
        return None


def _character() -> SimpleNamespace:
    return SimpleNamespace(id="char-1", user_id="user-1", name="Mio")


def _operator() -> SimpleNamespace:
    return SimpleNamespace(id="user-1", primary_language="zh-TW")


async def _load(
    loaders: _StubLoaders,
    *,
    embedder=None,  # noqa: ANN001
    load_operator_persona: bool = True,
    persona_enabled: bool = True,
    diversity_messages: list[Message] | None = None,
):
    return await load_turn_aux_context(
        loaders,
        character=_character(),
        operator=_operator(),
        conversation_id="conv-1",
        now=_NOW,
        main_model=SimpleNamespace(supports_vision=False),
        vision_urls=("https://example.invalid/a.png",),
        recognition_content_tolerance="standard",
        content_tolerance="strict",
        persona_enabled=persona_enabled,
        load_operator_persona=load_operator_persona,
        latest_user_message="今天想聊天",
        recent_dialogue_summary="前情提要",
        diversity_messages=diversity_messages or [],
        self_repetition_hint=None,
        embedder=embedder,
        script_mix_decorator=_with_script_mix_evidence,
    )


# --------------------------------------------------------------------------
# Concurrency proofs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wave_one_runs_every_ready_loader_concurrently() -> None:
    """All ten wave-1 steps must be in flight at the same moment.

    Serial awaits would park on the first arrival forever, because nobody
    else ever reaches the rendezvous. The material-digest read is one of
    the ten: it is a DB read like its neighbours, so it has to overlap
    them rather than add its own round trip to the turn.
    """
    rendezvous = _Rendezvous(len(_WAVE_ONE_LOADERS))
    loaders = _StubLoaders(rendezvous=rendezvous, blocking=_WAVE_ONE_LOADERS)
    embedder = _BlockingEmbedder(rendezvous)

    context = await asyncio.wait_for(
        _load(loaders, embedder=embedder), timeout=_TIMEOUT,
    )

    assert sorted(rendezvous.arrived) == sorted(_WAVE_ONE_LOADERS)
    assert context.image_recognition_context == "看到一張照片"
    assert context.phrase_habit_lines == ["句尾偶爾帶一個「欸」"]


@pytest.mark.asyncio
async def test_wave_two_runs_the_two_aux_llm_calls_concurrently() -> None:
    """Curiosity plan and register profile overlap.

    The digest is no longer among them — it arrived in wave 1, which is
    why this wave completes while nothing awaits a digester.
    """
    rendezvous = _Rendezvous(len(_WAVE_TWO_LOADERS))
    loaders = _StubLoaders(
        rendezvous=rendezvous,
        blocking=_WAVE_TWO_LOADERS,
        curiosity_plan=PersonaCuriosityPlan(should_ask=True),
        material_digest=PromptMaterialDigest(bullets=("今天的重點",)),
        register_profile=RegisterProfile(axes={}, confidence=0.5),
    )

    context = await asyncio.wait_for(_load(loaders), timeout=_TIMEOUT)

    assert sorted(rendezvous.arrived) == sorted(_WAVE_TWO_LOADERS)
    assert context.persona_curiosity_plan is loaders.curiosity_plan
    assert context.material_digest is loaders.material_digest
    assert context.register_profile is loaders.register_profile


@pytest.mark.asyncio
async def test_a_cold_digest_store_is_a_miss_not_an_inline_call() -> None:
    """DIGEST_OFFPATH — a miss renders the source blocks, full stop."""
    loaders = _StubLoaders(material_digest=None)

    context = await asyncio.wait_for(_load(loaders), timeout=_TIMEOUT)

    assert context.material_digest is None
    assert loaders.calls.count("material_digest_read") == 1
    # The read is given the turn's clock, which is what bounds the row's age.
    assert loaders.seen["material_digest"]["now"] == _NOW


@pytest.mark.asyncio
async def test_wave_two_still_consumes_wave_one_results() -> None:
    """Parallelism must not break the dependency edges between the waves."""
    loaders = _StubLoaders()

    context = await asyncio.wait_for(_load(loaders), timeout=_TIMEOUT)

    assert loaders.seen["curiosity"]["initial_relationship_lines"] == ["初識於書店"]
    assert loaders.seen["register_profile"]["relationship_context"] == (
        "關於對方：喜歡貓",
        "初識於書店",
    )
    # The persona lines are rendered from the wave-1 aggregate, before the
    # register profile that reads them is dispatched.
    assert loaders.calls.index("render_persona_lines") < loaders.calls.index(
        "register_profile",
    )
    assert context.operator_persona_lines == ["關於對方：喜歡貓"]


@pytest.mark.asyncio
async def test_cancellation_propagates_into_the_wave_and_leaves_no_task() -> None:
    """A disconnecting client cancels the children, not just the awaiter."""
    rendezvous = _Rendezvous(len(_WAVE_ONE_LOADERS) + 1)  # never released
    loaders = _StubLoaders(rendezvous=rendezvous, blocking=_WAVE_ONE_LOADERS)
    before = {task for task in asyncio.all_tasks()}

    task = asyncio.create_task(_load(loaders))
    while len(rendezvous.arrived) < len(_WAVE_ONE_LOADERS) - 1:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert rendezvous.cancelled, "wave-1 children must observe the cancellation"
    leaked = {t for t in asyncio.all_tasks() if t not in before and t is not task}
    assert not leaked, f"gather must not leave tasks behind: {leaked}"


# --------------------------------------------------------------------------
# Error semantics — identical to the serial version
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failing_db_loader_still_kills_the_turn() -> None:
    """``gather`` runs without ``return_exceptions``: DB errors propagate."""
    loaders = _StubLoaders(raises={"peer_roster": RuntimeError("db down")})

    with pytest.raises(RuntimeError, match="db down"):
        await asyncio.wait_for(_load(loaders), timeout=_TIMEOUT)


@pytest.mark.asyncio
async def test_a_failing_wave_two_loader_still_kills_the_turn() -> None:
    """Wave-2 adapters swallow their own errors; anything that escapes them
    ends the turn exactly as it did when the awaits were serial."""
    loaders = _StubLoaders(raises={"curiosity": RuntimeError("curiosity boom")})

    with pytest.raises(RuntimeError, match="curiosity boom"):
        await asyncio.wait_for(_load(loaders), timeout=_TIMEOUT)


@pytest.mark.asyncio
async def test_aux_llm_fallbacks_complete_the_turn() -> None:
    """The normal fail-open shape: adapters return ``None``, turn survives."""
    loaders = _StubLoaders()

    context = await asyncio.wait_for(_load(loaders), timeout=_TIMEOUT)

    assert context.persona_curiosity_plan is None
    assert context.material_digest is None
    assert context.register_profile is None
    assert context.diversity_evidence is not None


# --------------------------------------------------------------------------
# Per-call-site differences the two sites disagree on
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_persona_load_is_gated_independently_of_persona_enabled() -> None:
    """The tool path gates the aggregate; the stream path never does."""
    loaders = _StubLoaders()

    context = await asyncio.wait_for(
        _load(loaders, load_operator_persona=False, persona_enabled=True),
        timeout=_TIMEOUT,
    )

    assert "operator_persona" not in loaders.calls
    assert context.operator_persona is None
    assert context.operator_persona_lines == []
    # ``persona_enabled`` still drives the note and the curiosity plan.
    assert loaders.seen["player_persona_note"]["enabled"] is True
    assert loaders.seen["curiosity"]["enabled"] is True


@pytest.mark.asyncio
async def test_recognition_and_aux_tolerances_stay_separate() -> None:
    loaders = _StubLoaders()

    await asyncio.wait_for(_load(loaders), timeout=_TIMEOUT)

    assert loaders.seen["recognition"]["content_tolerance"] == "standard"
    assert loaders.seen["material_digest"]["content_tolerance"] == "strict"
    assert loaders.seen["register_profile"]["content_tolerance"] == "strict"


# --------------------------------------------------------------------------
# Both call sites really go through the helper
# --------------------------------------------------------------------------


class _RecordingPromptBuilder:
    def __init__(self) -> None:
        self.last_kwargs: dict = {}
        self.calls: list[dict] = []

    def build(self, **kwargs) -> str:  # noqa: ANN003
        self.last_kwargs = dict(kwargs)
        self.calls.append(dict(kwargs))
        return f"最新使用者訊息：{kwargs.get('latest_user_message', '')}"


class _RecordingChatModel:
    provider_id = "openai"
    supports_vision = False

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, object]] = []

    async def generate(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.calls.append({"prompt": prompt, "model": model})
        return self.reply

    async def generate_stream(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.calls.append({"prompt": prompt, "model": model})
        yield self.reply

    async def list_models(self) -> list[str]:
        return ["gpt-4o-mini"]


class _ActiveProvider:
    def __init__(self, model: _RecordingChatModel, model_id: str) -> None:
        self.model = model
        self.model_id = model_id

    async def resolve(self, feature_key=None, *, character=None, **kwargs):  # noqa: ANN001
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None, **kwargs):  # noqa: ANN001
        return self.model_id

    async def is_fake(self, feature_key=None, *, character=None, **kwargs):  # noqa: ANN001
        return False


class _OperatorProfileService:
    async def get_for_user(self, user_id: str):  # noqa: ANN001
        return SimpleNamespace(id=user_id, timezone_id="Asia/Taipei")

    async def get_current(self):  # noqa: ANN001
        return SimpleNamespace(id=DEFAULT_OPERATOR_ID, timezone_id="UTC")


class _PersonaService:
    async def get_current(self, character_id: str, operator_id: str):
        return OperatorPersona.empty(character_id, operator_id)

    def render_for_prompt(self, persona):  # noqa: ANN001
        return ["關於對方：目前還不熟。"]


class _CuriosityContextService:
    async def build_context(
        self,
        *,
        persona,  # noqa: ANN001
        surface: str,
        recent_dialogue_summary: str = "",
        initial_relationship_lines=(),  # noqa: ANN001
        now=None,  # noqa: ANN001
        operator_primary_language: str = "zh-TW",
    ):
        return SimpleNamespace(
            character_id=persona.character_id,
            operator_id=persona.operator_id,
            surface=surface,
        )

    async def record_planned_attempt(self, *, context, plan, conversation_id=None, now=None):  # noqa: ANN001
        return None


class _BlockingCuriosityPlanner:
    def __init__(self, rendezvous: _Rendezvous, plan: PersonaCuriosityPlan) -> None:
        self._rendezvous = rendezvous
        self.result = plan

    async def plan(self, context, *, character=None):  # noqa: ANN001
        await self._rendezvous.arrive("curiosity")
        return self.result


class _RecordingMaterialDigester:
    """No rendezvous: DIGEST_OFFPATH forbids the turn from calling it.

    It counts instead — the two end-to-end tests below assert the count
    stays at zero for the whole player-visible turn.
    """

    def __init__(self, digest: PromptMaterialDigest) -> None:
        self.result = digest
        self.calls: list[PromptMaterialDigestContext] = []

    async def digest(self, context: PromptMaterialDigestContext, *, character=None):  # noqa: ANN001
        self.calls.append(context)
        return self.result


class _BlockingRegisterProfiler:
    def __init__(self, rendezvous: _Rendezvous, profile: RegisterProfile) -> None:
        self._rendezvous = rendezvous
        self.result = profile

    async def profile(self, context: RegisterProfileContext, *, character=None):  # noqa: ANN001
        await self._rendezvous.arrive("register_profile")
        return self.result


class _NoveltyGate:
    def __init__(self) -> None:
        self.calls: list[NoveltyGateContext] = []

    async def evaluate(self, context: NoveltyGateContext, *, character=None):  # noqa: ANN001
        self.calls.append(context)
        return NoveltyVerdict(passes=True)


def _low_risk_register_profile() -> RegisterProfile:
    return RegisterProfile(
        axes={
            "emotional_intensity": 0.1,
            "seriousness": 0.1,
            "intimacy": 0.2,
            "humor_latitude": 0.5,
            "help_seeking": 0.0,
        },
        confidence=0.9,
        vulnerable_disclosure=False,
        note="日常閒聊",
    )


def _build_parallel_chat_service(reply: str):
    """A service whose three aux-LLM ports only return once all three run."""
    rendezvous = _Rendezvous(len(_WAVE_TWO_LOADERS))
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    prompt_builder = _RecordingPromptBuilder()
    active_model = _RecordingChatModel(reply)
    novelty_gate = _NoveltyGate()
    register_profiler = _BlockingRegisterProfiler(
        rendezvous, _low_risk_register_profile(),
    )
    digester = _RecordingMaterialDigester(
        PromptMaterialDigest(bullets=("今天的重點",)),
    )
    curiosity_planner = _BlockingCuriosityPlanner(
        rendezvous, PersonaCuriosityPlan(should_ask=False),
    )
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        active_llm_provider=_ActiveProvider(active_model, "gpt-4o-mini"),
        state_engine=SimpleStateEngine(),
        operator_profile_service=_OperatorProfileService(),
        operator_persona_service=_PersonaService(),
        persona_curiosity_service=_CuriosityContextService(),
        persona_curiosity_planner=curiosity_planner,
        prompt_material_digester=digester,
        prompt_material_digest_enabled=True,
        register_profiler=register_profiler,
        register_profile_enabled=True,
        reply_quality_gate=novelty_gate,
        reply_quality_gate_enabled=True,
        reply_quality_gate_risk_threshold=0.9,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=InMemoryMemoryRepository(),
    )
    return SimpleNamespace(
        rendezvous=rendezvous,
        chat_service=chat_service,
        character_service=character_service,
        prompt_builder=prompt_builder,
        active_model=active_model,
        novelty_gate=novelty_gate,
        register_profiler=register_profiler,
        digester=digester,
        curiosity_planner=curiosity_planner,
    )


async def _budgeted_digest(rig, character_id: str):  # noqa: ANN001
    """What the turn's post-turn left for the *next* turn to read."""
    return await rig.chat_service._load_cached_prompt_material_digest(
        character=SimpleNamespace(
            id=character_id, user_id=DEFAULT_OPERATOR_ID,
        ),
        operator=None,
        content_tolerance="frontier",
        now=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_tool_path_loads_the_aux_context_in_parallel() -> None:
    """``send_message`` → ``_generate_reply_with_tools`` (site B)."""
    rig = _build_parallel_chat_service("tool path reply")
    created = await rig.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    response = await asyncio.wait_for(
        rig.chat_service.send_message(
            SendChatMessageRequest(
                character_id=created.id,
                message="今天想聊天",
                operator_persona_enabled=True,
            ),
        ),
        timeout=_TIMEOUT,
    )

    assert sorted(rig.rendezvous.arrived) == sorted(_WAVE_TWO_LOADERS)
    assert response.assistant_message is not None
    assert response.assistant_message.content == "tool path reply"
    assert rig.prompt_builder.last_kwargs["turn_register_profile"] is (
        rig.register_profiler.result
    )
    # Cold cache on a first turn: source blocks. The single digester call
    # this rig records belongs to the post-turn budget that ran *after* the
    # reply — the prompt above was assembled without waiting for it.
    assert rig.prompt_builder.last_kwargs["material_digest"] is None
    assert await _budgeted_digest(rig, created.id) == rig.digester.result
    assert rig.prompt_builder.last_kwargs["reply_diversity_evidence"] is not None
    # Low-risk register profile keeps the gate off the critical path.
    assert rig.novelty_gate.calls == []


@pytest.mark.asyncio
async def test_stream_path_loads_the_aux_context_in_parallel() -> None:
    """``send_message_stream`` no-tool branch (site A)."""
    rig = _build_parallel_chat_service("stream path reply")
    created = await rig.character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    token_stream, finalizer = await asyncio.wait_for(
        rig.chat_service.send_message_stream(
            SendChatMessageRequest(
                character_id=created.id,
                message="今天想聊天",
                operator_persona_enabled=True,
            ),
        ),
        timeout=_TIMEOUT,
    )
    chunks = [chunk async for chunk in token_stream]
    response = await finalizer.finish("".join(chunks))

    assert sorted(rig.rendezvous.arrived) == sorted(_WAVE_TWO_LOADERS)
    assert chunks == ["stream path reply"]
    assert response.assistant_message is not None
    assert response.assistant_message.content == "stream path reply"
    assert rig.prompt_builder.last_kwargs["turn_register_profile"] is (
        rig.register_profiler.result
    )
    assert rig.prompt_builder.last_kwargs["material_digest"] is None
    assert await _budgeted_digest(rig, created.id) == rig.digester.result
    assert rig.novelty_gate.calls == []
