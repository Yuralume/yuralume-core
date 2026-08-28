"""D1 候選過濾（LR T1）.

The list an operator selects from is the whole safety story of this
feature: everything downstream trusts that a row here is a character who
has actually engaged, is actually dormant, and actually has a hosted
destination. So each of the four exclusions gets its own case, plus the
one inclusion that is *not* an exclusion — a probe that failed is listed,
marked, and not selectable.

The second half of the file pins the *shape* of the read path rather than
its verdicts, because that shape is what keeps a several-thousand
character deployment's single ``GET`` answerable: no character is ever
re-read, every operator is read once regardless of how many characters
they own, the fan-out is concurrent up to a stated ceiling, and a channel
that has gone slow costs the operator marked rows instead of a request
that never returns.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.line_reactivation import (
    ELIGIBILITY_REASON_TRANSIENT,
    LineReactivationCandidateService,
)
from kokoro_link.contracts.external_proactive import DeliveryEligibility
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.cloud.hosted_channel_proactive_client import (
    ChannelDeliveryTransientError,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

_PLUS = AccountRuntimeProfile(name="plus", background_dormancy_days=7)
_NO_DORMANCY = AccountRuntimeProfile(name="free", background_dormancy_days=None)


@dataclass
class _State:
    last_active_at: datetime | None = None


@dataclass
class _FakeCharacter:
    id: str
    name: str = "小晶"
    user_id: str = "op1"
    frozen: bool = False
    subscription_locked: bool = False
    created_at: datetime | None = NOW - timedelta(days=90)
    state: _State = field(default_factory=_State)


class _FakeCharacterRepository:
    def __init__(self, characters: list[_FakeCharacter]) -> None:
        self._characters = characters
        self.list_calls = 0

    async def list_active(self) -> list[_FakeCharacter]:
        self.list_calls += 1
        return [
            character
            for character in self._characters
            if not character.frozen and not character.subscription_locked
        ]

    async def get(self, character_id: str) -> _FakeCharacter:
        # The listing already holds every row ``list_active`` returned;
        # reaching back for one is the N+1 this service must not have.
        raise AssertionError(
            f"candidate listing must not re-read character {character_id!r}",
        )


class _FakeProfileResolver:
    def __init__(self, by_operator: dict[str, AccountRuntimeProfile]) -> None:
        self._by_operator = by_operator
        self.calls: list[str] = []

    async def resolve_for_operator(
        self, operator_id: str,
    ) -> AccountRuntimeProfile:
        self.calls.append(operator_id)
        return self._by_operator.get(
            operator_id, DEFAULT_ACCOUNT_RUNTIME_PROFILE,
        )


def _cloud_operator(operator_id: str) -> OperatorProfile:
    return OperatorProfile(
        id=operator_id,
        display_name=operator_id,
        auth_provider="cloud",
        cloud_tenant_id="tenant-A",
        cloud_account_id=f"acct-{operator_id}",
    )


class _FakeOperatorRepository:
    """Operator rows, counting reads — one per operator is the contract."""

    def __init__(self, *, unprojected: frozenset[str] = frozenset()) -> None:
        self._unprojected = unprojected
        self.calls: list[str] = []

    async def get(self, operator_id: str) -> OperatorProfile | None:
        self.calls.append(operator_id)
        if operator_id in self._unprojected:
            # A local (self-host) operator: no cloud projection at all.
            return OperatorProfile(id=operator_id, display_name=operator_id)
        return _cloud_operator(operator_id)


class _FakeDelivery:
    """Eligibility probe stub with per-character scripted answers."""

    def __init__(
        self,
        *,
        ineligible: dict[str, str] | None = None,
        raising: frozenset[str] = frozenset(),
        delay: float = 0.0,
    ) -> None:
        self._ineligible = ineligible or {}
        self._raising = raising
        self._delay = delay
        self.probed: list[str] = []
        self.in_flight = 0
        self.peak_in_flight = 0

    async def check_eligibility(self, character_id: str) -> DeliveryEligibility:
        self.probed.append(character_id)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if character_id in self._raising:
                raise ChannelDeliveryTransientError("channel down")
            reason = self._ineligible.get(character_id)
            if reason is not None:
                return DeliveryEligibility(eligible=False, reason=reason)
            return DeliveryEligibility(eligible=True)
        finally:
            self.in_flight -= 1

    async def accept(self, envelope, *, target=None):  # pragma: no cover
        raise AssertionError("candidate listing must never send")


def _service(
    characters: list[_FakeCharacter],
    *,
    profiles: dict[str, AccountRuntimeProfile] | None = None,
    delivery: _FakeDelivery | None = None,
    unprojected_operators: frozenset[str] = frozenset(),
    concurrency: int = 8,
    budget_seconds: float | None = None,
) -> tuple[LineReactivationCandidateService, _FakeDelivery]:
    resolved_delivery = delivery or _FakeDelivery()
    service = LineReactivationCandidateService(
        character_repository=_FakeCharacterRepository(characters),
        operator_repository=_FakeOperatorRepository(
            unprojected=unprojected_operators,
        ),
        profile_resolver=_FakeProfileResolver(
            profiles if profiles is not None else {"op1": _PLUS},
        ),
        external_delivery=resolved_delivery,
        concurrency=concurrency,
        **({} if budget_seconds is None else {"budget_seconds": budget_seconds}),
    )
    return service, resolved_delivery


def _dormant(character_id: str, *, days: int = 30, **kwargs) -> _FakeCharacter:
    return _FakeCharacter(
        id=character_id,
        state=_State(last_active_at=NOW - timedelta(days=days)),
        **kwargs,
    )


async def test_dormant_bound_character_is_listed_with_its_window() -> None:
    service, _ = _service([_dormant("c1", days=30)])

    listing = await service.list_candidates(now=NOW)

    assert listing.generated_at == NOW
    assert [c.character_id for c in listing.candidates] == ["c1"]
    candidate = listing.candidates[0]
    assert candidate.character_name == "小晶"
    assert candidate.user_id == "op1"
    assert candidate.tier_key == "plus"
    assert candidate.dormancy_days == 7
    assert candidate.dormant_for_days == 30
    assert candidate.last_active_at == NOW - timedelta(days=30)
    assert candidate.eligible is True
    assert candidate.eligibility_reason is None


async def test_never_interacted_character_is_excluded() -> None:
    """D1: dormant means "went quiet", not "never started".

    ``last_active_at is None`` is also what the scheduler calls dormant,
    so the rule alone would happily offer a character nobody has said a
    word to — which is spam, and would be refused by the first-contact
    gate anyway.
    """
    service, _ = _service([_FakeCharacter(id="c1", state=_State(None))])

    listing = await service.list_candidates(now=NOW)

    assert listing.candidates == ()


async def test_character_inside_the_window_is_excluded() -> None:
    service, _ = _service([_dormant("c1", days=3)])

    listing = await service.list_candidates(now=NOW)

    assert listing.candidates == ()


async def test_character_exactly_at_the_window_is_included() -> None:
    """``>=`` — the same boundary the scheduler stops the chain at."""
    service, _ = _service([_dormant("c1", days=7)])

    listing = await service.list_candidates(now=NOW)

    assert [c.character_id for c in listing.candidates] == ["c1"]


async def test_frozen_and_locked_characters_are_excluded() -> None:
    service, _ = _service([
        _dormant("frozen", frozen=True),
        _dormant("locked", subscription_locked=True),
        _dormant("ok"),
    ])

    listing = await service.list_candidates(now=NOW)

    assert [c.character_id for c in listing.candidates] == ["ok"]


async def test_tier_without_a_dormancy_window_is_excluded() -> None:
    """``None`` days means "never dormant" — the only spelling of it."""
    service, _ = _service(
        [_dormant("c1", days=365)], profiles={"op1": _NO_DORMANCY},
    )

    listing = await service.list_candidates(now=NOW)

    assert listing.candidates == ()


async def test_character_without_a_cloud_identity_is_excluded() -> None:
    """Parity with delivery: no projection ⇒ nothing could be sent.

    The projection is a property of the *operator*, so the exclusion is
    expressed the way production expresses it — one owner is a cloud
    account, the other is not.
    """
    service, _ = _service(
        [_dormant("c1", user_id="local-op"), _dormant("c2")],
        profiles={"op1": _PLUS, "local-op": _PLUS},
        unprojected_operators=frozenset({"local-op"}),
    )

    listing = await service.list_candidates(now=NOW)

    assert [c.character_id for c in listing.candidates] == ["c2"]


async def test_unprojected_operator_is_never_probed() -> None:
    """The channel is asked only about rows that could actually be sent."""
    delivery = _FakeDelivery()
    service, _ = _service(
        [_dormant("c1", user_id="local-op"), _dormant("c2")],
        profiles={"op1": _PLUS, "local-op": _PLUS},
        unprojected_operators=frozenset({"local-op"}),
        delivery=delivery,
    )

    await service.list_candidates(now=NOW)

    assert delivery.probed == ["c2"]


async def test_ineligible_character_is_listed_with_its_reason() -> None:
    delivery = _FakeDelivery(ineligible={"c1": "no active channel endpoint"})
    service, _ = _service([_dormant("c1")], delivery=delivery)

    listing = await service.list_candidates(now=NOW)

    candidate = listing.candidates[0]
    assert candidate.eligible is False
    assert candidate.eligibility_reason == "no active channel endpoint"


async def test_probe_failure_marks_transient_without_losing_the_list() -> None:
    """A channel outage must cost one row's certainty, not the page."""
    delivery = _FakeDelivery(raising=frozenset({"c1"}))
    service, _ = _service([_dormant("c1"), _dormant("c2")], delivery=delivery)

    listing = await service.list_candidates(now=NOW)

    by_id = {c.character_id: c for c in listing.candidates}
    assert by_id["c1"].eligible is False
    assert by_id["c1"].eligibility_reason == ELIGIBILITY_REASON_TRANSIENT
    assert by_id["c2"].eligible is True


