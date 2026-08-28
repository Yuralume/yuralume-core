"""The campaign runner and its idempotency (LR T2, plan D1/D5/D6).

Everything here runs against the in-memory ledger twin and a stub
dispatcher, because the questions are all about *sequencing and
bookkeeping*, not about what a proactive message says:

* does every item end up with the outcome the dispatcher actually gave;
* does one exploding character cost the rest of the selection its send;
* does a resume re-run only what has no outcome yet;
* is a re-used id with a different selection refused rather than merged;
* does the campaign reach ``completed`` exactly once, with a timestamp.

The stub dispatcher also counts overlap, so "serial" is asserted rather
than assumed — a concurrent walk would still pass every other test here
while quietly turning a paced recall into a burst at the channel.

Two groups exist for hazards that only appear off the happy path:

* **Two replicas, one ledger.** Hosted runs several API replicas behind a
  round-robin, so the console's retry of a POST can land on a *different*
  process than the one already walking that campaign. The assertion is
  the strict one — each character is evaluated at most once across both
  walkers — because the per-item claim is the only thing standing between
  a dropped HTTP response and two recall messages to one player.
* **The list is not a licence.** A selection is acted on minutes to hours
  after it was made, so D1 is re-asserted immediately before each send
  and a player who came back in the meantime is recorded as skipped
  rather than interrupted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.line_reactivation import (
    DEFAULT_ITEM_CLAIM_LEASE,
    MAX_CAMPAIGN_ID_CHARS,
    OUTCOME_SKIPPED_NOT_DORMANT,
    LineReactivationCampaignService,
    LineReactivationEmptySelectionError,
    LineReactivationInvalidCampaignIdError,
    LineReactivationUnknownCharactersError,
)
from kokoro_link.contracts.line_reactivation import (
    CAMPAIGN_STATUS_COMPLETED,
    CAMPAIGN_STATUS_RUNNING,
    LineReactivationCampaign,
    LineReactivationCampaignConflictError,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.repositories.in_memory_line_reactivation import (  # noqa: E501
    InMemoryLineReactivationCampaignRepository,
)

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
_CAMPAIGN = "3f1c0b6a-0000-4000-8000-000000000001"

_LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
"""Anchor for "definitely dormant", independent of the wall clock.

The service resolves ``now`` from its own clock, and the pre-send D1
re-check compares against it, so a fixture that pinned dormancy to a date
near ``_NOW`` would pass or fail depending on when the suite runs."""

_TIER = AccountRuntimeProfile(name="plus", background_dormancy_days=7)


@dataclass
class _State:
    last_active_at: datetime | None = _LONG_AGO


@dataclass
class _StubCharacter:
    id: str
    name: str
    user_id: str = "op1"
    frozen: bool = False
    subscription_locked: bool = False
    state: _State = field(default_factory=_State)


def _body(character_id: str) -> str:
    return f"好久不見，{character_id} 最近還好嗎"


class _StubDispatcher:
    """Answers with a scripted outcome per character id.

    Always attaches a composed body, whatever the outcome — the real
    dispatcher does the same on the ``ERRORED`` path where delivery
    itself raised after composition, and the runner is supposed to keep
    that text out of the report.
    """

    def __init__(  # noqa: ANN001
        self, outcomes=None, explode=frozenset(), bodies=None,
    ) -> None:
        self._outcomes = outcomes or {}
        self._explode = set(explode)
        self._bodies = bodies or {}
        self.calls: list[str] = []
        self.triggers: list[ProactiveTrigger] = []
        self.slots: list[object] = []
        self._in_flight = 0
        self.max_in_flight = 0

    async def evaluate(  # noqa: ANN201
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        now: datetime | None = None,
        logical_slot: str | None = None,
    ):
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            self.calls.append(character_id)
            self.triggers.append(trigger)
            self.slots.append(logical_slot)
            # Yield the loop so an accidentally-parallel runner would
            # actually overlap here and trip ``max_in_flight``.
            await asyncio.sleep(0)
            if character_id in self._explode:
                raise RuntimeError(f"boom for {character_id}")
            outcome = self._outcomes.get(character_id, ProactiveOutcome.SENT)
            return ProactiveAttempt.record(
                character_id=character_id,
                trigger=trigger,
                outcome=outcome,
                reason=f"reason for {character_id}",
                message=self._bodies.get(character_id, _body(character_id)),
                now=_NOW,
            )
        finally:
            self._in_flight -= 1


class _StubCharacterRepository:
    """Counts both read shapes so the report's cost can be asserted."""

    def __init__(self, characters) -> None:  # noqa: ANN001
        self._by_id = {character.id: character for character in characters}
        self.get_calls: list[str] = []
        self.name_lookups: list[tuple[str, ...]] = []

    async def get(self, character_id: str):  # noqa: ANN201
        self.get_calls.append(character_id)
        return self._by_id.get(character_id)

    async def list_names(self, character_ids) -> dict[str, str]:  # noqa: ANN001
        self.name_lookups.append(tuple(character_ids))
        return {
            character_id: self._by_id[character_id].name
            for character_id in character_ids
            if character_id in self._by_id
        }


