"""Fixture loader + harness runner.

Each fixture is one YAML file under ``tests/evals/fixtures/`` describing
a scenario:

* ``character`` — light overrides for the canned default character (name,
  personality lines, current state). Optional.
* ``seeded_messages`` — prior conversation, possibly multi-source. Each
  message carries an absolute ISO timestamp so the cross-source merge
  by ``created_at`` is exercised.
* ``trigger`` — what triggers the system-under-test call. For now only
  ``kind: chat`` is supported (sends a user message and captures the
  reply). ``proactive`` is left as future work — it requires more
  scaffolding around the dispatcher's gate/decider chain.
* ``judge`` — rubric + optional keyword guards.

The runner builds an in-memory env mirroring ``_messaging_harness`` but
swaps the model registry to a real LLM endpoint (configured via
``KOKORO_EVALS_SYSTEM_ENDPOINT``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.body_state import BodyState
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.personality_type import CharacterPersonalityType
from kokoro_link.infrastructure.embedder.null import NullEmbedder
from kokoro_link.infrastructure.llm.openai_compatible import (
    OpenAICompatibleChatModel,
)
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

from tests.evals.judge import JudgeCriteria, JudgeVerdict, evaluate

_LOGGER = logging.getLogger(__name__)

_SYSTEM_ENDPOINT_ENV = "KOKORO_EVALS_SYSTEM_ENDPOINT"
_SYSTEM_MODEL_ENV = "KOKORO_EVALS_SYSTEM_MODEL"
_SYSTEM_API_KEY_ENV = "KOKORO_EVALS_SYSTEM_API_KEY"
_JUDGE_ENDPOINT_ENV = "KOKORO_EVALS_JUDGE_ENDPOINT"
_JUDGE_MODEL_ENV = "KOKORO_EVALS_JUDGE_MODEL"
_JUDGE_API_KEY_ENV = "KOKORO_EVALS_JUDGE_API_KEY"

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True, slots=True)
class SeededMessage:
    source: str
    role: str
    content: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Trigger:
    kind: str
    user_message: str
    source: str = "web"


@dataclass(frozen=True, slots=True)
class SeededMemory:
    """Pre-existing memory row planted into the in-memory repo before the
    fixture triggers chat. Lets fixtures verify behaviours that depend on
    prior recall (e.g. ``relationship_milestone`` band crossings, recent
    EmotionEvents, role boundaries anchored by past evidence).
    """

    kind: str
    content: str
    salience: float = 0.5
    tags: tuple[str, ...] = field(default_factory=tuple)
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CharacterOverride:
    name: str = "小美"
    personality: tuple[str, ...] = ("溫柔", "細心", "愛美食")
    speaking_style: str = "輕鬆口語，偶爾撒嬌"
    summary: str = "便利商店打工的大學生。"
    emotion: str = "neutral"
    affection: int = 60
    fatigue: int = 30
    trust: int = 55
    energy: int = 70
    body_state: dict[str, str] = field(default_factory=dict)
    """HUMANIZATION_ROADMAP §4.1 — embodied signal overrides for body_state
    fixtures. Empty dict → default (all low / no body signal). Keys:
    hunger / thirst / sleep_debt / seasonal_allergy × low/medium/high."""
    personality_type: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InitialRelationshipOverride:
    relationship_label: str = ""
    known_context: str = ""
    living_arrangement: str = ""
    user_address_name: str = ""
    character_address_name: str = ""
    tone_distance: str = ""
    familiarity_boundary: str = ""
    schedule_involvement_policy: str = "none"
    proactive_permission: bool = False
    proactive_cadence_hint: str = ""
    user_profile_notes: str = ""

    @property
    def is_empty(self) -> bool:
        return not any((
            self.relationship_label,
            self.known_context,
            self.living_arrangement,
            self.user_address_name,
            self.character_address_name,
            self.tone_distance,
            self.familiarity_boundary,
            self.schedule_involvement_policy != "none",
            self.proactive_permission,
            self.proactive_cadence_hint,
            self.user_profile_notes,
        ))


@dataclass(frozen=True, slots=True)
class Fixture:
    id: str
    description: str
    seeded_messages: tuple[SeededMessage, ...]
    trigger: Trigger
    judge: JudgeCriteria
    character: CharacterOverride = field(default_factory=CharacterOverride)
    initial_relationship: InitialRelationshipOverride = field(
        default_factory=InitialRelationshipOverride,
    )
    seeded_memories: tuple[SeededMemory, ...] = field(default_factory=tuple)
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class FixtureResult:
    fixture_id: str
    candidate: str
    verdict: JudgeVerdict


# ---------- loader ------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    raw = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _coerce_messages(raw: list[dict[str, Any]] | None) -> tuple[SeededMessage, ...]:
    if not raw:
        return ()
    out: list[SeededMessage] = []
    for entry in raw:
        out.append(SeededMessage(
            source=str(entry.get("source", "web")),
            role=str(entry["role"]),
            content=str(entry["content"]),
            at=_parse_iso(str(entry["at"])),
        ))
    return tuple(out)


def _coerce_memories(raw: list[dict[str, Any]] | None) -> tuple[SeededMemory, ...]:
    if not raw:
        return ()
    out: list[SeededMemory] = []
    for entry in raw:
        at = entry.get("at")
        out.append(SeededMemory(
            kind=str(entry.get("kind", "episodic")),
            content=str(entry["content"]),
            salience=float(entry.get("salience", 0.5)),
            tags=tuple(str(t) for t in (entry.get("tags") or ())),
            at=_parse_iso(str(at)) if at else None,
        ))
    return tuple(out)


def _coerce_character(raw: dict[str, Any] | None) -> CharacterOverride:
    if not raw:
        return CharacterOverride()
    base = CharacterOverride()
    body_state_raw = raw.get("body_state") or {}
    body_state = {
        str(k): str(v) for k, v in body_state_raw.items()
    } if isinstance(body_state_raw, dict) else {}
    return CharacterOverride(
        name=str(raw.get("name", base.name)),
        personality=tuple(raw.get("personality", base.personality)),
        speaking_style=str(raw.get("speaking_style", base.speaking_style)),
        summary=str(raw.get("summary", base.summary)),
        emotion=str(raw.get("emotion", base.emotion)),
        affection=int(raw.get("affection", base.affection)),
        fatigue=int(raw.get("fatigue", base.fatigue)),
        trust=int(raw.get("trust", base.trust)),
        energy=int(raw.get("energy", base.energy)),
        body_state=body_state,
        personality_type=dict(raw.get("personality_type") or {}),
    )


def _coerce_initial_relationship(
    raw: dict[str, Any] | None,
) -> InitialRelationshipOverride:
    if not raw:
        return InitialRelationshipOverride()
    return InitialRelationshipOverride(
        relationship_label=str(raw.get("relationship_label", "")),
        known_context=str(raw.get("known_context", "")),
        living_arrangement=str(raw.get("living_arrangement", "")),
        user_address_name=str(raw.get("user_address_name", "")),
        character_address_name=str(raw.get("character_address_name", "")),
        tone_distance=str(raw.get("tone_distance", "")),
        familiarity_boundary=str(raw.get("familiarity_boundary", "")),
        schedule_involvement_policy=str(
            raw.get("schedule_involvement_policy", "none"),
        ),
        proactive_permission=bool(raw.get("proactive_permission", False)),
        proactive_cadence_hint=str(raw.get("proactive_cadence_hint", "")),
        user_profile_notes=str(raw.get("user_profile_notes", "")),
    )


def load_fixture(path: Path) -> Fixture:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"fixture {path} must be a mapping at top level")
    trig_raw = raw.get("trigger") or {}
    judge_raw = raw.get("judge") or {}
    return Fixture(
        id=str(raw["id"]),
        description=str(raw.get("description", "")),
        seeded_messages=_coerce_messages(raw.get("seeded_messages")),
        trigger=Trigger(
            kind=str(trig_raw.get("kind", "chat")),
            user_message=str(trig_raw["user_message"]),
            source=str(trig_raw.get("source", "web")),
        ),
        judge=JudgeCriteria(
            rubric=str(judge_raw["rubric"]),
            must_include_concepts=tuple(judge_raw.get("must_include_concepts") or ()),
            must_not_include_concepts=tuple(
                judge_raw.get("must_not_include_concepts") or (),
            ),
        ),
        character=_coerce_character(raw.get("character")),
        initial_relationship=_coerce_initial_relationship(
            raw.get("initial_relationship"),
        ),
        seeded_memories=_coerce_memories(raw.get("seeded_memories")),
        path=path,
    )


def discover_fixtures(root: Path = FIXTURES_DIR) -> list[Fixture]:
    if not root.exists():
        return []
    paths = sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml"))
    return [load_fixture(p) for p in paths]


# ---------- env config --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    endpoint: str
    model: str
    api_key: str | None


def _load_endpoint(prefix: str) -> EndpointConfig | None:
    base = os.environ.get({
        "system": _SYSTEM_ENDPOINT_ENV,
        "judge": _JUDGE_ENDPOINT_ENV,
    }[prefix])
    if not base:
        return None
    return EndpointConfig(
        endpoint=base.rstrip("/"),
        model=os.environ.get({
            "system": _SYSTEM_MODEL_ENV,
            "judge": _JUDGE_MODEL_ENV,
        }[prefix], ""),
        api_key=os.environ.get({
            "system": _SYSTEM_API_KEY_ENV,
            "judge": _JUDGE_API_KEY_ENV,
        }[prefix]),
    )


def load_system_endpoint() -> EndpointConfig | None:
    return _load_endpoint("system")


def load_judge_endpoint() -> EndpointConfig | None:
    return _load_endpoint("judge")


def _build_chat_model(cfg: EndpointConfig, provider_id: str) -> ChatModelPort:
    return OpenAICompatibleChatModel(
        provider_id=provider_id,
        base_url=cfg.endpoint,
        api_key=cfg.api_key,
        model=cfg.model or "default",
    )


# ---------- system-under-test driver ------------------------------------------


def _build_character(override: CharacterOverride) -> Character:
    state = CharacterState(
        emotion=override.emotion,
        affection=override.affection,
        fatigue=override.fatigue,
        trust=override.trust,
        energy=override.energy,
    )
    body_state = (
        BodyState.from_payload(override.body_state)
        if override.body_state else BodyState.DEFAULT
    )
    return Character(
        id=str(uuid4()),
        name=override.name,
        summary=override.summary,
        personality=list(override.personality),
        interests=[],
        speaking_style=override.speaking_style,
        boundaries=[],
        state=state,
        body_state=body_state,
        personality_type=CharacterPersonalityType.from_payload(
            override.personality_type,
        ),
    )


def _build_chat_service(
    model: ChatModelPort,
) -> tuple[
    ChatService,
    InMemoryCharacterRepository,
    InMemoryConversationRepository,
    InMemoryMemoryRepository,
    InMemoryCharacterOperatorRelationshipSeedRepository,
]:
    character_repo = InMemoryCharacterRepository()
    conversation_repo = InMemoryConversationRepository()
    memory_repo = InMemoryMemoryRepository()
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    registry = InMemoryChatModelRegistry(default_provider_id=model.provider_id)
    registry.register(model)
    service = ChatService(
        character_repository=character_repo,
        conversation_repository=conversation_repo,
        memory_repository=memory_repo,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        embedder=NullEmbedder(),
        relationship_seed_repository=relationship_repo,
    )
    return service, character_repo, conversation_repo, memory_repo, relationship_repo


async def _seed_history(
    *,
    conversation_repo: InMemoryConversationRepository,
    character: Character,
    seeded: tuple[SeededMessage, ...],
) -> None:
    # Group by source — production code merges across sources on read
    # via ``created_at``, but the underlying store has one Conversation
    # per (character, source).
    by_source: dict[str, list[SeededMessage]] = {}
    for msg in seeded:
        by_source.setdefault(msg.source, []).append(msg)
    for source, items in by_source.items():
        conv = Conversation(
            id=str(uuid4()),
            character_id=character.id,
            source=source,
            messages=[
                Message(
                    role=MessageRole(m.role.lower()),
                    content=m.content,
                    created_at=m.at,
                )
                for m in items
            ],
        )
        await conversation_repo.save(conv)


async def _seed_memories(
    *,
    memory_repo: InMemoryMemoryRepository,
    character: Character,
    seeded: tuple[SeededMemory, ...],
) -> None:
    """Persist fixture-defined memory rows before the chat trigger fires.

    Lets fixtures verify behaviours that depend on prior memory recall —
    e.g. ``relationship_milestone`` band crossings, anchored hearsay,
    stable user-profile facts — without having to drive the post-turn
    extractor through a long seeded conversation.
    """
    for entry in seeded:
        await memory_repo.add(MemoryItem.create(
            character_id=character.id,
            kind=MemoryKind(entry.kind),
            content=entry.content,
            salience=entry.salience,
            tags=entry.tags,
            created_at=entry.at,
        ))


async def _seed_initial_relationship(
    *,
    relationship_repo: InMemoryCharacterOperatorRelationshipSeedRepository,
    character: Character,
    initial_relationship: InitialRelationshipOverride,
) -> None:
    if initial_relationship.is_empty:
        return
    await relationship_repo.save(CharacterOperatorRelationshipSeed(
        character_id=character.id,
        operator_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
        relationship_label=initial_relationship.relationship_label,
        known_context=initial_relationship.known_context,
        living_arrangement=initial_relationship.living_arrangement,
        user_address_name=initial_relationship.user_address_name,
        character_address_name=initial_relationship.character_address_name,
        tone_distance=initial_relationship.tone_distance,
        familiarity_boundary=initial_relationship.familiarity_boundary,
        schedule_involvement_policy=(
            initial_relationship.schedule_involvement_policy
        ),
        proactive_permission=initial_relationship.proactive_permission,
        proactive_cadence_hint=initial_relationship.proactive_cadence_hint,
        user_profile_notes=initial_relationship.user_profile_notes,
    ))


async def run_fixture(
    fixture: Fixture,
    *,
    system_model: ChatModelPort,
    judge_model: ChatModelPort,
    system_model_id: str | None = None,
    judge_model_id: str | None = None,
) -> FixtureResult:
    service, char_repo, conv_repo, memory_repo, relationship_repo = (
        _build_chat_service(system_model)
    )
    character = _build_character(fixture.character)
    await char_repo.save(character)
    await _seed_history(
        conversation_repo=conv_repo,
        character=character,
        seeded=fixture.seeded_messages,
    )
    await _seed_memories(
        memory_repo=memory_repo,
        character=character,
        seeded=fixture.seeded_memories,
    )
    await _seed_initial_relationship(
        relationship_repo=relationship_repo,
        character=character,
        initial_relationship=fixture.initial_relationship,
    )

    request = SendChatMessageRequest(
        character_id=character.id,
        message=fixture.trigger.user_message,
        provider_id=system_model.provider_id,
        model_id=system_model_id,
        attachment_urls=[],
        operator_persona_enabled=True,
    )
    response = await service.send_message(request)
    candidate = (
        response.assistant_message.content
        if response.assistant_message is not None else ""
    )

    verdict = await evaluate(
        judge_model=judge_model,
        candidate=candidate,
        criteria=fixture.judge,
        judge_model_id=judge_model_id,
    )
    return FixtureResult(
        fixture_id=fixture.id,
        candidate=candidate,
        verdict=verdict,
    )
