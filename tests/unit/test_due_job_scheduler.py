"""Next-due computation: knob mult-in, deferral 順延, chain-stop, idempotency key."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.contracts.due_jobs import (
    BEAT_DUE_KIND,
    CHARACTER_UPKEEP_KIND,
    FEED_COMMENT_REPLY_KIND,
    FEED_COMPOSE_KIND,
    GOAL_REVIEW_KIND,
    MEMORIALIZE_KIND,
    PROACTIVE_EVALUATE_KIND,
    SCHEDULE_MAINTENANCE_KIND,
    SCHEDULE_WEATHER_VET_KIND,
    STORY_SCENE_TIMEOUT_KIND,
    KnobGate,
    character_chain_kinds,
    kind_spec,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEFAULT_ACCOUNT_RUNTIME_PROFILE,
    AccountRuntimeProfile,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _State:
    last_active_at: datetime | None = None


@dataclass
class _FakeCharacter:
    id: str = "c1"
    user_id: str = "op1"
    frozen: bool = False
    subscription_locked: bool = False
    proactive_enabled: bool = True
    created_at: datetime = BASE
    state: _State = field(default_factory=_State)


class _FakeResolver:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return self._profile


def _calc(profile: AccountRuntimeProfile = DEFAULT_ACCOUNT_RUNTIME_PROFILE) -> NextDueCalculator:
    return NextDueCalculator(resolver=_FakeResolver(profile))


async def test_background_multiplier_scales_cadence() -> None:
    base = _calc(AccountRuntimeProfile(name="x", background_activity_multiplier=1))
    slow = _calc(AccountRuntimeProfile(name="x", background_activity_multiplier=2))
    n1 = await base.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    n2 = await slow.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    assert n1.due_at == BASE + timedelta(seconds=5400)
    assert n2.due_at == BASE + timedelta(seconds=10800)  # 2× cadence


async def test_proactive_gate_uses_proactive_multiplier() -> None:
    calc = _calc(AccountRuntimeProfile(
        name="x", proactive_tick_multiplier=3, background_activity_multiplier=1,
    ))
    n = await calc.compute(_FakeCharacter(), PROACTIVE_EVALUATE_KIND, now=BASE)
    assert n.due_at == BASE + timedelta(seconds=900)  # 300 × 3


async def test_knob_gate_none_ignores_multiplier() -> None:
    # memorialize / upkeep never down-shift even under a heavy multiplier.
    calc = _calc(AccountRuntimeProfile(
        name="x", background_activity_multiplier=6, proactive_tick_multiplier=6,
    ))
    m = await calc.compute(_FakeCharacter(), MEMORIALIZE_KIND, now=BASE)
    u = await calc.compute(_FakeCharacter(), CHARACTER_UPKEEP_KIND, now=BASE)
    assert m.due_at == BASE + timedelta(seconds=3600)
    assert u.due_at == BASE + timedelta(seconds=3600)


async def test_idle_multiplier_applied_when_idle() -> None:
    profile = AccountRuntimeProfile(
        name="x", background_activity_multiplier=2,
        idle_downshift_days=1, idle_multiplier=3,
    )
    calc = _calc(profile)
    idle_char = _FakeCharacter(state=_State(last_active_at=BASE - timedelta(days=2)))
    n = await calc.compute(idle_char, FEED_COMPOSE_KIND, now=BASE)
    assert n.due_at == BASE + timedelta(seconds=5400 * 6)  # 2 × 3 idle

    active_char = _FakeCharacter(state=_State(last_active_at=BASE))
    a = await calc.compute(active_char, FEED_COMPOSE_KIND, now=BASE)
    assert a.due_at == BASE + timedelta(seconds=5400 * 2)  # not idle


async def test_frozen_stops_chain() -> None:
    calc = _calc()
    assert await calc.compute(
        _FakeCharacter(frozen=True), FEED_COMPOSE_KIND, now=BASE,
    ) is None


async def test_subscription_locked_stops_chain() -> None:
    calc = _calc()
    assert await calc.compute(
        _FakeCharacter(subscription_locked=True), PROACTIVE_EVALUATE_KIND, now=BASE,
    ) is None


async def test_deferral_pushes_due_forward_not_retry() -> None:
    calc = _calc()

    async def defer(character, kind, candidate):  # noqa: ANN001
        return candidate + timedelta(hours=2)

    n = await calc.compute(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, deferral_check=defer,
    )
    assert n.deferred is True
    assert n.due_at == BASE + timedelta(seconds=5400) + timedelta(hours=2)


async def test_deferral_never_pulls_due_earlier() -> None:
    calc = _calc()

    async def defer(character, kind, candidate):  # noqa: ANN001
        return candidate - timedelta(hours=1)  # earlier → ignored

    n = await calc.compute(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, deferral_check=defer,
    )
    assert n.deferred is False
    assert n.due_at == BASE + timedelta(seconds=5400)


async def test_idempotency_key_same_window_stable() -> None:
    calc = _calc()
    a = await calc.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    b = await calc.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    assert a.idempotency_key == b.idempotency_key
    assert a.idempotency_key.startswith("feed_compose:c1:")


async def test_idempotency_key_next_occurrence_differs() -> None:
    calc = _calc()
    first = await calc.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    second = await calc.compute(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, last_due=first.due_at,
    )
    assert second.due_at > first.due_at
    assert second.idempotency_key != first.idempotency_key


async def test_explicit_next_due_honoured_for_beat() -> None:
    calc = _calc()
    beat_at = BASE + timedelta(minutes=42)
    n = await calc.compute(
        _FakeCharacter(), BEAT_DUE_KIND, now=BASE, explicit_next_due=beat_at,
    )
    assert n.due_at == beat_at


async def test_never_schedules_in_the_past() -> None:
    calc = _calc()
    stale = BASE - timedelta(days=5)
    n = await calc.compute(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, last_due=stale,
    )
    assert n.due_at > BASE


async def test_priority_and_capability_carried_through() -> None:
    calc = _calc()
    n = await calc.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE)
    assert n.priority == 4
    assert n.capability == "image"


# --- NF4 真・休眠: background_dormancy_days -------------------------------- #

#: Every character-scoped kind that dormancy is allowed to stop, and the two it
#: is not. Written out rather than derived so that adding a kind — or flipping
#: an existing one's exemption — has to come here and be argued for.
_DORMANCY_COVERED_KINDS = (
    BEAT_DUE_KIND,
    SCHEDULE_MAINTENANCE_KIND,
    SCHEDULE_WEATHER_VET_KIND,
    MEMORIALIZE_KIND,
    FEED_COMPOSE_KIND,
    PROACTIVE_EVALUATE_KIND,
    GOAL_REVIEW_KIND,
    CHARACTER_UPKEEP_KIND,
)
_DORMANCY_EXEMPT_KINDS = (STORY_SCENE_TIMEOUT_KIND, FEED_COMMENT_REPLY_KIND)


def _dormancy_profile(days: int = 7, **knobs) -> AccountRuntimeProfile:
    return AccountRuntimeProfile(
        name="free", background_dormancy_days=days, **knobs,
    )


async def test_null_dormancy_is_bit_identical_to_today() -> None:
    """The self-host red line, stated as an equality rather than a vibe: with
    the knob unset every kind computes exactly the due it computed before NF4
    existed — including a never-interacted character, which is the case the
    knob's other half turns into "dormant"."""
    calc = _calc(AccountRuntimeProfile(
        name="selfhost", background_activity_multiplier=2,
        proactive_tick_multiplier=3,
    ))
    never_touched = _FakeCharacter()  # last_active_at is None
    for kind in character_chain_kinds():
        spec = kind_spec(kind)
        expected = {
            KnobGate.BACKGROUND: 2, KnobGate.PROACTIVE: 3, KnobGate.NONE: 1,
        }[spec.knob_gate]
        n = await calc.compute(never_touched, kind, now=BASE)
        assert n is not None, kind
        assert n.due_at == BASE + timedelta(
            seconds=spec.base_interval_seconds * expected,
        ), kind


