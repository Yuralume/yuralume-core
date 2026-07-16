"""Ports for the character feed-wall layer (動態牆).

Two ports keep concerns separated:

- ``FeedPostRepositoryPort`` — CRUD + queries needed by the API and
  the composer (dedup, today's count). In-memory + SA implementations.
- ``FeedComposerPort`` — the LLM-backed adapter that turns a candidate
  signal into post text. The application service composes candidates
  and calls this port; a ``NullFeedComposer`` no-ops so feature flags
  (no LLM available) cleanly suppress the whole pipeline without the
  caller having to special-case the absence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.feed_reaction import FeedReaction
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource


class FeedPostRepositoryPort(ABC):
    """CRUD + queries for the feed-wall."""

    @abstractmethod
    async def add(self, post: FeedPost) -> None: ...

    @abstractmethod
    async def get(self, post_id: str) -> FeedPost | None: ...

    @abstractmethod
    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 20,
        before: datetime | None = None,
    ) -> list[FeedPost]:
        """Reverse-chronological page. ``before`` is exclusive — pass
        the previous page's oldest ``created_at`` to walk backwards.
        ``limit`` is clamped at the implementation level (typically 100)
        so a malformed query can't exhaust the cursor."""

    @abstractmethod
    async def list_recent(
        self,
        *,
        limit: int = 20,
        before: datetime | None = None,
        character_ids: "Iterable[str] | None" = None,
    ) -> list[FeedPost]:
        """全局倒序頁：所有角色 mixed timeline。

        驅動 LumeGram 的全局牆（``GET /api/v1/feed``）。語意同
        ``list_for_character``，差別只在不過濾 ``character_id``；
        cursor 與 limit 規則一致，由 caller 補 character meta（頭像、
        名字）後再吐到前端。

        Multi-user filter: ``character_ids`` 限定貼文來自指定角色集合，
        通常由 route layer 帶入當前 user 的 owned character id。空集合
        會回 ``[]``，``None`` 維持全域語意（內部 job 用）。"""

    @abstractmethod
    async def count_since(
        self,
        *,
        since: datetime,
        character_ids: "Iterable[str] | None" = None,
    ) -> int:
        """``created_at > since`` 的全域貼文數。

        紅點未讀走前端 watermark，server 只需要對 ``since`` 計數即可，
        不需要存使用者 last_viewed_at — 多裝置情境本專案目前用不到。

        Multi-user filter: ``character_ids`` 同 ``list_recent``，限定
        計數來自指定角色集合；空集合回 0，``None`` 維持全域語意。"""

    @abstractmethod
    async def count_on_date(
        self,
        character_id: str,
        *,
        on: date,
        local_tz: tzinfo = timezone.utc,
    ) -> int:
        """Number of posts a character published on ``on`` (character
        local date semantics). Used by daily-limit guard."""

    @abstractmethod
    async def latest_for_character(
        self, character_id: str,
    ) -> FeedPost | None:
        """The single most-recent post for a character, or ``None``.
        Drives the cooldown gate without paginating."""

    @abstractmethod
    async def find_by_source(
        self,
        character_id: str,
        source: FeedSource,
    ) -> FeedPost | None:
        """Dedup probe: returns the existing post for
        ``(character_id, source)`` if any. ``FeedSource.ref_id`` is
        part of the match — silence-derived posts (ref_id None) are
        treated as one-per-character, with cooldown enforced separately."""

    @abstractmethod
    async def save(self, post: FeedPost) -> None: ...

    @abstractmethod
    async def delete(self, post_id: str) -> bool: ...

    @abstractmethod
    async def delete_for_character(self, character_id: str) -> int: ...


class FeedReactionRepositoryPort(ABC):
    """Persistence for likes (and, later, other reaction shapes).

    Phase A1 only handles likes; the entity carries no kind field, so
    every row in the underlying table is a like. The port keeps the
    door open for kind-tagged reactions later by funnelling all writes
    through ``add`` / ``remove`` rather than exposing kind-specific
    methods.

    Idempotency lives at this layer: ``add`` returning the existing row
    (instead of raising) means a double-tap from the UI is harmless,
    and ``remove`` returns ``False`` when there was nothing to delete
    so the caller can decide whether that's a 404 or a no-op.
    """

    @abstractmethod
    async def add(self, reaction: FeedReaction) -> FeedReaction:
        """Insert ``reaction`` if (post_id, liker_id) is new; otherwise
        return the row that was already there. Either way the caller
        has a canonical FeedReaction to thread back through the
        denormalised count update."""

    @abstractmethod
    async def remove(self, *, post_id: str, liker_id: str) -> bool:
        """Delete the (post_id, liker_id) row; return ``True`` when a
        row was deleted, ``False`` when nothing matched."""

    @abstractmethod
    async def has_liked(self, *, post_id: str, liker_id: str) -> bool:
        """Whether ``liker_id`` has an active like on ``post_id``."""

    @abstractmethod
    async def count_for_post(self, post_id: str) -> int:
        """Total likes for ``post_id``. Cheap; backs the denormalised
        ``FeedReactionSummary.likes`` recount after each toggle."""

    @abstractmethod
    async def list_since(
        self, *, post_id: str, since: datetime | None,
    ) -> list[FeedReaction]:
        """Reactions on ``post_id`` newer than ``since`` (exclusive).
        Used by the A3 ``reactions_seen_at`` flow to memorise the
        delta of unseen likes when the character "checks notifications"."""

    @abstractmethod
    async def liked_post_ids(
        self, *, post_ids: tuple[str, ...], liker_id: str,
    ) -> set[str]:
        """Subset of ``post_ids`` that ``liker_id`` has liked.

        Powers list-side hydration so a feed page doesn't need N
        roundtrips to know which hearts are filled. Empty input → empty
        set without hitting the DB."""


