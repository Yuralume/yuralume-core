"""Shared builders for the LH2 external-chat turn service unit tests.

Not a test module (no ``test_`` prefix, not collected). Assembles a minimal
but real :class:`ExternalChatTurnService` over in-memory repositories and the
counting fake LLM so both the happy-path/guard suite and the crash-recovery
suite drive the exact production orchestrator.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

from kokoro_link.application.dto.external_chat import (
    ExternalChatAttachmentInput,
    ExternalChatTurnCommand,
)
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.external_chat_roster_service import (
    ExternalChatRosterService,
)
from kokoro_link.application.services.external_chat_turn_service import (
    ExternalChatTurnService,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.contracts.object_storage import ObjectMetadata
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import (
    NullPostTurnProcessor,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_external_chat_turn import (
    InMemoryExternalChatTurnReceiptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

TENANT = "tenant-A"
ACCOUNT = "acct-1"
OPERATOR_ID = "cloud:acct-1"
CHARACTER_ID = "char-1"


class CountingFakeChatModel(FakeChatModel):
    """Fake LLM that counts ``generate`` calls so recovery tests can assert the
    LLM ran exactly once (or never, on a GENERATED/COMMITTED takeover)."""

    def __init__(self, provider_id: str = "fake") -> None:
        super().__init__(provider_id)
        self.generate_calls = 0

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.generate_calls += 1
        return await super().generate(prompt, image_urls=image_urls, model=model)


class FakeObjectStorage:
    """Reverses ``/v1/public/{key}`` refs and serves seeded ``stat`` metadata.

    ``public_url`` mints the app-relative ref for an inbound object key;
    ``stat`` returns the metadata seeded for outbound attachment normalisation
    (absent ⇒ ``None`` ⇒ the attachment is skipped)."""

    def __init__(self, stats: dict[str, ObjectMetadata] | None = None) -> None:
        self._stats = stats or {}

    def object_key_from_url(self, url: str) -> str | None:
        for prefix in ("/uploads/", "/v1/public/"):
            if url.startswith(prefix):
                return url[len(prefix):] or None
        return None

    async def public_url(self, *, object_key: str) -> str:
        return f"/v1/public/{object_key}"

    async def stat(self, *, object_key: str) -> ObjectMetadata | None:
        return self._stats.get(object_key)


class _FakeSubscriptionRepo:
    def __init__(self, locked_tenants: tuple[str, ...] = ()) -> None:
        self._locked = set(locked_tenants)

    async def get(self, tenant_id: str):
        from kokoro_link.domain.entities.cloud_subscription import (
            CloudSubscriptionState,
        )
        if tenant_id in self._locked:
            return CloudSubscriptionState(
                tenant_id=tenant_id,
                locked=True,
                updated_at=datetime.now(timezone.utc),
            )
        return None


class _StubScheduleService:
    def __init__(self, *, current_activity=None) -> None:
        self.current_activity = current_activity

    async def ensure_schedule(self, character):
        return SimpleNamespace(date=datetime.now(timezone.utc).date())

    def resolve_current(self, schedule, *, now=None):
        return self.current_activity, [], None

    def resolve_completed_today(self, schedule, *, now=None, local_tz=None, limit=8):
        return []

    def resolve_pending_invites_from_schedules(self, schedules, *, now=None, limit=1):
        return []

    async def get_schedule(self, character_id, *, date_=None):
        return None

    async def current_activity_response(self, character):  # pragma: no cover
        return None


def make_operator(auth_provider: str = "cloud") -> OperatorProfile:
    return OperatorProfile(
        id=OPERATOR_ID,
        display_name="Player",
        cloud_account_id=ACCOUNT,
        cloud_tenant_id=TENANT,
        auth_provider=auth_provider,
    )


def make_character(
    *, character_id: str = CHARACTER_ID, name: str = "Airi",
) -> Character:
    state = CharacterState(
        emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
    )
    return Character(
        id=character_id,
        name=name,
        summary="",
        user_id=OPERATOR_ID,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=state,
        image_urls=(),
    )


@dataclass(slots=True)
class TurnHarness:
    turn_service: ExternalChatTurnService
    receipt_repo: InMemoryExternalChatTurnReceiptRepository
    conversation_repo: InMemoryConversationRepository
    character_repo: InMemoryCharacterRepository
    operator_repo: InMemoryOperatorProfileRepository
    roster_service: ExternalChatRosterService
    storage: FakeObjectStorage
    model: CountingFakeChatModel
    chat_service: object


async def build_harness(
    *,
    operator_auth_provider: str = "cloud",
    locked_tenants: tuple[str, ...] = (),
    storage: FakeObjectStorage | None = None,
    conversation_repo: InMemoryConversationRepository | None = None,
    receipt_repo: InMemoryExternalChatTurnReceiptRepository | None = None,
    model: CountingFakeChatModel | None = None,
    decider=None,
    current_activity=None,
    chat_service_override: object | None = None,
) -> TurnHarness:
    character_repo = InMemoryCharacterRepository()
    await character_repo.save(make_character())
    operator_repo = InMemoryOperatorProfileRepository()
    await operator_repo.save(make_operator(operator_auth_provider))

    conversation_repo = conversation_repo or InMemoryConversationRepository()
    receipt_repo = receipt_repo or InMemoryExternalChatTurnReceiptRepository()
    storage = storage or FakeObjectStorage()
    model = model or CountingFakeChatModel()

    guard = SubscriptionAccessGuard(
        subscription_repository=_FakeSubscriptionRepo(locked_tenants),
        operator_profile_repository=operator_repo,
    )
    roster_service = ExternalChatRosterService(
        character_repository=character_repo,
        operator_profile_repository=operator_repo,
        subscription_access_guard=guard,
        object_storage=storage,
    )

    chat_service: object
    if chat_service_override is not None:
        chat_service = chat_service_override
    else:
        registry = InMemoryChatModelRegistry(default_provider_id="fake")
        registry.register(model)
        pending_repo = InMemoryPendingFollowUpRepository()
        chat_service = ChatService(
            character_repository=character_repo,
            conversation_repository=conversation_repo,
            memory_repository=InMemoryMemoryRepository(),
            post_turn_processor=NullPostTurnProcessor(),
            prompt_context_builder=DefaultPromptContextBuilder(),
            model_registry=registry,
            state_engine=SimpleStateEngine(),
            schedule_service=_StubScheduleService(
                current_activity=current_activity,
            ),
            busy_reply_decider=decider,
            pending_follow_up_repository=pending_repo if decider else None,
        )

    turn_service = ExternalChatTurnService(
        receipt_repository=receipt_repo,
        chat_service=chat_service,  # type: ignore[arg-type]
        conversation_repository=conversation_repo,
        roster_service=roster_service,
        operator_profile_repository=operator_repo,
        object_storage=storage,
    )
    return TurnHarness(
        turn_service=turn_service,
        receipt_repo=receipt_repo,
        conversation_repo=conversation_repo,
        character_repo=character_repo,
        operator_repo=operator_repo,
        roster_service=roster_service,
        storage=storage,
        model=model,
        chat_service=chat_service,
    )


def make_command(
    *,
    request_id: str = "line:tw-dev:evt-turn-001",
    message: str = "今天過得怎麼樣",
    tenant_id: str = TENANT,
    account_id: str = ACCOUNT,
    character_id: str = CHARACTER_ID,
    conversation_id: str | None = None,
    conversation_id_present: bool = False,
    attachments: tuple[ExternalChatAttachmentInput, ...] = (),
) -> ExternalChatTurnCommand:
    return ExternalChatTurnCommand(
        request_id=request_id,
        tenant_id=tenant_id,
        account_id=account_id,
        character_id=character_id,
        message=message,
        attachments=attachments,
        channel="line",
        contract_version=1,
        conversation_id=conversation_id,
        conversation_id_present=conversation_id_present,
    )