async def test_never_interacted_character_schedules_no_background() -> None:
    """互動前不啟動背景: a character the player has never spoken to has no
    ``last_active_at`` at all, and is dormant from creation — the anchor does
    NOT fall back to ``created_at`` the way the idle down-shift's does."""
    calc = _calc(_dormancy_profile())
    fresh = _FakeCharacter(created_at=BASE, state=_State(last_active_at=None))
    for kind in _DORMANCY_COVERED_KINDS:
        assert await calc.compute(fresh, kind, now=BASE) is None, kind


async def test_idle_beyond_dormancy_window_stops_the_chain() -> None:
    calc = _calc(_dormancy_profile(days=7))
    gone = _FakeCharacter(state=_State(last_active_at=BASE - timedelta(days=7)))
    for kind in _DORMANCY_COVERED_KINDS:
        assert await calc.compute(gone, kind, now=BASE) is None, kind


async def test_inside_the_dormancy_window_still_schedules() -> None:
    calc = _calc(_dormancy_profile(days=7))
    recent = _FakeCharacter(
        state=_State(last_active_at=BASE - timedelta(days=6, hours=23)),
    )
    n = await calc.compute(recent, FEED_COMPOSE_KIND, now=BASE)
    assert n is not None
    assert n.due_at == BASE + timedelta(seconds=5400)