async def test_eligibility_fan_out_is_concurrent_and_bounded() -> None:
    """Both halves matter: eight at once, and never a ninth.

    ``== 8`` rather than ``<= 8`` because a serial walk would also satisfy
    the ceiling — and a serial walk over a real roster is exactly the
    minutes-long ``GET`` this fan-out exists to prevent.
    """
    characters = [_dormant(f"c{index}") for index in range(20)]
    delivery = _FakeDelivery(delay=0.01)
    service, _ = _service(characters, delivery=delivery, concurrency=8)

    listing = await service.list_candidates(now=NOW)

    assert len(listing.candidates) == 20
    assert delivery.peak_in_flight == 8


async def test_longest_dormant_first() -> None:
    service, _ = _service([
        _dormant("recent", days=8),
        _dormant("ancient", days=120),
        _dormant("middle", days=40),
    ])

    listing = await service.list_candidates(now=NOW)

    assert [c.character_id for c in listing.candidates] == [
        "ancient", "middle", "recent",
    ]


async def test_one_profile_read_per_operator_not_per_character() -> None:
    resolver = _FakeProfileResolver({"op1": _PLUS})
    service = LineReactivationCandidateService(
        character_repository=_FakeCharacterRepository(
            [_dormant(f"c{index}") for index in range(5)],
        ),
        operator_repository=_FakeOperatorRepository(),
        profile_resolver=resolver,
        external_delivery=_FakeDelivery(),
    )

    await service.list_candidates(now=NOW)

    assert resolver.calls == ["op1"]


