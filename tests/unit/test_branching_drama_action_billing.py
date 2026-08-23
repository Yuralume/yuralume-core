"""BD3 — 分歧劇場 as three fixed-price actions.

The plan's D2 ruling is that the billable unit is not the drama but each
button the player presses, so this file is organised around what each press
promises:

* **create** charges *before* the ``202``. A drama is generated in the
  background, so a charge raised any later would reach an out-of-credits
  player minutes afterwards as a job that mysteriously turned ``failed``.
  The handle then rides the job params, and the finalizer that writes the
  terminal row is the one that closes it — settle for a ``ready`` tree,
  refund for anything else.
* **start** is the zeroth advance (FX1). It was free until then, which was
  not a discount but a hole: 「換條路再走」 makes opening a session repeatable,
  and each opening spawns the same prefetch an advance does — up to three
  planner calls and nine pictures, all of them billed by the Gateway per
  call because no charge was covering them.
* **advance** charges around the beat, and hands the still-open handle to
  the prefetch it paid for. Two refunds are load-bearing: the ``409`` while
  another replica owns the next layer, and a node that has no children left
  — in both the player pressed and got no beat.
* **interact** charges one exchange at a time, and refunds a failed one.

Every assertion below runs against the real
:class:`CloudActionBillingService` rather than a mock of it: what is under
test is the seam between this service's failure paths and the wallet's, and a
mock of the wallet would let that seam pass while the real one refuses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.branching_drama import router
from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
    BranchingGenerationInProgress,
    _charge_from_params,
)
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
)
from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.studio_job_recovery import (
    StudioJobRecoveryService,
)
from kokoro_link.contracts.studio_jobs import (
    JOB_KIND_BRANCHING_CREATE,
    JOB_STATUS_FAILED,
    StudioGenerationJob,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_BRANCHING_DRAMA_ADVANCE,
    ACTION_BRANCHING_DRAMA_CREATE,
    ACTION_BRANCHING_DRAMA_INTERACT,
    ACTION_BRANCHING_DRAMA_SCENE_REGEN,
    ActionCharge,
    ActionPriceChanged,
    client_quoted_price,
    client_quoted_price_scope,
)
from kokoro_link.contracts.interaction_context import (
    BILLING_COVERED_HEADER_NAME,
    covered_call_receipt,
    current_interaction,
    mark_interaction_call_served,
)
from kokoro_link.domain.entities.branching_drama import (
    STATUS_FAILED,
    STATUS_READY,
    TONE_DARK,
    TONE_NEUTRAL,
    TONE_SUNNY,
    BranchingDrama,
    DramaNode,
    DramaSession,
)
from kokoro_link.application.services.branching_drama_planner import NodeOutline
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    AccountRuntimeProfile,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    ExpectedCloudRefusal,
)
from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
    InMemoryBranchingDramaRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_studio_jobs import (
    InMemoryStudioJobRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

pytestmark = pytest.mark.asyncio

_ACTION_TIER = AccountRuntimeProfile(
    name="standard", billing_shape=BILLING_SHAPE_ACTION_FIXED,
)
_USER = "u-1"


# ── billing doubles ──────────────────────────────────────────────────


class _RecordingClient:
    """The User-service ledger, as this service's charge path reaches it."""

    def __init__(
        self, *, error: Exception | None = None, price_cr: float = 9.0,
    ) -> None:
        self.charges: list[dict] = []
        self.settled: list[str] = []
        self.released: list[str] = []
        self.probed_releases: list[bool] = []
        self._error = error
        self._price_cr = price_cr
        # Monotonic across the fixture's whole life: the per-test helpers
        # clear ``charges`` between phases, and reusing the list length would
        # hand two different reservations the same id.
        self._issued = 0

    async def charge(self, **kwargs) -> ActionCharge:  # noqa: ANN003
        self.charges.append(kwargs)
        if self._error is not None:
            raise self._error
        self._issued += 1
        return ActionCharge(
            charge_id=f"chg-{self._issued}", price_cr=self._price_cr,
        )

    async def settle(self, charge_id: str) -> None:
        self.settled.append(charge_id)

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        self.released.append(charge_id)
        self.probed_releases.append(settle_if_probed)

    @property
    def action_keys(self) -> list[str]:
        return [c["action_key"] for c in self.charges]


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


