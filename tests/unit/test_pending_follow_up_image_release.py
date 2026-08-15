"""PF3 — a promised photo renders under the deployment's image ceiling.

PF1 let a fulfilment call tools, and one of those tools drives a GPU. The
release job it runs inside is priority 1 and deliberately exempt from every
background ceiling, because what it owes is a message a player is waiting for.
Those two facts together meant a burst of promises falling due at 21:00 could
put an unbounded number of renders on the card at once.

The split here keeps both properties true at once:

* a **text** fulfilment stays exempt — an owed message never queues behind
  somebody else's picture (``test_text_fulfilment_...``);
* an **image** fulfilment is handed to a kind the claim filter counts against
  ``BG_CAP_IMAGE``, so it waits for the slot instead of racing for it.

Crucially the hand-off is not a prediction made at enqueue time: it happens
*after* the composer has chosen the tool, so the kind always tells the truth.
And it is never a refusal — the tool is still offered, still runs, just
somewhere it can be counted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.services.composer_tool_loop import ComposerToolLoop
from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
from kokoro_link.application.services.pending_follow_up_release import (
    PendingFollowUpReleaseEnqueuer,
    PendingFollowUpReleaseHandler,
)
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundJobSpec,
    ClaimedJob,
    JobStatus,
)
from kokoro_link.contracts.due_jobs import (
    DEFAULT_CAPABILITY_CAPS,
    FEED_COMPOSE_KIND,
    PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND,
    PENDING_FOLLOW_UP_RELEASE_KIND,
    JobCapability,
)
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeOutput,
    PendingFollowUpComposerPort,
)
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposeOutput,
    ScheduledPromiseComposerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.tool_call import ToolCall
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.tools.fake_tools import EchoTool, FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
IMAGE_CAPS = {JobCapability.IMAGE.value: DEFAULT_CAPABILITY_CAPS["image"]}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

def _character(cid: str = "char-1", *, allowed: tuple[str, ...] = ()) -> Character:
    return replace(
        Character.create(
            name="Aki", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50, energy=70,
            ),
        ),
        id=cid,
        allowed_tools=allowed,
    )


def _promise_row(character_id: str = "char-1") -> PendingFollowUp:
    return PendingFollowUp.new_promise(
        character_id=character_id,
        conversation_id="conv-1",
        promise_intent="晚點傳一張現在的照片",
        scheduled_for=NOW - timedelta(minutes=1),
        source_message_content="等等拍一張給我看",
        now=NOW - timedelta(hours=2),
    )


@dataclass
class _StubCharacterRepo:
    characters: dict[str, Character]

    async def get(self, character_id: str) -> Character | None:
        return self.characters.get(character_id)

    async def list(self) -> list[Character]:  # pragma: no cover
        return list(self.characters.values())

    async def save(self, c: Character) -> None:  # pragma: no cover
        self.characters[c.id] = c

    async def delete(self, c: str) -> bool:  # pragma: no cover
        return self.characters.pop(c, None) is not None


class _StubScheduleService:
    async def ensure_schedule(self, character):
        return object()

    def resolve_current(self, schedule, *, now):
        return None, [], None


class _UnusedBusyComposer(PendingFollowUpComposerPort):
    async def compose(self, payload):  # pragma: no cover - promise rows only
        return PendingFollowUpComposeOutput(content_text="should not run")


class _PhotoPromiseComposer(ScheduledPromiseComposerPort):
    """Asks for a tool on pass 1, writes prose once results are in."""

    def __init__(
        self,
        *,
        tool_calls: tuple[ToolCall, ...],
        final_text: str = "拍好了，你看~",
    ) -> None:
        self._tool_calls = tool_calls
        self._final_text = final_text
        self.calls: list[ScheduledPromiseComposeInput] = []

    async def compose(self, payload):
        self.calls.append(payload)
        if payload.tool_results:
            return ScheduledPromiseComposeOutput(content_text=self._final_text)
        if payload.available_tools:
            return ScheduledPromiseComposeOutput(
                content_text="", tool_calls=self._tool_calls,
            )
        return ScheduledPromiseComposeOutput(content_text="沒有工具就直接寫")


class _SpyProactiveDispatcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def deliver_pre_composed(
        self, *, character_id, text, trigger, reason="", attachments=(), now=None,
    ):
        self.calls.append({"text": text, "attachments": tuple(attachments)})
        return ProactiveAttempt.record(
            character_id=character_id, trigger=trigger,
            outcome=ProactiveOutcome.SENT, reason=reason or "stub",
            message=text, now=now or NOW,
        )


async def _leased_queue() -> tuple[
    InMemoryBackgroundJobQueue, InMemoryBackgroundCoordinatorLease, int,
]:
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=3600, now=NOW,
    )
    assert epoch is not None
    return queue, lease, epoch


def _build_dispatcher(
    *,
    repo: InMemoryPendingFollowUpRepository,
    character: Character,
    composer: ScheduledPromiseComposerPort,
    proactive: _SpyProactiveDispatcher,
    enqueuer: PendingFollowUpReleaseEnqueuer | None,
    tools: list | None = None,
    capability_caps: dict[str, int] | None = None,
    busy_composer: PendingFollowUpComposerPort | None = None,
    visible_slot_port=None,  # noqa: ANN001 - VisibleSlotPort-shaped stub
) -> PendingFollowUpDispatcher:
    registry = InMemoryToolRegistry(tools if tools is not None else [FakeImageTool()])
    loop = ComposerToolLoop(
        tool_registry=registry,
        tool_orchestrator=ToolOrchestrator(
            registry=registry,
            invocation_repository=InMemoryToolInvocationRepository(),
        ),
        public_base_url="https://yura.example",
        capability_caps=capability_caps,
    )
    dispatcher = PendingFollowUpDispatcher(
        repository=repo,
        composer=busy_composer or _UnusedBusyComposer(),
        proactive_dispatcher=proactive,
        character_repository=_StubCharacterRepo({character.id: character}),
        schedule_service=_StubScheduleService(),
        scheduled_promise_composer=composer,
        tool_loop=loop,
        visible_slot_port=visible_slot_port,
    )
    if enqueuer is not None:
        dispatcher.set_capability_release_enqueuer(enqueuer)
    return dispatcher


def _claimed(row: PendingFollowUp, kind: str) -> ClaimedJob:
    return ClaimedJob(
        id=f"job-{kind}", kind=kind, attempt_count=1, max_attempts=5,
        fencing_epoch=1, idempotency_key=f"follow_up:{row.id}:0",
        priority=1, due_at=row.scheduled_for, payload_version=1,
        payload={"follow_up_id": row.id}, lease_owner="w1",
        lease_until=NOW + timedelta(seconds=120), character_id=row.character_id,
    )


async def _kind_counts(queue: InMemoryBackgroundJobQueue) -> dict[str, int]:
    return (await queue.stats(now=NOW)).kind_counts


# --------------------------------------------------------------------------- #
# The hand-off
# --------------------------------------------------------------------------- #

async def test_promised_photo_is_handed_to_the_image_capped_kind() -> None:
    """The GPU half leaves the exempt job and becomes a countable one.

    Nothing is sent this pass and — the part that matters — nothing is lost:
    the row stays releasable and a job that WILL run the render is queued."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    composer = _PhotoPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
    )
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=composer,
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
    )

    released = await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    )

    assert released is False
    assert proactive.calls == []  # a half-kept promise is not sent
    assert len(composer.calls) == 1  # the second pass belongs to the image job
    counts = await _kind_counts(queue)
    assert counts.get(PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND) == 1
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED  # still owed, not failed


