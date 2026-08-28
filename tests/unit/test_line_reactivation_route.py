"""``GET /api/internal/v1/cloud/line-reactivation/candidates`` (LR T1).

Same credential posture as its ``internal_cloud`` siblings — unset
config fails closed with 503, a wrong credential is 401 — plus the one
thing this route adds: a self-host deployment must say **why** it cannot
answer. The campaign subsystem is wired only where the hosted proactive
path exists, so an unwired container answers 503
``cloud_mode_required`` rather than 404-ing a route that is in fact
mounted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_TOKEN = "s2s-secret-token"
_PATH = "/api/internal/v1/cloud/line-reactivation/candidates"
_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _configure_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-line-reactivation")


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _configure_env(monkeypatch)
    return TestClient(create_app())


class _StubService:
    """Stands in for the real service — the route is the unit here."""

    def __init__(self, candidates) -> None:  # noqa: ANN001
        self._candidates = candidates

    async def list_candidates(self):  # noqa: ANN201
        from kokoro_link.application.services.line_reactivation import (
            ReactivationCandidateList,
        )

        return ReactivationCandidateList(
            generated_at=_NOW, candidates=tuple(self._candidates),
        )


def _candidate(**overrides):  # noqa: ANN201
    from kokoro_link.application.services.line_reactivation import (
        ReactivationCandidate,
    )

    fields = {
        "character_id": "c1",
        "character_name": "小晶",
        "user_id": "cloud:acct-1",
        "tier_key": "plus",
        "last_active_at": _NOW - timedelta(days=30),
        "dormancy_days": 7,
        "dormant_for_days": 30,
        "eligible": True,
        "eligibility_reason": None,
    }
    fields.update(overrides)
    return ReactivationCandidate(**fields)


def test_missing_credential_config_returns_503(monkeypatch) -> None:
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_TOKENS", raising=False)
    monkeypatch.delenv("KOKORO_CLOUD_INTERNAL_CREDENTIALS", raising=False)
    client = _client(monkeypatch)

    resp = client.get(_PATH, headers={"Authorization": f"Bearer {_TOKEN}"})

    assert resp.status_code == 503


def test_wrong_token_returns_401(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)

    resp = client.get(_PATH, headers={"Authorization": "Bearer nope"})

    assert resp.status_code == 401


def test_self_host_reports_cloud_mode_required(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)
    # Cloud-inactive harness → the subsystem is not auto-wired.
    assert (
        client.app.state.container.line_reactivation_candidate_service is None
    )

    resp = client.get(_PATH, headers={"Authorization": f"Bearer {_TOKEN}"})

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "cloud_mode_required"


def test_candidates_are_rendered_in_the_contract_shape(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    client = _client(monkeypatch)
    client.app.state.container.line_reactivation_candidate_service = (
        _StubService([
            _candidate(),
            _candidate(
                character_id="c2",
                tier_key=None,
                eligible=False,
                eligibility_reason="transient_error",
            ),
        ])
    )

    resp = client.get(_PATH, headers={"Authorization": f"Bearer {_TOKEN}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["generated_at"].startswith("2026-08-28T12:00:00")
    assert [item["character_id"] for item in body["candidates"]] == ["c1", "c2"]
    first = body["candidates"][0]
    assert set(first) == {
        "character_id",
        "character_name",
        "user_id",
        "tier_key",
        "last_active_at",
        "dormancy_days",
        "dormant_for_days",
        "eligible",
        "eligibility_reason",
    }
    assert first["character_name"] == "小晶"
    assert first["dormant_for_days"] == 30
    second = body["candidates"][1]
    assert second["tier_key"] is None
    assert second["eligible"] is False
    assert second["eligibility_reason"] == "transient_error"