class _Gateway:
    """Whether the stubbed collaborators behave like a Gateway that waives.

    The real adapters mark a call served only when the response carried the
    covered header. Flipping this off simulates "the verifier is down, so the
    Gateway billed each call itself" — the case where settling the fixed price
    on top would be a second bill for the same work.
    """

    def __init__(self, *, covered: bool = True) -> None:
        self.covered = covered
        self.scopes: list[object] = []

    def serve(self) -> None:
        self.scopes.append(current_interaction())
        if self.covered:
            mark_interaction_call_served()

    @property
    def charge_ids(self) -> list[str | None]:
        return [
            getattr(scope, "charge_id", None) for scope in self.scopes
        ]


def _character(letter: str) -> Character:
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


class _NullBriefBuilder:
    def build_persona_only_many(self, characters):  # noqa: ANN001
        return [
            CharacterBrief(
                character_id=c.id,
                name=c.name,
                summary=c.summary or "",
                text=f"brief for {c.name}",
            )
            for c in characters
        ]


class _ScriptedPlanner:
    def __init__(
        self, gateway: _Gateway, *, root_error: Exception | None = None,
    ) -> None:
        self._gateway = gateway
        self._root_error = root_error
        self.children_calls = 0

    async def plan_root(self, *, prompt, briefs, total_segments):  # noqa: ANN001, ARG002
        # Raised *before* the gateway serves: a call that fails upstream was
        # never covered, which is exactly what makes the refund whole.
        if self._root_error is not None:
            raise self._root_error
        self._gateway.serve()
        return "測試劇場", NodeOutline(
            title="序幕",
            summary="角色們在咖啡廳相遇。",
            appearing_character_ids=tuple(b.character_id for b in briefs),
        )

    async def plan_children(
        self, *, prompt, briefs, parent_summary,  # noqa: ANN001, ARG002
        path_context, depth, total_segments,  # noqa: ANN001, ARG002
    ):
        self.children_calls += 1
        self._gateway.serve()
        all_ids = tuple(b.character_id for b in briefs)
        return {
            tone: NodeOutline(
                title=f"{tone}-{depth}",
                summary=f"{tone} 方向 depth={depth}。",
                appearing_character_ids=all_ids,
            )
            for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL)
        }


class _ScriptedDirector:
    def __init__(
        self,
        gateway: _Gateway,
        *,
        narrate_error: Exception | None = None,
        respond_error: Exception | None = None,
        classify_error: Exception | None = None,
    ) -> None:
        self._gateway = gateway
        self._narrate_error = narrate_error
        self._respond_error = respond_error
        self._classify_error = classify_error

    async def narrate(self, *, node, briefs, previous_turns, player_input=""):  # noqa: ANN001, ARG002
        if self._narrate_error is not None:
            raise self._narrate_error
        self._gateway.serve()
        return f"場景敘事：{node.title}"

    async def respond_in_scene(
        self, *, node, briefs, previous_turns, exchanges, player_input,  # noqa: ANN001, ARG002
    ):
        if self._respond_error is not None:
            raise self._respond_error
        self._gateway.serve()
        return f"回應：{player_input}", None

    async def classify_tone(self, *, exchanges, children):  # noqa: ANN001, ARG002
        if self._classify_error is not None:
            raise self._classify_error
        self._gateway.serve()
        return TONE_SUNNY


class _DeferringSceneImage:
    """A scene renderer shaped like the hosted image adapter.

    Two behaviours are copied deliberately. It records the charge each draw
    ran under, which is the only way to prove a picture the prefetch drew was
    actually covered rather than billed per call on top of the fixed price.
    And it *defers* its receipt instead of counting the call — bytes in memory
    are not the picture, the stored object is — so these tests also fail if
    the storage write ever stops confirming the delivery.
    """

    def __init__(self) -> None:
        self.charge_ids: list[str | None] = []

    async def generate(
        self, *, positive: str, aspect: str = "landscape", character=None,
    ) -> bytes:  # noqa: ANN001, ARG002
        self.charge_ids.append(
            getattr(current_interaction(), "charge_id", None),
        )
        covered_call_receipt(
            {BILLING_COVERED_HEADER_NAME: "1"},
        ).defer_delivery()
        return b"\x89PNG\r\n\x1a\nSCENE"


