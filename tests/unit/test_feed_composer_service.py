"""BDD for ``FeedComposerService``.

Covers the gating + materialisation glue between the candidate
collector, the LLM composer port, the optional portrait generator, and
the persistence + event-bus side effects.

Image generation isn't exercised here (the generator is left ``None``),
because the photo path lives behind ComfyUI and ``ComfyPortraitGenerator``
already has its own tests. Image fallback semantics live in a single
test that fakes the port; we don't import the real generator class.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kokoro_link.application.services.feed_candidates import (
    FeedCandidate,
    FeedCandidateCollector,
)
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.application.services.feed_event_bus import (
    FeedEventBus,
    FeedPostEvent,
)
from kokoro_link.application.services.cloud_action_billing_service import (
    ActionChargeHandle,
)
from kokoro_link.application.services.quota_overage_service import (
    OVERAGE_DENIED_INSUFFICIENT_CREDITS,
    OverageGrant,
)
from kokoro_link.application.services.visual_generation_style import (
    VisualGenerationStyleService,
)
from kokoro_link.contracts.feed import (
    FeedComposerInput,
    FeedComposerOutput,
    FeedComposerPort,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_FEED_POST,
    ACCOUNT_RUNTIME_EVENT_FEED_VIDEO,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
    DEMO_ACCOUNT_RUNTIME_PROFILE,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder


# ---------- helpers ----------


class _StaticDemoRuntimeProfileResolver:
    async def resolve_for_operator(self, operator_id: str):
        return DEMO_ACCOUNT_RUNTIME_PROFILE


def _make_character(*, feed_daily_limit: int = 3) -> Character:
    return Character.create(
        name="Aiko",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=20, trust=50, energy=80,
        ),
        feed_daily_limit=feed_daily_limit,
    )


class _FakeCollector:
    """Stand-in for ``FeedCandidateCollector`` — returns a fixed list.

    The composer service only calls ``collect``; we mirror just that
    method so the tests don't depend on the real collector's repo wiring.
    """

    def __init__(self, candidates: list[FeedCandidate]) -> None:
        self._candidates = candidates
        self.calls = 0

    async def collect(
        self, character: Character, *, now: datetime, local_tz: tzinfo = timezone.utc,
    ) -> tuple[FeedCandidate, ...]:
        self.calls += 1
        return tuple(self._candidates)


class _SequentialCollector:
    """Returns one scripted candidate batch per collect call."""

    def __init__(self, batches: list[list[FeedCandidate]]) -> None:
        self._batches = list(batches)
        self.calls = 0

    async def collect(
        self, character: Character, *, now: datetime, local_tz: tzinfo = timezone.utc,
    ) -> tuple[FeedCandidate, ...]:
        _ = character, now, local_tz
        self.calls += 1
        if not self._batches:
            return ()
        return tuple(self._batches.pop(0))


class _StaticSchedule:
    """Minimal stand-in for ScheduleService.current_activity_response."""

    def __init__(self, *, busy_score: float | None) -> None:
        self.busy_score = busy_score
        self.calls = 0
        self.received_character: Character | None = None

    async def current_activity_response(
        self,
        character_id: str,
        *,
        now: datetime | None = None,
        character: Character | None = None,
    ):
        _ = character_id, now
        self.calls += 1
        self.received_character = character
        current = (
            None
            if self.busy_score is None
            else SimpleNamespace(busy_score=self.busy_score)
        )
        return SimpleNamespace(current=current)


class _ScriptedComposer(FeedComposerPort):
    """Composer that returns pre-recorded outputs in order.

    Tests pass a list; each ``compose`` call pops the head. Empty
    output ``("", "")`` simulates the LLM declining; an exception
    instance simulates a crash.
    """

    def __init__(self, outputs: list[FeedComposerOutput | Exception]) -> None:
        self._outputs = list(outputs)
        self.inputs: list[FeedComposerInput] = []

    async def compose(self, payload: FeedComposerInput) -> FeedComposerOutput:
        self.inputs.append(payload)
        if not self._outputs:
            return FeedComposerOutput(content_text="", image_prompt="")
        out = self._outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class _StaticActiveVideoProvider:
    def __init__(self, provider) -> None:  # noqa: ANN001
        self.provider = provider

    async def resolve(self, feature_key=None, *, character=None):  # noqa: ANN001
        return self.provider

    async def resolve_profile_id(self, feature_key=None, *, character=None):  # noqa: ANN001
        return "video-stub" if self.provider is not None else None


class _RecordingVideoProvider:
    provider_id = "stub-video"

    def __init__(self, *, reported_duration_seconds: float | None = None) -> None:
        self.positives: list[str] = []
        # Mirrors the additive ``last_duration_seconds`` shape a real adapter
        # (e.g. ``CloudGatewayVideoProvider``) sets after ``generate()`` —
        # only set on the instance when a test opts in, so every existing
        # caller that constructs this fake without the kwarg keeps hitting
        # the ledger's fallback path unchanged.
        self._reported_duration_seconds = reported_duration_seconds

    async def generate(self, **kwargs) -> bytes:  # noqa: ANN003
        self.positives.append(str(kwargs.get("positive") or ""))
        if self._reported_duration_seconds is not None:
            self.last_duration_seconds = self._reported_duration_seconds
        return b"\x00\x00\x00\x18ftypmp42"


def _candidate(
    *,
    kind: FeedKind = FeedKind.MOOD,
    source: FeedSource | None = None,
    score: float = 0.5,
    image_required: bool = False,
) -> FeedCandidate:
    return FeedCandidate(
        kind=kind,
        source=source or FeedSource.silence(),
        hint="hint",
        score=score,
        context_snippets=("ctx",),
        image_required=image_required,
    )


# ---------- gates ----------


@pytest.mark.asyncio
async def test_tick_returns_none_when_feed_disabled() -> None:
    repo = InMemoryFeedPostRepository()
    collector = _FakeCollector([_candidate()])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="hi")])
    service = FeedComposerService(
        repository=repo, candidates=collector, composer=composer,
    )
    character = _make_character(feed_daily_limit=0)
    when = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)

    result = await service.tick(character, now=when)

    assert result is None
    assert collector.calls == 0  # gate short-circuits before collecting


@pytest.mark.asyncio
async def test_cooldown_blocks_when_recent_post_exists() -> None:
    repo = InMemoryFeedPostRepository()
    when = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    # Post from 30 min ago — well under the 90-min default cooldown.
    await repo.add(FeedPost.create(
        character_id="aiko", kind=FeedKind.MOOD,
        content_text="earlier", source=FeedSource.beat("b-prev"),
        created_at=when - timedelta(minutes=30),
    ))
    collector = _FakeCollector([_candidate()])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="x")])
    service = FeedComposerService(
        repository=repo, candidates=collector, composer=composer,
    )
    character = replace(_make_character(), id="aiko")

    result = await service.tick(character, now=when)

    assert result is None
    assert collector.calls == 0
    assert composer.inputs == []


@pytest.mark.asyncio
async def test_daily_limit_blocks_when_today_count_is_at_limit() -> None:
    repo = InMemoryFeedPostRepository()
    base = datetime(2026, 4, 29, 6, 0, tzinfo=timezone.utc)
    # Two posts already today, limit is 2.
    for idx in range(2):
        await repo.add(FeedPost.create(
            character_id="aiko", kind=FeedKind.MOOD,
            content_text=f"p{idx}",
            source=FeedSource.beat(f"b{idx}"),
            created_at=base + timedelta(hours=idx),
        ))
    when = base + timedelta(hours=4)  # past the cooldown
    collector = _FakeCollector([_candidate()])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="x")])
    service = FeedComposerService(
        repository=repo, candidates=collector, composer=composer,
    )
    character = replace(_make_character(feed_daily_limit=2), id="aiko")

    result = await service.tick(character, now=when)

    assert result is None
    assert composer.inputs == []


@pytest.mark.asyncio
async def test_demo_runtime_profile_resets_auto_feed_quota_on_utc_civil_day() -> None:
    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    collector = _SequentialCollector([
        [_candidate(source=FeedSource.beat("b1"))],
        [_candidate(source=FeedSource.beat("b2"))],
    ])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="第一篇。"),
        FeedComposerOutput(content_text="第二天的貼文。"),
    ])
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        cooldown=timedelta(0),
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
        account_runtime_usage_repository=usage,
    )
    character = replace(_make_character(), id="aiko")
    base = datetime(2026, 4, 29, 22, 0, tzinfo=timezone.utc)

    first = await service.tick(character, now=base)
    second = await service.tick(character, now=base + timedelta(hours=1))
    third = await service.tick(character, now=base + timedelta(hours=2, seconds=1))

    assert first is not None
    assert second is None
    assert third is not None
    assert collector.calls == 2
    assert len(composer.inputs) == 2
    assert await usage.count_events(
        operator_id=character.user_id,
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        since=base - timedelta(minutes=1),
        until=base + timedelta(days=2),
    ) == 2


@pytest.mark.asyncio
async def test_runtime_feed_quota_uses_operator_local_civil_day() -> None:
    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    collector = _SequentialCollector([
        [_candidate(source=FeedSource.beat("local-1"))],
        [_candidate(source=FeedSource.beat("local-2"))],
    ])
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=_ScriptedComposer([
            FeedComposerOutput(content_text="睡前。"),
            FeedComposerOutput(content_text="新的一天。"),
        ]),
        cooldown=timedelta(0),
        operator_profile_service=_FakeOperatorProfileService(
            "zh-TW", timezone_id="Asia/Taipei",
        ),
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
        account_runtime_usage_repository=usage,
    )
    character = replace(_make_character(), id="aiko")
    before_midnight = datetime(
        2026, 4, 29, 15, 30, tzinfo=timezone.utc,
    )
    after_midnight = before_midnight + timedelta(hours=1)

    first = await service.tick(character, now=before_midnight)
    second = await service.tick(character, now=after_midnight)

    assert first is not None
    assert second is not None
    assert after_midnight - before_midnight < timedelta(hours=24)


@pytest.mark.asyncio
async def test_account_feed_quota_reserves_one_daily_slot_per_active_character() -> None:
    class _HostedProfileResolver:
        async def resolve_for_operator(self, operator_id: str):
            return AccountRuntimeProfile(
                name="hosted-test",
                max_characters=2,
                daily_feed_post_limit=2,
            )

    posts = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    characters = InMemoryCharacterRepository()
    aiko = replace(_make_character(), id="aiko")
    yumi = replace(_make_character(), id="yumi", name="Yumi")
    await characters.save(aiko)
    await characters.save(yumi)
    collector = _SequentialCollector([
        [_candidate(source=FeedSource.beat("aiko-first"))],
        [_candidate(source=FeedSource.beat("yumi-first"))],
    ])
    service = FeedComposerService(
        repository=posts,
        candidates=collector,
        composer=_ScriptedComposer([
            FeedComposerOutput(content_text="Aiko 今天的第一篇。"),
            FeedComposerOutput(content_text="Yumi 今天的第一篇。"),
        ]),
        cooldown=timedelta(0),
        account_runtime_profile_resolver=_HostedProfileResolver(),
        account_runtime_usage_repository=usage,
        character_repository=characters,
    )
    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    assert await service.needs_daily_floor_retry(aiko, now=base) is True
    assert await service.needs_daily_floor_retry(yumi, now=base) is True

    aiko_first = await service.tick(aiko, now=base)
    aiko_second = await service.tick(aiko, now=base + timedelta(hours=2))
    yumi_first = await service.tick(yumi, now=base + timedelta(hours=2))

    assert aiko_first is not None
    assert aiko_second is None
    assert yumi_first is not None
    assert collector.calls == 2
    events = await usage.list_events(
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
    )
    assert [event.resource_id for event in events] == ["aiko", "yumi"]
    assert await service.needs_daily_floor_retry(aiko, now=base) is False
    assert await service.needs_daily_floor_retry(yumi, now=base) is False


@pytest.mark.asyncio
async def test_legacy_unattributed_quota_event_keeps_midday_reservation_fair() -> None:
    class _HostedProfileResolver:
        async def resolve_for_operator(self, operator_id: str):
            return AccountRuntimeProfile(
                name="hosted-test",
                max_characters=2,
                daily_feed_post_limit=2,
            )

    posts = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    characters = InMemoryCharacterRepository()
    aiko = replace(_make_character(), id="aiko")
    yumi = replace(_make_character(), id="yumi", name="Yumi")
    await characters.save(aiko)
    await characters.save(yumi)
    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    await posts.add(FeedPost.create(
        character_id=aiko.id,
        kind=FeedKind.MOOD,
        content_text="舊版已發出的第一篇。",
        source=FeedSource.beat("legacy-aiko"),
        created_at=base,
    ))
    await usage.record_event(
        operator_id=aiko.user_id,
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        occurred_at=base,
    )
    collector = _FakeCollector([
        _candidate(source=FeedSource.beat("yumi-first")),
    ])
    service = FeedComposerService(
        repository=posts,
        candidates=collector,
        composer=_ScriptedComposer([
            FeedComposerOutput(content_text="Yumi 今天的第一篇。"),
        ]),
        cooldown=timedelta(0),
        account_runtime_profile_resolver=_HostedProfileResolver(),
        account_runtime_usage_repository=usage,
        character_repository=characters,
    )
    now = base + timedelta(hours=2)

    aiko_second = await service.tick(aiko, now=now)
    yumi_first = await service.tick(yumi, now=now)

    assert aiko_second is None
    assert yumi_first is not None
    assert collector.calls == 1


class _StubFeedOverage:
    """Stands in for ``QuotaOverageService`` at the feed seam."""

    def __init__(self, *, granted: bool = True) -> None:
        self._granted = granted
        self.authorised: list[str] = []
        self.settled = 0
        self.settle_gateway_delivered: list[bool] = []
        self.released = 0

    async def authorise(self, item, *, operator_id, now) -> OverageGrant:
        self.authorised.append(item.key)
        if not self._granted:
            return OverageGrant(
                denied_reason=OVERAGE_DENIED_INSUFFICIENT_CREDITS,
            )
        return OverageGrant(
            charge=ActionChargeHandle(
                action_key=item.action_key,
                charge_id="chg-feed",
                interaction_id="int-feed",
            ),
        )

    async def settle(
        self, grant: OverageGrant | None, *, gateway_delivered: bool = True,
    ) -> None:
        if grant is not None and grant.charge is not None:
            self.settled += 1
            self.settle_gateway_delivered.append(gateway_delivered)

    async def release(self, grant: OverageGrant | None) -> None:
        if grant is not None and grant.charge is not None:
            self.released += 1


def _overage_service(
    *,
    overage: _StubFeedOverage,
    collector,
    composer,
    repo: InMemoryFeedPostRepository,
    usage: InMemoryAccountRuntimeUsageRepository,
) -> FeedComposerService:
    return FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        cooldown=timedelta(0),
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
        account_runtime_usage_repository=usage,
        quota_overage=overage,
    )


@pytest.mark.asyncio
async def test_authorised_overage_lets_the_character_keep_posting() -> None:
    """AP4: the player pre-authorised paying past ``daily_feed_post_limit``."""
    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    overage = _StubFeedOverage(granted=True)
    collector = _SequentialCollector([
        [_candidate(source=FeedSource.beat("b1"))],
        [_candidate(source=FeedSource.beat("b2"))],
    ])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="第一篇。"),
        FeedComposerOutput(content_text="加購的第二篇。"),
    ])
    service = _overage_service(
        overage=overage, collector=collector, composer=composer,
        repo=repo, usage=usage,
    )
    character = replace(_make_character(), id="aiko")
    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)

    first = await service.tick(character, now=base)
    second = await service.tick(character, now=base + timedelta(hours=2))

    assert first is not None
    assert second is not None
    # Only the over-limit tick consults the paid path.
    assert overage.authorised == ["feed_post"]
    assert overage.settled == 1
    assert overage.released == 0
    # C2' exception: composing a post is background work the Gateway never
    # waives, so the covered-call cross-check would refund every purchase —
    # while the post the player bought is live in the feed.
    assert overage.settle_gateway_delivered == [False]


@pytest.mark.asyncio
async def test_a_refused_overage_keeps_the_day_quiet_without_erroring() -> None:
    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    overage = _StubFeedOverage(granted=False)
    collector = _SequentialCollector([
        [_candidate(source=FeedSource.beat("b1"))],
        [_candidate(source=FeedSource.beat("b2"))],
    ])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="第一篇。"),
        FeedComposerOutput(content_text="不該出現的第二篇。"),
    ])
    service = _overage_service(
        overage=overage, collector=collector, composer=composer,
        repo=repo, usage=usage,
    )
    character = replace(_make_character(), id="aiko")
    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)

    await service.tick(character, now=base)
    second = await service.tick(character, now=base + timedelta(hours=2))

    assert second is None
    # The refused tick never even collected candidates — the old silent skip.
    assert collector.calls == 1
    assert overage.settled == 0
    assert overage.released == 0


@pytest.mark.asyncio
async def test_a_bought_post_that_never_materialises_is_refunded() -> None:
    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    overage = _StubFeedOverage(granted=True)
    collector = _SequentialCollector([
        [_candidate(source=FeedSource.beat("b1"))],
        [],  # nothing worth posting on the tick the player paid for
    ])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="第一篇。")])
    service = _overage_service(
        overage=overage, collector=collector, composer=composer,
        repo=repo, usage=usage,
    )
    character = replace(_make_character(), id="aiko")
    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)

    await service.tick(character, now=base)
    second = await service.tick(character, now=base + timedelta(hours=2))

    assert second is None
    assert overage.settled == 0
    assert overage.released == 1


@pytest.mark.asyncio
async def test_a_crash_after_a_bought_post_refunds_and_reraises() -> None:
    class _ExplodingCollector:
        calls = 0

        async def collect(self, character, *, now, local_tz):
            self.calls += 1
            if self.calls == 1:
                return [_candidate(source=FeedSource.beat("b1"))]
            raise RuntimeError("candidate collection blew up")

    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    overage = _StubFeedOverage(granted=True)
    service = _overage_service(
        overage=overage,
        collector=_ExplodingCollector(),
        composer=_ScriptedComposer([FeedComposerOutput(content_text="第一篇。")]),
        repo=repo, usage=usage,
    )
    character = replace(_make_character(), id="aiko")
    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)

    await service.tick(character, now=base)
    with pytest.raises(RuntimeError):
        await service.tick(character, now=base + timedelta(hours=2))

    assert overage.released == 1


@pytest.mark.asyncio
async def test_demo_runtime_profile_blocks_auto_feed_when_ledger_missing() -> None:
    repo = InMemoryFeedPostRepository()
    collector = _FakeCollector([_candidate()])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="不該發文")])
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
    )
    character = replace(_make_character(), id="aiko")

    result = await service.tick(
        character,
        now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert result is None
    assert collector.calls == 0
    assert composer.inputs == []
    assert await repo.list_for_character("aiko") == []


@pytest.mark.asyncio
async def test_demo_runtime_profile_fails_closed_when_feed_quota_claim_fails() -> None:
    class _ClaimFailingRuntimeUsageRepository:
        async def count_events(self, **_: Any) -> int:
            return 0

        async def claim_event_slot(self, **_: Any) -> str | None:
            raise RuntimeError("ledger claim failed")

    repo = InMemoryFeedPostRepository()
    collector = _FakeCollector([_candidate(source=FeedSource.beat("b1"))])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="不應留下")])
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        cooldown=timedelta(0),
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
        account_runtime_usage_repository=_ClaimFailingRuntimeUsageRepository(),
    )
    character = replace(_make_character(), id="aiko")

    result = await service.tick(
        character,
        now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert result is None
    assert collector.calls == 0
    assert composer.inputs == []
    assert await repo.list_for_character("aiko") == []


@pytest.mark.asyncio
async def test_manual_feed_post_bypasses_demo_runtime_feed_post_quota() -> None:
    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    now = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    character = replace(_make_character(feed_daily_limit=1), id="aiko")
    await usage.record_event(
        operator_id=character.user_id,
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        occurred_at=now,
    )
    service = FeedComposerService(
        repository=repo,
        candidates=_FakeCollector([]),
        composer=_ScriptedComposer([]),
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
        account_runtime_usage_repository=usage,
    )

    post = await service.create_manual_post(
        character,
        content_text="手動補一篇。",
        now=now + timedelta(hours=1),
    )

    assert post.content_text == "手動補一篇。"
    saved = await repo.list_for_character("aiko", limit=10)
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_tick_skips_when_current_activity_is_high_busy() -> None:
    """Automatic posts should not publish while the character is in a
    high-busy slot such as sleep, driving, an exam, or a critical meeting."""
    repo = InMemoryFeedPostRepository()
    collector = _FakeCollector([_candidate()])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="不該發文")])
    schedule = _StaticSchedule(busy_score=0.95)
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        schedule_service=schedule,
    )
    character = replace(_make_character(), id="aiko")

    result = await service.tick(
        character, now=datetime(2026, 4, 29, 2, 30, tzinfo=timezone.utc),
    )

    assert result is None
    assert schedule.calls == 1
    assert schedule.received_character is character
    assert collector.calls == 0
    assert composer.inputs == []
    assert await repo.list_for_character("aiko") == []


@pytest.mark.asyncio
async def test_tick_allows_when_current_activity_is_reachable() -> None:
    """Low/mid-busy daily life should still allow organic feed posts."""
    repo = InMemoryFeedPostRepository()
    collector = _FakeCollector([_candidate()])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="散步一下。")])
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        schedule_service=_StaticSchedule(busy_score=0.4),
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    assert post.content_text == "散步一下。"
    assert collector.calls == 1
    assert len(composer.inputs) == 1


# ---------- materialisation ----------


@pytest.mark.asyncio
async def test_tick_persists_post_and_publishes_event() -> None:
    repo = InMemoryFeedPostRepository()
    bus = FeedEventBus()
    queue = bus.subscribe()
    candidate = _candidate(
        kind=FeedKind.SCENE_BEAT,
        source=FeedSource.beat("beat-1"),
    )
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="今天好充實。", image_prompt=""),
    ])
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        event_bus=bus,
    )
    character = replace(_make_character(), id="aiko")
    when = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)

    post = await service.tick(character, now=when)

    assert post is not None
    assert post.content_text == "今天好充實。"
    assert post.kind is FeedKind.SCENE_BEAT
    assert post.source == FeedSource.beat("beat-1")
    assert post.image_url is None
    persisted = await repo.get(post.id)
    assert persisted == post
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert isinstance(event, FeedPostEvent)
    assert event.post_id == post.id
    assert event.character_id == "aiko"


@pytest.mark.asyncio
async def test_tick_falls_through_to_next_candidate_when_top_is_empty() -> None:
    """Empty composer output on the top-scoring candidate must not lose
    the tick — the runner-up gets a shot."""
    repo = InMemoryFeedPostRepository()
    primary = _candidate(score=0.9, source=FeedSource.beat("b-top"))
    backup = _candidate(score=0.4, source=FeedSource.memory("m-low"))
    collector = _FakeCollector([primary, backup])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text=""),  # top candidate declines
        FeedComposerOutput(content_text="後備內容"),  # runner-up wins
    ])
    service = FeedComposerService(
        repository=repo, candidates=collector, composer=composer,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    assert post.content_text == "後備內容"
    assert post.source == FeedSource.memory("m-low")
    assert len(composer.inputs) == 2


@pytest.mark.asyncio
async def test_tick_returns_none_when_composer_crashes() -> None:
    repo = InMemoryFeedPostRepository()
    collector = _FakeCollector([_candidate()])
    composer = _ScriptedComposer([RuntimeError("llm exploded")])
    service = FeedComposerService(
        repository=repo, candidates=collector, composer=composer,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is None
    assert await repo.list_for_character("aiko") == []


@pytest.mark.asyncio
async def test_tick_returns_none_when_no_candidates() -> None:
    repo = InMemoryFeedPostRepository()
    collector = _FakeCollector([])
    composer = _ScriptedComposer([])
    service = FeedComposerService(
        repository=repo, candidates=collector, composer=composer,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is None


@pytest.mark.asyncio
async def test_image_fallback_when_portrait_generator_raises(
    tmp_path: Path,
) -> None:
    """ComfyUI down → text-only post still ships; the prompt is kept
    on the row for debug / regenerate."""
    from kokoro_link.infrastructure.tools.comfyui.generator import (
        PortraitGenerationError,
    )

    class _BoomPortrait:
        async def generate(self, **_: Any) -> list[bytes]:
            raise PortraitGenerationError("comfy down")

    repo = InMemoryFeedPostRepository()
    candidate = _candidate(image_required=True)
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(
            content_text="今天的雲很好看。",
            image_prompt="masterpiece, sky",
        ),
    ])
    from tests.unit._image_provider_stub import StaticActiveImageProvider
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        image_provider=StaticActiveImageProvider(_BoomPortrait()),  # type: ignore[arg-type]
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    assert post.image_url is None
    assert post.image_prompt == "masterpiece, sky"


@pytest.mark.asyncio
async def test_image_persists_when_portrait_generator_succeeds(
    tmp_path: Path,
) -> None:
    class _OkPortrait:
        provider_id = "stub-image"

        async def generate(self, **_: Any) -> list[bytes]:
            return [b"\x89PNG fake"]

    repo = InMemoryFeedPostRepository()
    candidate = _candidate(image_required=True)
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(
            content_text="拍下來了。",
            image_prompt="masterpiece, sky",
        ),
    ])
    from tests.unit._image_provider_stub import StaticActiveImageProvider
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        image_provider=StaticActiveImageProvider(_OkPortrait()),  # type: ignore[arg-type]
        uploads_dir=tmp_path,
        object_storage=storage,
        usage_recorder=usage_recorder,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )
    await usage_recorder.flush()

    assert post is not None
    assert post.image_url is not None
    assert post.image_url.startswith("/uploads/feed/aiko/")
    object_key = storage.object_key_from_url(post.image_url)
    assert object_key is not None
    assert await storage.get_bytes(object_key=object_key) == b"\x89PNG fake"
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.capability == "image"
    assert row.feature_key == "feed_image"
    assert row.provider_id == "stub-image"
    assert row.profile_id == "stub"
    assert row.quantity.usage_unit == "image"
    assert row.quantity.input_quantity == 1
    assert row.quantity.output_quantity == 1
    assert row.quantity.billable_quantity == 1
    assert row.artifact_count == 1
    assert row.output_bytes == len(b"\x89PNG fake")


# ---------- CV2 characterization: render / persist / ledger ----------
#
# The feed picture used to be produced by one method that resolved the
# provider, rendered bytes, wrote them to object storage and filed the
# usage row in a single straight line. CV2 splits that line into a render
# step (provider call only) and a persist step (``feed/{character_id}/``
# write) so the async video pipeline can reuse the render half for the
# clip's first frame. These tests pin every observable the old shape
# produced — the returned prompt, whether an object landed, and the exact
# ledger row per outcome — so the split can be proven to change nothing.


class _RecordingImageProvider:
    """Image provider fake that records the positives it was handed.

    Mirrors ``ImageProviderPort.generate``'s keyword-only surface. The
    scripted ``outcome`` is either the byte list to return or an
    exception instance to raise.
    """

    provider_id = "stub-image"

    def __init__(self, outcome: list[bytes] | Exception) -> None:
        self._outcome = outcome
        self.positives: list[str] = []
        self.calls = 0

    async def generate(self, **kwargs: Any) -> list[bytes]:
        self.calls += 1
        self.positives.append(str(kwargs.get("positive") or ""))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return list(self._outcome)


class _WriteFailingObjectStorage(InMemoryObjectStorage):
    """Storage whose writes always fail — the ``_write_image_bytes``
    ``except`` arm, which returns ``None`` while the render already
    happened (and therefore still has to be billed)."""

    async def put_bytes(self, **kwargs: Any) -> Any:
        raise RuntimeError("bucket unreachable")


def _image_service(
    *,
    provider: Any,
    storage: Any,
    usage_recorder: Any = None,
    style_service: VisualGenerationStyleService | None = None,
    composer_output: FeedComposerOutput | None = None,
    image_required: bool = True,
    tmp_path: Path | None = None,
) -> tuple[FeedComposerService, InMemoryFeedPostRepository]:
    from tests.unit._image_provider_stub import StaticActiveImageProvider

    repo = InMemoryFeedPostRepository()
    collector = _FakeCollector([_candidate(image_required=image_required)])
    composer = _ScriptedComposer([
        composer_output
        or FeedComposerOutput(
            content_text="今天的雲很好看。", image_prompt="masterpiece, sky",
        ),
    ])
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        image_provider=StaticActiveImageProvider(provider),
        uploads_dir=tmp_path,
        object_storage=storage,
        usage_recorder=usage_recorder,
        visual_style_service=style_service,
    )
    return service, repo


_TICK_AT = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_image_skipped_when_candidate_does_not_require_one(
    tmp_path: Path,
) -> None:
    """``image_required=False`` short-circuits before the provider is
    even resolved — no bytes, no prompt echoed onto the row, no ledger."""
    provider = _RecordingImageProvider([b"\x89PNG fake"])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service, _ = _image_service(
        provider=provider,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        image_required=False,
        tmp_path=tmp_path,
    )

    post = await service.tick(replace(_make_character(), id="aiko"), now=_TICK_AT)
    await usage_recorder.flush()

    assert post is not None
    assert post.image_url is None
    assert post.image_prompt is None
    assert provider.calls == 0
    assert await usage_events.list_recent() == []


@pytest.mark.asyncio
async def test_image_skipped_when_composer_returned_a_blank_prompt(
    tmp_path: Path,
) -> None:
    provider = _RecordingImageProvider([b"\x89PNG fake"])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service, _ = _image_service(
        provider=provider,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        composer_output=FeedComposerOutput(
            content_text="今天的雲很好看。", image_prompt="   ",
        ),
        tmp_path=tmp_path,
    )

    post = await service.tick(replace(_make_character(), id="aiko"), now=_TICK_AT)
    await usage_recorder.flush()

    assert post is not None
    assert post.image_url is None
    assert post.image_prompt is None
    assert provider.calls == 0
    assert await usage_events.list_recent() == []


@pytest.mark.asyncio
async def test_unresolved_image_provider_echoes_the_unstyled_prompt(
    tmp_path: Path,
) -> None:
    """No provider wired for this deployment: the row keeps the composer's
    raw prompt (styling happens *after* the provider check) and nothing is
    billed."""
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    style_service = VisualGenerationStyleService(
        preferences=InMemoryPreferencesRepository(),
    )
    service, _ = _image_service(
        provider=None,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        style_service=style_service,
        tmp_path=tmp_path,
    )
    character = replace(_make_character(), id="aiko")
    await style_service.set_style("realistic", user_id=character.user_id)

    post = await service.tick(character, now=_TICK_AT)
    await usage_recorder.flush()

    assert post is not None
    assert post.image_url is None
    assert post.image_prompt == "masterpiece, sky"
    assert await usage_events.list_recent() == []


@pytest.mark.asyncio
async def test_image_generation_error_files_a_failed_ledger_row(
    tmp_path: Path,
) -> None:
    from kokoro_link.contracts.image_provider import ImageGenerationError

    provider = _RecordingImageProvider(ImageGenerationError("comfy down"))
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    style_service = VisualGenerationStyleService(
        preferences=InMemoryPreferencesRepository(),
    )
    service, _ = _image_service(
        provider=provider,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        style_service=style_service,
        tmp_path=tmp_path,
    )
    character = replace(_make_character(), id="aiko")
    await style_service.set_style("realistic", user_id=character.user_id)

    post = await service.tick(character, now=_TICK_AT)
    await usage_recorder.flush()

    assert post is not None
    assert post.image_url is None
    # The *styled* prompt is what was attempted, so it is what is kept.
    assert post.image_prompt is not None
    assert post.image_prompt == provider.positives[0]
    assert "masterpiece, sky" in post.image_prompt
    assert post.image_prompt != "masterpiece, sky"
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.capability == "image"
    assert row.feature_key == "feed_image"
    assert row.status == "failed"
    assert row.error_code == "ImageGenerationError"
    assert row.error_message == "feed image generation failed"
    assert row.artifact_count == 0
    assert row.output_bytes is None
    assert row.quantity.input_quantity == 1
    assert row.quantity.output_quantity == 0
    assert row.quantity.billable_quantity == 0


@pytest.mark.asyncio
async def test_unexpected_image_crash_files_the_concrete_error_type(
    tmp_path: Path,
) -> None:
    provider = _RecordingImageProvider(RuntimeError("boom"))
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service, _ = _image_service(
        provider=provider,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        tmp_path=tmp_path,
    )

    post = await service.tick(replace(_make_character(), id="aiko"), now=_TICK_AT)
    await usage_recorder.flush()

    assert post is not None
    assert post.image_url is None
    assert post.image_prompt == "masterpiece, sky"
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "RuntimeError"
    assert rows[0].error_message == "boom"
    assert rows[0].artifact_count == 0


@pytest.mark.asyncio
async def test_image_provider_returning_nothing_files_a_succeeded_empty_row(
    tmp_path: Path,
) -> None:
    """The provider answered, it just had no picture. Not a failure — the
    call still happened upstream and is still billed as a zero-artifact
    success."""
    provider = _RecordingImageProvider([])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service, _ = _image_service(
        provider=provider,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        tmp_path=tmp_path,
    )

    post = await service.tick(replace(_make_character(), id="aiko"), now=_TICK_AT)
    await usage_recorder.flush()

    assert post is not None
    assert post.image_url is None
    assert post.image_prompt == "masterpiece, sky"
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "succeeded"
    assert row.error_code is None
    assert row.artifact_count == 0
    assert row.output_bytes is None
    assert row.quantity.output_quantity == 0


@pytest.mark.asyncio
async def test_image_storage_write_failure_still_bills_the_render(
    tmp_path: Path,
) -> None:
    """Bytes were rendered upstream and the bucket ate them. The post
    degrades to text-only, but the render is billed with the byte count
    it produced and zero artifacts."""
    provider = _RecordingImageProvider([b"\x89PNG fake"])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service, _ = _image_service(
        provider=provider,
        storage=_WriteFailingObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        tmp_path=tmp_path,
    )

    post = await service.tick(replace(_make_character(), id="aiko"), now=_TICK_AT)
    await usage_recorder.flush()

    assert post is not None
    assert post.image_url is None
    assert post.image_prompt == "masterpiece, sky"
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "succeeded"
    assert row.artifact_count == 0
    assert row.output_bytes == len(b"\x89PNG fake")
    assert row.quantity.output_quantity == 1


@pytest.mark.asyncio
async def test_image_prompt_is_styled_before_the_provider_call(
    tmp_path: Path,
) -> None:
    provider = _RecordingImageProvider([b"\x89PNG fake"])
    style_service = VisualGenerationStyleService(
        preferences=InMemoryPreferencesRepository(),
    )
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    service, _ = _image_service(
        provider=provider,
        storage=storage,
        style_service=style_service,
        tmp_path=tmp_path,
    )
    character = replace(_make_character(), id="aiko")
    await style_service.set_style("realistic", user_id=character.user_id)

    post = await service.tick(character, now=_TICK_AT)

    assert post is not None
    assert provider.positives
    assert "masterpiece, sky" in provider.positives[0]
    assert provider.positives[0] != "masterpiece, sky"
    assert post.image_prompt == provider.positives[0]
    assert post.image_url is not None
    key = storage.object_key_from_url(post.image_url)
    assert key is not None
    assert key.startswith("feed/aiko/")
    assert key.endswith(".png")


# ---------- CV2: the video pipeline's first frame ----------
#
# Deliberately unwired (CV4 threads it into ``_materialise``). Called
# directly so the helper's contract — one render, one durable object, one
# ledger row, url+key back for three downstream consumers — is pinned
# before anything depends on it.


@pytest.mark.asyncio
async def test_first_frame_lands_in_feed_storage_and_bills_once(
    tmp_path: Path,
) -> None:
    provider = _RecordingImageProvider([b"\x89PNG frame"])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    style_service = VisualGenerationStyleService(
        preferences=InMemoryPreferencesRepository(),
    )
    service, _ = _image_service(
        provider=provider,
        storage=storage,
        usage_recorder=usage_recorder,
        style_service=style_service,
        tmp_path=tmp_path,
    )
    character = replace(_make_character(), id="aiko")
    await style_service.set_style("realistic", user_id=character.user_id)

    frame = await service.generate_video_first_frame(character, "masterpiece, sky")
    await usage_recorder.flush()

    assert frame is not None
    # 1) the I2V reference URL, 2) the poster frame for the video post,
    # 3) the picture of the degraded image post — one object, three uses.
    assert frame.url.startswith("/uploads/feed/aiko/")
    assert frame.object_key.startswith("feed/aiko/")
    assert frame.object_key.endswith(".png")
    assert storage.object_key_from_url(frame.url) == frame.object_key
    assert await storage.get_bytes(object_key=frame.object_key) == b"\x89PNG frame"
    assert frame.prompt == provider.positives[0]
    assert "masterpiece, sky" in frame.prompt
    assert frame.prompt != "masterpiece, sky"

    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.capability == "image"
    assert row.feature_key == "feed_image"
    assert row.provider_id == "stub-image"
    assert row.status == "succeeded"
    assert row.artifact_count == 1
    assert row.output_bytes == len(b"\x89PNG frame")


@pytest.mark.asyncio
async def test_first_frame_render_failure_returns_none_and_bills_the_attempt(
    tmp_path: Path,
) -> None:
    from kokoro_link.contracts.image_provider import ImageGenerationError

    provider = _RecordingImageProvider(ImageGenerationError("comfy down"))
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service, _ = _image_service(
        provider=provider,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        tmp_path=tmp_path,
    )

    frame = await service.generate_video_first_frame(
        replace(_make_character(), id="aiko"), "masterpiece, sky",
    )
    await usage_recorder.flush()

    assert frame is None
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "ImageGenerationError"
    assert rows[0].artifact_count == 0


@pytest.mark.asyncio
async def test_first_frame_empty_render_returns_none(tmp_path: Path) -> None:
    provider = _RecordingImageProvider([])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service, _ = _image_service(
        provider=provider,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        tmp_path=tmp_path,
    )

    frame = await service.generate_video_first_frame(
        replace(_make_character(), id="aiko"), "masterpiece, sky",
    )
    await usage_recorder.flush()

    assert frame is None
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].artifact_count == 0


@pytest.mark.asyncio
async def test_first_frame_storage_failure_returns_none_but_bills(
    tmp_path: Path,
) -> None:
    """No landed object means no poster and no I2V reference, so there is
    nothing to submit — but the upstream render happened and is billed."""
    provider = _RecordingImageProvider([b"\x89PNG frame"])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service, _ = _image_service(
        provider=provider,
        storage=_WriteFailingObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        tmp_path=tmp_path,
    )

    frame = await service.generate_video_first_frame(
        replace(_make_character(), id="aiko"), "masterpiece, sky",
    )
    await usage_recorder.flush()

    assert frame is None
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].artifact_count == 0
    assert rows[0].output_bytes == len(b"\x89PNG frame")


@pytest.mark.asyncio
async def test_first_frame_returns_none_without_provider_storage_or_prompt(
    tmp_path: Path,
) -> None:
    """Every pre-render gate returns ``None`` without touching the ledger —
    self-host installs with no image stack must not grow a usage row from
    a code path they never enter."""
    provider = _RecordingImageProvider([b"\x89PNG frame"])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    character = replace(_make_character(), id="aiko")

    no_provider, _ = _image_service(
        provider=None,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        tmp_path=tmp_path,
    )
    assert await no_provider.generate_video_first_frame(
        character, "masterpiece, sky",
    ) is None

    no_storage, _ = _image_service(
        provider=provider,
        storage=None,
        usage_recorder=usage_recorder,
        tmp_path=tmp_path,
    )
    assert await no_storage.generate_video_first_frame(
        character, "masterpiece, sky",
    ) is None

    blank_prompt, _ = _image_service(
        provider=provider,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
        tmp_path=tmp_path,
    )
    assert await blank_prompt.generate_video_first_frame(character, "  ") is None

    await usage_recorder.flush()
    assert provider.calls == 0
    assert await usage_events.list_recent() == []


@pytest.mark.asyncio
async def test_video_generation_applies_visual_style_preference(
    tmp_path: Path,
) -> None:
    repo = InMemoryFeedPostRepository()
    candidate = _candidate()
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(
            content_text="想拍下這段路。",
            media_kind="video",
            video_prompt="walking through neon rain",
            image_prompt="fallback still",
        ),
    ])
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    video_provider = _RecordingVideoProvider()
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    style_service = VisualGenerationStyleService(
        preferences=InMemoryPreferencesRepository(),
    )
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        video_provider=_StaticActiveVideoProvider(video_provider),
        uploads_dir=tmp_path,
        object_storage=storage,
        visual_style_service=style_service,
        usage_recorder=usage_recorder,
    )
    character = replace(_make_character(), id="aiko")
    await style_service.set_style("realistic", user_id=character.user_id)

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )
    await usage_recorder.flush()

    assert post is not None
    assert post.video_url is not None
    assert video_provider.positives
    assert "walking through neon rain" in video_provider.positives[0]
    assert "realistic live-action" in video_provider.positives[0]
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.capability == "video"
    assert row.feature_key == "feed_video"
    assert row.provider_id == "stub-video"
    assert row.profile_id == "video-stub"
    assert row.quantity.usage_unit == "second"
    assert row.quantity.billable_quantity >= 5
    assert row.artifact_count == 1
    assert row.output_bytes == len(b"\x00\x00\x00\x18ftypmp42")
    assert row.duration_seconds is not None
    assert row.metadata["length_frames"] == 81


@pytest.mark.asyncio
async def test_video_post_publishes_event_with_video_url(
    tmp_path: Path,
) -> None:
    """CV0-2: the SSE gap — ``_publish`` only ever set ``image_url`` on the
    ``FeedPostEvent`` it broadcast, so a realtime subscriber had no way to
    tell a video post apart from a text/image one until the next poll.
    """
    repo = InMemoryFeedPostRepository()
    bus = FeedEventBus()
    queue = bus.subscribe()
    candidate = _candidate()
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(
            content_text="想拍下這段路。",
            media_kind="video",
            video_prompt="walking through neon rain",
            image_prompt="fallback still",
        ),
    ])
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    video_provider = _RecordingVideoProvider()
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        event_bus=bus,
        video_provider=_StaticActiveVideoProvider(video_provider),
        uploads_dir=tmp_path,
        object_storage=storage,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    assert post.video_url is not None
    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert isinstance(event, FeedPostEvent)
    assert event.video_url == post.video_url


@pytest.mark.asyncio
async def test_video_usage_ledger_uses_provider_reported_duration(
    tmp_path: Path,
) -> None:
    """CV0-3: the usage ledger hard-coded 81 frames / 6 seconds for every
    video regardless of what the provider actually rendered. When the
    active provider reports its real duration via the additive
    ``last_duration_seconds`` attribute (mirroring the existing
    ``last_request_id`` shape), the ledger must record that instead of the
    hard-coded assumption.
    """
    repo = InMemoryFeedPostRepository()
    candidate = _candidate()
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(
            content_text="想拍下這段路。",
            media_kind="video",
            video_prompt="walking through neon rain",
            image_prompt="fallback still",
        ),
    ])
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    video_provider = _RecordingVideoProvider(reported_duration_seconds=12.0)
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        video_provider=_StaticActiveVideoProvider(video_provider),
        uploads_dir=tmp_path,
        object_storage=storage,
        usage_recorder=usage_recorder,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )
    await usage_recorder.flush()

    assert post is not None
    assert post.video_url is not None
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.duration_seconds == Decimal("12.0")
    assert row.quantity.billable_quantity == 12
    assert "length_frames" not in row.metadata


@pytest.mark.asyncio
async def test_video_usage_ledger_falls_back_to_default_when_provider_silent(
    tmp_path: Path,
) -> None:
    """Same fixture, but the provider never sets ``last_duration_seconds``
    (every adapter except the ones wired to report it, and every existing
    test double) — the ledger must fall back to the historical 81
    frames / 16 fps assumption rather than record nothing or crash."""
    repo = InMemoryFeedPostRepository()
    candidate = _candidate()
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(
            content_text="想拍下這段路。",
            media_kind="video",
            video_prompt="walking through neon rain",
            image_prompt="fallback still",
        ),
    ])
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    video_provider = _RecordingVideoProvider()
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        video_provider=_StaticActiveVideoProvider(video_provider),
        uploads_dir=tmp_path,
        object_storage=storage,
        usage_recorder=usage_recorder,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )
    await usage_recorder.flush()

    assert post is not None
    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.quantity.billable_quantity == 6
    assert row.metadata["length_frames"] == 81
    assert row.metadata["fps"] == 16


@pytest.mark.asyncio
async def test_demo_runtime_profile_disables_feed_video_generation(
    tmp_path: Path,
) -> None:
    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    candidate = _candidate(source=FeedSource.beat("video-beat"))
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(
            content_text="想拍影片但今天先文字。",
            media_kind="video",
            video_prompt="walking through neon rain",
            image_prompt="",
        ),
    ])
    video_provider = _RecordingVideoProvider()
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        video_provider=_StaticActiveVideoProvider(video_provider),
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        cooldown=timedelta(0),
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
        account_runtime_usage_repository=usage,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    assert post.video_url is None
    assert video_provider.positives == []
    assert await usage.count_events(
        operator_id=character.user_id,
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_POST,
        since=datetime(2026, 4, 29, 9, 59, tzinfo=timezone.utc),
        until=datetime(2026, 4, 29, 10, 1, tzinfo=timezone.utc),
    ) == 1


# ---------- CV5: video_daily_limit volume control ----------


class _HostedVideoProfileResolver:
    """Resolves to a hosted tier with a configurable ``video_daily_limit``."""

    def __init__(self, *, video_daily_limit: int | None) -> None:
        self._video_daily_limit = video_daily_limit

    async def resolve_for_operator(self, operator_id: str):
        return AccountRuntimeProfile(
            name="hosted-test",
            video_daily_limit=self._video_daily_limit,
        )


def _video_candidate_and_composer():
    candidate = _candidate(source=FeedSource.beat("video-beat"))
    composer = _ScriptedComposer([
        FeedComposerOutput(
            content_text="想拍下這段路。",
            media_kind="video",
            video_prompt="walking through neon rain",
            image_prompt="fallback still",
        ),
    ])
    return candidate, composer


@pytest.mark.asyncio
async def test_video_daily_limit_over_quota_falls_back_to_image_branch(
    tmp_path: Path,
) -> None:
    """CV5: an operator who already used up today's video slots gets the
    ordinary picture path — no first frame, no storyboard/provider call —
    the moment ``_video_volume_allows`` sees the rolling-24h count at or
    above the limit."""
    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    when = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    character = replace(_make_character(), id="aiko")
    # Pre-spend the single daily video slot within the rolling window.
    await usage.record_event(
        operator_id=character.user_id,
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_VIDEO,
        occurred_at=when - timedelta(hours=1),
    )
    candidate, composer = _video_candidate_and_composer()
    collector = _FakeCollector([candidate])
    video_provider = _RecordingVideoProvider()
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        video_provider=_StaticActiveVideoProvider(video_provider),
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        cooldown=timedelta(0),
        account_runtime_profile_resolver=_HostedVideoProfileResolver(
            video_daily_limit=1,
        ),
        account_runtime_usage_repository=usage,
    )

    post = await service.tick(character, now=when)

    assert post is not None
    assert post.video_url is None
    assert video_provider.positives == []
    # The over-quota check itself does not add a second video event.
    assert await usage.count_events(
        operator_id=character.user_id,
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_VIDEO,
        since=when - timedelta(hours=24),
        until=when,
    ) == 1


@pytest.mark.asyncio
async def test_video_daily_limit_none_is_unlimited(tmp_path: Path) -> None:
    """CV5: ``video_daily_limit=None`` (the default, and every tier until an
    operator opts in) never blocks — video generates exactly as it did
    before this ticket. The ledger is never touched either: an unlimited
    tier bails out of ``_video_volume_allows`` before the usage repository
    is read, same as ``daily_chat_image_limit``'s ``None`` fast path."""
    repo = InMemoryFeedPostRepository()
    usage = InMemoryAccountRuntimeUsageRepository()
    when = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    character = replace(_make_character(), id="aiko")
    candidate, composer = _video_candidate_and_composer()
    collector = _FakeCollector([candidate])
    video_provider = _RecordingVideoProvider()
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        video_provider=_StaticActiveVideoProvider(video_provider),
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        cooldown=timedelta(0),
        account_runtime_profile_resolver=_HostedVideoProfileResolver(
            video_daily_limit=None,
        ),
        account_runtime_usage_repository=usage,
    )

    post = await service.tick(character, now=when)

    assert post is not None
    assert post.video_url is not None
    assert video_provider.positives
    assert await usage.count_events(
        operator_id=character.user_id,
        event_type=ACCOUNT_RUNTIME_EVENT_FEED_VIDEO,
        since=when - timedelta(hours=24),
        until=when,
    ) == 0


