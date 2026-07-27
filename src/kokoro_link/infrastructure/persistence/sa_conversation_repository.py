import json
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, sessionmaker

from kokoro_link.contracts.repositories import AppendResult, ConversationRepositoryPort
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageAttachment,
    MessageContentMode,
    MessageKind,
    MessageRole,
)
from kokoro_link.infrastructure.persistence.models import ConversationRow, MessageRow

_LOG = logging.getLogger(__name__)


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

    async def append_messages(
        self,
        conversation_id: str,
        *,
        expected_next_position: int,
        messages: list[Message],
        idempotency_key: str | None = None,
    ) -> AppendResult:
        """CAS-append rows to a conversation tail without the replace path.

        Locks the conversation row (``FOR UPDATE``) so a concurrent appender
        serialises behind it, recomputes the tail (``MAX(position) + 1``) and
        only inserts when it equals ``expected_next_position``. Rows are
        inserted individually — the existing tail is never cleared — so a
        concurrent turn's rows can never be lost the way the whole-conversation
        ``save()`` replace would drop them. Each append also bumps the
        conversation ``version`` (B3) so a stale ``save()`` can detect the tail
        growth.

        B1: when ``idempotency_key`` is supplied and a message already carries
        it on this conversation, the existing row is returned unchanged — a
        takeover re-appending the same turn adopts the original row instead of
        writing a duplicate.
        """
        if not messages:
            return AppendResult(ok=True, conflict=False)
        async with self._session_factory() as session:
            conv_row = (
                await session.execute(
                    select(ConversationRow)
                    .where(ConversationRow.id == conversation_id)
                    .with_for_update(),
                )
            ).scalar_one_or_none()
            if conv_row is None:
                # The turn service resolves / creates the conversation before
                # driving the seam; an absent row here is a caller contract
                # violation, surfaced as a non-writing conflict.
                return AppendResult(ok=False, conflict=True)
            if idempotency_key is not None:
                existing = (
                    await session.execute(
                        select(MessageRow)
                        .where(
                            MessageRow.conversation_id == conversation_id,
                            MessageRow.idempotency_key == idempotency_key,
                        )
                        .order_by(MessageRow.position),
                    )
                ).scalars().first()
                if existing is not None:
                    # Already durably appended by a prior attempt of this turn.
                    return AppendResult(
                        ok=True, conflict=False,
                        row_ids=(existing.id,), positions=(existing.position,),
                    )
            max_position = (
                await session.execute(
                    select(func.max(MessageRow.position)).where(
                        MessageRow.conversation_id == conversation_id,
                    ),
                )
            ).scalar_one_or_none()
            next_position = 0 if max_position is None else max_position + 1
            if next_position != expected_next_position:
                return AppendResult(ok=False, conflict=True)
            rows: list[MessageRow] = []
            for offset, msg in enumerate(messages):
                row = _new_message_row(
                    conversation_id, next_position + offset, msg,
                    # The idempotency key scopes a single-message append; only
                    # the first row carries it.
                    idempotency_key=idempotency_key if offset == 0 else None,
                )
                session.add(row)
                rows.append(row)
            conv_row.version = (conv_row.version or 0) + 1
            await session.flush()
            row_ids = tuple(r.id for r in rows)
            positions = tuple(r.position for r in rows)
            await session.commit()
            return AppendResult(
                ok=True, conflict=False, row_ids=row_ids, positions=positions,
            )

    async def save(
        self, conversation: Conversation, *, truncation: bool = False,
    ) -> None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ConversationRow)
                    .where(ConversationRow.id == conversation.id)
                    .options(selectinload(ConversationRow.messages))
                    .with_for_update(),
                )
            ).scalar_one_or_none()

            if row is None:
                row = ConversationRow(
                    id=conversation.id,
                    character_id=conversation.character_id,
                    source=conversation.source,
                    version=1,
                )
                session.add(row)
                for position, msg in enumerate(conversation.messages):
                    row.messages.append(
                        _new_message_row(conversation.id, position, msg),
                    )
                await session.commit()
                return

            row.source = conversation.source
            db_version = row.version or 0
            # B3: a version mismatch means a write landed AFTER this snapshot was
            # loaded. That write is EITHER a concurrent CAS ``append_messages``
            # (whose tail row must be preserved — dropping it is the silent
            # lost-update this guard exists to prevent) OR this same writer's own
            # follow-up whole-conversation save (the web path persists the user
            # turn, then the full turn) — which merely re-writes the same tail
            # and must NOT be mistaken for a concurrent append. An equal version
            # means no writer touched the row, so the replace applies verbatim.
            stale = conversation.version != db_version

            # Only rows beyond the snapshot's read-time length can be concurrent
            # appends (a CAS append only grows the tail past the snapshot). Fall
            # back to the write length for a snapshot with no recorded boundary.
            boundary = conversation.loaded_message_count
            if boundary < 0:
                boundary = len(conversation.messages)
            web_messages = conversation.messages

            # R-B3: the concurrent-append set. A tail row is THIS writer's own
            # (skip) only when it is un-keyed AND identical to the writer's
            # message at that position — i.e. its own earlier durable save. A
            # keyed row (every durable CAS append is keyed) or an un-keyed row
            # the writer did not write is a genuine concurrent append and is
            # preserved. An authoritative truncation (undo) skips the merge
            # entirely — truncation wins over a racing append.
            preserved: list[tuple[Message, str | None]] = []
            preserved_positions: set[int] = set()
            if stale and not truncation:
                for m in sorted(row.messages, key=lambda r: r.position):
                    if m.position < boundary:
                        continue
                    if (
                        m.idempotency_key is None
                        and m.position < len(web_messages)
                        and _same_persisted_message(
                            m, web_messages[m.position],
                        )
                    ):
                        continue
                    preserved.append(
                        (_message_row_to_domain(m), m.idempotency_key),
                    )
                    preserved_positions.add(m.position)
                if preserved:
                    _LOG.warning(
                        "conversation %s save() is stale (loaded v%s, now v%s); "
                        "merging %d concurrently-appended tail message(s) not in "
                        "this snapshot instead of dropping them",
                        conversation.id, conversation.version, db_version,
                        len(preserved),
                    )

            # B1: carry each surviving DB row's idempotency key back onto the
            # rebuilt row at the same position so a durable keyed append survives
            # a whole-conversation replace and a re-append under the same key
            # still adopts the existing row. Concurrent-append rows are excluded
            # here — their keys travel with the preserved messages reflowed onto
            # the tail below.
            original_keys_by_position = {
                m.position: m.idempotency_key
                for m in row.messages
                if m.idempotency_key is not None
                and m.position not in preserved_positions
            }

            # Rebuild: drop every existing row and flush before re-inserting the
            # same positions so the (conversation_id, position) unique index is
            # never transiently violated mid-flush.
            for existing in list(row.messages):
                row.messages.remove(existing)
            await session.flush()
            next_position = 0
            for position, msg in enumerate(conversation.messages):
                row.messages.append(
                    _new_message_row(
                        conversation.id, position, msg,
                        idempotency_key=original_keys_by_position.get(position),
                    ),
                )
                next_position = position + 1
            # Reflow preserved concurrent appends onto the new tail (after the
            # replaced messages), carrying their original idempotency keys.
            for msg, key in preserved:
                row.messages.append(
                    _new_message_row(
                        conversation.id, next_position, msg, idempotency_key=key,
                    ),
                )
                next_position += 1
            row.version = db_version + 1
            await session.commit()