async def test_dormancy_stops_rather_than_stretches() -> None:
    """The knob is a stop, not a fourth multiplier: a dormant character does
    not get a very long cadence, it gets no next occurrence at all — and the
    idle multiplier that WOULD have applied is never consulted."""
    calc = _calc(_dormancy_profile(
        days=1, background_activity_multiplier=2,
        idle_downshift_days=1, idle_multiplier=3,
    ))
    gone = _FakeCharacter(state=_State(last_active_at=BASE - timedelta(days=2)))
    assert await calc.compute(gone, FEED_COMPOSE_KIND, now=BASE) is None


async def test_dormancy_covers_knob_gate_none_kinds() -> None:
    """"Cheap enough not to down-shift" is not "worth running for a character
    nobody has spoken to in a week": the un-gated DB-only chains stop too."""
    calc = _calc(_dormancy_profile(days=1))
    gone = _FakeCharacter(state=_State(last_active_at=BASE - timedelta(days=5)))
    for none_gate_kind in (BEAT_DUE_KIND, MEMORIALIZE_KIND, CHARACTER_UPKEEP_KIND):
        assert await calc.compute(gone, none_gate_kind, now=BASE) is None


async def test_exempt_kinds_keep_running_while_dormant() -> None:
    """The exemption criterion — finishing an in-progress player-visible
    operation. An open 起幕 scene must still get its wrap-up, and a comment the
    player left must still get its answer, however long they have been away."""
    calc = _calc(_dormancy_profile(days=1))
    gone = _FakeCharacter(state=_State(last_active_at=BASE - timedelta(days=90)))
    for kind in _DORMANCY_EXEMPT_KINDS:
        n = await calc.compute(gone, kind, now=BASE)
        assert n is not None, kind
    # ...and an exempt kind's exact deadline is still honoured, so an open
    # scene closes at its real timeout rather than the fallback recheck.
    deadline = BASE + timedelta(minutes=12)
    n = await calc.compute(
        gone, STORY_SCENE_TIMEOUT_KIND, now=BASE, explicit_next_due=deadline,
    )
    assert n.due_at == deadline


async def test_exempt_kinds_are_exactly_the_two_argued_for() -> None:
    """Guards the blast radius from the other side: the covered / exempt split
    above must account for every character-scoped kind, so a new kind cannot
    slip in unclassified."""
    assert set(_DORMANCY_COVERED_KINDS) | set(_DORMANCY_EXEMPT_KINDS) == set(
        character_chain_kinds(),
    )