@pytest.mark.asyncio
async def test_video_daily_limit_self_host_default_leaves_behavior_unchanged(
    tmp_path: Path,
) -> None:
    """CV5: self-host (no cloud profile resolver, no usage ledger wired at
    all) never sees ``video_daily_limit`` — the permissive default profile's
    ``video_daily_limit`` is ``None``, so the volume check returns True
    without ever touching the (absent) ledger, and video generation is
    unaffected by this ticket."""
    repo = InMemoryFeedPostRepository()
    when = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    character = replace(_make_character(), id="aiko")
    candidate, composer = _video_candidate_and_composer()
    collector = _FakeCollector([candidate])
    video_provider = _RecordingVideoProvider()
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        video_provider=_StaticActiveVideoProvider(video_provider),
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        cooldown=timedelta(0),
        # No account_runtime_profile_resolver / account_runtime_usage_repository
        # — exactly the self-host wiring.
    )

    post = await service.tick(character, now=when)

    assert post is not None
    assert post.video_url is not None
    assert video_provider.positives


# ---------- self-memorialisation ----------


@pytest.mark.asyncio
async def test_memorialize_writes_episodic_memory_on_publish() -> None:
    """A successful post must leave behind a small episodic memory.

    Without this the chat-side LLM has no record the character ever
    posted, so when the user opens chat with "你那篇咖啡的動態怎麼了"
    the character looks blank. The memory is the durable rail (the
    prompt-side recent_feed_posts block is the immediate one).
    """
    from kokoro_link.domain.value_objects.memory_kind import MemoryKind
    from kokoro_link.infrastructure.embedder.null import NullEmbedder
    from kokoro_link.infrastructure.memory.in_memory import (
        InMemoryMemoryRepository,
    )

    feed_repo = InMemoryFeedPostRepository()
    memory_repo = InMemoryMemoryRepository()
    candidate = _candidate(
        kind=FeedKind.SCENE_BEAT, source=FeedSource.beat("beat-coffee"),
    )
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="今天的咖啡好香。", image_prompt=""),
    ])
    service = FeedComposerService(
        repository=feed_repo,
        candidates=collector,
        composer=composer,
        memory_repository=memory_repo,
        embedder=NullEmbedder(),
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    saved = await memory_repo.query("aiko")
    assert len(saved) == 1
    memory = saved[0]
    assert memory.kind is MemoryKind.EPISODIC
    assert "今天的咖啡好香" in memory.content
    assert "動態牆" in memory.content
    assert "feed" in memory.tags
    assert "self_post" in memory.tags
    assert "beat" in memory.tags  # source.kind tag


@pytest.mark.asyncio
async def test_memorialize_skipped_when_repo_not_wired() -> None:
    """No memory repo → composer still publishes, just no memory row.
    Mirrors the fail-soft contract — the post must NOT be undone."""
    feed_repo = InMemoryFeedPostRepository()
    candidate = _candidate()
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="hello", image_prompt=""),
    ])
    service = FeedComposerService(
        repository=feed_repo, candidates=collector, composer=composer,
        memory_repository=None,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    persisted = await feed_repo.list_for_character("aiko")
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_memorialize_does_not_undo_post_when_persist_fails() -> None:
    """If memory persistence crashes, the published post stays — the
    SSE event has already shipped and the row is live."""
    from kokoro_link.contracts.memory import MemoryRepositoryPort
    from kokoro_link.infrastructure.embedder.null import NullEmbedder

    class _BoomMemoryRepo:
        async def add_many(self, items: Any) -> None:
            raise RuntimeError("db down")

        def __getattr__(self, _name: str) -> Any:
            async def _noop(*_a: Any, **_kw: Any) -> Any:
                return None
            return _noop

    feed_repo = InMemoryFeedPostRepository()
    candidate = _candidate()
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="ok", image_prompt=""),
    ])
    service = FeedComposerService(
        repository=feed_repo,
        candidates=collector,
        composer=composer,
        memory_repository=_BoomMemoryRepo(),  # type: ignore[arg-type]
        embedder=NullEmbedder(),
    )
    _ = MemoryRepositoryPort  # keep import live for the type spelling
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    persisted = await feed_repo.list_for_character("aiko")
    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_memorialize_snippet_is_capped() -> None:
    """Long post bodies must be trimmed in the memory snippet so a
    chatty post doesn't crowd out higher-salience memories."""
    from kokoro_link.infrastructure.embedder.null import NullEmbedder
    from kokoro_link.infrastructure.memory.in_memory import (
        InMemoryMemoryRepository,
    )

    feed_repo = InMemoryFeedPostRepository()
    memory_repo = InMemoryMemoryRepository()
    long_text = "今天" + "好開心" * 200
    candidate = _candidate()
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text=long_text, image_prompt=""),
    ])
    service = FeedComposerService(
        repository=feed_repo,
        candidates=collector,
        composer=composer,
        memory_repository=memory_repo,
        embedder=NullEmbedder(),
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    saved = await memory_repo.query("aiko")
    assert len(saved) == 1
    assert saved[0].content.endswith("…」")
    assert long_text not in saved[0].content


def _has_han(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _has_kana(text: str) -> bool:
    return any("぀" <= ch <= "ヿ" for ch in text)


class _FakeOperatorProfile:
    def __init__(
        self, primary_language: str, *, timezone_id: str = "UTC",
    ) -> None:
        self.primary_language = primary_language
        self.timezone_id = timezone_id
        self.location_label = ""
        self.latitude = None
        self.longitude = None
        self.country_code = ""


class _FakeOperatorProfileService:
    def __init__(
        self, primary_language: str, *, timezone_id: str = "UTC",
    ) -> None:
        self._primary_language = primary_language
        self._timezone_id = timezone_id

    async def get_for_user(self, user_id: str):  # noqa: ARG002
        return _FakeOperatorProfile(
            self._primary_language, timezone_id=self._timezone_id,
        )


@pytest.mark.asyncio
async def test_memorialize_self_post_localizes_to_english() -> None:
    """``_post_to_memory`` hardcoded a zh-TW sentence template
    (「我在動態牆發了一篇貼文」) regardless of the owning operator's
    ``primary_language``."""
    from kokoro_link.infrastructure.embedder.null import NullEmbedder
    from kokoro_link.infrastructure.memory.in_memory import (
        InMemoryMemoryRepository,
    )

    feed_repo = InMemoryFeedPostRepository()
    memory_repo = InMemoryMemoryRepository()
    candidate = _candidate(
        kind=FeedKind.SCENE_BEAT, source=FeedSource.beat("beat-coffee"),
    )
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="Coffee smells great today.", image_prompt=""),
    ])
    service = FeedComposerService(
        repository=feed_repo,
        candidates=collector,
        composer=composer,
        memory_repository=memory_repo,
        embedder=NullEmbedder(),
        operator_profile_service=_FakeOperatorProfileService("en-US"),
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    saved = await memory_repo.query("aiko")
    assert len(saved) == 1
    assert not _has_han(saved[0].content), saved[0].content
    assert "Coffee smells great today" in saved[0].content


@pytest.mark.asyncio
async def test_memorialize_self_post_localizes_to_japanese() -> None:
    from kokoro_link.infrastructure.embedder.null import NullEmbedder
    from kokoro_link.infrastructure.memory.in_memory import (
        InMemoryMemoryRepository,
    )

    feed_repo = InMemoryFeedPostRepository()
    memory_repo = InMemoryMemoryRepository()
    candidate = _candidate(
        kind=FeedKind.SCENE_BEAT, source=FeedSource.beat("beat-coffee"),
    )
    collector = _FakeCollector([candidate])
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="今天的咖啡好香。", image_prompt=""),
    ])
    service = FeedComposerService(
        repository=feed_repo,
        candidates=collector,
        composer=composer,
        memory_repository=memory_repo,
        embedder=NullEmbedder(),
        operator_profile_service=_FakeOperatorProfileService("ja-JP"),
    )
    character = replace(_make_character(), id="aiko")

    post = await service.tick(
        character, now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert post is not None
    saved = await memory_repo.query("aiko")
    assert len(saved) == 1
    assert _has_kana(saved[0].content), saved[0].content


# Ensure the unused FeedCandidateCollector import doesn't get pruned —
# importing it here gives quick coverage that the public surface is
# stable enough for our fake to substitute.
def test_collector_class_imports_cleanly() -> None:
    assert FeedCandidateCollector.__name__ == "FeedCandidateCollector"


# ---------- manual post path (Phase A4) ----------


@pytest.mark.asyncio
async def test_create_manual_post_persists_with_manual_source() -> None:
    repo = InMemoryFeedPostRepository()
    composer = _ScriptedComposer([])  # never called for manual path
    collector = _FakeCollector([])
    service = FeedComposerService(
        repository=repo, candidates=collector, composer=composer,
    )
    character = replace(_make_character(), id="aiko")

    post = await service.create_manual_post(
        character,
        content_text="今天我去了陽明山",
    )

    assert post.character_id == "aiko"
    assert post.content_text == "今天我去了陽明山"
    assert post.source.kind == "manual"
    assert post.source.ref_id is None
    saved = await repo.list_for_character("aiko", limit=10)
    assert len(saved) == 1
    assert saved[0].id == post.id
    # Composer / collector must NOT be invoked on the manual path.
    assert collector.calls == 0
    assert composer.inputs == []


@pytest.mark.asyncio
async def test_create_manual_post_bypasses_cooldown_and_daily_limit() -> None:
    repo = InMemoryFeedPostRepository()
    when = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
    # Pre-existing post within cooldown window AND character has 1/day
    # limit already met. Tick gates would block; manual must not.
    await repo.add(FeedPost.create(
        character_id="aiko", kind=FeedKind.MOOD,
        content_text="auto", source=FeedSource.silence(),
        created_at=when - timedelta(minutes=10),
    ))
    composer = _ScriptedComposer([])
    collector = _FakeCollector([])
    service = FeedComposerService(
        repository=repo, candidates=collector, composer=composer,
    )
    character = replace(_make_character(feed_daily_limit=1), id="aiko")

    post = await service.create_manual_post(
        character, content_text="想到再寫一篇", now=when,
    )

    assert post is not None
    assert (await repo.list_for_character("aiko", limit=10))[0].id == post.id


@pytest.mark.asyncio
async def test_create_manual_post_rejects_blank_text() -> None:
    repo = InMemoryFeedPostRepository()
    service = FeedComposerService(
        repository=repo,
        candidates=_FakeCollector([]),
        composer=_ScriptedComposer([]),
    )
    character = replace(_make_character(), id="aiko")
    with pytest.raises(ValueError):
        await service.create_manual_post(character, content_text="   ")


@pytest.mark.asyncio
async def test_create_manual_post_publishes_event() -> None:
    seen: list[FeedPostEvent] = []

    class _CapturingBus:
        async def publish(self, event: FeedPostEvent) -> None:
            seen.append(event)

    repo = InMemoryFeedPostRepository()
    service = FeedComposerService(
        repository=repo,
        candidates=_FakeCollector([]),
        composer=_ScriptedComposer([]),
        event_bus=_CapturingBus(),  # type: ignore[arg-type]
    )
    character = replace(_make_character(), id="aiko")
    post = await service.create_manual_post(
        character, content_text="嗨，大家",
    )
    assert len(seen) == 1
    assert seen[0].post_id == post.id
    assert seen[0].character_id == "aiko"


@pytest.mark.asyncio
async def test_composer_receives_recent_media_kinds_newest_first() -> None:
    repo = InMemoryFeedPostRepository()
    when = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    prior_posts = (
        FeedPost.create(
            character_id="aiko", kind=FeedKind.MOOD, content_text="純文字",
            source=FeedSource.state_shift("prior-none"),
            created_at=when - timedelta(minutes=30),
        ),
        FeedPost.create(
            character_id="aiko", kind=FeedKind.MOOD, content_text="影片",
            source=FeedSource.state_shift("prior-video"),
            image_url="https://example.test/poster.webp",
            video_url="https://example.test/clip.mp4",
            created_at=when - timedelta(minutes=60),
        ),
        FeedPost.create(
            character_id="aiko", kind=FeedKind.MOOD, content_text="圖片",
            source=FeedSource.state_shift("prior-image"),
            image_url="https://example.test/image.webp",
            created_at=when - timedelta(minutes=90),
        ),
    )
    for post in prior_posts:
        await repo.add(post)
    composer = _ScriptedComposer([
        FeedComposerOutput(content_text="新的貼文", media_kind="none"),
    ])
    service = FeedComposerService(
        repository=repo,
        candidates=_FakeCollector([_candidate()]),
        composer=composer,
        video_provider=_StaticActiveVideoProvider(None),
        cooldown=timedelta(0),
    )
    character = replace(_make_character(feed_daily_limit=10), id="aiko")

    await service.tick(character, now=when)

    assert composer.inputs[0].recent_media_kinds == ("none", "video", "image")