class _StubProfileResolver:
    def __init__(self, profile: AccountRuntimeProfile | None = _TIER) -> None:
        self._profile = profile

    async def resolve_for_operator(
        self, operator_id: str,
    ) -> AccountRuntimeProfile:
        if self._profile is None:
            raise RuntimeError(f"control plane down for {operator_id}")
        return self._profile


_DEFAULT_NAMES = {"c1": "小晶", "c2": "阿澈"}


def _characters(names=None, **overrides):  # noqa: ANN001, ANN201
    """Build one dormant stub character per name, then apply overrides."""

    resolved = _DEFAULT_NAMES if names is None else names
    built = [
        _StubCharacter(id=character_id, name=name)
        for character_id, name in resolved.items()
    ]
    for character in built:
        for field_name, value in overrides.get(character.id, {}).items():
            setattr(character, field_name, value)
    return built


def _service(  # noqa: ANN201
    dispatcher,  # noqa: ANN001
    *,
    names=None,  # noqa: ANN001
    repository=None,  # noqa: ANN001
    characters=None,  # noqa: ANN001
    profile=_TIER,  # noqa: ANN001
):
    repository = repository or InMemoryLineReactivationCampaignRepository()
    character_repository = _StubCharacterRepository(
        characters if characters is not None else _characters(names),
    )
    service = LineReactivationCampaignService(
        repository=repository,
        dispatcher=dispatcher,
        character_repository=character_repository,
        profile_resolver=_StubProfileResolver(profile),
    )
    return service, repository, character_repository


def _campaign(total: int = 1) -> LineReactivationCampaign:
    return LineReactivationCampaign(
        campaign_id=_CAMPAIGN,
        actor="ops@example",
        status=CAMPAIGN_STATUS_RUNNING,
        created_at=_NOW,
        total=total,
    )


async def test_every_item_records_the_dispatcher_outcome() -> None:
    dispatcher = _StubDispatcher(
        {"c2": ProactiveOutcome.GATE_BLOCKED},
    )
    service, _, _ = _service(dispatcher)

    result = await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1", "c2"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert result.resumed is False
    assert result.status == CAMPAIGN_STATUS_RUNNING
    assert result.total == 2
    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.status == CAMPAIGN_STATUS_COMPLETED
    assert report.completed_at is not None
    assert report.done == 2
    outcomes = {item.character_id: item.outcome for item in report.items}
    assert outcomes == {"c1": "sent", "c2": "gate_blocked"}
    details = {item.character_id: item.detail for item in report.items}
    assert details["c2"] == "reason for c2"
    names = {item.character_id: item.character_name for item in report.items}
    assert names == {"c1": "小晶", "c2": "阿澈"}
    assert all(item.attempted_at is not None for item in report.items)


async def test_the_runner_uses_the_admin_trigger_and_claims_no_slot() -> None:
    dispatcher = _StubDispatcher()
    service, _, _ = _service(dispatcher, names={"c1": "小晶"})

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert dispatcher.triggers == [ProactiveTrigger.ADMIN_REACTIVATION]
    assert dispatcher.slots == [None]


async def test_the_walk_is_serial() -> None:
    dispatcher = _StubDispatcher()
    service, _, _ = _service(
        dispatcher, names={"c1": "a", "c2": "b", "c3": "c"},
    )

    await service.start(
        campaign_id=_CAMPAIGN,
        character_ids=["c1", "c2", "c3"],
        actor="ops@example",
    )
    await service.wait_for_idle()

    assert dispatcher.max_in_flight == 1
    assert len(dispatcher.calls) == 3


