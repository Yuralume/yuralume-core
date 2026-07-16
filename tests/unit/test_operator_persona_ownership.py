"""Owner-scoped persona mutations — the security boundary behind the
``/operator/persona`` reject / state-transition endpoints.

The persona aggregate is per-(character, operator). A single-row mutation
that only takes a row id (``candidate_id`` / ``field_id``) must still
refuse to touch a row owned by a different operator — otherwise any
logged-in user could veto another user's staging rows by guessing ids.
These tests exercise that check at the service layer with a fake repo,
so the rule holds regardless of the HTTP wiring.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from kokoro_link.application.services.operator_persona_service import (
    OperatorPersonaService,
)


class _FakeRepo:
    """Minimal persona repo: only the methods the owner-checked mutations
    touch. ``scope_by_id`` maps a row id to its ``(character, operator)``."""

    def __init__(self, scope_by_id: dict[str, tuple[str, str]]) -> None:
        self._scope = scope_by_id
        self.candidate_marks: list[tuple[str, str]] = []
        self.field_marks: list[tuple[str, str]] = []

    async def get_row_scope(self, row_id: str) -> tuple[str, str] | None:
        return self._scope.get(row_id)

    async def mark_state(self, candidate_id: str, state: str) -> None:
        self.candidate_marks.append((candidate_id, state))

    async def mark_field_state(self, field_id: str, state: str) -> None:
        self.field_marks.append((field_id, state))


def _service(repo: _FakeRepo) -> OperatorPersonaService:
    # strength_calculator / settings aren't touched by the mutation paths;
    # invalidate_cache only clears an in-process dict.
    return OperatorPersonaService(
        repository=repo,
        strength_calculator=None,  # type: ignore[arg-type]
        settings=SimpleNamespace(),  # type: ignore[arg-type]
    )


def test_reject_candidate_succeeds_for_owner():
    repo = _FakeRepo({"cand-1": ("char-A", "alice")})
    svc = _service(repo)

    ok = asyncio.run(svc.reject_candidate_for_operator("cand-1", "alice"))

    assert ok is True
    assert repo.candidate_marks == [("cand-1", "rejected")]


def test_reject_candidate_refuses_other_operator():
    repo = _FakeRepo({"cand-1": ("char-A", "alice")})
    svc = _service(repo)

    ok = asyncio.run(svc.reject_candidate_for_operator("cand-1", "bob"))

    assert ok is False
    assert repo.candidate_marks == []  # never mutated


def test_reject_candidate_missing_row_is_false():
    repo = _FakeRepo({})
    svc = _service(repo)

    ok = asyncio.run(svc.reject_candidate_for_operator("ghost", "alice"))

    assert ok is False
    assert repo.candidate_marks == []


def test_transition_field_succeeds_for_owner():
    repo = _FakeRepo({"fld-1": ("char-A", "alice")})
    svc = _service(repo)

    ok = asyncio.run(
        svc.transition_field_state_for_operator("fld-1", "stale", "alice"),
    )

    assert ok is True
    assert repo.field_marks == [("fld-1", "stale")]


def test_transition_field_refuses_other_operator():
    repo = _FakeRepo({"fld-1": ("char-A", "alice")})
    svc = _service(repo)

    ok = asyncio.run(
        svc.transition_field_state_for_operator("fld-1", "stale", "bob"),
    )

    assert ok is False
    assert repo.field_marks == []


def test_transition_field_missing_row_is_false():
    repo = _FakeRepo({})
    svc = _service(repo)

    ok = asyncio.run(
        svc.transition_field_state_for_operator("ghost", "stale", "alice"),
    )

    assert ok is False
    assert repo.field_marks == []