class _RecordingJobs(InMemoryStudioJobRepository):
    """Adds the one lookup the port does not need but this file does."""

    def __init__(self) -> None:
        super().__init__()
        self.added: list[str] = []

    async def add(self, job) -> None:  # noqa: ANN001
        self.added.append(job.id)
        await super().add(job)

    async def for_target(self, target_id: str):
        for job_id in self.added:
            job = await self.get(job_id)
            if job is not None and job.target_id == target_id:
                return job
        raise AssertionError(f"no studio job recorded for {target_id}")


class _NeverAcquiredLease:
    """A cross-replica lease permanently held by somebody else.

    Drives the one refund a player is most likely to meet in production: the
    ``409`` raised when another replica owns the next outline layer.
    """

    ttl_seconds = 30

    async def acquire(self, target_id: str) -> int | None:  # noqa: ARG002
        return None

    async def release(self, target_id: str, epoch: int) -> None:  # noqa: ARG002
        return None

    async def renew(self, target_id: str, epoch: int) -> bool:  # noqa: ARG002
        return False


# ── fixture ──────────────────────────────────────────────────────────


class _Fixture:
    def __init__(
        self,
        *,
        gateway: _Gateway | None = None,
        charge_error: Exception | None = None,
        root_error: Exception | None = None,
        narrate_error: Exception | None = None,
        respond_error: Exception | None = None,
        with_jobs: bool = True,
        billed: bool = True,
        execution_lease=None,  # noqa: ANN001
        with_scene_images: bool = False,
    ) -> None:
        self.gateway = gateway or _Gateway()
        self.repo = InMemoryBranchingDramaRepository()
        self.jobs = _RecordingJobs() if with_jobs else None
        self.billing, self.client = _billing(error=charge_error)
        self.planner = _ScriptedPlanner(self.gateway, root_error=root_error)
        self.director = _ScriptedDirector(
            self.gateway,
            narrate_error=narrate_error,
            respond_error=respond_error,
        )
        # Off by default: most of this file is about text presses, and a
        # renderer would only add noise to their charge assertions.
        self.scene_image = _DeferringSceneImage() if with_scene_images else None
        self.storage = (
            InMemoryObjectStorage(public_base_url="/media")
            if with_scene_images else None
        )
        chars = {c.id: c for c in (_character("a"), _character("b"))}
        self.service = BranchingDramaService(
            repository=self.repo,
            character_service=_CharServiceStub(by_id=chars),
            brief_builder=_NullBriefBuilder(),
            planner=self.planner,
            director=self.director,
            scene_image=self.scene_image,
            object_storage=self.storage,
            jobs=self.jobs,
            execution_lease=execution_lease,
            lazy_layer_poll_interval_seconds=0.0,
            lazy_layer_poll_attempts=1,
            action_billing=self.billing if billed else None,
        )

    async def create(self, *, total_segments: int = 3):
        return await self.service.create(
            character_ids=["c-a", "c-b"],
            prompt="在咖啡廳發生的故事",
            total_segments=total_segments,
            user_id=_USER,
        )

    async def drain(self) -> None:
        """Let every spawned pipeline / prefetch task finish."""
        for _ in range(40):
            tasks = [t for t in self.service._tasks.values() if not t.done()]
            if not tasks:
                break
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.billing.wait_for_pending_closes()


# ── create ───────────────────────────────────────────────────────────


async def test_create_charges_before_the_202_and_settles_a_ready_tree() -> None:
    fixture = _Fixture()

    drama = await fixture.create()

    # The charge is raised while the caller is still inside ``create`` — the
    # route answers 202 only afterwards.
    assert fixture.client.action_keys == [ACTION_BRANCHING_DRAMA_CREATE]
    assert fixture.client.settled == []

    await fixture.drain()

    stored = await fixture.repo.get(drama.id)
    assert stored is not None and stored.status == STATUS_READY
    assert fixture.client.settled == ["chg-1"]
    assert fixture.client.released == []


