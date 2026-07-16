from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform


def _create(
    *,
    platform: Platform = Platform.TELEGRAM,
    credentials: dict[str, str] | None = None,
    **kwargs: object,
) -> MessagingAccount:
    if credentials is None:
        credentials = _default_credentials(platform)
    return MessagingAccount.create(
        character_id="char-1",
        platform=platform,
        credentials=credentials,
        **kwargs,  # type: ignore[arg-type]
    )


def _default_credentials(platform: Platform) -> dict[str, str]:
    if platform == Platform.TELEGRAM:
        return {"bot_token": "t"}
    if platform == Platform.DISCORD:
        return {"bot_token": "d"}
    if platform == Platform.WHATSAPP:
        return {
            "sidecar_url": "http://127.0.0.1:32190",
            "session_id": "default",
        }
    return {"channel_secret": "s", "channel_access_token": "a"}


def test_create_assigns_id_slug_and_timestamps() -> None:
    account = _create()
    assert account.id
    assert account.webhook_slug
    assert len(account.webhook_slug) >= 20
    assert account.enabled is True
    assert account.delivery_mode == DeliveryMode.POLLING
    assert account.allowed_sender_refs == ()
    assert account.created_at == account.updated_at


def test_create_rejects_empty_character_id() -> None:
    with pytest.raises(ValueError):
        MessagingAccount.create(
            character_id="",
            platform=Platform.TELEGRAM,
            credentials={"bot_token": "t"},
        )


def test_create_requires_platform_specific_credentials() -> None:
    with pytest.raises(ValueError, match="bot_token"):
        MessagingAccount.create(
            character_id="c1",
            platform=Platform.TELEGRAM,
            credentials={},
        )
    with pytest.raises(ValueError, match="channel_secret"):
        MessagingAccount.create(
            character_id="c1",
            platform=Platform.LINE,
            credentials={"channel_access_token": "a"},
        )
    with pytest.raises(ValueError, match="channel_access_token"):
        MessagingAccount.create(
            character_id="c1",
            platform=Platform.LINE,
            credentials={"channel_secret": "s"},
        )
    with pytest.raises(ValueError, match="sidecar_url"):
        MessagingAccount.create(
            character_id="c1",
            platform=Platform.WHATSAPP,
            credentials={"session_id": "default"},
        )
    with pytest.raises(ValueError, match="session_id"):
        MessagingAccount.create(
            character_id="c1",
            platform=Platform.WHATSAPP,
            credentials={"sidecar_url": "http://127.0.0.1:32190"},
        )


def test_each_create_yields_distinct_slug() -> None:
    a = _create()
    b = _create()
    assert a.webhook_slug != b.webhook_slug


def test_with_credentials_revalidates() -> None:
    account = _create()
    with pytest.raises(ValueError):
        account.with_credentials({})
    updated = account.with_credentials({"bot_token": "new"})
    assert updated.credentials == {"bot_token": "new"}


def test_with_allowed_senders_replaces_list_and_bumps_timestamp() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    account = _create(now=t0)

    updated = account.with_allowed_senders(("U1", "U2"), now=t1)

    assert updated.allowed_sender_refs == ("U1", "U2")
    assert updated.updated_at == t1
    assert updated.created_at == t0


def test_is_sender_allowed_empty_list_accepts_all() -> None:
    account = _create()
    assert account.is_sender_allowed("anyone")
    assert account.is_sender_allowed("")


def test_is_sender_allowed_enforces_list() -> None:
    account = _create().with_allowed_senders(("U1", "U2"))
    assert account.is_sender_allowed("U1")
    assert account.is_sender_allowed("U2")
    assert not account.is_sender_allowed("U3")


def test_with_enabled_and_display_name() -> None:
    account = _create(display_name="  My Bot  ")
    assert account.display_name == "My Bot"

    disabled = account.with_enabled(False)
    assert disabled.enabled is False

    renamed = account.with_display_name("Second")
    assert renamed.display_name == "Second"


def test_line_delivery_mode_must_be_webhook() -> None:
    line = _create(platform=Platform.LINE)
    assert line.delivery_mode == DeliveryMode.WEBHOOK

    with pytest.raises(ValueError, match="webhook"):
        _create(platform=Platform.LINE, delivery_mode=DeliveryMode.POLLING)


def test_whatsapp_delivery_mode_must_be_gateway() -> None:
    account = _create(platform=Platform.WHATSAPP)
    assert account.delivery_mode == DeliveryMode.GATEWAY

    with pytest.raises(ValueError, match="gateway"):
        _create(platform=Platform.WHATSAPP, delivery_mode=DeliveryMode.WEBHOOK)
