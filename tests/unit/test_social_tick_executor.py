"""SocialTickExecutor — the extracted global-social tick body (P3-A).

Proves maintenance (demo reaper always, freeze sweep only when flagged),
follow-up release, and social advancement (encounters run-always / plan-flagged,
peer knowledge flagged, persona dream) reproduce the scheduler's behaviour
byte-for-byte, including the SocialTickOutcome the caller reads to advance its
throttle timestamps, per-step fail-soft isolation, and the step_timer names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.character_encounter_service import (
    EncounterTickResult,
)
from kokoro_link.application.services.character_social_knowledge_service import (
    PeerKnowledgeTickResult,
)
from kokoro_link.application.services.social_tick_executor import (
    SocialTickExecutor,
    SocialTickOutcome,
)
from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class _AllowGate:
    async def allows(self, character, activity_class) -> bool:  # noqa: ANN001
        return True

    def any_operator_allows(self, activity_class) -> bool:  # noqa: ANN001
        return True


@dataclass
class _DemoResult:
    scanned_characters: int = 0
    expired_characters: int = 0
    deleted_characters: int = 0
    released_accounts: int = 0
    delete_failures: int = 0
    release_failures: int = 0


class _DemoReaper:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    async def run_once(self, *, now):  # noqa: ANN001
        self.calls.append(now)
        return _DemoResult()


class _FreezeReaper:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    async def run_once(self, *, now):  # noqa: ANN001
        self.calls.append(now)


class _Encounters:
    def __init__(self, *, crash: bool = False) -> None:
        self.run_nows: list[datetime] = []
        self.plan_nows: list[datetime] = []
        self._crash = crash

    async def run_pending(self, *, now, gate):  # noqa: ANN001
        self.run_nows.append(now)
        if self._crash:
            raise RuntimeError("run boom")
        return EncounterTickResult()

    async def plan_pending(self, *, now, gate):  # noqa: ANN001
        self.plan_nows.append(now)
        if self._crash:
            raise RuntimeError("plan boom")
        return EncounterTickResult()


class _Peer:
    def __init__(self, *, crash: bool = False) -> None:
        self.calls = 0
        self._crash = crash

    async def consolidate_due(self, *, gate):  # noqa: ANN001
        self.calls += 1
        if self._crash:
            raise RuntimeError("peer boom")
        return PeerKnowledgeTickResult()


def _step_timer_recorder(names: list[str]):
    async def step_timer(name, coro):
        names.append(name)
        return await coro

    return step_timer


# --------------------------------------------------------------------------- #
# run_maintenance
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_maintenance_runs_demo_always_and_freeze_only_when_flagged() -> None:
    demo = _DemoReaper()
    freeze = _FreezeReaper()
    executor = SocialTickExecutor(
        demo_account_reaper=demo, character_freeze_reaper=freeze,
    )
    names: list[str] = []

    await executor.run_maintenance(
        now=NOW, sweep_freeze=False, step_timer=_step_timer_recorder(names),
    )
    assert demo.calls == [NOW]
    assert freeze.calls == []  # not swept
    # Both steps are still timed (freeze step is a no-op, not skipped).
    assert names == ["demo_reaper", "freeze_sweep"]

    await executor.run_maintenance(now=NOW, sweep_freeze=True)
    assert demo.calls == [NOW, NOW]
    assert freeze.calls == [NOW]  # swept this time


@pytest.mark.asyncio
async def test_maintenance_demo_reaper_crash_is_isolated() -> None:
    class _Boom:
        async def run_once(self, *, now):  # noqa: ANN001
            raise RuntimeError("demo boom")

    freeze = _FreezeReaper()
    executor = SocialTickExecutor(
        demo_account_reaper=_Boom(), character_freeze_reaper=freeze,
    )
    # Must not raise; freeze sweep still runs.
    await executor.run_maintenance(now=NOW, sweep_freeze=True)
    assert freeze.calls == [NOW]


@pytest.mark.asyncio
async def test_setters_update_live_reapers() -> None:
    executor = SocialTickExecutor()
    demo = _DemoReaper()
    freeze = _FreezeReaper()
    executor.set_demo_account_reaper(demo)
    executor.set_character_freeze_reaper(freeze)
    await executor.run_maintenance(now=NOW, sweep_freeze=True)
    assert demo.calls == [NOW]
    assert freeze.calls == [NOW]


# --------------------------------------------------------------------------- #
# run_pending_follow_ups
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pending_follow_ups_timed_and_fail_soft() -> None:
    class _Dispatcher:
        def __init__(self, *, crash: bool) -> None:
            self.calls: list[datetime] = []
            self._crash = crash

        async def tick(self, *, now):  # noqa: ANN001
            self.calls.append(now)
            if self._crash:
                raise RuntimeError("follow-up boom")

    dispatcher = _Dispatcher(crash=False)
    executor = SocialTickExecutor(pending_follow_up_dispatcher=dispatcher)
    names: list[str] = []
    await executor.run_pending_follow_ups(
        now=NOW, step_timer=_step_timer_recorder(names),
    )
    assert dispatcher.calls == [NOW]
    assert names == ["pending_follow_ups"]

    crashing = _Dispatcher(crash=True)
    executor = SocialTickExecutor(pending_follow_up_dispatcher=crashing)
    await executor.run_pending_follow_ups(now=NOW)  # must not raise
    assert crashing.calls == [NOW]


# --------------------------------------------------------------------------- #
# run_social
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_social_runs_encounters_always_plan_flagged_and_reports_outcome() -> None:
    encounters = _Encounters()
    peer = _Peer()
    executor = SocialTickExecutor(
        character_encounter_service=encounters,
        character_social_knowledge_service=peer,
    )
    names: list[str] = []

    # plan off, peer off → run_pending still fires, no plan/peer, no timestamps.
    outcome = await executor.run_social(
        now=NOW,
        gate=_AllowGate(),
        plan_encounters=False,
        consolidate_peer=False,
        step_timer=_step_timer_recorder(names),
    )
    assert encounters.run_nows == [NOW]
    assert encounters.plan_nows == []
    assert peer.calls == 0
    assert outcome == SocialTickOutcome(
        encounter_planned=False, peer_consolidated=False,
    )
    assert names == ["encounters", "peer_knowledge", "persona_dream"]

    # plan on, peer on → both fire, both outcome flags true.
    outcome = await executor.run_social(
        now=NOW,
        gate=_AllowGate(),
        plan_encounters=True,
        consolidate_peer=True,
    )
    assert encounters.plan_nows == [NOW]
    assert peer.calls == 1
    assert outcome == SocialTickOutcome(
        encounter_planned=True, peer_consolidated=True,
    )


@pytest.mark.asyncio
async def test_social_plan_crash_leaves_outcome_false() -> None:
    # A crashing plan_pending must not report success — so the caller does NOT
    # advance _last_encounter_plan_at (it will retry next tick), matching today.
    encounters = _Encounters(crash=True)
    executor = SocialTickExecutor(character_encounter_service=encounters)
    outcome = await executor.run_social(
        now=NOW, gate=_AllowGate(), plan_encounters=True, consolidate_peer=False,
    )
    assert encounters.run_nows == [NOW]
    assert encounters.plan_nows == [NOW]
    assert outcome.encounter_planned is False


@pytest.mark.asyncio
async def test_social_peer_crash_leaves_outcome_false() -> None:
    peer = _Peer(crash=True)
    executor = SocialTickExecutor(character_social_knowledge_service=peer)
    outcome = await executor.run_social(
        now=NOW, gate=_AllowGate(), plan_encounters=False, consolidate_peer=True,
    )
    assert peer.calls == 1
    assert outcome.peer_consolidated is False


@pytest.mark.asyncio
async def test_social_encounter_run_crash_does_not_block_plan() -> None:
    # run_pending crashing must not stop plan_pending — the scheduler ran plan
    # regardless of run outcome.
    class _RunCrashes:
        def __init__(self) -> None:
            self.plan_nows: list[datetime] = []

        async def run_pending(self, *, now, gate):  # noqa: ANN001
            raise RuntimeError("run boom")

        async def plan_pending(self, *, now, gate):  # noqa: ANN001
            self.plan_nows.append(now)
            return EncounterTickResult()

    service = _RunCrashes()
    executor = SocialTickExecutor(character_encounter_service=service)
    outcome = await executor.run_social(
        now=NOW, gate=_AllowGate(), plan_encounters=True, consolidate_peer=False,
    )
    assert service.plan_nows == [NOW]
    assert outcome.encounter_planned is True


# --------------------------------------------------------------------------- #
# persona dream (background-character loader semantics)
# --------------------------------------------------------------------------- #


@dataclass
class _Char:
    id: str = "c1"
    user_id: str = "op1"
    frozen: bool = False
    subscription_locked: bool = False


class _DreamRepo:
    def __init__(self, pairs) -> None:  # noqa: ANN001
        self._pairs = pairs

    async def list_characters_with_pending(self):
        return self._pairs


@dataclass
class _DreamService:
    should: bool = True
    should_calls: list[tuple] = field(default_factory=list)
    run_calls: list[tuple] = field(default_factory=list)

    async def should_run_now(self, character_id, operator_id, *, now):  # noqa: ANN001
        self.should_calls.append((character_id, operator_id, now))
        return self.should

    async def run_consolidation(self, character_id, operator_id, *, now):  # noqa: ANN001
        self.run_calls.append((character_id, operator_id, now))


class _CharRepo:
    def __init__(self, char, *, crash: bool = False) -> None:  # noqa: ANN001
        self._char = char
        self._crash = crash

    async def get(self, character_id):  # noqa: ANN001
        if self._crash:
            raise RuntimeError("lookup boom")
        return self._char


@pytest.mark.asyncio
async def test_persona_dream_runs_for_eligible_pair() -> None:
    char = _Char()
    dream = _DreamService()
    executor = SocialTickExecutor(
        persona_dream_service=dream,
        persona_dream_repository=_DreamRepo([(char.id, char.user_id)]),
        character_repository=_CharRepo(char),
    )
    await executor.run_social(
        now=NOW, gate=_AllowGate(), plan_encounters=False, consolidate_peer=False,
    )
    assert dream.run_calls == [(char.id, char.user_id, NOW)]


@pytest.mark.asyncio
async def test_persona_dream_lookup_failure_fails_open() -> None:
    # Transient repo error → skip only the cost gate, still run the pass.
    char = _Char()
    dream = _DreamService()
    executor = SocialTickExecutor(
        persona_dream_service=dream,
        persona_dream_repository=_DreamRepo([(char.id, char.user_id)]),
        character_repository=_CharRepo(char, crash=True),
    )
    await executor.run_social(
        now=NOW, gate=_AllowGate(), plan_encounters=False, consolidate_peer=False,
    )
    assert dream.run_calls == [(char.id, char.user_id, NOW)]


@pytest.mark.asyncio
async def test_persona_dream_skips_frozen_character() -> None:
    char = _Char(frozen=True)
    dream = _DreamService()
    executor = SocialTickExecutor(
        persona_dream_service=dream,
        persona_dream_repository=_DreamRepo([(char.id, char.user_id)]),
        character_repository=_CharRepo(char),
    )
    await executor.run_social(
        now=NOW, gate=_AllowGate(), plan_encounters=False, consolidate_peer=False,
    )
    assert dream.run_calls == []


@pytest.mark.asyncio
async def test_persona_dream_respects_gate_deny() -> None:
    class _DenyDream:
        async def allows(self, character, activity_class) -> bool:  # noqa: ANN001
            return False

        def any_operator_allows(self, activity_class) -> bool:  # noqa: ANN001
            return True

    char = _Char()
    dream = _DreamService()
    executor = SocialTickExecutor(
        persona_dream_service=dream,
        persona_dream_repository=_DreamRepo([(char.id, char.user_id)]),
        character_repository=_CharRepo(char),
    )
    await executor.run_social(
        now=NOW, gate=_DenyDream(), plan_encounters=False, consolidate_peer=False,
    )
    assert dream.run_calls == []