async def test_create_stamps_the_background_pipeline_with_the_charge() -> None:
    """Without the scope every stage would be billed per call on top."""
    fixture = _Fixture()

    await fixture.create()
    await fixture.drain()

    assert fixture.gateway.scopes, "the pipeline made no upstream call"
    assert set(fixture.gateway.charge_ids) == {"chg-1"}


async def test_create_binds_the_charge_to_the_price_on_the_players_screen(
) -> None:
    fixture = _Fixture()

    with client_quoted_price_scope({ACTION_BRANCHING_DRAMA_CREATE: 42.0}):
        await fixture.create()
    await fixture.drain()

    assert fixture.client.charges[0]["expected_price_cr"] == 42.0


async def test_create_refuses_out_of_credits_before_writing_anything(
) -> None:
    fixture = _Fixture(charge_error=_refusal())

    with pytest.raises(ExpectedCloudRefusal):
        await fixture.create()

    assert await fixture.repo.list_recent(limit=10) == []
    assert fixture.client.settled == []


async def test_create_refunds_a_pipeline_that_failed() -> None:
    fixture = _Fixture(root_error=RuntimeError("planner down"))

    drama = await fixture.create()
    await fixture.drain()

    stored = await fixture.repo.get(drama.id)
    assert stored is not None and stored.status == STATUS_FAILED
    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-1"]


async def test_create_without_a_job_ledger_still_closes_its_charge() -> None:
    """No row to hang the handle off — the run owns its own lifecycle."""
    fixture = _Fixture(with_jobs=False)

    await fixture.create()
    await fixture.drain()

    assert fixture.client.settled == ["chg-1"]


async def test_create_carries_the_charge_in_the_job_params() -> None:
    """A restarted process has nothing else to re-attach the charge with."""
    fixture = _Fixture()

    drama = await fixture.create()
    await fixture.drain()

    job = await fixture.jobs.for_target(drama.id)
    rebuilt = _charge_from_params(job.params)
    assert rebuilt is not None
    assert rebuilt.charge_id == "chg-1"
    assert rebuilt.action_key == ACTION_BRANCHING_DRAMA_CREATE


async def test_resume_hands_a_rebuilt_handle_to_the_ledgers_probe() -> None:
    """A handle rebuilt from params cannot know what the dead process served.

    So it never asserts an outcome: it releases with ``settle_if_probed`` and
    the User service answers from the covered-call probe only it still holds.
    """
    fixture = _Fixture()
    drama = await fixture.create()
    await fixture.drain()
    fixture.client.settled.clear()

    job = await fixture.jobs.for_target(drama.id)

    outcome = await fixture.service.resume_job(job)

    assert outcome == "finalized"
    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-1"]
    assert fixture.client.probed_releases == [True]


# ── start (the zeroth advance) ───────────────────────────────────────


async def _ready_drama(fixture: _Fixture, *, total_segments: int = 3):
    """A ``ready`` tree with the creation charge already closed and cleared."""
    drama = await fixture.create(total_segments=total_segments)
    await fixture.drain()
    fixture.client.charges.clear()
    fixture.client.settled.clear()
    fixture.client.released.clear()
    fixture.client.probed_releases.clear()
    fixture.gateway.scopes.clear()
    return drama


async def test_start_charges_one_advance_and_settles_after_its_prefetch(
) -> None:
    """Opening a playthrough is a press, and it was running for free."""
    fixture = _Fixture()
    drama = await _ready_drama(fixture, total_segments=6)

    await fixture.service.start_session(drama.id, user_id=_USER)

    # The opening narration is delivered, but the prefetch it paid for is
    # still running — closing here would leave its calls uncovered.
    assert fixture.client.action_keys == [ACTION_BRANCHING_DRAMA_ADVANCE]
    assert fixture.client.settled == []
    await fixture.drain()

    assert fixture.client.settled == ["chg-2"]
    assert fixture.client.released == []


async def test_start_covers_the_layers_its_prefetch_pulls_in() -> None:
    """The hole FX1 closes: three planner calls per opening, all unbilled."""
    fixture = _Fixture()
    drama = await _ready_drama(fixture, total_segments=6)

    await fixture.service.start_session(drama.id, user_id=_USER)
    prefetch_before = fixture.planner.children_calls
    await fixture.drain()

    assert fixture.planner.children_calls > prefetch_before, (
        "this test proves nothing unless the prefetch actually ran"
    )
    assert set(fixture.gateway.charge_ids) == {"chg-2"}


