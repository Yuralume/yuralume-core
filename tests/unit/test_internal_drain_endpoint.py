"""The drain endpoint and its two metrics series (GD1-A).

``HOSTED_PROMPT_PACK_DISTRIBUTION_PLAN`` §7.3 GD1 turns these into a deploy
handshake: the router removes the replica, ``deploy.sh`` posts ``/drain``, then
polls ``yuralume_core_active_turns`` to zero (180s cap) before recreating the
container. What that contract needs pinned:

1. the endpoint lives on the internal management surface and answers to the same
   bearer channel as the metrics scrape — no second credential to rotate, and no
   public route a self-host operator did not already have;
2. it is idempotent, because a deploy that retries or restarts a half-finished
   roll will send it twice, and the second answer must still be the live count;
3. ``yuralume_core_draining`` acknowledges the request, so an ``active_turns 0``
   reading can be told apart from a replica that never heard about the drain and
   is still happily accepting turns.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.services.drain_state import (
    SERVER_DRAINING_CODE,
    ServerDrainingError,
)
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.observability.drain_metrics import (
    render_drain_metrics,
)

_DRAIN_PATH = "/api/internal/v1/drain"
_METRICS_PATH = "/api/internal/v1/metrics"
_METRICS_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_TOKEN = "scrape-secret"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _app(monkeypatch: pytest.MonkeyPatch, *, token: str | None = _TOKEN):
    if token is None:
        monkeypatch.delenv(_METRICS_ENV, raising=False)
    else:
        monkeypatch.setenv(_METRICS_ENV, token)
    return create_app(
        AppSettings(database_url="", process=ProcessSettings(role="api")),
    )


def _scrape(client: TestClient) -> str:
    resp = client.get(_METRICS_PATH, headers=_AUTH)
    assert resp.status_code == 200
    return resp.text


# --- auth: the same channel as the scrape, fail-closed ----------------------


def test_drain_requires_the_metrics_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    with TestClient(app) as client:
        missing = client.post(_DRAIN_PATH)
        wrong = client.post(_DRAIN_PATH, headers={"Authorization": "Bearer nope"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    # The refused calls must not have flipped anything: an endpoint that drained
    # first and authorized afterwards would let an unauthenticated request take
    # a replica out of service.
    assert app.state.container.drain_state.draining is False


def test_drain_is_fail_closed_when_the_channel_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Self-host default: no token env, so the management surface answers 503 to
    # everyone — the same posture the metrics scrape already has.
    app = _app(monkeypatch, token=None)
    with TestClient(app) as client:
        resp = client.post(_DRAIN_PATH, headers=_AUTH)

    assert resp.status_code == 503
    assert app.state.container.drain_state.draining is False


def test_drain_is_not_on_the_public_api_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    with TestClient(app) as client:
        # Not served on the operator surface, and not at the app root either.
        # (The SPA catch-all answers unknown GETs, hence 405 rather than 404 —
        # what matters is that neither address drains anything.)
        assert client.post("/api/v1/drain").status_code in (404, 405)
        assert client.post("/drain").status_code in (404, 405)

    assert app.state.container.drain_state.draining is False
    assert [r.path for r in app.routes if "drain" in getattr(r, "path", "")] == [
        _DRAIN_PATH,
    ]


# --- the handshake ----------------------------------------------------------


def test_drain_flips_the_state_and_reports_the_live_turn_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    state = app.state.container.drain_state
    state.try_enter_turn()
    state.try_enter_turn()

    with TestClient(app) as client:
        resp = client.post(_DRAIN_PATH, headers=_AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"draining": True, "active_turns": 2}
    assert state.draining is True


def test_drain_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(monkeypatch)
    state = app.state.container.drain_state
    # A turn that was already running when the deploy reached this replica —
    # the only way one can be counted at all, since admission and the drain
    # check are the same atomic step.
    turn = state.try_enter_turn()

    with TestClient(app) as client:
        first = client.post(_DRAIN_PATH, headers=_AUTH)
        second = client.post(_DRAIN_PATH, headers=_AUTH)
        turn.release()
        third = client.post(_DRAIN_PATH, headers=_AUTH)

    # A retry is not an error — and its body is the reason to retry: the caller
    # is asking "how much is still running", not "please drain again".
    assert [first.status_code, second.status_code, third.status_code] == [200] * 3
    assert first.json()["active_turns"] == 1
    assert second.json()["active_turns"] == 1
    assert third.json()["active_turns"] == 0
    assert all(r.json()["draining"] is True for r in (first, second, third))


# --- metrics: the gauge the deploy polls ------------------------------------


def test_metrics_expose_the_drain_switch_and_turn_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    state = app.state.container.drain_state

    with TestClient(app) as client:
        before = _scrape(client)
        assert "yuralume_core_draining 0" in before
        assert "yuralume_core_active_turns 0" in before

        turn = state.try_enter_turn()
        assert "yuralume_core_active_turns 1" in _scrape(client)

        assert client.post(_DRAIN_PATH, headers=_AUTH).status_code == 200
        draining = _scrape(client)
        assert "yuralume_core_draining 1" in draining
        # Still 1: drain does not abort the turn, it waits for it. A gauge that
        # dropped to 0 on the drain request would tell the deploy to kill it.
        assert "yuralume_core_active_turns 1" in draining

        turn.release()
        settled = _scrape(client)

    assert "yuralume_core_active_turns 0" in settled
    assert "yuralume_core_draining 1" in settled
    # The pre-existing series are untouched.
    assert "yuralume_core_scheduler_ticks_total" in settled
    assert "yuralume_core_prompt_pack_info" in settled


def test_scrape_survives_a_drain_state_that_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken:
        @property
        def draining(self) -> bool:
            raise RuntimeError("boom")

        @property
        def active_turns(self) -> int:
            raise RuntimeError("boom")

    app = _app(monkeypatch)
    app.state.container.drain_state = _Broken()

    with TestClient(app) as client:
        body = _scrape(client)

    # Honest omission, not a 500: the scrape carries every other subsystem's
    # observability and must not be taken down by this one.
    assert "yuralume_core_draining" not in body
    assert "yuralume_core_scheduler_ticks_total" in body


def test_public_health_never_mentions_draining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app(monkeypatch)
    with TestClient(app) as client:
        assert client.post(_DRAIN_PATH, headers=_AUTH).status_code == 200
        health = client.get("/health")

    # /health is the router's own liveness probe; if it started failing on drain
    # the router would mark the replica down for the wrong reason, and the drain
    # story here is "stop new work", not "stop being alive".
    assert health.status_code == 200
    assert "drain" not in health.text


# --- what a refused turn looks like on the wire -----------------------------


def test_a_draining_refusal_becomes_a_structured_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One app-wide handler, so every transport that drives a turn — web chat,
    external chat, messaging webhooks — refuses identically. Uniformity is the
    point: this is the fallback for the case where the router did *not* stop
    sending this replica traffic, and a caller cannot know which door it came
    through."""
    app = _app(monkeypatch)

    async def _refuse() -> None:
        raise ServerDrainingError()

    app.add_api_route("/__drain_probe", _refuse, methods=["GET"])
    # Ahead of the SPA catch-all, which otherwise answers unknown GETs.
    app.router.routes.insert(0, app.router.routes.pop())

    with TestClient(app) as client:
        resp = client.get("/__drain_probe")

    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == SERVER_DRAINING_CODE
    assert resp.json()["detail"]["message"]
    # A retryable answer: the client's next attempt should reach a live replica.
    assert resp.headers["Retry-After"] == "1"


# --- renderer ---------------------------------------------------------------


def test_renderer_declares_types_and_ends_with_a_newline() -> None:
    body = render_drain_metrics(draining=True, active_turns=3)

    assert "# TYPE yuralume_core_active_turns gauge" in body
    assert "# TYPE yuralume_core_draining gauge" in body
    assert "yuralume_core_active_turns 3" in body
    assert "yuralume_core_draining 1" in body
    assert body.endswith("\n")
