"""Turn user likes / comments into episodic memories for the character.

Whenever the user opens chat we run this pass: any reactions or
comments newer than the post's ``reactions_seen_at`` watermark become
short episodic memories so the character can naturally reference them
("謝謝你昨天在我貼文下面留言『我也想喝』！") instead of acting like the
engagement never happened.

Design decisions, mirrored on ``ScheduleMemorializer``:

- Idempotent via the ``reactions_seen_at`` watermark on each post. Once
  a post is "seen" up to time T, subsequent passes only persist deltas.
- Recent window only — old posts are unlikely to receive new
  interactions, and scanning the whole history would balloon the work
  on every turn.
- Memory salience is intentionally low (~0.4) so engagement memories
  add color without crowding out story-critical recall. Comments
  earn a small bump over likes since their textual content is more
  retrieval-worthy.
- Embedder failures fail loud: we skip the memory write AND skip the
  watermark update so the next turn retries; otherwise an embedder
  outage would silently desync the watermark from reality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.feed import (
    FeedCommentRepositoryPort,
    FeedPostRepositoryPort,
    FeedReactionRepositoryPort,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.feed_reaction import FeedReaction
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_OPERATOR_LANGUAGE = "zh-TW"

_RECENT_POSTS_TO_SCAN = 30
"""How many of the character's most-recent posts to scan per pass.
30 covers ~1.5 weeks at the default 2 posts/day cadence — old enough
that the engagement is likely already "seen" if it was going to be.
Bumping this requires no schema change; the cost is one extra
``list_since`` round-trip per scanned post."""

_PREVIEW_CHARS = 30
"""How many characters of the post body to quote in the memory line.
Long enough to anchor the memory; short enough to keep the embedding
focused on the engagement, not the original post text."""

_COMMENT_PREVIEW_CHARS = 60
"""Comment body preview budget — long enough to capture intent, short
enough that several comments can fit in one memory line without
exploding the embedding."""

_LIKE_SALIENCE = 0.40
_COMMENT_SALIENCE = 0.50


@dataclass(frozen=True, slots=True)
class _PostDelta:
    post: FeedPost
    new_likes: list[FeedReaction]
    new_comments: list[FeedComment]
    latest_event_at: datetime

    @property
    def has_new(self) -> bool:
        return bool(self.new_likes) or bool(self.new_comments)


class FeedReactionMemorializer:
    def __init__(
        self,
        *,
        post_repository: FeedPostRepositoryPort,
        reaction_repository: FeedReactionRepositoryPort,
        comment_repository: FeedCommentRepositoryPort,
        memory_repository: MemoryRepositoryPort,
        embedder: EmbedderPort | None = None,
        character_repository: CharacterRepositoryPort | None = None,
        operator_profile_service: Any | None = None,
    ) -> None:
        self._posts = post_repository
        self._reactions = reaction_repository
        self._comments = comment_repository
        self._memories = memory_repository
        self._embedder = embedder
        # Both optional: a container missing either falls back to the
        # ship-first zh-TW template (same contract as
        # ScheduleMemorializer._resolve_operator_language).
        self._character_repository = character_repository
        self._operator_profile_service = operator_profile_service

    async def memorialize(
        self,
        *,
        character_id: str,
        now: datetime | None = None,
    ) -> int:
        """Scan recent posts; persist memories for new interactions and
        bump ``reactions_seen_at``. Returns the number of posts whose
        watermarks were updated.
        """
        moment = _ensure_utc(now)
        try:
            recent = await self._posts.list_for_character(
                character_id, limit=_RECENT_POSTS_TO_SCAN,
            )
        except Exception:
            _LOGGER.exception(
                "feed memorializer: failed to list posts character=%s",
                character_id,
            )
            return 0

        deltas: list[_PostDelta] = []
        for post in recent:
            delta = await self._collect_delta(post, character_id=character_id)
            if delta is not None and delta.has_new:
                deltas.append(delta)

        if not deltas:
            return 0

        language = await self._resolve_operator_language(character_id)
        memories = [
            self._delta_to_memory(
                character_id=character_id, delta=delta, language=language,
            )
            for delta in deltas
        ]
        try:
            embedded = await attach_embeddings(memories, self._embedder)
        except EmbedderError:
            _LOGGER.exception(
                "feed memorializer: embedder unavailable; "
                "deferring %d post(s)",
                len(deltas),
            )
            return 0
        try:
            await self._memories.add_many(embedded)
        except Exception:
            _LOGGER.exception(
                "feed memorializer: failed to persist memories",
            )
            return 0

        updated = 0
        for delta in deltas:
            try:
                seen = delta.post.mark_reactions_seen(when=moment)
                await self._posts.save(seen)
                updated += 1
            except Exception:
                _LOGGER.exception(
                    "feed memorializer: failed to mark seen post=%s",
                    delta.post.id,
                )
        return updated

    async def _collect_delta(
        self, post: FeedPost, *, character_id: str,
    ) -> _PostDelta | None:
        watermark = post.reactions_seen_at
        try:
            new_likes = await self._reactions.list_since(
                post_id=post.id, since=watermark,
            )
            new_comments_all = await self._comments.list_since(
                post_id=post.id, since=watermark,
            )
        except Exception:
            _LOGGER.exception(
                "feed memorializer: list_since failed post=%s", post.id,
            )
            return None
        # Drop the character's own scheduler-tick replies — those are
        # *outbound* and already memorialised by ``FeedCommentReplyService``
        # with the correct first-person framing. Without this filter the
        # next pass would fold the character's reply into a "user said …"
        # memory and warp recall.
        new_comments = [
            c for c in new_comments_all if c.author_id != character_id
        ]

        candidates: list[datetime] = []
        candidates.extend(r.created_at for r in new_likes)
        candidates.extend(c.created_at for c in new_comments)
        if not candidates:
            return None
        return _PostDelta(
            post=post,
            new_likes=list(new_likes),
            new_comments=list(new_comments),
            latest_event_at=max(candidates),
        )

    async def _resolve_operator_language(self, character_id: str) -> str:
        """Resolve the owning operator's content language.

        Falls back to the ship-first ``zh-TW`` when either the character
        repository or the profile service is missing, or resolution
        fails (legacy / tests). Mirrors
        ``schedule_memorializer._resolve_operator_language``."""
        if (
            self._character_repository is None
            or self._operator_profile_service is None
        ):
            return _DEFAULT_OPERATOR_LANGUAGE
        try:
            character = await self._character_repository.get(character_id)
        except Exception:  # pragma: no cover - defensive
            return _DEFAULT_OPERATOR_LANGUAGE
        if character is None:
            return _DEFAULT_OPERATOR_LANGUAGE
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await self._operator_profile_service.get_for_user(
                user_id,
            )
        except Exception:  # pragma: no cover - defensive
            return _DEFAULT_OPERATOR_LANGUAGE
        if operator is None:
            return _DEFAULT_OPERATOR_LANGUAGE
        lang = (getattr(operator, "primary_language", "") or "").strip()
        return lang or _DEFAULT_OPERATOR_LANGUAGE

    def _delta_to_memory(
        self,
        *,
        character_id: str,
        delta: _PostDelta,
        language: str = _DEFAULT_OPERATOR_LANGUAGE,
    ) -> MemoryItem:
        excerpt = _shorten(delta.post.content_text, _PREVIEW_CHARS)
        parts: list[str] = [
            localized_fallback_text(
                "memory.feed_reaction_post_reference", language,
                excerpt=excerpt,
            ),
        ]
        if delta.new_likes:
            parts.append(
                localized_fallback_text(
                    "memory.feed_reaction_liked", language,
                    count=len(delta.new_likes),
                ),
            )
        if delta.new_comments:
            preview_join = localized_fallback_text(
                "memory.feed_reaction_preview_join", language,
            )
            previews = [
                f"「{_shorten(c.content_text, _COMMENT_PREVIEW_CHARS)}」"
                for c in delta.new_comments[:3]
            ]
            extra = ""
            if len(delta.new_comments) > 3:
                extra = localized_fallback_text(
                    "memory.feed_reaction_comment_extra_count", language,
                    count=len(delta.new_comments),
                )
            parts.append(
                localized_fallback_text(
                    "memory.feed_reaction_commented", language,
                    previews=preview_join.join(previews), extra=extra,
                ),
            )
        part_join = localized_fallback_text(
            "memory.feed_reaction_part_join", language,
        )
        content = part_join.join(parts)
        salience = _COMMENT_SALIENCE if delta.new_comments else _LIKE_SALIENCE
        tags = ("feed_reaction",)
        if delta.new_comments:
            tags = ("feed_reaction", "feed_comment")
        return MemoryItem.create(
            character_id=character_id,
            kind=MemoryKind.EPISODIC,
            content=content,
            salience=salience,
            tags=tags,
        )


def _ensure_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _shorten(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)] + "…"
