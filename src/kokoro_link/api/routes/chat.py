import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter, Depends, File, HTTPException, Query, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    ensure_not_draining,
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
)
from kokoro_link.api.routes._cloud_errors import (
    insufficient_credits_detail,
    insufficient_credits_guard,
)
from kokoro_link.api.routes._uploads import ensure_allowed_upload_content_type
from kokoro_link.application.dto.character import CharacterResponse
from kokoro_link.application.dto.chat import (
    DEFAULT_CONVERSATION_PAGE_SIZE,
    MAX_CONVERSATION_PAGE_SIZE,
    ChatReplyResponse,
    ConversationResponse,
    SendChatMessageRequest,
)
from kokoro_link.application.services.chat_service import (
    ChatCharacterContractEndedError,
    ChatCharacterRestoringError,
    ChatRuntimeLimitExceeded,
    ChatSubscriptionFrozen,
)
from kokoro_link.application.services.chat_stream_relay import (
    TurnCompleted,
    TurnFrame,
    TurnStreamRelay,
    TurnToken,
)
from kokoro_link.application.services.chat_turn_lease import (
    CONVERSATION_BUSY_CODE,
    ConversationBusyError,
)
from kokoro_link.application.services.turn_undo_service import (
    NoJournalError, UndoResult,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayRequestTooLargeError,
)
from kokoro_link.infrastructure.storage.keys import safe_key_segment

_LOGGER = logging.getLogger(__name__)

_CHAT_UPLOAD_SUBDIR = "chat-uploads"
_CHAT_UPLOAD_MAX_BYTES = 8 * 1024 * 1024  # 8 MB — matches character images
_CHAT_UPLOAD_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_CHAT_UPLOAD_MAX_PER_REQUEST = 4
_PAYLOAD_TOO_LARGE_DETAIL = {
    "code": "payload_too_large",
    "message": "LLM request exceeds the size limit",
    "retryable": False,
}


class ChatUploadsResponse(BaseModel):
    urls: list[str] = Field(default_factory=list)


router = APIRouter(tags=["chat"])


@router.get(
    "/characters/{character_id}/conversations/latest",
    response_model=ConversationResponse | None,
)
async def get_latest_conversation(
    character_id: str,
    limit: int | None = Query(
        default=None,
        ge=1,
        le=MAX_CONVERSATION_PAGE_SIZE,
    ),
    before: int | None = Query(default=None, ge=0),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> ConversationResponse | None:
    """The newest page of the character's latest thread (IV10).

    Which thread is "latest" is exactly what it always was; ``limit`` /
    ``before`` only decide how much of its message tail comes back. ``before``
    is a message ``position`` (exclusive) — the feed's keyset shape (plan D8),
    not a page number: this codebase has no ``page`` / ``page_size`` anywhere
    and a second convention would be one more thing to get wrong.

    The IV10 panel sends ``limit`` explicitly.  Omitting both parameters keeps
    the whole-thread response used by cached pre-IV10 clients; this is the
    compatibility seam that makes a mixed-replica rollout safe.
    """
    return await container.chat_service.get_latest_conversation(
        character_id,
        limit=limit if limit is not None else (
            DEFAULT_CONVERSATION_PAGE_SIZE if before is not None else None
        ),
        before_position=before,
    )


@router.post(
    "/characters/{character_id}/conversations/mark-read",
    response_model=CharacterResponse,
)
async def mark_conversation_read(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> CharacterResponse:
    """Zero out the proactive unread badge for this character.

    Called by the frontend as soon as the user opens / refocuses the
    chat panel. Idempotent — repeatedly marking a character with zero
    unread is a no-op.
    """
    result = await container.character_service.mark_web_conversation_read(
        character_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"character {character_id} not found",
        )
    return result


@router.post("/chat/uploads", response_model=ChatUploadsResponse)
async def upload_chat_attachments(
    files: list[UploadFile] = File(...),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ChatUploadsResponse:
    """Receive up to 4 image files the user wants to attach to their
    next chat turn. Writes them to Object Storage under
    ``users/{user_id}/chat-uploads/`` with a fresh UUID filename and
    returns public URLs the caller passes back in
    ``SendChatMessageRequest.attachment_urls``.

    Images are not associated with a conversation at this point — the
    frontend holds the returned URLs until the user hits send. Any
    files uploaded but never referenced stay in Object Storage
    (size-bounded by the per-file cap, so leakage is small in practice).

    Multi-user (P1-5 in the auth review): the per-user subdirectory
    plus UUID filename behaves as a capability URL — the URL itself
    structurally segregates which user owns the file, and the UUID
    prevents enumeration. A signed / auth-protected file route is the
    middle-term follow-up the review flags; until then, treat upload
    URLs as bearer-equivalent secrets in the public API."""
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no files provided",
        )
    if len(files) > _CHAT_UPLOAD_MAX_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"too many files (max {_CHAT_UPLOAD_MAX_PER_REQUEST})",
        )

    object_storage = container.object_storage
    # Sanitise the user_id before it becomes part of an object key.
    # Strict allow-list: alphanum / dash / underscore / dot only.
    safe_user_dir = _safe_user_dir(current_user_id)

    saved: list[str] = []
    for upload in files:
        filename = upload.filename or ""
        suffix = Path(filename).suffix.lower()
        if suffix not in _CHAT_UPLOAD_ALLOWED_EXT:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"unsupported file type: {suffix or '(missing)'}",
            )
        # The suffix check above cannot stand in for this one. The stored object
        # is served back from the app's own origin *with this content type*, so
        # a part named ``a.png`` but declared ``text/html`` becomes same-origin
        # HTML at a URL the app minted — the extension never entered into it.
        content_type = ensure_allowed_upload_content_type(upload.content_type)
        data = await upload.read()
        if len(data) > _CHAT_UPLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"{filename!r} exceeds {_CHAT_UPLOAD_MAX_BYTES} bytes"
                ),
            )
        if not data:
            continue
        out_name = f"{uuid4().hex}{suffix}"
        stored = await object_storage.put_bytes(
            object_key=f"users/{safe_user_dir}/{_CHAT_UPLOAD_SUBDIR}/{out_name}",
            content=data,
            # The validated (and normalised) type, not the raw header: this is
            # the value the public media route will serve it back with.
            content_type=content_type,
            metadata={"user_id": safe_user_dir, "kind": "chat-upload"},
        )
        saved.append(stored.url)

    return ChatUploadsResponse(urls=saved)


