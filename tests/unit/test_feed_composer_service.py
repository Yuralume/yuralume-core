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
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEMO_ACCOUNT_RUNTIME_PROFILE,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
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

    def __init__(self) -> None:
        self.positives: list[str] = []

    async def generate(self, **kwargs) -> bytes:  # noqa: ANN003
        self.positives.append(str(kwargs.get("positive") or ""))
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
async def test_demo_runtime_profile_blocks_second_auto_feed_post_within_24h() -> None:
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
    base = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)

    first = await service.tick(character, now=base)
    second = await service.tick(character, now=base + timedelta(hours=2))
    third = await service.tick(character, now=base + timedelta(days=1, seconds=1))

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
async def test_demo_runtime_profile_rolls_back_post_when_feed_ledger_record_fails() -> None:
    class _RecordFailingRuntimeUsageRepository:
        async def count_events(self, **_: Any) -> int:
            return 0

        async def record_event(self, **_: Any) -> None:
            raise RuntimeError("ledger write failed")

    repo = InMemoryFeedPostRepository()
    collector = _FakeCollector([_candidate(source=FeedSource.beat("b1"))])
    composer = _ScriptedComposer([FeedComposerOutput(content_text="不應留下")])
    service = FeedComposerService(
        repository=repo,
        candidates=collector,
        composer=composer,
        cooldown=timedelta(0),
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
        account_runtime_usage_repository=_RecordFailingRuntimeUsageRepository(),
    )
    character = replace(_make_character(), id="aiko")

    result = await service.tick(
        character,
        now=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
    )

    assert result is None
    assert collector.calls == 1
    assert len(composer.inputs) == 1
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
    def __init__(self, primary_language: str) -> None:
        self.primary_language = primary_language
        self.timezone_id = "UTC"
        self.location_label = ""
        self.latitude = None
        self.longitude = None
        self.country_code = ""


class _FakeOperatorProfileService:
    def __init__(self, primary_language: str) -> None:
        self._primary_language = primary_language

    async def get_for_user(self, user_id: str):  # noqa: ARG002
        return _FakeOperatorProfile(self._primary_language)


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
