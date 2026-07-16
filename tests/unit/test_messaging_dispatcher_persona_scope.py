from __future__ import annotations

from kokoro_link.application.services.messaging_dispatcher import (
    _persona_safe_for_account,
)
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.platform import Platform


def _account(*, senders: tuple[str, ...]) -> MessagingAccount:
    return MessagingAccount.create(
        character_id="char-A",
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "token"},
        allowed_sender_refs=senders,
    )


def test_persona_enabled_only_for_single_allowed_sender() -> None:
    assert _persona_safe_for_account(_account(senders=("user-1",))) is True
    assert _persona_safe_for_account(_account(senders=())) is False
    assert _persona_safe_for_account(_account(senders=("user-1", "user-2"))) is False
