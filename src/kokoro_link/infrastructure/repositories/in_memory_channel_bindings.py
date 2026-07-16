"""In-process channel binding repository for dev/tests."""

from kokoro_link.contracts.messaging import ChannelBindingRepositoryPort
from kokoro_link.domain.entities.channel_binding import ChannelBinding


class InMemoryChannelBindingRepository(ChannelBindingRepositoryPort):
    def __init__(self) -> None:
        self._by_id: dict[str, ChannelBinding] = {}

    async def get(self, binding_id: str) -> ChannelBinding | None:
        return self._by_id.get(binding_id)

    async def find(
        self, account_id: str, chat_ref: str,
    ) -> ChannelBinding | None:
        for binding in self._by_id.values():
            if binding.account_id == account_id and binding.chat_ref == chat_ref:
                return binding
        return None

    async def list_for_account(self, account_id: str) -> list[ChannelBinding]:
        items = [b for b in self._by_id.values() if b.account_id == account_id]
        items.sort(key=lambda b: b.created_at)
        return items

    async def save(self, binding: ChannelBinding) -> None:
        self._by_id[binding.id] = binding

    async def delete(self, binding_id: str) -> bool:
        return self._by_id.pop(binding_id, None) is not None

    async def delete_for_account(self, account_id: str) -> int:
        victims = [
            bid for bid, b in self._by_id.items() if b.account_id == account_id
        ]
        for bid in victims:
            del self._by_id[bid]
        return len(victims)