async def test_one_exploding_character_does_not_end_the_campaign() -> None:
    dispatcher = _StubDispatcher(explode={"c1"})
    service, _, _ = _service(dispatcher)

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1", "c2"], actor="ops@example",
    )
    await service.wait_for_idle()

    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.status == CAMPAIGN_STATUS_COMPLETED
    by_id = {item.character_id: item for item in report.items}
    assert by_id["c1"].outcome == "errored"
    assert "boom for c1" in (by_id["c1"].detail or "")
    # The point of the test: the *next* character still got its send.
    assert by_id["c2"].outcome == "sent"


# ---------------------------------------------------------------------
# The sent body reaches the report (G1)
# ---------------------------------------------------------------------


async def test_a_sent_row_carries_the_message_verbatim() -> None:
    """The report column the operator's workflow depends on.

    Fire a small batch, read what the characters actually said, judge
    whether it lands as a reunion, then release the rest — none of which
    ``outcome`` and a gate ``reason`` can support.
    """

    dispatcher = _StubDispatcher()
    service, _, _ = _service(dispatcher, names={"c1": "小晶"})

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.items[0].outcome == "sent"
    assert report.items[0].message_text == _body("c1")


async def test_a_long_message_is_stored_unclipped() -> None:
    """``detail`` earns its 500-char ceiling because it is a scan column;
    the message is the artefact under review, and one clipped mid-thought
    reads like a message that ends badly rather than like a truncation."""

    body = "好久不見。" * 500
    dispatcher = _StubDispatcher(bodies={"c1": body})
    service, _, _ = _service(dispatcher, names={"c1": "小晶"})

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.items[0].message_text == body


async def test_a_blocked_row_records_no_message_even_though_one_exists() -> (
    None
):
    """The gate, stated: the column means "what the player received".

    The stub attaches a body to every attempt, exactly as the real
    dispatcher does on the ``ERRORED`` path where delivery raised after
    composition. Copying that text into the report would have the
    operator judging the recall on a message nobody ever saw.
    """

    dispatcher = _StubDispatcher(
        {
            "c1": ProactiveOutcome.GATE_BLOCKED,
            "c2": ProactiveOutcome.QUALITY_WITHHELD,
        },
    )
    service, _, _ = _service(dispatcher, names={"c1": "a", "c2": "b"})

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1", "c2"], actor="ops@example",
    )
    await service.wait_for_idle()

    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert [item.outcome for item in report.items] == [
        "gate_blocked", "quality_withheld",
    ]
    assert all(item.message_text is None for item in report.items)


async def test_skipped_and_errored_and_pending_rows_have_no_message() -> None:
    """Every non-``sent`` shape in one pass: never dispatched (D1
    re-check), dispatched and blew up, and not yet reached."""

    stalled = asyncio.Event()

    class _Stalling(_StubDispatcher):
        async def evaluate(self, **kwargs):  # noqa: ANN003, ANN201
            if kwargs["character_id"] == "c3":
                await stalled.wait()
            return await super().evaluate(**kwargs)

    characters = _characters(
        {"c1": "a", "c2": "b", "c3": "c"},
        c1={"state": _State(last_active_at=datetime.now(UTC))},
    )
    service, _, _ = _service(_Stalling(explode={"c2"}), characters=characters)

    await service.start(
        campaign_id=_CAMPAIGN,
        character_ids=["c1", "c2", "c3"],
        actor="ops@example",
    )
    # Let the walk reach c3 and park inside the dispatcher, so the report
    # is read with c1/c2 answered and c3 genuinely still pending. Bounded
    # rather than a bare ``while True``: a runner that died would
    # otherwise hang the suite instead of failing it.
    for _ in range(100):
        report = await service.report(_CAMPAIGN)
        assert report is not None
        if report.done == 2:
            break
        await asyncio.sleep(0)
    else:  # pragma: no cover - the walk stalled before reaching c3
        raise AssertionError("runner never answered c1 and c2")

    by_id = {item.character_id: item for item in report.items}
    assert by_id["c1"].outcome == OUTCOME_SKIPPED_NOT_DORMANT
    assert by_id["c2"].outcome == "errored"
    assert by_id["c3"].outcome is None
    assert all(item.message_text is None for item in report.items)

    stalled.set()
    await service.wait_for_idle()


