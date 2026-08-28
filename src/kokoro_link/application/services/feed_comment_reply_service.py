"""Phase B — character replies to user comments on LumeGram posts.

Driven by ``ProactiveScheduler._tick_all`` (one ``tick(character)``
call per scheduler pass), mirroring ``FeedComposerService`` so the
fail-soft contract stays uniform: an LLM hiccup, a slow repo, or a
character who shouldn't reply right now must all degrade to "do
nothing this tick" without disturbing the rest of the scheduler loop.

## Why scheduler tick, not inline

Memorialised in ``project_feed_phase_b.md`` — the user (2026-04-30)
explicitly chose tick-driven over inline. The IG metaphor has the
character "online sometimes": a reply may land minutes or hours after
the comment, conditioned on whether the character is currently free.
Inline replies break that illusion and also burn LLM calls on every
keystroke-batch the user types.

## Algorithm (per character per tick)

1. Cheap gate — ``feed_daily_limit > 0`` flips the whole feature off.
2. Walk the recent posts (cap 20) once. For each post:

   * sort comments chronologically;
   * the watermark is the latest character-authored comment's
     ``created_at`` on that post (or epoch);
   * user comments after the watermark form the "unanswered batch".

3. Aggregate two cross-post metrics in the same pass:

   * the **latest** character-comment timestamp across all scanned
     posts → drives the global reply cooldown;
   * the count of character comments **today** (character-local
     date) → drives the daily reply cap.

4. Pick the post whose unanswered batch's *oldest* user comment is
   the earliest — FIFO fairness, the user's first unanswered
   complaint shouldn't get leapfrogged.
5. Reject the candidate if:

   * the batch's oldest comment is younger than ``min_wait`` (gives
     the user time to finish typing follow-ups before the character
     chimes in — IG-realistic);
   * the batch's newest comment is older than ``max_age`` (the
     conversation has cooled — replying now feels weird);
   * cooldown / daily cap not satisfied;
   * the character's *current* schedule activity is high-busy (we
     skip; next tick re-evaluates).

6. Compose via :class:`FeedCommentReplyComposerPort`. Empty body =>
   skip (do not advance any watermark; next tick retries).
7. Persist via :class:`FeedCommentService.add` with
   ``author_id=character.id`` so the same row layout that holds user
   comments now hosts character replies (no schema change).
8. Memorialise: write one episodic memory linking the user's batch
   to the character's reply so future chats can reference both
   sides naturally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from kokoro_link.application.services.feed_comment_service import (
    FeedCommentService,
    FeedPostNotFound,
)
from kokoro_link.application.services.feed_event_bus import (
    FeedCommentReplyEvent,
    FeedEventBus,
)
from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.application.services.notification_service import NotificationService
from kokoro_link.application.services.output_quality import (
    OutputQualityOrchestrator,
    OutputQualityPolicy,
    length_overrun_lines,
)
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.feed import (
    FeedCommentRepositoryPort,
    FeedPostRepositoryPort,
)
from kokoro_link.contracts.feed_comment_reply import (
    FeedCommentReplyComposerPort,
    FeedCommentReplyInput,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.novelty_gate import NoveltyGateContext
from kokoro_link.contracts.visible_slots import (
    SLOT_KIND_FEED_REPLY,
    VisibleSlotPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_comment import (
    LOCAL_COMMENTER_ID,
    FeedComment,
)
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.memory_item import (
    PLAYER_KNOWLEDGE_SHARED,
    MemoryItem,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.timezone import timezone_for_id

_LOGGER = logging.getLogger(__name__)

_RECENT_POSTS_TO_SCAN = 20
"""Cap on posts inspected per tick. Older posts are unlikely to have
unanswered comments worth surfacing — if the user wanted a reply on
something from two weeks ago they'd nudge in chat. Bumping this is one
config flip; the cost is one extra ``list_for_post`` call per post."""

_COMMENTS_PER_POST = 100
"""Page size when fetching comments for a single scanned post. The
practical comment count per post is far below this; the value just
prevents an unbounded fetch on a hypothetical viral post."""

_DEFAULT_MIN_WAIT = timedelta(minutes=2)
"""Don't reply within this window of the user's latest unanswered
comment. Lets the user finish a thought before the character chimes
in — and dampens cost when the user types three short comments in 30
seconds (we still reply once, addressing all of them)."""

_DEFAULT_MAX_AGE = timedelta(hours=48)
"""Stale cut-off: an unanswered comment older than this is treated as
"the moment passed" — replying now feels off. Memorialiser still
folds it into character memory via the existing watermark flow."""

_DEFAULT_COOLDOWN = timedelta(minutes=20)
"""Floor between two consecutive character replies (across all posts)
for the same character. Prevents reply storms when the user spreads
comments across multiple posts at once. Independent from the post
composer's own 90-min cooldown — these are different signals."""