async def test_the_image_job_runs_the_render_and_ships_the_photo() -> None:
    """The other half. This job WAS claimed under the image ceiling, so it
    runs the tool itself — deferring again would be an infinite hand-off."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    composer = _PhotoPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
    )
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=composer,
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
    )
    handler = PendingFollowUpReleaseHandler(
        repository=repo, dispatcher=dispatcher,
    )

    result = await handler.handle(
        _claimed(row, PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND), now=NOW,
    )

    assert result.executed is True
    assert len(composer.calls) == 2  # tool ran between the two passes
    assert proactive.calls[0]["text"] == "拍好了，你看~"
    assert proactive.calls[0]["attachments"][0].url == (
        "https://yura.example/uploads/stub/fake.png"
    )
    # No further deferral was minted — the render happened here.
    assert PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND not in await _kind_counts(queue)


async def test_a_text_fulfilment_never_touches_the_image_queue() -> None:
    """The common case: a promise to look something up. Same deferring
    caller, same wiring — the tool consumes nothing scarce, so it just runs."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    composer = _PhotoPromiseComposer(
        tool_calls=(ToolCall(name="echo", arguments={"text": "夏祭抽選"}),),
        final_text="查到了，七月一號開始。",
    )
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("echo",)),
        composer=composer,
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
        tools=[EchoTool()],
    )

    released = await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    )

    assert released is True
    assert proactive.calls[0]["text"] == "查到了，七月一號開始。"
    assert await _kind_counts(queue) == {}


