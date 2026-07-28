"""Recoverable external-chat turn orchestrator (LH2, DR-LH0-004).

The single entry point (:meth:`ExternalChatTurnService.execute`) for the
hosted LINE official-channel Cloud service's ``POST .../external-chat/turns``.
It turns one wire request into an outcome the route serialises verbatim, while
driving the turn through the durable receipt state machine so a retried /
crashed delivery converges on exactly one user turn, one assistant turn and one
byte-identical response snapshot:

    canonical hash → claim_or_get → {replay | in-progress | recover | conflict}
      Claimed → guards → resolve/self-heal conversation → ChatService (one LLM)
              → GENERATED checkpoint → COMMITTED (CAS append + snapshot)
              → COMPLETED

Every conversation write goes through the fenced :class:`TurnExecutionAdapter`
(never the whole-conversation ``save()``), and the LLM is run at most once —
a ``GENERATED`` or ``COMMITTED`` recovery finalises from the stored snapshot
and never re-calls ChatService.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from kokoro_link.application.dto.chat import PresenceFramePayload, SendChatMessageRequest
from kokoro_link.application.dto.external_chat import ExternalChatTurnCommand
from kokoro_link.application.services.chat_service import (
    ChatRuntimeLimitExceeded,
    ChatService,
    ChatSubscriptionFrozen,
)
from kokoro_link.application.services.external_chat.canonical_hash import (
    CANONICAL_HASH_VERSION,
    compute_canonical_request_hash,
)
from kokoro_link.application.services.external_chat.line_conversation import (
    resolve_or_create_line_conversation,
)
from kokoro_link.application.services.external_chat_attachment_service import (
    inbound_object_key_prefix,
)
from kokoro_link.application.services.external_chat.turn_support import (
    TurnExecutionAdapter,
    TurnFencingStale,
    build_response_snapshot,
    dumps_stable,
    error_envelope,
    rebuild_assistant_from_generated,
)
from kokoro_link.application.services.external_chat_roster_service import (
    ExternalChatRosterService,
    RosterCharacterView,
    RosterView,
    resolve_cloud_operator,
)
from kokoro_link.contracts.external_chat_turn import (
    Claimed,
    CommittedRecovery,
    CompletedReplay,
    ExternalChatTurnReceipt,
    ExternalChatTurnReceiptPort,
    ExternalChatTurnState,
    GeneratedRecovery,
    HashConflict,
    InProgress,
    TerminalReplay,
)
from kokoro_link.contracts.object_storage import (
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStoragePort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.presence_frame import PresenceFrame

_LOG = logging.getLogger(__name__)

# Idempotency is scoped to the single internal caller that owns this surface
# (the credential gate already fixes ``caller=cloud-channel``); the Channel's
# ``request_id`` is globally unique per delivered event, and a reuse with a
# different logical request is caught by the canonical-hash gate as a 409.
_AUTHENTICATED_CALLER = "cloud-channel"

_RETRY_AFTER_SECONDS = 3
_OPAQUE_NOT_FOUND_MESSAGE = "not found"


@dataclass(frozen=True, slots=True)
class TurnResult:
    """The turn outcome the route serialises.

    ``body_json`` is a raw JSON string returned verbatim (a completed replay
    returns the *stored* snapshot bytes unchanged); ``None`` means an empty
    body (the 202 in-progress case). ``retry_after_seconds`` sets the
    ``Retry-After`` header when present.
    """

    status_code: int
    body_json: str | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class _Fence:
    turn_id: str
    owner_id: str
    lease_version: int


class ExternalChatTurnService:
    def __init__(
        self,
        *,
        receipt_repository: ExternalChatTurnReceiptPort,
        chat_service: ChatService,
        conversation_repository: ConversationRepositoryPort,
        roster_service: ExternalChatRosterService,
        operator_profile_repository: OperatorProfileRepositoryPort,
        object_storage: ObjectStoragePort,
    ) -> None:
        self._receipts = receipt_repository
        self._chat_service = chat_service
        self._conversations = conversation_repository
        self._roster = roster_service
        self._operators = operator_profile_repository
        self._object_storage = object_storage

    async def execute(self, command: ExternalChatTurnCommand) -> TurnResult:
        canonical_hash = compute_canonical_request_hash(
            _build_canonical_payload(command),
        )
        result = await self._receipts.claim_or_get(
            authenticated_caller=_AUTHENTICATED_CALLER,
            request_id=command.request_id,
            canonical_request_hash=canonical_hash,
            canonical_hash_version=CANONICAL_HASH_VERSION,
            contract_version=command.contract_version,
            tenant_id=command.tenant_id,
            account_id=command.account_id,
            character_id=command.character_id,
            requested_conversation_id=command.conversation_id,
        )
        match result:
            case HashConflict():
                return _hash_conflict_result()
            case InProgress():
                return TurnResult(
                    202, None, retry_after_seconds=_RETRY_AFTER_SECONDS,
                )
            case CompletedReplay(response_snapshot_json=snapshot):
                return _completed_result(snapshot)
            case TerminalReplay(terminal_error_json=terminal):
                return _replay_terminal(terminal)
            case CommittedRecovery(receipt=receipt):
                return await self._finalize_committed(receipt)
            case GeneratedRecovery(receipt=receipt):
                return await self._finalize_generated(receipt, command)
            case Claimed(receipt=receipt):
                return await self._drive(receipt, command)
        # ``claim_or_get`` returns a sealed union; the match is exhaustive.
        raise AssertionError(  # pragma: no cover
            f"unhandled claim_or_get result {result!r}",
        )

    # -- Claimed: full drive ------------------------------------------------ #

    async def _drive(
        self, receipt: ExternalChatTurnReceipt, command: ExternalChatTurnCommand,
    ) -> TurnResult:
        fence = _Fence(receipt.turn_id, receipt.owner_id or "", receipt.lease_version)

        operator = await resolve_cloud_operator(
            self._operators,
            tenant_id=command.tenant_id,
            account_id=command.account_id,
        )
        if operator is None:
            return await self._fail_terminal(
                fence, status=404, code="not_found",
                message=_OPAQUE_NOT_FOUND_MESSAGE,
            )

        character_view = await self._resolve_character_view(
            command.tenant_id, command.account_id, command.character_id,
        )
        if character_view is None:
            return await self._fail_terminal(
                fence, status=404, code="not_found",
                message=_OPAQUE_NOT_FOUND_MESSAGE,
            )

        # H5: every inbound attachment ref must be owned by this operator and
        # match its declared digest/size/type BEFORE it is fetched into the
        # prompt. A foreign (cross-tenant) key or a tampered declaration fails
        # closed here — the LLM never sees an object this pair does not own.
        attachment_failure = await self._validate_attachment_refs(
            fence, operator, command,
        )
        if attachment_failure is not None:
            return attachment_failure

        try:
            conversation = await self._resolve_conversation(
                receipt, fence, command,
            )
            if isinstance(conversation, TurnResult):
                return conversation
            adapter = TurnExecutionAdapter(
                receipts=self._receipts,
                conversations=self._conversations,
                object_storage=self._object_storage,
                turn_id=fence.turn_id,
                owner_id=fence.owner_id,
                lease_version=fence.lease_version,
                request_id=command.request_id,
                conversation_id=conversation.id,
                character_view=character_view,
                user_message_row_id=receipt.user_message_row_id,
            )
            request = await self._build_send_request(command, conversation)
            await self._chat_service.send_message(
                request, current_user_id=operator.id, external_turn=adapter,
            )
        except ChatSubscriptionFrozen:
            return await self._fail_terminal(
                fence, status=404, code="not_found",
                message=_OPAQUE_NOT_FOUND_MESSAGE,
            )
        except ChatRuntimeLimitExceeded:
            return await self._fail_retryable(
                fence, status=429, code="rate_limited",
                message="runtime limit exceeded",
            )
        except TurnFencingStale:
            return await self._fail_retryable(
                fence, status=503, code="unavailable",
                message="turn lease lost",
            )
        except Exception:  # noqa: BLE001 - any failure is a safe retry (503)
            _LOG.exception(
                "external-chat turn execution failed turn=%s", fence.turn_id,
            )
            return await self._fail_retryable(
                fence, status=503, code="unavailable",
                message="turn execution failed",
            )

        return await self._complete_from_committed(fence)

    async def _complete_from_committed(self, fence: _Fence) -> TurnResult:
        receipt = await self._receipts.get(fence.turn_id)
        if (
            receipt is None
            or receipt.state is not ExternalChatTurnState.COMMITTED
            or receipt.response_snapshot_json is None
        ):
            # The drive should have committed exactly once; anything else is a
            # lost-lease / concurrent-takeover race — a safe retry.
            return _retryable_result(
                503, code="unavailable", message="turn not committed",
            )
        await self._receipts.mark_completed(
            turn_id=fence.turn_id,
            owner_id=fence.owner_id,
            lease_version=fence.lease_version,
        )
        return _completed_result(receipt.response_snapshot_json)

    # -- recovery finalizers ------------------------------------------------ #

    async def _finalize_committed(
        self, receipt: ExternalChatTurnReceipt,
    ) -> TurnResult:
        # COMMITTED: assistant durably appended, snapshot stored. Just finalize.
        if receipt.response_snapshot_json is None:
            return _retryable_result(
                503, code="unavailable", message="committed snapshot missing",
            )
        await self._receipts.mark_completed(
            turn_id=receipt.turn_id,
            owner_id=receipt.owner_id or "",
            lease_version=receipt.lease_version,
        )
        return _completed_result(receipt.response_snapshot_json)

    async def _finalize_generated(
        self, receipt: ExternalChatTurnReceipt, command: ExternalChatTurnCommand,
    ) -> TurnResult:
        fence = _Fence(receipt.turn_id, receipt.owner_id or "", receipt.lease_version)
        if receipt.generated_snapshot_json is None or receipt.conversation_id is None:
            return await self._fail_retryable(
                fence, status=503, code="unavailable",
                message="generated snapshot missing",
            )
        assistant_message = rebuild_assistant_from_generated(
            receipt.generated_snapshot_json,
        )
        character_view = await self._resolve_character_view(
            receipt.tenant_id, receipt.account_id, receipt.character_id,
        )
        if character_view is None:
            return await self._fail_retryable(
                fence, status=503, code="unavailable",
                message="character presentation unavailable",
            )
        conversation = await self._conversations.get(receipt.conversation_id)
        if conversation is None:
            return await self._fail_retryable(
                fence, status=503, code="unavailable",
                message="conversation vanished on recovery",
            )

        if receipt.assistant_message_row_id is None:
            # Assistant not yet durably appended — append it now from the
            # snapshot (the LLM is never re-run).
            result = await self._conversations.append_messages(
                receipt.conversation_id,
                expected_next_position=len(conversation.messages),
                messages=[assistant_message],
                # B1: same key the adapter's assistant append uses, so a
                # GENERATED-recovery that races the original commit adopts the
                # existing assistant row instead of appending a duplicate.
                idempotency_key=f"{receipt.turn_id}:assistant",
            )
            if not result.ok:
                return await self._fail_retryable(
                    fence, status=503, code="unavailable",
                    message="assistant append conflict on recovery",
                )
            assistant_row_id = result.row_ids[0]
            assistant_position = result.positions[0]
        else:
            assistant_row_id = receipt.assistant_message_row_id
            assistant_position = (
                receipt.assistant_position
                if receipt.assistant_position is not None
                else len(conversation.messages) - 1
            )

        snapshot_json = dumps_stable(
            await build_response_snapshot(
                object_storage=self._object_storage,
                character_view=character_view,
                request_id=command.request_id,
                turn_id=receipt.turn_id,
                conversation_id=receipt.conversation_id,
                assistant_message=assistant_message,
            ),
        )
        committed = await self._receipts.mark_committed(
            turn_id=fence.turn_id,
            owner_id=fence.owner_id,
            lease_version=fence.lease_version,
            assistant_message_row_id=assistant_row_id,
            assistant_position=assistant_position,
            response_snapshot_json=snapshot_json,
        )
        if not committed:
            return await self._fail_retryable(
                fence, status=503, code="unavailable",
                message="mark_committed fenced out on recovery",
            )
        await self._receipts.mark_completed(
            turn_id=fence.turn_id,
            owner_id=fence.owner_id,
            lease_version=fence.lease_version,
        )
        return _completed_result(snapshot_json)

    # -- guards / resolution ------------------------------------------------ #

    async def _resolve_character_view(
        self, tenant_id: str, account_id: str, character_id: str,
    ) -> RosterCharacterView | None:
        """The roster is the single source of chat authorization: a character
        absent from the pair's roster is either not owned by this operator or
        subscription-denied — both collapse to an opaque not-found."""
        roster = await self._roster.resolve_roster(
            tenant_id=tenant_id, account_id=account_id,
        )
        return _find_character(roster, character_id)

    async def _validate_attachment_refs(
        self, fence: _Fence, operator: object, command: ExternalChatTurnCommand,
    ) -> TurnResult | None:
        """Fail closed unless every attachment ref is owned by ``operator`` and
        matches its declared metadata (H5).

        * A key outside this operator's ``users/{owner}/messaging-inbound/``
          prefix, or one that genuinely does not exist, is an opaque 404 — a
          cross-tenant / forged ref is indistinguishable from "not found" so
          the caller learns nothing about another tenant's namespace.
        * A stored object whose digest / size / media type disagrees with the
          declaration is a hard 409 — the declared ref does not describe the
          bytes Core actually holds. When the store did not persist a digest,
          the bytes are re-hashed (never skipped) so a ``None`` stored digest
          can NEVER wave a tampered declaration through.
        * A transient storage failure (timeout / 5xx / backend unreachable) is a
          retryable 503, NEVER a terminal 404 — swallowing it as "not found"
          would burn a recoverable turn into a permanent, replayed rejection.
        """
        if not command.attachments:
            return None
        prefix = inbound_object_key_prefix(getattr(operator, "id", "") or "")
        for attachment in command.attachments:
            object_ref = attachment.object_ref
            if not object_ref.startswith(prefix):
                return await self._fail_terminal(
                    fence, status=404, code="not_found",
                    message=_OPAQUE_NOT_FOUND_MESSAGE,
                )
            try:
                metadata = await self._object_storage.stat(object_key=object_ref)
            except ObjectNotFoundError:
                return await self._fail_terminal(
                    fence, status=404, code="not_found",
                    message=_OPAQUE_NOT_FOUND_MESSAGE,
                )
            except ObjectStorageError:
                # Backend down / timed out / 5xx — the object may well exist;
                # never a terminal verdict. A retry converges once storage heals.
                return await self._fail_retryable(
                    fence, status=503, code="unavailable",
                    message="attachment storage temporarily unavailable",
                )
            if metadata is None:
                return await self._fail_terminal(
                    fence, status=404, code="not_found",
                    message=_OPAQUE_NOT_FOUND_MESSAGE,
                )
            if (
                metadata.size_bytes != attachment.size_bytes
                or metadata.content_type != attachment.media_type
            ):
                return await self._fail_terminal(
                    fence, status=409, code="attachment_mismatch",
                    message="attachment does not match the stored object",
                )
            digest_result = await self._verify_attachment_digest(
                fence, object_ref, metadata, attachment.sha256,
            )
            if digest_result is not None:
                return digest_result
        return None

    async def _verify_attachment_digest(
        self,
        fence: _Fence,
        object_ref: str,
        metadata: ObjectMetadata,
        declared_sha256: str,
    ) -> TurnResult | None:
        """Confirm the stored bytes' SHA-256 matches ``declared_sha256``.

        Prefers the store's own digest; when it is absent, the bytes are
        fetched and re-hashed rather than skipping verification (H5). Returns a
        409 on a real mismatch, a 503 on a transient fetch failure, ``None``
        when the digest agrees.
        """
        if metadata.sha256 is not None:
            if metadata.sha256 == declared_sha256:
                return None
            return await self._fail_terminal(
                fence, status=409, code="attachment_mismatch",
                message="attachment does not match the stored object",
            )
        try:
            data = await self._object_storage.get_bytes(object_key=object_ref)
        except ObjectNotFoundError:
            return await self._fail_terminal(
                fence, status=404, code="not_found",
                message=_OPAQUE_NOT_FOUND_MESSAGE,
            )
        except ObjectStorageError:
            return await self._fail_retryable(
                fence, status=503, code="unavailable",
                message="attachment storage temporarily unavailable",
            )
        if hashlib.sha256(data).hexdigest() != declared_sha256:
            return await self._fail_terminal(
                fence, status=409, code="attachment_mismatch",
                message="attachment does not match the stored object",
            )
        return None

    async def _resolve_conversation(
        self,
        receipt: ExternalChatTurnReceipt,
        fence: _Fence,
        command: ExternalChatTurnCommand,
    ) -> Conversation | TurnResult:
        # Retry reuse: a prior attempt already recorded (self-heal / created)
        # the conversation — reuse it, never create a second one.
        if receipt.conversation_id:
            existing = await self._conversations.get(receipt.conversation_id)
            if existing is not None:
                return existing

        if command.conversation_id:
            requested = await self._conversations.get(command.conversation_id)
            if requested is not None:
                if requested.character_id != command.character_id:
                    return await self._fail_terminal(
                        fence, status=409,
                        code="conversation_character_mismatch",
                        message="conversation does not belong to this character",
                    )
                await self._record_conversation(fence, requested.id)
                return requested
            # Requested id missing / deleted — self-heal below.

        # M9: no usable pointer. Route through the single shared line-thread
        # resolver so an inbound-first and a proactive-first ordering converge
        # on ONE ``source="line"`` conversation (reusing the one a proactive
        # push may have already created, and converging a genuine double-create
        # race) instead of forking a parallel history per direction.
        conversation = await resolve_or_create_line_conversation(
            self._conversations, character_id=command.character_id,
        )
        await self._record_conversation(fence, conversation.id)
        return conversation

    async def _record_conversation(self, fence: _Fence, conversation_id: str) -> None:
        recorded = await self._receipts.record_conversation(
            turn_id=fence.turn_id,
            owner_id=fence.owner_id,
            lease_version=fence.lease_version,
            conversation_id=conversation_id,
        )
        if not recorded:
            raise TurnFencingStale("record_conversation fenced out")

    async def _build_send_request(
        self, command: ExternalChatTurnCommand, conversation: Conversation,
    ) -> SendChatMessageRequest:
        attachment_urls: list[str] = []
        for attachment in command.attachments:
            url = await self._object_storage.public_url(
                object_key=attachment.object_ref,
            )
            attachment_urls.append(url)
        presence = PresenceFramePayload.from_domain(
            PresenceFrame.messaging(
                platform=Platform.LINE,
                has_attachments=bool(attachment_urls),
            ),
        )
        return SendChatMessageRequest(
            character_id=command.character_id,
            conversation_id=conversation.id,
            message=command.message,
            attachment_urls=attachment_urls,
            operator_persona_enabled=True,
            presence_frame=presence,
        )

    # -- terminal / retryable failure recording ----------------------------- #

    async def _fail_terminal(
        self, fence: _Fence, *, status: int, code: str, message: str,
    ) -> TurnResult:
        body = error_envelope(code=code, message=message, retryable=False)
        terminal_error_json = dumps_stable({"status": status, "body": body})
        fenced = await self._receipts.mark_failed(
            turn_id=fence.turn_id,
            owner_id=fence.owner_id,
            lease_version=fence.lease_version,
            retryable=False,
            error_code=code,
            error_message=message,
            terminal_error_json=terminal_error_json,
        )
        if not fenced:
            # M10: the terminal write was fenced out — a concurrent owner took
            # over the lease and may already have driven the turn to a real
            # outcome. Never assert our stale terminal verdict over theirs;
            # re-read the receipt and answer from its actual current state.
            return await self._respond_from_current_state(fence)
        return TurnResult(status, dumps_stable(body))

    async def _respond_from_current_state(self, fence: _Fence) -> TurnResult:
        """Map the turn's actual persisted state to a response after a fenced
        write lost the lease (the authoritative owner is elsewhere)."""
        receipt = await self._receipts.get(fence.turn_id)
        if receipt is None:
            return _retryable_result(
                503, code="unavailable", message="turn state unavailable",
            )
        if (
            receipt.state is ExternalChatTurnState.COMPLETED
            and receipt.response_snapshot_json is not None
        ):
            return _completed_result(receipt.response_snapshot_json)
        if receipt.state is ExternalChatTurnState.FAILED_TERMINAL:
            return _replay_terminal(receipt.terminal_error_json)
        # Still in flight under another owner (PROCESSING / GENERATED /
        # COMMITTED / FAILED_RETRYABLE) — a safe retry converges on it.
        return _retryable_result(
            503, code="unavailable", message="turn taken over",
        )

    async def _fail_retryable(
        self, fence: _Fence, *, status: int, code: str, message: str,
    ) -> TurnResult:
        await self._receipts.mark_failed(
            turn_id=fence.turn_id,
            owner_id=fence.owner_id,
            lease_version=fence.lease_version,
            retryable=True,
            error_code=code,
            error_message=message,
        )
        return _retryable_result(status, code=code, message=message)


# -- module helpers --------------------------------------------------------- #


def _build_canonical_payload(command: ExternalChatTurnCommand) -> dict[str, object]:
    """The logical contract fields the canonical hash covers.

    ``conversation_id`` is omitted entirely when the wire request never carried
    the key, and hashed as ``null`` when it was present-and-null — matching the
    Channel producer so a retried delivery hashes byte-identically. Attachment
    order is preserved (never sorted / de-duplicated)."""
    payload: dict[str, object] = {
        "request_id": command.request_id,
        "tenant_id": command.tenant_id,
        "account_id": command.account_id,
        "character_id": command.character_id,
        "message": command.message,
        "attachments": [
            {
                "object_ref": item.object_ref,
                "media_type": item.media_type,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in command.attachments
        ],
        "channel": command.channel,
        "contract_version": command.contract_version,
    }
    if command.conversation_id_present:
        payload["conversation_id"] = command.conversation_id
    return payload


def _find_character(
    roster: RosterView | None, character_id: str,
) -> RosterCharacterView | None:
    if roster is None:
        return None
    for character in roster.characters:
        if character.character_id == character_id:
            return character
    return None


def _completed_result(snapshot_json: str | None) -> TurnResult:
    if snapshot_json is None:
        return _retryable_result(
            503, code="unavailable", message="completed snapshot missing",
        )
    return TurnResult(200, snapshot_json)


def _hash_conflict_result() -> TurnResult:
    body = error_envelope(
        code="idempotency_conflict",
        message="request id reused with a different request",
        retryable=False,
    )
    return TurnResult(409, dumps_stable(body))


def _replay_terminal(terminal_error_json: str | None) -> TurnResult:
    if terminal_error_json is None:
        body = error_envelope(
            code="not_found", message=_OPAQUE_NOT_FOUND_MESSAGE, retryable=False,
        )
        return TurnResult(404, dumps_stable(body))
    data = json.loads(terminal_error_json)
    return TurnResult(int(data["status"]), dumps_stable(data["body"]))


def _retryable_result(status: int, *, code: str, message: str) -> TurnResult:
    body = error_envelope(code=code, message=message, retryable=True)
    return TurnResult(status, dumps_stable(body))
