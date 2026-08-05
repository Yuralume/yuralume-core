"""The showcase pipeline's application service.

Core's half of the public wall: it reads the character's own data, runs
the two LLM steps (advisory pre-review, translation) and renders the
snapshot document. It stores **nothing** — the owner's decisions, the
translation cache and the published document all live in the Cloud
control plane, which is the only place a human ever approves anything.

That split is deliberate. Approval is an operations decision about a
public marketing surface; the posts, the schedule and the character card
are Core's data. Copying the posts out to decide about them would create
a second, drifting copy of private content in a service that has no
business holding it.

Every entry point is tenant-scoped. A character id alone is not
authority: pointing this at the wrong character would publish a paying
player's private feed on the front page, so the tenant that owns the
character is checked on every call and a mismatch is indistinguishable
from "not found".
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from kokoro_link.application.services.feature_keys import (
    FEATURE_SHOWCASE_REVIEW,
    FEATURE_SHOWCASE_TRANSLATE,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.showcase.filters import (
    FilterOutcome,
    PublicActivity,
    ShowcaseCandidate,
    dedupe_texts,
    mechanical_filter,
    select_public_activities,
)
from kokoro_link.application.services.showcase.review import (
    ReviewSummary,
    ShowcaseReviewer,
    review_candidates,
)
from kokoro_link.application.services.showcase.snapshot import (
    DEFAULT_LOCALES,
    SOURCE_LOCALE,
    CharacterCard,
    SnapshotPost,
    SnapshotResult,
    build_snapshot,
)
from kokoro_link.application.services.showcase.translate import ShowcaseTranslator
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.feed_source import SOURCE_SCHEDULE

_LOGGER = logging.getLogger(__name__)

DEFAULT_POST_LIMIT = 60
"""Posts scanned per collect. Generous — the control plane merges by id,
so a wide window costs one query rather than one review per post."""

_SCHEDULE_STRIP_LIMIT = 12


class ShowcaseError(RuntimeError):
    """Unusable request or deployment (unknown character, wrong tenant, a
    repository this deployment does not wire)."""


class ShowcaseNotFound(ShowcaseError):
    """The character does not exist *or* does not belong to the caller's
    tenant. One error for both on purpose: a distinct "wrong tenant"
    answer is an existence oracle over other people's characters."""


def post_source_hash(text: str) -> str:
    """Fingerprint of the text a decision was made about.

    The control plane stores this instead of the post body, and compares
    it again at publish time: an approval refers to the words that were
    read, and a post edited afterwards must not ship under it.
    """
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ShowcaseCharacterSummary:
    id: str
    name: str
    summary: str
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class CandidateBundle:
    character_id: str
    character_name: str
    persona: str
    outcome: FilterOutcome
    posts_scanned: int


@dataclass(frozen=True, slots=True)
class SnapshotContext:
    card: CharacterCard
    now_entry: PublicActivity | None
    schedule: tuple[PublicActivity, ...]


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    """One text to render into ``locales``.

    ``key`` is opaque to Core — the control plane uses it to put the
    answer back beside the row it asked for.
    """

    key: str
    text: str


@dataclass(frozen=True, slots=True)
class TranslationResult:
    key: str
    translations: Mapping[str, str]
    missing: tuple[str, ...]


