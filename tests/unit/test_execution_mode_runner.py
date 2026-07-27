"""Distributed execution runner + worker mode flip (HOSTED_CORE_SCALING §13 P3-C).

Covers: the worker flips dry-run ↔ execute on the ownership mode (fail-closed to
dry-run on a read error); a character_tick executes and completes ``executed:true``;
collected beats dispatch INLINE on the ARC_BEAT path; the heartbeat abort stops
remaining steps and the job is NOT completed; the outcome-classification matrix
(ReadTimeout → outcome_unknown complete, ConnectTimeout/other → fail-retry); the
character_warmup handler runs rest-recovery + ensure_window; social cadence flags
derive from the durable cursor store.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kokoro_link.application.services.background_shadow_coordinator import (
    CHARACTER_TICK_KIND,
    CHARACTER_WARMUP_KIND,
    SOCIAL_TICK_KIND,
)
from kokoro_link.application.services.beat_due_checker import BeatScanResult
from kokoro_link.application.services.character_tick_executor import (
    CharacterTickExecutor,
)
from kokoro_link.application.services.execution_mode_runner import (
    ExecutionModeRunner,
)
from kokoro_link.application.services.runtime_activity_gate import (
    RuntimeActivityGateService,
)
from kokoro_link.application.services.social_tick_executor import (
    SocialTickExecutor,
)
from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.contracts.background_jobs import (
    BackgroundJobSpec,
    ClaimedJob,
    JobOutcome,
    JobStatus,
)
from kokoro_link.contracts.execution_mode import MODE_DISTRIBUTED
from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    current_generation_trigger,
)
from kokoro_link.contracts.image_provider import ImageTimeoutError
from kokoro_link.contracts.video_provider import VideoTimeoutError
from kokoro_link.domain.entities.character import Character, CharacterState
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorCursor,
    InMemoryBackgroundJobQueue,
    InMemoryRuntimeOwnership,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = int(NOW.timestamp()) // 300


def _character(cid: str = "char-1", *, proactive: bool = True) -> Character:
    character = Character.create(
        name="Airi", summary="", user_id="op-1", personality=[], interests=[],
        speaking_style="", boundaries=[], proactive_enabled=proactive,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=NOW,
        ),
    )
    return replace(character, id=cid, created_at=NOW - timedelta(days=30))


class _CharRepo:
    def __init__(self, characters: list[Character]) -> None:
        self._characters = characters

    async def get(self, character_id: str):
        return next((c for c in self._characters if c.id == character_id), None)

    async def list_active(self):
        return [c for c in self._characters if not c.frozen]

    async def list(self):
        return list(self._characters)


@dataclass
class _RecordingDispatcher:
    calls: list[tuple[str, ProactiveTrigger, str | None]] = field(
        default_factory=list,
    )
    generation_triggers: list[GenerationTrigger] = field(default_factory=list)

    async def evaluate(self, *, character_id, trigger, now, logical_slot=None):
        self.calls.append((character_id, trigger, logical_slot))
        self.generation_triggers.append(current_generation_trigger())


def _gate_service(characters: list[Character]) -> RuntimeActivityGateService:
    return RuntimeActivityGateService(
        resolver=PermissiveAccountRuntimeProfileResolver(),
        character_repository=_CharRepo(characters),
    )


def _char_executor(dispatcher, **kw) -> CharacterTickExecutor:
    return CharacterTickExecutor(dispatcher=dispatcher, **kw)


def _spec(kind: str, key: str, *, character_id: str | None) -> BackgroundJobSpec:
    return BackgroundJobSpec(
        kind=kind, idempotency_key=key, due_at=NOW, fencing_epoch=0,
        character_id=character_id, payload={"bucket": BUCKET},
    )


def _claimed(kind: str, key: str, *, character_id: str | None) -> ClaimedJob:
    return ClaimedJob(
        id=f"job-{key}", kind=kind, attempt_count=1, max_attempts=5,
        fencing_epoch=0, idempotency_key=key, priority=3, due_at=NOW,
        payload_version=1, payload={"bucket": BUCKET}, lease_owner="w1",
        lease_until=NOW + timedelta(seconds=120), character_id=character_id,
    )


# --------------------------------------------------------------------------- #
# Worker mode flip
# --------------------------------------------------------------------------- #

async def _build_worker(ownership, *, dispatcher=None, characters=None):
    from kokoro_link.application.services.background_shadow_worker import (
        ShadowDryRunWorker,
    )

    characters = characters or [_character()]
    dispatcher = dispatcher or _RecordingDispatcher()
    queue = InMemoryBackgroundJobQueue()
    char_exec = _char_executor(dispatcher)
    social_exec = SocialTickExecutor(character_repository=_CharRepo(characters))
    runner = ExecutionModeRunner(
        queue=queue,
        character_repository=_CharRepo(characters),
        character_tick_executor=char_exec,
        social_tick_executor=social_exec,
        gate_service=_gate_service(characters),
        cursor=InMemoryBackgroundCoordinatorCursor(),
        bucket_seconds=300,
    )
    worker = ShadowDryRunWorker(
        queue=queue, character_repository=_CharRepo(characters),
        worker_id="w1", dry_run_hold_seconds=0.0,
    )
    worker.enable_execution(
        runtime_ownership=ownership, execution_runner=runner,
    )
    return worker, queue, dispatcher


async def test_embedded_mode_dry_runs() -> None:
    ownership = InMemoryRuntimeOwnership()  # absent → embedded
    worker, queue, dispatcher = await _build_worker(ownership)
    job_id = await queue.enqueue(_spec(CHARACTER_TICK_KIND, "k", character_id="char-1"), now=NOW)
    await worker.run_once(now=NOW)
    job = await queue.get(job_id)
    assert job.status == JobStatus.DONE
    assert job.outcome["dry_run"] is True
    assert dispatcher.calls == []  # no real dispatch in dry-run


async def test_distributed_mode_executes_and_completes() -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip(MODE_DISTRIBUTED, 0, now=NOW)
    worker, queue, dispatcher = await _build_worker(ownership)
    job_id = await queue.enqueue(_spec(CHARACTER_TICK_KIND, "k", character_id="char-1"), now=NOW)
    await worker.run_once(now=NOW)
    job = await queue.get(job_id)
    assert job.status == JobStatus.DONE
    assert job.outcome["executed"] is True
    assert job.outcome["kind"] == CHARACTER_TICK_KIND
    # proactive dispatch ran on the real path (gate multiplier 1 → allowed).
    assert (("char-1", ProactiveTrigger.TICK, str(BUCKET))) in dispatcher.calls
    assert dispatcher.generation_triggers == [GenerationTrigger.BACKGROUND]


async def test_ownership_read_error_fails_closed_to_dry_run() -> None:
    class _ExplodingOwnership:
        async def get(self):
            raise RuntimeError("db down")

        async def flip(self, *a, **k):  # pragma: no cover
            return None

    worker, queue, dispatcher = await _build_worker(_ExplodingOwnership())
    job_id = await queue.enqueue(_spec(CHARACTER_TICK_KIND, "k", character_id="char-1"), now=NOW)
    await worker.run_once(now=NOW)
    job = await queue.get(job_id)
    assert job.outcome["dry_run"] is True  # fail-closed to dry-run
    assert dispatcher.calls == []


async def test_frozen_character_completes_not_executed() -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip(MODE_DISTRIBUTED, 0, now=NOW)
    frozen = replace(_character("char-f"), frozen=True)
    worker, queue, _dispatcher = await _build_worker(
        ownership, characters=[frozen],
    )
    job_id = await queue.enqueue(_spec(CHARACTER_TICK_KIND, "k", character_id="char-f"), now=NOW)
    await worker.run_once(now=NOW)
    job = await queue.get(job_id)
    assert job.status == JobStatus.DONE
    assert job.outcome["executed"] is False
    assert job.outcome["skip_reason"] == "frozen"


# --------------------------------------------------------------------------- #
# Inline beat dispatch
# --------------------------------------------------------------------------- #

class _BeatChecker:
    async def scan(self, character, *, now=None):
        return BeatScanResult(attempted_beat_id="beat-1", should_notify=True)


async def test_collected_beats_dispatch_inline_on_arc_beat_path() -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip(MODE_DISTRIBUTED, 0, now=NOW)
    characters = [_character()]
    dispatcher = _RecordingDispatcher()
    queue = InMemoryBackgroundJobQueue()
    char_exec = _char_executor(dispatcher, beat_due_checker=_BeatChecker())
    runner = ExecutionModeRunner(
        queue=queue, character_repository=_CharRepo(characters),
        character_tick_executor=char_exec,
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=_gate_service(characters),
        cursor=InMemoryBackgroundCoordinatorCursor(), bucket_seconds=300,
    )
    disposition = await runner.execute(
        _claimed(CHARACTER_TICK_KIND, "k", character_id="char-1"),
        would_run=True, skip_reason=None, now=NOW, worker_id="w1",
        lease_seconds=120,
    )
    assert disposition.action == "complete"
    assert disposition.outcome["beats_dispatched"] == 1
    arc_calls = [c for c in dispatcher.calls if c[1] is ProactiveTrigger.ARC_BEAT]
    assert arc_calls == [("char-1", ProactiveTrigger.ARC_BEAT, str(BUCKET))]


# --------------------------------------------------------------------------- #
# Phase 5 per-kind dispatch
# --------------------------------------------------------------------------- #

async def test_per_kind_job_routes_to_character_kind_handler() -> None:
    from kokoro_link.application.services.due_job_handlers import (
        CharacterKindHandler,
    )
    from kokoro_link.application.services.due_job_scheduler import (
        NextDueCalculator,
    )
    from kokoro_link.contracts.due_jobs import PROACTIVE_EVALUATE_KIND

    ownership = InMemoryRuntimeOwnership()
    await ownership.flip(MODE_DISTRIBUTED, 0, now=NOW)
    characters = [_character()]
    dispatcher = _RecordingDispatcher()
    queue = InMemoryBackgroundJobQueue()  # lease=None → fencing disabled
    char_exec = _char_executor(dispatcher)
    handler = CharacterKindHandler(
        executor=char_exec,
        queue=queue,
        next_due_calculator=NextDueCalculator(
            resolver=PermissiveAccountRuntimeProfileResolver(),
        ),
        epoch_provider=lambda: 0,
    )
    runner = ExecutionModeRunner(
        queue=queue, character_repository=_CharRepo(characters),
        character_tick_executor=char_exec,
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=_gate_service(characters),
        cursor=InMemoryBackgroundCoordinatorCursor(), bucket_seconds=300,
        character_kind_handler=handler,
    )
    disposition = await runner.execute(
        _claimed(PROACTIVE_EVALUATE_KIND, "k", character_id="char-1"),
        would_run=True, skip_reason=None, now=NOW, worker_id="w1",
        lease_seconds=120,
    )
    assert disposition.action == "complete"
    assert disposition.outcome["executed"] is True
    assert disposition.outcome["kind"] == PROACTIVE_EVALUATE_KIND
    # The bound step ran the proactive dispatch on the TICK path with the bucket
    # slot (reusing the same visible-slot dedup the whole-tick body uses).
    assert ("char-1", ProactiveTrigger.TICK, str(BUCKET)) in dispatcher.calls
    # The self-chain advanced: a next proactive_evaluate is queued.
    assert (PROACTIVE_EVALUATE_KIND, "char-1") in await queue.active_chain_keys()


async def test_per_kind_job_unwired_handler_completes_noop() -> None:
    from kokoro_link.contracts.due_jobs import PROACTIVE_EVALUATE_KIND

    ownership = InMemoryRuntimeOwnership()
    await ownership.flip(MODE_DISTRIBUTED, 0, now=NOW)
    characters = [_character()]
    queue = InMemoryBackgroundJobQueue()
    runner = ExecutionModeRunner(
        queue=queue, character_repository=_CharRepo(characters),
        character_tick_executor=_char_executor(_RecordingDispatcher()),
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=_gate_service(characters),
        cursor=InMemoryBackgroundCoordinatorCursor(), bucket_seconds=300,
        # No handler wired.
    )
    disposition = await runner.execute(
        _claimed(PROACTIVE_EVALUATE_KIND, "k", character_id="char-1"),
        would_run=True, skip_reason=None, now=NOW, worker_id="w1",
        lease_seconds=120,
    )
    assert disposition.action == "complete"
    assert disposition.outcome["reason"] == "handler_unwired"


# --------------------------------------------------------------------------- #
# Heartbeat abort
# --------------------------------------------------------------------------- #

async def test_heartbeat_abort_stops_steps_and_does_not_complete() -> None:
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip(MODE_DISTRIBUTED, 0, now=NOW)
    characters = [_character()]
    renew_reached = asyncio.Event()
    steps: list[str] = []

    class _AbortQueue(InMemoryBackgroundJobQueue):
        async def extend_lease(self, *a, **k):
            renew_reached.set()
            return False  # lease lost → abort

    class _BlockingRest:
        async def refresh(self, character, *, now=None):
            steps.append("rest")
            await renew_reached.wait()  # block until heartbeat fired

    class _RecordingSchedule:
        async def ensure_window(self, character):
            steps.append("ensure_window")

    queue = _AbortQueue()
    char_exec = _char_executor(
        _RecordingDispatcher(),
        rest_recovery_refresher=_BlockingRest(),
        schedule_service=_RecordingSchedule(),
    )
    runner = ExecutionModeRunner(
        queue=queue, character_repository=_CharRepo(characters),
        character_tick_executor=char_exec,
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=_gate_service(characters),
        cursor=InMemoryBackgroundCoordinatorCursor(), bucket_seconds=300,
        heartbeat_interval_seconds=0.0,
    )
    from kokoro_link.application.services.background_shadow_worker import (
        ShadowDryRunWorker,
    )
    worker = ShadowDryRunWorker(
        queue=queue, character_repository=_CharRepo(characters), worker_id="w1",
        dry_run_hold_seconds=0.0,
    )
    worker.enable_execution(runtime_ownership=ownership, execution_runner=runner)
    job_id = await queue.enqueue(_spec(CHARACTER_TICK_KIND, "k", character_id="char-1"), now=NOW)
    await worker.run_once()  # unpinned now → real clock for heartbeat
    # rest ran, but abort stopped the tick before ensure_window.
    assert steps == ["rest"]
    job = await queue.get(job_id)
    assert job.status != JobStatus.DONE  # NOT completed — reclaimable


# --------------------------------------------------------------------------- #
# Outcome classification
# --------------------------------------------------------------------------- #

class _RaisingGate:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def prepare_distributed(self, *a, **k):
        raise self._exc


async def _classify(exc: BaseException):
    characters = [_character()]
    queue = InMemoryBackgroundJobQueue()
    runner = ExecutionModeRunner(
        queue=queue, character_repository=_CharRepo(characters),
        character_tick_executor=_char_executor(_RecordingDispatcher()),
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=_RaisingGate(exc),
        cursor=InMemoryBackgroundCoordinatorCursor(), bucket_seconds=300,
    )
    return await runner.execute(
        _claimed(CHARACTER_TICK_KIND, "k", character_id="char-1"),
        would_run=True, skip_reason=None, now=NOW, worker_id="w1",
        lease_seconds=120,
    )


@pytest.mark.parametrize("exc", [
    httpx.ReadTimeout("read"),
    ImageTimeoutError("img"),
    VideoTimeoutError("vid"),
])
async def test_ambiguous_timeout_completes_outcome_unknown(exc) -> None:
    disposition = await _classify(exc)
    assert disposition.action == "complete"
    assert disposition.outcome["outcome_unknown"] is True
    assert disposition.outcome["error_class"] == type(exc).__name__


async def test_connect_timeout_fails_retry() -> None:
    disposition = await _classify(httpx.ConnectTimeout("connect"))
    assert disposition.action == "fail_retry"
    assert isinstance(disposition.error, httpx.ConnectTimeout)


async def test_other_exception_fails_retry() -> None:
    disposition = await _classify(ValueError("boom"))
    assert disposition.action == "fail_retry"
    assert isinstance(disposition.error, ValueError)


# --------------------------------------------------------------------------- #
# Warmup handler
# --------------------------------------------------------------------------- #

async def test_character_warmup_runs_rest_and_ensure_window() -> None:
    calls: list[str] = []

    class _Rest:
        async def refresh(self, character, *, now=None):
            calls.append("rest")

    class _Schedule:
        async def ensure_window(self, character):
            calls.append("ensure_window")

    characters = [_character()]
    queue = InMemoryBackgroundJobQueue()
    char_exec = _char_executor(
        _RecordingDispatcher(),
        rest_recovery_refresher=_Rest(), schedule_service=_Schedule(),
    )
    runner = ExecutionModeRunner(
        queue=queue, character_repository=_CharRepo(characters),
        character_tick_executor=char_exec,
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=_gate_service(characters),
        cursor=InMemoryBackgroundCoordinatorCursor(), bucket_seconds=300,
    )
    disposition = await runner.execute(
        _claimed(CHARACTER_WARMUP_KIND, "warm:char-1", character_id="char-1"),
        would_run=True, skip_reason=None, now=NOW, worker_id="w1",
        lease_seconds=120,
    )
    assert disposition.action == "complete"
    assert disposition.outcome["kind"] == CHARACTER_WARMUP_KIND
    assert calls == ["rest", "ensure_window"]


# --------------------------------------------------------------------------- #
# Social cadence from durable cursor
# --------------------------------------------------------------------------- #

async def test_social_cadence_first_run_due_then_throttled() -> None:
    freeze_calls: list[datetime] = []

    class _FreezeReaper:
        async def run_once(self, *, now):
            freeze_calls.append(now)

    characters = [_character()]
    queue = InMemoryBackgroundJobQueue()
    cursor = InMemoryBackgroundCoordinatorCursor()
    social_exec = SocialTickExecutor(
        character_freeze_reaper=_FreezeReaper(),
        character_repository=_CharRepo(characters),
    )
    runner = ExecutionModeRunner(
        queue=queue, character_repository=_CharRepo(characters),
        character_tick_executor=_char_executor(_RecordingDispatcher()),
        social_tick_executor=social_exec,
        gate_service=_gate_service(characters),
        cursor=cursor, bucket_seconds=300,
    )
    spec = _claimed(SOCIAL_TICK_KIND, "social:1", character_id=None)
    first = await runner.execute(
        spec, would_run=True, skip_reason=None, now=NOW, worker_id="w1",
        lease_seconds=120,
    )
    assert first.outcome["sweep_freeze"] is True
    assert len(freeze_calls) == 1  # first run: no cursor → due
    # A second run 10 minutes later is throttled (freeze interval is hourly).
    soon = NOW + timedelta(minutes=10)
    second = await runner.execute(
        spec, would_run=True, skip_reason=None, now=soon, worker_id="w1",
        lease_seconds=120,
    )
    assert second.outcome["sweep_freeze"] is False
    assert len(freeze_calls) == 1  # not swept again


async def test_heartbeat_exception_abandons_without_leaking_or_completing() -> None:
    marker: list[str] = []

    class _RaisingHeartbeatQueue(InMemoryBackgroundJobQueue):
        async def extend_lease(self, *args, **kwargs):
            marker.append("heartbeat")
            raise RuntimeError("transient renewal failure")

    class _Rest:
        async def refresh(self, character, *, now=None):
            marker.append("body")
            await asyncio.sleep(0.02)

    class _Schedule:
        async def ensure_window(self, character):
            marker.append("ensure")

    characters = [_character()]
    ownership = InMemoryRuntimeOwnership()
    await ownership.flip(MODE_DISTRIBUTED, 0, now=NOW)
    queue = _RaisingHeartbeatQueue()
    runner = ExecutionModeRunner(
        queue=queue, character_repository=_CharRepo(characters),
        character_tick_executor=_char_executor(
            _RecordingDispatcher(), rest_recovery_refresher=_Rest(),
            schedule_service=_Schedule(),
        ),
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=_gate_service(characters),
        cursor=InMemoryBackgroundCoordinatorCursor(), bucket_seconds=300,
        heartbeat_interval_seconds=0.001,
        runtime_ownership=ownership,
    )
    disposition = await runner.execute(
        _claimed(CHARACTER_WARMUP_KIND, "heartbeat-error", character_id="char-1"),
        would_run=True, skip_reason=None, now=NOW, worker_id="w1",
        lease_seconds=120,
    )
    assert marker[:2] == ["body", "heartbeat"]
    assert disposition.action == "abandon"
    assert disposition.outcome is None


async def test_warmup_abort_before_recovery_skips_all_steps() -> None:
    calls: list[str] = []

    class _Rest:
        async def refresh(self, character, *, now=None):
            calls.append("rest")

    class _Schedule:
        async def ensure_window(self, character):
            calls.append("ensure")

    executor = _char_executor(
        _RecordingDispatcher(), rest_recovery_refresher=_Rest(),
        schedule_service=_Schedule(),
    )
    await executor.run_warmup(
        _character(), now=NOW, abort_check=lambda: True,
    )
    assert calls == []


async def test_warmup_abort_after_recovery_skips_schedule() -> None:
    calls: list[str] = []
    aborted = False

    class _Rest:
        async def refresh(self, character, *, now=None):
            nonlocal aborted
            calls.append("rest")
            aborted = True

    class _Schedule:
        async def ensure_window(self, character):
            calls.append("ensure")

    executor = _char_executor(
        _RecordingDispatcher(), rest_recovery_refresher=_Rest(),
        schedule_service=_Schedule(),
    )
    await executor.run_warmup(
        _character(), now=NOW, abort_check=lambda: aborted,
    )
    assert calls == ["rest"]


async def test_cadence_reservation_is_at_most_once_and_released_on_failure() -> None:
    cursor = InMemoryBackgroundCoordinatorCursor()
    assert await cursor.claim_due("cadence", "worker-a", now=NOW, interval_seconds=60)
    assert not await cursor.claim_due(
        "cadence", "worker-b", now=NOW, interval_seconds=60,
    )
    assert await cursor.release_due("cadence", "worker-a")
    assert await cursor.claim_due(
        "cadence", "worker-b", now=NOW, interval_seconds=60,
    )
    assert await cursor.complete_due("cadence", "worker-b", completed_at=NOW)
    assert not await cursor.claim_due(
        "cadence", "worker-a", now=NOW + timedelta(seconds=30),
        interval_seconds=60,
    )
    assert await cursor.claim_due(
        "cadence", "worker-a", now=NOW + timedelta(seconds=61),
        interval_seconds=60,
    )


# --------------------------------------------------------------------------- #
# Social per-kind routing (§13 split)
# --------------------------------------------------------------------------- #

from kokoro_link.application.services.social_due_job_handlers import (  # noqa: E402
    SocialHandlerResult,
)
from kokoro_link.contracts.due_jobs import (  # noqa: E402
    ENCOUNTER_TICK_KIND,
    PERSONA_DREAM_KIND,
)


@dataclass
class _RecordingSocialHandler:
    character_calls: list[tuple[str, str]] = field(default_factory=list)
    pair_calls: list[tuple[str, str, str]] = field(default_factory=list)
    abandon: bool = False

    async def handle_character(self, character, kind, *, now, last_due, fencing_epoch):
        self.character_calls.append((character.id, kind))
        return SocialHandlerResult(kind, executed=True, next_enqueued=True)

    async def handle_pair(
        self, char_a, char_b, relationship, *, now, last_due, fencing_epoch, gate,
    ):
        self.pair_calls.append((char_a.id, char_b.id, relationship.id))
        assert gate is not None  # the worker prepares the both-side gate
        return SocialHandlerResult(
            ENCOUNTER_TICK_KIND,
            executed=not self.abandon,
            next_enqueued=not self.abandon,
            abandoned=self.abandon,
        )


class _RelRepo:
    def __init__(self, rows) -> None:
        self._rows = rows

    async def get(self, relationship_id: str):
        return next((r for r in self._rows if r.id == relationship_id), None)


def _social_runner(handler, characters, relationships):
    return ExecutionModeRunner(
        queue=InMemoryBackgroundJobQueue(),
        character_repository=_CharRepo(characters),
        character_tick_executor=_char_executor(_RecordingDispatcher()),
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=_gate_service(characters),
        cursor=InMemoryBackgroundCoordinatorCursor(),
        bucket_seconds=300,
        social_kind_handler=handler,
        character_relationship_repository=_RelRepo(relationships),
    )


async def test_persona_dream_job_routes_to_social_handler() -> None:
    handler = _RecordingSocialHandler()
    runner = _social_runner(handler, [_character("c1")], [])
    job = _claimed(PERSONA_DREAM_KIND, "k", character_id="c1")
    disposition = await runner.execute(
        job, would_run=True, skip_reason=None, now=NOW,
        worker_id="w1", lease_seconds=120,
    )
    assert disposition.action == "complete"
    assert disposition.outcome["executed"] is True
    assert handler.character_calls == [("c1", PERSONA_DREAM_KIND)]


async def test_encounter_tick_routes_to_pair_handler_with_gate() -> None:
    from kokoro_link.domain.entities.character_relationship import (
        CharacterRelationship,
    )

    rel = CharacterRelationship.create(character_a_id="a", character_b_id="b")
    handler = _RecordingSocialHandler()
    runner = _social_runner(handler, [_character("a"), _character("b")], [rel])
    job = replace(
        _claimed(ENCOUNTER_TICK_KIND, "k", character_id=rel.id),
        payload={
            "bucket": BUCKET,
            "character_a_id": "a",
            "character_b_id": "b",
            "relationship_id": rel.id,
        },
    )
    disposition = await runner.execute(
        job, would_run=False, skip_reason="missing_character", now=NOW,
        worker_id="w1", lease_seconds=120,
    )
    # would_run=False (character_id was a pair id) is overridden for social kinds.
    assert disposition.action == "complete"
    assert handler.pair_calls == [("a", "b", rel.id)]


async def test_encounter_lease_lost_abandons_the_job() -> None:
    from kokoro_link.domain.entities.character_relationship import (
        CharacterRelationship,
    )

    rel = CharacterRelationship.create(character_a_id="a", character_b_id="b")
    handler = _RecordingSocialHandler(abandon=True)
    runner = _social_runner(handler, [_character("a"), _character("b")], [rel])
    job = replace(
        _claimed(ENCOUNTER_TICK_KIND, "k", character_id=rel.id),
        payload={
            "bucket": BUCKET,
            "character_a_id": "a",
            "character_b_id": "b",
            "relationship_id": rel.id,
        },
    )
    disposition = await runner.execute(
        job, would_run=True, skip_reason=None, now=NOW,
        worker_id="w1", lease_seconds=120,
    )
    assert disposition.action == "abandon"  # not completed
