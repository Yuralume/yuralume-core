"""BD6 — the player redraws one scene by hand.

The prefetch draws every picture fail-soft, which leaves two states no
automatic retry ever repairs: a node whose renderer was down when it was
drawn (no picture at all) and one whose picture the player simply does not
want. Both are the same press, so both are the same action —
``branching_drama_scene_regen`` — and this file is organised around what
that press promises:

* **it is answered, never skipped.** A failed redraw is a 502, not the
  silent "carry on without a picture" the prefetch is entitled to.
* **it is charged once.** A double-click is refused, not billed twice; a
  failure refunds; a deployment with no renderer refuses *above* the
  charge so a press that never ran leaves no spend/refund pair.
* **the new picture is actually seen.** A redraw that reused the object
  key would leave every cache in the path serving the picture the player
  just rejected, so the new one takes a key nothing has ever fetched.

The billing assertions run against the real
:class:`CloudActionBillingService`: what is under test is the seam between
this service's failure paths and the wallet's, and a mock of the wallet
would let that seam pass while the real one refuses.
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
    SceneImageUnavailable,
    SceneRegenerationFailed,
    SceneRegenerationInProgress,
)
from kokoro_link.application.services.character_activity_anchor import (
    CharacterActivityAnchor,
)
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
)
from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_BRANCHING_DRAMA_SCENE_REGEN,
    ActionCharge,
    client_quoted_price_scope,
)
from kokoro_link.contracts.interaction_context import (
    BILLING_COVERED_HEADER_NAME,
    covered_call_receipt,
)
from kokoro_link.contracts.object_storage import ObjectStorageError
from kokoro_link.contracts.scene_image import SceneImageError
from kokoro_link.domain.entities.branching_drama import (
    STATUS_READY,
    BranchingDrama,
    DramaNode,
)
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
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

pytestmark = pytest.mark.asyncio

_USER = "u-1"
_DRAMA_ID = "drama-1"
_ROOT_ID = "root-1"
_ORIGINAL_KEY = f"branching-dramas/{_DRAMA_ID}/{_ROOT_ID}.png"
_ACTION_TIER = AccountRuntimeProfile(
    name="standard", billing_shape=BILLING_SHAPE_ACTION_FIXED,
)


# ── doubles ──────────────────────────────────────────────────────────


class _RecordingClient:
    """The User-service ledger, as the charge path reaches it."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.charges: list[dict] = []
        self.settled: list[str] = []
        self.released: list[str] = []
        self._error = error
        self._issued = 0

    async def charge(self, **kwargs) -> ActionCharge:  # noqa: ANN003
        self.charges.append(kwargs)
        if self._error is not None:
            raise self._error
        self._issued += 1
        return ActionCharge(charge_id=f"chg-{self._issued}", price_cr=3.0)

    async def settle(self, charge_id: str) -> None:
        self.settled.append(charge_id)

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        self.released.append(charge_id)

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


def _refusal() -> ExpectedCloudRefusal:
    request = httpx.Request("POST", "https://user.example/charge")
    return ExpectedCloudRefusal(
        "402",
        request=request,
        response=httpx.Response(402, request=request),
        code=INSUFFICIENT_CREDITS_CODE,
        reason="螢火不足",
    )