class ShowcaseService:
    def __init__(
        self,
        *,
        character_service,  # noqa: ANN001 — CharacterService
        operator_profile_repository,  # noqa: ANN001 — OperatorProfileRepositoryPort
        feed_post_repository=None,  # noqa: ANN001
        schedule_service=None,  # noqa: ANN001
        memory_repository=None,  # noqa: ANN001
        story_arc_repository=None,  # noqa: ANN001
        active_llm_provider=None,  # noqa: ANN001 — ActiveLLMProviderPort
    ) -> None:
        self._characters = character_service
        self._operators = operator_profile_repository
        self._feed_posts = feed_post_repository
        self._schedule = schedule_service
        self._memories = memory_repository
        self._arcs = story_arc_repository
        self._provider = active_llm_provider

    # ----- characters -------------------------------------------------

    async def list_characters(
        self, *, tenant_id: str,
    ) -> tuple[ShowcaseCharacterSummary, ...]:
        operator_ids = await self._operator_ids(tenant_id)
        summaries: list[ShowcaseCharacterSummary] = []
        for operator_id in sorted(operator_ids):
            for character in await self._characters.list_characters(
                user_id=operator_id,
            ):
                summaries.append(
                    ShowcaseCharacterSummary(
                        id=character.id,
                        name=character.name,
                        summary=character.summary,
                        created_at=getattr(character, "created_at", None),
                    ),
                )
        return tuple(summaries)

    # ----- collect ----------------------------------------------------

    async def list_candidates(
        self,
        *,
        tenant_id: str,
        character_id: str,
        limit: int = DEFAULT_POST_LIMIT,
    ) -> CandidateBundle:
        """Feed posts for the character, already through the mechanical
        filter. Comments are never fetched — see
        :mod:`kokoro_link.application.services.showcase.filters`."""
        character = await self._require_character(tenant_id, character_id)
        if self._feed_posts is None:
            raise ShowcaseError(
                "feed_post_repository is not wired on this deployment",
            )
        posts = await self._feed_posts.list_for_character(
            character.id, limit=max(1, limit),
        )
        activities = await self._index_source_activities(character, posts)
        return CandidateBundle(
            character_id=character.id,
            character_name=character.name,
            persona=character.summary,
            outcome=mechanical_filter(posts, activities=activities),
            posts_scanned=len(posts),
        )

    async def review_posts(
        self,
        *,
        tenant_id: str,
        character_id: str,
        post_ids: Sequence[str],
        limit: int = DEFAULT_POST_LIMIT,
    ) -> ReviewSummary:
        """Advisory pre-review for the requested posts.

        Ids that are not (or no longer) publishable candidates are simply
        absent from the result: the caller asked about posts, and a post
        the mechanical filter rejects has no verdict to give.
        """
        bundle = await self.list_candidates(
            tenant_id=tenant_id, character_id=character_id, limit=limit,
        )
        wanted = {post_id for post_id in post_ids if post_id}
        selected = [
            candidate
            for candidate in bundle.outcome.candidates
            if candidate.id in wanted
        ]
        if not selected:
            return ReviewSummary()
        character = await self._require_character(tenant_id, character_id)
        reviewer = ShowcaseReviewer(self._resolver(FEATURE_SHOWCASE_REVIEW))
        return await review_candidates(
            selected, reviewer=reviewer, character=character,
        )

    # ----- publish ----------------------------------------------------

    async def translate_texts(
        self,
        *,
        tenant_id: str,
        character_id: str,
        items: Sequence[TranslationRequest],
        locales: Sequence[str] = DEFAULT_LOCALES,
    ) -> tuple[TranslationResult, ...]:
        """Translate each item into every non-source locale.

        A locale that fails comes back in ``missing`` rather than as a
        silently untranslated string — the publisher's rule is that a post
        missing a locale does not ship, and it can only apply that rule if
        failure is visible.
        """
        character = await self._require_character(tenant_id, character_id)
        targets = tuple(
            locale for locale in _clean_locales(locales) if locale != SOURCE_LOCALE
        )
        translator = self._translator(character)
        results: list[TranslationResult] = []
        for item in items:
            translations: dict[str, str] = {}
            missing: list[str] = []
            for locale in targets:
                value = await translator.translate(item.text, locale)
                if value is None:
                    missing.append(locale)
                else:
                    translations[locale] = value
            results.append(
                TranslationResult(
                    key=item.key,
                    translations=translations,
                    missing=tuple(missing),
                ),
            )
        return tuple(results)

    async def render_snapshot(
        self,
        *,
        tenant_id: str,
        character_id: str,
        posts: Sequence[SnapshotPost],
        base_url: str,
        locales: Sequence[str] = DEFAULT_LOCALES,
        now: datetime | None = None,
    ) -> SnapshotResult:
        """Render the public document from already-approved posts."""
        character = await self._require_character(tenant_id, character_id)
        moment = now or datetime.now(timezone.utc)
        context = await self._snapshot_context(character, moment)
        resolved_locales = _clean_locales(locales)
        translate = await self._ephemeral_translations(
            character=character, context=context, locales=resolved_locales,
        )
        return build_snapshot(
            card=context.card,
            posts=posts,
            now_entry=context.now_entry,
            schedule=context.schedule,
            base_url=base_url,
            locales=resolved_locales,
            translate=translate,
            generated_at=moment,
        )

    # ----- internals --------------------------------------------------

    async def _ephemeral_translations(
        self,
        *,
        character: Character,
        context: SnapshotContext,
        locales: Sequence[str],
    ):  # noqa: ANN202 — returns (text, locale) -> str | None
        """Pre-translate the strings that change every publish.

        ``build_snapshot`` is synchronous by design (it is the schema, and
        the schema should be testable without an event loop), so the async
        calls happen here and the builder gets a lookup over the answers.
        """
        targets = [locale for locale in locales if locale != SOURCE_LOCALE]
        sources = dedupe_texts(
            [
                character.name,
                character.summary,
                *( [context.now_entry.text] if context.now_entry else [] ),
                *[activity.text for activity in context.schedule],
            ],
        )
        if not targets or not sources:
            return None
        translator = self._translator(character)
        memo: dict[tuple[str, str], str] = {}
        for text in sources:
            for locale in targets:
                value = await translator.translate(text, locale)
                if value is not None:
                    memo[(locale, text)] = value
        return lambda text, locale: memo.get((locale, (text or "").strip()))

    async def _snapshot_context(
        self, character: Character, moment: datetime,
    ) -> SnapshotContext:
        local_tz = timezone.utc
        now_entry: PublicActivity | None = None
        strip: tuple[PublicActivity, ...] = ()
        if self._schedule is not None:
            local_tz = await self._schedule.timezone_for_character(character)
            today = await self._schedule.today_for_character(character, moment)
            schedule = await self._schedule.get_schedule(character.id, date_=today)
            now_entry, strip = select_public_activities(
                schedule, now=moment, local_tz=local_tz, limit=_SCHEDULE_STRIP_LIMIT,
            )
        else:
            _LOGGER.warning(
                "schedule_service not wired — showcase snapshot ships no schedule",
            )
        card = CharacterCard(
            name=character.name,
            persona=character.summary,
            portrait_url=character.image_urls[0] if character.image_urls else None,
            days=_days_alive(character.created_at, moment, local_tz),
            memories=await self._count_memories(character.id),
            stories=await self._count_stories(character.id),
        )
        return SnapshotContext(card=card, now_entry=now_entry, schedule=strip)

    async def _index_source_activities(
        self, character: Character, posts,  # noqa: ANN001
    ) -> dict[str, ScheduleActivity]:
        """``{activity_id: ScheduleActivity}`` for every schedule-sourced post.

        A post seeded by a schedule block is written on (or just after)
        that block's civil day, so the days worth loading are the posts'
        own local dates plus the preceding one — a post composed at 00:20
        about the 23:00 block belongs to yesterday's schedule.

        An unreachable schedule service yields an empty index, which makes
        the mechanical filter reject every schedule-sourced post. That is
        the intended direction: without the activity rows there is no way
        to tell a solo café afternoon from an afternoon with the owner.
        """
        wanted = {
            post.source.ref_id
            for post in posts
            if post.source.kind == SOURCE_SCHEDULE and post.source.ref_id
        }
        if not wanted:
            return {}
        if self._schedule is None:
            _LOGGER.warning(
                "schedule_service not wired — %d schedule-sourced post(s) cannot "
                "be checked against their activity and will be excluded",
                len(wanted),
            )
            return {}
        local_tz = await self._schedule.timezone_for_character(character)
        days: set[date] = set()
        for post in posts:
            if post.source.kind != SOURCE_SCHEDULE or not post.source.ref_id:
                continue
            local_date = _as_local_date(post.created_at, local_tz)
            days.add(local_date)
            days.add(local_date - timedelta(days=1))
        index: dict[str, ScheduleActivity] = {}
        for day in sorted(days):
            schedule = await self._schedule.get_schedule(character.id, date_=day)
            if schedule is None:
                continue
            for activity in schedule.activities:
                index[activity.id] = activity
        missing = wanted - index.keys()
        if missing:
            _LOGGER.warning(
                "%d schedule-sourced post(s) reference an activity that is no "
                "longer stored — excluded rather than published unchecked",
                len(missing),
            )
        return index

    async def _count_memories(self, character_id: str) -> int:
        if self._memories is None:
            return 0
        try:
            return await self._memories.count_for_character(character_id)
        except Exception:  # noqa: BLE001 — a stat is never worth failing a publish
            _LOGGER.exception("showcase memory count failed for %s", character_id)
            return 0

    async def _count_stories(self, character_id: str) -> int:
        """Story arcs the character has been through, started or finished.

        ``StoryArcRepositoryPort`` has no count method and the per-character
        arc count is small (tens at most), so listing is honest rather than
        wasteful."""
        if self._arcs is None:
            return 0
        try:
            arcs = await self._arcs.list_for_character(character_id)
        except Exception:  # noqa: BLE001 — same reasoning as memories
            _LOGGER.exception("showcase story arc count failed for %s", character_id)
            return 0
        return len(arcs)

    async def _operator_ids(self, tenant_id: str) -> set[str]:
        cleaned = (tenant_id or "").strip()
        if not cleaned:
            raise ShowcaseError("tenant_id is required")
        profiles = await self._operators.list_by_cloud_tenant_id(cleaned)
        return {profile.id for profile in profiles}

    async def _require_character(
        self, tenant_id: str, character_id: str,
    ) -> Character:
        operator_ids = await self._operator_ids(tenant_id)
        character = await self._characters.get_character_entity(character_id)
        if character is None or character.user_id not in operator_ids:
            raise ShowcaseNotFound(
                f"character {character_id!r} is not available to this tenant",
            )
        return character

    def _resolver(self, feature_key: str) -> ModelResolver:
        if self._provider is None:
            raise ShowcaseError(
                "no LLM provider is wired on this deployment",
            )
        return ModelResolver(provider=self._provider, feature_key=feature_key)

    def _translator(self, character: Character) -> ShowcaseTranslator:
        return ShowcaseTranslator(
            self._resolver(FEATURE_SHOWCASE_TRANSLATE),
            character=character,
            persona=character.summary,
            character_name=character.name,
        )


