"""Per-tick feed composer.

Reads candidates from :class:`FeedCandidateCollector`, picks the
highest-scoring one that passes the daily limit + cooldown gates, asks
the LLM composer for body text + image prompt, optionally generates an
image via ComfyUI, persists the post, and publishes a feed event.

All steps are fail-soft — a slow ComfyUI degrades to a text-only post,
an LLM error skips this tick entirely (no half-baked rows). The
service is stateless; tick safety comes from the repo's daily-count +
``find_by_source`` dedup, not in-process flags.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.feed_candidates import (
    FeedCandidate,
    FeedCandidateCollector,
)
from kokoro_link.application.services.feed_event_bus import (
    FeedEventBus,
    FeedPostEvent,
)
from kokoro_link.application.services.feature_keys import FEATURE_VIDEO_FEED
from kokoro_link.application.services.feed_video_job_service import (
    SUBMIT_DEFERRED,
    FeedVideoDraft,
    FeedVideoJobService,
    ResolvedVideoTarget,
)
from kokoro_link.application.services.image_usage import image_usage_parts_from_provider
from kokoro_link.application.services.location_context import (
    calendar_region_from_operator,
    prompt_location_fact,
    weather_location_from_operator,
)
from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.application.services.official_reference_attachments import (
    official_reference_attachments,
)
from kokoro_link.application.services.quota_overage_service import (
    FEED_POST_OVERAGE,
    OVERAGE_DENIED_TIER_OFF,
    OverageGrant,
    QuotaOverageService,
)
from kokoro_link.application.services.visual_generation_style import (
    VisualGenerationStyleService,
)
from kokoro_link.contracts.calendar_context import CalendarContextPort
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_FEED_POST,
    ACCOUNT_RUNTIME_EVENT_FEED_VIDEO,
    AccountRuntimeUsageRepositoryPort,
)
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.feed_video_debug import (
    STAGE_NO_ASYNC_TARGET,
    STAGE_NO_FIRST_FRAME,
    STAGE_NO_PIPELINE,
    STAGE_VIDEO_DISABLED,
    VideoTriggerReport,
)
from kokoro_link.contracts.feed_video_jobs import PendingFeedVideo
from kokoro_link.contracts.feed import (
    FeedComposerInput,
    FeedComposerOutput,
    FeedComposerPort,
    FeedPostRepositoryPort,
)
from kokoro_link.contracts.generation_usage import (
    UsageEventDraft,
    UsageEventRecorderPort,
)
from kokoro_link.application.services.output_quality import (
    OUTCOME_HARD_DEGRADED,
    OutputQualityOrchestrator,
    OutputQualityPolicy,
    length_overrun_lines,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyGatePort,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort, StoredObject
from kokoro_link.contracts.register_profile import (
    RegisterProfileContext,
    RegisterProfilePort,
)
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.contracts.weather_context import WeatherContextPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_IMAGE,
    CAPABILITY_VIDEO,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    UsageQuantity,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID, OperatorProfile
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import (
    SOURCE_INTERNAL_TEST,
    FeedSource,
)
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.timezone import timezone_for_id, to_timezone
from kokoro_link.infrastructure.feed.llm_composer import MAX_BODY_CHARS
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)

if TYPE_CHECKING:
    from kokoro_link.application.services.event_seed_dispenser import (
        EventSeedDispenser,
    )
    from kokoro_link.application.services.schedule_service import ScheduleService
    from kokoro_link.application.services.notification_service import (
        NotificationService,
    )
    from kokoro_link.contracts.active_image import ActiveImageProviderPort
    from kokoro_link.contracts.active_video import ActiveVideoProviderPort

_LOGGER = logging.getLogger(__name__)

_DEFAULT_COOLDOWN = timedelta(minutes=90)
"""Hard floor between two consecutive posts for the same character.
Tighter than ``proactive_cooldown_minutes`` because the feed is browse-
based (low-attention surface) and we want some rhythm; loose enough
that a 3/day limit + 5-minute tick isn't all clustered around morning.
"""

_RECENT_MEDIA_HISTORY_LIMIT = 5

_DEFAULT_TEST_VIDEO_PROMPT = (
    "gentle idle motion, subtle camera drift, soft ambient light, "
    "natural breathing"
)
"""CV6 fallback ``base_video_prompt`` for the
admin test trigger, which has no LLM-composed draft to fall back to. The
storyboard step reads the *rendered* first frame via vision, so this
text only matters when the storyboard call itself cannot run."""

_HIGH_BUSY_THRESHOLD = 0.85
"""Current-activity floor where automatic feed posting should wait.

The same threshold is used by feed comment replies. At this level the
character is effectively unavailable: sleep, driving, exam, stage,
critical meeting, or similarly no-phone slots. The post candidate stays
unclaimed and can fire on a later tick when the schedule becomes reachable.
"""

_QUALITY_SURFACE = "feed"
"""Counter / log label for this surface's output-quality outcomes."""

_QUALITY_DEGRADE_AXES = frozenset({"tool_prompt_defect"})
"""The one hard axis a feed post can survive by dropping something (D1
exception). A defective ``image_prompt`` costs the post its picture; the
prose the player came for is untouched, and a text-only post is an
ordinary shape on this wall. Every other hard axis is *in* the prose, so
there is nothing to drop and the post is withheld instead."""

_RECENT_SELF_POST_LIMIT = 5
"""How many published posts the gate sees as this character's own recent
voice. The novelty axes need real material to compare against — before
QG2 this surface handed the judge a hard-coded zero and one generic
sentence, which is a self-repetition check that cannot fire."""

_DIVERSITY_PROMPT_LINE = (
    "feed composer context snippets 已提供；請判斷貼文是否像套版或重複同一角度。"
)
"""Kept alongside the real history as a standing instruction — it says
what to look for, where ``recent_self_lines`` says what to look at."""


@dataclass(frozen=True, slots=True)
class _RuntimeFeedQuotaDecision:
    allowed: bool
    claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class _RenderedImage:
    """One image-provider call, decoupled from storage and the ledger.

    The render step deliberately does neither of the two things the old
    single-method shape did on its way out: it never writes an object and
    never files a usage row. It only carries back everything a caller
    needs to do both — because the two callers disagree about *where* the
    bytes go (a post's picture vs a clip's first frame) while agreeing
    exactly on how the attempt is billed.

    ``provider is None`` means no image provider resolved for this
    deployment: nothing was attempted, so nothing may be billed, and
    ``styled_prompt`` still holds the raw prompt (styling happens after
    the provider check, and a prompt no model ever saw must not be
    dressed up as one that was).
    """

    styled_prompt: str
    started_at: datetime
    provider: object | None = None
    profile_id: str = ""
    image_bytes: bytes | None = None
    returned: int = 0
    error: BaseException | None = None
    """The exception the provider raised, kept so the *caller* can log it
    with its own fallback narrative — "text-only post" and "no video job"
    are different stories about the same failure."""
    error_code: str | None = None
    error_message: str | None = None
    typed_failure: bool = False
    """``ImageGenerationError`` (an upstream saying no) rather than an
    unexpected crash. Only the log level and wording differ."""

    @property
    def status(self) -> str:
        return STATUS_FAILED if self.error is not None else STATUS_SUCCEEDED

    @property
    def output_bytes(self) -> int | None:
        return None if self.image_bytes is None else len(self.image_bytes)


@dataclass(frozen=True, slots=True)
class FeedFirstFrame:
    """A rendered opening frame that already lives in feed storage.

    Landed eagerly rather than held in memory because the async video
    pipeline hands the same object to three
    consumers with three different lifetimes: the I2V provider reads
    ``url`` as the frame the clip must start on, a successful post keeps
    it as the poster (``FeedPost.image_url``), and a job that times out,
    fails or is refused becomes a plain image post using *these* bytes —
    never a second render, never a second usage row.
    """

    url: str
    object_key: str
    prompt: str
    """The styled prompt the frame was rendered from — what the degraded
    image post stores as its ``image_prompt``."""
    data: bytes = b""
    """The frame's bytes, kept for the length of the submitting tick only.

    The hosted job wire takes the start frame as bytes, not a URL: the
    worker that renders the clip lives on somebody else's GPU and cannot
    reach Core's object store at all (Media Jobs Service spec §2). They
    are carried rather than re-read because the render just produced them
    — and because a durable record must never hold image bytes, which is
    why the pending row stores only ``url`` / ``object_key``."""


@dataclass(frozen=True, slots=True)
class _TickOutcome:
    """What one candidate produced.

    ``post`` and ``deferred`` are two different kinds of "yes": a post was
    published, or a video job was queued and the post will be published by
    whichever poll sees it finish. The distinction only matters to the two
    callers that decide whether the tick's quota slot was consumed — both
    of those answers are "yes", while a plain ``None`` outcome means the
    candidate produced nothing and the slot goes back.

    ``first_frame`` rides along for the degrade path: the asynchronous
    pipeline may come back with a rendered frame and no job (refused
    queue, unsubmittable prompt), and the post it publishes right there
    must reuse those exact bytes rather than render a second picture.

    ``hard_skipped`` is the D1 signal: the output-quality gate hard-failed
    this candidate (reviewed, regenerated, still failing) rather than the
    composer simply declining to produce anything. Both read as "no post"
    to every other field here, but only a plain no-op should send
    ``_compose_first_viable`` on to the next candidate — a hard failure is
    a reason to go quiet for the whole tick, not a cue to keep trying.
    """

    post: FeedPost | None = None
    deferred: bool = False
    first_frame: "FeedFirstFrame | None" = None
    hard_skipped: bool = False


@dataclass(frozen=True, slots=True)
class _VideoBranch:
    """What the video branch of one tick decided.

    ``outcome is None`` is the self-host answer — *this deployment has no
    asynchronous pipeline*, run the historical inline render — and
    ``resolved`` is the resolution that answer came from, handed back so
    the inline render does not repeat it. Before this existed the tick
    resolved the video provider twice, which self-host paid for on every
    single video post.
    """

    outcome: "_TickOutcome | None"
    resolved: "ResolvedVideoTarget | None" = None


