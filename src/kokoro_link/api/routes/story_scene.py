"""起幕 (story scene) REST routes.

Three endpoints, all scoped to a character the caller owns:

* ``POST   /characters/{id}/story-scene``      — raise the curtain
* ``GET    /characters/{id}/story-scene``      — the live scene, or null
* ``POST   /characters/{id}/story-scene/end``  — wrap up (SC1-D)

Every refusal carries a structured ``{"code", "message"}`` detail rather
than bare prose: the frontend has to tell "you are already in a scene"
from "there is nothing to play" from "the writer failed", and each of
those is a different thing to say to the player.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
    get_owned_character,
)
from kokoro_link.api.routes._cloud_errors import insufficient_credits_guard
from kokoro_link.application.dto.chat import ChatMessageResponse
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_STORY_SCENE_OPEN,
    client_quoted_price_scope,
)
from kokoro_link.application.dto.story_scene import (
    EndStorySceneRequest,
    EndStorySceneResponse,
    OpenStorySceneRequest,
    OpenStorySceneResponse,
    StorySceneSessionResponse,
    SuggestedActionResponse,
)
from kokoro_link.application.services.chat_turn_lease import (
    ConversationBusyError,
)
from kokoro_link.application.services.story_scene_quota import (
    StorySceneDailyLimitReached,
    StorySceneQuotaUnavailable,
)
from kokoro_link.application.services.story_scene_service import (
    SceneAlreadyOpen,
    SceneMaterialUnavailable,
    SceneNotOpen,
    SceneOpenFailed,
    SceneSessionMismatch,
    StorySceneService,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character import Character


router = APIRouter(tags=["story-scene"])

_LOGGER = logging.getLogger(__name__)


def _require_service(container: ServiceContainer) -> StorySceneService:
    service = getattr(container, "story_scene_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Story scene service not configured",
        )
    return service


def _structured(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


@router.post(
    "/characters/{character_id}/story-scene",
    response_model=OpenStorySceneResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_story_scene(
    character_id: str,
    payload: OpenStorySceneRequest | None = None,
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> OpenStorySceneResponse:
    """Play the next piece of this character's story, right now.

    The body is optional in both directions (SC3-C): a client that posts
    nothing opens a scene as before, and a hosted one posts the price it
    was quoting so the charge binds to the number on the player's screen.
    Binding happens through a scope rather than an argument because the
    charge is raised inside the service, alongside the quota gate that
    must refuse *before* any money moves.
    """
    service = _require_service(container)
    try:
        with insufficient_credits_guard(), client_quoted_price_scope(
            {
                ACTION_STORY_SCENE_OPEN: (
                    payload.quoted_price_cr if payload is not None else None
                ),
            },
        ):
            opening = await service.open_scene(character)
    except ConversationBusyError as error:
        # Same 409 + code the chat routes use: a turn is already running
        # on this thread and retrying after it lands will succeed.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_structured(error.code, str(error)),
        ) from error
    except SceneAlreadyOpen as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_structured(error.code, str(error)),
        ) from error
    except SceneMaterialUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_structured(error.code, str(error)),
        ) from error
    except StorySceneDailyLimitReached as error:
        # SC3-B tier knob: a rate ceiling on a paid action, retryable when
        # the rolling window clears — 429, not 403.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_structured(error.code, str(error)),
        ) from error
    except StorySceneQuotaUnavailable as error:
        # The ceiling exists but could not be checked (fail-closed):
        # transient and retryable, distinct from "out for today".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_structured(error.code, str(error)),
        ) from error
    except SceneOpenFailed as error:
        # Upstream (the model) failed, not the request. 502 keeps it out
        # of the client's "I sent something wrong" bucket and marks it
        # retryable.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_structured(error.code, str(error)),
        ) from error
    return OpenStorySceneResponse(
        session=StorySceneSessionResponse.from_domain(opening.session),
        narration=ChatMessageResponse.from_domain(opening.narration),
        character_message=ChatMessageResponse.from_domain(
            opening.character_message,
        ),
        suggested_actions=[
            SuggestedActionResponse(text=text)
            for text in opening.suggested_actions
        ],
    )


@router.get(
    "/characters/{character_id}/story-scene",
    response_model=StorySceneSessionResponse | None,
)
async def get_story_scene(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> StorySceneSessionResponse | None:
    """The character's live scene, or ``null`` when it is not in one.

    ``null`` rather than 404: "this character is not in a scene" is a
    perfectly good answer about a character that exists, and the SC2
    composer polls this on every thread open — a 404 there would be
    indistinguishable from a genuinely missing character.
    """
    service = _require_service(container)
    session = await service.current_scene(character_id)
    if session is None:
        return None
    return StorySceneSessionResponse.from_domain(session)


@router.post(
    "/characters/{character_id}/story-scene/end",
    response_model=EndStorySceneResponse,
    status_code=status.HTTP_200_OK,
)
async def end_story_scene(
    character_id: str,
    payload: EndStorySceneRequest,
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> EndStorySceneResponse:
    """Wrap the scene up and land it as canon (SC1-D).

    Both refusals are 409 rather than 404: the character exists and is
    owned, and what is wrong is the *state* the client believed it was
    in — either no scene is running, or the one it named has already
    ended and a different one is. Both are fixed by re-reading
    ``GET /story-scene``, which a 404 would not suggest.

    Succeeding is unconditional once a matching scene is found: the
    response always carries a closed session, and ``closing_narration``
    is ``null`` whenever no prose could honestly be written.
    """
    service = _require_service(container)
    try:
        # No credit guard here, unlike the opening: the wrap-up runs on a
        # background feature key (§4.2 ③ — its cost is already inside the
        # opening's one price) and its writer swallows its own upstream
        # failures, so there is no charge and no 402 to translate.
        closing = await service.end_scene(
            character, session_id=payload.session_id,
        )
    except ConversationBusyError as error:
        # A turn is mid-flight on this thread; the wrap-up would be
        # writing narration into a reply that has not landed yet.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_structured(error.code, str(error)),
        ) from error
    except (SceneNotOpen, SceneSessionMismatch) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_structured(error.code, str(error)),
        ) from error
    return EndStorySceneResponse(
        session=StorySceneSessionResponse.from_domain(closing.session),
        closing_narration=(
            ChatMessageResponse.from_domain(closing.closing_narration)
            if closing.closing_narration is not None else None
        ),
    )