def build_showcase_service(container) -> ShowcaseService:  # noqa: ANN001
    """Assemble the service from an already-built container.

    Deliberately not a container field: this surface exists only for the
    Cloud control plane's ops calls, and every dependency it needs is
    already wired for the features that own them.
    """
    return ShowcaseService(
        character_service=container.character_service,
        operator_profile_repository=container.operator_profile_repository,
        feed_post_repository=getattr(container, "feed_post_repository", None),
        schedule_service=getattr(container, "schedule_service", None),
        memory_repository=getattr(container, "memory_repository", None),
        story_arc_repository=getattr(container, "story_arc_repository", None),
        active_llm_provider=getattr(container, "active_llm_provider", None),
    )


def _clean_locales(locales: Sequence[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for locale in locales or ():
        value = (locale or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return tuple(cleaned or DEFAULT_LOCALES)


def _days_alive(
    created_at: datetime | None, now: datetime, local_tz,  # noqa: ANN001
) -> int:
    """Inclusive day count in the character's local calendar — the day she
    was created is day 1, which is what "第 N 天" means to a reader. ``0``
    when the row predates the ``created_at`` column being populated, so the
    portal can hide a number it cannot justify."""
    if created_at is None:
        return 0
    start = _as_local_date(created_at, local_tz)
    today = _as_local_date(now, local_tz)
    return max(1, (today - start).days + 1)


def _as_local_date(moment: datetime, local_tz) -> date:  # noqa: ANN001
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(local_tz).date()


def format_exclusions(excluded: Mapping[str, int]) -> str:
    return ", ".join(f"{reason}={count}" for reason, count in excluded.items()) or "none"


__all__ = [
    "DEFAULT_POST_LIMIT",
    "CandidateBundle",
    "ShowcaseCandidate",
    "ShowcaseCharacterSummary",
    "ShowcaseError",
    "ShowcaseNotFound",
    "ShowcaseService",
    "SnapshotContext",
    "TranslationRequest",
    "TranslationResult",
    "build_showcase_service",
    "format_exclusions",
    "post_source_hash",
]