async def test_with_no_queue_to_defer_to_the_render_runs_inline() -> None:
    """Self-host. There is no distributed claim to schedule against, so the
    hand-off must degrade to PF1's behaviour rather than to a broken promise."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    composer = _PhotoPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
    )
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=composer,
        proactive=proactive,
        enqueuer=None,  # embedded: nothing to hand off to
    )

    released = await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    )

    assert released is True
    assert len(composer.calls) == 2
    assert proactive.calls[0]["attachments"][0].url.endswith("fake.png")


async def test_a_leaderless_deployment_falls_back_to_running_it_inline() -> None:
    """The enqueuer is wired but no coordinator holds the lease: the deferral
    would vanish into a queue nobody drains. Keep the promise instead."""
    lease = InMemoryBackgroundCoordinatorLease()  # never acquired
    queue = InMemoryBackgroundJobQueue(lease=lease)
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    composer = _PhotoPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
    )
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=composer,
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
    )

    released = await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    )

    assert released is True
    assert proactive.calls[0]["attachments"][0].url.endswith("fake.png")


async def test_re_deferring_is_idempotent_not_a_second_render_job() -> None:
    """The reconcile re-mints the text half every sweep while the image half
    waits for its slot. Each replay must find the work already scheduled and
    still refuse to run the tool locally — one queued render, not N."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    composer = _PhotoPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
    )
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=composer,
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
    )

    for _ in range(3):
        current = await repo.get(row.id)
        assert current is not None
        assert await dispatcher.release_row(
            current, now=NOW, defer_capabilities=True,
        ) is False

    assert (await _kind_counts(queue))[PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND] == 1
    assert proactive.calls == []


# --------------------------------------------------------------------------- #
# A closed capability (BG_CAP_IMAGE=0)
# --------------------------------------------------------------------------- #

CLOSED_IMAGE_CAPS = {JobCapability.IMAGE.value: 0}


async def test_a_closed_image_cap_answers_in_words_instead_of_stalling() -> None:
    """``BG_CAP_IMAGE=0`` is the operator saying "no background renders here".

    The queue obeys it by never claiming that kind — so minting the job would
    strand the promise forever (never claimed, never attempted, never dead)
    while every 15-minute reconcile burned another pass 1. The tool is
    therefore not on the table at all, and the character keeps the promise the
    only way this deployment can: in words."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    composer = _PhotoPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
    )
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=composer,
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
        capability_caps=CLOSED_IMAGE_CAPS,
    )

    released = await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    )

    assert released is True
    assert len(composer.calls) == 1
    assert composer.calls[0].available_tools == ()  # never offered
    assert proactive.calls[0]["text"] == "沒有工具就直接寫"
    assert await _kind_counts(queue) == {}  # no unclaimable job was minted
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED


async def test_a_capped_but_open_image_slot_still_gets_the_tool() -> None:
    """The other side of the same knob: cap >= 1 changes nothing at all —
    the tool is offered, chosen, and handed to the queue as before."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    composer = _PhotoPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
    )
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=composer,
        proactive=_SpyProactiveDispatcher(),
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
        capability_caps=IMAGE_CAPS,
    )

    assert await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    ) is False
    assert [t.name for t in composer.calls[0].available_tools] == ["fake_image"]
    assert (await _kind_counts(queue))[PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND] == 1


