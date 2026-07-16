from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.application.dto.chat_assist import ChatAssistSuggestionsResponse
from kokoro_link.application.services.chat_assist_service import (
    ChatAssistCharacterNotFoundError,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(tags=["chat"])


class ChatAssistSuggestionsRequest(BaseModel):
    count: int = Field(default=4, ge=1, le=5)


@router.post(
    "/characters/{character_id}/chat-assist/suggestions",
    response_model=ChatAssistSuggestionsResponse,
)
async def suggest_chat_starters(
    character_id: str,
    payload: ChatAssistSuggestionsRequest | None = None,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ChatAssistSuggestionsResponse:
    service = container.chat_assist_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="chat assist service unavailable",
        )
    try:
        return await service.suggest(
            character_id,
            user_id=current_user_id,
            count=(payload.count if payload is not None else 4),
        )
    except ChatAssistCharacterNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Character not found",
        ) from exc
