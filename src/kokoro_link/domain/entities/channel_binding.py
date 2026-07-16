"""ChannelBinding entity.

Binds a single chat (identified by ``chat_ref``) under a
``MessagingAccount`` to its per-chat conversation thread. Platform and
character identity come from the parent account — a binding is purely
"this account has spoken with this chat, here is the thread".

``conversation_id`` is created lazily on the first inbound and written
back so subsequent messages continue the same thread. Dispatcher also
self-heals when the id points at a deleted conversation.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    id: str
    account_id: str
    chat_ref: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
    conversation_id: str | None = None
    accepts_proactive: bool = False
    """Whether this specific chat should receive proactive (character-
    initiated) messages. Defaults to off — proactive sends to a group
    chat would be weird, and even in a 1:1 DM the operator should have
    to explicitly authorise it."""

    @classmethod
    def create(
        cls,
        *,
        account_id: str,
        chat_ref: str,
        enabled: bool = True,
        accepts_proactive: bool = False,
        now: datetime | None = None,
    ) -> "ChannelBinding":
        if not account_id or not account_id.strip():
            raise ValueError("account_id must be non-empty")
        chat_ref_clean = chat_ref.strip()
        if not chat_ref_clean:
            raise ValueError("chat_ref must be non-empty")
        current = now or datetime.now(timezone.utc)
        return cls(
            id=str(uuid4()),
            account_id=account_id,
            chat_ref=chat_ref_clean,
            enabled=enabled,
            created_at=current,
            updated_at=current,
            conversation_id=None,
            accepts_proactive=accepts_proactive,
        )

    def with_enabled(
        self, enabled: bool, *, now: datetime | None = None,
    ) -> "ChannelBinding":
        return replace(
            self,
            enabled=enabled,
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_conversation(
        self, conversation_id: str, *, now: datetime | None = None,
    ) -> "ChannelBinding":
        return replace(
            self,
            conversation_id=conversation_id,
            updated_at=now or datetime.now(timezone.utc),
        )

    def with_accepts_proactive(
        self, accepts_proactive: bool, *, now: datetime | None = None,
    ) -> "ChannelBinding":
        return replace(
            self,
            accepts_proactive=accepts_proactive,
            updated_at=now or datetime.now(timezone.utc),
        )