_DEFAULT_DAILY_CAP = 6
"""Hard cap on character-authored replies per character per civil day
(character's local timezone). Tuned to match the post composer's
default rhythm (3 posts/day) × 2 — replies are cheaper to author than
posts (no image, shorter prompt), but we still want a ceiling so a
chatty user can't drain the LLM budget on LumeGram alone."""

_REPLY_CAP_CHARS = 180
"""Hard length ceiling enforced *after* the output-quality gate has
reviewed the draft (QG3). Mirrors ``llm_comment_reply.MAX_REPLY_CHARS``
— the target length the prompt states — but the two constants live in
different layers on purpose: this service works against
``FeedCommentReplyComposerPort`` generically and must not reach into a
specific adapter's internals to learn the cap. Change the target
length in both places together.

Applied unconditionally, including when no gate is wired, so the
observable output (what actually gets persisted) is unchanged from
before QG3 in that configuration — only the truncation's *position*
moved, from inside the composer to here, so the gate gets to see the
overrun before anything is cut."""

_BUSY_THRESHOLD = 0.85
"""Schedule activity busy_score at or above which the character won't
reply this tick. Mirrors the existing prompt-tone heuristic — at this
level the character is in deep work / meeting / crunch-mode. The
unanswered batch stays pending; the next tick after the busy block
ends will reconsider."""