async def test_one_operator_read_per_operator_not_per_character() -> None:
    """The cloud projection is an operator fact, so it is read once.

    Before this, the listing asked a character-shaped identity resolver
    per row, and that resolver re-read the character *and* its operator —
    two round trips per dormant character, for an answer identical across
    a whole roster.
    """
    operators = _FakeOperatorRepository()
    characters = [_dormant(f"a{index}") for index in range(4)]
    characters += [_dormant(f"b{index}", user_id="op2") for index in range(3)]
    service = LineReactivationCandidateService(
        character_repository=_FakeCharacterRepository(characters),
        operator_repository=operators,
        profile_resolver=_FakeProfileResolver({"op1": _PLUS, "op2": _PLUS}),
        external_delivery=_FakeDelivery(),
    )

    listing = await service.list_candidates(now=NOW)

    assert len(listing.candidates) == 7
    assert sorted(operators.calls) == ["op1", "op2"]


async def test_listing_never_re_reads_a_character_row() -> None:
    """``_FakeCharacterRepository.get`` raises — reaching it is the bug."""
    service, _ = _service([_dormant(f"c{index}") for index in range(3)])

    listing = await service.list_candidates(now=NOW)

    assert len(listing.candidates) == 3


async def test_spent_budget_marks_unprobed_rows_transient() -> None:
    """A slow channel costs certainty per row, not the whole request.

    With a budget far shorter than the probe, the first row's probe is cut
    off and the rest never start — and every one of them still comes back,
    marked, in the operator's page.
    """
    delivery = _FakeDelivery(delay=5.0)
    service, _ = _service(
        [_dormant(f"c{index}") for index in range(3)],
        delivery=delivery,
        concurrency=1,
        budget_seconds=0.05,
    )

    listing = await service.list_candidates(now=NOW)

    assert len(listing.candidates) == 3
    assert all(not c.eligible for c in listing.candidates)
    assert {c.eligibility_reason for c in listing.candidates} == {
        ELIGIBILITY_REASON_TRANSIENT,
    }
    # Only the row that actually got a turn reached the channel.
    assert delivery.probed == ["c0"]


async def test_exhausted_budget_yields_an_empty_page_not_an_error() -> None:
    """Nothing can be asserted dormant, so nothing is offered."""
    service, delivery = _service(
        [_dormant("c1")], budget_seconds=0.0,
    )

    listing = await service.list_candidates(now=NOW)

    assert listing.candidates == ()
    assert delivery.probed == []
