"""``POST /campaigns`` and ``GET /campaigns/{id}`` (LR T2).

Wire-level pins for the two shapes T3's proxy and T4's console are being
written against — the 202/409/400/404 status map, and the report body
field-for-field against plan §2. The service itself is stubbed; its
behaviour has its own tests, and mixing the two here would let a route
regression hide behind a passing runner assertion.
"""

from __future__ import annotations

from datetime import datetime, timezone

_TOKEN = "s2s-secret-token"
_BASE = "/api/internal/v1/cloud/line-reactivation/campaigns"
_CAMPAIGN = "3f1c0b6a-0000-4000-8000-000000000001"
_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _client(monkeypatch):  # noqa: ANN001, ANN201
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-line-reactivation")
    monkeypatch.setenv("KOKORO_CLOUD_INTERNAL_TOKENS", _TOKEN)
    return TestClient(create_app())


class _StubCampaignService:
    def __init__(self, *, start=None, report=None) -> None:  # noqa: ANN001
        self._start = start
        self._report = report
        self.started: list[tuple] = []

    async def start(self, *, campaign_id, character_ids, actor):  # noqa: ANN001, ANN201
        self.started.append((campaign_id, tuple(character_ids), actor))
        if isinstance(self._start, Exception):
            raise self._start
        return self._start

    async def report(self, campaign_id):  # noqa: ANN001, ANN201
        if isinstance(self._report, Exception):
            raise self._report
        return self._report


def _install(client, service):  # noqa: ANN001, ANN201
    client.app.state.container.line_reactivation_campaign_service = service
    return service


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _start_result(**overrides):  # noqa: ANN201
    from kokoro_link.application.services.line_reactivation import (
        CampaignStartResult,
    )

    fields = {
        "campaign_id": _CAMPAIGN,
        "status": "running",
        "total": 2,
        "resumed": False,
    }
    fields.update(overrides)
    return CampaignStartResult(**fields)


def test_start_accepts_and_reports_the_selection_size(monkeypatch) -> None:
    client = _client(monkeypatch)
    service = _install(client, _StubCampaignService(start=_start_result()))

    resp = client.post(
        _BASE,
        headers=_headers(),
        json={
            "campaign_id": _CAMPAIGN,
            "character_ids": ["c1", "c2"],
            "actor": "ops@example",
        },
    )

    assert resp.status_code == 202
    assert resp.json() == {
        "campaign_id": _CAMPAIGN,
        "status": "running",
        "total": 2,
        "resumed": False,
    }
    assert service.started == [(_CAMPAIGN, ("c1", "c2"), "ops@example")]