def _safe_user_dir(user_id: str) -> str:
    """Restrict ``user_id`` to a single key-safe subdirectory name.

    Thin wrapper over the shared
    :func:`~kokoro_link.infrastructure.storage.keys.safe_key_segment`, which
    documents the allow-list and the two traps it closes.

    That helper reads alnum as **ASCII** alnum, where this used to accept
    anything ``str.isalnum`` liked — a tightening with no effect on real
    traffic (auth mints UUIDs; every id that reaches here is ASCII) and a
    correction where it does bite: a CJK id previously produced a segment the
    downstream key validator rejects, i.e. a "sanitised" name that only ever
    turned into a failed write.
    """
    return safe_key_segment(user_id, fallback="unknown")


class UndoTurnResponse(BaseModel):
    """Summary of what the turn-undo operation reversed.

    Every field the whole TU series reports is declared here at once,
    each with a default, so a client can be written against the final
    shape today: a subsystem whose rollback has not shipped yet answers
    ``0`` / ``false``, which is also exactly what it answers when the
    subsystem is not wired on this deployment. Fields are additive and a
    reader must not assume the set is closed.
    """

    conversation_id: str
    turn_index: int
    reverted_messages: int = 0
    deleted_memories: int = 0
    deleted_state_snapshots: int = 0
    rejected_persona_fields: int = 0
    restored_goals: bool = False
    restored_arc: bool = False
    restored_schedule: bool = False
    restored_character_state: bool = False
    recorded_tombstone: bool = False
    deleted_emotion_events: int = 0
    deleted_follow_ups: int = 0
    restored_follow_ups: int = 0
    cancelled_follow_up_jobs: int = 0
    restored_address_preference: bool = False
    reverted_address_log_entries: int = 0
    restored_scene_session: bool = False
    deleted_created_arc: bool = False
    deleted_encounter_intents: int = 0
    deleted_curiosity_attempts: int = 0
    deleted_story_events: int = 0
    marked_checkpoint_stale: bool = False

    @classmethod
    def from_result(cls, result: UndoResult) -> "UndoTurnResponse":
        # Field-for-field with ``UndoResult``; copied by name rather than
        # by ``**asdict`` so a field added on one side and forgotten on
        # the other is a missing key here, not a silently dropped number.
        return cls(
            conversation_id=result.conversation_id,
            turn_index=result.turn_index,
            reverted_messages=result.reverted_messages,
            deleted_memories=result.deleted_memories,
            deleted_state_snapshots=result.deleted_state_snapshots,
            rejected_persona_fields=result.rejected_persona_fields,
            restored_goals=result.restored_goals,
            restored_arc=result.restored_arc,
            restored_schedule=result.restored_schedule,
            restored_character_state=result.restored_character_state,
            recorded_tombstone=result.recorded_tombstone,
            deleted_emotion_events=result.deleted_emotion_events,
            deleted_follow_ups=result.deleted_follow_ups,
            restored_follow_ups=result.restored_follow_ups,
            cancelled_follow_up_jobs=result.cancelled_follow_up_jobs,
            restored_address_preference=result.restored_address_preference,
            reverted_address_log_entries=result.reverted_address_log_entries,
            restored_scene_session=result.restored_scene_session,
            deleted_created_arc=result.deleted_created_arc,
            deleted_encounter_intents=result.deleted_encounter_intents,
            deleted_curiosity_attempts=result.deleted_curiosity_attempts,
            deleted_story_events=result.deleted_story_events,
            marked_checkpoint_stale=result.marked_checkpoint_stale,
        )


