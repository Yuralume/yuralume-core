"""The chat routes refuse a drain *before* they touch persistent state (C4).

GD1-A's refusal promise is specific: "a 503 here leaves no trace at all". The
service layer keeps it — ``_begin_turn`` refuses on its first line. The route
did not, and the gap was invisible because it lived above the service: both chat
entry points resolve character ownership first, and that lookup runs the
read-time rest-recovery refresh, which *persists* character / state / emotion
rows. So a request that arrived at a draining replica wrote to the database on
its way to being told "this replica is not accepting work" — on a replica about
to be SIGKILLed, for a turn that never ran.

Hence the gate moves in front of the lookup and the tests assert the ordering
directly (the lookup is never called), not just the status code. The service
gate stays where it is: routes are not the only way into ``ChatService``.

Driven through the real app because the 503 shape is a collaboration between
the dependency and the app-wide ``ServerDrainingError`` handler, and a
hand-rolled test app would only be testing a copy of it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app

_PATHS = ["/api/v1/chat/messages", "/api/v1/chat/messages/stream"]


def _configure_test_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")


class _App:
    """A live app plus a count of how often ownership was resolved."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure_test_app_env(monkeypatch)
        self.app = create_app()
        self.client = TestClient(self.app)
        self.container = self.app.state.container
        self.lookups = 0
        self.saves = 0
        self._wrap_character_lookup()
        self._wrap_character_save()

    def _wrap_character_lookup(self) -> None:
        service = self.container.character_service
        inner = service.get_character_entity

        async def _counted(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self.lookups += 1
            return await inner(*args, **kwargs)

        service.get_character_entity = _counted

    def _wrap_character_save(self) -> None:
        repository = self.container.character_repository
        if repository is None:  # pragma: no cover — always wired in this app
            return
        inner = repository.save

        async def _counted(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self.saves += 1
            return await inner(*args, **kwargs)

        repository.save = _counted

    def create_character(self) -> str:
        created = self.client.post(
            "/api/v1/characters",
            json={"name": "Mio", "summary": "", "personality": [], "interests": []},
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    def send(self, path: str, character_id: str):  # noqa: ANN202
        return self.client.post(
            path,
            json={
                "character_id": character_id,
                "provider_id": "fake",
                "message": "hi",
            },
        )


@pytest.mark.parametrize("path", _PATHS)
def test_draining_refuses_before_the_ownership_lookup_can_persist_anything(
    path: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _App(monkeypatch)
    character_id = harness.create_character()
    harness.lookups = 0
    harness.saves = 0

    harness.container.drain_state.begin_drain()
    response = harness.send(path, character_id)

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "draining"
    assert response.headers["Retry-After"] == "1"
    # The whole point: not one read that could have written, and therefore not
    # one write. A refusal that persisted rest-recovery state would still be a
    # refusal — just not a free one.
    assert harness.lookups == 0
    assert harness.saves == 0
    assert harness.container.drain_state.active_turns == 0


@pytest.mark.parametrize("path", _PATHS)
def test_an_undrained_replica_is_untouched(
    path: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The self-host red line: the gate costs one ``False`` and changes nothing."""
    harness = _App(monkeypatch)
    character_id = harness.create_character()
    harness.lookups = 0

    response = harness.send(path, character_id)

    assert response.status_code == 200, response.text
    assert harness.lookups == 1
    assert harness.container.drain_state.draining is False
    assert harness.container.drain_state.active_turns == 0
