import pytest

from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.infrastructure.repositories.in_memory_channel_bindings import (
    InMemoryChannelBindingRepository,
)


@pytest.mark.asyncio
async def test_save_and_get_roundtrip() -> None:
    repo = InMemoryChannelBindingRepository()
    binding = ChannelBinding.create(account_id="acct-1", chat_ref="c1")
    await repo.save(binding)

    assert await repo.get(binding.id) == binding


@pytest.mark.asyncio
async def test_find_by_account_and_chat_ref() -> None:
    repo = InMemoryChannelBindingRepository()
    a = ChannelBinding.create(account_id="acct-1", chat_ref="c1")
    b = ChannelBinding.create(account_id="acct-2", chat_ref="c1")
    for binding in (a, b):
        await repo.save(binding)

    assert await repo.find("acct-1", "c1") == a
    assert await repo.find("acct-2", "c1") == b
    assert await repo.find("acct-1", "c-missing") is None


@pytest.mark.asyncio
async def test_list_for_account() -> None:
    repo = InMemoryChannelBindingRepository()
    a = ChannelBinding.create(account_id="acct-1", chat_ref="c1")
    b = ChannelBinding.create(account_id="acct-1", chat_ref="c2")
    other = ChannelBinding.create(account_id="acct-2", chat_ref="c1")
    for binding in (a, b, other):
        await repo.save(binding)

    listed = await repo.list_for_account("acct-1")
    assert {x.id for x in listed} == {a.id, b.id}


@pytest.mark.asyncio
async def test_delete_and_delete_for_account() -> None:
    repo = InMemoryChannelBindingRepository()
    a = ChannelBinding.create(account_id="acct-1", chat_ref="c1")
    b = ChannelBinding.create(account_id="acct-1", chat_ref="c2")
    other = ChannelBinding.create(account_id="acct-2", chat_ref="c1")
    for binding in (a, b, other):
        await repo.save(binding)

    assert await repo.delete(a.id) is True
    assert await repo.get(a.id) is None

    removed = await repo.delete_for_account("acct-1")
    assert removed == 1  # only b remained on acct-1
    assert await repo.list_for_account("acct-1") == []
    assert len(await repo.list_for_account("acct-2")) == 1