async def test_start_covers_every_picture_its_prefetch_draws() -> None:
    """The expensive half: an opening spawns up to nine scene draws.

    Each one is a Gateway image call, and without a covering charge the
    Gateway bills every one of them per call — the single worst instance of
    the missing ``start`` charge, and the one 「換條路再走」 could repeat all day.
    """
    fixture = _Fixture(with_scene_images=True)
    drama = await _ready_drama(fixture, total_segments=6)
    fixture.scene_image.charge_ids.clear()

    await fixture.service.start_session(drama.id, user_id=_USER)
    await fixture.drain()

    assert fixture.scene_image.charge_ids, (
        "this test proves nothing unless the image prefetch actually drew"
    )
    assert set(fixture.scene_image.charge_ids) == {"chg-2"}
    # And the pictures landing in storage is what lets the charge stick.
    assert fixture.client.settled == ["chg-2"]
    assert fixture.client.released == []


async def test_start_refunds_an_opening_that_never_landed() -> None:
    fixture = _Fixture(narrate_error=RuntimeError("director down"))
    drama = await _ready_drama(fixture)

    with pytest.raises(RuntimeError):
        await fixture.service.start_session(drama.id, user_id=_USER)
    await fixture.drain()

    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-2"]


async def test_start_refuses_a_drama_that_is_not_ready_above_the_charge(
) -> None:
    """A press this service can refuse on its own leaves no spend/refund pair."""
    fixture = _Fixture(root_error=RuntimeError("planner down"))
    drama = await fixture.create()
    await fixture.drain()
    fixture.client.charges.clear()

    with pytest.raises(ValueError):
        await fixture.service.start_session(drama.id, user_id=_USER)

    assert fixture.client.charges == []


async def test_start_binds_the_charge_to_the_price_on_screen() -> None:
    fixture = _Fixture()
    drama = await _ready_drama(fixture)

    with client_quoted_price_scope({ACTION_BRANCHING_DRAMA_ADVANCE: 4.0}):
        await fixture.service.start_session(drama.id, user_id=_USER)
    await fixture.drain()

    assert fixture.client.charges[0]["expected_price_cr"] == 4.0


# ── advance ──────────────────────────────────────────────────────────


async def _ready_session(fixture: _Fixture, *, total_segments: int = 3):
    # The opening deliberately runs *unbilled* here (no ``user_id``): the
    # advance tests below name their own charge ids, and a charged opening
    # would only shift every one of them by one. What the opening charges is
    # pinned in its own section above.
    drama = await fixture.create(total_segments=total_segments)
    await fixture.drain()
    session, _, _ = await fixture.service.start_session(drama.id)
    await fixture.drain()
    fixture.client.charges.clear()
    fixture.client.settled.clear()
    fixture.client.released.clear()
    fixture.client.probed_releases.clear()
    fixture.gateway.scopes.clear()
    return drama, session


async def test_advance_charges_once_and_settles_after_its_prefetch() -> None:
    fixture = _Fixture()
    _, session = await _ready_session(fixture)

    await fixture.service.advance_session(session.id, user_id=_USER)

    # The beat is delivered, but the prefetch this press paid for is still
    # running — closing here would leave its calls uncovered.
    assert fixture.client.action_keys == [ACTION_BRANCHING_DRAMA_ADVANCE]
    await fixture.drain()

    assert fixture.client.settled == ["chg-2"]
    assert fixture.client.released == []


async def test_advance_covers_the_prefetch_it_paid_for() -> None:
    """D3's amortisation is only real if those calls are actually waived."""
    # Deep enough that a layer is still left to prefetch after the opening —
    # a three-segment tree is fully drawn before the first advance.
    fixture = _Fixture()
    _, session = await _ready_session(fixture, total_segments=6)

    await fixture.service.advance_session(session.id, user_id=_USER)
    prefetch_before = fixture.planner.children_calls
    await fixture.drain()

    assert fixture.planner.children_calls > prefetch_before, (
        "this test proves nothing unless the prefetch actually ran"
    )
    assert set(fixture.gateway.charge_ids) == {"chg-2"}


