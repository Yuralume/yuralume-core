import pytest

from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.repositories.in_memory_messaging_accounts import (
    InMemoryMessagingAccountRepository,
)


def _make(platform: Platform, character_id: str) -> MessagingAccount:
    creds = (
        {"bot_token": "t"} if platform == Platform.TELEGRAM
        else {"channel_secret": "s", "channel_access_token": "a"}
    )
    return MessagingAccount.create(
        character_id=character_id, platform=platform, credentials=creds,
    )


@pytest.mark.asyncio
async def test_save_and_get_roundtrip() -> None:
    repo = InMemoryMessagingAccountRepository()
    account = _make(Platform.TELEGRAM, "c1")
    await repo.save(account)

    assert await repo.get(account.id) == account


@pytest.mark.asyncio
async def test_find_by_slug() -> None:
    repo = InMemoryMessagingAccountRepository()
    account = _make(Platform.TELEGRAM, "c1")
    await repo.save(account)

    assert await repo.find_by_slug(account.webhook_slug) == account
    assert await repo.find_by_slug("does-not-exist") is None


@pytest.mark.asyncio
async def test_find_for_character_scopes_by_platform() -> None:
    repo = InMemoryMessagingAccountRepository()
    tg = _make(Platform.TELEGRAM, "c1")
    line = _make(Platform.LINE, "c1")
    other = _make(Platform.TELEGRAM, "c2")
    for a in (tg, line, other):
        await repo.save(a)

    assert await repo.find_for_character(Platform.TELEGRAM, "c1") == tg
    assert await repo.find_for_character(Platform.LINE, "c1") == line
    assert await repo.find_for_character(Platform.LINE, "c-missing") is None


@pytest.mark.asyncio
async def test_list_for_character() -> None:
    repo = InMemoryMessagingAccountRepository()
    tg = _make(Platform.TELEGRAM, "c1")
    line = _make(Platform.LINE, "c1")
    other = _make(Platform.TELEGRAM, "c2")
    for a in (tg, line, other):
        await repo.save(a)

    listed = await repo.list_for_character("c1")
    assert {a.id for a in listed} == {tg.id, line.id}


@pytest.mark.asyncio
async def test_delete() -> None:
    repo = InMemoryMessagingAccountRepository()
    account = _make(Platform.TELEGRAM, "c1")
    await repo.save(account)

    assert await repo.delete(account.id) is True
    assert await repo.get(account.id) is None
    assert await repo.delete(account.id) is False


@pytest.mark.asyncio
async def test_delete_for_character() -> None:
    repo = InMemoryMessagingAccountRepository()
    await repo.save(_make(Platform.TELEGRAM, "c1"))
    await repo.save(_make(Platform.LINE, "c1"))
    await repo.save(_make(Platform.TELEGRAM, "c2"))

    removed = await repo.delete_for_character("c1")

    assert removed == 2
    assert await repo.list_for_character("c1") == []
    assert len(await repo.list_for_character("c2")) == 1