async def _ensure_conversation_owner(
    *,
    container: ServiceContainer,
    conversation_id: str,
    current_user_id: str,
) -> None:
    """Verify the caller owns the character behind ``conversation_id``.

    Collapses every cross-user case (missing conversation, missing
    character, owner mismatch) to the same 404 so callers can't probe
    which ids exist.
    """
    repo = container.conversation_repository
    if repo is None:
        # Test harness without conversation persistence — fall back to
        # the chat-service surface. Skipping the owner check here is
        # safe because such harnesses don't multiplex users.
        return
    conversation = await repo.get(conversation_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="conversation not found",
        )
    character = await _safe_get_character_entity(
        container, conversation.character_id, current_user_id,
    )
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="conversation not found",
        )


async def _safe_get_character_entity(
    container: ServiceContainer,
    character_id: str,
    current_user_id: str,
):
    """Ownership-aware character entity lookup with stub-compat fallback.

    Mirrors ``api.dependencies.get_owned_character`` so chat routes that
    have to resolve ownership from a payload (rather than a path
    variable) can share the same compat behaviour with pre-auth
    ``CharacterService`` stubs in unit tests."""
    service = container.character_service
    try:
        character = await service.get_character_entity(
            character_id, user_id=current_user_id,
        )
    except TypeError:
        character = await service.get_character_entity(character_id)
        if character is not None and getattr(
            character, "user_id", current_user_id,
        ) != current_user_id:
            character = None
    return character


@router.post(
    "/conversations/{conversation_id}/turns/undo",
    response_model=UndoTurnResponse,
)
async def undo_last_turn(
    conversation_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> UndoTurnResponse:
    """Reverse the most recent turn of a conversation.

    Pops the last user + assistant message pair, restores character
    state / goals / active arc / today's schedule from the journal
    snapshot, and deletes any memories + state-history rows created
    during the turn window. Limited to the 5 most recent turns (older
    journals are GC'd after each new turn).

    Returns 409 when the conversation has no undoable turns (either
    brand new or all journals already consumed / pruned).
    """
    if container.turn_undo_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="undo subsystem not wired",
        )
    await _ensure_conversation_owner(
        container=container,
        conversation_id=conversation_id,
        current_user_id=current_user_id,
    )
    try:
        result = await container.turn_undo_service.undo_last_turn(
            conversation_id,
        )
    except NoJournalError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return UndoTurnResponse.from_result(result)