def test_resubmitting_the_same_selection_reads_as_a_resume(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install(client, _StubCampaignService(start=_start_result(resumed=True)))

    resp = client.post(
        _BASE,
        headers=_headers(),
        json={
            "campaign_id": _CAMPAIGN,
            "character_ids": ["c1", "c2"],
            "actor": "ops@example",
        },
    )

    assert resp.status_code == 202
    assert resp.json()["resumed"] is True


def test_a_reused_id_with_another_selection_is_409(monkeypatch) -> None:
    from kokoro_link.contracts.line_reactivation import (
        LineReactivationCampaignConflictError,
    )

    client = _client(monkeypatch)
    _install(
        client,
        _StubCampaignService(
            start=LineReactivationCampaignConflictError("different selection"),
        ),
    )

    resp = client.post(
        _BASE,
        headers=_headers(),
        json={
            "campaign_id": _CAMPAIGN,
            "character_ids": ["c9"],
            "actor": "ops@example",
        },
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "campaign_conflict"


def test_an_empty_selection_is_400(monkeypatch) -> None:
    from kokoro_link.application.services.line_reactivation import (
        LineReactivationEmptySelectionError,
    )

    client = _client(monkeypatch)
    _install(
        client,
        _StubCampaignService(
            start=LineReactivationEmptySelectionError("nothing selected"),
        ),
    )

    resp = client.post(
        _BASE,
        headers=_headers(),
        json={
            "campaign_id": _CAMPAIGN,
            "character_ids": [],
            "actor": "ops@example",
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "empty_selection"


def test_a_malformed_campaign_id_is_400_not_500(monkeypatch) -> None:
    """The ledger column is ``String(64)``. Letting the database notice
    turns a client defect into a driver error and a 500."""
    from kokoro_link.application.services.line_reactivation import (
        LineReactivationInvalidCampaignIdError,
    )

    client = _client(monkeypatch)
    _install(
        client,
        _StubCampaignService(
            start=LineReactivationInvalidCampaignIdError("too long"),
        ),
    )

    resp = client.post(
        _BASE,
        headers=_headers(),
        json={
            "campaign_id": "x" * 200,
            "character_ids": ["c1"],
            "actor": "ops@example",
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_campaign_id"


def test_a_selection_naming_unknown_characters_is_400(monkeypatch) -> None:
    """Not 409: a new ``campaign_id`` cannot fix a stale candidate list,
    and the body names the rows the console should drop."""
    from kokoro_link.application.services.line_reactivation import (
        LineReactivationUnknownCharactersError,
    )

    client = _client(monkeypatch)
    _install(
        client,
        _StubCampaignService(
            start=LineReactivationUnknownCharactersError(("ghost", "phantom")),
        ),
    )

    resp = client.post(
        _BASE,
        headers=_headers(),
        json={
            "campaign_id": _CAMPAIGN,
            "character_ids": ["c1", "ghost", "phantom"],
            "actor": "ops@example",
        },
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_characters"
    assert detail["missing_character_ids"] == ["ghost", "phantom"]


def test_start_requires_the_credential(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install(client, _StubCampaignService(start=_start_result()))

    resp = client.post(
        _BASE,
        headers={"Authorization": "Bearer nope"},
        json={
            "campaign_id": _CAMPAIGN,
            "character_ids": ["c1"],
            "actor": "ops@example",
        },
    )

    assert resp.status_code == 401


def test_start_on_a_self_host_deployment_says_cloud_mode_required(
    monkeypatch,
) -> None:
    client = _client(monkeypatch)
    assert client.app.state.container.line_reactivation_campaign_service is None

    resp = client.post(
        _BASE,
        headers=_headers(),
        json={
            "campaign_id": _CAMPAIGN,
            "character_ids": ["c1"],
            "actor": "ops@example",
        },
    )

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "cloud_mode_required"


def test_report_renders_the_contract_shape(monkeypatch) -> None:
    from kokoro_link.application.services.line_reactivation import (
        CampaignItemReport,
        CampaignReport,
    )

    client = _client(monkeypatch)
    _install(
        client,
        _StubCampaignService(
            report=CampaignReport(
                campaign_id=_CAMPAIGN,
                status="running",
                actor="ops@example",
                created_at=_NOW,
                completed_at=None,
                total=3,
                done=2,
                items=(
                    CampaignItemReport(
                        character_id="c1",
                        character_name="小晶",
                        outcome="gate_blocked",
                        detail="night-hours floor",
                        attempted_at=_NOW,
                    ),
                    CampaignItemReport(
                        character_id="c2",
                        character_name="阿澈",
                        outcome=None,
                        detail=None,
                        attempted_at=None,
                    ),
                    CampaignItemReport(
                        character_id="c3",
                        character_name="小雨",
                        outcome="sent",
                        detail="admin reactivation",
                        message_text="好久不見，最近過得還好嗎？",
                        attempted_at=_NOW,
                    ),
                ),
            ),
        ),
    )

    resp = client.get(f"{_BASE}/{_CAMPAIGN}", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "campaign_id",
        "status",
        "actor",
        "created_at",
        "completed_at",
        "total",
        "done",
        "items",
    }
    assert body["completed_at"] is None
    assert body["done"] == 2
    assert set(body["items"][0]) == {
        "character_id",
        "character_name",
        "outcome",
        "detail",
        "message_text",
        "attempted_at",
    }
    assert body["items"][0]["outcome"] == "gate_blocked"
    assert body["items"][1]["outcome"] is None
    assert body["items"][1]["attempted_at"] is None
    # The contract G3 aligns on: ``message_text`` is non-null on exactly
    # the ``sent`` rows, and carries the body in full.
    assert body["items"][2]["outcome"] == "sent"
    assert body["items"][2]["message_text"] == "好久不見，最近過得還好嗎？"
    assert body["items"][0]["message_text"] is None
    assert body["items"][1]["message_text"] is None


def test_report_is_404_for_an_unknown_campaign(monkeypatch) -> None:
    client = _client(monkeypatch)
    _install(client, _StubCampaignService(report=None))

    resp = client.get(f"{_BASE}/nope", headers=_headers())

    assert resp.status_code == 404