async def test_advance_refunds_the_409_from_another_replica() -> None:
    fixture = _Fixture()
    _, session = await _ready_session(fixture)
    # Another replica now owns the drama's lease, and the layer it is drawing
    # has not landed — the one condition that raises the retryable 409.
    fixture.service._execution_lease = _NeverAcquiredLease()
    fixture.service._repo = _NoChildrenRepo(fixture.repo)

    with pytest.raises(BranchingGenerationInProgress):
        await fixture.service.advance_session(session.id, user_id=_USER)
    await fixture.drain()

    assert fixture.client.action_keys == [ACTION_BRANCHING_DRAMA_ADVANCE]
    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-2"]
    assert fixture.client.probed_releases == [False]


async def test_advance_refunds_when_the_branch_has_no_children() -> None:
    """Walking into an ending delivered no new beat either."""
    fixture = _Fixture()
    _, session = await _ready_session(fixture)

    # total_segments=3 → depth 2 is terminal; two advances get there.
    await fixture.service.advance_session(session.id, user_id=_USER)
    await fixture.service.advance_session(session.id, user_id=_USER)
    await fixture.drain()
    settled_before = list(fixture.client.settled)

    with pytest.raises(ValueError):
        await fixture.service.advance_session(session.id, user_id=_USER)
    await fixture.drain()

    assert fixture.client.settled == settled_before
    assert len(fixture.client.released) == 1


async def test_advance_refunds_a_failure_before_any_covered_call() -> None:
    fixture = _Fixture()
    _, session = await _ready_session(fixture)
    fixture.director._classify_error = RuntimeError("director down")

    with pytest.raises(RuntimeError):
        await fixture.service.advance_session(session.id, user_id=_USER)
    await fixture.drain()

    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-2"]


async def test_advance_settles_a_failure_the_gateway_already_served() -> None:
    """A refund after a covered call would be a free generation.

    The tone classifier lands (and is waived under this charge) before the
    narrator falls over; refunding then would let "advance, then make the
    narrator fail" hand out upstream tokens the provider was already paid
    for. This is the billing service's own rule — pinned here because the
    advance path is where a drama can fail halfway.
    """
    fixture = _Fixture()
    _, session = await _ready_session(fixture)
    fixture.director._narrate_error = RuntimeError("director down")

    with pytest.raises(RuntimeError):
        await fixture.service.advance_session(session.id, user_id=_USER)
    await fixture.drain()

    assert fixture.client.settled == ["chg-2"]


# ── interact ─────────────────────────────────────────────────────────


async def test_interact_charges_and_settles_one_exchange() -> None:
    fixture = _Fixture()
    _, session = await _ready_session(fixture)

    await fixture.service.interact_session(
        session.id, player_input="你還好嗎？", user_id=_USER,
    )
    await fixture.drain()

    assert fixture.client.action_keys == [ACTION_BRANCHING_DRAMA_INTERACT]
    assert fixture.client.settled == ["chg-2"]
    assert fixture.client.released == []


async def test_interact_refunds_a_failed_exchange() -> None:
    fixture = _Fixture()
    _, session = await _ready_session(fixture)
    fixture.director._respond_error = RuntimeError("director down")

    with pytest.raises(RuntimeError):
        await fixture.service.interact_session(
            session.id, player_input="你還好嗎？", user_id=_USER,
        )
    await fixture.drain()

    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-2"]


async def test_interact_refuses_before_charging_on_a_dead_session() -> None:
    """Every answer this service already knows stays above the charge."""
    fixture = _Fixture()
    _, session = await _ready_session(fixture)
    await fixture.service.end_session(session.id)

    with pytest.raises(ValueError):
        await fixture.service.interact_session(
            session.id, player_input="還在嗎？", user_id=_USER,
        )

    assert fixture.client.charges == []


async def test_an_uncovered_action_is_refunded_not_settled() -> None:
    """The Gateway billed those calls itself; settling too is a double bill."""
    fixture = _Fixture(gateway=_Gateway(covered=False))
    _, session = await _ready_session(fixture)

    await fixture.service.interact_session(
        session.id, player_input="你還好嗎？", user_id=_USER,
    )
    await fixture.drain()

    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-2"]


