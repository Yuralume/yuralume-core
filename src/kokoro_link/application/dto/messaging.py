"""DTOs for messaging account + channel binding REST API."""

from datetime import datetime

from pydantic import BaseModel, Field

from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode


class PollingStatusResponse(BaseModel):
    enabled: bool
    running: bool
    last_update_at: datetime | None = None
    last_error: str | None = None

    @classmethod
    def from_domain(cls, account: MessagingAccount) -> "PollingStatusResponse":
        from datetime import timezone

        now = datetime.now(timezone.utc)
        lock_until = account.polling_lock_until
        enabled = (
            account.enabled
            and account.delivery_mode in (DeliveryMode.POLLING, DeliveryMode.GATEWAY)
        )
        running = enabled and lock_until is not None and lock_until > now
        return cls(
            enabled=enabled,
            running=running,
            last_update_at=account.polling_last_update_at,
            last_error=account.polling_last_error,
        )


class MessagingAccountResponse(BaseModel):
    id: str
    character_id: str
    platform: str
    display_name: str
    webhook_slug: str
    delivery_mode: str
    allowed_sender_refs: list[str]
    enabled: bool
    polling_status: PollingStatusResponse
    created_at: datetime
    updated_at: datetime
    # Credentials are deliberately NOT returned — they're write-only from
    # the API surface. Operators see if secrets are set via the presence
    # of ``has_credentials``, not the raw values.
    has_credentials: bool

    @classmethod
    def from_domain(cls, account: MessagingAccount) -> "MessagingAccountResponse":
        return cls(
            id=account.id,
            character_id=account.character_id,
            platform=account.platform.value,
            display_name=account.display_name,
            webhook_slug=account.webhook_slug,
            delivery_mode=account.delivery_mode.value,
            allowed_sender_refs=list(account.allowed_sender_refs),
            enabled=account.enabled,
            polling_status=PollingStatusResponse.from_domain(account),
            created_at=account.created_at,
            updated_at=account.updated_at,
            has_credentials=bool(account.credentials),
        )


class CreateMessagingAccountRequest(BaseModel):
    character_id: str = Field(..., min_length=1)
    platform: str = Field(..., min_length=1)
    display_name: str = ""
    credentials: dict[str, str]
    allowed_sender_refs: list[str] = Field(default_factory=list)
    enabled: bool = True
    delivery_mode: str | None = None


class UpdateMessagingAccountRequest(BaseModel):
    display_name: str | None = None
    credentials: dict[str, str] | None = None
    allowed_sender_refs: list[str] | None = None
    enabled: bool | None = None
    delivery_mode: str | None = None


class ChannelBindingResponse(BaseModel):
    id: str
    account_id: str
    chat_ref: str
    conversation_id: str | None
    enabled: bool
    accepts_proactive: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, binding: ChannelBinding) -> "ChannelBindingResponse":
        return cls(
            id=binding.id,
            account_id=binding.account_id,
            chat_ref=binding.chat_ref,
            conversation_id=binding.conversation_id,
            enabled=binding.enabled,
            accepts_proactive=binding.accepts_proactive,
            created_at=binding.created_at,
            updated_at=binding.updated_at,
        )


class CreateChannelBindingRequest(BaseModel):
    account_id: str = Field(..., min_length=1)
    chat_ref: str = Field(..., min_length=1)
    enabled: bool = True
    accepts_proactive: bool = False


class UpdateChannelBindingRequest(BaseModel):
    enabled: bool | None = None
    accepts_proactive: bool | None = None
