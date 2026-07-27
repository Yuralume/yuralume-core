"""Cooperative pair-lease abort threading through the encounter stages (#8).

Before the fix the pair lease was only checked (via ``session.lost``) AFTER
``step_pair`` had fully returned — the losing worker ran every LLM stage and wrote
transcript / memory / relationship first. These tests lock the new wiring: the
abort hook is checked BETWEEN plan and run (``step_pair``) and BEFORE claiming each
encounter (``run_pair``), so a stolen lease stops the loser at a stage boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterRunner,
    CharacterEncounterService,
)
from kokoro_link.application.services.encounter_pair_lease import (
    EncounterPairLeaseLost,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _Rel:
    id: str = "rel-1"
    character_a_id: str = "a"
    character_b_id: str = "b"


def _lost_hook():
    """An abort hook that always raises — a lease already lost."""
    def _raise() -> None:
        raise EncounterPairLeaseLost("a", "b")
    return _raise


# --------------------------------------------------------------------------- #
# Service: step_pair checks abort BETWEEN plan and run
# --------------------------------------------------------------------------- #

class _FakePlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan_pair(self, relationship, *, now=None, gate=None):  # noqa: ANN001
        self.calls += 1
        return None


class _FakeRunner:
    def __init__(self) -> None:
        self.abort_seen: list = []

    async def run_pair(  # noqa: ANN001
        self, relationship, *, now=None, gate=None, abort=None,
    ):
        self.abort_seen.append(abort)
        return ([], [])


async def test_step_pair_threads_abort_into_run_pair() -> None:
    planner, runner = _FakePlanner(), _FakeRunner()
    service = CharacterEncounterService(
        planner=planner, runner=runner, encounter_repository=None,
    )
    hook = _lost_hook()

    await service.step_pair(_Rel(), now=NOW, abort=None)
    assert runner.abort_seen == [None]  # no abort → threaded through as None

    runner.abort_seen.clear()
    await service.step_pair(_Rel(), now=NOW, abort=lambda: None)
    assert len(runner.abort_seen) == 1 and runner.abort_seen[0] is not None


async def test_step_pair_aborts_between_plan_and_run() -> None:
    # A lease lost during planning must stop BEFORE the run stage (no run_pair).
    planner, runner = _FakePlanner(), _FakeRunner()
    service = CharacterEncounterService(
        planner=planner, runner=runner, encounter_repository=None,
    )

    with pytest.raises(EncounterPairLeaseLost):
        await service.step_pair(_Rel(), now=NOW, abort=_lost_hook())

    assert planner.calls == 1        # planning happened
    assert runner.abort_seen == []   # run stage was never entered


# --------------------------------------------------------------------------- #
# Runner: run_pair aborts BEFORE claiming each subsequent encounter
# --------------------------------------------------------------------------- #

@dataclass
class _Enc:
    id: str
    scheduled_for: datetime
    status: str = "planned"


@dataclass
class _RunResult:
    id: str
    status: str = "completed"


class _FakeEncounterRepo:
    def __init__(self, encounters: list[_Enc]) -> None:
        self._encounters = encounters

    async def list_for_relationship(self, relationship_id: str, *, limit: int):
        return list(self._encounters)


class _AbortStopRunner(CharacterEncounterRunner):
    """Runner whose ``run`` is stubbed cheap; the pair lease is 'lost' the moment
    the first encounter finishes, so the second must never be claimed."""

    def __init__(self, encounters: list[_Enc]) -> None:
        super().__init__(
            encounter_repository=_FakeEncounterRepo(encounters),
            character_repository=None,
            memory_writer=None,
            relationship_service=None,
            provider=None,
        )
        self.ran: list[str] = []
        self.lost = False

    async def _background_gate_allows(self, encounter, *, gate=None):  # noqa: ANN001
        return True

    async def run(self, encounter_id, *, now=None, abort=None):  # noqa: ANN001
        # Mirror the real run(): honour the abort at the top before doing work.
        if abort is not None:
            abort()
        self.ran.append(encounter_id)
        # Simulate the pair lease being stolen DURING this encounter's execution.
        self.lost = True
        return _RunResult(encounter_id)


async def test_run_pair_stops_claiming_after_lease_lost() -> None:
    encounters = [
        _Enc("enc1", NOW - timedelta(seconds=2)),
        _Enc("enc2", NOW - timedelta(seconds=1)),
    ]
    runner = _AbortStopRunner(encounters)

    def _hook() -> None:
        if runner.lost:
            raise EncounterPairLeaseLost("a", "b")

    with pytest.raises(EncounterPairLeaseLost):
        await runner.run_pair(_Rel(), now=NOW, abort=_hook)

    # Only the first encounter ran; the loop aborted before claiming enc2 — the
    # reclaiming worker owns it (no double-run of the remaining encounters).
    assert runner.ran == ["enc1"]


async def test_run_pair_runs_all_when_lease_held() -> None:
    encounters = [
        _Enc("enc1", NOW - timedelta(seconds=2)),
        _Enc("enc2", NOW - timedelta(seconds=1)),
    ]
    runner = _AbortStopRunner(encounters)
    runner.lost = False

    # An abort hook that never trips (lease held) lets the whole batch run. Reset
    # the simulated loss so it stays held across both encounters.
    def _never() -> None:
        return None

    # Neutralise the mid-run loss for this held-lease scenario.
    async def _run(encounter_id, *, now=None, abort=None):  # noqa: ANN001
        if abort is not None:
            abort()
        runner.ran.append(encounter_id)
        return _RunResult(encounter_id)

    runner.run = _run  # type: ignore[method-assign]

    completed, _failed = await runner.run_pair(_Rel(), now=NOW, abort=_never)
    assert runner.ran == ["enc1", "enc2"]
    assert completed == ["enc1", "enc2"]