async def test_resume_reruns_only_pending_items() -> None:
    """A Core restart mid-campaign, staged through the ledger only.

    The ledger is written directly here — a campaign whose first item was
    stamped and whose second never was — because that is exactly the
    state a restart leaves behind, and the point is that a *fresh*
    service reconstructs the remaining work from those rows alone.
    """

    repository = InMemoryLineReactivationCampaignRepository()
    await repository.create(_campaign(total=2), ["c1", "c2"])
    await repository.record_outcome(
        _CAMPAIGN,
        "c1",
        outcome=ProactiveOutcome.SENT.value,
        detail="sent before the restart",
        attempted_at=_NOW,
    )
    dispatcher = _StubDispatcher()
    service, _, _ = _service(dispatcher, repository=repository)

    result = await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c2", "c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert result.resumed is True
    # c1 already had an outcome — it must not be messaged a second time.
    assert dispatcher.calls == ["c2"]
    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.status == CAMPAIGN_STATUS_COMPLETED
    assert report.done == 2


async def test_a_completed_campaign_is_not_walked_again() -> None:
    dispatcher = _StubDispatcher()
    service, _, _ = _service(dispatcher, names={"c1": "小晶"})
    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    result = await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert result.resumed is True
    assert result.status == CAMPAIGN_STATUS_COMPLETED
    assert dispatcher.calls == ["c1"]


async def test_a_different_selection_on_the_same_id_conflicts() -> None:
    dispatcher = _StubDispatcher()
    service, _, _ = _service(
        dispatcher, names={"c1": "a", "c2": "b", "c3": "c"},
    )
    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1", "c2"], actor="ops@example",
    )
    await service.wait_for_idle()

    with pytest.raises(LineReactivationCampaignConflictError):
        await service.start(
            campaign_id=_CAMPAIGN,
            character_ids=["c1", "c3"],
            actor="ops@example",
        )


async def test_an_empty_selection_is_refused() -> None:
    service, _, _ = _service(_StubDispatcher())

    with pytest.raises(LineReactivationEmptySelectionError):
        await service.start(
            campaign_id=_CAMPAIGN, character_ids=["", "  "], actor="ops@example",
        )


@pytest.mark.parametrize(
    "campaign_id",
    ["", "   ", "x" * (MAX_CAMPAIGN_ID_CHARS + 1)],
    ids=["blank", "whitespace", "over-column-width"],
)
async def test_a_malformed_campaign_id_is_a_bad_request(campaign_id) -> None:  # noqa: ANN001
    """Not a 500. The column is ``String(64)``; letting the database be
    the one to notice turns a client defect into a driver error."""

    service, _, _ = _service(_StubDispatcher())

    with pytest.raises(LineReactivationInvalidCampaignIdError):
        await service.start(
            campaign_id=campaign_id,
            character_ids=["c1"],
            actor="ops@example",
        )


async def test_a_selection_naming_unknown_characters_is_refused() -> None:
    """The foreign key must never be the thing that reports this.

    An ``IntegrityError`` from the item rows is indistinguishable from a
    duplicate ``campaign_id``, so it used to surface as 409 — sending the
    console to mint a new id for a stale candidate list, which a new id
    cannot fix.
    """

    dispatcher = _StubDispatcher()
    service, repository, _ = _service(dispatcher)

    with pytest.raises(LineReactivationUnknownCharactersError) as excinfo:
        await service.start(
            campaign_id=_CAMPAIGN,
            character_ids=["c1", "ghost", "phantom"],
            actor="ops@example",
        )

    assert excinfo.value.missing_character_ids == ("ghost", "phantom")
    # Nothing was created, so a corrected retry may re-use the same id.
    assert await repository.get(_CAMPAIGN) is None
    assert dispatcher.calls == []


async def test_duplicate_ids_are_folded_into_one_item() -> None:
    """``total`` is derived from the folded selection, so the progress bar
    cannot promise a send the item table can never record."""

    dispatcher = _StubDispatcher()
    service, _, _ = _service(dispatcher, names={"c1": "小晶"})

    result = await service.start(
        campaign_id=_CAMPAIGN,
        character_ids=["c1", "c1", " c1 "],
        actor="ops@example",
    )
    await service.wait_for_idle()

    assert result.total == 1
    assert dispatcher.calls == ["c1"]
    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert len(report.items) == 1


async def test_a_deleted_character_still_gets_a_report_row() -> None:
    """Deleted *after* selection — the ledger is seeded directly because
    ``start`` refuses a selection naming a character that is already
    gone."""

    repository = InMemoryLineReactivationCampaignRepository()
    await repository.create(_campaign(), ["c1"])
    dispatcher = _StubDispatcher()
    service, _, _ = _service(dispatcher, repository=repository, names={})

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    report = await service.report(_CAMPAIGN)
    assert report is not None
    # Falls back to the id rather than a blank cell.
    assert report.items[0].character_name == "c1"
    assert report.items[0].outcome == OUTCOME_SKIPPED_NOT_DORMANT
    assert report.items[0].detail == "character_not_found"
    assert dispatcher.calls == []


