"""Support types for the recoverable external-chat turn service (LH2).

Splits the mechanical pieces out of ``external_chat_turn_service`` so the
orchestrator reads as flow control only:

* :class:`TurnExecutionAdapter` — the concrete
  :class:`ExternalChatTurnExecutionPort` the turn service injects into
  ``ChatService.send_message``. Every conversation write is a CAS
  ``append_messages`` and every receipt mutation is fenced on
  ``owner_id`` + ``lease_version``; a lost lease (stale owner) raises
  :class:`TurnFencingStale` so the whole turn maps to a safe-retry 503.
* :func:`build_response_snapshot` — assembles the byte-stable response
  snapshot (segments / normalised attachment refs / character presentation)
  stored at ``COMMITTED`` and replayed verbatim at ``COMPLETED``. Shared by
  the adapter (normal + busy-defer commit) and the ``GENERATED`` recovery
  finalize so both produce identical bytes.
* generated-snapshot (de)serialisation for the ``GENERATED`` checkpoint so an
  expired-lease takeover rebuilds the assistant turn without re-running the LLM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable
from typing import Any

from kokoro_link.application.services.external_chat_roster_service import (
    RosterCharacterView,
)
from kokoro_link.application.services.outbound_message_segments import (
    split_outbound_text_segments,
)
from kokoro_link.contracts.external_chat_execution import (
    AttachmentSnapshot,
    CommittedTurnRef,
    ExternalChatTurnExecutionPort,
    GeneratedTurnSnapshot,
    PersistedTurnRef,
    PostTurnAnchors,
)
from kokoro_link.contracts.external_chat_turn import ExternalChatTurnReceiptPort
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageAttachment,
    MessageContentMode,
    MessageKind,
    MessageRole,
)

_LOG = logging.getLogger(__name__)


class TurnFencingStale(RuntimeError):
    """A fenced write lost its lease (a stale, taken-over owner).

    The turn service maps this to a retryable 503: the concurrent owner (or a
    later recovery pass) drives the turn; this attempt safely backs off.
    """


def dumps_stable(payload: Any) -> str:
    """Serialise ``payload`` to a deterministic JSON string.

    ``sort_keys`` + fixed separators make the bytes reproducible; storing and
    replaying the *same* string is what guarantees a byte-identical completed
    replay (the stored snapshot is returned verbatim, never re-serialised).
    """
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def error_envelope(*, code: str, message: str, retryable: bool) -> dict[str, Any]:
    """Build the OpenAPI ``ErrorEnvelope`` body."""
    return {"error": {"code": code, "message": message, "retryable": retryable}}


async def run_with_lease_heartbeat(
    external_turn: ExternalChatTurnExecutionPort,
    coro: Awaitable[Any],
) -> Any:
    """Run ``coro`` (the LLM / tool pipeline) while periodically extending the
    turn lease, so a long generation never lets the lease lapse into a
    concurrent takeover (H7).

    A heartbeat fires every ``heartbeat_interval_seconds`` until the work
    finishes. A lost lease — ``heartbeat`` raises :class:`TurnFencingStale` —
    cancels the in-flight work and re-raises, so ChatService performs no further
    write against a turn it no longer owns. The final ``return`` re-raises any
    exception the work itself produced (via ``task.result()``).
    """
    interval = max(0.001, external_turn.heartbeat_interval_seconds())
    task: asyncio.Task[Any] = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if task in done:
                return task.result()
            # Still running past a heartbeat window — extend the lease. A lost
            # lease raises out of here and aborts the turn.
            await external_turn.heartbeat()
    except BaseException:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        raise


async def build_response_snapshot(
    *,
    object_storage: ObjectStoragePort,
    character_view: RosterCharacterView,
    request_id: str,
    turn_id: str,
    conversation_id: str,
    assistant_message: Message,
) -> dict[str, Any]:
    """Assemble the ``ExternalChatTurnResponse`` snapshot dict.

    ``segments`` uses the shared self-host outbound split rule; ``attachments``
    normalises each assistant attachment url back to its object key and stats
    it for authoritative ``media_type`` / ``size_bytes`` / ``sha256`` (an
    unresolvable or un-stat-able attachment is skipped with a warning rather
    than failing the whole turn); ``character`` is the roster's single
    presentation row for this character.
    """
    segments = [
        {"text": segment}
        for segment in split_outbound_text_segments(assistant_message.content)
    ]
    attachments = await _resolve_attachment_refs(
        object_storage, assistant_message.attachments,
    )
    character = {
        "character_id": character_view.character_id,
        "display_name": character_view.display_name,
        "background_frozen": character_view.background_frozen,
        "avatar_object_ref": character_view.avatar_object_ref,
        "presentation_version": character_view.presentation_version,
    }
    return {
        "request_id": request_id,
        "turn_id": turn_id,
        "conversation_id": conversation_id,
        "segments": segments,
        "attachments": attachments,
        "character": character,
    }


async def _resolve_attachment_refs(
    object_storage: ObjectStoragePort,
    attachments: tuple[MessageAttachment, ...],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for attachment in attachments:
        key = object_storage.object_key_from_url(attachment.url)
        if not key:
            _LOG.warning(
                "external-chat turn: unresolvable attachment url, skipped",
            )
            continue
        try:
            metadata = await object_storage.stat(object_key=key)
        except Exception:  # noqa: BLE001 - defensive; skip a bad attachment
            _LOG.warning(
                "external-chat turn: attachment stat failed, skipped",
                exc_info=True,
            )
            continue
        if metadata is None or not metadata.sha256:
            _LOG.warning(
                "external-chat turn: attachment metadata missing, skipped",
            )
            continue
        refs.append({
            "object_ref": key,
            "media_type": metadata.content_type,
            "size_bytes": metadata.size_bytes,
            "sha256": metadata.sha256,
        })
    return refs


def generated_snapshot_to_json(snapshot: GeneratedTurnSnapshot) -> str:
    """Serialise the ``GENERATED`` checkpoint for durable storage."""
    return dumps_stable({
        "turn_id": snapshot.turn_id,
        "assistant_text": snapshot.assistant_text,
        "attachments": [
            {
                "kind": item.kind,
                "url": item.url,
                "mime_type": item.mime_type,
                "caption": item.caption,
            }
            for item in snapshot.attachments
        ],
        "assistant_kind": snapshot.assistant_kind,
        "content_mode": snapshot.content_mode,
    })


def rebuild_assistant_from_generated(generated_snapshot_json: str) -> Message:
    """Rebuild the assistant :class:`Message` from a ``GENERATED`` checkpoint,
    so a takeover finalises without ever re-running the LLM."""
    data = json.loads(generated_snapshot_json)
    attachments = tuple(
        MessageAttachment(
            kind=item["kind"],
            url=item["url"],
            mime_type=item.get("mime_type", "application/octet-stream"),
            caption=item.get("caption"),
        )
        for item in data.get("attachments", ())
    )
    return Message(
        role=MessageRole.ASSISTANT,
        content=data["assistant_text"],
        attachments=attachments,
        kind=MessageKind(data.get("assistant_kind", MessageKind.CHAT.value)),
        content_mode=MessageContentMode(
            data.get("content_mode", MessageContentMode.NORMAL.value),
        ),
    )


class TurnExecutionAdapter(ExternalChatTurnExecutionPort):
    """Durable execution seam bound to one claimed turn.

    Holds the fencing coordinates (``turn_id`` / ``owner_id`` /
    ``lease_version``) and the resolved presentation context so every write
    ChatService delegates lands on the exactly-once, fenced spine.
    """

    def __init__(
        self,
        *,
        receipts: ExternalChatTurnReceiptPort,
        conversations: ConversationRepositoryPort,
        object_storage: ObjectStoragePort,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        request_id: str,
        conversation_id: str,
        character_view: RosterCharacterView,
        user_message_row_id: int | None = None,
        lease_seconds: float = 300.0,
    ) -> None:
        self._receipts = receipts
        self._conversations = conversations
        self._object_storage = object_storage
        self._turn_id = turn_id
        self._owner_id = owner_id
        self._lease_version = lease_version
        self._lease_seconds = lease_seconds
        self._request_id = request_id
        self._conversation_id = conversation_id
        self._character_view = character_view
        self._user_row_id = user_message_row_id
        self._user_position: int | None = None
        self._assistant_row_id: int | None = None
        self._assistant_position: int | None = None
        self._response_snapshot_json: str | None = None

    @property
    def response_snapshot_json(self) -> str | None:
        return self._response_snapshot_json

    async def persist_user_turn(
        self, conversation: Conversation, message: Message,
    ) -> PersistedTurnRef:
        # Recovery: the user row was already durably appended on a prior
        # attempt (receipt carried its id). Never rewrite it — return the
        # existing ref. The user turn is the durable tail (the assistant has
        # not been appended yet), so its position is the last index.
        if self._user_row_id is not None:
            if self._user_position is None:
                self._user_position = len(conversation.messages) - 1
            return PersistedTurnRef(
                row_id=self._user_row_id, position=self._user_position,
            )
        expected = len(conversation.messages)
        # B1: the append is keyed on the stable turn id, so a takeover that
        # re-drives after a crash between this durable append and the
        # ``record_user_turn`` below adopts the SAME row instead of appending a
        # duplicate user turn. The receipt record is then re-applied idempotently.
        result = await self._conversations.append_messages(
            conversation.id,
            expected_next_position=expected,
            messages=[message],
            idempotency_key=f"{self._turn_id}:user",
        )
        if not result.ok:
            raise TurnFencingStale("user turn append conflict")
        row_id = result.row_ids[0]
        position = result.positions[0]
        recorded = await self._receipts.record_user_turn(
            turn_id=self._turn_id,
            owner_id=self._owner_id,
            lease_version=self._lease_version,
            user_message_row_id=row_id,
        )
        if not recorded:
            raise TurnFencingStale("record_user_turn fenced out")
        self._user_row_id = row_id
        self._user_position = position
        return PersistedTurnRef(row_id=row_id, position=position)

    async def checkpoint_generated(
        self, generated: GeneratedTurnSnapshot,
    ) -> None:
        marked = await self._receipts.mark_generated(
            turn_id=self._turn_id,
            owner_id=self._owner_id,
            lease_version=self._lease_version,
            generated_snapshot_json=generated_snapshot_to_json(generated),
        )
        if not marked:
            raise TurnFencingStale("mark_generated fenced out")

    async def commit_assistant_turn(
        self, conversation: Conversation, assistant_message: Message,
    ) -> CommittedTurnRef:
        return await self._append_and_commit(assistant_message)

    async def commit_deferred_reply(
        self,
        conversation: Conversation,
        user_message: Message,
        assistant_message: Message,
    ) -> CommittedTurnRef:
        # The busy-defer branch's sole conversation write-exit. The user turn
        # was already persisted by ``persist_user_turn`` before the defer
        # decision; only append it here in the defensive case it was not.
        if self._user_row_id is None:
            await self.persist_user_turn(conversation, user_message)
        # GENERATED-checkpoint the brief ack so a crash before the commit still
        # finalises from the snapshot (parity with the normal path).
        await self.checkpoint_generated(
            GeneratedTurnSnapshot(
                turn_id=self._turn_id,
                assistant_text=assistant_message.content,
                attachments=tuple(
                    AttachmentSnapshot(
                        kind=item.kind,
                        url=item.url,
                        mime_type=item.mime_type,
                        caption=item.caption,
                    )
                    for item in assistant_message.attachments
                ),
                assistant_kind=assistant_message.kind.value,
                content_mode=assistant_message.content_mode.value,
            ),
        )
        return await self._append_and_commit(assistant_message)

    async def _append_and_commit(
        self, assistant_message: Message,
    ) -> CommittedTurnRef:
        expected = (self._user_position or 0) + 1
        result = await self._conversations.append_messages(
            self._conversation_id,
            expected_next_position=expected,
            messages=[assistant_message],
            # B1: keyed so a takeover between this append and ``mark_committed``
            # adopts the same assistant row instead of double-appending.
            idempotency_key=f"{self._turn_id}:assistant",
        )
        if not result.ok:
            raise TurnFencingStale("assistant turn append conflict")
        row_id = result.row_ids[0]
        position = result.positions[0]
        snapshot_json = dumps_stable(
            await self._build_snapshot(assistant_message),
        )
        committed = await self._receipts.mark_committed(
            turn_id=self._turn_id,
            owner_id=self._owner_id,
            lease_version=self._lease_version,
            assistant_message_row_id=row_id,
            assistant_position=position,
            response_snapshot_json=snapshot_json,
        )
        if not committed:
            raise TurnFencingStale("mark_committed fenced out")
        self._assistant_row_id = row_id
        self._assistant_position = position
        self._response_snapshot_json = snapshot_json
        return CommittedTurnRef(row_id=row_id, position=position)

    async def _build_snapshot(
        self, assistant_message: Message,
    ) -> dict[str, Any]:
        return await build_response_snapshot(
            object_storage=self._object_storage,
            character_view=self._character_view,
            request_id=self._request_id,
            turn_id=self._turn_id,
            conversation_id=self._conversation_id,
            assistant_message=assistant_message,
        )

    def stable_turn_id(self) -> str:
        return self._turn_id

    def post_turn_anchors(self) -> PostTurnAnchors:
        return PostTurnAnchors(
            user_message_row_id=self._user_row_id or 0,
            assistant_message_row_id=self._assistant_row_id or 0,
            assistant_position=(
                self._assistant_position
                if self._assistant_position is not None
                else 0
            ),
        )

    async def heartbeat(self) -> None:
        alive = await self._receipts.heartbeat_lease(
            turn_id=self._turn_id,
            owner_id=self._owner_id,
            lease_version=self._lease_version,
        )
        if not alive:
            raise TurnFencingStale("lease heartbeat lost")

    def heartbeat_interval_seconds(self) -> float:
        # A third of the lease TTL: three chances to refresh before a lapse.
        return max(1.0, self._lease_seconds / 3.0)