class _FakeSceneImagePort:
    """A renderer shaped like the hosted one: covered, but *deferred*.

    Two details are load-bearing and they pull in opposite directions. The
    billing service refunds a charge that never saw a covered Gateway call,
    so a double that skipped the receipt entirely would make every settle
    assertion below vacuously "released". But the real hosted adapter does
    **not** count the call when the bytes arrive: it defers the receipt,
    because bytes in memory are not the thing the player bought — the stored
    object is. A double that marked delivery itself would therefore hide
    exactly the bug FX1 fixed (the redraw never confirming, and so refunding
    every hosted picture it drew), and would let a storage failure look
    settled when the player got nothing.
    """

    def __init__(
        self,
        *,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.calls = 0
        self.error = error
        self._gate = gate

    async def generate(
        self, *, positive: str, aspect: str = "landscape", character=None,
    ) -> bytes:  # noqa: ANN001, ARG002
        self.calls += 1
        if self._gate is not None:
            await self._gate.wait()
        if self.error is not None:
            raise self.error
        covered_call_receipt(
            {BILLING_COVERED_HEADER_NAME: "1"},
        ).defer_delivery()
        return b"\x89PNG\r\n\x1a\nREDRAWN"


class _RefusingStorage(InMemoryObjectStorage):
    async def put_bytes(self, **kwargs):  # noqa: ANN003
        raise ObjectStorageError("bucket on fire")


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


# ── fixture ──────────────────────────────────────────────────────────


class _Fixture:
    def __init__(
        self,
        *,
        scene_image: object | None = "default",
        storage: InMemoryObjectStorage | None = None,
        charge_error: Exception | None = None,
        billed: bool = True,
        image_prefetch_depth: int = 0,
        characters=None,  # noqa: ANN001 - CharacterRepositoryPort | None
    ) -> None:
        # Depth 0 by default so nothing is drawn automatically: every
        # picture in this file is one the player asked for by hand.
        self.repo = InMemoryBranchingDramaRepository()
        self.storage = (
            storage if storage is not None
            else InMemoryObjectStorage(public_base_url="/media")
        )
        self.scene_image = (
            _FakeSceneImagePort() if scene_image == "default" else scene_image
        )
        self.client = _RecordingClient(error=charge_error)
        self.billing = CloudActionBillingService(
            client=self.client,
            profile_resolver=_StubProfiles(),
            operator_profiles=_StubOperatorRepository(),
        )
        chars = {c.id: c for c in (_character("a"), _character("b"))}
        self.service = BranchingDramaService(
            repository=self.repo,
            character_service=_CharServiceStub(by_id=chars),
            brief_builder=_NullBriefBuilder(),
            planner=object(),
            director=object(),
            scene_image=self.scene_image,
            object_storage=self.storage if scene_image is not None else None,
            image_prefetch_depth=image_prefetch_depth,
            action_billing=self.billing if billed else None,
            activity_anchor=(
                CharacterActivityAnchor(characters)
                if characters is not None else None
            ),
        )

    async def seed(self, *, with_image: bool = True) -> DramaNode:
        drama = BranchingDrama.create_pending(
            id=_DRAMA_ID,
            character_ids=["c-a", "c-b"],
            prompt="在咖啡廳發生的故事",
            total_segments=3,
        ).with_title("測試劇場").with_status(STATUS_READY)
        await self.repo.add(drama)
        node = DramaNode.create_root(
            id=_ROOT_ID,
            drama_id=_DRAMA_ID,
            title="序幕",
            summary="角色們在咖啡廳相遇。",
            appearing_character_ids=("c-a", "c-b"),
        )
        if with_image:
            stored = await self.storage.put_bytes(
                object_key=_ORIGINAL_KEY,
                content=b"\x89PNG\r\n\x1a\nOLD",
                content_type="image/png",
            )
            node = node.with_image_path(stored.url)
        await self.repo.save_node(node)
        return node

    async def drain(self) -> None:
        await self.billing.wait_for_pending_closes()


# ── the redraw itself ────────────────────────────────────────────────


async def test_redraw_lands_on_a_key_no_cache_has_ever_seen() -> None:
    """Reusing the key would leave the player looking at what they rejected."""
    fixture = _Fixture()
    original = await fixture.seed()

    redrawn = await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )

    assert redrawn.image_path != original.image_path
    new_key = fixture.storage.object_key_from_url(redrawn.image_path)
    assert new_key is not None
    assert new_key != _ORIGINAL_KEY
    assert new_key.startswith(f"branching-dramas/{_DRAMA_ID}/{_ROOT_ID}-")
    assert (
        await fixture.storage.get_bytes(object_key=new_key)
        == b"\x89PNG\r\n\x1a\nREDRAWN"
    )
    # And the node persisted, not just the returned copy.
    stored = await fixture.repo.get_node(_ROOT_ID)
    assert stored is not None and stored.image_path == redrawn.image_path


async def test_redraw_discards_the_picture_it_replaced() -> None:
    fixture = _Fixture()
    await fixture.seed()

    await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )

    assert await fixture.storage.stat(object_key=_ORIGINAL_KEY) is None