async def test_report_is_none_for_an_unknown_campaign() -> None:
    service, _, _ = _service(_StubDispatcher())

    assert await service.report("no-such-campaign") is None


async def test_report_counts_pending_items_as_not_done() -> None:
    """Progress is read off the rows, never off the runner's memory."""

    repository = InMemoryLineReactivationCampaignRepository()
    stalled = asyncio.Event()

    class _Stalling(_StubDispatcher):
        async def evaluate(self, **kwargs):  # noqa: ANN003, ANN201
            await stalled.wait()
            return await super().evaluate(**kwargs)

    service, _, _ = _service(_Stalling(), repository=repository)
    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1", "c2"], actor="ops@example",
    )
    await asyncio.sleep(0)

    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.status == CAMPAIGN_STATUS_RUNNING
    assert report.total == 2
    assert report.done == 0
    assert all(item.outcome is None for item in report.items)

    stalled.set()
    await service.wait_for_idle()


async def test_the_report_names_every_item_in_one_lookup() -> None:
    """The console polls this every few seconds over a few hundred rows.

    One character aggregate per row per poll would make an operator
    watching a progress bar into a load generator, so the report is
    pinned to the bulk name lookup and to *zero* aggregate loads.
    """

    dispatcher = _StubDispatcher()
    service, _, characters = _service(
        dispatcher, names={"c1": "a", "c2": "b", "c3": "c"},
    )
    await service.start(
        campaign_id=_CAMPAIGN,
        character_ids=["c1", "c2", "c3"],
        actor="ops@example",
    )
    await service.wait_for_idle()
    gets_before = len(characters.get_calls)
    lookups_before = len(characters.name_lookups)

    report = await service.report(_CAMPAIGN)

    assert report is not None
    assert [item.character_name for item in report.items] == ["a", "b", "c"]
    assert len(characters.get_calls) == gets_before
    assert characters.name_lookups[lookups_before:] == [("c1", "c2", "c3")]


# ---------------------------------------------------------------------
# Two replicas, one ledger
# ---------------------------------------------------------------------


async def test_two_services_over_one_ledger_evaluate_each_character_once() -> (
    None
):
    """The hosted double-send, reproduced.

    Two API replicas, no session stickiness: the console's retry of a
    POST reaches a second process, which resumes the campaign and walks
    the same rows the first is still working through. ``record_outcome``
    is fenced on ``outcome IS NULL``, but that fence is crossed *after*
    the message is already sent — it stops the second bookkeeping entry,
    not the second message. Only the per-item claim, taken before the
    dispatcher is called, stops the send.
    """

    repository = InMemoryLineReactivationCampaignRepository()
    gate = asyncio.Event()
    dispatcher = _StubDispatcher()

    class _Gated(_StubDispatcher):
        async def evaluate(self, **kwargs):  # noqa: ANN003, ANN201
            await gate.wait()
            return await dispatcher.evaluate(**kwargs)

    selection = ["c1", "c2", "c3"]
    names = {"c1": "a", "c2": "b", "c3": "c"}
    replica_a, _, _ = _service(_Gated(), repository=repository, names=names)
    replica_b, _, _ = _service(_Gated(), repository=repository, names=names)

    await replica_a.start(
        campaign_id=_CAMPAIGN, character_ids=selection, actor="ops@example",
    )
    # Let A claim its first row and block inside the dispatcher, so B's
    # resume genuinely overlaps a walk in progress.
    await asyncio.sleep(0)
    resumed = await replica_b.start(
        campaign_id=_CAMPAIGN, character_ids=selection, actor="ops@example",
    )
    gate.set()
    await replica_a.wait_for_idle()
    await replica_b.wait_for_idle()

    assert resumed.resumed is True
    assert sorted(dispatcher.calls) == selection
    assert len(dispatcher.calls) == len(selection)
    report = await replica_a.report(_CAMPAIGN)
    assert report is not None
    assert report.status == CAMPAIGN_STATUS_COMPLETED
    assert report.done == len(selection)