class FeedCommentRepositoryPort(ABC):
    """Persistence for user comments on feed posts.

    Phase A2 only models user → character comments (no replies / no
    character-authored comments). The port stays kind-agnostic so the
    same table can host bot replies later without a schema change.
    """

    @abstractmethod
    async def add(self, comment: FeedComment) -> FeedComment:
        """Persist ``comment``. Unlike likes, comments are not
        idempotent: two visually-identical comments are two rows. Caller
        is responsible for any UI-level dedup."""

    @abstractmethod
    async def get(self, comment_id: str) -> FeedComment | None: ...

    @abstractmethod
    async def remove(self, comment_id: str) -> bool:
        """Hard-delete the comment. Returns ``True`` when a row was
        deleted, ``False`` when nothing matched. Author authorisation is
        enforced one layer up (the service)."""

    @abstractmethod
    async def list_for_post(
        self,
        post_id: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[FeedComment]:
        """Reverse-chronological page of comments on ``post_id``.
        ``before`` is exclusive (oldest item's ``created_at`` from the
        previous page). ``limit`` is clamped at the implementation
        level."""

    @abstractmethod
    async def count_for_post(self, post_id: str) -> int:
        """Total comments on ``post_id``. Backs the denormalised
        ``FeedReactionSummary.comments`` recount after each add/remove."""

    @abstractmethod
    async def list_since(
        self, *, post_id: str, since: datetime | None,
    ) -> list[FeedComment]:
        """Comments on ``post_id`` newer than ``since`` (exclusive). Used
        by the A3 ``reactions_seen_at`` flow."""


@dataclass(frozen=True, slots=True)
class FeedComposerInput:
    """Everything the LLM composer needs to author one post.

    Pre-rendered by the application service from the live candidate so
    the adapter doesn't have to reach back into repositories. The
    ``hint`` is a short instruction the service crafts based on the
    candidate's source kind ("user 已超過 8 小時沒回應，發一段帶點不
    爽但不撕破臉的抒發").
    """

    character: Character
    kind: FeedKind
    source: FeedSource
    hint: str
    """Plain-text directive — guides tone, length, and content focus."""
    context_snippets: tuple[str, ...] = ()
    """Optional supporting bullets (recent chat lines, beat summary,
    memory text) the LLM should draw on. Caller pre-trims; adapter
    just renders into the prompt."""
    image_required: bool = True
    """Whether the composer should also produce a positive image prompt
    for ComfyUI. If ``False`` the service skips the image step entirely
    even when a generator is wired up — used for purely textual
    reflections where a visual would feel forced."""
    calendar_context: str = ""
    """Pre-rendered "today is a holiday / weekday / 連假第 N 天" block
    from :class:`CalendarContextPort`. Empty string = calendar disabled
    / unavailable; composer renders no calendar section in that case."""
    weather_context: str = ""
    """Pre-rendered current-weather block from :class:`WeatherContextPort`
    (台北 / 多雲 / 23°C / 高 26 低 21…). Empty string when disabled or
    lookup failed. Mirrors ``calendar_context`` so feed posts don't
    claim "晴朗的午後" while the character's chat / proactive paths
    know it's raining — the same fact must reach every prompt site."""
    operator_location_context: str = ""
    """Prompt-ready coarse operator location fact. Empty when unset.
    For RSS-backed posts this sits alongside the source locale so the
    LLM can decide geographic relevance without service-side filters."""
    operator_primary_language: str = "zh-TW"
    """BCP 47 tag of the character owner's pinned content language
    (FRONTEND_I18N_PLAN). The composer injects this as a fact line so
    the feed post lands in the operator's chosen language. Defaults to
    ``zh-TW`` for backward compatibility with callers that haven't
    been ported."""
    now: datetime | None = None
    """UTC instant for prompt-side local-current-time rendering."""
    local_tz: tzinfo = timezone.utc
    """Operator timezone used only for prompt-visible civil time."""


@dataclass(frozen=True, slots=True)
class FeedComposerOutput:
    """Result of one composer call.

    Empty ``content_text`` signals "the LLM declined / failed; skip
    publishing". ``image_prompt`` / ``video_prompt`` may be empty even
    when ``image_required=True`` — service degrades to a text-only post
    rather than failing the whole tick.

    ``media_kind`` lets the LLM declare whether the post is best
    accompanied by a still image, a short video clip, or no media at
    all. Values:

      * ``"image"`` (default) — use ``image_prompt`` to render a still.
      * ``"video"`` — use ``video_prompt`` to render a Wan2.2 clip;
        ``image_prompt`` may still be set as a fallback still if video
        generation fails or no video provider is wired.
      * ``"none"`` — text-only post, both prompts ignored.

    Composers that pre-date this field omit ``media_kind`` and the
    service treats them as ``"image"`` for backwards compatibility.
    """

    content_text: str
    image_prompt: str = ""
    video_prompt: str = ""
    media_kind: str = "image"


class FeedComposerPort(ABC):
    """Turn a curated candidate into a post body (and image prompt)."""

    @abstractmethod
    async def compose(self, payload: FeedComposerInput) -> FeedComposerOutput:
        """Compose post body + image prompt for ``payload``.

        Adapters must be fail-soft: any LLM error returns an empty
        output. The service layer treats an empty body as "skip this
        tick" — no exception propagates."""
