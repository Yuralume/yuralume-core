import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from fastapi import (
    APIRouter, Depends, File, HTTPException, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
)
from kokoro_link.application.dto.character import CharacterResponse
from kokoro_link.application.dto.chat import (
    ChatReplyResponse,
    ConversationResponse,
    SendChatMessageRequest,
)
from kokoro_link.application.services.chat_service import (
    ChatRuntimeLimitExceeded,
    ChatSubscriptionFrozen,
)
from kokoro_link.application.services.turn_undo_service import (
    NoJournalError, UndoResult,
)
from kokoro_link.bootstrap.container import ServiceContainer

_LOGGER = logging.getLogger(__name__)

_CHAT_UPLOAD_SUBDIR = "chat-uploads"
_CHAT_UPLOAD_MAX_BYTES = 8 * 1024 * 1024  # 8 MB — matches character images
_CHAT_UPLOAD_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_CHAT_UPLOAD_MAX_PER_REQUEST = 4


class ChatUploadsResponse(BaseModel):
    urls: list[str] = Field(default_factory=list)


router = APIRouter(tags=["chat"])


@router.get(
    "/characters/{character_id}/conversations/latest",
    response_model=ConversationResponse | None,
)
async def get_latest_conversation(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> ConversationResponse | None:
    return await container.chat_service.get_latest_conversation(character_id)


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
            content_type=upload.content_type or "application/octet-stream",
            metadata={"user_id": safe_user_dir, "kind": "chat-upload"},
        )
        saved.append(stored.url)

    return ChatUploadsResponse(urls=saved)


def _safe_user_dir(user_id: str) -> str:
    """Restrict ``user_id`` to a filesystem-safe subdirectory name.

    Keeps alnum / dash / underscore / dot (id schemes used by the auth
    layer are UUIDs or short string handles) and falls back to a fixed
    ``unknown`` bucket when the input contains anything else. Drops the
    risk of a path-traversal payload reaching ``mkdir``.
    """
    cleaned = "".join(
        ch for ch in (user_id or "") if ch.isalnum() or ch in ("-", "_", ".")
    )
    return cleaned or "unknown"


class UndoTurnResponse(BaseModel):
    """Summary of what the turn-undo operation reversed."""

    conversation_id: str
    turn_index: int
    reverted_messages: int
    deleted_memories: int
    deleted_state_snapshots: int
    rejected_persona_fields: int
    restored_goals: bool
    restored_arc: bool
    restored_schedule: bool
    restored_character_state: bool

    @classmethod
    def from_result(cls, result: UndoResult) -> "UndoTurnResponse":
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


@router.post("/chat/messages", response_model=ChatReplyResponse)
async def send_chat_message(
    payload: SendChatMessageRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
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
        return await container.chat_service.send_message(
            payload, current_user_id=current_user_id,
        )
    except ChatSubscriptionFrozen as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "subscription_frozen", "message": str(error)},
        ) from error
    except ChatRuntimeLimitExceeded as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error


@router.post("/chat/messages/stream")
async def send_chat_message_stream(
    payload: SendChatMessageRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
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
        token_stream, finalizer = await container.chat_service.send_message_stream(
            payload, current_user_id=current_user_id,
        )
    except ChatSubscriptionFrozen as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "subscription_frozen", "message": str(error)},
        ) from error
    except ChatRuntimeLimitExceeded as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    async def event_generator() -> AsyncIterator[str]:
        collected: list[str] = []

        # Send conversation_id immediately so frontend can track it
        yield f"data: {json.dumps({'conversation_id': finalizer.conversation_id})}\n\n"

        try:
            async for token in token_stream:
                collected.append(token)
                yield f"data: {json.dumps({'token': token})}\n\n"

            full_text = "".join(collected)
            # ``shield`` so a late client disconnect (user navigated away
            # right as the LLM finished) doesn't abort the DB save
            # half-way through SQLAlchemy's greenlet — otherwise uvicorn
            # logs the CancelledError traceback from inside
            # ``_concurrency_py3k.greenlet_spawn``. Data integrity wins:
            # the assistant reply always lands in the DB.
            response = await asyncio.shield(finalizer.finish(full_text))
            # mode='json' so datetime/UUID fields become primitives; otherwise json.dumps
            # raises TypeError mid-stream, the connection closes without the final event,
            # and the client is left waiting with no way to unstick its UI state.
            yield f"data: {json.dumps({'done': True, 'response': response.model_dump(mode='json')})}\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # Browser closed the tab / navigated away mid-generation.
            # User turn is already persisted (send_message_stream saved
            # it pre-LLM); the shielded finalize above will complete
            # regardless. Nothing else to do — re-raise so uvicorn can
            # unwind the task cleanly.
            _LOGGER.info(
                "chat stream cancelled by client for conversation %s",
                finalizer.conversation_id,
            )
            raise

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