class FeedCommentReplyService:
    """Tick-driven LumeGram comment-reply composer."""

    def __init__(
        self,
        *,
        post_repository: FeedPostRepositoryPort,
        comment_repository: FeedCommentRepositoryPort,
        comment_service: FeedCommentService,
        composer: FeedCommentReplyComposerPort,
        memory_repository: MemoryRepositoryPort | None = None,
        embedder: EmbedderPort | None = None,
        schedule_service: ScheduleService | None = None,
        local_tz: ZoneInfo | None = None,
        character_repository: CharacterRepositoryPort | None = None,
        event_bus: FeedEventBus | None = None,
        min_wait: timedelta = _DEFAULT_MIN_WAIT,
        max_age: timedelta = _DEFAULT_MAX_AGE,
        cooldown: timedelta = _DEFAULT_COOLDOWN,
        daily_cap: int = _DEFAULT_DAILY_CAP,
        operator_profile_service=None,  # noqa: ANN001 - optional; resolves primary_language
        notification_service: NotificationService | None = None,
        visible_slot_port: VisibleSlotPort | None = None,
        player_persona_note_repository=None,  # noqa: ANN001 - PlayerPersonaNoteRepositoryPort | None
        # QG0 — the shared review→regenerate→dispose band, taken now so the
        # container wiring lands once. This is the one player-visible
        # surface that has never had *any* quality gate; QG3 turns it on
        # through this orchestrator alone, which is why no separate gate
        # port or register profiler is threaded in beside it.
        output_quality_orchestrator: OutputQualityOrchestrator | None = None,
        # RC — the deployment's switches for that band, named as every
        # other adopting surface names them. Defaults reproduce exactly
        # what QG3 hard-coded, so an unwired caller is unchanged.
        reply_quality_gate_enabled: bool = True,
        reply_quality_gate_max_retries: int = 1,
    ) -> None:
        self._posts = post_repository
        self._comments = comment_repository
        self._comment_service = comment_service
        self._composer = composer
        self._memory_repo = memory_repository
        self._embedder = embedder
        self._schedule = schedule_service
        self._local_tz = local_tz or timezone.utc
        # Optional — persists the unread-reply counter so the StagePage
        # launcher can render a red dot. Wired in production; tests can
        # leave it ``None`` and the increment becomes a soft no-op.
        self._characters = character_repository
        # Optional — broadcasts to open SSE streams so the badge updates
        # without a refresh. Same fail-soft contract: a missing bus
        # silently drops the broadcast and the next character GET still
        # returns the persisted count.
        self._bus = event_bus
        self._min_wait = min_wait
        self._max_age = max_age
        self._cooldown = cooldown
        self._daily_cap = max(0, daily_cap)
        # FRONTEND_I18N_PLAN — same operator-language fact threaded
        # through the rest of the LLM surfaces so comment replies don't
        # drift into a different language than the post they're on.
        self._operator_profile_service = operator_profile_service
        self._notification_service = notification_service
        self._visible_slot_port = visible_slot_port
        # PP3 — the player's declared identity. A reply is the character
        # talking *to this player*, so the declaration is staging here in
        # a way it is not on the post composer, which addresses nobody.
        self._player_persona_note_repository = player_persona_note_repository
        self._output_quality_orchestrator = output_quality_orchestrator
        # RC. The batch's rollback switch does not remove the orchestrator
        # — it hands it a null gate that passes everything — so without
        # these the "off" position still spent a review per reply and
        # recorded a ``pass`` for it on the scrape.
        self._quality_gate_enabled = bool(reply_quality_gate_enabled)
        self._quality_gate_max_retries = max(0, int(reply_quality_gate_max_retries))

    async def tick(
        self,
        character: Character,
        *,
        now: datetime | None = None,
        logical_slot: str | None = None,
    ) -> FeedComment | None:
        """One pass for ``character``. Returns the reply that landed,
        or ``None`` when nothing was warranted this tick."""
        when = now or datetime.now(timezone.utc)
        if not self._is_enabled(character):
            return None
        # P3-Dedup §3.4 — claim the reply pass for this tick bucket before
        # scanning / composing. A distributed reclaimed job racing the original
        # on the same character loses the claim and skips the whole pass, so the
        # visible reply is authored at most once per bucket. ``logical_slot`` is
        # ``None`` off the tick path (no manual pass exists) → claim skipped.
        if not await self._claim_reply_slot(character.id, logical_slot):
            _LOGGER.debug(
                "feed reply: slot already claimed, skipping pass character=%s "
                "slot=%s", character.id, logical_slot,
            )
            return None
        local_tz = await self._resolve_operator_timezone(character)
        if await self._is_high_busy(character, when):
            return None

        posts = await self._scan_posts(character.id)
        if not posts:
            return None

        agg = await self._aggregate(character, posts, when, local_tz)
        if agg is None:
            return None
        if agg.last_reply_at is not None and (
            when - agg.last_reply_at
        ) < self._cooldown:
            return None
        if agg.replies_today >= self._daily_cap > 0:
            return None
        if not agg.candidates:
            return None

        candidate = self._pick_candidate(agg.candidates, when)
        if candidate is None:
            return None

        body = await self._compose_body(character, candidate)
        if not body:
            return None

        try:
            stored = await self._comment_service.add(
                post_id=candidate.post.id,
                content_text=body,
                author_id=character.id,
            )
        except FeedPostNotFound:
            # Race: the post was removed between scan and persist.
            return None
        except ValueError:
            _LOGGER.warning(
                "feed reply: composer produced invalid comment body "
                "character=%s post=%s",
                character.id, candidate.post.id, exc_info=True,
            )
            return None
        except Exception:
            _LOGGER.exception(
                "feed reply: persist failed character=%s post=%s",
                character.id, candidate.post.id,
            )
            return None

        unread = await self._bump_unread(character.id)
        await self._notify_web_push(character, stored)
        await self._broadcast(character.id, candidate.post.id, stored, unread)
        await self._memorialize(character, candidate, stored)
        return stored

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    async def _claim_reply_slot(
        self, character_id: str, logical_slot: str | None,
    ) -> bool:
        """Own this character's reply pass for ``logical_slot``.

        ``True`` (proceed) when nothing to claim (no slot port, or off-tick
        ``logical_slot is None``) or the claim wins. ``False`` only when the
        slot is verifiably owned by another executor. A slot-store error fails
        OPEN — a claim failure must never suppress a single-runner reply."""
        if self._visible_slot_port is None or logical_slot is None:
            return True
        try:
            return await self._visible_slot_port.claim(
                character_id, SLOT_KIND_FEED_REPLY, logical_slot,
            )
        except Exception:
            _LOGGER.exception(
                "feed reply: visible slot claim crashed; failing open "
                "character=%s slot=%s", character_id, logical_slot,
            )
            return True

    def _is_enabled(self, character: Character) -> bool:
        # Reuse the same toggle that controls auto-posts: a character
        # whose feed is fully disabled also shouldn't reply. Keeps the
        # mental model "LumeGram is one feature" — Phase B's reply
        # is part of it, not an independently-flagged sub-feature.
        return character.feed_daily_limit > 0

    async def _is_high_busy(
        self, character: Character, now: datetime,
    ) -> bool:
        """True when the character's current activity is high-busy.

        Resolved via :class:`ScheduleService` so the busy_score from
        the live activity row drives the gate. A missing schedule, a
        gap between activities, or a wiring without ``schedule_service``
        all return False — the gate degrades to "not busy" rather than
        blocking replies forever in a misconfigured environment.
        """
        if self._schedule is None:
            return False
        try:
            response = await self._schedule.current_activity_response(
                character.id, now=now, character=character,
            )
        except Exception:
            _LOGGER.exception(
                "feed reply: schedule lookup crashed character=%s",
                character.id,
            )
            return False
        current = response.current
        if current is None:
            return False
        return current.busy_score >= _BUSY_THRESHOLD

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    async def _scan_posts(self, character_id: str) -> list[FeedPost]:
        try:
            return await self._posts.list_for_character(
                character_id, limit=_RECENT_POSTS_TO_SCAN,
            )
        except Exception:
            _LOGGER.exception(
                "feed reply: list posts failed character=%s",
                character_id,
            )
            return []

    async def _aggregate(
        self,
        character: Character,
        posts: list[FeedPost],
        now: datetime,
        local_tz: tzinfo,
    ) -> "_TickAggregate | None":
        local_today = self._local_today(now, local_tz)
        candidates: list[_PostCandidate] = []
        last_reply_at: datetime | None = None
        replies_today = 0

        for post in posts:
            try:
                comments = await self._comments.list_for_post(
                    post.id, limit=_COMMENTS_PER_POST,
                )
            except Exception:
                _LOGGER.exception(
                    "feed reply: list comments failed post=%s",
                    post.id,
                )
                continue
            if not comments:
                continue
            chronological = sorted(comments, key=lambda c: c.created_at)
            character_comments = [
                c for c in chronological if c.author_id == character.id
            ]
            if character_comments:
                last_for_post = character_comments[-1].created_at
                if last_reply_at is None or last_for_post > last_reply_at:
                    last_reply_at = last_for_post
                replies_today += sum(
                    1 for c in character_comments
                    if self._to_local_date(c.created_at, local_tz) == local_today
                )
            watermark = (
                character_comments[-1].created_at
                if character_comments
                else None
            )
            # User-authored comments are anything not by the character.
            # Pre-auth this was hard-coded to ``LOCAL_COMMENTER_ID``; the
            # multi-user route layer now stamps comments with the real
            # user id (= character owner), so "not the character" is the
            # right filter and stays valid for both modes.
            unanswered = [
                c for c in chronological
                if c.author_id != character.id
                and (watermark is None or c.created_at > watermark)
            ]
            if unanswered:
                candidates.append(_PostCandidate(post=post, unanswered=unanswered))

        return _TickAggregate(
            candidates=candidates,
            last_reply_at=last_reply_at,
            replies_today=replies_today,
        )

    def _pick_candidate(
        self,
        candidates: list["_PostCandidate"],
        now: datetime,
    ) -> "_PostCandidate | None":
        eligible: list[_PostCandidate] = []
        for c in candidates:
            oldest = c.unanswered[0].created_at
            newest = c.unanswered[-1].created_at
            if (now - newest) < self._min_wait:
                # User may still be typing follow-ups; wait one tick.
                continue
            if (now - oldest) > self._max_age:
                # Conversation has cooled; replying now feels off.
                continue
            eligible.append(c)
        if not eligible:
            return None
        # FIFO fairness: oldest unanswered batch wins. If two posts tie
        # (same first-unanswered ts, unlikely) the more recently
        # commented one breaks the tie since it's "warmer".
        eligible.sort(
            key=lambda c: (c.unanswered[0].created_at, -c.unanswered[-1].created_at.timestamp()),
        )
        return eligible[0]

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    async def _compose_body(
        self,
        character: Character,
        candidate: "_PostCandidate",
    ) -> str:
        operator_language = await self._resolve_operator_language(character)
        persona_note = await self._load_player_persona_note(character)
        raw = await self._compose_once(
            character, candidate, operator_language, persona_note,
        )
        if not raw:
            return ""

        orchestrator = self._output_quality_orchestrator
        if orchestrator is None:
            # No gate wired — behaviour must stay exactly what it was
            # before QG3, just with the cap applied here instead of
            # inside the composer.
            return _cap_reply(raw)

        async def regenerate(feedback: str) -> str | None:
            retry = await self._compose_once(
                character, candidate, operator_language, persona_note,
                quality_feedback=feedback,
            )
            return retry or None

        def context_for(reply_text: str) -> NoveltyGateContext:
            return self._reply_gate_context(
                character, candidate, operator_language, reply_text,
            )

        review = await orchestrator.review(
            raw,
            surface="feed_comment",
            context_for=context_for,
            regenerate=regenerate,
            policy=OutputQualityPolicy.BACKGROUND_FAIL_CLOSED,
            character=character,
            max_retries=self._quality_gate_max_retries,
            enabled=self._quality_gate_enabled,
        )
        if review.skipped:
            # A hard defect survived its regeneration — nothing ships
            # this tick. The empty-string return is the same "skip"
            # signal ``tick()`` already understood before QG3.
            return ""
        return _cap_reply(review.final or "")

    async def _compose_once(
        self,
        character: Character,
        candidate: "_PostCandidate",
        operator_language: str,
        persona_note: str,
        *,
        quality_feedback: str = "",
    ) -> str:
        """One LLM round trip. ``quality_feedback`` is empty on the
        ordinary compose and carries the gate's verdict feedback on the
        orchestrator's regeneration call — see
        ``FeedCommentReplyInput.quality_feedback``."""
        try:
            output = await self._composer.compose(FeedCommentReplyInput(
                character=character,
                post=candidate.post,
                user_comments=tuple(candidate.unanswered),
                busy_hint=self._build_busy_hint(character),
                operator_primary_language=operator_language,
                player_persona_note=persona_note,
                quality_feedback=quality_feedback,
            ))
        except Exception:
            _LOGGER.exception(
                "feed reply: composer crashed character=%s post=%s",
                character.id, candidate.post.id,
            )
            return ""
        return (output.content_text or "").strip()

    def _reply_gate_context(
        self,
        character: Character,
        candidate: "_PostCandidate",
        operator_language: str,
        reply_text: str,
    ) -> NoveltyGateContext:
        """QG3's gate context, built entirely from what ``_compose_once``
        already sent the composer — no extra repository query. The post
        body and every unanswered comment but the newest count as
        ``known_material`` (context already established in this thread);
        the newest unanswered comment is what the reply is actually
        answering, so it becomes ``latest_user_message``."""
        unanswered = candidate.unanswered
        latest_message = unanswered[-1].content_text if unanswered else ""
        known_material = tuple(
            text for text in (
                candidate.post.content_text,
                *(c.content_text for c in unanswered[:-1]),
            )
            if text and text.strip()
        )
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        return NoveltyGateContext(
            character_id=character.id,
            operator_id=operator_id,
            response_text=reply_text,
            known_material=known_material,
            latest_user_message=latest_message,
            operator_primary_language=operator_language,
            mechanical_evidence_lines=length_overrun_lines(
                reply_text, _REPLY_CAP_CHARS,
            ),
        )

    async def _load_player_persona_note(self, character: Character) -> str:
        """The player's declared identity for this pair, or nothing.

        Fail-soft in the same direction as the language resolve below: a
        reply written without the declaration is a weaker reply, a reply
        that raised is a comment left hanging.
        """
        repository = self._player_persona_note_repository
        if repository is None:
            return ""
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            row = await repository.get(
                character_id=character.id, operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "feed reply: player persona note load failed character=%s",
                character.id,
            )
            return ""
        return row.note if row is not None else ""

    async def _resolve_operator_language(self, character: Character) -> str:
        """Same shape as the schedule / proactive / feed-composer paths
        — fall through to ``"zh-TW"`` when the service is unwired so
        legacy single-user tests stay green."""
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = getattr(operator, "primary_language", "") or ""
        return lang.strip() or default

    async def _resolve_operator_timezone(self, character: Character) -> tzinfo:
        service = self._operator_profile_service
        if service is None:
            return self._local_tz
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return self._local_tz

    def _build_busy_hint(self, character: Character) -> str:
        # Keep this cheap & deterministic. The composer can lean on
        # the busy_hint to shape tone without needing the raw schedule.
        state = character.state
        bits: list[str] = []
        if state.fatigue >= 70:
            bits.append("有點累")
        if state.energy <= 30:
            bits.append("能量低")
        if state.affection >= 70:
            bits.append(f"對使用者好感不錯（{state.affection}）")
        if not bits:
            return ""
        return "、".join(bits)

    # ------------------------------------------------------------------
    # Badge + broadcast
    # ------------------------------------------------------------------

    async def _bump_unread(self, character_id: str) -> int:
        """Increment the character's unread feed-reply counter.

        Returns the new count; ``0`` when no character repo is wired
        (tests) so the broadcast still carries a sane integer."""
        if self._characters is None:
            return 0
        try:
            character = await self._characters.get(character_id)
        except Exception:
            _LOGGER.exception(
                "feed reply: unread counter read crashed character=%s",
                character_id,
            )
            return 0
        if character is None:
            return 0
        next_count = character.unread_feed_reply_count + 1
        try:
            await self._characters.save(
                character.with_unread_feed_reply(next_count),
            )
        except Exception:
            _LOGGER.exception(
                "feed reply: unread counter persist crashed character=%s",
                character_id,
            )
            return 0
        return next_count

    async def _broadcast(
        self,
        character_id: str,
        post_id: str,
        reply: FeedComment,
        unread_count: int,
    ) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(FeedCommentReplyEvent(
                character_id=character_id,
                post_id=post_id,
                comment_id=reply.id,
                content_text=reply.content_text,
                unread_count=unread_count,
                created_at=reply.created_at,
            ))
        except Exception:
            _LOGGER.exception(
                "feed reply: SSE broadcast failed character=%s reply=%s",
                character_id, reply.id,
            )

    async def _notify_web_push(
        self,
        character: Character,
        reply: FeedComment,
    ) -> None:
        if self._notification_service is None:
            return
        try:
            await self._notification_service.notify_feed_reply(character, reply)
        except Exception:
            _LOGGER.exception(
                "feed reply web push notification failed character=%s reply=%s",
                character.id,
                reply.id,
            )

    # ------------------------------------------------------------------
    # Memorialise
    # ------------------------------------------------------------------

    async def _memorialize(
        self,
        character: Character,
        candidate: "_PostCandidate",
        reply: FeedComment,
    ) -> None:
        if self._memory_repo is None:
            return
        try:
            item = _build_reply_memory(
                character_id=character.id,
                post=candidate.post,
                user_comments=candidate.unanswered,
                reply=reply,
            )
        except Exception:
            _LOGGER.exception(
                "feed reply memorialise: build failed character=%s reply=%s",
                character.id, reply.id,
            )
            return
        try:
            embedded = await attach_embeddings([item], self._embedder)
        except EmbedderError:
            _LOGGER.warning(
                "feed reply memorialise: embedder unavailable, "
                "skipping memory character=%s reply=%s",
                character.id, reply.id,
            )
            return
        except Exception:
            _LOGGER.exception(
                "feed reply memorialise: embedding crashed character=%s reply=%s",
                character.id, reply.id,
            )
            return
        try:
            await self._memory_repo.add_many(embedded)
        except Exception:
            _LOGGER.exception(
                "feed reply memorialise: persist failed character=%s reply=%s",
                character.id, reply.id,
            )

    # ------------------------------------------------------------------
    # tz helpers
    # ------------------------------------------------------------------

    def _local_today(self, now: datetime, local_tz: tzinfo) -> date:
        return now.astimezone(local_tz).date()

    def _to_local_date(self, when: datetime, local_tz: tzinfo) -> date:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(local_tz).date()


