from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.nsfw_mode import (
    CONTENT_MODE_NORMAL,
    CONTENT_MODE_NSFW,
    NsfwModeService,
    NsfwModeTargetError,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


@pytest.mark.asyncio
async def test_enable_requires_admin_configured_target() -> None:
    service = NsfwModeService(
        preferences=InMemoryPreferencesRepository(),
        clock=_Clock(datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )

    with pytest.raises(NsfwModeTargetError):
        await service.enable(user_id="alice")


@pytest.mark.asyncio
async def test_global_target_requires_complete_values() -> None:
    service = NsfwModeService(
        preferences=InMemoryPreferencesRepository(),
        clock=_Clock(datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )

    with pytest.raises(NsfwModeTargetError):
        await service.set_global_target(
            llm_provider_id="",
            llm_model_id="community-model",
            image_profile_id="community-image",
        )


@pytest.mark.asyncio
async def test_enable_returns_active_target_and_marks_content_mode() -> None:
    service = NsfwModeService(
        preferences=InMemoryPreferencesRepository(),
        ttl_seconds=600,
        clock=_Clock(datetime(2026, 1, 1, 12, tzinfo=timezone.utc)),
    )
    await service.set_global_target(
        llm_provider_id="lmstudio",
        llm_model_id="local-nsfw",
        image_profile_id="anime_nsfw",
    )

    status = await service.enable(user_id="alice")

    assert status.active is True
    assert status.configured is True
    assert status.target is not None
    assert status.target.llm_provider_id == "lmstudio"
    assert status.target.llm_model_id == "local-nsfw"
    assert status.target.image_profile_id == "anime_nsfw"
    assert await service.content_mode_for_write(user_id="alice") == CONTENT_MODE_NSFW
    assert await service.content_mode_for_write(user_id="bob") == CONTENT_MODE_NORMAL


@pytest.mark.asyncio
async def test_status_expires_lazily_after_idle_ttl() -> None:
    clock = _Clock(datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    service = NsfwModeService(
        preferences=InMemoryPreferencesRepository(),
        ttl_seconds=60,
        clock=clock,
    )
    await service.set_global_target(
        llm_provider_id="lmstudio",
        llm_model_id="local-nsfw",
        image_profile_id="anime_nsfw",
    )
    await service.enable(user_id="alice")

    clock.advance(timedelta(seconds=61))

    status = await service.get_status(user_id="alice")
    assert status.active is False
    assert status.configured is True
    assert status.target is None
    assert status.configured_target is not None
    assert status.configured_target.llm_provider_id == "lmstudio"
    assert await service.active_target(user_id="alice") is None
    assert await service.configured_target(user_id="alice") is not None


@pytest.mark.asyncio
async def test_disable_preserves_configured_target_for_rule_b_routing() -> None:
    service = NsfwModeService(
        preferences=InMemoryPreferencesRepository(),
        ttl_seconds=600,
        clock=_Clock(datetime(2026, 1, 1, 12, tzinfo=timezone.utc)),
    )
    await service.set_global_target(
        llm_provider_id="lmstudio",
        llm_model_id="local-nsfw",
        image_profile_id="anime_nsfw",
    )
    await service.enable(user_id="alice")

    status = await service.disable(user_id="alice")

    assert status.active is False
    assert status.configured is True
    assert status.target is None
    assert status.configured_target is not None
    assert status.configured_target.llm_model_id == "local-nsfw"
    assert await service.content_mode_for_write(user_id="alice") == CONTENT_MODE_NORMAL


@pytest.mark.asyncio
async def test_refresh_activity_extends_expiration_while_active() -> None:
    clock = _Clock(datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    service = NsfwModeService(
        preferences=InMemoryPreferencesRepository(),
        ttl_seconds=60,
        clock=clock,
    )
    await service.set_global_target(
        llm_provider_id="lmstudio",
        llm_model_id="local-nsfw",
        image_profile_id="anime_nsfw",
    )
    await service.enable(user_id="alice")
    clock.advance(timedelta(seconds=30))

    refreshed = await service.refresh_activity(user_id="alice")

    assert refreshed.active is True
    assert refreshed.last_activity_at == clock.value
    assert refreshed.expires_at == clock.value + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_usage_metrics_count_enable_manual_disable_and_average_duration() -> None:
    clock = _Clock(datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    service = NsfwModeService(
        preferences=InMemoryPreferencesRepository(),
        ttl_seconds=600,
        clock=clock,
    )
    await service.set_global_target(
        llm_provider_id="lmstudio",
        llm_model_id="local-nsfw",
        image_profile_id="anime_nsfw",
    )
    await service.enable(user_id="alice")
    clock.advance(timedelta(seconds=120))
    await service.disable(user_id="alice")

    metrics = await service.usage_metrics(user_id="alice")

    assert metrics.active is False
    assert metrics.configured is True
    assert metrics.enable_count == 1
    assert metrics.manual_disable_count == 1
    assert metrics.idle_expired_count == 0
    assert metrics.average_active_seconds == 120
    assert metrics.last_enabled_at == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert metrics.last_disabled_at == datetime(
        2026, 1, 1, 12, 2, tzinfo=timezone.utc,
    )


@pytest.mark.asyncio
async def test_usage_metrics_record_idle_expiration_once() -> None:
    clock = _Clock(datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    service = NsfwModeService(
        preferences=InMemoryPreferencesRepository(),
        ttl_seconds=60,
        clock=clock,
    )
    await service.set_global_target(
        llm_provider_id="lmstudio",
        llm_model_id="local-nsfw",
        image_profile_id="anime_nsfw",
    )
    await service.enable(user_id="alice")
    clock.advance(timedelta(seconds=61))

    first = await service.usage_metrics(user_id="alice")
    second = await service.usage_metrics(user_id="alice")

    assert first.active is False
    assert first.idle_expired_count == 1
    assert first.average_active_seconds == 61
    assert second.idle_expired_count == 1
    assert second.last_expired_at == first.last_expired_at
