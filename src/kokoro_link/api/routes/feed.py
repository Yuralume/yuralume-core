"""Character feed-wall HTTP routes.

Read paths (Phase 1):

* ``GET /api/v1/characters/{character_id}/feed`` — reverse-chronological
  list with cursor-based pagination via ``before`` (an ISO-8601
  timestamp). The ``limit`` query param is clamped server-side.
* ``GET /api/v1/feed/posts/{post_id}`` — single post lookup, used for
  permalinks / SSE follow-ups.

Manual post (Phase A4):

* ``POST /api/v1/characters/{character_id}/feed`` — operator-authored
  post that bypasses the daily-limit / cooldown gates. Flows through
  the same persist → SSE → memorialize pipeline as auto-composed posts.

Browse-trigger memorialize (Phase A close-out):

* ``POST /api/v1/characters/{character_id}/feed/seen`` — convert any
  unseen likes/comments since the last call into character memories.
  The chat-open path already calls this internally; this endpoint lets
  the frontend trigger it when the user only browses the feed without
  opening chat, so engagement still surfaces in the next conversation.

Like paths (Phase A1):

* ``POST /api/v1/feed/posts/{post_id}/like`` — idempotent like.
* ``DELETE /api/v1/feed/posts/{post_id}/like`` — idempotent unlike.

Comment paths (Phase A2):

* ``GET /api/v1/feed/posts/{post_id}/comments`` — reverse-chronological
  list of comments.
* ``POST /api/v1/feed/posts/{post_id}/comments`` — author a comment.
* ``DELETE /api/v1/feed/comments/{comment_id}`` — delete own comment.

All write endpoints are single-user today: ``liker_id`` / ``author_id``
are stamped server-side so the frontend doesn't have to plumb identity.
When auth lands we'll switch to a request dependency that resolves the
caller; the service signatures already accept a kwarg.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
    get_current_user,
    get_current_user_id,
    get_owned_character,
)
from kokoro_link.application.dto.feed import (
    FeedListResponse,
    FeedPostResponse,
)
from kokoro_link.application.services.feed_comment_service import (
    FeedCommentForbidden,
    FeedCommentNotFound,
)
from kokoro_link.application.services.feed_comment_service import (
    FeedPostNotFound as FeedCommentPostNotFound,
)
from kokoro_link.application.services.feed_reaction_service import (
    FeedPostNotFound,
    FeedReactionState,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.operator_profile import OperatorProfile

router = APIRouter(tags=["feed"])

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


async def _owned_character_ids(
    container: ServiceContainer, current_user_id: str,
) -> list[str]:
    """Return the character ids owned by ``current_user_id``.

    Used to filter the global feed wall + unread badge so users only
    see posts from their own characters. Falls back to ``[]`` when the
    character service can't list (test stub containers) — global feed
    then renders empty rather than leak cross-user data.
    """
    service = getattr(container, "character_service", None)
    if service is None:
        return []
    try:
        characters = await service.list_characters(user_id=current_user_id)
    except TypeError:
        # Stub services without the auth-aware kwarg — fall back to a
        # global list and filter by user_id attribute when present.
        characters = await service.list_characters()
    ids: list[str] = []
    for c in characters:
        owner = getattr(c, "user_id", current_user_id)
        if owner == current_user_id:
            ids.append(c.id)
    return ids


async def _ensure_post_owner_or_404(
    container: ServiceContainer,
    post_id: str,
    current_user_id: str,
):
    """Resolve ``post_id`` and verify its character is owned by the
    current user. Returns the loaded post; raises 404 otherwise.

    Collapses missing-post and cross-user access to the same 404 so
    callers cannot probe which post ids exist."""
    repo = container.feed_post_repository
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feed post not found",
        )
    post = await repo.get(post_id)
    if post is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feed post not found",
        )
    character_service = getattr(container, "character_service", None)
    if character_service is None:
        return post
    try:
        character = await character_service.get_character_entity(
            post.character_id, user_id=current_user_id,
        )
    except TypeError:
        character = await character_service.get_character_entity(
            post.character_id,
        )
        if (
            character is not None
            and getattr(character, "user_id", current_user_id)
            != current_user_id
        ):
            character = None
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feed post not found",
        )
    return post


class FeedCommentResponse(BaseModel):
    """API shape for one user comment.

    ``content_text`` is whatever the user typed, after the entity-level
    trim. ``author_id`` remains the stable identity used for ownership
    checks; ``author_display_name`` is the account label the UI should
    render for the current operator instead of leaking raw ids such as
    ``default``."""

    id: str
    post_id: str
    author_id: str
    author_display_name: str | None = None
    author_display_name_is_placeholder: bool = False
    """True when ``author_display_name`` is the seeded ``操作者`` sentinel
    so the UI can substitute a localized placeholder label at the display
    boundary. The stored value + prompt sentinel are never mutated."""
    content_text: str
    created_at: datetime

    @classmethod
    def from_entity(
        cls,
        comment: FeedComment,
        *,
        current_user: OperatorProfile | None = None,
    ) -> "FeedCommentResponse":
        author_display_name = None
        is_placeholder = False
        if current_user is not None and comment.author_id == current_user.id:
            author_display_name = current_user.display_name
            is_placeholder = not current_user.has_real_name()
        return cls(
            id=comment.id,
            post_id=comment.post_id,
            author_id=comment.author_id,
            author_display_name=author_display_name,
            author_display_name_is_placeholder=is_placeholder,
            content_text=comment.content_text,
            created_at=comment.created_at,
        )


class FeedCommentListResponse(BaseModel):
    items: list[FeedCommentResponse]


class FeedCommentCreateRequest(BaseModel):
    content_text: str = Field(min_length=1, max_length=2000)


class FeedManualPostRequest(BaseModel):
    """User-authored feed post payload.

    ``kind`` follows the same vocabulary as auto-composed posts so the
    timeline rendering is uniform; defaults to ``"manual"`` to make
    operator-authored entries visually traceable. ``image_url`` is
    pre-uploaded — the manual flow doesn't trigger image generation.
    """

    content_text: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="manual", min_length=1, max_length=32)
    image_url: str | None = Field(default=None, max_length=512)
    image_prompt: str | None = Field(default=None, max_length=2000)


class FeedSeenResponse(BaseModel):
    """Result of converting unseen feed reactions into memories.

    ``updated`` is the number of posts whose ``reactions_seen_at``
    watermark advanced; zero is the steady-state response when nothing
    new has happened since the last call.
    """

    updated: int


class FeedUnreadResponse(BaseModel):
    """全局未讀貼文計數，紅點通知用。

    Watermark 由前端維護（``localStorage`` 的 last-viewed timestamp），
    所以這個端點只負責「比這個 ISO 之後多了幾篇」。``since=None``（沒
    傳）視為「初次造訪」，回 0 — 第一次就有紅點對使用者體驗反而吵。
    """

    count: int


class FeedReactionStateResponse(BaseModel):
    """API shape for the like/unlike result.

    Mirrors :class:`FeedReactionState` field-for-field so the frontend
    can consume the body directly without re-mapping. Returned by
    POST/DELETE so the UI can update the heart + count from one
    response without an extra GET round-trip.
    """

    post_id: str
    liked: bool
    likes: int

    @classmethod
    def from_state(cls, state: FeedReactionState) -> "FeedReactionStateResponse":
        return cls(
            post_id=state.post_id,
            liked=state.liked,
            likes=state.likes,
        )


@router.get(
    "/feed",
    response_model=FeedListResponse,
)
async def list_global_feed(
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    before: datetime | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FeedListResponse:
    """Mixed reverse-chronological timeline across the caller's characters.

    Drives the LumeGram global wall — same DTO shape as the
    per-character endpoint so ``FeedCard`` doesn't need a parallel
    rendering path. Character meta (name / avatar) is fetched once by
    the frontend via the existing ``GET /characters`` and joined
    client-side; embedding it here would 1) inflate every page payload
    and 2) duplicate state that already lives on the StagePage.

    Multi-user: only the current user's characters' posts appear on
    their wall — there's no shared global feed. Filtering happens at
    the repo level via the ``character_ids`` kwarg so pagination
    cursors keep working.
    """
    repo = container.feed_post_repository
    if repo is None:
        return FeedListResponse(items=[], has_more=False, next_before=None)
    owned_ids = await _owned_character_ids(container, current_user_id)
    posts = await repo.list_recent(
        limit=limit, before=before, character_ids=owned_ids,
    )
    liked_ids: set[str] = set()
    reaction_repo = container.feed_reaction_repository
    if reaction_repo is not None and posts:
        liked_ids = await reaction_repo.liked_post_ids(
            post_ids=tuple(p.id for p in posts),
            liker_id=current_user_id,
        )
    return FeedListResponse.from_domain(
        posts, limit=limit, liked_post_ids=liked_ids,
    )


@router.get(
    "/feed/unread",
    response_model=FeedUnreadResponse,
)
async def get_global_feed_unread(
    since: datetime | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FeedUnreadResponse:
    """Count of new posts after ``since`` — backs the launcher red dot.

    ``since`` 通常是前端 ``localStorage`` 裡的 last-viewed timestamp。
    第一次造訪（沒帶 since）回 0 比直接 reporting 全部貼文數實用 —
    紅點本來就是「上次看完後新增的」這個語意，不是「總共有幾篇」。

    Multi-user: counts only the caller's owned characters so each user
    sees their own unread total.
    """
    repo = container.feed_post_repository
    if repo is None or since is None:
        return FeedUnreadResponse(count=0)
    owned_ids = await _owned_character_ids(container, current_user_id)
    count = await repo.count_since(since=since, character_ids=owned_ids)
    return FeedUnreadResponse(count=count)


@router.get(
    "/characters/{character_id}/feed",
    response_model=FeedListResponse,
)
async def list_character_feed(
    character_id: str,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    before: datetime | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
    current_user_id: str = Depends(get_current_user_id),
) -> FeedListResponse:
    repo = container.feed_post_repository
    if repo is None:
        # Test harness without the feed stack — return an empty page
        # rather than 500 so the frontend's polling code stays happy.
        return FeedListResponse(items=[], has_more=False, next_before=None)
    posts = await repo.list_for_character(
        character_id, limit=limit, before=before,
    )
    liked_ids: set[str] = set()
    reaction_repo = container.feed_reaction_repository
    if reaction_repo is not None and posts:
        liked_ids = await reaction_repo.liked_post_ids(
            post_ids=tuple(p.id for p in posts),
            liker_id=current_user_id,
        )
    return FeedListResponse.from_domain(
        posts, limit=limit, liked_post_ids=liked_ids,
    )


@router.post(
    "/characters/{character_id}/feed",
    response_model=FeedPostResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_manual_feed_post(
    character_id: str,
    payload: FeedManualPostRequest,
    container: ServiceContainer = Depends(get_container),
    character: Character = Depends(get_owned_character),
) -> FeedPostResponse:
    composer = container.feed_composer_service
    if composer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feed composer is not available",
        )
    try:
        post = await composer.create_manual_post(
            character,
            content_text=payload.content_text,
            kind=payload.kind,
            image_url=payload.image_url,
            image_prompt=payload.image_prompt,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    # Manual posts default to "not yet liked by the local user".
    return FeedPostResponse.from_domain(post, liked=False)


@router.post(
    "/characters/{character_id}/feed/seen",
    response_model=FeedSeenResponse,
)
async def mark_feed_reactions_seen(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> FeedSeenResponse:
    """Run the reaction memorializer on demand.

    The frontend fires this when the user opens the feed panel so
    likes/comments still produce character-side memories even when the
    user never opens the chat surface in the same session.
    """
    # Always clear the unread-reply badge when the user opens the
    # overlay — the user has now "seen" any character replies that
    # landed since the last open. Independent from the memorializer
    # path so it still runs in test harnesses without a memorializer.
    await container.character_service.mark_feed_replies_seen(character_id)
    memorializer = container.feed_reaction_memorializer
    if memorializer is None:
        # Test harnesses without the memorializer wired still get a 200
        # so the frontend's fire-and-forget call doesn't surface an error.
        return FeedSeenResponse(updated=0)
    updated = await memorializer.memorialize(character_id=character_id)
    return FeedSeenResponse(updated=updated)


@router.get(
    "/feed/posts/{post_id}",
    response_model=FeedPostResponse,
)
async def get_feed_post(
    post_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FeedPostResponse:
    post = await _ensure_post_owner_or_404(
        container, post_id, current_user_id,
    )
    liked = False
    reaction_repo = container.feed_reaction_repository
    if reaction_repo is not None:
        liked = await reaction_repo.has_liked(
            post_id=post.id, liker_id=current_user_id,
        )
    return FeedPostResponse.from_domain(post, liked=liked)


@router.post(
    "/feed/posts/{post_id}/like",
    response_model=FeedReactionStateResponse,
)
async def like_feed_post(
    post_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FeedReactionStateResponse:
    service = container.feed_reaction_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feed reactions are not available",
        )
    await _ensure_post_owner_or_404(container, post_id, current_user_id)
    try:
        state = await service.like(
            post_id=post_id, liker_id=current_user_id,
        )
    except FeedPostNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feed post not found",
        )
    return FeedReactionStateResponse.from_state(state)


@router.delete(
    "/feed/posts/{post_id}/like",
    response_model=FeedReactionStateResponse,
)
async def unlike_feed_post(
    post_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FeedReactionStateResponse:
    service = container.feed_reaction_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feed reactions are not available",
        )
    await _ensure_post_owner_or_404(container, post_id, current_user_id)
    try:
        state = await service.unlike(
            post_id=post_id, liker_id=current_user_id,
        )
    except FeedPostNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feed post not found",
        )
    return FeedReactionStateResponse.from_state(state)


@router.get(
    "/feed/posts/{post_id}/comments",
    response_model=FeedCommentListResponse,
)
async def list_feed_post_comments(
    post_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> FeedCommentListResponse:
    service = container.feed_comment_service
    if service is None:
        # Test harness without the comment stack — return an empty page
        # rather than 500 so the frontend stays happy.
        return FeedCommentListResponse(items=[])
    await _ensure_post_owner_or_404(container, post_id, current_user.id)
    try:
        comments = await service.list_for_post(post_id, limit=limit)
    except FeedCommentPostNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feed post not found",
        )
    return FeedCommentListResponse(
        items=[
            FeedCommentResponse.from_entity(c, current_user=current_user)
            for c in comments
        ],
    )


@router.post(
    "/feed/posts/{post_id}/comments",
    response_model=FeedCommentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_feed_post_comment(
    post_id: str,
    payload: FeedCommentCreateRequest,
    container: ServiceContainer = Depends(get_container),
    current_user: OperatorProfile = Depends(get_current_user),
) -> FeedCommentResponse:
    service = container.feed_comment_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feed comments are not available",
        )
    await _ensure_post_owner_or_404(container, post_id, current_user.id)
    try:
        comment = await service.add(
            post_id=post_id,
            content_text=payload.content_text,
            author_id=current_user.id,
        )
    except FeedCommentPostNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feed post not found",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    return FeedCommentResponse.from_entity(comment, current_user=current_user)


@router.delete(
    "/feed/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_feed_comment(
    comment_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    service = container.feed_comment_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feed comments are not available",
        )
    # Look up the underlying comment so we can resolve the post and
    # verify ownership before the service's own author check runs.
    repo = container.feed_comment_repository
    if repo is not None:
        comment_row = await repo.get(comment_id)
        if comment_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Comment not found",
            )
        await _ensure_post_owner_or_404(
            container, comment_row.post_id, current_user_id,
        )
    try:
        await service.remove(
            comment_id=comment_id, author_id=current_user_id,
        )
    except FeedCommentNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Comment not found",
        )
    except FeedCommentForbidden:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete another user's comment",
        )


__all__ = ["router"]