# ----------------------------------------------------------------------
# Internal value objects
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PostCandidate:
    """One post's unanswered batch, ready for ranking."""

    post: FeedPost
    unanswered: list[FeedComment]


@dataclass(frozen=True, slots=True)
class _TickAggregate:
    """Cross-post stats accumulated in one scan pass."""

    candidates: list[_PostCandidate]
    last_reply_at: datetime | None
    replies_today: int


# ----------------------------------------------------------------------
# Memory rendering
# ----------------------------------------------------------------------

_USER_PREVIEW_CHARS = 60
_REPLY_PREVIEW_CHARS = 80
_POST_PREVIEW_CHARS = 30
_REPLY_SALIENCE = 0.55
"""A notch above the 0.50 used by the inbound comment memorializer:
this memory captures both sides of the exchange, so it should be
slightly easier for the ranker to surface."""


def _build_reply_memory(
    *,
    character_id: str,
    post: FeedPost,
    user_comments: list[FeedComment],
    reply: FeedComment,
) -> MemoryItem:
    post_excerpt = _shorten(post.content_text, _POST_PREVIEW_CHARS)
    user_previews = "、".join(
        f"「{_shorten(c.content_text, _USER_PREVIEW_CHARS)}」"
        for c in user_comments[:3]
    )
    extra = ""
    if len(user_comments) > 3:
        extra = f"（共 {len(user_comments)} 則）"
    reply_excerpt = _shorten(reply.content_text, _REPLY_PREVIEW_CHARS)
    content = (
        f"我在動態牆「{post_excerpt}」這篇下面回覆了使用者的留言"
        f"{user_previews}{extra}：「{reply_excerpt}」"
    )
    tags: tuple[str, ...] = ("feed", "feed_comment_reply", "self_reply")
    return MemoryItem.create(
        character_id=character_id,
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=_REPLY_SALIENCE,
        tags=tags,
        created_at=reply.created_at,
        # KB6: a two-sided exchange the player started — their comment
        # is the trigger and the reply lands where they will read it.
        player_knowledge=PLAYER_KNOWLEDGE_SHARED,
    )


def _shorten(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)] + "…"


def _cap_reply(text: str) -> str:
    """QG3's post-gate safety net (D6): the same "trim to cap" behaviour
    ``llm_comment_reply._normalize`` used to apply unconditionally,
    moved here so it runs after the gate has seen — and can act on —
    the untruncated draft. If the model produced something coherent
    we'd rather keep the gist than drop the whole reply."""
    if len(text) <= _REPLY_CAP_CHARS:
        return text
    return text[: _REPLY_CAP_CHARS - 1].rstrip() + "…"
