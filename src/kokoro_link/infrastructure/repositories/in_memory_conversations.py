from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)


class InMemoryConversationRepository(ConversationRepositoryPort):
    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        # Parallel list preserving insertion/update order so
        # ``latest_for_character`` can return the most recent one
        # without needing timestamps.
        self._order: list[str] = []

    async def get(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    async def save(self, conversation: Conversation) -> None:
        if conversation.id in self._conversations:
            self._order.remove(conversation.id)
        self._conversations[conversation.id] = conversation
        self._order.append(conversation.id)

    async def latest_for_character(
        self, character_id: str, *, source: str | None = "web",
    ) -> Conversation | None:
        for conv_id in reversed(self._order):
            conversation = self._conversations[conv_id]
            if conversation.character_id != character_id:
                continue
            if source is not None and conversation.source != source:
                continue
            return conversation
        return None

    async def recent_messages_for_character(
        self,
        character_id: str,
        *,
        limit: int,
        exclude_tool_only: bool = False,
    ) -> list[Message]:
        pool: list[Message] = []
        for conversation in self._conversations.values():
            if conversation.character_id != character_id:
                continue
            for msg in conversation.messages:
                if exclude_tool_only and msg.kind is MessageKind.TOOL_ONLY:
                    continue
                pool.append(msg)
        pool.sort(key=lambda m: m.created_at)
        if limit <= 0:
            return []
        return pool[-limit:]

    async def has_user_message_for_character(self, character_id: str) -> bool:
        return any(
            msg.role is MessageRole.USER
            for conversation in self._conversations.values()
            if conversation.character_id == character_id
            for msg in conversation.messages
        )

    async def delete_for_character(self, character_id: str) -> int:
        victims = [cid for cid, conv in self._conversations.items() if conv.character_id == character_id]
        for cid in victims:
            self._conversations.pop(cid, None)
            if cid in self._order:
                self._order.remove(cid)
        return len(victims)
