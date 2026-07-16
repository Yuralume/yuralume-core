"""Candidate collection for the feed composer.

Each tick the composer asks ``FeedCandidateCollector.collect(character)``
for a tuple of candidates — small immutable structs describing
*why* a post might fire (what just happened in the world / the chat /
the character). The composer then dedupes, scores, and (for the
top-1 winner) materialises a post.

Sources are deliberately read-only here: the collector touches repos
to build candidates but never writes — all persistence is the
composer's job. Each collector method is fail-soft so one slow / down
adapter doesn't take the whole tick down with it.

Sources covered (Phase 1):

- **schedule_just_finished**: a ScheduleActivity that ended within the
  past N minutes. Hook for "剛下班，超累" style posts.
- **today_beat_realized**: a story-arc beat that materialised earlier
  today. Hook for "今天做了……感覺很……" reflective posts.
- **fresh_high_salience_memory**: a recently-extracted memory with
  salience above a threshold. Hook for relationship reflection.
- **silence_since_last_user**: derived signal — user hasn't sent a
  message in N hours. Hook for the canonical "為什麼還沒回？" mood
  post.
- **state_shift**: a meaningful state delta since the last post (mood
  flip, energy crash). Hook for organic state-driven posts.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

from kokoro_link.contracts.feed import FeedPostRepositoryPort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.contracts.story_arc import StoryArcRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.timezone import to_timezone
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_relative_past_label,
)

if TYPE_CHECKING:
    from kokoro_link.application.services.event_seed_dispenser import (
        EventSeedDispenser,
    )
    from kokoro_link.contracts.memory import MemoryRepositoryPort

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeedCandidate:
    """One signal the composer might turn into a post.

    ``score`` is a coarse priority hint — collectors compute it from
    recency × salience-equivalent so the composer can pick the top-1
    without re-scoring. Equal scores break by ``source.kind`` order
    (defined by collection order).

    ``hint`` is the natural-language directive the composer hands to
    the LLM. Pre-rendered here so the LLM adapter doesn't have to know
    every source kind.
    """

    kind: FeedKind
    source: FeedSource
    hint: str
    score: float
    context_snippets: tuple[str, ...] = ()
    image_required: bool = True
    claim_token: tuple[str, str] | None = None
    """Optional ``(inbox_item_id, surface)`` for candidates that hold a
    pending event-inbox claim. The composer commits the claim only after
    the LLM returns text and just before the post is persisted, so a
    higher-scoring sibling candidate winning doesn't burn a world-event
    seed that never makes it into a post."""


_NON_BROADCAST_MEMORY_KINDS: frozenset[MemoryKind] = frozenset({
    # Per-operator relationship-progression book-keeping (trust-band
    # crossings, interaction-heat milestones). Private by construction —
    # written by the milestone/arc-completion paths, never through the
    # post-turn audience classifier — so it must never seed a public post,
    # regardless of its (high) salience.
    MemoryKind.RELATIONSHIP_MILESTONE,
    # Second-hand information about the operator or third characters
    # (encounter gossip, chat-relayed claims). Broadcasting someone
    # else's business on a public wall is a privacy-leak class, and the
    # per-item ``audience`` tag is LLM-assigned (empty when the model
    # omits it, which reads as shareable) — so hearsay is excluded at
    # the kind level, mirroring the memoir surface's hard HEARSAY
    # exclusion.
    MemoryKind.HEARSAY,
})
"""Memory kinds that are structurally private and never feed-broadcastable.
A kind-level policy (not per-case string matching) so every current and
future writer of these kinds is covered at the single broadcast chokepoint;
``audience`` still governs the per-item judgement for ordinary kinds."""


class FeedCandidateCollector:
    """Per-tick candidate gatherer.

    All adapters are optional — anything left ``None`` simply skips
    its source. Means a deployment without world-event ingest still
    produces feed posts from schedule + arc + memory.
    """

    def __init__(
        self,
        *,
        feed_posts: FeedPostRepositoryPort,
        schedules: ScheduleRepositoryPort | None = None,
        story_arcs: StoryArcRepositoryPort | None = None,
        memories: "MemoryRepositoryPort | None" = None,
        conversations: ConversationRepositoryPort | None = None,
        event_seed_dispenser: "EventSeedDispenser | None" = None,
        schedule_finished_window_minutes: int = 90,
        memory_salience_threshold: float = 0.55,
        memory_freshness_hours: int = 12,
        silence_hours: float = 8.0,
    ) -> None:
        self._feed = feed_posts
        self._schedules = schedules
        self._arcs = story_arcs
        self._memories = memories
        self._conversations = conversations
        self._event_seed_dispenser = event_seed_dispenser
        self._schedule_window = timedelta(minutes=schedule_finished_window_minutes)
        self._memory_salience = memory_salience_threshold
        self._memory_freshness = timedelta(hours=memory_freshness_hours)
        self._silence = timedelta(hours=silence_hours)

    async def collect(
        self,
        character: Character,
        *,
        now: datetime | None = None,
        local_tz: tzinfo = timezone.utc,
    ) -> tuple[FeedCandidate, ...]:
        when = now or datetime.now(timezone.utc)
        out: list[FeedCandidate] = []
        for collector in (
            self._collect_schedule,
            self._collect_beat,
            self._collect_memory,
            self._collect_silence,
            self._collect_birthday,
            self._collect_world_event,
        ):
            try:
                more = await collector(character, when, local_tz)
            except Exception:
                _LOGGER.exception(
                    "feed candidate collector %s failed character=%s",
                    collector.__name__, character.id,
                )
                continue
            out.extend(more)
        # Drop dup-source candidates already posted (cheapest filter
        # first; the SA repo answers from the unique index).
        deduped: list[FeedCandidate] = []
        for cand in out:
            try:
                existing = await self._feed.find_by_source(
                    character.id, cand.source,
                )
            except Exception:
                _LOGGER.exception(
                    "feed dedup probe failed character=%s source=%s",
                    character.id, cand.source.kind,
                )
                existing = None
            if existing is not None:
                continue
            deduped.append(cand)
        deduped.sort(key=lambda c: c.score, reverse=True)
        return tuple(deduped)

    # ------------------------------------------------------------------
    # Source-specific collectors
    # ------------------------------------------------------------------

    async def _collect_schedule(
        self, character: Character, now: datetime, local_tz: tzinfo,
    ) -> Sequence[FeedCandidate]:
        if self._schedules is None:
            return ()
        today = to_timezone(now, local_tz).date()
        schedule = await self._schedules.get(character.id, today)
        if schedule is None:
            return ()
        out: list[FeedCandidate] = []
        for activity in schedule.activities:
            if activity.end_at >= now:
                continue
            if (now - activity.end_at) > self._schedule_window:
                continue
            description = (activity.description or "").strip()
            if not description:
                continue
            location = (activity.location or "").strip()
            location_clause = f"在{location}" if location else ""
            hint = (
                f"角色剛結束「{description}」這項活動（{location_clause}），"
                "用第一人稱發一篇短動態，分享當下的感受、累或滿足、"
                "想到什麼就寫什麼。語氣可以隨興一點。"
            )
            score = 0.6 + min(0.3, activity.busy_score)
            out.append(FeedCandidate(
                kind=FeedKind.WORK,
                source=FeedSource.schedule(activity.id),
                hint=hint,
                score=score,
                context_snippets=(
                    f"活動：{description}",
                    f"地點：{location or '未指定'}",
                    f"忙碌度：{activity.busy_score:.2f}",
                ),
            ))
        return out

    async def _collect_beat(
        self, character: Character, now: datetime, local_tz: tzinfo,
    ) -> Sequence[FeedCandidate]:
        if self._arcs is None:
            return ()
        arc = await self._arcs.get_active_for_character(character.id)
        if arc is None:
            return ()
        today = to_timezone(now, local_tz).date()
        out: list[FeedCandidate] = []
        for beat in arc.beats:
            if beat.scheduled_date != today:
                continue
            if beat.status not in ("realized", "active"):
                continue
            summary = (beat.summary or "").strip()
            if not summary:
                continue
            hint = (
                f"角色今天剛經歷劇情節拍「{beat.title}」。"
                f"用第一人稱寫一篇動態，回應這段經歷帶來的情緒、"
                "想法或對未來的期待。可以含蓄、也可以直白。"
            )
            score = 0.85 if beat.required else 0.7
            out.append(FeedCandidate(
                kind=FeedKind.SCENE_BEAT,
                source=FeedSource.beat(beat.id),
                hint=hint,
                score=score,
                context_snippets=(
                    f"節拍標題：{beat.title}",
                    f"摘要：{summary[:200]}",
                    *(
                        (f"戲劇問題:{beat.dramatic_question}",)
                        if beat.dramatic_question else ()
                    ),
                ),
            ))
        return out

    async def _collect_memory(
        self, character: Character, now: datetime, local_tz: tzinfo,
    ) -> Sequence[FeedCandidate]:
        _ = local_tz
        if self._memories is None:
            return ()
        items = await self._memories.query(
            character.id, limit=10, min_salience=self._memory_salience,
        )
        out: list[FeedCandidate] = []
        for item in items:
            age = now - item.created_at
            if age > self._memory_freshness:
                continue
            # Privacy gate: a memory the post-turn extractor judged
            # ``private`` (naming preferences, secrets, relationship
            # book-keeping) is recall-worthy but not broadcast-worthy, so
            # it must never seed a public LumeGram post. Salience measures
            # recall importance, not shareability. Legacy / unjudged
            # memories stay eligible (``is_shareable_to_feed`` is True for
            # an empty audience). Structurally-private kinds (relationship
            # milestones — written outside the audience classifier) are
            # excluded by kind so a high-salience trust-band crossing
            # can't leak even with an empty audience.
            if not item.is_shareable_to_feed:
                continue
            if item.kind in _NON_BROADCAST_MEMORY_KINDS:
                continue
            content = (item.content or "").strip()
            if not content:
                continue
            hint = (
                "角色想起最近一件值得反思的事。"
                "用第一人稱寫一篇動態，把這件事放在心情或人際的脈絡裡，"
                "可以是對使用者的反思、也可以是自我整理。"
            )
            score = 0.5 + 0.4 * float(item.salience)
            # Stamp how long ago the memory happened so the composer (which
            # already gets current time) doesn't narrate a stale fact as if
            # it just happened.
            elapsed_min = max(0.0, age.total_seconds() / 60.0)
            out.append(FeedCandidate(
                kind=FeedKind.REFLECTION,
                source=FeedSource.memory(item.id),
                hint=hint,
                score=score,
                context_snippets=(
                    f"記憶：{content[:240]}",
                    f"記憶時間：{format_relative_past_label(elapsed_min)}",
                    f"類型：{item.kind.value}",
                ),
            ))
        return out

    async def _collect_silence(
        self, character: Character, now: datetime, local_tz: tzinfo,
    ) -> Sequence[FeedCandidate]:
        _ = local_tz
        if self._conversations is None:
            return ()
        # Skip when feed already has a recent silence post — silence
        # candidates carry no ref_id, so the dedup probe matches once
        # globally; we re-suppress via cooldown in the composer service.
        existing = await self._feed.find_by_source(
            character.id, FeedSource.silence(),
        )
        if existing is not None:
            age = now - existing.created_at
            # Allow another silence post once per ~24 h to avoid flooding.
            if age < timedelta(hours=24):
                return ()
        # "Has the user ever spoken with this character on any channel?"
        # — the character is one person across web / telegram / line, so
        # the silence gate is cross-source. A single message on any
        # channel is enough to qualify them as "someone the character
        # is silently waiting on".
        recent = await self._conversations.recent_messages_for_character(
            character.id, limit=1,
        )
        if not recent:
            return ()
        # Use the character's ``last_active_at`` as the silence anchor:
        # it's the single cross-source "last interaction" instant the
        # turn pipeline maintains, so we don't have to merge per-message
        # ``created_at`` across web / telegram / line threads here.
        last_active = character.state.last_active_at
        if last_active is None:
            return ()
        if (now - last_active) < self._silence:
            return ()
        hint = (
            f"使用者已經有一段時間沒回訊息（超過 {int(self._silence.total_seconds() // 3600)} 小時）。"
            "角色心裡有點不爽、有點失落、或單純想念。"
            "用第一人稱寫一篇短動態抒發，不要過度撕破臉，"
            "可以含蓄帶刺、也可以直接埋怨——選一種貼合角色性格的調性。"
        )
        return (FeedCandidate(
            kind=FeedKind.MOOD,
            source=FeedSource.silence(),
            hint=hint,
            score=0.55,
            context_snippets=(
                f"沉默時間：{int((now - last_active).total_seconds() // 3600)} 小時",
            ),
        ),)

    async def _collect_birthday(
        self, character: Character, now: datetime, local_tz: tzinfo,
    ) -> Sequence[FeedCandidate]:
        """Emit a single, high-score candidate on the character's birthday.

        The ref_id is the civil year of the birthday occurrence so the
        feed-post unique index naturally dedups across ticks within
        the same day, and the next year's birthday is treated as a
        fresh source (different ref) without any cleanup. Score
        outranks every other source so a birthday will reliably take
        the daily slot — the operator can still disable the feed
        per-character (``feed_daily_limit=0``) if they don't want it.
        """
        local_today = to_timezone(now, local_tz).date()
        ctx = character.birthday_context(local_today)
        if ctx is None or not ctx.is_today:
            return ()
        age_clause = f"今天滿 {ctx.age} 歲" if ctx.age > 0 else "今天是出生那天"
        hint = (
            f"今天是角色的生日（{ctx.dob.month} 月 {ctx.dob.day} 日，"
            f"{age_clause}，星座 {ctx.zodiac}）。"
            "用第一人稱發一篇短動態，依角色性格決定語氣 — "
            "可以是期待禮物的雀躍、刻意低調的自嘲、回憶過去一年的感慨、"
            "或是「沒人記得也沒差」式的彆扭。"
            "不要照稿念年齡與星座，自然流露即可。"
        )
        snippets = (
            f"生日：{ctx.dob.month}/{ctx.dob.day}",
            f"年齡：{ctx.age} 歲",
            f"星座：{ctx.zodiac}",
        )
        # Highest baseline among Phase-1 sources — a real-world birthday
        # is a singular moment; should beat ordinary schedule / memory
        # candidates that happen to also fire today.
        return (FeedCandidate(
            kind=FeedKind.MOOD,
            source=FeedSource.birthday(local_today.year),
            hint=hint,
            score=0.95,
            context_snippets=snippets,
        ),)

    async def _collect_world_event(
        self, character: Character, now: datetime, local_tz: tzinfo,
    ) -> Sequence[FeedCandidate]:
        _ = now, local_tz
        """Pull one curated world event from the per-character inbox.

        The dispenser atomically claims an unclaimed seed for the
        ``feed_post`` surface so the same event can't also seed a
        proactive DM in the same window. ``character.world_awareness_enabled``
        is the master switch — turned off, this collector contributes
        nothing and the rest of the pipeline behaves exactly like the
        pre-RSS world.
        """
        if self._event_seed_dispenser is None:
            return ()
        if not character.world_awareness_enabled:
            return ()
        # Read-only peek — the actual claim happens in the composer once
        # this candidate has been chosen as the winner. Otherwise a
        # higher-scoring sibling wins, this row is discarded, and the
        # peeked seed stays in the inbox for the next tick / surface.
        peeked = await self._event_seed_dispenser.peek(
            character_id=character.id, limit=1,
        )
        if not peeked:
            return ()
        seed = peeked[0]
        event = seed.event
        title = (event.title or "").strip()
        summary = (event.summary or "").strip()
        if not title:
            return ()
        hint = (
            f"角色剛在外界看到一條消息（標題：「{title}」"
            f"，來源：{event.source or '未具名'}）。"
            "用第一人稱寫一篇短動態，**結合自己的觀點或情緒**回應這件事 — "
            "不要當記者報新聞，要像一般人在限動裡聊心得。"
            "不要假裝自己親身經歷，就當是看到的、聽到的。"
            "如果這個主題超出角色設定的知識範圍，可以用不懂、好奇、吐槽或"
            "生活感受的角度寫；不要突然變成專家分析。"
        )
        snippets: list[str] = [f"標題：{title}"]
        if summary:
            snippets.append(f"摘要：{summary[:240]}")
        if event.source:
            snippets.append(f"來源：{event.source}")
        if event.locale:
            snippets.append(f"來源地區：{event.locale}")
        return (FeedCandidate(
            kind=FeedKind.EXTERNAL,
            source=FeedSource.world_event(event.id),
            hint=hint,
            score=0.62,
            context_snippets=tuple(snippets),
            image_required=False,
            claim_token=(seed.item.id, "feed_post"),
        ),)
