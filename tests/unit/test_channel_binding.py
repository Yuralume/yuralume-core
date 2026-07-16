from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.channel_binding import ChannelBinding


def test_create_assigns_id_and_timestamps() -> None:
    binding = ChannelBinding.create(account_id="acct-1", chat_ref="chat-1")

    assert binding.id
    assert binding.account_id == "acct-1"
    assert binding.chat_ref == "chat-1"
    assert binding.enabled is True
    assert binding.conversation_id is None
    assert binding.created_at == binding.updated_at


def test_create_strips_chat_ref() -> None:
    binding = ChannelBinding.create(account_id="acct-1", chat_ref="   U123   ")
    assert binding.chat_ref == "U123"


def test_create_rejects_empty_chat_ref() -> None:
    with pytest.raises(ValueError):
        ChannelBinding.create(account_id="acct-1", chat_ref="   ")


def test_create_rejects_empty_account_id() -> None:
    with pytest.raises(ValueError):
        ChannelBinding.create(account_id="", chat_ref="c1")


def test_with_enabled_updates_flag_and_timestamp() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    binding = ChannelBinding.create(
        account_id="acct-1", chat_ref="c1", now=t0,
    )

    disabled = binding.with_enabled(False, now=t1)

    assert disabled.enabled is False
    assert disabled.updated_at == t1
    assert disabled.created_at == t0


def test_with_conversation_attaches_thread() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    binding = ChannelBinding.create(
        account_id="acct-1", chat_ref="c1", now=t0,
    )

    attached = binding.with_conversation("conv-1", now=t1)

    assert attached.conversation_id == "conv-1"
    assert attached.updated_at == t1
