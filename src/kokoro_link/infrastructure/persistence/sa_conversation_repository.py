import json

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, sessionmaker

from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageAttachment,
    MessageContentMode,
    MessageKind,
    MessageRole,
)
from kokoro_link.infrastructure.persistence.models import ConversationRow, MessageRow


class SAConversationRepository(ConversationRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self._session_factory() as session:
            stmt = (
                select(ConversationRow)
                .where(ConversationRow.id == conversation_id)
                .options(selectinload(ConversationRow.messages))
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_domain(row)

    async def latest_for_character(
        self, character_id: str, *, source: str | None = "web",
    ) -> Conversation | None:
        """Return the conversation with the most recent message activity.

        Uses ``MAX(MessageRow.id)`` as the recency proxy (MessageRow.id is
        autoincrement). Conversations without any message fall back to the
        one saved most recently per id ordering.
        """
        async with self._session_factory() as session:
            latest_msg_subq = (
                select(
                    MessageRow.conversation_id.label("conv_id"),
                    func.max(MessageRow.id).label("last_msg_id"),
                )
                .group_by(MessageRow.conversation_id)
                .subquery()
            )
            stmt = (
                select(ConversationRow)
                .outerjoin(latest_msg_subq, latest_msg_subq.c.conv_id == ConversationRow.id)
                .where(ConversationRow.character_id == character_id)
                .order_by(latest_msg_subq.c.last_msg_id.desc().nullslast(), ConversationRow.id.desc())
                .options(selectinload(ConversationRow.messages))
                .limit(1)
            )
            if source is not None:
                stmt = stmt.where(ConversationRow.source == source)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return _row_to_domain(row)

    async def recent_messages_for_character(
        self,
        character_id: str,
        *,
        limit: int,
        exclude_tool_only: bool = False,
    ) -> list[Message]:
        """Merge the character's messages across every source (web /
        telegram / line / …) into a single chronological tail.

        SQL strategy: descending ``created_at`` LIMIT, then reverse in
        Python so the caller still gets oldest-first. Tiebreak on
        ``MessageRow.id`` (autoincrement, insertion order) so two
        messages saved with the same wall-clock instant don't reorder
        on every read.
        """
        if limit <= 0:
            return []
        async with self._session_factory() as session:
            stmt = (
                select(MessageRow)
                .join(ConversationRow, ConversationRow.id == MessageRow.conversation_id)
                .where(ConversationRow.character_id == character_id)
                .order_by(MessageRow.created_at.desc(), MessageRow.id.desc())
                .limit(limit)
            )
            if exclude_tool_only:
                stmt = stmt.where(MessageRow.kind != "tool_only")
            rows = list((await session.execute(stmt)).scalars().all())
            rows.reverse()
            return [_message_row_to_domain(r) for r in rows]

    async def has_user_message_for_character(self, character_id: str) -> bool:
        async with self._session_factory() as session:
            stmt = (
                select(MessageRow.id)
                .join(ConversationRow, ConversationRow.id == MessageRow.conversation_id)
                .where(
                    ConversationRow.character_id == character_id,
                    MessageRow.role == MessageRole.USER.value,
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade-delete all conversations and their messages."""
        async with self._session_factory() as session:
            ids_stmt = select(ConversationRow.id).where(ConversationRow.character_id == character_id)
            ids = list((await session.execute(ids_stmt)).scalars().all())
            if not ids:
                return 0
            await session.execute(delete(MessageRow).where(MessageRow.conversation_id.in_(ids)))
            await session.execute(delete(ConversationRow).where(ConversationRow.id.in_(ids)))
            await session.commit()
            return len(ids)

    async def save(self, conversation: Conversation) -> None:
        async with self._session_factory() as session:
            stmt = (
                select(ConversationRow)
                .where(ConversationRow.id == conversation.id)
                .options(selectinload(ConversationRow.messages))
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

            if row is None:
                row = ConversationRow(
                    id=conversation.id,
                    character_id=conversation.character_id,
                    source=conversation.source,
                )
                session.add(row)
            else:
                row.source = conversation.source

            # Sync messages: replace all with current domain state
            row.messages.clear()
            for position, msg in enumerate(conversation.messages):
                row.messages.append(
                    MessageRow(
                        conversation_id=conversation.id,
                        position=position,
                        role=msg.role.value,
                        content=msg.content,
                        kind=msg.kind.value,
                        content_mode=msg.content_mode.value,
                        safe_summary=msg.safe_summary,
                        attachments_json=_dump_attachments(msg.attachments),
                        created_at=msg.created_at,
                    )
                )
            await session.commit()


def _dump_attachments(attachments: tuple[MessageAttachment, ...]) -> str:
    return json.dumps(
        [
            {
                "kind": a.kind,
                "url": a.url,
                "mime_type": a.mime_type,
                "caption": a.caption,
            }
            for a in attachments
        ],
        ensure_ascii=False,
    )


def _load_attachments(raw: str | None) -> tuple[MessageAttachment, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(data, list):
        return ()
    results: list[MessageAttachment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        url = item.get("url")
        if not isinstance(kind, str) or not isinstance(url, str):
            continue
        mime_type = str(item.get("mime_type") or "application/octet-stream")
        caption = item.get("caption")
        results.append(
            MessageAttachment(
                kind=kind, url=url, mime_type=mime_type,
                caption=caption if isinstance(caption, str) else None,
            ),
        )
    return tuple(results)


def _coerce_kind(raw: str | None) -> MessageKind:
    if not raw:
        return MessageKind.CHAT
    try:
        return MessageKind(raw)
    except ValueError:
        return MessageKind.CHAT


def _coerce_content_mode(raw: str | None) -> MessageContentMode:
    if not raw:
        return MessageContentMode.NORMAL
    try:
        return MessageContentMode(raw)
    except ValueError:
        return MessageContentMode.NORMAL


def _row_to_domain(row: ConversationRow) -> Conversation:
    messages = [_message_row_to_domain(m) for m in sorted(row.messages, key=lambda m: m.position)]
    return Conversation(
        id=row.id,
        character_id=row.character_id,
        messages=messages,
        source=row.source,
    )


def _message_row_to_domain(row: MessageRow) -> Message:
    return Message(
        role=MessageRole(row.role),
        content=row.content,
        attachments=_load_attachments(row.attachments_json),
        kind=_coerce_kind(row.kind),
        content_mode=_coerce_content_mode(getattr(row, "content_mode", None)),
        safe_summary=getattr(row, "safe_summary", "") or "",
        created_at=row.created_at,
    )
