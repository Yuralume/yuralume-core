"""F2 — lifespan shutdown drains ChatService's fire-and-forget background
work before the shared engine's pool is disposed.

``ChatService._schedule_background`` (post-turn outcome-claim audits,
auto-consolidation, …) is plain ``asyncio.create_task`` — nothing tracked
it at shutdown before this ticket, so a rolling deploy could kill a task
mid-judge with zero log and zero counter, the exact "Never silent"
violation the rest of HV4 exists to close (see
``test_chat_outcome_claim_repair.py`` for the auditor-side half: what the
cancelled task itself records when this drain gives up on it).

These tests pin the two guarantees this ticket adds:
* shutdown actually awaits ``ChatService.wait_for_pending()`` — and does
  so *before* the engine it may still be writing through gets disposed;
* the wait is bounded — a background task that never finishes must not
  hang the process shutdown forever, and giving up on it is logged.

Detached chat turns (``chat_stream_relay``) joined the same sequence later,
one step earlier in it: a turn that is still generating for a player who
already closed the tab is what *creates* the F2 tails, so it has to land
first. Same two guarantees, same bound.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import kokoro_link.api.app as app_module
import kokoro_link.application.services.chat_stream_relay as relay_module
from kokoro_link.api.app import create_app
from kokoro_link.bootstrap.settings import AppSettings


class _RecordingEngine:
    """Stand-in for an AsyncEngine — records when dispose() actually ran."""

    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def dispose(self) -> None:
        self._order.append("engine_dispose")


class _StubChatService:
    """Stands in for the real ``ChatService.wait_for_pending()`` seam."""

    def __init__(
        self,
        *,
        order: list[str] | None = None,
        hang: bool = False,
        raise_error: Exception | None = None,
    ) -> None:
        self._order = order
        self._hang = hang
        self._raise_error = raise_error
        self.wait_calls = 0

    async def wait_for_pending(self) -> None:
        self.wait_calls += 1
        if self._order is not None:
            self._order.append("chat_drain")
        if self._raise_error is not None:
            raise self._raise_error
        if self._hang:
            # Never completes on its own — only a bounded caller escapes.
            await asyncio.Event().wait()


def _app_with_stub_chat_service(chat_service: _StubChatService):
    app = create_app(AppSettings(database_url=""))
    app.state.container.chat_service = chat_service
    return app


def test_shutdown_awaits_chat_service_drain_before_engine_dispose() -> None:
    order: list[str] = []
    app = _app_with_stub_chat_service(_StubChatService(order=order))
    app.state.container.db_engine = _RecordingEngine(order)

    with TestClient(app):
        assert order == []  # nothing runs until shutdown

    assert order == ["chat_drain", "engine_dispose"]


def test_shutdown_drain_gives_up_after_its_bound_and_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app_module, "CHAT_BACKGROUND_DRAIN_TIMEOUT_SECONDS", 0.05)
    order: list[str] = []
    stub = _StubChatService(hang=True)
    app = _app_with_stub_chat_service(stub)
    app.state.container.db_engine = _RecordingEngine(order)

    # The whole point: this must not block forever even though the stub's
    # wait_for_pending() never returns on its own.
    with TestClient(app):
        pass

    assert stub.wait_calls == 1
    # Fail-soft: giving up on the drain must not skip the engine dispose
    # that follows it.
    assert order == ["engine_dispose"]
    captured = capsys.readouterr()
    assert "chat service background drain timed out" in captured.out


def _spawn_route(app, work) -> None:  # noqa: ANN001
    """Register a route that starts ``work`` *inside the app's own loop*.

    A task created from the test thread lands on a different loop than the one
    the lifespan shuts down, so it would be filtered out of the drain and the
    test would pass for the wrong reason.
    """

    @app.get("/__test/spawn-detached-turn")
    async def _spawn() -> dict:  # noqa: ANN202
        relay_module._track(asyncio.create_task(work()))  # noqa: SLF001
        return {"ok": True}

    # …and ahead of the SPA catch-all, which is registered first and would
    # otherwise answer 200 without this handler ever running.
    app.router.routes.insert(0, app.router.routes.pop())


def test_shutdown_waits_for_detached_turns_before_the_background_drain() -> None:
    """Order matters: finishing a turn is what schedules the F2 tails.

    Draining the fire-and-forget work first would drain an empty set and then
    abandon the very tasks the detached turn was about to create.
    """
    order: list[str] = []
    app = _app_with_stub_chat_service(_StubChatService(order=order))
    app.state.container.db_engine = _RecordingEngine(order)

    async def _detached_turn() -> None:
        await asyncio.sleep(0.05)
        order.append("detached_turn")

    _spawn_route(app, _detached_turn)

    with TestClient(app) as client:
        # A turn whose client walked away, still writing at shutdown.
        assert client.get("/__test/spawn-detached-turn").status_code == 200
        assert order == []

    assert order == ["detached_turn", "chat_drain", "engine_dispose"]


def test_shutdown_gives_up_on_a_wedged_detached_turn_and_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A turn that cannot finish must not hold the process open."""
    monkeypatch.setattr(app_module, "CHAT_BACKGROUND_DRAIN_TIMEOUT_SECONDS", 0.05)
    order: list[str] = []
    app = _app_with_stub_chat_service(_StubChatService(order=order))
    app.state.container.db_engine = _RecordingEngine(order)

    async def _never_ends() -> None:
        await asyncio.Event().wait()

    _spawn_route(app, _never_ends)

    with TestClient(app) as client:
        assert client.get("/__test/spawn-detached-turn").status_code == 200

    # Fail-soft: giving up must not skip the rest of the shutdown sequence.
    assert order == ["chat_drain", "engine_dispose"]
    assert "detached chat turns still running" in capsys.readouterr().out


def test_shutdown_drain_failure_is_fail_soft(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unexpected exception out of the drain itself (not a timeout) must
    not mask a clean shutdown — same contract as the db_engine dispose
    fail-soft it sits next to."""
    stub = _StubChatService(raise_error=RuntimeError("boom"))
    app = _app_with_stub_chat_service(stub)
    app.state.container.db_engine = _RecordingEngine([])

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    captured = capsys.readouterr()
    assert "chat service background drain failed" in captured.out