async def test_redraw_fills_in_a_node_the_prefetch_never_drew() -> None:
    """The repair case: no picture at all, because the renderer was down."""
    fixture = _Fixture()
    await fixture.seed(with_image=False)

    redrawn = await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )

    assert redrawn.image_path is not None
    assert fixture.client.settled == ["chg-1"]


async def test_two_redraws_do_not_collide_on_one_key() -> None:
    fixture = _Fixture()
    await fixture.seed()

    first = await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )
    second = await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )

    assert first.image_path != second.image_path


# ── one press, one charge ────────────────────────────────────────────


async def test_redraw_charges_its_own_action_and_settles() -> None:
    fixture = _Fixture()
    await fixture.seed()

    await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )
    await fixture.drain()

    assert fixture.client.action_keys == [ACTION_BRANCHING_DRAMA_SCENE_REGEN]
    assert fixture.client.settled == ["chg-1"]
    assert fixture.client.released == []


async def test_redraw_binds_the_charge_to_the_price_on_screen() -> None:
    fixture = _Fixture()
    await fixture.seed()

    with client_quoted_price_scope(
        {ACTION_BRANCHING_DRAMA_SCENE_REGEN: 3.0},
    ):
        await fixture.service.regenerate_node_image(
            _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
        )

    assert fixture.client.charges[0]["expected_price_cr"] == 3.0


async def test_a_renderer_failure_refunds_and_is_never_silent() -> None:
    """The prefetch may skip a picture; a press must be answered."""
    fixture = _Fixture(
        scene_image=_FakeSceneImagePort(error=SceneImageError("comfy down")),
    )
    original = await fixture.seed()

    with pytest.raises(SceneRegenerationFailed):
        await fixture.service.regenerate_node_image(
            _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
        )
    await fixture.drain()

    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-1"]
    # The picture the player already had survives a failed redraw.
    stored = await fixture.repo.get_node(_ROOT_ID)
    assert stored is not None and stored.image_path == original.image_path
    assert await fixture.storage.stat(object_key=_ORIGINAL_KEY) is not None


async def test_a_storage_failure_refunds_the_picture_that_never_landed() -> None:
    """A picture the player cannot see is not a picture they bought.

    The whole point of the hosted adapter deferring its receipt is that the
    deliverable is the *stored* object: bytes lost between the renderer and
    the bucket leave the player with nothing, so the fixed charge is refunded
    and the platform absorbs the upstream cost of the draw. The opposite
    reading — "the renderer was paid, so settle" — makes a broken bucket the
    player's bill.
    """
    fixture = _Fixture(
        storage=_RefusingStorage(public_base_url="/media"),
    )
    fixture_node = DramaNode.create_root(
        id=_ROOT_ID,
        drama_id=_DRAMA_ID,
        title="序幕",
        summary="角色們在咖啡廳相遇。",
        appearing_character_ids=("c-a", "c-b"),
    )
    await fixture.repo.add(
        BranchingDrama.create_pending(
            id=_DRAMA_ID,
            character_ids=["c-a", "c-b"],
            prompt="在咖啡廳發生的故事",
            total_segments=3,
        ).with_title("測試劇場").with_status(STATUS_READY),
    )
    await fixture.repo.save_node(fixture_node)

    with pytest.raises(SceneRegenerationFailed):
        await fixture.service.regenerate_node_image(
            _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
        )
    await fixture.drain()

    assert fixture.client.settled == []
    assert fixture.client.released == ["chg-1"]


async def test_a_stored_picture_is_what_makes_the_charge_stick() -> None:
    """The regression FX1 closes: a redraw that worked, refunded anyway.

    The hosted renderer hands back a *deferred* receipt, so nothing counts
    the call until the object-storage write confirms it. Before FX1 the
    redraw path never confirmed, every handle looked unconsumed, and the
    billing service's "succeeded without a covered call" rule refunded each
    one — a hosted picture that drew perfectly and cost nothing, forever.
    """
    fixture = _Fixture()
    await fixture.seed(with_image=False)

    await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )
    await fixture.drain()

    assert fixture.client.settled == ["chg-1"]
    assert fixture.client.released == []
    assert fixture.billing.counters.released_uncovered == 0


