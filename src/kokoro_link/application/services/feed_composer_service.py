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
from dataclasses import replace
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
from kokoro_link.application.services.image_usage import image_usage_parts_from_provider
from kokoro_link.application.services.location_context import (
    calendar_region_from_operator,
    prompt_location_fact,
    weather_location_from_operator,
)
from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.application.services.visual_generation_style import (
    VisualGenerationStyleService,
)
from kokoro_link.contracts.calendar_context import CalendarContextPort
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_FEED_POST,
    AccountRuntimeUsageRepositoryPort,
)
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
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
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyGatePort,
    NoveltyVerdict,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.register_profile import (
    RegisterProfileContext,
    RegisterProfilePort,
)
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
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
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.timezone import timezone_for_id, to_timezone
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

_HIGH_BUSY_THRESHOLD = 0.85
"""Current-activity floor where automatic feed posting should wait.

The same threshold is used by feed comment replies. At this level the
character is effectively unavailable: sleep, driving, exam, stage,
critical meeting, or similarly no-phone slots. The post candidate stays
unclaimed and can fire on a later tick when the schedule becomes reachable.
"""


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
        account_runtime_profile_resolver: (
            AccountRuntimeProfileResolverPort | None
        ) = None,
        account_runtime_usage_repository: (
            AccountRuntimeUsageRepositoryPort | None
        ) = None,
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
        self._reply_quality_gate = reply_quality_gate
        self._reply_quality_gate_enabled = bool(reply_quality_gate_enabled)
        self._reply_quality_gate_max_retries = max(
            0,
            int(reply_quality_gate_max_retries),
        )
        self._account_runtime_profile_resolver = (
            account_runtime_profile_resolver
            or PermissiveAccountRuntimeProfileResolver()
        )
        self._account_runtime_usage_repository = account_runtime_usage_repository

    def set_usage_recorder(self, recorder: UsageEventRecorderPort | None) -> None:
        self._usage_recorder = recorder

    async def tick(
        self,
        character: Character,
        *,
        now: datetime | None = None,
    ) -> FeedPost | None:
        when = now or datetime.now(timezone.utc)
        if not self._is_feed_enabled(character):
            return None
        local_tz = await self._resolve_operator_timezone(character)
        if not await self._gate_passes(character, when, local_tz):
            return None
        if await self._is_current_activity_high_busy(character, when):
            return None
        if await self._runtime_feed_post_quota_exhausted(character, when):
            return None
        candidates = await self._candidates.collect(
            character, now=when, local_tz=local_tz,
        )
        if not candidates:
            return None
        # Try candidates in priority order so a composer no-op on the
        # top pick doesn't lose the whole tick — second-best still
        # gets a shot.
        for candidate in candidates:
            post = await self._materialise(character, candidate, when)
            if post is not None:
                return post
        return None

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
    ) -> FeedPost | None:
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
            return None
        text = (output.content_text or "").strip()
        if not text:
            return None
        output = await self._gate_feed_output(
            composer_input=composer_input,
            output=output,
            operator=operator,
        )
        text = (output.content_text or "").strip()
        if not text:
            return None
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
                return None
            if committed is None:
                _LOGGER.info(
                    "feed: world-event seed lost race character=%s item=%s",
                    character.id, item_id,
                )
                return None
        # Branch on the LLM's media_kind pick. Video first when chosen
        # (and a provider is wired): success → ship as a video post.
        # Failure or fallback through to image generation so the post
        # still ships with *some* visual rather than an empty card.
        video_url: str | None = None
        video_prompt: str | None = None
        if (
            output.media_kind == "video"
            and output.video_prompt
            and self._video_provider is not None
            and await self._runtime_video_generation_enabled(character)
        ):
            video_url, video_prompt = await self._maybe_generate_video(
                character, output.video_prompt,
            )

        image_url: str | None = None
        image_prompt: str | None = None
        if video_url is None and output.media_kind != "none":
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
            return None
        if not await self._record_runtime_feed_post(character, when):
            _LOGGER.error(
                "feed runtime quota record failed after persist; "
                "deleting unmetered post character=%s post=%s",
                character.id,
                post.id,
            )
            try:
                await self._repo.delete(post.id)
            except Exception:
                _LOGGER.exception(
                    "feed runtime quota rollback delete failed character=%s "
                    "post=%s",
                    character.id,
                    post.id,
                )
            if (
                candidate.claim_token is not None
                and self._event_seed_dispenser is not None
            ):
                item_id, surface = candidate.claim_token
                try:
                    await self._event_seed_dispenser.release(
                        item_id=item_id,
                        surface=surface,
                    )
                except Exception:
                    _LOGGER.exception(
                        "feed: world-event release after quota-record-fail "
                        "crashed character=%s item=%s",
                        character.id,
                        item_id,
                    )
            return None
        await self._publish(post)
        await self._notify_web_push(character, post)
        await self._memorialize(character, post)
        return post

    async def _runtime_feed_post_quota_exhausted(
        self,
        character: Character,
        now: datetime,
    ) -> bool:
        profile = await self._account_runtime_profile_resolver.resolve_for_operator(
            character.user_id,
        )
        limit = profile.daily_feed_post_limit
        if limit is None:
            return False
        if self._account_runtime_usage_repository is None:
            _LOGGER.error(
                "feed runtime quota ledger is not configured (operator=%s)",
                character.user_id,
            )
            return True
        try:
            used = await self._account_runtime_usage_repository.count_events(
                operator_id=character.user_id,
                event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
                since=now - timedelta(hours=24),
                until=now,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed runtime quota check failed (operator=%s)",
                character.user_id,
            )
            return True
        return used >= limit

    async def _record_runtime_feed_post(
        self,
        character: Character,
        now: datetime,
    ) -> bool:
        profile = await self._account_runtime_profile_resolver.resolve_for_operator(
            character.user_id,
        )
        if profile.daily_feed_post_limit is None:
            return True
        if self._account_runtime_usage_repository is None:
            return False
        try:
            await self._account_runtime_usage_repository.record_event(
                operator_id=character.user_id,
                event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
                occurred_at=now,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "feed runtime quota record failed (operator=%s)",
                character.user_id,
            )
            return False
        return True

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
    ) -> FeedComposerOutput:
        if (
            not self._reply_quality_gate_enabled
            or self._reply_quality_gate is None
        ):
            return output
        profile = await self._profile_feed_register(composer_input, operator)
        diversity = ReplyDiversityEvidence(
            assistant_line_count=0,
            phrase_frequency_lines=(
                "feed composer context snippets 已提供；請判斷貼文是否像套版或重複同一角度。",
            ),
        )
        verdict = await self._evaluate_feed_quality_gate(
            composer_input=composer_input,
            output=output,
            operator=operator,
            register_profile=profile,
            diversity_evidence=diversity,
        )
        if (
            verdict is None
            or verdict.passes
            or self._reply_quality_gate_max_retries <= 0
        ):
            return output
        retry_input = replace(
            composer_input,
            hint=(
                f"{composer_input.hint}\n"
                f"上一輪貼文品質問題：{verdict.feedback}"
            ).strip(),
        )
        try:
            retry_output = await self._composer.compose(retry_input)
        except Exception:
            _LOGGER.exception(
                "feed composer quality retry crashed character=%s",
                composer_input.character.id,
            )
            return output
        return retry_output if (retry_output.content_text or "").strip() else output

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

    async def _evaluate_feed_quality_gate(
        self,
        *,
        composer_input: FeedComposerInput,
        output: FeedComposerOutput,
        operator: OperatorProfile | None,
        register_profile,
        diversity_evidence: ReplyDiversityEvidence,
    ) -> NoveltyVerdict | None:
        if self._reply_quality_gate is None:
            return None
        character = composer_input.character
        gate_context = NoveltyGateContext(
            character_id=character.id,
            operator_id=(
                getattr(operator, "id", None)
                or getattr(character, "user_id", DEFAULT_OPERATOR_ID)
            ),
            response_text=output.content_text,
            known_material=tuple(
                line for line in composer_input.context_snippets if line.strip()
            ),
            recent_self_lines=(),
            self_repetition_hint="",
            latest_user_message=composer_input.hint,
            content_tolerance="frontier",
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
            persona_context=(
                f"性格：{', '.join(character.personality)}",
                f"說話風格：{character.speaking_style}",
            ),
        )
        try:
            return await self._reply_quality_gate.evaluate(
                gate_context,
                character=character,
            )
        except Exception as exc:
            _LOGGER.exception("feed reply quality gate failed open")
            return NoveltyVerdict.pass_open(repr(exc))

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
        if not candidate.image_required:
            return None, None
        if self._image_provider is None or self._object_storage is None:
            return None, None
        prompt = (composer_prompt or "").strip()
        if not prompt:
            return None, None
        from kokoro_link.application.services.feature_keys import (
            FEATURE_IMAGE_FEED,
        )
        from kokoro_link.contracts.image_provider import ImageGenerationError

        provider = await self._image_provider.resolve(
            FEATURE_IMAGE_FEED, character=character,
        )
        profile_id = await self._image_provider.resolve_profile_id(
            FEATURE_IMAGE_FEED, character=character,
        )
        if provider is None:
            return None, prompt
        styled_prompt = await self._styled_prompt(prompt, character=character)
        started_at = datetime.now(timezone.utc)
        try:
            images = await provider.generate(
                character=character,
                positive=styled_prompt,
                aspect="portrait",
                batch=1,
                use_runtime_state=True,
            )
        except ImageGenerationError:
            await self._record_image_usage_safely(
                character=character,
                provider=provider,
                profile_id=profile_id or "",
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code="ImageGenerationError",
                error_message="feed image generation failed",
                started_at=started_at,
            )
            _LOGGER.warning(
                "feed image generation failed character=%s — falling back "
                "to text-only post",
                character.id, exc_info=True,
            )
            return None, styled_prompt
        except Exception as exc:
            await self._record_image_usage_safely(
                character=character,
                provider=provider,
                profile_id=profile_id or "",
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            _LOGGER.exception(
                "feed image generation crashed character=%s",
                character.id,
            )
            return None, styled_prompt
        if not images:
            await self._record_image_usage_safely(
                character=character,
                provider=provider,
                profile_id=profile_id or "",
                returned=0,
                artifact_count=0,
                status=STATUS_SUCCEEDED,
                started_at=started_at,
            )
            return None, styled_prompt
        url = await self._write_image_bytes(character, images[0])
        await self._record_image_usage_safely(
            character=character,
            provider=provider,
            profile_id=profile_id or "",
            returned=len(images),
            artifact_count=1 if url else 0,
            status=STATUS_SUCCEEDED,
            output_bytes=len(images[0]) if images else None,
            started_at=started_at,
        )
        return url, styled_prompt

    async def _maybe_generate_video(
        self,
        character: Character,
        composer_prompt: str,
    ) -> tuple[str | None, str | None]:
        """Resolve the active video provider and render a Wan2.2 clip.

        Returns ``(url, prompt)`` on success and ``(None, prompt)`` on
        any failure so the caller can decide whether to fall back to an
        image post or drop the visual entirely. The prompt is echoed so
        the post row still stores what was attempted, even when the
        upstream couldn't render — useful for debugging mid-rollout."""
        if self._video_provider is None or self._object_storage is None:
            return None, None
        prompt = (composer_prompt or "").strip()
        if not prompt:
            return None, None
        from kokoro_link.application.services.feature_keys import (
            FEATURE_VIDEO_FEED,
        )
        from kokoro_link.contracts.video_provider import VideoGenerationError

        provider = await self._video_provider.resolve(
            FEATURE_VIDEO_FEED, character=character,
        )
        profile_id = await self._video_provider.resolve_profile_id(
            FEATURE_VIDEO_FEED, character=character,
        )
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

    async def _write_image_bytes(
        self, character: Character, blob: bytes,
    ) -> str | None:
        from uuid import uuid4

        filename = f"{uuid4().hex}.png"
        if self._object_storage is None:
            return None
        try:
            stored = await self._object_storage.put_bytes(
                object_key=f"feed/{character.id}/{filename}",
                content=blob,
                content_type="image/png",
                metadata={"character_id": character.id, "kind": "feed-image"},
            )
            return stored.url
        except Exception:
            _LOGGER.exception(
                "feed image object write failed character=%s",
                character.id,
            )
            return None

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
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self._usage_recorder is None:
            return
        completed_at = datetime.now(timezone.utc)
        length_frames = 81
        duration = Decimal(length_frames) / Decimal(16)
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
                metadata={
                    "aspect": "portrait",
                    "length_frames": length_frames,
                    "fps": 16,
                },
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
    )


def _operator_language(operator: OperatorProfile | None) -> str:
    if operator is None:
        return "zh-TW"
    lang = (operator.primary_language or "").strip()
    return lang or "zh-TW"


def _timezone_for_operator(operator: OperatorProfile | None) -> tzinfo:
    if operator is None:
        return timezone.utc
    try:
        return timezone_for_id(getattr(operator, "timezone_id", None))
    except Exception:  # pragma: no cover - defensive
        return timezone.utc