# ── the duplicate row nobody re-drives ───────────────────────────────


def _interrupted_job(drama_id: str, *, charge_id: str) -> StudioGenerationJob:
    """A branching job row exactly as a dead replica would have left it."""
    return StudioGenerationJob.create(
        kind=JOB_KIND_BRANCHING_CREATE,
        target_id=drama_id,
        params={
            "operator_primary_language": "zh-TW",
            "charge_id": charge_id,
            "interaction_id": "int-1",
            "action_key": ACTION_BRANCHING_DRAMA_CREATE,
        },
    )


async def test_a_superseded_branching_row_gives_its_charge_back() -> None:
    """Recovery re-drives only the newest row — the loser's money needs a home.

    Since BD3b a branching row carries the ids of the charge its replica
    raised, so failing the duplicate with a bare ``save`` would strand that
    reservation until the User-side sweeper expired it. Neither handle is
    live here, so both go through the ledger's probe: an empty local usage
    record is ignorance, not evidence that nothing was served.
    """
    fixture = _Fixture()
    drama = await _ready_drama(fixture)
    loser = _interrupted_job(drama.id, charge_id="chg-loser")
    await fixture.jobs.add(loser)
    winner = _interrupted_job(drama.id, charge_id="chg-winner")
    await fixture.jobs.add(winner)

    report = await StudioJobRecoveryService(
        jobs=fixture.jobs, branching_drama_service=fixture.service,
    ).recover()
    await fixture.drain()

    assert report["superseded"] == 1
    assert sorted(fixture.client.released) == ["chg-loser", "chg-winner"]
    assert fixture.client.settled == []
    assert fixture.client.probed_releases == [True, True]
    closed = await fixture.jobs.get(loser.id)
    assert closed is not None and closed.status == JOB_STATUS_FAILED


async def test_a_row_finalized_elsewhere_keeps_its_verdict() -> None:
    """Losing the compare-and-swap means somebody else owns this money."""
    fixture = _Fixture()
    drama = await _ready_drama(fixture)
    job = _interrupted_job(drama.id, charge_id="chg-loser")
    await fixture.jobs.add(
        job.with_status(JOB_STATUS_FAILED, error_message="done"),
    )

    await fixture.service.supersede_job(job)

    assert fixture.client.settled == [] and fixture.client.released == []
    untouched = await fixture.jobs.get(job.id)
    assert untouched is not None and untouched.error_message == "done"


# ── zero regression ──────────────────────────────────────────────────


async def test_an_unbilled_deployment_never_touches_the_ledger() -> None:
    """Self-host and every token-billed tier run byte-identically to pre-BD3."""
    fixture = _Fixture(billed=False)
    drama = await fixture.create()
    await fixture.drain()
    session, _, _ = await fixture.service.start_session(drama.id)
    await fixture.service.advance_session(session.id, user_id=_USER)
    await fixture.service.interact_session(
        session.id, player_input="嗨", user_id=_USER,
    )
    await fixture.drain()

    stored = await fixture.repo.get(drama.id)
    assert stored is not None and stored.status == STATUS_READY
    assert fixture.client.charges == []
    assert fixture.gateway.charge_ids == [None] * len(fixture.gateway.scopes)


# ── the route guard create never had ─────────────────────────────────


@dataclass
class _RefusingServiceStub:
    """Only ``create`` matters here — it is the entry point under test."""

    error: Exception

    async def create(self, **kwargs):  # noqa: ANN003, ARG002
        raise self.error


@dataclass
class _Container:
    branching_drama_service: object
    character_service = None
    operator_profile_service = None


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _USER
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


_CREATE_BODY = {
    "character_ids": ["c-a", "c-b"],
    "prompt": "在咖啡廳發生的故事",
    "total_segments": 3,
    "quoted_price_cr": 9.0,
}


async def test_create_route_answers_402_when_the_player_is_out_of_credits(
) -> None:
    """The hole BD3 closes: create ran outside every credits guard, so an
    out-of-credits player got a 500 instead of "top up"."""
    client = _client(_Container(_RefusingServiceStub(error=_refusal())))

    response = client.post("/api/v1/branching-dramas", json=_CREATE_BODY)

    assert response.status_code == 402
    assert response.json()["detail"]["code"] == INSUFFICIENT_CREDITS_CODE


