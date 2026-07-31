"""Hosted vs self-host proactive-delivery wiring branch (LH4, §8.3).

The Channel env keys land on ``CloudSettings``; the scheduler forwards and drives
the pre-send retry worker once per tick when wired, and stays byte-identical
(no worker, no call) on the self-host default.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.bootstrap.settings import _load_cloud_settings


def test_channel_env_keys_land_on_cloud_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KOKORO_CHANNEL_BASE_URL", "https://channel.example/")
    monkeypatch.setenv(
        "KOKORO_CHANNEL_SERVICE_CREDENTIAL",
        "kid|core|yuralume-channel|delivery:eligibility-read,delivery:create|secret",
    )
    settings = _load_cloud_settings(role="all")
    assert settings.channel_base_url == "https://channel.example"
    assert (
        settings.channel_service_credential
        == "kid|core|yuralume-channel|delivery:eligibility-read,delivery:create|secret"
    )


def test_channel_env_keys_default_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KOKORO_CHANNEL_BASE_URL", raising=False)
    monkeypatch.delenv("KOKORO_CHANNEL_SERVICE_CREDENTIAL", raising=False)
    monkeypatch.delenv("YURALUME_CLOUD_CHANNEL_REQUIRED", raising=False)
    settings = _load_cloud_settings(role="all")
    assert settings.channel_required is False
    assert settings.channel_base_url == ""
    assert settings.channel_service_credential == ""


def test_inactive_self_host_ignores_stale_partial_channel_env(monkeypatch) -> None:
    monkeypatch.setenv("YURALUME_CLOUD_ENABLED", "false")
    monkeypatch.setenv("YURALUME_CLOUD_CHANNEL_REQUIRED", "false")
    monkeypatch.setenv("KOKORO_CHANNEL_BASE_URL", "http://retired-channel:8080")
    monkeypatch.delenv("KOKORO_CHANNEL_SERVICE_CREDENTIAL", raising=False)

    settings = _load_cloud_settings(role="all")

    assert settings.active is False
    assert settings.channel_base_url == "http://retired-channel:8080"


def test_required_channel_accepts_bound_versioned_credential(monkeypatch) -> None:
    monkeypatch.setenv("YURALUME_CLOUD_CHANNEL_REQUIRED", "true")
    monkeypatch.setenv("KOKORO_CHANNEL_BASE_URL", "http://channel-api:8080/")
    monkeypatch.setenv(
        "KOKORO_CHANNEL_SERVICE_CREDENTIAL",
        "kid|core|yuralume-channel|delivery:eligibility-read,delivery:create|secret",
    )

    settings = _load_cloud_settings(role="worker")

    assert settings.channel_required is True
    assert settings.channel_base_url == "http://channel-api:8080"


@pytest.mark.parametrize(
    ("base_url", "credential", "message"),
    [
        ("", "", "requires KOKORO_CHANNEL_BASE_URL"),
        ("http://channel-api:8080", "", "all-or-none"),
        ("", "kid|core|yuralume-channel|delivery:create|secret", "all-or-none"),
        ("channel-api:8080", "kid|core|yuralume-channel|delivery:eligibility-read,delivery:create|secret", "http"),
        ("http://channel-api:8080", "malformed", "service credentials"),
        ("http://channel-api:8080", "kid|wrong|yuralume-channel|delivery:eligibility-read,delivery:create|secret", "caller=core"),
        ("http://channel-api:8080", "kid|core|wrong|delivery:eligibility-read,delivery:create|secret", "audience=yuralume-channel"),
        ("http://channel-api:8080", "kid|core|yuralume-channel|delivery:create|secret", "delivery:eligibility-read"),
    ],
)
def test_required_channel_rejects_missing_or_misbound_configuration(
    monkeypatch, base_url: str, credential: str, message: str,
) -> None:
    monkeypatch.setenv("YURALUME_CLOUD_CHANNEL_REQUIRED", "true")
    monkeypatch.setenv("KOKORO_CHANNEL_BASE_URL", base_url)
    monkeypatch.setenv("KOKORO_CHANNEL_SERVICE_CREDENTIAL", credential)

    with pytest.raises((RuntimeError, ValueError), match=message):
        _load_cloud_settings(role="worker")


class _FakeCharRepo:
    async def list_active(self):
        return []


class _FakeRetryWorker:
    def __init__(self) -> None:
        self.ticks = 0

    async def tick(self, *, now) -> None:
        self.ticks += 1


def _quiet(scheduler: ProactiveScheduler) -> None:
    # Suppress the throttled social passes so an empty tick never reaches the
    # operator-gate collaborators — this test only asserts the retry-worker seam.
    now = datetime.now(timezone.utc)
    scheduler._last_encounter_plan_at = now
    scheduler._last_peer_knowledge_at = now


@pytest.mark.asyncio
async def test_scheduler_runs_retry_worker_when_wired() -> None:
    worker = _FakeRetryWorker()
    scheduler = ProactiveScheduler(
        dispatcher=object(),
        character_repository=_FakeCharRepo(),
        proactive_delivery_retry_worker=worker,
    )
    _quiet(scheduler)
    await scheduler._run_tick({})
    assert worker.ticks == 1


@pytest.mark.asyncio
async def test_scheduler_no_retry_worker_on_self_host() -> None:
    scheduler = ProactiveScheduler(
        dispatcher=object(),
        character_repository=_FakeCharRepo(),
    )
    _quiet(scheduler)
    assert scheduler._proactive_delivery_retry_worker is None
    # The guarded block is skipped; the tick still completes cleanly.
    await scheduler._run_tick({})
