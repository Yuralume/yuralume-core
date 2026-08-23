"""Cloud→Core internal stats channel.

``GET /api/internal/v1/cloud/stats/characters`` — credential-gated with its own
``stats:read`` scope, never behind the operator JWT. What is covered:

* the gate's three states (unconfigured → 503, wrong credential → 401, a
  credential minted for another scope → 401) — a showcase or freeze credential
  must not be able to read a platform-wide census,
* an unwired read model answers 503, never ``{"total": 0}``: "no database" and
  "no characters" are different facts and the dashboard renders them
  differently ("—" vs "0"),
* the happy path shape the Cloud client parses.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from kokoro_link.application.services.character_activity_stats import (
    CharacterActivityStatsService,
)
from kokoro_link.contracts.character_activity_stats import TierCharacterCounts

_URL = "/api/internal/v1/cloud/stats/characters"
_CREDENTIALS = (
    "core-kid|cloud-user|yuralume-core|freeze:write,showcase:read,stats:read|core-secret"
)


def _client(monkeypatch, *, credentials: str | None = _CREDENTIALS) -> TestClient:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-stats-key")
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_TOKENS", raising=False)
    if credentials is None:
        monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_CREDENTIALS", raising=False)
    else:
        monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_CREDENTIALS", credentials)

    from kokoro_link.api.app import create_app

    return TestClient(create_app())


def _headers(scope: str = "stats:read") -> dict[str, str]:
    return {
        "X-Yuralume-Service-Token": "core-secret",
        "X-Yuralume-Service-Key-Id": "core-kid",
        "X-Yuralume-Service-Caller": "cloud-user",
        "X-Yuralume-Service-Audience": "yuralume-core",
        "X-Yuralume-Service-Scope": scope,
    }


class _StubStats:
    def __init__(self, buckets):
        self._buckets = buckets

    async def counts_by_tier(self):
        return list(self._buckets)

    async def count_engaged_since(self, tier, cutoff):  # pragma: no cover - unused
        raise AssertionError("no dormancy policy is wired in this test")


def _wire(client: TestClient, buckets) -> None:
    client.app.state.container.character_activity_stats_service = (
        CharacterActivityStatsService(stats=_StubStats(buckets))
    )


def test_unconfigured_channel_answers_503(monkeypatch):
    client = _client(monkeypatch, credentials=None)
    assert client.get(_URL, headers=_headers()).status_code == 503


def test_wrong_secret_answers_401(monkeypatch):
    client = _client(monkeypatch)
    headers = _headers() | {"X-Yuralume-Service-Token": "not-the-secret"}
    assert client.get(_URL, headers=headers).status_code == 401


def test_credential_minted_for_another_scope_cannot_read_the_census(monkeypatch):
    """A showcase credential reads one tenant's content; it must not be able to
    ask how large the whole platform is."""
    client = _client(monkeypatch)
    assert client.get(_URL, headers=_headers("showcase:read")).status_code == 401


def test_unwired_read_model_answers_503_rather_than_a_fabricated_zero(monkeypatch):
    client = _client(monkeypatch)
    client.app.state.container.character_activity_stats_service = None

    assert client.get(_URL, headers=_headers()).status_code == 503


def test_returns_total_and_active_counts(monkeypatch):
    client = _client(monkeypatch)
    _wire(client, [
        TierCharacterCounts(tier="standard", total=9, schedulable=6),
        TierCharacterCounts(tier="free", total=4, schedulable=1),
    ])

    response = client.get(_URL, headers=_headers())

    assert response.status_code == 200
    assert response.json() == {"total": 13, "active": 7}