class FeedComposerService:
    """Tick-driven post composer.

    The scheduler calls :meth:`tick` once per (character, tick) — same
    cadence and fail-soft semantics as ``BeatDueChecker`` / rest
    recovery / ensure_schedule. Returns ``None`` when nothing was
    posted; returns the freshly-published ``FeedPost`` otherwise.
    """

    def __init__(
        self,
        *,
        repository: FeedPostRepositoryPort,
        candidates: FeedCandidateCollector,
        composer: FeedComposerPort,
        event_bus: FeedEventBus | None = None,
        image_provider: "ActiveImageProviderPort | None" = None,
        video_provider: "ActiveVideoProviderPort | None" = None,
        uploads_dir: Path | None = None,
        url_prefix: str = "/uploads",
        object_storage: ObjectStoragePort | None = None,
        cooldown: timedelta = _DEFAULT_COOLDOWN,
        memory_repository: MemoryRepositoryPort | None = None,
        embedder: EmbedderPort | None = None,
        event_seed_dispenser: "EventSeedDispenser | None" = None,
        schedule_service: "ScheduleService | None" = None,
        calendar_context_port: CalendarContextPort | None = None,
        weather_context_port: WeatherContextPort | None = None,
        operator_profile_service=None,  # noqa: ANN001 - optional for primary_language
        visual_style_service: VisualGenerationStyleService | None = None,
        usage_recorder: UsageEventRecorderPort | None = None,
        notification_service: "NotificationService | None" = None,
        register_profiler: RegisterProfilePort | None = None,
        register_profile_enabled: bool = False,
        reply_quality_gate: NoveltyGatePort | None = None,
        reply_quality_gate_enabled: bool = False,
        reply_quality_gate_max_retries: int = 1,
        # QG2 — the shared review→regenerate→dispose band, which this
        # surface now runs its composed posts through. ``None`` (self-host,
        # legacy tests) means no gate at all: the composer's output
        # publishes exactly as it did before QG.
        output_quality_orchestrator: OutputQualityOrchestrator | None = None,
        account_runtime_profile_resolver: (
            AccountRuntimeProfileResolverPort | None
        ) = None,
        account_runtime_usage_repository: (
            AccountRuntimeUsageRepositoryPort | None
        ) = None,
        character_repository: CharacterRepositoryPort | None = None,
        quota_overage: "QuotaOverageService | None" = None,
        video_job_service: "FeedVideoJobService | None" = None,
    ) -> None:
        self._repo = repository
        self._candidates = candidates
        self._composer = composer
        self._bus = event_bus
        self._image_provider = image_provider
        self._video_provider = video_provider
        _ = uploads_dir, url_prefix
        self._object_storage = object_storage
        self._cooldown = cooldown
        self._memory_repo = memory_repository
        self._embedder = embedder
        self._event_seed_dispenser = event_seed_dispenser
        self._schedule_service = schedule_service
        # Optional fact-layer ports — same fall-through shape as
        # ScheduleService / ChatService: ``None`` collapses to empty
        # strings on the composer input, prompt builder renders nothing.
        self._calendar_port = calendar_context_port
        self._weather_port = weather_context_port
        # FRONTEND_I18N_PLAN §使用者主要語言 — same operator language
        # signal threaded through chat / proactive / planner so feed
        # posts don't drift into a different output language. Optional
        # so legacy tests / single-user installs continue to default to
        # "zh-TW" without wiring.
        self._operator_profile_service = operator_profile_service
        self._visual_style_service = visual_style_service
        self._usage_recorder = usage_recorder
        self._notification_service = notification_service
        self._register_profiler = register_profiler
        self._register_profile_enabled = bool(register_profile_enabled)
        # QG2 moved the judging itself into the orchestrator, which the
        # container builds around this very gate. The port is still
        # accepted (one construction signature for every gated surface) and
        # kept only so a caller can see what this service is gated by.
        self._reply_quality_gate = reply_quality_gate
        self._reply_quality_gate_enabled = bool(reply_quality_gate_enabled)
        self._reply_quality_gate_max_retries = max(
            0,
            int(reply_quality_gate_max_retries),
        )
        self._output_quality_orchestrator = output_quality_orchestrator
        self._account_runtime_profile_resolver = (
            account_runtime_profile_resolver
            or PermissiveAccountRuntimeProfileResolver()
        )
        self._account_runtime_usage_repository = account_runtime_usage_repository
        self._character_repository = character_repository
        # AP4: the only credit spend in this service, and the only one in the
        # product that a player pre-authorises rather than presses a button
        # for. ``None`` (self-host, legacy tests) means the tier's daily post
        # limit stays the hard wall it has always been.
        self._quota_overage = quota_overage
        # CV4: the deferred video pipeline. ``None`` — and, when wired, a
        # provider that renders synchronously — both mean the composer's
        # historical video branch runs unchanged. Nothing about this field
        # can change a self-host tick.
        self._video_job_service = video_job_service
        # Whether this deployment can queue a render at all. Off (self-host,
        # and hosted until the broker knob is on) means a pending row cannot
        # exist, so the tick must not spend a query looking for one — see
        # ``_has_video_in_flight``.
        self._deferred_video_possible = False

    def set_usage_recorder(self, recorder: UsageEventRecorderPort | None) -> None:
        self._usage_recorder = recorder

    def set_video_job_service(
        self,
        service: "FeedVideoJobService | None",
        *,
        deferred_pipeline_possible: bool = False,
    ) -> None:
        """Close the composer ↔ pipeline cycle after construction.

        The pipeline lands its posts *through* this service (it is the
        lander), so the two cannot both be constructor arguments of each
        other.

        ``deferred_pipeline_possible`` is the deployment-level answer to
        "can a render ever be queued here" — the same condition that
        decides whether a poll carrier is wired at all. It defaults to the
        conservative answer because the *service* is wired unconditionally
        (it is inert without an async-capable provider), so its presence
        alone cannot tell the tick whether pending rows are possible."""
        self._video_job_service = service
        self._deferred_video_possible = bool(
            service is not None and deferred_pipeline_possible,
        )

    async def tick(
        self,
        character: Character,
        *,
        now: datetime | None = None,
    ) -> FeedPost | None:
        when = now or datetime.now(timezone.utc)
        if not self._is_feed_enabled(character):
            return None
        if await self._has_video_in_flight(character):
            # A queued clip *is* this character's next post — it lands as
            # the clip, or as the first frame it was already billed for,
            # but it lands. Until then it holds no ``feed_posts`` row, so
            # the cooldown and daily-count gates below cannot see it and
            # would happily compose a second post minutes later: two posts
            # for one slot, two first-frame renders, and a second video job
            # occupying the queue. Skipping the whole tick (rather than
            # only the video branch) is the point — the tick that submitted
            # already consumed its slot and settled its quota.
            return None
        local_tz = await self._resolve_operator_timezone(character)
        if not await self._gate_passes(character, when, local_tz):
            return None
        if await self._is_current_activity_high_busy(character, when):
            return None
        quota = await self._claim_runtime_feed_post_quota(
            character, when, local_tz,
        )
        overage: OverageGrant | None = None
        if not quota.allowed:
            overage = await self._authorise_feed_post_overage(character, when)
            if not overage.granted:
                # Every denial — tier closed, switch off, ceiling spent, out
                # of 螢火, upstream down — lands here as the historical silent
                # skip. This is a background path: it has no player in front
                # of it to tell, and inventing an error would be worse than
                # the day simply staying quiet.
                return None
        try:
            outcome = await self._compose_first_viable(character, when, local_tz)
        except BaseException:
            await self._discard_runtime_feed_post_claim(quota.claim_id)
            await self._release_feed_post_overage(overage)
            raise
        if outcome.post is None and not outcome.deferred:
            await self._discard_runtime_feed_post_claim(quota.claim_id)
            await self._release_feed_post_overage(overage)
        else:
            # A deferred tick settles like a published one. Once a video job
            # is queued a post *will* appear — the clip if it lands, the
            # first frame if it does not — so handing the slot (and the 螢火
            # it may have cost) back here would give away a free post and let
            # the same character compose again while the first is in flight.
            await self._settle_feed_post_overage(overage)
        return outcome.post

    async def _compose_first_viable(
        self, character: Character, when: datetime, local_tz: tzinfo,
    ) -> "_TickOutcome":
        candidates = await self._candidates.collect(
            character, now=when, local_tz=local_tz,
        )
        if not candidates:
            return _TickOutcome()
        recent_media_kinds = (
            await self._recent_media_kinds(character.id)
            if self._video_provider is not None
            else ()
        )
        # Try candidates in priority order so a composer no-op on the
        # top pick doesn't lose the whole tick — second-best still
        # gets a shot. A *hard* failure is different (D1): the quality
        # gate reviewed this candidate, regenerated it, and it still
        # failed — that is a reason to go quiet for the whole tick, not
        # a cue to burn another compose + judge pass on the next
        # candidate. Falling through here would also let a systemic
        # model-layer problem re-fire the same failure on every
        # remaining candidate, every tick.
        for candidate in candidates:
            outcome = await self._materialise(
                character,
                candidate,
                when,
                recent_media_kinds=recent_media_kinds,
            )
            if outcome.post is not None or outcome.deferred:
                # A deferred candidate consumed the tick as surely as a
                # published one did: its post is queued, and trying the
                # next candidate would compose a *second* post for the
                # same slot.
                return outcome
            if outcome.hard_skipped:
                return outcome
        return _TickOutcome()

    async def _recent_media_kinds(self, character_id: str) -> tuple[str, ...]:
        """Return published media outcomes newest-first, failing soft."""
        try:
            posts = await self._repo.list_for_character(
                character_id,
                limit=_RECENT_MEDIA_HISTORY_LIMIT,
            )
        except Exception:
            _LOGGER.exception(
                "feed: recent media history lookup failed character=%s",
                character_id,
            )
            return ()
        return tuple(_published_media_kind(post) for post in posts)

    async def _authorise_feed_post_overage(
        self, character: Character, now: datetime,
    ) -> OverageGrant:
        """AP4: may this character buy one post past the tier allowance?

        Never raises — an unwired service and every refusal alike come back
        as a denial, so the caller falls through to the silent skip."""
        if self._quota_overage is None:
            return OverageGrant(denied_reason=OVERAGE_DENIED_TIER_OFF)
        return await self._quota_overage.authorise(
            FEED_POST_OVERAGE, operator_id=character.user_id, now=now,
        )

    async def _settle_feed_post_overage(
        self, grant: OverageGrant | None,
    ) -> None:
        """Keep the purchase: the post the player paid for is published.

        ``gateway_delivered=False`` because this is the one action whose
        deliverable is not a Gateway call. Composing a post runs as *background*
        work, which the Gateway never waives, so the covered-call cross-check
        that protects every foreground action from a double bill would refund
        every single post here — while the post itself is live in the feed.
        """
        if self._quota_overage is not None:
            await self._quota_overage.settle(grant, gateway_delivered=False)

    async def _release_feed_post_overage(
        self, grant: OverageGrant | None,
    ) -> None:
        """Give the purchase back when no post was published.

        The composer can no-op on every candidate, and ``_materialise`` can
        roll its own post back; either way the player bought nothing, so the
        credits must not stay reserved."""
        if self._quota_overage is not None:
            await self._quota_overage.release(grant)

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def _is_feed_enabled(self, character: Character) -> bool:
        return character.feed_daily_limit > 0

    async def _gate_passes(
        self, character: Character, now: datetime, local_tz: tzinfo,
    ) -> bool:
        latest = await self._repo.latest_for_character(character.id)
        if latest is not None and (now - latest.created_at) < self._cooldown:
            return False
        local_today = to_timezone(now, local_tz).date()
        today_count = await self._repo.count_on_date(
            character.id, on=local_today, local_tz=local_tz,
        )
        if today_count >= character.feed_daily_limit:
            return False
        return True

    async def _has_video_in_flight(self, character: Character) -> bool:
        """Is a deferred clip already queued for this character?

        Gated on the deployment being able to queue one at all, and that
        gate is what keeps the plan's promise that a self-host tick gains
        **zero** extra queries: without it every tick of every deployment
        would probe a table that, off the hosted broker path, can never
        hold a row. Both execution modes (embedded sweep and distributed
        due-job) share this composer, so one gate covers both."""
        if not self._deferred_video_possible:
            return False
        service = self._video_job_service
        if service is None:
            return False
        return await service.has_in_flight(character.id)

    async def _is_current_activity_high_busy(
        self, character: Character, now: datetime,
    ) -> bool:
        """Return True when auto-posting would contradict the schedule.

        LumeGram comments already wait during high-busy activities; the
        post composer needs the same guard so a birthday, silence, or
        world-event candidate does not publish while the character is
        asleep or otherwise unable to check their phone.
        """
        if self._schedule_service is None:
            return False
        try:
            response = await self._schedule_service.current_activity_response(
                character.id, now=now, character=character,
            )
        except Exception:
            _LOGGER.exception(
                "feed: schedule lookup crashed character=%s; "
                "falling back to allow",
                character.id,
            )
            return False
        current = getattr(response, "current", None)
        if current is None:
            return False
        try:
            busy_score = float(getattr(current, "busy_score"))
        except (AttributeError, TypeError, ValueError):
            return False
        return busy_score >= _HIGH_BUSY_THRESHOLD

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def _describe_calendar(
        self,
        when: datetime,
        local_tz: tzinfo,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Render today's calendar block, empty string when port missing
        or the adapter raises. Mirrors ``ScheduleService._describe_calendar``
        so feed posts see the same shape of fact line as the rest of the
        prompts."""
        if self._calendar_port is None:
            return ""
        try:
            return self._calendar_port.describe(
                to_timezone(when, local_tz).date(),
                region=calendar_region_from_operator(operator),
            )
        except Exception:
            _LOGGER.exception(
                "feed: calendar describe failed character path; "
                "falling back to empty string",
            )
            return ""

    async def _describe_weather(
        self,
        when: datetime,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Async counterpart for the weather port — HTTP-backed adapter
        so we can't go sync without a thread pool. Same fail-soft
        contract as ``_describe_calendar``."""
        if self._weather_port is None:
            return ""
        try:
            return await self._weather_port.describe(
                now=when,
                location=weather_location_from_operator(operator),
            )
        except Exception:
            _LOGGER.exception(
                "feed: weather describe failed; falling back to empty string",
            )
            return ""

    async def _resolve_operator_language(self, character: Character) -> str:
        """Look up the character owner's pinned ``primary_language``.
        Fails soft to ``"zh-TW"`` so a missing service / missing row
        doesn't break feed composition."""
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

    async def _resolve_operator_profile(
        self, character: Character,
    ) -> OperatorProfile | None:
        service = self._operator_profile_service
        if service is None:
            return None
        user_id = getattr(character, "user_id", None) or "default"
        try:
            return await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return None

    async def _resolve_operator_timezone(self, character: Character) -> tzinfo:
        service = self._operator_profile_service
        if service is None:
            return timezone.utc
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return timezone.utc

    async def _materialise(
        self,
        character: Character,
        candidate: FeedCandidate,
        when: datetime,
        *,
        recent_media_kinds: tuple[str, ...] = (),
    ) -> "_TickOutcome":
        operator = await self._resolve_operator_profile(character)
        local_tz = _timezone_for_operator(operator)
        calendar_context = self._describe_calendar(
            when, local_tz, operator=operator,
        )
        weather_context = await self._describe_weather(when, operator=operator)
        operator_language = _operator_language(operator)
        operator_location_context = prompt_location_fact(operator)
        composer_input = FeedComposerInput(
            character=character,
            kind=candidate.kind,
            source=candidate.source,
            hint=candidate.hint,
            context_snippets=candidate.context_snippets,
            recent_media_kinds=recent_media_kinds,
            image_required=candidate.image_required,
            calendar_context=calendar_context,
            weather_context=weather_context,
            operator_location_context=operator_location_context,
            operator_primary_language=operator_language,
            now=when,
            local_tz=local_tz,
        )
        try:
            output = await self._composer.compose(composer_input)
        except Exception:
            _LOGGER.exception(
                "feed composer crashed character=%s source=%s",
                character.id, candidate.source.kind,
            )
            return _TickOutcome()
        text = (output.content_text or "").strip()
        if not text:
            return _TickOutcome()
        gated = await self._gate_feed_output(
            composer_input=composer_input,
            output=output,
            operator=operator,
        )
        if gated is None:
            # D1 background row: reviewed, regenerated, still hard-failing.
            # This tick publishes nothing at all, and the quota slot goes
            # back — same as a plain no-op. What differs is
            # ``hard_skipped``: it tells ``_compose_first_viable`` to stop
            # here rather than spend another compose + judge pass on the
            # next candidate.
            return _TickOutcome(hard_skipped=True)
        output = gated
        # The published cap, applied here and nowhere earlier (D6). By this
        # point an over-long body has been shown to the judge as evidence
        # and given its regeneration; a slice that still lands is a last
        # resort on a draft the gate already accepted, not a silent edit of
        # the model's first answer.
        text = (output.content_text or "").strip()[:MAX_BODY_CHARS]
        if not text:
            return _TickOutcome()
        # Late-bind the world-event claim now that we know this candidate
        # is the one that produced text. Lost race (another surface
        # already claimed) → drop this candidate so the seed isn't
        # double-counted; outer loop falls through to the next.
        if candidate.claim_token is not None and self._event_seed_dispenser is not None:
            item_id, surface = candidate.claim_token
            try:
                committed = await self._event_seed_dispenser.commit(
                    item_id=item_id, surface=surface,
                )
            except Exception:
                _LOGGER.exception(
                    "feed: world-event commit crashed character=%s item=%s",
                    character.id, item_id,
                )
                return _TickOutcome()
            if committed is None:
                _LOGGER.info(
                    "feed: world-event seed lost race character=%s item=%s",
                    character.id, item_id,
                )
                return _TickOutcome()
        # Branch on the LLM's media_kind pick. Video first when chosen
        # (and a provider is wired): success → ship as a video post.
        # Failure or fallback through to image generation so the post
        # still ships with *some* visual rather than an empty card.
        video_url: str | None = None
        video_prompt: str | None = None
        first_frame: FeedFirstFrame | None = None
        skip_image_render = False
        wants_video = (
            output.media_kind == "video"
            and bool(output.video_prompt)
            and self._video_provider is not None
            and await self._runtime_video_generation_enabled(character)
            # CV5 seam: volume control decides *before* any spend — no job,
            # no storyboard call, no first frame. An over-quota character
            # simply takes the ordinary picture path below.
            and await self._video_volume_allows(character, when)
        )
        if wants_video:
            branch = await self._try_deferred_video(
                character, candidate, output, text, when,
            )
            if branch.outcome is not None:
                if branch.outcome.deferred:
                    # Submitted. No post this tick — the poll publishes it.
                    return branch.outcome
                # The asynchronous pipeline ran and decided not to wait:
                # publish now with whatever it managed to render. The image
                # step is skipped either way — a frame already exists, or
                # the render already failed and paying for a second attempt
                # would bill the same picture twice.
                first_frame = branch.outcome.first_frame
                skip_image_render = True
            else:
                # Self-host: the same resolution the branch already made is
                # handed on, so this tick resolves the video provider once.
                video_url, video_prompt = await self._maybe_generate_video(
                    character, output.video_prompt, resolved=branch.resolved,
                )

        image_url: str | None = None
        image_prompt: str | None = None
        if first_frame is not None:
            image_url, image_prompt = first_frame.url, first_frame.prompt
        elif (
            video_url is None
            and output.media_kind != "none"
            and not skip_image_render
        ):
            image_url, image_prompt = await self._maybe_generate_image(
                character, candidate, output.image_prompt,
            )

        post = FeedPost.create(
            character_id=character.id,
            kind=candidate.kind,
            content_text=text,
            source=candidate.source,
            image_url=image_url,
            image_prompt=image_prompt,
            video_url=video_url,
            video_prompt=video_prompt,
            created_at=when,
        )
        try:
            await self._repo.add(post)
        except Exception:
            # ValueError fires when a parallel tick (or chat-driven
            # composer) raced ahead and persisted the same source. Treat
            # as a benign skip — the other branch already published.
            _LOGGER.warning(
                "feed post persist skipped (likely race) character=%s "
                "source=%s",
                character.id, candidate.source.kind,
                exc_info=True,
            )
            if (
                candidate.claim_token is not None
                and self._event_seed_dispenser is not None
            ):
                item_id, surface = candidate.claim_token
                try:
                    await self._event_seed_dispenser.release(
                        item_id=item_id, surface=surface,
                    )
                except Exception:
                    _LOGGER.exception(
                        "feed: world-event release after persist-fail "
                        "crashed character=%s item=%s",
                        character.id, item_id,
            )
            return _TickOutcome()
        await self._publish(post)
        await self._notify_web_push(character, post)
        await self._memorialize(character, post)
        return _TickOutcome(post=post)

    async def _claim_runtime_feed_post_quota(
        self,
        character: Character,
        now: datetime,
        local_tz: tzinfo,
    ) -> _RuntimeFeedQuotaDecision:
        """Reserve one base-quota post for this character's local day.

        The account ceiling remains authoritative.  When it can cover every
        active character, one slot is held for each character until that
        character publishes its first post of the civil day.  Any remaining
        slots are shared normally after those floors have been satisfied.
        """
        try:
            profile = (
                await self._account_runtime_profile_resolver.resolve_for_operator(
                    character.user_id,
                )
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed runtime quota profile check failed (operator=%s)",
                character.user_id,
            )
            return _RuntimeFeedQuotaDecision(allowed=False)
        limit = profile.daily_feed_post_limit
        if limit is None:
            return _RuntimeFeedQuotaDecision(allowed=True)
        usage = self._account_runtime_usage_repository
        if usage is None:
            _LOGGER.error(
                "feed runtime quota ledger is not configured (operator=%s)",
                character.user_id,
            )
            return _RuntimeFeedQuotaDecision(allowed=False)

        day_start = _local_day_start(now, local_tz)
        local_today = to_timezone(now, local_tz).date()
        try:
            active = await self._active_feed_characters(character)
            reservations_enabled = bool(active) and limit >= len(active)
            if active and not reservations_enabled:
                _LOGGER.warning(
                    "feed daily floor cannot cover every active character; "
                    "using account ceiling only operator=%s limit=%s active=%s",
                    character.user_id,
                    limit,
                    len(active),
                )

            effective_limit = limit
            resource_limit: int | None = None
            if reservations_enabled:
                current_used = await usage.count_events(
                    operator_id=character.user_id,
                    event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
                    since=day_start,
                    until=now,
                    resource_id=character.id,
                )
                if current_used == 0:
                    # Rows written before character attribution have NULL
                    # resource_id. The actual feed row preserves fairness
                    # across a mid-day rolling deploy; attributed claims still
                    # provide single-flight before a new post is persisted.
                    current_used = await self._repo.count_on_date(
                        character.id,
                        on=local_today,
                        local_tz=local_tz,
                    )
                if current_used == 0:
                    resource_limit = 1
                else:
                    unserved_others = 0
                    for other in active:
                        if other.id == character.id:
                            continue
                        used = await usage.count_events(
                            operator_id=character.user_id,
                            event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
                            since=day_start,
                            until=now,
                            resource_id=other.id,
                        )
                        if used == 0:
                            used = await self._repo.count_on_date(
                                other.id,
                                on=local_today,
                                local_tz=local_tz,
                            )
                        if used == 0:
                            unserved_others += 1
                    effective_limit = max(0, limit - unserved_others)

            claim_id = await usage.claim_event_slot(
                operator_id=character.user_id,
                event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
                occurred_at=now,
                since=day_start,
                limit=effective_limit,
                resource_id=character.id,
                resource_limit=resource_limit,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed runtime quota claim failed (operator=%s character=%s)",
                character.user_id,
                character.id,
            )
            return _RuntimeFeedQuotaDecision(allowed=False)
        return _RuntimeFeedQuotaDecision(
            allowed=claim_id is not None,
            claim_id=claim_id,
        )

    async def _active_feed_characters(
        self,
        character: Character,
    ) -> tuple[Character, ...]:
        repository = self._character_repository
        if repository is None:
            return (character,)
        characters = await repository.list_for_user(character.user_id)
        active = tuple(
            item
            for item in characters
            if item.feed_daily_limit > 0
            and not item.frozen
            and not item.subscription_locked
        )
        if any(item.id == character.id for item in active):
            return active
        return (*active, character)

    async def _discard_runtime_feed_post_claim(
        self,
        claim_id: str | None,
    ) -> None:
        if claim_id is None or self._account_runtime_usage_repository is None:
            return
        try:
            await self._account_runtime_usage_repository.discard_event(
                event_id=claim_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed runtime quota claim release failed (claim=%s)",
                claim_id,
            )

    async def needs_daily_floor_retry(
        self,
        character: Character,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return whether the character still lacks today's first post."""
        if not self._is_feed_enabled(character):
            return False
        when = now or datetime.now(timezone.utc)
        try:
            profile = (
                await self._account_runtime_profile_resolver.resolve_for_operator(
                    character.user_id,
                )
            )
            if (
                profile.daily_feed_post_limit is not None
                and profile.daily_feed_post_limit <= 0
            ):
                return False
            local_tz = await self._resolve_operator_timezone(character)
            local_today = to_timezone(when, local_tz).date()
            count = await self._repo.count_on_date(
                character.id,
                on=local_today,
                local_tz=local_tz,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed daily floor retry check failed character=%s",
                character.id,
            )
            return False
        return count == 0

    async def _runtime_video_generation_enabled(self, character: Character) -> bool:
        try:
            profile = await self._account_runtime_profile_resolver.resolve_for_operator(
                character.user_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed runtime video profile check failed (operator=%s)",
                character.user_id,
            )
            return False
        return profile.video_generation_enabled

    async def _gate_feed_output(
        self,
        *,
        composer_input: FeedComposerInput,
        output: FeedComposerOutput,
        operator: OperatorProfile | None,
    ) -> FeedComposerOutput | None:
        """QG2: review the draft, regenerate once, decide what publishes.

        ``None`` is the D1 background answer — *publish nothing this tick*.
        It is the whole point of the batch: the 2026-08-26 post shipped
        because the old shape here could only ever return **something**, so
        a regenerated draft that was merely non-empty ended the review.

        The one draft that survives a hard failure is the text-only degrade
        (``_QUALITY_DEGRADE_AXES``): a broken image prompt costs the post
        its picture, not its existence.
        """
        orchestrator = self._output_quality_orchestrator
        if (
            orchestrator is None
            or orchestrator.gate is None
            or not self._reply_quality_gate_enabled
        ):
            # Asked before the evidence is gathered, not after: the
            # register profile is an LLM call and the history window is a
            # query, and a deployment with the gate switched off must not
            # pay for either.
            return output
        character = composer_input.character
        profile = await self._profile_feed_register(composer_input, operator)
        recent_self_lines = await self._recent_post_bodies(character.id)
        diversity = ReplyDiversityEvidence(
            assistant_line_count=len(recent_self_lines),
            phrase_frequency_lines=(_DIVERSITY_PROMPT_LINE,),
        )

        def context_for(draft: FeedComposerOutput) -> NoveltyGateContext:
            # Rebuilt per draft, not hoisted: the mechanical evidence and
            # the tool-prompt lines describe *this* draft, and a re-review
            # that judged the first draft's evidence would be worthless.
            return self._feed_gate_context(
                composer_input=composer_input,
                output=draft,
                operator=operator,
                register_profile=profile,
                diversity_evidence=diversity,
                recent_self_lines=recent_self_lines,
            )

        async def regenerate(feedback: str) -> FeedComposerOutput | None:
            retry_input = replace(
                composer_input,
                hint=(
                    f"{composer_input.hint}\n"
                    f"上一輪貼文品質問題：{feedback}"
                ).strip(),
            )
            try:
                retry_output = await self._composer.compose(retry_input)
            except Exception:
                _LOGGER.exception(
                    "feed composer quality retry crashed character=%s",
                    character.id,
                )
                return None
            # An empty body is the composer's own "I declined"; handing it
            # back as a candidate would let a blank draft be re-reviewed
            # and published as an empty post.
            if not (retry_output.content_text or "").strip():
                return None
            return retry_output

        review = await orchestrator.review(
            output,
            surface=_QUALITY_SURFACE,
            context_for=context_for,
            regenerate=regenerate,
            policy=OutputQualityPolicy.BACKGROUND_FAIL_CLOSED,
            character=character,
            max_retries=self._reply_quality_gate_max_retries,
            enabled=self._reply_quality_gate_enabled,
            degrade_axes=_QUALITY_DEGRADE_AXES,
        )
        if review.outcome == OUTCOME_HARD_DEGRADED and review.final is not None:
            _LOGGER.warning(
                "feed: publishing text-only after an unfixable tool prompt "
                "character=%s", character.id,
            )
            return _without_tool_prompts(review.final)
        return review.final

    async def _recent_post_bodies(self, character_id: str) -> tuple[str, ...]:
        """This character's own recent post bodies, newest first.

        Fails soft to ``()`` — the gate then sees an empty history, which
        is honest (no material to compare against) and is also what a brand
        new character legitimately has.
        """
        try:
            posts = await self._repo.list_for_character(
                character_id, limit=_RECENT_SELF_POST_LIMIT,
            )
        except Exception:
            _LOGGER.exception(
                "feed: recent post history lookup failed character=%s",
                character_id,
            )
            return ()
        return tuple(
            body
            for post in posts
            if (body := (post.content_text or "").strip())
        )

    async def _profile_feed_register(
        self,
        composer_input: FeedComposerInput,
        operator: OperatorProfile | None,
    ):
        if (
            not self._register_profile_enabled
            or self._register_profiler is None
        ):
            return None
        character = composer_input.character
        context = RegisterProfileContext(
            character_id=character.id,
            operator_id=(
                getattr(operator, "id", None)
                or getattr(character, "user_id", DEFAULT_OPERATOR_ID)
            ),
            latest_user_message=composer_input.hint,
            recent_dialogue_summary="\n".join(composer_input.context_snippets),
            relationship_context=(),
            content_tolerance="frontier",
        )
        try:
            return await self._register_profiler.profile(
                context,
                character=character,
            )
        except Exception:
            _LOGGER.exception("feed register profiler failed open")
            return None

    def _feed_gate_context(
        self,
        *,
        composer_input: FeedComposerInput,
        output: FeedComposerOutput,
        operator: OperatorProfile | None,
        register_profile,
        diversity_evidence: ReplyDiversityEvidence,
        recent_self_lines: tuple[str, ...],
    ) -> NoveltyGateContext:
        """Everything the judge sees about one draft post."""
        character = composer_input.character
        return NoveltyGateContext(
            character_id=character.id,
            operator_id=(
                getattr(operator, "id", None)
                or getattr(character, "user_id", DEFAULT_OPERATOR_ID)
            ),
            response_text=output.content_text,
            known_material=tuple(
                line for line in composer_input.context_snippets if line.strip()
            ),
            recent_self_lines=recent_self_lines,
            self_repetition_hint="",
            latest_user_message=composer_input.hint,
            content_tolerance="frontier",
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
            persona_context=(
                f"性格：{', '.join(character.personality)}",
                f"說話風格：{character.speaking_style}",
            ),
            operator_primary_language=composer_input.operator_primary_language,
            tool_prompt_lines=_tool_prompt_lines(output),
            mechanical_evidence_lines=length_overrun_lines(
                output.content_text, MAX_BODY_CHARS,
            ),
        )

    async def create_manual_post(
        self,
        character: Character,
        *,
        content_text: str,
        kind: "FeedKind | str" = "manual",
        image_url: str | None = None,
        image_prompt: str | None = None,
        now: datetime | None = None,
    ) -> FeedPost:
        """Persist a user-authored post for ``character``.

        Bypasses the daily limit + cooldown gates because the user opted
        in explicitly; still flows through the same persist → publish →
        memorialize pipeline so the character "remembers" the post and
        the SSE stream surfaces it just like an automated tick. Image
        generation is intentionally NOT auto-fired — the caller supplies
        a pre-uploaded ``image_url`` if one is wanted.
        """
        # Late import to keep the existing top-level import surface
        # narrow; ``FeedKind`` is only needed when this method is called.
        from kokoro_link.domain.value_objects.feed_kind import FeedKind

        text = (content_text or "").strip()
        if not text:
            raise ValueError("content_text must be non-empty")
        when = now or datetime.now(timezone.utc)
        resolved_kind = (
            kind if isinstance(kind, FeedKind) else FeedKind.from_string(kind)
        )
        post = FeedPost.create(
            character_id=character.id,
            kind=resolved_kind,
            content_text=text,
            source=FeedSource.manual(),
            image_url=image_url,
            image_prompt=image_prompt,
            created_at=when,
        )
        await self._repo.add(post)
        await self._publish(post)
        await self._memorialize(character, post)
        return post

    async def _notify_web_push(
        self,
        character: Character,
        post: FeedPost,
    ) -> None:
        if self._notification_service is None:
            return
        try:
            await self._notification_service.notify_feed_post(character, post)
        except Exception:
            _LOGGER.exception(
                "feed post web push notification failed character=%s post=%s",
                character.id,
                post.id,
            )

    async def _maybe_generate_image(
        self,
        character: Character,
        candidate: FeedCandidate,
        composer_prompt: str,
    ) -> tuple[str | None, str | None]:
        """The post's picture: render → persist → bill, degrading to a
        text-only post at every step that can fail."""
        if not candidate.image_required:
            return None, None
        if self._image_provider is None or self._object_storage is None:
            return None, None
        prompt = (composer_prompt or "").strip()
        if not prompt:
            return None, None
        render = await self._render_feed_image(character, prompt)
        if render.provider is None:
            return None, render.styled_prompt
        if render.error is not None:
            await self._record_image_render_usage(
                character=character, render=render, artifact_count=0,
            )
            if render.typed_failure:
                _LOGGER.warning(
                    "feed image generation failed character=%s — falling back "
                    "to text-only post",
                    character.id, exc_info=render.error,
                )
            else:
                _LOGGER.error(
                    "feed image generation crashed character=%s",
                    character.id, exc_info=render.error,
                )
            return None, render.styled_prompt
        if render.image_bytes is None:
            await self._record_image_render_usage(
                character=character, render=render, artifact_count=0,
            )
            return None, render.styled_prompt
        url = await self._write_image_bytes(character, render.image_bytes)
        await self._record_image_render_usage(
            character=character, render=render, artifact_count=1 if url else 0,
        )
        return url, render.styled_prompt

    async def generate_video_first_frame(
        self,
        character: Character,
        composer_prompt: str,
    ) -> FeedFirstFrame | None:
        """Render the opening frame of a clip and land it in feed storage.

        The async video pipeline's entry point (
        CV4): one render, one durable object, one image usage row, reused
        as the I2V reference, the poster frame, and — when the job times
        out, fails or is refused — the picture of the plain image post we
        fall back to. Landing it *before* the job is submitted is what
        makes that fallback free: the degraded post reuses these bytes
        instead of paying for a second render minutes later.

        ``None`` means no frame exists. There is then nothing to anchor an
        I2V pass on and nothing to post a picture with, so the caller
        drops straight to a text-only post without submitting a job —
        never to the synchronous video path, which this method's presence
        does not change in any way.
        """
        if self._image_provider is None or self._object_storage is None:
            return None
        prompt = (composer_prompt or "").strip()
        if not prompt:
            return None
        render = await self._render_feed_image(character, prompt)
        if render.provider is None:
            return None
        if render.error is not None:
            await self._record_image_render_usage(
                character=character, render=render, artifact_count=0,
            )
            _LOGGER.warning(
                "feed video first frame render failed character=%s — no "
                "video job will be submitted",
                character.id, exc_info=render.error,
            )
            return None
        if render.image_bytes is None:
            await self._record_image_render_usage(
                character=character, render=render, artifact_count=0,
            )
            return None
        stored = await self._store_feed_image(character, render.image_bytes)
        await self._record_image_render_usage(
            character=character,
            render=render,
            artifact_count=1 if stored is not None else 0,
        )
        if stored is None:
            return None
        return FeedFirstFrame(
            url=stored.url,
            object_key=stored.object_key,
            prompt=render.styled_prompt,
            data=render.image_bytes,
        )

    async def _render_feed_image(
        self,
        character: Character,
        prompt: str,
    ) -> _RenderedImage:
        """Render step — ask the active image provider for one picture.

        Writes nothing and bills nothing; see :class:`_RenderedImage` for
        why those belong to the caller. An unwired port collapses into the
        same "no provider" result as an unresolved one, so a caller that
        forgot its own gate degrades instead of crashing a background tick.
        """
        from kokoro_link.application.services.feature_keys import (
            FEATURE_IMAGE_FEED,
        )
        from kokoro_link.contracts.image_provider import ImageGenerationError

        if self._image_provider is None:
            return _RenderedImage(
                styled_prompt=prompt,
                started_at=datetime.now(timezone.utc),
            )
        provider = await self._image_provider.resolve(
            FEATURE_IMAGE_FEED, character=character,
        )
        profile_id = await self._image_provider.resolve_profile_id(
            FEATURE_IMAGE_FEED, character=character,
        )
        if provider is None:
            return _RenderedImage(
                styled_prompt=prompt,
                started_at=datetime.now(timezone.utc),
            )
        styled_prompt = await self._styled_prompt(prompt, character=character)
        started_at = datetime.now(timezone.utc)
        try:
            images = await provider.generate(
                character=character,
                positive=styled_prompt,
                aspect="portrait",
                batch=1,
                use_runtime_state=True,
                # EC6: the background of a managed character's post is drawn
                # from the partner's reference art like every other surface —
                # including when this render is the first frame a video job
                # is later built on.
                user_attachment_urls=official_reference_attachments(
                    character, object_storage=self._object_storage,
                ),
            )
        except ImageGenerationError as exc:
            return _RenderedImage(
                styled_prompt=styled_prompt,
                started_at=started_at,
                provider=provider,
                profile_id=profile_id or "",
                error=exc,
                error_code="ImageGenerationError",
                error_message="feed image generation failed",
                typed_failure=True,
            )
        except Exception as exc:
            return _RenderedImage(
                styled_prompt=styled_prompt,
                started_at=started_at,
                provider=provider,
                profile_id=profile_id or "",
                error=exc,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
        return _RenderedImage(
            styled_prompt=styled_prompt,
            started_at=started_at,
            provider=provider,
            profile_id=profile_id or "",
            image_bytes=images[0] if images else None,
            returned=len(images) if images else 0,
        )

    async def _record_image_render_usage(
        self,
        *,
        character: Character,
        render: _RenderedImage,
        artifact_count: int,
    ) -> None:
        """File the ledger row for one render. ``artifact_count`` is the
        caller's business — it is the only fact the render step cannot
        know, because it depends on whether the persist step landed."""
        await self._record_image_usage_safely(
            character=character,
            provider=render.provider,
            profile_id=render.profile_id,
            returned=render.returned,
            artifact_count=artifact_count,
            status=render.status,
            error_code=render.error_code,
            error_message=render.error_message,
            output_bytes=render.output_bytes,
            started_at=render.started_at,
        )

    # ------------------------------------------------------------------
    # Deferred (asynchronous) video — CV4
    # ------------------------------------------------------------------

    async def _video_volume_allows(
        self, character: Character, when: datetime,
    ) -> bool:
        """Volume control seam for CV5 (``AccountRuntimeProfile.video_daily_limit``).

        A separate decision point rather than a check folded into the submit
        path: the plan's requirement is that an over-quota character spends
        *nothing* — no storyboard call, no first frame, no queued job — so
        the answer has to be known before the pipeline is entered at all
        (this gates both the deferred pipeline in ``_try_deferred_video``
        *and* the legacy synchronous ``_maybe_generate_video`` fallback,
        since both live behind the same ``wants_video`` flag in
        ``_materialise``).

        Same rolling-24h check-then-record shape as
        ``ChatService._reserve_runtime_chat_image_quota``
        (``daily_chat_image_limit``): resolve the profile, bail out cheaply
        when the tier has no limit (``None`` — self-host, and every hosted
        tier until an operator opts in — never touches the ledger), else
        count this operator's video attempts in the last 24h and record one
        more the moment the check passes. Recording *before* the pipeline
        runs (rather than after a successful submit) is deliberate: the
        spend this knob bounds — first-frame render, storyboard call — happens
        regardless of whether the async job later lands, degrades, or is
        rejected by the broker, so counting the decision is the only point
        that cannot be bypassed by a downstream failure.

        Fail-closed on an unreadable ledger or a missing repository, same
        direction as every other hosted pacing wall in this file: an
        account that configured a limit must not silently get an unbounded
        one because a query broke.
        """
        try:
            profile = (
                await self._account_runtime_profile_resolver.resolve_for_operator(
                    character.user_id,
                )
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed video volume profile check failed (operator=%s)",
                character.user_id,
            )
            return False
        limit = profile.video_daily_limit
        if limit is None:
            return True
        usage = self._account_runtime_usage_repository
        if usage is None:
            _LOGGER.error(
                "feed video volume ledger is not configured (operator=%s)",
                character.user_id,
            )
            return False
        try:
            used = await usage.count_events(
                operator_id=character.user_id,
                event_type=ACCOUNT_RUNTIME_EVENT_FEED_VIDEO,
                since=when - timedelta(hours=24),
                until=when,
            )
            if used >= limit:
                return False
            await usage.record_event(
                operator_id=character.user_id,
                event_type=ACCOUNT_RUNTIME_EVENT_FEED_VIDEO,
                occurred_at=when,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed video volume check failed (operator=%s)",
                character.user_id,
            )
            return False
        return True

    async def _try_deferred_video(
        self,
        character: Character,
        candidate: FeedCandidate,
        output: FeedComposerOutput,
        text: str,
        when: datetime,
    ) -> "_VideoBranch":
        """Run the asynchronous pipeline, or decline to.

        ``outcome is None`` means *this deployment has no asynchronous
        pipeline* — the resolved provider renders synchronously (every
        self-host adapter) — and the caller runs the historical branch
        untouched, reusing the ``resolved`` provider carried back with
        that answer. That single return is the self-host red line in this
        file.

        Otherwise the post is not published by this tick: either the job
        was queued (``deferred``) or the pipeline gave up early and handed
        back the frame it had already rendered, which the caller publishes
        as a picture post right now.
        """
        service = self._video_job_service
        if service is None:
            return _VideoBranch(outcome=None)
        resolved = await service.resolve_target(
            character, feature_key=FEATURE_VIDEO_FEED,
        )
        target = resolved.async_target
        if target is None:
            return _VideoBranch(outcome=None, resolved=resolved)
        # The clip's opening frame is a *still*, and only the image prompt
        # describes one. The video prompt is not a stand-in for it: its
        # mandated shape is a three-beat "A → then B → finally C" action,
        # and an image model asked to draw three beats at once returns one
        # picture holding three stacked panels. The storyboard step then
        # reads that composition off the frame and pins it as a
        # consistency anchor ("preserving vertically stacked three-tier
        # composition"), so the clip comes out as a contact sheet that
        # never moves as one scene. The composer fills this field for
        # every video pick — including a second pass when its first answer
        # left it empty — so an empty one here means both attempts failed
        # and there is no still worth rendering.
        frame_prompt = (output.image_prompt or "").strip()
        if not frame_prompt:
            _LOGGER.warning(
                "video post has no first-frame prompt character=%s — "
                "posting text only instead of animating a still the "
                "composer never described",
                character.id,
            )
            return _VideoBranch(outcome=_TickOutcome(), resolved=resolved)
        frame = await self.generate_video_first_frame(character, frame_prompt)
        if frame is None:
            # No frame: nothing to anchor an I2V pass on and nothing to
            # publish as a picture. The render was already billed, so the
            # caller must not try again — text-only post.
            return _VideoBranch(outcome=_TickOutcome(), resolved=resolved)
        result = await service.submit(
            character=character,
            target=target,
            draft=FeedVideoDraft(
                content_text=text,
                post_kind=candidate.kind.value,
                source_kind=candidate.source.kind,
                source_ref_id=candidate.source.ref_id,
                base_video_prompt=(output.video_prompt or "").strip(),
                context_snippets=tuple(candidate.context_snippets or ()),
            ),
            first_frame_url=frame.url,
            first_frame_key=frame.object_key,
            first_frame_bytes=frame.data,
            first_frame_prompt=frame.prompt,
            now=when,
        )
        if result.outcome == SUBMIT_DEFERRED:
            return _VideoBranch(
                outcome=_TickOutcome(deferred=True), resolved=resolved,
            )
        return _VideoBranch(
            outcome=_TickOutcome(first_frame=frame), resolved=resolved,
        )

    # ------------------------------------------------------------------
    # CV6 — internal full-chain video test trigger
    # ------------------------------------------------------------------

    async def trigger_internal_video_test(
        self,
        character: Character,
        *,
        dry_run: bool = True,
        timeout_seconds: int | None = None,
        pipeline_override: str | None = None,
        now: datetime | None = None,
    ) -> VideoTriggerReport:
        """Admin-only full-chain video trigger (D11 / CV6).

        Runs the exact production pipeline — first frame → storyboard →
        submit — bypassing only the two decisions a real tick makes
        *before* entering it: the LLM's ``media_kind`` pick and the CV5
        volume gate (``_video_volume_allows``). There is no candidate and
        no composer call here at all — this is a synthetic post, not a
        real tick, so there is no ``media_kind`` to bypass so much as
        there is none to begin with. Every other gate stays, most
        importantly ``_runtime_video_generation_enabled``: an operator
        who switched video off for their account is not silently
        overridden by an admin diagnostic.

        Never runs the synchronous self-host branch. A deployment with no
        asynchronous target (no provider, or one that only implements the
        synchronous ``VideoProviderPort.generate()``) has nothing this
        endpoint can usefully exercise, and driving a 30-minute
        synchronous render from an HTTP debug call would be its own
        footgun — so it stops with :data:`STAGE_NO_ASYNC_TARGET` instead.

        See :meth:`FeedVideoJobService.submit` for what ``dry_run`` and
        ``timeout_seconds`` actually do — this method is a thin,
        traced assembly of already-public building blocks
        (:meth:`generate_video_first_frame`, ``submit``), not a second
        implementation of the pipeline.
        """
        from uuid import uuid4

        when = now if now is not None else datetime.now(timezone.utc)
        trigger_id = uuid4().hex

        def report(**kwargs: object) -> VideoTriggerReport:
            return VideoTriggerReport(
                character_id=character.id,
                trigger_id=trigger_id,
                dry_run=dry_run,
                **kwargs,  # type: ignore[arg-type]
            )

        service = self._video_job_service
        if service is None:
            return report(available=False, stage=STAGE_NO_PIPELINE)
        if not await self._runtime_video_generation_enabled(character):
            return report(available=False, stage=STAGE_VIDEO_DISABLED)
        target = await service.resolve_async_target(
            character, feature_key=FEATURE_VIDEO_FEED,
        )
        if target is None:
            return report(available=False, stage=STAGE_NO_ASYNC_TARGET)

        image_prompt = (
            character.appearance or character.summary or character.name
        ).strip()
        content_text = (
            f"（內部測試貼文）{character.name} 的影片管線全鏈觸發測試 "
            f"（trigger={trigger_id[:8]}）。"
        )
        frame = await self.generate_video_first_frame(character, image_prompt)
        if frame is None:
            return report(available=True, stage=STAGE_NO_FIRST_FRAME)

        draft = FeedVideoDraft(
            content_text=content_text,
            post_kind=FeedKind.DAILY.value,
            # A raw string, not ``FeedSource.INTERNAL_TEST``: this dataclass
            # is ``slots=True`` and its ``ClassVar`` ints/strs resolve to
            # ``member_descriptor`` when read directly off the class on this
            # Python version (a pre-existing quirk shared by every other
            # ``FeedSource.*`` constant — nothing else in the codebase reads
            # them that way either, only through the ``FeedSource.xxx()``
            # factory methods, which build the string themselves).
            source_kind=SOURCE_INTERNAL_TEST,
            source_ref_id=trigger_id,
            base_video_prompt=_DEFAULT_TEST_VIDEO_PROMPT,
        )
        extra_metadata = (
            {"pipeline_override": pipeline_override}
            if pipeline_override else None
        )
        result = await service.submit(
            character=character,
            target=target,
            draft=draft,
            first_frame_url=frame.url,
            first_frame_key=frame.object_key,
            first_frame_bytes=frame.data,
            first_frame_prompt=frame.prompt,
            now=when,
            timeout_seconds=timeout_seconds,
            dry_run=dry_run,
            capture_trace=True,
            extra_metadata=extra_metadata,
        )
        trace = result.trace
        return report(
            available=True,
            stage=result.outcome,
            first_frame_url=frame.url,
            first_frame_key=frame.object_key,
            first_frame_prompt=frame.prompt,
            first_frame_bytes=len(frame.data),
            storyboard_input=trace.storyboard_input if trace else None,
            storyboard_output=trace.storyboard_output if trace else None,
            storyboard_source=trace.storyboard_source if trace else None,
            storyboard_reason=trace.storyboard_reason if trace else None,
            job_payload=trace.job_payload if trace else None,
            submitted=trace.submitted if trace else False,
            job_id=trace.job_id if trace else None,
            poll_after_seconds=trace.poll_after_seconds if trace else None,
            pending_id=trace.pending_id if trace else None,
            failure_reason=trace.failure_reason if trace else "",
            rejected_code=trace.rejected_code if trace else None,
            error=trace.error if trace else None,
        )

    # --- PendingFeedVideoLanderPort ------------------------------------
    #
    # The three things the poll path cannot do for itself. Public because
    # they are a port, not because anything else may call them.

    async def store_feed_video(
        self, character: Character, blob: bytes,
    ) -> str | None:
        return await self._write_video_bytes(character, blob)

    async def record_feed_video_usage(
        self,
        *,
        character: Character,
        provider: object,
        profile_id: str,
        artifact_count: int,
        output_bytes: int | None,
        status: str,
        started_at: datetime,
        duration_seconds: Decimal | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        await self._record_video_usage_safely(
            character=character,
            provider=provider,
            profile_id=profile_id,
            artifact_count=artifact_count,
            output_bytes=output_bytes,
            status=status,
            started_at=started_at,
            duration_seconds=duration_seconds,
            error_code=error_code,
            error_message=error_message,
        )

    async def land_pending_feed_video_post(
        self,
        *,
        character: Character,
        pending: "PendingFeedVideo",
        video_url: str | None,
        when: datetime,
    ) -> FeedPost | None:
        """Publish a post the composer drafted minutes ago.

        Same tail as :meth:`_materialise` — persist, publish, notify,
        remember — and deliberately nothing else: the LLM, the gates, the
        quota claim and the world-event commit all happened at submit
        time and must not run twice.

        The first frame is the post's ``image_url`` on **both** exits: as
        the clip's poster when the video landed, and as the picture itself
        when it did not. ``created_at`` is the landing instant rather than
        the submit instant so the post appears in the feed at the moment
        it actually becomes visible.
        """
        post = FeedPost.create(
            character_id=pending.character_id,
            kind=pending.post_kind,
            content_text=pending.content_text,
            source=FeedSource(
                kind=pending.source_kind, ref_id=pending.source_ref_id,
            ),
            image_url=pending.first_frame_url or None,
            image_prompt=pending.image_prompt or None,
            video_url=video_url,
            # Only a post that actually carries a clip carries the prompt
            # that produced one; a degraded post is a picture post and
            # saying otherwise would misread as a broken video.
            video_prompt=(pending.video_prompt or None) if video_url else None,
            created_at=when,
        )
        try:
            await self._repo.add(post)
        except Exception:
            # The (character, source) unique index caught a duplicate: a
            # racing carrier already published this draft. Benign.
            _LOGGER.warning(
                "feed video: deferred post persist skipped (likely race) "
                "character=%s source=%s",
                pending.character_id, pending.source_kind,
                exc_info=True,
            )
            return None
        await self._publish(post)
        await self._notify_web_push(character, post)
        await self._memorialize(character, post)
        return post

    async def _maybe_generate_video(
        self,
        character: Character,
        composer_prompt: str,
        *,
        resolved: "ResolvedVideoTarget | None" = None,
    ) -> tuple[str | None, str | None]:
        """Resolve the active video provider and render a Wan2.2 clip.

        Returns ``(url, prompt)`` on success and ``(None, prompt)`` on
        any failure so the caller can decide whether to fall back to an
        image post or drop the visual entirely. The prompt is echoed so
        the post row still stores what was attempted, even when the
        upstream couldn't render — useful for debugging mid-rollout.

        ``resolved`` is the deferred branch's already-made resolution, so
        the tick that fell through to here does not repeat it. Left
        ``None`` (no pipeline wired at all) this resolves for itself,
        exactly as it always did."""
        if self._video_provider is None or self._object_storage is None:
            return None, None
        prompt = (composer_prompt or "").strip()
        if not prompt:
            return None, None
        from kokoro_link.application.services.feature_keys import (
            FEATURE_VIDEO_FEED,
        )
        from kokoro_link.contracts.video_provider import VideoGenerationError

        if resolved is None:
            provider = await self._video_provider.resolve(
                FEATURE_VIDEO_FEED, character=character,
            )
            profile_id = await self._video_provider.resolve_profile_id(
                FEATURE_VIDEO_FEED, character=character,
            )
        else:
            provider = resolved.provider
            profile_id = resolved.profile_id
        if provider is None:
            # No video profile wired for this deployment; let the caller
            # fall back to image generation by signalling "no video".
            return None, prompt
        styled_prompt = await self._styled_prompt(prompt, character=character)
        started_at = datetime.now(timezone.utc)
        try:
            blob = await provider.generate(
                character=character,
                positive=styled_prompt,
                aspect="portrait",
                use_runtime_state=True,
            )
        except VideoGenerationError:
            await self._record_video_usage_safely(
                character=character,
                provider=provider,
                profile_id=profile_id or "",
                artifact_count=0,
                output_bytes=None,
                status=STATUS_FAILED,
                error_code="VideoGenerationError",
                error_message="feed video generation failed",
                started_at=started_at,
            )
            _LOGGER.warning(
                "feed video generation failed character=%s — falling back "
                "to image post",
                character.id, exc_info=True,
            )
            return None, styled_prompt
        except Exception as exc:
            await self._record_video_usage_safely(
                character=character,
                provider=provider,
                profile_id=profile_id or "",
                artifact_count=0,
                output_bytes=None,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            _LOGGER.exception(
                "feed video generation crashed character=%s",
                character.id,
            )
            return None, styled_prompt
        if not blob:
            await self._record_video_usage_safely(
                character=character,
                provider=provider,
                profile_id=profile_id or "",
                artifact_count=0,
                output_bytes=0,
                status=STATUS_SUCCEEDED,
                started_at=started_at,
            )
            return None, styled_prompt
        url = await self._write_video_bytes(character, blob)
        await self._record_video_usage_safely(
            character=character,
            provider=provider,
            profile_id=profile_id or "",
            artifact_count=1 if url else 0,
            output_bytes=len(blob),
            status=STATUS_SUCCEEDED,
            started_at=started_at,
        )
        return url, styled_prompt

    async def _styled_prompt(
        self,
        positive: str,
        *,
        character: Character,
    ) -> str:
        if self._visual_style_service is None:
            return positive
        return await self._visual_style_service.styled_prompt(
            positive, character=character,
        )

    async def _write_video_bytes(
        self, character: Character, blob: bytes,
    ) -> str | None:
        from uuid import uuid4

        filename = f"{uuid4().hex}.mp4"
        if self._object_storage is None:
            return None
        try:
            stored = await self._object_storage.put_bytes(
                object_key=f"feed/{character.id}/{filename}",
                content=blob,
                content_type="video/mp4",
                metadata={"character_id": character.id, "kind": "feed-video"},
            )
            return stored.url
        except Exception:
            _LOGGER.exception(
                "feed video object write failed character=%s",
                character.id,
            )
            return None

    async def _store_feed_image(
        self, character: Character, blob: bytes,
    ) -> StoredObject | None:
        """Persist step — land one picture under ``feed/{character_id}/``.

        Returns the whole :class:`StoredObject` rather than just its URL
        because the video pipeline needs the object key too (the worker
        is handed a key, not a browser-facing URL). ``None`` on any
        storage failure, same fail-soft contract as before.
        """
        from uuid import uuid4

        filename = f"{uuid4().hex}.png"
        if self._object_storage is None:
            return None
        try:
            return await self._object_storage.put_bytes(
                object_key=f"feed/{character.id}/{filename}",
                content=blob,
                content_type="image/png",
                metadata={"character_id": character.id, "kind": "feed-image"},
            )
        except Exception:
            _LOGGER.exception(
                "feed image object write failed character=%s",
                character.id,
            )
            return None

    async def _write_image_bytes(
        self, character: Character, blob: bytes,
    ) -> str | None:
        stored = await self._store_feed_image(character, blob)
        return None if stored is None else stored.url

    async def _record_image_usage_safely(
        self,
        *,
        character: Character,
        provider: object,
        profile_id: str,
        returned: int,
        artifact_count: int,
        status: str,
        started_at: datetime,
        output_bytes: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self._usage_recorder is None:
            return
        completed_at = datetime.now(timezone.utc)
        usage_parts = image_usage_parts_from_provider(
            provider=provider,
            requested=1,
            returned=returned,
            status=status,
            base_metadata={"aspect": "portrait", "batch": 1},
        )
        try:
            await self._usage_recorder.record(UsageEventDraft(
                capability=CAPABILITY_IMAGE,
                character_id=character.id,
                operator_id=getattr(character, "user_id", ""),
                feature_key="feed_image",
                source_surface="feed_composer",
                upstream_request_id=str(
                    getattr(provider, "last_request_id", "") or "",
                ),
                provider_id=usage_parts.provider_id,
                model_id=usage_parts.model_id,
                profile_id=profile_id,
                quantity=usage_parts.quantity,
                cost=usage_parts.cost,
                latency_ms=int((completed_at - started_at).total_seconds() * 1000),
                status=status,
                error_code=error_code,
                error_message=error_message,
                artifact_count=artifact_count,
                output_bytes=output_bytes,
                metadata=usage_parts.metadata,
                completed_at=completed_at,
            ))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("feed image usage recorder dispatch failed")

    async def _record_video_usage_safely(
        self,
        *,
        character: Character,
        provider: object,
        profile_id: str,
        artifact_count: int,
        output_bytes: int | None,
        status: str,
        started_at: datetime,
        duration_seconds: Decimal | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self._usage_recorder is None:
            return
        completed_at = datetime.now(timezone.utc)
        # CV0-3: this used to hard-code 81 frames / 16 fps for every video
        # regardless of what actually rendered. ``last_duration_seconds`` is
        # an additive, provider-optional signal — same shape as
        # ``last_request_id`` read just below — that a concrete adapter
        # (e.g. ``CloudGatewayVideoProvider``) sets after ``generate()``
        # once it knows the real duration. Adapters that don't set it (and
        # every existing test double) fall back to the historical
        # frames/fps assumption rather than record a lie.
        default_length_frames = 81
        default_fps = 16
        duration = Decimal(default_length_frames) / Decimal(default_fps)
        metadata: dict[str, object] = {"aspect": "portrait"}
        # CV4: the asynchronous path knows the real clip length only when the
        # artifact arrives — minutes after ``generate()`` would have set the
        # provider-side signal, and on a different process. An explicit
        # override therefore wins over the provider attribute, which stays
        # the synchronous path's channel and is untouched when this is None.
        reported_seconds = (
            duration_seconds if duration_seconds is not None
            else getattr(provider, "last_duration_seconds", None)
        )
        if reported_seconds is not None:
            try:
                reported_duration = Decimal(str(reported_seconds))
            except (ArithmeticError, ValueError, TypeError):
                reported_duration = None
            if reported_duration is not None and reported_duration > 0:
                duration = reported_duration
                metadata["reported_duration_seconds"] = float(reported_duration)
        if "reported_duration_seconds" not in metadata:
            metadata["length_frames"] = default_length_frames
            metadata["fps"] = default_fps
        billable_seconds = int(duration.to_integral_value(rounding="ROUND_CEILING"))
        try:
            await self._usage_recorder.record(UsageEventDraft(
                capability=CAPABILITY_VIDEO,
                character_id=character.id,
                operator_id=getattr(character, "user_id", ""),
                feature_key="feed_video",
                source_surface="feed_composer",
                upstream_request_id=str(
                    getattr(provider, "last_request_id", "") or "",
                ),
                provider_id=str(getattr(provider, "provider_id", "") or ""),
                profile_id=profile_id,
                quantity=UsageQuantity(
                    usage_unit="second",
                    input_quantity=billable_seconds,
                    output_quantity=billable_seconds if status != STATUS_FAILED else 0,
                    total_quantity=billable_seconds if status != STATUS_FAILED else 0,
                    billable_quantity=billable_seconds if status != STATUS_FAILED else 0,
                ),
                latency_ms=int((completed_at - started_at).total_seconds() * 1000),
                status=status,
                error_code=error_code,
                error_message=error_message,
                artifact_count=artifact_count,
                output_bytes=output_bytes,
                duration_seconds=duration,
                metadata=metadata,
                completed_at=completed_at,
            ))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("feed video usage recorder dispatch failed")

    async def _publish(self, post: FeedPost) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(FeedPostEvent(
                character_id=post.character_id,
                post_id=post.id,
                kind=post.kind.value,
                content_text=post.content_text,
                image_url=post.image_url,
                video_url=post.video_url,
                created_at=post.created_at,
            ))
        except Exception:
            _LOGGER.exception(
                "feed event bus publish failed character=%s post=%s",
                post.character_id, post.id,
            )

    # ------------------------------------------------------------------
    # Self-memorialisation
    # ------------------------------------------------------------------

    async def _memorialize(
        self,
        character: Character,
        post: FeedPost,
    ) -> None:
        """Write a small episodic memory so the character knows it
        published this post.

        Without this, a user bringing up "你今天那篇咖啡的動態" in chat
        finds a character with no recollection of having posted it —
        the feed surface and the chat surface become disconnected
        identities. We persist a single concise memory tagged
        ``feed`` / ``self_post`` / ``<source.kind>`` so the existing
        memory ranker can surface it like any other episodic.

        Fail-soft on every step: a missing repo, embedder outage, or
        persist crash must NOT undo the post (the row is already live
        and the SSE event has already shipped). The next post + the
        chat-side recent-feed prompt rail are the safety nets.
        """
        if self._memory_repo is None:
            return
        try:
            language = await self._resolve_operator_language(character)
            item = _post_to_memory(post, language=language)
        except Exception:
            _LOGGER.exception(
                "feed memorialise: building memory item failed character=%s post=%s",
                character.id, post.id,
            )
            return
        try:
            embedded = await attach_embeddings([item], self._embedder)
        except EmbedderError:
            # Same fail-loud rule as ScheduleMemorializer: don't write
            # an embedding-less memory when the embedder is operational
            # but momentarily unhappy. The chat-side recent-feed rail
            # still gives the LLM context, so we can afford to skip.
            _LOGGER.warning(
                "feed memorialise: embedder unavailable, skipping memory "
                "for character=%s post=%s",
                character.id, post.id,
            )
            return
        except Exception:
            _LOGGER.exception(
                "feed memorialise: embedding crashed character=%s post=%s",
                character.id, post.id,
            )
            return
        try:
            await self._memory_repo.add_many(embedded)
        except Exception:
            _LOGGER.exception(
                "feed memorialise: persist failed character=%s post=%s",
                character.id, post.id,
            )


_FEED_MEMORY_SNIPPET_CHARS = 80
"""Cap how much of the post body lands in the memory content. Long
posts make the ranker noisy and crowd out other memories; the snippet
plus the source tag gives enough signal for recall."""


def _post_to_memory(post: FeedPost, *, language: str = "zh-TW") -> MemoryItem:
    """Render a ``FeedPost`` as a single-line episodic memory.

    Salience is moderate (0.5) — high enough that the post is reachable
    by the ranker for a few days, low enough that a stream of feed
    posts doesn't drown out the high-salience consolidations the
    memory pipeline produces from real conversations. ``language`` is
    the owning operator's ``primary_language`` (plan #14) — this memory
    reaches the player via MemoryBrowserPanel and feeds back into recall
    prompts, so the wrapper sentence must follow it.
    """
    snippet = post.content_text.strip()
    if len(snippet) > _FEED_MEMORY_SNIPPET_CHARS:
        snippet = snippet[:_FEED_MEMORY_SNIPPET_CHARS].rstrip() + "…"
    content = localized_fallback_text(
        "memory.feed_self_post", language, snippet=snippet,
    )
    tags: tuple[str, ...] = ("feed", "self_post", post.source.kind)
    return MemoryItem.create(
        character_id=post.character_id,
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=0.5,
        tags=tags,
        created_at=post.created_at,
        # F3b: left unjudged (""), deliberately — NOT ``disclosed``.
        # Posting is not reading: this memory is the character's own act
        # of publishing, which happened the moment the post went live,
        # while whether the player has actually *seen* it is a separate,
        # unknown fact. Stamping ``disclosed`` here would assert "the
        # player now knows this" the instant the post is created, which
        # is exactly the "told ≠ read" gap the owner's disclosure-ledger
        # ruling (real read flips the ledger, not the act of sending)
        # exists to prevent. There is also no view-gate to route this
        # through: unlike a source memory a post can later be verified
        # against once the player is shown to have read it (KB8/KB11),
        # this feed-post memory carries no link back to the post id it
        # summarises, so there is nothing to flip when a read happens.
        # "" is the honest value — "not judged", not "not disclosed" —
        # and it renders exactly as it does today (no frame). If a
        # future station links this memory to its post's read state,
        # the link belongs here and the flip can replace this "".
        player_knowledge="",
    )


def _local_day_start(now: datetime, local_tz: tzinfo) -> datetime:
    local = to_timezone(now, local_tz)
    start = datetime(
        local.year, local.month, local.day, tzinfo=local_tz,
    )
    return start.astimezone(timezone.utc)


def _operator_language(operator: OperatorProfile | None) -> str:
    if operator is None:
        return "zh-TW"
    lang = (operator.primary_language or "").strip()
    return lang or "zh-TW"


def _tool_prompt_lines(output: FeedComposerOutput) -> tuple[str, ...]:
    """The draft's tool prompts, each labelled with the field it came from.

    Two jobs at once, which is why they are separated from the prose
    instead of concatenated with it: the judge can only see
    ``tool_prompt_defect`` if it is shown the prompts, and it must not read
    their (legitimately English) tag strings as a language mismatch in a
    Chinese post.
    """
    return tuple(
        f"{label}: {value}"
        for label, value in (
            ("image_prompt", (output.image_prompt or "").strip()),
            ("video_prompt", (output.video_prompt or "").strip()),
        )
        if value
    )


def _without_tool_prompts(output: FeedComposerOutput) -> FeedComposerOutput:
    """The same post, minus every visual the gate could not fix.

    ``media_kind="none"`` rather than merely blank prompts: it is the
    composer's own vocabulary for a text-only post, so the render branches
    downstream take the path they already have instead of each having to
    notice an empty prompt on its own.
    """
    return replace(
        output, image_prompt="", video_prompt="", media_kind="none",
    )


def _timezone_for_operator(operator: OperatorProfile | None) -> tzinfo:
    if operator is None:
        return timezone.utc
    try:
        return timezone_for_id(getattr(operator, "timezone_id", None))
    except Exception:  # pragma: no cover - defensive
        return timezone.utc


def _published_media_kind(post: FeedPost) -> str:
    """Classify what readers actually received, not what the LLM requested."""
    if (post.video_url or "").strip():
        return "video"
    if (post.image_url or "").strip():
        return "image"
    return "none"