async def test_a_double_click_is_refused_not_billed_twice() -> None:
    gate = asyncio.Event()
    fixture = _Fixture(scene_image=_FakeSceneImagePort(gate=gate))
    await fixture.seed()

    first = asyncio.ensure_future(
        fixture.service.regenerate_node_image(
            _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
        ),
    )
    # Let the first press get as far as the renderer before pressing again.
    for _ in range(20):
        if fixture.scene_image.calls:
            break
        await asyncio.sleep(0)

    with pytest.raises(SceneRegenerationInProgress):
        await fixture.service.regenerate_node_image(
            _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
        )

    gate.set()
    await first
    await fixture.drain()

    assert fixture.scene_image.calls == 1
    assert fixture.client.action_keys == [ACTION_BRANCHING_DRAMA_SCENE_REGEN]
    assert fixture.client.settled == ["chg-1"]


async def test_the_guard_clears_so_a_later_press_is_accepted() -> None:
    fixture = _Fixture()
    await fixture.seed()

    await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )
    await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )

    assert fixture.scene_image.calls == 2


async def test_out_of_credits_never_touches_the_picture() -> None:
    fixture = _Fixture(charge_error=_refusal())
    original = await fixture.seed()

    with pytest.raises(ExpectedCloudRefusal):
        await fixture.service.regenerate_node_image(
            _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
        )

    assert fixture.scene_image.calls == 0
    stored = await fixture.repo.get_node(_ROOT_ID)
    assert stored is not None and stored.image_path == original.image_path


# ── capability, not prefetch policy ──────────────────────────────────


async def test_a_deployment_with_no_renderer_refuses_above_the_charge() -> None:
    fixture = _Fixture(scene_image=None)
    await fixture.seed(with_image=False)

    with pytest.raises(SceneImageUnavailable):
        await fixture.service.regenerate_node_image(
            _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
        )

    assert fixture.client.charges == []


async def test_prefetch_switched_off_still_honours_a_hand_drawn_press() -> None:
    """Depth 0 means "stop drawing branches nobody walks into", not
    "this deployment cannot draw" — the player is paying for this one."""
    fixture = _Fixture(image_prefetch_depth=0)
    await fixture.seed(with_image=False)

    redrawn = await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )

    assert redrawn.image_path is not None


async def test_self_host_redraws_for_free() -> None:
    fixture = _Fixture(billed=False)
    await fixture.seed()

    redrawn = await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )

    assert redrawn.image_path is not None
    assert fixture.client.charges == []


async def test_a_node_from_another_drama_is_not_found() -> None:
    fixture = _Fixture()
    await fixture.seed()

    with pytest.raises(ValueError):
        await fixture.service.regenerate_node_image(
            _ROOT_ID, drama_id="drama-other", user_id=_USER,
        )

    assert fixture.client.charges == []


# ── the prefetch must not undo the redraw (FX3) ──────────────────────
#
# The automatic prefetch and this manual redraw draw into the same field
# on the same node, and the prefetch decides "this node has no picture"
# from a snapshot taken before it started drawing. Drawing a subtree takes
# long enough for the player to press 重繪 inside that window, so without a
# barrier the prefetch writes its snapshot back over a picture the player
# was *charged* for — and nothing anywhere reports it.


_CHILD_ID = "child-1"


