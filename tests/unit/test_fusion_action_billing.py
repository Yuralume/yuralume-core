"""AP2 second wave — the fusion pipeline as a single fixed-price action.

A fusion run is unlike the first-wave entry points in one decisive way: the
work happens in a background task *after* the HTTP layer has already answered
``202``. So the money questions are all about the seam between the request and
the job:

* the charge has to be raised **before** the 202, or an out-of-credits player
  discovers it minutes later as a mysteriously failed story;
* the interaction scope has to wrap the background runner, or the Gateway bills
  every stage per call on top of the fixed price;
* the close has to follow the story's terminal status, because that is the only
  honest record of whether the player got what they paid for; and
* a replica that dies mid-pipeline must be able to pick the same charge back
  up from the job params, or the credits stay reserved until an upstream
  sweeper notices.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import httpx
import pytest

from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
)
from kokoro_link.application.services.fusion_character_brief import (
    FusionCharacterBriefBuilder,
)
from kokoro_link.application.services.fusion_story_service import (
    FusionStoryService,
    _charge_from_params,
)
from kokoro_link.application.services.studio_execution_lease import (
    LEASE_ABANDONED,
)
from kokoro_link.application.services.studio_job_recovery import (
    StudioJobRecoveryService,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_FUSION_STORY,
    ACTION_FUSION_STORY_ITERATE,
    ActionCharge,
    ActionPriceChanged,
    client_quoted_price_scope,
)
from kokoro_link.contracts.interaction_context import (
    current_interaction,
    mark_interaction_call_served,
)
from kokoro_link.contracts.studio_jobs import (
    JOB_KIND_FUSION_CREATE,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    MAX_JOB_ATTEMPTS,
    StudioGenerationJob,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import (
    STATUS_FAILED,
    STATUS_READY,
    FusionStory,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    AccountRuntimeProfile,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.fusion_critique import (
    FusionStoryCritique,
)
from kokoro_link.domain.value_objects.fusion_outline import (
    ACT_OPENING,
    ACT_RESOLUTION,
    ACT_RISING,
    ACT_TURN,
    FusionBeatPlan,
    FusionOutline,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    ExpectedCloudRefusal,
)
from kokoro_link.infrastructure.repositories.in_memory_fusion_stories import (
    InMemoryFusionStoryRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_studio_jobs import (
    InMemoryStudioJobRepository,
)

pytestmark = pytest.mark.asyncio

_ACTION_TIER = AccountRuntimeProfile(
    name="standard", billing_shape=BILLING_SHAPE_ACTION_FIXED,
)


# ── billing doubles ──────────────────────────────────────────────────


class _RecordingClient:
    def __init__(
        self, *, error: Exception | None = None, price_cr: float = 12.0,
    ) -> None:
        self.charges: list[dict] = []
        self.settled: list[str] = []
        self.released: list[str] = []
        self.probed_releases: list[bool] = []
        """``settle_if_probed`` as sent per release — the flag that tells
        the ledger "Core cannot know what was served, you decide"."""
        self._error = error
        self._price_cr = price_cr

    async def charge(self, **kwargs) -> ActionCharge:  # noqa: ANN003
        self.charges.append(kwargs)
        if self._error is not None:
            raise self._error
        return ActionCharge(charge_id="chg-1", price_cr=self._price_cr)

    async def settle(self, charge_id: str) -> None:
        self.settled.append(charge_id)

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        self.released.append(charge_id)
        self.probed_releases.append(settle_if_probed)


class _StubProfiles:
    async def resolve_for_operator(self, operator_id: str):  # noqa: ARG002
        return _ACTION_TIER


class _StubOperatorRepository:
    async def get(self, operator_id: str):  # noqa: ARG002
        class _Operator:
            cloud_tenant_id = "tenant-1"

        return _Operator()


def _billing(
    *, error: Exception | None = None,
) -> tuple[CloudActionBillingService, _RecordingClient]:
    client = _RecordingClient(error=error)
    return (
        CloudActionBillingService(
            client=client,
            profile_resolver=_StubProfiles(),
            operator_profiles=_StubOperatorRepository(),
        ),
        client,
    )


def _refusal() -> ExpectedCloudRefusal:
    request = httpx.Request("POST", "https://user.example/charge")
    return ExpectedCloudRefusal(
        "402",
        request=request,
        response=httpx.Response(402, request=request),
        code=INSUFFICIENT_CREDITS_CODE,
        reason="螢火不足",
    )


# ── pipeline doubles ─────────────────────────────────────────────────


def _make_character(letter: str) -> Character:
    char = Character.create(
        name=f"Char-{letter}",
        summary=f"summary {letter}",
        personality=["calm"],
        interests=["coffee"],
        speaking_style="quiet",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    object.__setattr__(char, "id", f"c-{letter}")
    return char


@dataclass
class _CharServiceStub:
    by_id: dict[str, Character]

    async def get_character_entity(self, character_id: str):
        return self.by_id.get(character_id)


def _outline() -> FusionOutline:
    beats = [
        FusionBeatPlan.create(
            sequence=i, act=act, title=f"幕{i}",
            hook=f"hook{i}", dramatic_question="",
            target_chars=500, focus_character_ids=("c-a",),
        )
        for i, act in enumerate(
            (ACT_OPENING, ACT_RISING, ACT_TURN, ACT_RESOLUTION),
        )
    ]
    return FusionOutline.create(
        title="標題", premise="前提", theme="custom", beats=beats,
    )


class _Gateway:
    """Whether the stubbed stages behave like a Gateway that waives billing.

    Every stage is one upstream call, and the real adapters mark it served only
    when the response carried ``X-Yuralume-Billing-Covered: 1``. Flipping this
    off is how a "the verifier is down, so the Gateway billed each call itself"
    deployment is simulated — the case where settling the fixed price on top
    would be a second bill for the same story.
    """

    def __init__(self, *, covered: bool = True) -> None:
        self.covered = covered
        self.scopes: list[object] = []

    def serve(self) -> None:
        self.scopes.append(current_interaction())
        if self.covered:
            mark_interaction_call_served()


class _ScriptedPlanner:
    def __init__(self, gateway: _Gateway) -> None:
        self.calls = 0
        self._gateway = gateway

    async def plan(self, *, prompt, briefs, previous_outline=None):  # noqa: ANN001, ARG002
        self.calls += 1
        self._gateway.serve()
        return _outline()


class _ScriptedWriter:
    def __init__(
        self, gateway: _Gateway, *, fail_sequences: set[int] | None = None,
    ) -> None:
        self.calls: list[int] = []
        self._gateway = gateway
        self._fail_sequences = fail_sequences or set()

    async def write_beat(
        self,
        *,
        prompt,  # noqa: ANN001, ARG002
        outline,  # noqa: ANN001, ARG002
        beat: FusionBeatPlan,
        briefs,  # noqa: ANN001, ARG002
        previously_summary="",  # noqa: ARG002
        previous_tail="",  # noqa: ARG002
        regenerate_hint=None,  # noqa: ARG002
    ) -> str:
        self.calls.append(beat.sequence)
        if beat.sequence in self._fail_sequences:
            raise RuntimeError(f"scripted writer failure seq={beat.sequence}")
        self._gateway.serve()
        return f"PROSE-{beat.sequence} 內容。"


class _ScriptedPolisher:
    async def polish(
        self, *, prompt, outline, draft_text, briefs,  # noqa: ANN001, ARG002
        critique=None, round_index=0,  # noqa: ARG002
    ) -> str:
        return f"POLISHED:{draft_text}"


class _ScriptedCritic:
    def __init__(self, gateway: _Gateway) -> None:
        self._gateway = gateway

    async def review(
        self, *, prompt, outline, draft_text, briefs,  # noqa: ANN001, ARG002
        round_index=0, previous_critique=None,  # noqa: ARG002
    ) -> FusionStoryCritique:
        self._gateway.serve()
        return FusionStoryCritique.clean()


def _rig(
    *,
    billing_error: Exception | None = None,
    covered: bool = True,
    fail_sequences: set[int] | None = None,
):
    repo = InMemoryFusionStoryRepository()
    jobs = InMemoryStudioJobRepository()
    billing, client = _billing(error=billing_error)
    gateway = _Gateway(covered=covered)
    service = FusionStoryService(
        repository=repo,
        character_service=_CharServiceStub(  # type: ignore[arg-type]
            by_id={"c-a": _make_character("a")},
        ),
        brief_builder=FusionCharacterBriefBuilder(memory_repository=None),
        planner=_ScriptedPlanner(gateway),  # type: ignore[arg-type]
        writer=_ScriptedWriter(  # type: ignore[arg-type]
            gateway, fail_sequences=fail_sequences,
        ),
        polisher=_ScriptedPolisher(),  # type: ignore[arg-type]
        critic=_ScriptedCritic(gateway),  # type: ignore[arg-type]
        jobs=jobs,
        action_billing=billing,
    )
    return service, repo, jobs, client, gateway


async def _await_terminal(service: FusionStoryService, story_id: str) -> None:
    import asyncio

    for _ in range(400):
        story = await service.get(story_id)
        assert story is not None
        if story.is_terminal():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("fusion pipeline never reached terminal state")


async def _await_closed(client: _RecordingClient) -> None:
    import asyncio

    for _ in range(400):
        if client.settled or client.released:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the action charge was never closed")


# ── create ───────────────────────────────────────────────────────────


async def test_create_charges_the_fusion_action_once_and_settles() -> None:
    service, _, _, client, gateway = _rig()

    story = await service.create(
        character_ids=["c-a"], prompt="提示", user_id="op-1",
    )
    await _await_terminal(service, story.id)
    await _await_closed(client)

    assert [c["action_key"] for c in client.charges] == [ACTION_FUSION_STORY]
    assert client.settled == ["chg-1"]
    assert client.released == []
    # Every stage ran under the charge's scope, so the Gateway could waive it.
    assert gateway.scopes and gateway.scopes[0] is not None


async def test_a_failed_pipeline_refunds_the_fixed_charge() -> None:
    """Nothing was covered before the writer blew up, so nothing is owed."""
    service, _, _, client, _ = _rig(covered=False, fail_sequences={0})

    story = await service.create(
        character_ids=["c-a"], prompt="提示", user_id="op-1",
    )
    await _await_terminal(service, story.id)
    await _await_closed(client)

    final = await service.get(story.id)
    assert final is not None and final.status == STATUS_FAILED
    assert client.released == ["chg-1"]
    assert client.settled == []
    # A live handle knows its own usage record, so it refunds outright rather
    # than asking the ledger to guess for it.
    assert client.probed_releases == [False]


async def test_a_success_the_gateway_never_covered_is_refunded() -> None:
    """C2': a pipeline the Gateway billed per call must not also pay a price.

    When the verifier is down the Gateway charges every stage on its own, so
    settling the fixed charge on top would take the player's money twice for
    one story.
    """
    service, _, _, client, _ = _rig(covered=False)

    story = await service.create(
        character_ids=["c-a"], prompt="提示", user_id="op-1",
    )
    await _await_terminal(service, story.id)
    await _await_closed(client)

    final = await service.get(story.id)
    assert final is not None and final.status == STATUS_READY
    assert client.released == ["chg-1"]
    assert client.settled == []


async def test_out_of_credits_surfaces_before_the_story_exists() -> None:
    """The 402 has to beat the 202, or the player sees a broken Studio."""
    service, repo, jobs, client, gateway = _rig(billing_error=_refusal())

    with pytest.raises(ExpectedCloudRefusal):
        await service.create(
            character_ids=["c-a"], prompt="提示", user_id="op-1",
        )

    assert await repo.list_recent(limit=10) == []
    assert await jobs.list_running() == []
    assert gateway.scopes == []
    assert client.settled == [] and client.released == []


async def test_a_moved_price_refuses_the_run_and_reserves_nothing() -> None:
    service, repo, _, client, _ = _rig(
        billing_error=ActionPriceChanged(
            action_key=ACTION_FUSION_STORY,
            expected_price_cr=12.0,
            current_price_cr=15.0,
        ),
    )

    with pytest.raises(ActionPriceChanged):
        await service.create(
            character_ids=["c-a"], prompt="提示", user_id="op-1",
        )

    assert await repo.list_recent(limit=10) == []
    assert client.settled == [] and client.released == []


async def test_create_binds_the_price_the_player_was_quoted() -> None:
    """R9: the run is billed at the number that was on screen, not in a cache."""
    service, _, _, client, _ = _rig()

    with client_quoted_price_scope({ACTION_FUSION_STORY: 12.0}):
        story = await service.create(
            character_ids=["c-a"], prompt="提示", user_id="op-1",
        )
    await _await_terminal(service, story.id)

    assert client.charges[0]["expected_price_cr"] == 12.0


# ── iterate ──────────────────────────────────────────────────────────


async def _ready_story(
    service: FusionStoryService, client: _RecordingClient,
) -> FusionStory:
    story = await service.create(
        character_ids=["c-a"], prompt="提示", user_id="op-1",
    )
    await _await_terminal(service, story.id)
    await _await_closed(client)
    client.charges.clear()
    client.settled.clear()
    client.released.clear()
    ready = await service.get(story.id)
    assert ready is not None and ready.status == STATUS_READY
    return ready


@pytest.mark.parametrize("operation", ["outline", "beat", "polish"])
async def test_every_iteration_charges_the_iterate_action(
    operation: str,
) -> None:
    service, _, _, client, _ = _rig()
    story = await _ready_story(service, client)

    if operation == "outline":
        await service.iterate_outline(story.id, user_id="op-1")
    elif operation == "beat":
        await service.iterate_beat(story.id, beat_index=0, user_id="op-1")
    else:
        await service.iterate_polish(story.id, user_id="op-1")
    await _await_terminal(service, story.id)
    await _await_closed(client)

    assert [c["action_key"] for c in client.charges] == [
        ACTION_FUSION_STORY_ITERATE,
    ]
    assert client.settled == ["chg-1"]


# ── lease handover ───────────────────────────────────────────────────


async def _abandoned() -> str:
    return LEASE_ABANDONED


async def test_an_abandoned_lease_leaves_the_charge_for_the_owning_replica(
) -> None:
    """Closing here would race the replica that is actually generating.

    Whichever replica holds the lease finalizes the job, and the charge ids
    travel with the job params — so this one must not settle (billing for work
    it did not do) nor release (refunding work the other replica is producing).
    The row stays ``running`` for the same reason: it is the only thing that can
    still hand this charge to whoever finishes the story.
    """
    service, _, jobs, client, _ = _rig()
    charge = await service._begin_action_charge(  # type: ignore[attr-defined]
        ACTION_FUSION_STORY, user_id="op-1",
    )
    job = _interrupted_job("story-x")
    await jobs.add(job)

    await service._run_tracked(  # type: ignore[attr-defined]
        job, _abandoned(), charge=charge,
    )

    assert client.settled == [] and client.released == []
    assert charge is not None and charge.closed is False
    still_running = await jobs.get(job.id)
    assert still_running is not None
    assert still_running.status == JOB_STATUS_RUNNING


async def test_superseding_a_duplicate_row_refunds_the_charge_it_held(
) -> None:
    """The one place a lease-loser's money is decided.

    Two ``running`` rows for one story mean two charges (a second call that
    lost the target's lease left its own row behind). Recovery re-drives only
    the newest, so failing the loser without closing its reservation would hold
    the player's credits for a run nothing will ever finish.
    """
    service, repo, jobs, client, _ = _rig()
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    story = story.with_status(STATUS_FAILED, error_message="炸了")
    await repo.add(story)
    loser = _interrupted_job(story.id, charge_id="chg-loser")
    object.__setattr__(
        loser, "created_at", loser.created_at - timedelta(minutes=5),
    )
    await jobs.add(loser)
    winner = _interrupted_job(story.id, charge_id="chg-winner")
    await jobs.add(winner)

    report = await StudioJobRecoveryService(
        jobs=jobs, fusion_story_service=service,
    ).recover()

    assert report["superseded"] == 1
    # Both charges are closed, and both go through the ledger's probe: neither
    # handle is live here, so "my usage record is empty" is ignorance, not
    # evidence that nothing was served.
    assert sorted(client.released) == ["chg-loser", "chg-winner"]
    assert client.settled == []
    assert client.probed_releases == [True, True]
    closed = await jobs.get(loser.id)
    assert closed is not None and closed.status == JOB_STATUS_FAILED


async def test_an_unrecorded_run_that_never_got_the_lease_refunds() -> None:
    """The job-ledger outage path has no params to hand the charge over with."""
    service, _, _, client, _ = _rig()
    charge = await service._begin_action_charge(  # type: ignore[attr-defined]
        ACTION_FUSION_STORY, user_id="op-1",
    )

    await service._run_billed(  # type: ignore[attr-defined]
        _abandoned(), target_id="story-x", charge=charge,
    )

    assert client.released == ["chg-1"]
    assert client.settled == []


# ── recovery ─────────────────────────────────────────────────────────


def _interrupted_job(
    story_id: str, *, attempts: int = 1, charge_id: str = "chg-1",
) -> StudioGenerationJob:
    """A job row exactly as a dead replica would have left it behind."""
    job = StudioGenerationJob.create(
        kind=JOB_KIND_FUSION_CREATE,
        target_id=story_id,
        params={
            "operator_primary_language": "zh-TW",
            "user_id": "op-1",
            "charge_id": charge_id,
            "interaction_id": "int-1",
            "action_key": ACTION_FUSION_STORY,
        },
    )
    return job.with_attempts(attempts) if attempts != job.attempts else job


async def test_create_writes_the_charge_into_the_job_params() -> None:
    """The params are all a restarted process has to find the charge with."""
    service, _, jobs, _, _ = _rig()

    story = await service.create(
        character_ids=["c-a"], prompt="提示", user_id="op-1",
    )
    await _await_terminal(service, story.id)
    recorded = [
        job for job in jobs._jobs.values()  # noqa: SLF001 — no list-all port
        if job.target_id == story.id
    ]
    assert len(recorded) == 1
    params = dict(recorded[0].params)

    assert params["charge_id"] == "chg-1"
    assert params["action_key"] == ACTION_FUSION_STORY
    assert params["interaction_id"]


async def test_resume_hands_a_story_finished_before_the_restart_to_the_probe(
) -> None:
    """R2: a ready story is not this charge's receipt — the ledger decides.

    The rebuilt handle has an empty usage record, so it can neither refund
    (that would hand back money for a story the player can read) nor settle
    (the story may have been produced by a competing charge). It asks the User
    service, which still holds the covered-call probe for *this* charge id.
    """
    service, repo, jobs, client, _ = _rig()
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    story = story.with_outline(_outline())
    for beat in story.beats:
        story = story.with_beat_content(
            beat_id=beat.id, content=f"PROSE-{beat.sequence}",
        )
    story = story.with_full_text("完成")
    assert story.status == STATUS_READY
    await repo.add(story)
    job = _interrupted_job(story.id)
    await jobs.add(job)

    assert await service.resume_job(job) == "finalized"

    assert client.released == ["chg-1"]
    assert client.settled == []
    assert client.probed_releases == [True]


async def test_resume_hands_a_pre_restart_failure_to_the_ledger_probe() -> None:
    """The dead process's covered calls are not this one's to write off.

    A story that failed before the restart may still have burned two beats
    upstream. The rebuilt handle cannot see that — its usage record is empty by
    construction — so a plain refund here would hand back money for work the
    provider was already paid for. ``settle_if_probed`` asks the ledger, which
    is the only party that still remembers.
    """
    service, repo, jobs, client, _ = _rig()
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    story = story.with_status(STATUS_FAILED, error_message="炸了")
    await repo.add(story)
    job = _interrupted_job(story.id)
    await jobs.add(job)

    assert await service.resume_job(job) == "finalized"

    assert client.released == ["chg-1"]
    assert client.settled == []
    assert client.probed_releases == [True]


async def test_resume_remounts_the_scope_and_asks_the_probe_when_it_finishes(
) -> None:
    service, repo, jobs, client, gateway = _rig()
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    await repo.add(story)  # still ``planning`` — the pipeline never started
    job = _interrupted_job(story.id)
    await jobs.add(job)

    assert await service.resume_job(job) == "resumed"
    await _await_terminal(service, story.id)
    await _await_closed(client)

    # No second charge: the resumed run reuses the one the params carried.
    assert client.charges == []
    # ...and the close is still the probe's call, not this handle's: the run it
    # finished may not be the run this reservation paid for. The stamped calls
    # it just made are what the ledger will answer from.
    assert client.released == ["chg-1"]
    assert client.settled == []
    assert client.probed_releases == [True]
    # ...and it kept stamping the same interaction, so the Gateway keeps
    # waiving the remaining stages instead of billing them per call.
    scope = gateway.scopes[0]
    assert scope is not None and scope.charge_id == "chg-1"
    assert scope.interaction_id == "int-1"


async def test_resume_closes_the_charge_when_the_target_is_gone() -> None:
    service, _, jobs, client, _ = _rig()
    job = _interrupted_job("story-missing")
    await jobs.add(job)

    assert await service.resume_job(job) == "failed"

    assert client.released == ["chg-1"]
    assert client.settled == []
    assert client.probed_releases == [True]


async def test_resume_closes_the_charge_once_the_attempt_cap_is_reached(
) -> None:
    service, repo, jobs, client, _ = _rig()
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    await repo.add(story)
    job = _interrupted_job(story.id, attempts=MAX_JOB_ATTEMPTS)
    await jobs.add(job)

    assert await service.resume_job(job) == "failed"

    assert client.released == ["chg-1"]
    assert client.settled == []
    assert client.probed_releases == [True]


async def test_a_resumed_run_that_fails_again_still_asks_the_probe() -> None:
    """The pre-crash covered calls survive the second failure too.

    The resumed pipeline generates under the original charge, so its own usage
    record only ever reflects what *this* attempt served. Whatever the dead
    process burned before it died is still only visible to the ledger.
    """
    service, repo, jobs, client, _ = _rig(covered=False, fail_sequences={0})
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    await repo.add(story)  # still ``planning`` — the pipeline never started
    job = _interrupted_job(story.id)
    await jobs.add(job)

    assert await service.resume_job(job) == "resumed"
    await _await_terminal(service, story.id)
    await _await_closed(client)

    final = await service.get(story.id)
    assert final is not None and final.status == STATUS_FAILED
    assert client.released == ["chg-1"]
    assert client.probed_releases == [True]


async def test_a_job_without_charge_params_resumes_unbilled() -> None:
    """Pre-AP2 rows (and un-billed tiers) must resume exactly as before."""
    service, repo, jobs, client, _ = _rig()
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    await repo.add(story)
    job = StudioGenerationJob.create(
        kind=JOB_KIND_FUSION_CREATE,
        target_id=story.id,
        params={"operator_primary_language": "zh-TW", "user_id": "op-1"},
    )
    await jobs.add(job)

    assert await service.resume_job(job) == "resumed"
    await _await_terminal(service, story.id)

    assert client.charges == []
    assert client.settled == [] and client.released == []
    finished = await jobs.get(job.id)
    assert finished is not None and finished.status != JOB_STATUS_RUNNING


async def test_a_charge_still_closes_when_the_job_ledger_is_unavailable(
) -> None:
    """Losing restart durability must not also strand the player's credits.

    Job bookkeeping is fail-soft by design (C0), so a ledger outage lets the
    pipeline run anyway — but the money still has to be closed on the story's
    outcome, or the reservation sits open until an upstream sweeper expires it.
    """
    service, _, jobs, client, _ = _rig()

    async def _explode(job):  # noqa: ANN001, ARG001
        raise RuntimeError("job ledger down")

    jobs.add = _explode  # type: ignore[method-assign]

    story = await service.create(
        character_ids=["c-a"], prompt="提示", user_id="op-1",
    )
    await _await_terminal(service, story.id)
    await _await_closed(client)

    assert client.settled == ["chg-1"]


# ── the order the two records land in (R1) ───────────────────────────


async def test_the_job_row_is_written_before_the_charge_is_closed() -> None:
    """State first, money second — the ordering the whole recovery story rests on.

    The job row is the only durable record that this run ended. Closing the
    charge first and then failing to write the row would leave a ``running``
    row whose money is already spent, and the next recovery pass would happily
    decide it a second time.
    """
    service, _, jobs, client, _ = _rig()
    order: list[str] = []
    original_claim = jobs.save_terminal_if_running
    original_settle = client.settle

    async def _tracking_claim(job: StudioGenerationJob) -> bool:
        won = await original_claim(job)
        if won:
            order.append("job")
        return won

    async def _tracking_settle(charge_id: str) -> None:
        order.append("charge")
        await original_settle(charge_id)

    jobs.save_terminal_if_running = _tracking_claim  # type: ignore[method-assign]
    client.settle = _tracking_settle  # type: ignore[method-assign]

    story = await service.create(
        character_ids=["c-a"], prompt="提示", user_id="op-1",
    )
    await _await_terminal(service, story.id)
    await _await_closed(client)

    assert order == ["job", "charge"]


async def test_a_job_row_that_cannot_be_finalized_leaves_the_charge_open(
) -> None:
    """No durable terminal state ⇒ no verdict on the money.

    The charge stays open on purpose: the row is still ``running``, so the next
    recovery pass owns both halves and closes them together. Deciding the money
    here would be deciding it without the record that says it was decided.
    """
    service, repo, jobs, client, _ = _rig()
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    story = story.with_status(STATUS_FAILED, error_message="炸了")
    await repo.add(story)
    job = _interrupted_job(story.id)
    await jobs.add(job)

    async def _explode(saved):  # noqa: ANN001, ARG001
        raise RuntimeError("job ledger down")

    jobs.save_terminal_if_running = _explode  # type: ignore[method-assign]
    charge = await service._begin_action_charge(  # type: ignore[attr-defined]
        ACTION_FUSION_STORY, user_id="op-1",
    )

    await service._finalize_job(job, charge=charge)  # type: ignore[attr-defined]

    assert client.settled == [] and client.released == []
    assert charge is not None and charge.closed is False
    still_running = await jobs.get(job.id)
    assert still_running is not None
    assert still_running.status == JOB_STATUS_RUNNING


async def test_finalizing_the_same_job_twice_closes_the_charge_once() -> None:
    """R4: every close path is reachable twice; the wallet must see one.

    A recovery pass racing a late in-process finalize is the realistic way
    here, and a second close would be a second ledger movement on a
    reservation that no longer exists. The row transition is what decides it:
    the first call flips it out of ``running`` and the second finds nothing
    left to claim.
    """
    service, repo, jobs, client, _ = _rig()
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    story = story.with_outline(_outline())
    for beat in story.beats:
        story = story.with_beat_content(
            beat_id=beat.id, content=f"PROSE-{beat.sequence}",
        )
    story = story.with_full_text("完成")
    await repo.add(story)
    job = _interrupted_job(story.id)
    await jobs.add(job)
    charge = _charge_from_params(job.params)

    await service._finalize_job(  # type: ignore[attr-defined]
        job, charge=charge, fresh_handle=True,
    )
    await service._finalize_job(  # type: ignore[attr-defined]
        job, charge=charge, fresh_handle=True,
    )

    assert client.released == ["chg-1"]
    assert client.settled == []
    assert client.probed_releases == [True]


# ── R2: a ready story is not proof that *this* charge delivered ──────


def _delivered_story() -> FusionStory:
    """A story in exactly the shape a finished pipeline leaves behind."""
    story = FusionStory.create_pending(character_ids=["c-a"], prompt="提示")
    story = story.with_outline(_outline())
    for beat in story.beats:
        story = story.with_beat_content(
            beat_id=beat.id, content=f"PROSE-{beat.sequence}",
        )
    story = story.with_full_text("完成")
    assert story.status == STATUS_READY
    return story


async def test_a_losers_orphan_charge_is_not_settled_by_the_winners_story(
) -> None:
    """The double-charge R2 closes: one story, two charges, one settle.

    A second entry-point call that lost the target's lease leaves a ``running``
    row carrying its own reservation, while the replica holding the lease
    finishes the story under a *different* charge. When recovery re-drives the
    loser's row it finds a ``ready`` story — the winner's — and the old rule
    ("story ready ⇒ settle") billed the player a second time for prose the
    loser never produced.

    The loser's charge has no covered call stamped against it, so handing the
    verdict to the ledger's probe is what tells the two apart: the winner's
    charge has evidence and settles, the loser's has none and is refunded.
    """
    service, repo, jobs, client, _ = _rig()
    story = _delivered_story()
    await repo.add(story)
    # The winner already finalized its own row under its own charge.
    winner = _interrupted_job(story.id, charge_id="chg-winner")
    await jobs.add(winner.with_status(JOB_STATUS_SUCCEEDED))
    # The loser's row is still ``running`` and still holds its reservation.
    loser = _interrupted_job(story.id, charge_id="chg-loser")
    await jobs.add(loser)

    assert await service.resume_job(loser) == "finalized"

    # Nothing is settled locally — the loser's money is the ledger's call.
    assert client.settled == []
    assert client.released == ["chg-loser"]
    assert client.probed_releases == [True]
    closed = await jobs.get(loser.id)
    assert closed is not None and closed.status == JOB_STATUS_SUCCEEDED


# ── R4: the row transition is the concurrency token ──────────────────


async def test_two_racing_handles_close_one_job_charge_exactly_once() -> None:
    """Two independent finalizers, one job row, one movement on the wallet.

    Both handles are rebuilt from the same params, so neither can see that the
    other has closed: their ``closed`` flags are per-object. The only thing
    they share is the job row, so the compare-and-swap on ``running`` is what
    has to pick a winner — and the loser must leave the money alone.
    """
    service, repo, jobs, client, _ = _rig()
    story = _delivered_story()
    await repo.add(story)
    job = _interrupted_job(story.id)
    await jobs.add(job)
    first = _charge_from_params(job.params)
    second = _charge_from_params(job.params)
    assert first is not None and second is not None and first is not second

    await asyncio.gather(
        service._finalize_job(  # type: ignore[attr-defined]
            job, charge=first, fresh_handle=True,
        ),
        service._finalize_job(  # type: ignore[attr-defined]
            job, charge=second, fresh_handle=True,
        ),
    )

    assert client.settled + client.released == ["chg-1"]
    # Exactly one handle believes it closed the reservation.
    assert [first.closed, second.closed].count(True) == 1


async def test_superseding_a_row_another_finalizer_already_took_closes_nothing(
) -> None:
    """Supersede loses the same swap, and stops at the same place.

    The in-process task that owns this row can finalize it (and close its
    charge) between recovery listing the row and superseding it. Writing the
    ``failed`` status unconditionally would then close a reservation that is
    already closed — the second ledger movement R4 exists to prevent.
    """
    service, repo, jobs, client, _ = _rig()
    story = _delivered_story()
    await repo.add(story)
    job = _interrupted_job(story.id, charge_id="chg-loser")
    await jobs.add(job.with_status(JOB_STATUS_FAILED, error_message="done"))

    await service.supersede_job(job)

    assert client.settled == [] and client.released == []
    untouched = await jobs.get(job.id)
    assert untouched is not None
    assert untouched.error_message == "done"