def _same_persisted_message(row: MessageRow, msg: Message) -> bool:
    """Whether a persisted row is the same message the writer holds at that
    position — used to tell this writer's own re-saved tail from a concurrent
    append (B3). Role + content is a stable identity here (the web writer never
    edits a message between its user-turn and full-turn saves)."""
    return row.role == msg.role.value and row.content == msg.content


def _new_message_row(
    conversation_id: str,
    position: int,
    msg: Message,
    *,
    idempotency_key: str | None = None,
) -> MessageRow:
    return MessageRow(
        conversation_id=conversation_id,
        position=position,
        role=msg.role.value,
        content=msg.content,
        kind=msg.kind.value,
        content_mode=msg.content_mode.value,
        safe_summary=msg.safe_summary,
        attachments_json=_dump_attachments(msg.attachments),
        created_at=msg.created_at,
        idempotency_key=idempotency_key,
    )


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
    ordered = sorted(row.messages, key=lambda m: m.position)
    messages = [_message_row_to_domain(m) for m in ordered]
    return Conversation(
        id=row.id,
        character_id=row.character_id,
        messages=messages,
        source=row.source,
        version=row.version or 0,
        # B3: capture the read-time boundary so a later stale save() only treats
        # rows BEYOND the loaded tail as candidate concurrent appends, not the
        # snapshot prefix it is re-writing.
        loaded_message_count=len(ordered),
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