async def test_a_closed_cap_does_not_reach_the_self_host_release() -> None:
    """Embedded runs its tools inline; it never asks the queue for anything,
    so the background ceiling is not its sentence to obey. The promised photo
    is taken exactly as it was before the knob was ever read."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    composer = _PhotoPromiseComposer(
        tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
    )
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=composer,
        proactive=proactive,
        enqueuer=None,  # embedded
        capability_caps=CLOSED_IMAGE_CAPS,
    )

    assert await dispatcher.release_row(row, now=NOW) is True
    assert [t.name for t in composer.calls[0].available_tools] == ["fake_image"]
    assert proactive.calls[0]["attachments"][0].url.endswith("fake.png")


# --------------------------------------------------------------------------- #
# Two ways for the queue to say "no job for you"
# --------------------------------------------------------------------------- #

class _RefusingQueue:
    """A queue whose ``enqueue`` returns ``None`` — the ambiguous answer.

    The port uses one value for "an active job already covers this row" and
    for "your coordinator epoch is stale, nothing was inserted"."""

    def __init__(self) -> None:
        self.specs: list[BackgroundJobSpec] = []

    async def enqueue(self, spec: BackgroundJobSpec, *, now: datetime) -> None:
        self.specs.append(spec)
        return None


class _TakeoverLease:
    """Reports one epoch, then a newer one — a coordinator changing hands
    between the enqueuer's fencing read and the queue's own re-read."""

    def __init__(self, epochs: list[int]) -> None:
        self._epochs = list(epochs)
        self.reads = 0

    async def current(self, name: str) -> tuple[str, int, datetime]:
        epoch = self._epochs[min(self.reads, len(self._epochs) - 1)]
        self.reads += 1
        return ("coord", epoch, NOW + timedelta(hours=1))


async def test_a_fencing_refusal_is_not_mistaken_for_scheduled_work() -> None:
    """Nothing was inserted, so nobody will run the render. Reporting the
    deferral as taken would park the row on a job that does not exist and make
    the player wait a reconcile for a promise we could keep right now."""
    enqueuer = PendingFollowUpReleaseEnqueuer(
        queue=_RefusingQueue(), coordinator_lease=_TakeoverLease([7, 8]),
    )

    taken = await enqueuer.defer_capability(
        _promise_row(), capability=JobCapability.IMAGE.value, now=NOW,
    )

    assert taken is False  # → the caller runs the tool inline


async def test_a_duplicate_refusal_is_still_work_already_scheduled() -> None:
    """The benign half of the same ``None``: same epoch, still live, so the
    queue can only have refused on its active-idempotency index — the render
    IS queued and this pass must send nothing."""
    enqueuer = PendingFollowUpReleaseEnqueuer(
        queue=_RefusingQueue(), coordinator_lease=_TakeoverLease([7, 7]),
    )

    taken = await enqueuer.defer_capability(
        _promise_row(), capability=JobCapability.IMAGE.value, now=NOW,
    )

    assert taken is True


# --------------------------------------------------------------------------- #
# Parking must not overwrite a newer decision
# --------------------------------------------------------------------------- #

class _ResolvingRaceComposer(_PhotoPromiseComposer):
    """Asks for the photo — and while "composing", the image job that took
    the previous hand-off finishes, sends, and resolves the same row."""

    def __init__(self, repo: InMemoryPendingFollowUpRepository, row_id: str) -> None:
        super().__init__(
            tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
        )
        self._repo = repo
        self._row_id = row_id

    async def compose(self, payload):
        stored = await self._repo.get(self._row_id)
        assert stored is not None
        await self._repo.save(
            stored.marked_resolved(message_text="拍好了，你看~", now=NOW),
        )
        return await super().compose(payload)


async def test_parking_never_resurrects_a_row_the_image_job_already_sent() -> None:
    """PF3 keeps both halves alive over one row on purpose, and ``save`` is a
    version-less upsert. A reconcile-minted text half that started before the
    image half finished must not write its pre-compose snapshot back — that
    would file an already-delivered message as never sent."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=_ResolvingRaceComposer(repo, row.id),
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
    )

    released = await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    )

    assert released is False
    assert proactive.calls == []  # this half sends nothing either way
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED
    assert stored.resolved_message == "拍好了，你看~"
    assert stored.resolved_at is not None


DELIVERED_BY_THE_IMAGE_HALF = "拍好了，你看~"


