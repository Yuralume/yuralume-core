"""Cross-replica safety for branching-drama lazy child generation.

``_ensure_children_exist`` is the *interactive* lazy-generation path (a
player pressed "advance" and is waiting). It used to be guarded only by a
process-local ``asyncio.Lock``, so with the hosted ``api`` role scaled to N
replicas two requests landing on two replicas both read an empty child set
and both generated a whole layer — the same parent ending up with two
duplicate sets of children.

These tests wire TWO service instances over ONE repository and ONE
``InMemoryBackgroundCoordinatorLease`` backend (distinct ``owner_id`` per
"replica", exactly how the SA adapter behaves across processes) and assert:

1. concurrent lazy generation produces exactly one layer;
2. when the tree is held by another replica, the waiting request re-checks
   briefly and returns the other replica's children once they land;
3. if they do not land inside the bounded window, the caller gets an explicit
   "generation in progress" signal instead of a duplicate layer;
4. ``advance_session`` propagates that signal instead of ending the session.
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
    BranchingGenerationInProgress,
)
from kokoro_link.application.services.studio_execution_lease import (
    StudioExecutionLease,
)
from kokoro_link.domain.entities.branching_drama import (
    TONE_DARK,
    TONE_NEUTRAL,
    TONE_SUNNY,
    DramaNode,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
    InMemoryBranchingDramaRepository,
)
from tests.unit.test_branching_drama_service import (
    _CharServiceStub,
    _NullBriefBuilder,
    _ScriptedDirector,
    _ScriptedPlanner,
    _make_character,
)


_POLL_INTERVAL = 0.01
_POLL_ATTEMPTS = 30


class _SlowPlanner(_ScriptedPlanner):
    """Scripted planner with a real suspension point.

    Without an await inside the planner the two "replicas" would run to
    completion one after another and never actually interleave.
    """

    async def plan_children(
        self, *, prompt, briefs, parent_summary,
        path_context, depth, total_segments,
    ):  # noqa: ANN001, ANN201
        await asyncio.sleep(0.02)
        return await super().plan_children(
            prompt=prompt,
            briefs=briefs,
            parent_summary=parent_summary,
            path_context=path_context,
            depth=depth,
            total_segments=total_segments,
        )


def _service(
    repo: InMemoryBranchingDramaRepository,
    planner: _ScriptedPlanner,
    backend: InMemoryBackgroundCoordinatorLease | None,
    *,
    owner_id: str,
    chars,
    poll_attempts: int = _POLL_ATTEMPTS,
) -> BranchingDramaService:
    lease = (
        StudioExecutionLease(backend, owner_id=owner_id, ttl_seconds=100)
        if backend is not None
        else None
    )
    return BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(by_id={c.id: c for c in chars}),
        brief_builder=_NullBriefBuilder(),
        planner=planner,
        director=_ScriptedDirector(),
        execution_lease=lease,
        lease_heartbeat_interval_seconds=1000,
        lazy_layer_poll_interval_seconds=_POLL_INTERVAL,
        lazy_layer_poll_attempts=poll_attempts,
    )


async def _drain(service: BranchingDramaService) -> None:
    tasks = list(service._tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _seed_tree(service: BranchingDramaService, repo):
    """Create a drama; return ``(drama, a depth-1 node with no children)``."""
    drama = await service.create(
        character_ids=["c-a", "c-b"],
        prompt="跨實例懶生成",
        total_segments=4,
    )
    for _ in range(300):
        root = await repo.get_root_node(drama.id)
        if root is not None and await repo.get_children(root.id):
            break
        await asyncio.sleep(0.01)
    await _drain(service)
    root = await repo.get_root_node(drama.id)
    children = await repo.get_children(root.id)
    assert len(children) == 3
    node = children[0]
    assert await repo.get_children(node.id) == []
    return await repo.get(drama.id), node


def _other_children(drama_id: str, parent: DramaNode) -> list[DramaNode]:
    return [
        DramaNode.create_child(
            drama_id=drama_id,
            parent_node_id=parent.id,
            depth=parent.depth + 1,
            tone=tone,
            title=f"other-{tone}",
            summary="由另一個實例生成",
            appearing_character_ids=("c-a", "c-b"),
        )
        for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL)
    ]


@pytest.mark.asyncio
async def test_two_replicas_generate_one_child_layer() -> None:
    repo = InMemoryBranchingDramaRepository()
    planner = _SlowPlanner()
    backend = InMemoryBackgroundCoordinatorLease()
    chars = [_make_character("a"), _make_character("b")]

    replica_a = _service(repo, planner, backend, owner_id="A", chars=chars)
    replica_b = _service(repo, planner, backend, owner_id="B", chars=chars)

    drama, node = await _seed_tree(replica_a, repo)
    calls_before = planner.children_calls

    results = await asyncio.gather(
        replica_a._ensure_children_exist(node, drama),
        replica_b._ensure_children_exist(node, drama),
        return_exceptions=True,
    )

    persisted = await repo.get_children(node.id)
    assert len(persisted) == 3, (
        f"expected exactly one lazily generated layer, got {len(persisted)}"
    )
    assert {n.tone for n in persisted} == {TONE_DARK, TONE_SUNNY, TONE_NEUTRAL}
    # Exactly one replica ran the planner for this parent.
    assert planner.children_calls == calls_before + 1
    # Neither replica raised: the loser re-checked and saw the winner's layer.
    ids = {n.id for n in persisted}
    for outcome in results:
        assert not isinstance(outcome, BaseException), outcome
        assert {n.id for n in outcome} == ids

    await _drain(replica_a)
    await _drain(replica_b)


@pytest.mark.asyncio
async def test_bounded_recheck_returns_other_replicas_children() -> None:
    """Lease held elsewhere → poll briefly, return their layer once it lands."""
    repo = InMemoryBranchingDramaRepository()
    planner = _SlowPlanner()
    backend = InMemoryBackgroundCoordinatorLease()
    chars = [_make_character("a"), _make_character("b")]

    replica_a = _service(repo, planner, backend, owner_id="A", chars=chars)
    drama, node = await _seed_tree(replica_a, repo)
    calls_before = planner.children_calls

    # Another replica currently owns the tree.
    other = StudioExecutionLease(backend, owner_id="other", ttl_seconds=100)
    assert await other.acquire(drama.id) is not None

    async def land_children_later() -> None:
        await asyncio.sleep(_POLL_INTERVAL * 3)
        await repo.add_nodes(_other_children(drama.id, node))

    lander = asyncio.create_task(land_children_later())
    children = await replica_a._ensure_children_exist(node, drama)
    await lander

    assert len(children) == 3
    assert all(c.title.startswith("other-") for c in children)
    # We must NOT have generated a competing layer.
    assert planner.children_calls == calls_before
    assert len(await repo.get_children(node.id)) == 3

    await _drain(replica_a)


@pytest.mark.asyncio
async def test_bounded_recheck_gives_up_with_in_progress_signal() -> None:
    """Nothing lands inside the window → explicit in-progress error, no dup."""
    repo = InMemoryBranchingDramaRepository()
    planner = _SlowPlanner()
    backend = InMemoryBackgroundCoordinatorLease()
    chars = [_make_character("a"), _make_character("b")]

    replica_a = _service(
        repo, planner, backend, owner_id="A", chars=chars, poll_attempts=3,
    )
    drama, node = await _seed_tree(replica_a, repo)
    calls_before = planner.children_calls

    other = StudioExecutionLease(backend, owner_id="other", ttl_seconds=100)
    assert await other.acquire(drama.id) is not None

    with pytest.raises(BranchingGenerationInProgress) as caught:
        await replica_a._ensure_children_exist(node, drama)

    assert caught.value.drama_id == drama.id
    assert caught.value.node_id == node.id
    assert planner.children_calls == calls_before
    assert await repo.get_children(node.id) == []

    await _drain(replica_a)


@pytest.mark.asyncio
async def test_lease_less_service_keeps_single_process_behaviour() -> None:
    """No lease wired (self-host) → the historical lock-only path, unchanged."""
    repo = InMemoryBranchingDramaRepository()
    planner = _SlowPlanner()
    chars = [_make_character("a"), _make_character("b")]

    service = _service(repo, planner, None, owner_id="solo", chars=chars)
    drama, node = await _seed_tree(service, repo)
    calls_before = planner.children_calls

    first, second = await asyncio.gather(
        service._ensure_children_exist(node, drama),
        service._ensure_children_exist(node, drama),
    )

    assert len(first) == 3 and len(second) == 3
    assert planner.children_calls == calls_before + 1
    assert len(await repo.get_children(node.id)) == 3

    await _drain(service)


@pytest.mark.asyncio
async def test_advance_session_propagates_in_progress_without_ending() -> None:
    """A busy tree must never be mistaken for "no children → already ending"."""
    repo = InMemoryBranchingDramaRepository()
    planner = _SlowPlanner()
    backend = InMemoryBackgroundCoordinatorLease()
    chars = [_make_character("a"), _make_character("b")]

    service = _service(repo, planner, backend, owner_id="A", chars=chars)
    drama, _node = await _seed_tree(service, repo)
    session, _root, _narration = await service.start_session(drama.id)
    await _drain(service)

    async def _busy(node, drama_arg, **_kwargs):  # noqa: ANN001, ANN202
        raise BranchingGenerationInProgress(drama_arg.id, node.id)

    service._ensure_children_exist = _busy  # type: ignore[method-assign]

    with pytest.raises(BranchingGenerationInProgress):
        await service.advance_session(session.id)

    reloaded = await repo.get_session(session.id)
    assert reloaded is not None
    assert not reloaded.is_ended

    await _drain(service)