async def test_foreground_interaction_restores_scheduling() -> None:
    """恢復: the chain stops on the same object it resumes on. Moving the
    foreground anchor is the whole of the wake-up — the reconciler then reseeds
    exactly as it does after a thaw."""
    calc = _calc(_dormancy_profile(days=7))
    character = _FakeCharacter(
        state=_State(last_active_at=BASE - timedelta(days=30)),
    )
    assert await calc.compute(character, PROACTIVE_EVALUATE_KIND, now=BASE) is None

    character.state.last_active_at = BASE  # the player says something
    n = await calc.compute(character, PROACTIVE_EVALUATE_KIND, now=BASE)
    assert n is not None
    assert n.due_at == BASE + timedelta(seconds=300)


async def test_unresolvable_profile_fails_open_and_keeps_scheduling() -> None:
    """A control-plane blip must not silently switch off every hosted
    character's background: the wrong ``False`` costs one cadence, the wrong
    ``True`` costs a deployment that nobody gets an error about."""

    class _BrokenResolver:
        async def resolve_for_operator(self, operator_id: str):
            raise RuntimeError("control plane down")

    calc = NextDueCalculator(resolver=_BrokenResolver())
    never_touched = _FakeCharacter()
    n = await calc.compute(never_touched, FEED_COMPOSE_KIND, now=BASE)
    assert n is not None
    assert n.due_at == BASE + timedelta(seconds=5400)  # multiplier 1


async def test_raising_dormancy_resolver_fails_open() -> None:
    async def _boom(character, profile, now):  # noqa: ANN001
        raise RuntimeError("anchor lookup exploded")

    calc = NextDueCalculator(
        resolver=_FakeResolver(_dormancy_profile()), dormancy_resolver=_boom,
    )
    assert await calc.compute(_FakeCharacter(), FEED_COMPOSE_KIND, now=BASE) is not None


# --- NF4 follow-up: a chain that belongs to more than one character -------- #
#
# ``co_characters`` is how a pair chain (encounter) tells the calculator about
# its second side. The bug it fixes: asking only about the canonical-low id
# made the pair's survival depend on which character id sorts first.


async def test_dormant_co_character_stops_the_chain() -> None:
    """a active + b never interacted ⇒ stop. Before this, the pair chain ran
    forever because only ``char_a`` was ever asked."""
    calc = _calc(_dormancy_profile(days=7))
    active = _FakeCharacter(id="c-01", state=_State(last_active_at=BASE))
    never = _FakeCharacter(id="c-99")  # never interacted
    assert await calc.compute(
        active, FEED_COMPOSE_KIND, now=BASE, co_characters=(never,),
    ) is None


async def test_dormant_primary_with_active_co_character_also_stops() -> None:
    """The mirror image, which the old code got wrong in the other direction:
    with the never-interacted character sorted first the whole chain stopped
    even though the pair's other side was being played daily. Both orders now
    answer the same way — the stop is a property of the pair, not of ids."""
    calc = _calc(_dormancy_profile(days=7))
    never = _FakeCharacter(id="c-01")
    active = _FakeCharacter(id="c-99", state=_State(last_active_at=BASE))
    assert await calc.compute(
        never, FEED_COMPOSE_KIND, now=BASE, co_characters=(active,),
    ) is None


async def test_both_sides_active_keeps_the_chain() -> None:
    calc = _calc(_dormancy_profile(days=7))
    a = _FakeCharacter(id="c-01", state=_State(last_active_at=BASE))
    b = _FakeCharacter(id="c-99", state=_State(last_active_at=BASE))
    n = await calc.compute(
        a, FEED_COMPOSE_KIND, now=BASE, co_characters=(b,),
    )
    assert n is not None
    assert n.due_at == BASE + timedelta(seconds=5400)


async def test_co_characters_are_inert_without_the_knob() -> None:
    """self-host: a second side changes nothing, because dormancy is off."""
    calc = _calc()
    never = _FakeCharacter(id="c-99")
    n = await calc.compute(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE, co_characters=(never,),
    )
    assert n is not None


# --- NF4 follow-up: the dormancy predicate handlers pre-flight with -------- #