async def test_create_route_answers_409_when_the_price_moved() -> None:
    client = _client(
        _Container(
            _RefusingServiceStub(
                error=ActionPriceChanged(
                    action_key=ACTION_BRANCHING_DRAMA_CREATE,
                    expected_price_cr=9.0,
                    current_price_cr=12.0,
                ),
            ),
        ),
    )

    response = client.post("/api/v1/branching-dramas", json=_CREATE_BODY)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "price_changed"


@dataclass
class _StartServiceStub:
    """Only what the start route touches before delegating."""

    calls: list[dict]
    quotes: list[float | None]

    async def get(self, drama_id: str):
        return (
            BranchingDrama.create_pending(
                id=drama_id,
                character_ids=["c-a", "c-b"],
                prompt="在咖啡廳發生的故事",
                total_segments=3,
            )
            .with_title("測試劇場")
            .with_status(STATUS_READY)
        )

    async def start_session(self, drama_id: str, **kwargs):  # noqa: ANN003
        self.calls.append({"drama_id": drama_id, **kwargs})
        self.quotes.append(
            client_quoted_price(ACTION_BRANCHING_DRAMA_ADVANCE),
        )
        root = DramaNode.create_root(
            id="root-1",
            drama_id=drama_id,
            title="序幕",
            summary="相遇。",
            appearing_character_ids=("c-a", "c-b"),
        )
        session = DramaSession.start(
            drama_id=drama_id, root_node_id=root.id,
        ).with_turn(node_id=root.id, narration="場景敘事")
        return session, root, "場景敘事"


def _start_stub() -> _StartServiceStub:
    return _StartServiceStub(calls=[], quotes=[])


_START_PATH = "/api/v1/branching-dramas/drama-1/sessions"


async def test_start_route_names_the_player_who_pays() -> None:
    """No ``user_id`` reaching the service is a charge that never happens."""
    stub = _start_stub()

    response = _client(_Container(stub)).post(_START_PATH)

    assert response.status_code == 201
    assert stub.calls[0]["user_id"] == _USER


async def test_start_route_forwards_the_price_on_screen() -> None:
    stub = _start_stub()

    response = _client(_Container(stub)).post(
        _START_PATH, json={"quoted_price_cr": 4.0},
    )

    assert response.status_code == 201
    assert stub.quotes == [4.0]


async def test_start_route_still_takes_no_body_at_all() -> None:
    """Self-host and every old client quote nothing; the body stays optional."""
    stub = _start_stub()

    assert _client(_Container(stub)).post(_START_PATH).status_code == 201
    assert stub.quotes == [None]


async def test_start_route_answers_402_when_the_player_is_out_of_credits(
) -> None:
    @dataclass
    class _RefusingStart(_StartServiceStub):
        async def start_session(self, drama_id: str, **kwargs):  # noqa: ANN003, ARG002
            raise _refusal()

    response = _client(
        _Container(_RefusingStart(calls=[], quotes=[])),
    ).post(_START_PATH)

    assert response.status_code == 402
    assert response.json()["detail"]["code"] == INSUFFICIENT_CREDITS_CODE


async def test_advance_route_takes_no_body_as_it_always_did() -> None:
    """Old clients post nothing; the optional quote must not become required."""
    from kokoro_link.application.dto.branching_drama import (
        AdvanceSessionRequest,
    )

    assert AdvanceSessionRequest().quoted_price_cr is None


async def test_the_scene_regen_key_is_declared_for_bd6() -> None:
    """BD6 wires it; BD3 owns the constant so both tickets name one string."""
    assert ACTION_BRANCHING_DRAMA_SCENE_REGEN == "branching_drama_scene_regen"


class _NoChildrenRepo:
    """Delegating repo that reports every node as childless.

    Forces the lazy-generation path even for a layer the creation pipeline
    already drew, which is what makes the lost-lease ``409`` reachable
    without racing two real replicas.
    """

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner

    async def get_children(self, parent_node_id: str):
        return []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)