async def test_an_item_another_runner_holds_is_left_pending() -> None:
    """A live claim is not a licence to skip *and* declare victory.

    The walker passes over the row, but the campaign must stay running:
    saying ``completed`` over a character nobody has stamped would report
    a send that never happened.
    """

    repository = InMemoryLineReactivationCampaignRepository()
    await repository.create(_campaign(total=2), ["c1", "c2"])
    held = await repository.claim_item(
        _CAMPAIGN,
        "c1",
        now=datetime.now(UTC),
        lease=DEFAULT_ITEM_CLAIM_LEASE,
    )
    assert held is True
    dispatcher = _StubDispatcher()
    service, _, _ = _service(dispatcher, repository=repository)

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1", "c2"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert dispatcher.calls == ["c2"]
    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.status == CAMPAIGN_STATUS_RUNNING
    assert report.done == 1


async def test_a_lapsed_claim_is_re_run_by_a_resume() -> None:
    """The replica that died mid-send must not strand the row forever."""

    repository = InMemoryLineReactivationCampaignRepository()
    await repository.create(_campaign(), ["c1"])
    await repository.claim_item(
        _CAMPAIGN,
        "c1",
        now=_LONG_AGO,
        lease=DEFAULT_ITEM_CLAIM_LEASE,
    )
    dispatcher = _StubDispatcher()
    service, _, _ = _service(
        dispatcher, repository=repository, names={"c1": "小晶"},
    )

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert dispatcher.calls == ["c1"]
    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.status == CAMPAIGN_STATUS_COMPLETED


# ---------------------------------------------------------------------
# The list is not a licence: D1 re-asserted at send time
# ---------------------------------------------------------------------


async def test_a_player_who_came_back_is_skipped_not_messaged() -> None:
    """The whole reason the pre-send re-check exists.

    The operator selected this character while it was dormant; by the
    time the walk reached it the player was typing. Sending here would
    drop a recall message into a live conversation — and every rhythm
    gate that would ordinarily have caught that is bypassed by
    ``ADMIN_REACTIVATION`` (D3).
    """

    dispatcher = _StubDispatcher()
    characters = _characters(
        {"c1": "小晶", "c2": "阿澈"},
        c1={"state": _State(last_active_at=datetime.now(UTC))},
    )
    service, _, _ = _service(dispatcher, characters=characters)

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1", "c2"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert dispatcher.calls == ["c2"]
    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.status == CAMPAIGN_STATUS_COMPLETED
    by_id = {item.character_id: item for item in report.items}
    assert by_id["c1"].outcome == OUTCOME_SKIPPED_NOT_DORMANT
    assert by_id["c1"].detail == "no_longer_dormant"
    assert by_id["c2"].outcome == "sent"


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"frozen": True}, "frozen"),
        ({"subscription_locked": True}, "subscription_locked"),
    ],
    ids=["frozen", "subscription-locked"],
)
async def test_a_locked_character_is_skipped_not_messaged(  # noqa: ANN001
    overrides, detail,
) -> None:
    dispatcher = _StubDispatcher()
    characters = _characters({"c1": "小晶"}, c1=overrides)
    service, _, _ = _service(dispatcher, characters=characters)

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert dispatcher.calls == []
    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.items[0].outcome == OUTCOME_SKIPPED_NOT_DORMANT
    assert report.items[0].detail == detail


async def test_an_unreachable_control_plane_skips_rather_than_sends() -> None:
    """Fail-closed. Without a dormancy policy there is no way to say the
    player is still away, and a recall message is not the thing to send on
    a guess. Recorded (D6), so the operator can open a new campaign once
    the control plane answers again."""

    dispatcher = _StubDispatcher()
    service, _, _ = _service(
        dispatcher, names={"c1": "小晶"}, profile=None,
    )

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert dispatcher.calls == []
    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.items[0].outcome == OUTCOME_SKIPPED_NOT_DORMANT
    assert report.items[0].detail == "no_dormancy_policy"


async def test_a_skipped_row_still_counts_toward_completion() -> None:
    """A skip is an outcome, not a hole: the campaign must not hang."""

    dispatcher = _StubDispatcher()
    characters = _characters(
        {"c1": "小晶"}, c1={"state": _State(last_active_at=datetime.now(UTC))},
    )
    service, repository, _ = _service(dispatcher, characters=characters)

    await service.start(
        campaign_id=_CAMPAIGN, character_ids=["c1"], actor="ops@example",
    )
    await service.wait_for_idle()

    assert await repository.list_pending_items(_CAMPAIGN) == []
    report = await service.report(_CAMPAIGN)
    assert report is not None
    assert report.status == CAMPAIGN_STATUS_COMPLETED
    assert report.done == 1