class _ResolveThenCrashComposer(ScheduledPromiseComposerPort):
    """The image half resolves the row mid-compose, then this pass dies."""

    def __init__(self, repo: InMemoryPendingFollowUpRepository, row_id: str) -> None:
        self._repo = repo
        self._row_id = row_id

    async def compose(self, payload):
        await _resolve_elsewhere(self._repo, self._row_id)
        raise RuntimeError("provider returned 502")


class _ResolveThenEmptyComposer(ScheduledPromiseComposerPort):
    """Same race, the other fail-soft exit: the model returns nothing."""

    def __init__(self, repo: InMemoryPendingFollowUpRepository, row_id: str) -> None:
        self._repo = repo
        self._row_id = row_id

    async def compose(self, payload):
        await _resolve_elsewhere(self._repo, self._row_id)
        return ScheduledPromiseComposeOutput(content_text="")


class _ResolveThenWriteComposer(ScheduledPromiseComposerPort):
    """Same race, but this pass DOES produce prose — it just arrives after
    the other executor already sent its own."""

    def __init__(self, repo: InMemoryPendingFollowUpRepository, row_id: str) -> None:
        self._repo = repo
        self._row_id = row_id

    async def compose(self, payload):
        await _resolve_elsewhere(self._repo, self._row_id)
        return ScheduledPromiseComposeOutput(content_text="我後來才寫好的字")


class _ResolveThenEmptyBusyComposer(PendingFollowUpComposerPort):
    """The busy-defer kind's copy of the same exit (both kinds share the
    write-back path, and a fix that only reached one would rot)."""

    def __init__(self, repo: InMemoryPendingFollowUpRepository, row_id: str) -> None:
        self._repo = repo
        self._row_id = row_id

    async def compose(self, payload):
        await _resolve_elsewhere(self._repo, self._row_id)
        return PendingFollowUpComposeOutput(content_text="")


async def _resolve_elsewhere(
    repo: InMemoryPendingFollowUpRepository, row_id: str,
) -> None:
    """What the image half does while this pass is still composing: finish
    the render, send, and mark the shared row resolved."""
    stored = await repo.get(row_id)
    assert stored is not None
    await repo.save(
        stored.marked_resolved(message_text=DELIVERED_BY_THE_IMAGE_HALF, now=NOW),
    )


async def _assert_the_delivered_message_survived(
    repo: InMemoryPendingFollowUpRepository, row_id: str,
) -> None:
    stored = await repo.get(row_id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED
    assert stored.resolved_message == DELIVERED_BY_THE_IMAGE_HALF
    assert stored.resolved_at is not None


async def test_a_crashed_compose_never_resurrects_a_row_already_sent() -> None:
    """The failure exits write back the same pre-compose snapshot parking
    does, and the race is the same one: PF3 keeps a text half and an image
    half alive over one row, ``save`` is a version-less upsert, and the
    reconcile re-mints the text half every 15 minutes while the image half
    waits for its slot. A 5xx on that replay must not file the message the
    player already has as never sent — which would also re-render it."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=_ResolveThenCrashComposer(repo, row.id),
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
    )

    released = await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    )

    assert released is False
    assert proactive.calls == []
    await _assert_the_delivered_message_survived(repo, row.id)


async def test_an_empty_compose_never_resurrects_a_row_already_sent() -> None:
    """The other fail-soft exit, same window, same consequence."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=_ResolveThenEmptyComposer(repo, row.id),
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
    )

    released = await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    )

    assert released is False
    assert proactive.calls == []
    await _assert_the_delivered_message_survived(repo, row.id)


async def test_a_busy_defer_empty_compose_never_resurrects_a_row_already_sent(
) -> None:
    """The busy-defer kind reaches the identical exits through its own
    branch, so it gets its own proof rather than trusting a shared helper."""
    from kokoro_link.domain.entities.pending_follow_up import (
        PendingFollowUpMessage,
    )

    repo = InMemoryPendingFollowUpRepository()
    row = PendingFollowUp.new(
        character_id="char-1",
        conversation_id="conv-1",
        first_message=PendingFollowUpMessage.new(content="嗨"),
        brief_reply="先回，等我忙完",
        defer_reason="會議中",
        scheduled_for=NOW - timedelta(minutes=5),
    )
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=()),
        composer=_PhotoPromiseComposer(tool_calls=()),  # promise path unused
        proactive=proactive,
        enqueuer=None,
        busy_composer=_ResolveThenEmptyBusyComposer(repo, row.id),
    )

    released = await dispatcher.release_row(row, now=NOW)

    assert released is False
    assert proactive.calls == []
    await _assert_the_delivered_message_survived(repo, row.id)