class _RedrawWhileDrawing:
    """A renderer that lets a redraw land *inside* the prefetch's draw.

    Real code, real ordering: the hook runs while the prefetch is awaiting
    its own ``generate``, which is exactly where the stale snapshot the
    prefetch is holding goes out of date.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.on_first_call = None

    async def generate(
        self, *, positive: str, aspect: str = "landscape", character=None,
    ) -> bytes:  # noqa: ANN001, ARG002
        self.calls += 1
        if self.calls == 1 and self.on_first_call is not None:
            hook, self.on_first_call = self.on_first_call, None
            await hook()
        covered_call_receipt(
            {BILLING_COVERED_HEADER_NAME: "1"},
        ).defer_delivery()
        return f"PNG-{self.calls}".encode()


async def _seed_child(fixture: _Fixture) -> DramaNode:
    """One picture-less child under the root — what a prefetch walks into."""
    child = DramaNode.create_child(
        id=_CHILD_ID,
        drama_id=_DRAMA_ID,
        parent_node_id=_ROOT_ID,
        depth=1,
        tone="dark",
        title="第二幕",
        summary="她把那截電線舉到燈下。",
        appearing_character_ids=("c-a", "c-b"),
    )
    await fixture.repo.save_node(child)
    return child


async def test_prefetch_does_not_overwrite_the_redraw_the_player_paid_for() -> None:
    renderer = _RedrawWhileDrawing()
    fixture = _Fixture(scene_image=renderer, image_prefetch_depth=2)
    root = await fixture.seed()
    await _seed_child(fixture)
    paid: dict[str, str] = {}

    async def redraw() -> None:
        node = await fixture.service.regenerate_node_image(
            _CHILD_ID, drama_id=_DRAMA_ID, user_id=_USER,
        )
        paid["image_path"] = node.image_path

    renderer.on_first_call = redraw

    drama = await fixture.repo.get(_DRAMA_ID)
    await fixture.service._prefetch_subtree_images(root, drama, 1)
    await fixture.drain()

    # The prefetch drew (call 1) and the redraw drew (call 2) — the point
    # is which one is still on the node.
    assert renderer.calls == 2
    stored = await fixture.repo.get_node(_CHILD_ID)
    assert stored is not None
    assert stored.image_path == paid["image_path"]
    # And the player was charged exactly once, for the picture they kept.
    assert fixture.client.action_keys == [ACTION_BRANCHING_DRAMA_SCENE_REGEN]


async def test_prefetch_skips_a_node_that_is_being_redrawn_right_now() -> None:
    """No point spending a Gateway call on a picture about to be replaced.

    The nesting is the other way round from the test above: here the
    *redraw* is the one in flight, and the prefetch walks past mid-draw.
    """
    renderer = _RedrawWhileDrawing()
    fixture = _Fixture(scene_image=renderer, image_prefetch_depth=2)
    root = await fixture.seed()
    await _seed_child(fixture)
    drama = await fixture.repo.get(_DRAMA_ID)

    async def prefetch_walks_past() -> None:
        await fixture.service._prefetch_subtree_images(root, drama, 1)

    renderer.on_first_call = prefetch_walks_past

    redrawn = await fixture.service.regenerate_node_image(
        _CHILD_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )
    await fixture.drain()

    # The prefetch saw a picture-less node and still drew nothing: the
    # only call is the redraw the player pressed for.
    assert renderer.calls == 1
    stored = await fixture.repo.get_node(_CHILD_ID)
    assert stored is not None
    assert stored.image_path == redrawn.image_path


async def test_initial_images_yield_to_a_picture_that_landed_first() -> None:
    """The same barrier on the creation-time path, which walks by depth."""
    renderer = _RedrawWhileDrawing()
    fixture = _Fixture(scene_image=renderer, image_prefetch_depth=1)
    await fixture.seed(with_image=False)
    paid: dict[str, str] = {}

    async def redraw() -> None:
        node = await fixture.service.regenerate_node_image(
            _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
        )
        paid["image_path"] = node.image_path

    renderer.on_first_call = redraw

    drama = await fixture.repo.get(_DRAMA_ID)
    ctx = await fixture.service._make_context(drama)
    await fixture.service._generate_initial_images(ctx)
    await fixture.drain()

    stored = await fixture.repo.get_node(_ROOT_ID)
    assert stored is not None
    assert stored.image_path == paid["image_path"]


# ── the route ────────────────────────────────────────────────────────


@dataclass
class _ServiceStub:
    """Only the two calls the route makes before delegating."""

    node: DramaNode | None
    error: Exception | None = None
    result: DramaNode | None = None
    calls: list[dict] | None = None

    async def get(self, drama_id: str) -> BranchingDrama:  # noqa: ARG002
        return (
            BranchingDrama.create_pending(
                id=_DRAMA_ID,
                character_ids=["c-a", "c-b"],
                prompt="在咖啡廳發生的故事",
                total_segments=3,
            )
            .with_title("測試劇場")
            .with_status(STATUS_READY)
        )

    async def get_node(self, node_id: str) -> DramaNode | None:  # noqa: ARG002
        return self.node

    async def regenerate_node_image(self, node_id: str, **kwargs):  # noqa: ANN003
        if self.calls is not None:
            self.calls.append({"node_id": node_id, **kwargs})
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@dataclass
class _Container:
    branching_drama_service: object
    character_service = None
    operator_profile_service = None


def _http(service: _ServiceStub) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: _Container(service)
    app.dependency_overrides[get_current_user_id] = lambda: _USER
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


_PATH = f"/api/v1/branching-dramas/{_DRAMA_ID}/nodes/{_ROOT_ID}/image/regenerate"


def _node(image_path: str | None = None, drama_id: str = _DRAMA_ID) -> DramaNode:
    node = DramaNode.create_root(
        id=_ROOT_ID,
        drama_id=drama_id,
        title="序幕",
        summary="角色們在咖啡廳相遇。",
        appearing_character_ids=("c-a", "c-b"),
    )
    return node.with_image_path(image_path) if image_path else node


async def test_route_returns_the_redrawn_node() -> None:
    calls: list[dict] = []
    client = _http(
        _ServiceStub(
            node=_node(),
            result=_node("/media/branching-dramas/drama-1/root-1-abc123.png"),
            calls=calls,
        ),
    )

    response = client.post(_PATH, json={"quoted_price_cr": 3.0})

    assert response.status_code == 200
    assert response.json()["image_path"].endswith("root-1-abc123.png")
    assert calls == [
        {"node_id": _ROOT_ID, "drama_id": _DRAMA_ID, "user_id": _USER},
    ]


async def test_route_takes_no_body_at_all() -> None:
    """Self-host quotes nothing; the optional body must stay optional."""
    client = _http(
        _ServiceStub(node=_node(), result=_node("/media/x-1.png")),
    )

    assert client.post(_PATH).status_code == 200


async def test_route_404s_a_node_belonging_to_another_drama() -> None:
    client = _http(_ServiceStub(node=_node(drama_id="drama-other")))

    response = client.post(_PATH)

    assert response.status_code == 404


async def test_route_404s_a_missing_node() -> None:
    client = _http(_ServiceStub(node=None))

    assert client.post(_PATH).status_code == 404


async def test_route_503s_when_this_deployment_cannot_draw() -> None:
    client = _http(
        _ServiceStub(node=_node(), error=SceneImageUnavailable("no renderer")),
    )

    assert client.post(_PATH).status_code == 503


async def test_route_409s_a_redraw_already_running() -> None:
    client = _http(
        _ServiceStub(
            node=_node(), error=SceneRegenerationInProgress("already drawing"),
        ),
    )

    assert client.post(_PATH).status_code == 409


async def test_route_502s_a_broken_renderer() -> None:
    client = _http(
        _ServiceStub(node=_node(), error=SceneRegenerationFailed("comfy down")),
    )

    assert client.post(_PATH).status_code == 502


async def test_route_402s_an_empty_wallet() -> None:
    client = _http(_ServiceStub(node=_node(), error=_refusal()))

    response = client.post(_PATH)

    assert response.status_code == 402
    assert response.json()["detail"]["code"] == INSUFFICIENT_CREDITS_CODE


# ── NF4: a redraw is a press, and a press is an interaction ──────────


async def test_redraw_marks_the_cast_engaged() -> None:
    """The player pressed 重繪 and paid for it — evidence they are here, on
    the same footing as an advance or a chat turn."""
    from kokoro_link.infrastructure.repositories.in_memory_characters import (
        InMemoryCharacterRepository,
    )

    characters = InMemoryCharacterRepository()
    for letter in ("a", "b"):
        await characters.save(_character(letter))
    fixture = _Fixture(characters=characters)
    await fixture.seed()
    assert (await characters.get("c-a")).state.last_active_at is None

    await fixture.service.regenerate_node_image(
        _ROOT_ID, drama_id=_DRAMA_ID, user_id=_USER,
    )

    assert (await characters.get("c-a")).state.last_active_at is not None
    assert (await characters.get("c-b")).state.last_active_at is not None