def _conversation_busy_http_error(
    error: ConversationBusyError,
) -> HTTPException:
    """409 for "this conversation already has a turn in flight".

    A dedicated status + structured code so it stays distinguishable from the
    existing chat outcomes: 403 ``subscription_frozen`` (entitlement), 404
    (unknown/foreign character or conversation), 429 (session message cap). 409 is
    the accurate shape — the request is well-formed and authorized, it just
    conflicts with the conversation's current state, and retrying after the
    in-flight turn lands will succeed.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": CONVERSATION_BUSY_CODE,
            "message": str(error),
            "conversation_id": error.conversation_id,
        },
    )


@router.post("/chat/messages", response_model=ChatReplyResponse)
async def send_chat_message(
    payload: SendChatMessageRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
    # Runs before the handler body — which is the point: the ownership lookup
    # below persists rest-recovery state, so a drain refused only inside the
    # service would still have written on the way to its 503.
    _drain_gate: None = Depends(ensure_not_draining),
) -> ChatReplyResponse:
    character = await _safe_get_character_entity(
        container, payload.character_id, current_user_id,
    )
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        )
    try:
        with insufficient_credits_guard():
            return await container.chat_service.send_message(
                payload, current_user_id=current_user_id,
            )
    except ConversationBusyError as error:
        raise _conversation_busy_http_error(error) from error
    except ChatSubscriptionFrozen as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "subscription_frozen", "message": str(error)},
        ) from error
    except ChatCharacterContractEndedError as error:
        # D7 / EC10: the licensor's contract for this character's source
        # card ended. Its own code, not `subscription_frozen` — the player's
        # subscription is fine and renewing it would change nothing.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "character_contract_ended", "message": str(error),
            },
        ) from error
    except ChatCharacterRestoringError as error:
        # CB A3: the character row landed but its restore is still running
        # — a temporary state conflict, not an authorization problem.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "character_restoring", "message": str(error)},
        ) from error
    except ChatRuntimeLimitExceeded as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
        ) from error
    except CloudGatewayRequestTooLargeError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=_PAYLOAD_TOO_LARGE_DETAIL,
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post("/chat/messages/stream")
async def send_chat_message_stream(
    payload: SendChatMessageRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
    # Same gate, same reason as the sync route above.
    _drain_gate: None = Depends(ensure_not_draining),
) -> StreamingResponse:
    character = await _safe_get_character_entity(
        container, payload.character_id, current_user_id,
    )
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        )
    try:
        with insufficient_credits_guard():
            token_stream, finalizer = (
                await container.chat_service.send_message_stream(
                    payload, current_user_id=current_user_id,
                )
            )
    except ConversationBusyError as error:
        raise _conversation_busy_http_error(error) from error
    except ChatSubscriptionFrozen as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "subscription_frozen", "message": str(error)},
        ) from error
    except ChatCharacterContractEndedError as error:
        # D7 / EC10 — same mapping, same reason as the sync route above.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "character_contract_ended", "message": str(error),
            },
        ) from error
    except ChatCharacterRestoringError as error:
        # CB A3 — same mapping, same reason as the sync route above.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "character_restoring", "message": str(error)},
        ) from error
    except ChatRuntimeLimitExceeded as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
        ) from error
    except CloudGatewayRequestTooLargeError as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=_PAYLOAD_TOO_LARGE_DETAIL,
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    # The turn runs in its own task from here. This generator is a *forwarder*
    # and owns nothing: a client that goes away cancels the forwarding and
    # leaves the turn to finish, because a disconnect means the audience left,
    # not that the performance was cancelled. Everything about releasing the
    # lease / charge / drain slot lives in the relay, which is also what makes
    # the release single-owner — the double-release window between this
    # ``finally`` and a shielded finalize no longer exists.
    relay = TurnStreamRelay(token_stream, finalizer).start()

    async def event_generator() -> AsyncIterator[str]:
        try:
            # Send conversation_id immediately so frontend can track it.
            #
            # Inside the ``try`` on purpose: a client that goes away while this
            # very first frame is in flight closes the generator *at this yield
            # point*, and a yield sitting above the ``try`` would skip the
            # ``detach`` below — leaving the turn running with nothing bounding
            # it. The window is small (one frame) but it is a disconnect at
            # exactly the moment disconnects cluster: the user hitting send and
            # immediately navigating away.
            yield f"data: {json.dumps({'conversation_id': finalizer.conversation_id})}\n\n"

            # Ordered by frequency: every reply is mostly tokens.
            async for event in relay.frames():
                if isinstance(event, TurnToken):
                    yield f"data: {json.dumps({'token': event.text})}\n\n"
                elif isinstance(event, TurnFrame):
                    # Tool-activity and friends: forwarded verbatim.
                    yield f"data: {json.dumps(event.payload)}\n\n"
                elif isinstance(event, TurnCompleted):
                    # mode='json' so datetime/UUID fields become primitives;
                    # otherwise json.dumps raises TypeError mid-stream, the
                    # connection closes without the final event, and the client
                    # is left waiting with no way to unstick its UI state.
                    yield (
                        "data: "
                        f"{json.dumps({'done': True, 'response': event.response.model_dump(mode='json')})}"
                        "\n\n"
                    )
                    yield "data: [DONE]\n\n"
                else:
                    # ``TurnFailed``. The 200 + SSE headers are already on the
                    # wire, so an out-of-credits refusal cannot become a 402
                    # status here — it travels as a terminal ``error`` frame
                    # instead, mirroring the sync route's structured detail.
                    # Everything else keeps the old behaviour (propagate, no
                    # final event); the relay has already released the turn by
                    # the time it publishes this.
                    detail = insufficient_credits_detail(event.error)
                    if detail is None:
                        raise event.error
                    _LOGGER.info(
                        "chat stream stopped: insufficient credits "
                        "(conversation %s)",
                        finalizer.conversation_id,
                    )
                    yield f"data: {json.dumps({'error': detail})}\n\n"
                    yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # Browser closed the tab / navigated away mid-generation. The user
            # turn is already persisted (send_message_stream saved it pre-LLM)
            # and the turn task keeps going, so the assistant reply still lands
            # and the player sees it when they reload. Re-raise so uvicorn can
            # unwind the request task cleanly.
            _LOGGER.info(
                "chat stream cancelled by client for conversation %s",
                finalizer.conversation_id,
            )
            raise
        finally:
            # Whether we finished, failed or were cancelled, nobody is reading
            # after this point. ``detach`` stops publishing and — if the turn is
            # still running — starts the watchdog that bounds it.
            relay.detach()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