async def test_is_chain_dormant_matches_compute() -> None:
    calc = _calc(_dormancy_profile(days=7))
    gone = _FakeCharacter(state=_State(last_active_at=BASE - timedelta(days=8)))
    here = _FakeCharacter(state=_State(last_active_at=BASE))
    assert await calc.is_chain_dormant(gone, FEED_COMPOSE_KIND, now=BASE) is True
    assert await calc.is_chain_dormant(here, FEED_COMPOSE_KIND, now=BASE) is False
    # …and the equivalence with the stop it guards.
    assert await calc.compute(gone, FEED_COMPOSE_KIND, now=BASE) is None
    assert await calc.compute(here, FEED_COMPOSE_KIND, now=BASE) is not None


async def test_is_chain_dormant_is_false_for_exempt_kinds() -> None:
    calc = _calc(_dormancy_profile(days=1))
    gone = _FakeCharacter(state=_State(last_active_at=BASE - timedelta(days=90)))
    for kind in _DORMANCY_EXEMPT_KINDS:
        assert await calc.is_chain_dormant(gone, kind, now=BASE) is False, kind


async def test_is_chain_dormant_is_true_when_any_side_is() -> None:
    calc = _calc(_dormancy_profile(days=7))
    active = _FakeCharacter(id="c-01", state=_State(last_active_at=BASE))
    never = _FakeCharacter(id="c-99")
    assert await calc.is_chain_dormant(
        active, FEED_COMPOSE_KIND, now=BASE, co_characters=(never,),
    ) is True
    assert await calc.is_chain_dormant(
        never, FEED_COMPOSE_KIND, now=BASE, co_characters=(active,),
    ) is True


async def test_is_chain_dormant_stays_false_without_the_knob() -> None:
    calc = _calc()
    assert await calc.is_chain_dormant(
        _FakeCharacter(), FEED_COMPOSE_KIND, now=BASE,
    ) is False


# --- NF4 follow-up: what a computation costs ------------------------------- #


class _CountingResolver:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self._profile = profile
        self.calls: list[str] = []

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        self.calls.append(operator_id)
        return self._profile


async def test_profile_scope_resolves_one_operator_once_per_pass() -> None:
    """The pass-scoped memo: ten kinds × many characters of one operator used
    to be one uncached read each."""
    resolver = _CountingResolver(_dormancy_profile(days=7))
    calc = NextDueCalculator(resolver=resolver)
    scope = calc.new_profile_scope()
    characters = [
        _FakeCharacter(id=f"c{i}", state=_State(last_active_at=BASE))
        for i in range(5)
    ]
    for character in characters:
        for kind in character_chain_kinds():
            await calc.compute(
                character, kind, now=BASE, profile_scope=scope,
            )
    assert resolver.calls == ["op1"]


async def test_without_a_scope_every_computation_resolves() -> None:
    """The property the scope exists to change — stated so a regression that
    silently drops the ``profile_scope`` argument shows up as a count."""
    resolver = _CountingResolver(_dormancy_profile(days=7))
    calc = NextDueCalculator(resolver=resolver)
    character = _FakeCharacter(state=_State(last_active_at=BASE))
    await calc.compute(character, FEED_COMPOSE_KIND, now=BASE)
    await calc.compute(character, FEED_COMPOSE_KIND, now=BASE)
    assert len(resolver.calls) == 2


async def test_one_computation_resolves_the_profile_at_most_once() -> None:
    """Dormancy and the multiplier read the same profile; a kind must not pay
    twice for one computation."""
    resolver = _CountingResolver(_dormancy_profile(days=7))
    calc = NextDueCalculator(resolver=resolver)
    character = _FakeCharacter(state=_State(last_active_at=BASE))
    await calc.compute(character, FEED_COMPOSE_KIND, now=BASE)
    assert len(resolver.calls) == 1


async def test_fully_exempt_ungated_kind_still_reads_nothing() -> None:
    """``story_scene_timeout`` / ``feed_comment_reply`` are dormancy-exempt AND
    un-gated: they needed no profile before NF4 and must still need none."""
    resolver = _CountingResolver(_dormancy_profile(days=7))
    calc = NextDueCalculator(resolver=resolver)
    for kind in _DORMANCY_EXEMPT_KINDS:
        await calc.compute(_FakeCharacter(), kind, now=BASE)
    assert resolver.calls == []
