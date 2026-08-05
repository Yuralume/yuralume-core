from dataclasses import replace

from kokoro_link.contracts.repositories import (
    AppendResult,
    ConversationMessagePage,
    ConversationRepositoryPort,
)
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)


def _same_message(a: Message, b: Message) -> bool:
    """Whether two messages are the same turn (role + content) — used to tell a
    writer's own re-saved tail from a concurrent append (B3)."""
    return a.role == b.role and a.content == b.content


class InMemoryConversationRepository(ConversationRepositoryPort):
    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}
        # Parallel list preserving insertion/update order so
        # ``latest_for_character`` can return the most recent one
        # without needing timestamps.
        self._order: list[str] = []
        # Monotonic surrogate for the SA autoincrement message row id so the
        # ``append_messages`` twin can hand back stable ``row_ids``.
        self._row_seq = 0
        # B1 twin: per-conversation idempotency map key -> (row_id, position),
        # mirroring the SA ``uq_messages_conversation_idempotency`` behaviour.
        self._idempotency: dict[str, dict[str, tuple[int, int]]] = {}

    async def get(self, conversation_id: str) -> Conversation | None:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return None
        return self._stamp_read(conversation)

    def _stamp_read(self, conversation: Conversation) -> Conversation:
        """Stamp the read-time B3 snapshot boundary onto a returned snapshot,
        mirroring the SA repo's ``_row_to_domain``."""
        return replace(
            conversation,
            loaded_message_count=len(conversation.messages),
        )

    async def save(
        self, conversation: Conversation, *, truncation: bool = False,
    ) -> None:
        existing = self._conversations.get(conversation.id)
        if existing is None:
            merged = replace(conversation, version=1)
        else:
            messages = list(conversation.messages)
            # B3 parity: a stale save (a write bumped the stored row after this
            # snapshot loaded) preserves genuine concurrent appends but NOT this
            # writer's own re-saved tail. An authoritative truncation (undo)
            # skips the merge entirely.
            if conversation.version != existing.version and not truncation:
                boundary = conversation.loaded_message_count
                if boundary < 0:
                    boundary = len(conversation.messages)
                keyed_positions = {
                    pos
                    for _key, (_rid, pos) in self._idempotency.get(
                        conversation.id, {},
                    ).items()
                }
                for idx, stored in enumerate(existing.messages):
                    if idx < boundary:
                        continue
                    # This writer's own earlier save (skip) only when the tail
                    # row is un-keyed AND identical to the writer's message at
                    # that position; a keyed row or a divergent one is a genuine
                    # concurrent append that must survive.
                    if (
                        idx not in keyed_positions
                        and idx < len(conversation.messages)
                        and _same_message(stored, conversation.messages[idx])
                    ):
                        continue
                    messages.append(stored)
            merged = replace(
                conversation, messages=messages, version=existing.version + 1,
            )
        if conversation.id in self._conversations:
            self._order.remove(conversation.id)
        self._conversations[conversation.id] = merged
        self._order.append(conversation.id)

    def _select_latest(
        self, character_id: str, source: str | None,
    ) -> Conversation | None:
        """Which conversation is "the latest" — the single source of that
        answer for both the full read and the paginated one, mirroring the SA
        twin's shared statement so pagination cannot drift the selection."""
        for conv_id in reversed(self._order):
            conversation = self._conversations[conv_id]
            if conversation.character_id != character_id:
                continue
            if source is not None and conversation.source != source:
                continue
            return conversation
        return None

    async def latest_for_character(
        self, character_id: str, *, source: str | None = "web",
    ) -> Conversation | None:
        conversation = self._select_latest(character_id, source)
        if conversation is None:
            return None
        return self._stamp_read(conversation)

    async def latest_page_for_character(
        self,
        character_id: str,
        *,
        source: str | None = "web",
        limit: int,
        before_position: int | None = None,
    ) -> ConversationMessagePage | None:
        """SA twin (IV10). ``position`` is the list index here: the CAS append
        assigns positions from ``len(messages)``, so index and position are the
        same number and the cursor arithmetic is identical to the SQL one."""
        conversation = self._select_latest(character_id, source)
        if conversation is None:
            return None
        end = (
            len(conversation.messages)
            if before_position is None
            else max(0, min(before_position, len(conversation.messages)))
        )
        if limit <= 0:
            return ConversationMessagePage(
                conversation_id=conversation.id,
                character_id=conversation.character_id,
                messages=(),
                has_more=False,
                next_before=None,
            )
        start = max(0, end - limit)
        has_more = start > 0
        return ConversationMessagePage(
            conversation_id=conversation.id,
            character_id=conversation.character_id,
            messages=tuple(conversation.messages[start:end]),
            has_more=has_more,
            next_before=start if has_more else None,
        )

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

    async def append_messages(
        self,
        conversation_id: str,
        *,
        expected_next_position: int,
        messages: list[Message],
        idempotency_key: str | None = None,
    ) -> AppendResult:
        """Same CAS semantics as the SA twin: position-checked tail append,
        never a whole-conversation replace. ``idempotency_key`` mirrors the SA
        unique-index behaviour: a repeat keyed append adopts the existing row
        (B1)."""
        if not messages:
            return AppendResult(ok=True, conflict=False)
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            return AppendResult(ok=False, conflict=True)
        if idempotency_key is not None:
            existing = self._idempotency.get(conversation_id, {}).get(
                idempotency_key,
            )
            if existing is not None:
                row_id, position = existing
                return AppendResult(
                    ok=True, conflict=False,
                    row_ids=(row_id,), positions=(position,),
                )
        next_position = len(conversation.messages)
        if next_position != expected_next_position:
            return AppendResult(ok=False, conflict=True)
        updated = conversation
        row_ids: list[int] = []
        positions: list[int] = []
        for offset, msg in enumerate(messages):
            updated = updated.append(msg)
            self._row_seq += 1
            row_ids.append(self._row_seq)
            positions.append(next_position + offset)
        # B3: bump the stored version so a concurrent whole-conversation
        # ``save()`` loaded before this append detects the mismatch and merges.
        updated = replace(updated, version=conversation.version + 1)
        self._conversations[conversation_id] = updated
        if idempotency_key is not None:
            self._idempotency.setdefault(conversation_id, {})[idempotency_key] = (
                row_ids[0], positions[0],
            )
        return AppendResult(
            ok=True,
            conflict=False,
            row_ids=tuple(row_ids),
            positions=tuple(positions),
        )

    async def delete_for_character(self, character_id: str) -> int:
        victims = [cid for cid, conv in self._conversations.items() if conv.character_id == character_id]
        for cid in victims:
            self._conversations.pop(cid, None)
            if cid in self._order:
                self._order.remove(cid)
        return len(victims)