class _TakenSlots:
    """The at-most-once visible slot, already owned by the executor that
    actually sent this row's message."""

    def __init__(self) -> None:
        self.releases = 0

    async def claim(self, character_id: str, kind: str, key: str) -> bool:
        return False

    async def release(self, character_id: str, kind: str, key: str) -> None:
        self.releases += 1


async def test_a_taken_send_slot_does_not_restamp_the_delivered_message(
) -> None:
    """The slot guard already stops the duplicate SEND. What it never
    stopped is the write that follows it: stamping this pass's own,
    never-delivered prose over ``resolved_message`` records the wrong
    sentence as the one the player received."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=_ResolveThenWriteComposer(repo, row.id),
        proactive=proactive,
        enqueuer=None,
        visible_slot_port=_TakenSlots(),
    )

    released = await dispatcher.release_row(row, now=NOW)

    assert released is True  # nothing is owed any more
    assert proactive.calls == []  # the slot stopped the duplicate send
    await _assert_the_delivered_message_survived(repo, row.id)


# --------------------------------------------------------------------------- #
# ...and the same asymmetry from the other side: a concurrent QUEUED
# --------------------------------------------------------------------------- #

class _ParkThenWriteComposer(ScheduledPromiseComposerPort):
    """A *second* executor parks the shared row while this pass composes.

    The reconciler re-mints the text half of a promise whose image half is
    still queued behind the ceiling. That text half asks for the picture,
    finds this pass already holding the image slot, and parks — which is
    ``marked_failed``, i.e. the row goes back to ``queued``. Meanwhile this
    pass, the one that owns the render, is a moment away from sending it."""

    def __init__(self, repo: InMemoryPendingFollowUpRepository, row_id: str) -> None:
        self._repo = repo
        self._row_id = row_id

    async def compose(self, payload):
        stored = await self._repo.get(self._row_id)
        assert stored is not None
        await self._repo.save(
            stored.marked_failed(error="deferred to the image queue", now=NOW),
        )
        return ScheduledPromiseComposeOutput(
            content_text=DELIVERED_BY_THE_IMAGE_HALF,
        )


async def test_a_concurrent_park_cannot_bury_a_photo_that_did_go_out() -> None:
    """The delivered fact outranks a concurrent retry.

    Both executors legitimately see ``resolving`` — the claim that sets it is
    a version-less upsert too, so the write-back re-read cannot tell whose
    ``resolving`` it is reading. If a delivery write gave up on ``queued`` the
    way a retry write does, this row would end the pass on ``queued`` with an
    empty ``resolved_message`` **after the player already had the photo** —
    the exact bug the re-read exists to prevent, entered through the other
    door, and the next sweep would render it a second time."""
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=_ParkThenWriteComposer(repo, row.id),
        proactive=proactive,
        enqueuer=None,
    )

    released = await dispatcher.release_row(row, now=NOW)

    assert released is True
    assert [c["text"] for c in proactive.calls] == [DELIVERED_BY_THE_IMAGE_HALF]
    await _assert_the_delivered_message_survived(repo, row.id)


PARKED_BY_THE_OTHER_HALF = "the other half parked it first"


class _ParkElsewhereThenAskForImage(_PhotoPromiseComposer):
    """The mirror setup: somebody else parks the row, and *this* pass is the
    one that ends up with nothing to say — it wants the image queue."""

    def __init__(self, repo: InMemoryPendingFollowUpRepository, row_id: str) -> None:
        super().__init__(
            tool_calls=(ToolCall(name="fake_image", arguments={"scene": "廚房"}),),
        )
        self._repo = repo
        self._row_id = row_id

    async def compose(self, payload):
        stored = await self._repo.get(self._row_id)
        assert stored is not None
        await self._repo.save(
            stored.marked_failed(error=PARKED_BY_THE_OTHER_HALF, now=NOW),
        )
        return await super().compose(payload)


async def test_a_park_does_not_overwrite_another_executors_queued_row() -> None:
    """The asymmetry is deliberate, not an oversight: only the *delivery*
    write was allowed through ``queued``. A park carries nothing but "this
    pass didn't finish", which has no strength to overwrite anybody's record
    — and the retry it wants is already scheduled by the row being queued."""
    queue, lease, _ = await _leased_queue()
    repo = InMemoryPendingFollowUpRepository()
    row = _promise_row()
    await repo.add(row)
    proactive = _SpyProactiveDispatcher()
    dispatcher = _build_dispatcher(
        repo=repo,
        character=_character(allowed=("fake_image",)),
        composer=_ParkElsewhereThenAskForImage(repo, row.id),
        proactive=proactive,
        enqueuer=PendingFollowUpReleaseEnqueuer(
            queue=queue, coordinator_lease=lease,
        ),
    )

    released = await dispatcher.release_row(
        row, now=NOW, defer_capabilities=True,
    )

    assert released is False
    assert proactive.calls == []
    stored = await repo.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED
    assert stored.last_error == PARKED_BY_THE_OTHER_HALF


# --------------------------------------------------------------------------- #
# The ceiling itself (claim-time admission)
# --------------------------------------------------------------------------- #

async def _enqueue(
    queue: InMemoryBackgroundJobQueue, kind: str, epoch: int, key: str,
) -> None:
    assert await queue.enqueue(
        BackgroundJobSpec(
            kind=kind, idempotency_key=key, due_at=NOW - timedelta(minutes=1),
            fencing_epoch=epoch, priority=1,
        ),
        now=NOW,
    ) is not None


async def test_a_promised_render_waits_for_the_image_slot() -> None:
    """Two photo fulfilments come due together; the cap is one. Exactly one
    runs — the other stays queued and is claimed once the slot frees."""
    queue, _lease, epoch = await _leased_queue()
    await _enqueue(queue, PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND, epoch, "img-a")
    await _enqueue(queue, PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND, epoch, "img-b")

    claimed = await queue.claim(
        "w1", now=NOW, limit=10, lease_seconds=120, capability_caps=IMAGE_CAPS,
    )

    assert len(claimed) == 1
    stats = await queue.stats(now=NOW)
    assert stats.status_counts[JobStatus.QUEUED.value] == 1


async def test_a_promised_render_shares_the_ceiling_with_the_feed() -> None:
    """The cap models one card, so it has to be one cap. A feed picture in
    flight is exactly as much of a reason to wait as another promise is."""
    queue, _lease, epoch = await _leased_queue()
    await _enqueue(queue, FEED_COMPOSE_KIND, epoch, "feed-a")
    first = await queue.claim(
        "w1", now=NOW, limit=10, lease_seconds=120, capability_caps=IMAGE_CAPS,
    )
    assert [j.kind for j in first] == [FEED_COMPOSE_KIND]

    await _enqueue(queue, PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND, epoch, "img-a")
    blocked = await queue.claim(
        "w2", now=NOW, limit=10, lease_seconds=120, capability_caps=IMAGE_CAPS,
    )

    assert blocked == []


async def test_text_fulfilment_is_not_blocked_by_a_saturated_image_gate() -> None:
    """The constraint that rules out simply declaring the whole kind ``image``.

    A player's owed message must go out while the card is busy with somebody
    else's picture — the release kind is in no capped set at all."""
    queue, _lease, epoch = await _leased_queue()
    await _enqueue(queue, FEED_COMPOSE_KIND, epoch, "feed-a")
    await _enqueue(queue, PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND, epoch, "img-a")
    await _enqueue(queue, PENDING_FOLLOW_UP_RELEASE_KIND, epoch, "text-a")
    await _enqueue(queue, PENDING_FOLLOW_UP_RELEASE_KIND, epoch, "text-b")

    claimed = await queue.claim(
        "w1", now=NOW, limit=10, lease_seconds=120, capability_caps=IMAGE_CAPS,
    )

    kinds = [j.kind for j in claimed]
    assert kinds.count(PENDING_FOLLOW_UP_RELEASE_KIND) == 2  # both went out
    assert kinds.count(FEED_COMPOSE_KIND) == 1  # the single image slot
    assert PENDING_FOLLOW_UP_IMAGE_RELEASE_KIND not in kinds
