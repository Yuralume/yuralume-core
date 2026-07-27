"""G2 — hosted player locale / location lifecycle service.

Covers the owner-ratified hybrid: a free first-login confirmation plus a
guarded later change (2 per rolling 30 days across both locale fields),
and the "you seem to have moved" hint that never rewrites the profile.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.player_locale_service import (
    LOCALE_CHANGE_LIMIT,
    LOCALE_CHANGE_WINDOW_DAYS,
    LOCALE_CLAIM_PREFIX,
    LOCALE_CONFIRMED_KEY,
    LocaleAlreadyConfirmed,
    LocaleChangeInProgress,
    LocaleChangeLimitExceeded,
    OperatorProfileMissing,
    PlayerLocaleService,
    locale_claim_parts,
)
from kokoro_link.application.services.runtime_claim import RuntimeClaim
from kokoro_link.application.services.scoped_preferences import (
    user_preference_key,
)
from kokoro_link.contracts.geo_location import GeoLocation
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)

USER_ID = "cloud:acct-1"
START = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


class _YieldingPreferences(InMemoryPreferencesRepository):
    """In-memory store with a real suspension point on every operation.

    The plain repo never awaits anything, so ``asyncio.gather`` runs each
    caller straight through and a read-modify-write race is invisible. Every
    real adapter suspends on I/O; this one does too, which is what makes the
    guardrail's critical section observable in a unit test.
    """

    async def get(self, key: str) -> object | None:
        await asyncio.sleep(0)
        return await super().get(key)

    async def set(self, key: str, value: object) -> None:
        await asyncio.sleep(0)
        await super().set(key, value)

    async def delete(self, key: str) -> bool:
        await asyncio.sleep(0)
        return await super().delete(key)


class _StubCharacters:
    """Only the one seam ``PlayerLocaleService`` uses (``list_for_user``)."""

    def __init__(self, *, owned: int = 0, explode: bool = False) -> None:
        self.owned = owned
        self.explode = explode
        self.calls = 0

    async def list_for_user(self, user_id: str) -> list[object]:
        self.calls += 1
        if self.explode:
            raise RuntimeError("character store is down")
        return [object() for _ in range(self.owned)]


class _MovableClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def _profile(**overrides: object) -> OperatorProfile:
    base = {
        "id": USER_ID,
        "display_name": "Player",
        "primary_language": "zh-TW",
        "timezone_id": "Asia/Taipei",
        "country_code": "TW",
        "latitude": 25.03,
        "longitude": 121.56,
        "location_label": "Taipei, TW",
        "cloud_account_id": "acct-1",
        "cloud_tenant_id": "tenant-1",
        "auth_provider": "cloud",
    }
    base.update(overrides)
    return OperatorProfile(**base)  # type: ignore[arg-type]


def _build(
    profile: OperatorProfile | None = None,
    *,
    characters: _StubCharacters | None = None,
    change_claim: RuntimeClaim | None = None,
    yielding_preferences: bool = False,
) -> tuple[
    PlayerLocaleService,
    InMemoryOperatorProfileRepository,
    InMemoryPreferencesRepository,
    _MovableClock,
]:
    profiles = InMemoryOperatorProfileRepository()
    preferences = (
        _YieldingPreferences()
        if yielding_preferences
        else InMemoryPreferencesRepository()
    )
    clock = _MovableClock(START)
    if profile is not None:
        profiles._profiles[profile.id] = profile  # noqa: SLF001 - test seam
    service = PlayerLocaleService(
        profiles=profiles,
        preferences=preferences,
        clock=clock,
        characters=characters,
        change_claim=change_claim,
    )
    return service, profiles, preferences, clock


# ----------------------------------------------------------------------
# confirmation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fresh_player_needs_confirmation() -> None:
    service, _, _, _ = _build(_profile())

    assert await service.needs_confirmation(USER_ID) is True
    assert await service.is_confirmed(USER_ID) is False


@pytest.mark.asyncio
async def test_confirming_marks_the_account_and_keeps_values() -> None:
    service, profiles, _, _ = _build(_profile())

    result = await service.confirm(USER_ID)

    assert await service.needs_confirmation(USER_ID) is False
    assert result.timezone_id == "Asia/Taipei"
    assert (await profiles.get(USER_ID)).primary_language == "zh-TW"


@pytest.mark.asyncio
async def test_confirmation_corrections_do_not_spend_the_guardrail() -> None:
    """The whole point of the first-login gate: correcting a bad GeoIP
    guess before any content exists must be free."""
    service, profiles, _, _ = _build(_profile())

    updated = await service.confirm(
        USER_ID, timezone_id="Asia/Tokyo", primary_language="ja",
    )

    assert updated.timezone_id == "Asia/Tokyo"
    assert updated.primary_language == "ja"
    assert (await profiles.get(USER_ID)).timezone_id == "Asia/Tokyo"
    assert await service.remaining_changes(USER_ID) == LOCALE_CHANGE_LIMIT


@pytest.mark.asyncio
async def test_confirmation_can_also_fix_the_location() -> None:
    service, profiles, _, _ = _build(_profile())

    updated = await service.confirm(
        USER_ID,
        country_code="JP",
        latitude=35.68,
        longitude=139.76,
        location_label="Tokyo, JP",
    )

    assert updated.country_code == "JP"
    stored = await profiles.get(USER_ID)
    assert stored.location_label == "Tokyo, JP"
    assert stored.latitude == pytest.approx(35.68)


@pytest.mark.asyncio
async def test_confirmation_without_a_profile_row_raises() -> None:
    service, _, _, _ = _build(None)

    with pytest.raises(OperatorProfileMissing):
        await service.confirm(USER_ID)


# ----------------------------------------------------------------------
# confirm() must not be a free second door past the guardrail
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_cannot_change_the_locale_once_confirmed() -> None:
    """The free correction is the *first-login* one. After that, ``confirm``
    is a dismiss / place surface — a locale edit must go through the
    guarded PATCH or the 30-day counter means nothing."""
    service, profiles, _, _ = _build(_profile())
    await service.confirm(USER_ID)

    with pytest.raises(LocaleAlreadyConfirmed):
        await service.confirm(
            USER_ID, timezone_id="Asia/Tokyo", primary_language="ja",
        )

    stored = await profiles.get(USER_ID)
    assert stored.timezone_id == "Asia/Taipei"
    assert stored.primary_language == "zh-TW"
    assert await service.remaining_changes(USER_ID) == LOCALE_CHANGE_LIMIT


@pytest.mark.asyncio
async def test_a_confirmed_account_may_still_fix_its_place_and_hint() -> None:
    """Only timezone / language are guarded; the location fields have their
    own (unguarded) surface, and dismissing the hint must keep working."""
    profile = _profile()
    service, profiles, _, _ = _build(profile)
    await service.confirm(USER_ID)
    await service.record_location_hint(
        USER_ID, location=_geo("JP"), profile=profile,
    )

    updated = await service.confirm(
        USER_ID,
        country_code="JP",
        location_label="Tokyo, JP",
        dismiss_location_hint=True,
    )

    assert updated.country_code == "JP"
    assert (await profiles.get(USER_ID)).location_label == "Tokyo, JP"
    assert await service.get_location_hint(USER_ID, profile=updated) is None


@pytest.mark.asyncio
async def test_re_confirming_the_same_values_stays_idempotent() -> None:
    """A double-submitted onboarding form re-sends what it just wrote; that
    is a no-op, not an attempt to dodge the guardrail."""
    service, _, _, _ = _build(_profile())
    await service.confirm(USER_ID, timezone_id="Asia/Tokyo")

    again = await service.confirm(USER_ID, timezone_id="Asia/Tokyo")

    assert again.timezone_id == "Asia/Tokyo"


# ----------------------------------------------------------------------
# pre-existing accounts (no preference row) are not brand-new players
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_that_already_owns_characters_is_self_healed() -> None:
    """Accounts created before the confirmation gate shipped have no
    preference row; blocking them behind an onboarding dialog would be a
    regression, so the first read back-fills the flag."""
    characters = _StubCharacters(owned=2)
    service, _, preferences, _ = _build(_profile(), characters=characters)

    assert await service.needs_confirmation(USER_ID) is False
    assert await preferences.get(
        user_preference_key(USER_ID, LOCALE_CONFIRMED_KEY),
    ) is not None
    # Self-healed once: the second read short-circuits on the preference.
    assert await service.is_confirmed(USER_ID) is True
    assert characters.calls == 1


@pytest.mark.asyncio
async def test_a_brand_new_account_still_needs_confirmation() -> None:
    service, _, _, _ = _build(_profile(), characters=_StubCharacters(owned=0))

    assert await service.needs_confirmation(USER_ID) is True


@pytest.mark.asyncio
async def test_a_self_healed_account_gets_no_free_locale_correction() -> None:
    service, profiles, _, _ = _build(
        _profile(), characters=_StubCharacters(owned=1),
    )

    with pytest.raises(LocaleAlreadyConfirmed):
        await service.confirm(USER_ID, timezone_id="Asia/Tokyo")

    assert (await profiles.get(USER_ID)).timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_a_broken_character_store_falls_back_to_needing_confirmation() -> None:
    """Fail-soft: an unreadable character store must not crash the startup
    probe, and "ask again" is the harmless side of the guess."""
    service, _, _, _ = _build(
        _profile(), characters=_StubCharacters(explode=True),
    )

    assert await service.needs_confirmation(USER_ID) is True


# ----------------------------------------------------------------------
# guarded change
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_changes_are_allowed_and_the_third_is_refused() -> None:
    service, profiles, _, clock = _build(_profile())

    first = await service.change_locale(USER_ID, timezone_id="Asia/Tokyo")
    assert first.remaining_changes == 1
    assert first.profile.timezone_id == "Asia/Tokyo"

    clock.advance(days=1)
    second = await service.change_locale(USER_ID, primary_language="ja")
    assert second.remaining_changes == 0
    assert second.profile.primary_language == "ja"

    clock.advance(days=1)
    with pytest.raises(LocaleChangeLimitExceeded) as excinfo:
        await service.change_locale(USER_ID, timezone_id="Europe/Berlin")

    assert excinfo.value.limit == LOCALE_CHANGE_LIMIT
    assert excinfo.value.window_days == LOCALE_CHANGE_WINDOW_DAYS
    # Next slot frees up 30 days after the *oldest* in-window change.
    assert excinfo.value.retry_at == START + timedelta(
        days=LOCALE_CHANGE_WINDOW_DAYS,
    )
    # The refused change must not have touched the profile.
    assert (await profiles.get(USER_ID)).timezone_id == "Asia/Tokyo"


@pytest.mark.asyncio
async def test_timezone_and_language_share_one_counter() -> None:
    """Counting them separately would double the real budget by alternating."""
    service, _, _, clock = _build(_profile())

    await service.change_locale(USER_ID, timezone_id="Asia/Tokyo")
    clock.advance(hours=1)
    await service.change_locale(USER_ID, primary_language="en-US")

    assert await service.remaining_changes(USER_ID) == 0


@pytest.mark.asyncio
async def test_the_window_slides_so_old_changes_free_a_slot() -> None:
    service, _, _, clock = _build(_profile())

    await service.change_locale(USER_ID, timezone_id="Asia/Tokyo")
    clock.advance(days=1)
    await service.change_locale(USER_ID, primary_language="ja")
    assert await service.remaining_changes(USER_ID) == 0

    # Past the first change's 30-day mark but not the second's: one slot back.
    clock.advance(days=LOCALE_CHANGE_WINDOW_DAYS - 1, hours=1)
    assert await service.remaining_changes(USER_ID) == 1
    result = await service.change_locale(USER_ID, timezone_id="Europe/Berlin")
    assert result.profile.timezone_id == "Europe/Berlin"
    assert result.remaining_changes == 0


@pytest.mark.asyncio
async def test_a_no_op_change_does_not_spend_a_slot() -> None:
    """A double-submitted form must not cost the player a monthly change."""
    service, _, _, _ = _build(_profile())

    result = await service.change_locale(USER_ID, timezone_id="Asia/Taipei")

    assert result.remaining_changes == LOCALE_CHANGE_LIMIT
    assert result.profile.timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_change_goes_through_the_dedicated_repository_seam() -> None:
    """``save`` pins the locale columns on purpose; the change must not
    silently no-op because it took the generic upsert path."""
    profile = _profile()
    service, profiles, _, _ = _build(profile)

    await service.change_locale(USER_ID, timezone_id="Asia/Tokyo")

    reloaded = await profiles.get(USER_ID)
    assert reloaded.timezone_id == "Asia/Tokyo"
    # Unrelated identity facts survive the targeted write.
    assert reloaded.display_name == profile.display_name
    assert reloaded.cloud_tenant_id == profile.cloud_tenant_id


@pytest.mark.asyncio
async def test_corrupt_change_log_is_treated_as_empty() -> None:
    service, _, preferences, _ = _build(_profile())
    await preferences.set(
        user_preference_key(USER_ID, "player_locale_change_log"),
        "not-a-list",
    )

    assert await service.remaining_changes(USER_ID) == LOCALE_CHANGE_LIMIT


# ----------------------------------------------------------------------
# location hint
# ----------------------------------------------------------------------


def _geo(country: str) -> GeoLocation:
    return GeoLocation(
        country_code=country,
        latitude=35.68,
        longitude=139.76,
        label="Tokyo, Japan",
        timezone_id="Asia/Tokyo",
    )


@pytest.mark.asyncio
async def test_a_login_from_another_country_records_a_hint_only() -> None:
    profile = _profile()
    service, profiles, _, _ = _build(profile)

    hint = await service.record_location_hint(
        USER_ID, location=_geo("JP"), profile=profile,
    )

    assert hint is not None
    assert hint.country_code == "JP"
    assert hint.timezone_id == "Asia/Tokyo"
    # The profile itself is untouched — the player may have set it by hand.
    stored = await profiles.get(USER_ID)
    assert stored.country_code == "TW"
    assert stored.location_label == "Taipei, TW"


@pytest.mark.asyncio
async def test_a_login_from_the_same_country_records_nothing() -> None:
    profile = _profile()
    service, _, _, _ = _build(profile)

    assert await service.record_location_hint(
        USER_ID, location=_geo("TW"), profile=profile,
    ) is None
    assert await service.get_location_hint(USER_ID, profile=profile) is None


@pytest.mark.asyncio
async def test_a_failed_geoip_lookup_records_nothing() -> None:
    profile = _profile()
    service, _, _, _ = _build(profile)

    assert await service.record_location_hint(
        USER_ID, location=None, profile=profile,
    ) is None


@pytest.mark.asyncio
async def test_no_stored_country_leaves_the_hint_to_the_seed_path() -> None:
    profile = _profile(country_code=None)
    service, _, _, _ = _build(profile)

    assert await service.record_location_hint(
        USER_ID, location=_geo("JP"), profile=profile,
    ) is None


@pytest.mark.asyncio
async def test_accepting_the_new_location_retires_the_hint_on_read() -> None:
    profile = _profile()
    service, profiles, _, _ = _build(profile)
    await service.record_location_hint(
        USER_ID, location=_geo("JP"), profile=profile,
    )

    moved = profile.update(country_code="JP", location_label="Tokyo, JP")
    await profiles.save(moved)

    assert await service.get_location_hint(USER_ID, profile=moved) is None


@pytest.mark.asyncio
async def test_dismissing_through_confirm_clears_the_hint() -> None:
    profile = _profile()
    service, _, _, _ = _build(profile)
    await service.record_location_hint(
        USER_ID, location=_geo("JP"), profile=profile,
    )

    await service.confirm(USER_ID, dismiss_location_hint=True)

    assert await service.get_location_hint(USER_ID, profile=profile) is None


@pytest.mark.asyncio
async def test_an_unrelated_hint_survives_until_answered() -> None:
    profile = _profile()
    service, _, _, _ = _build(profile)
    await service.record_location_hint(
        USER_ID, location=_geo("JP"), profile=profile,
    )

    hint = await service.get_location_hint(USER_ID, profile=profile)

    assert hint is not None
    assert hint.country_code == "JP"
    assert hint.detected_at == START


# ----------------------------------------------------------------------
# guardrail atomicity (read-window → verify → write is one critical section)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_changes_cannot_overspend_the_window() -> None:
    """Four simultaneous PATCHes each read the log, each saw a free slot and
    each wrote — burning the budget twice over and clobbering one another.
    Serialised, exactly ``LOCALE_CHANGE_LIMIT`` may land."""
    service, _, _, _ = _build(_profile(), yielding_preferences=True)

    zones = ["Asia/Tokyo", "Europe/Berlin", "America/New_York", "Asia/Seoul"]
    outcomes = await asyncio.gather(
        *(service.change_locale(USER_ID, timezone_id=zone) for zone in zones),
        return_exceptions=True,
    )

    accepted = [o for o in outcomes if not isinstance(o, BaseException)]
    refused = [o for o in outcomes if isinstance(o, LocaleChangeLimitExceeded)]
    assert len(accepted) == LOCALE_CHANGE_LIMIT
    assert len(refused) == len(zones) - LOCALE_CHANGE_LIMIT
    assert await service.remaining_changes(USER_ID) == 0


@pytest.mark.asyncio
async def test_a_change_held_by_another_replica_is_told_to_retry() -> None:
    """Cross-instance half of the same guard: the process-local lock cannot
    see replica B, so the shared TTL claim has to."""
    lease = InMemoryBackgroundCoordinatorLease()
    peer = RuntimeClaim(lease, prefix=LOCALE_CLAIM_PREFIX, owner_id="replica-b")
    assert await peer.acquire(*locale_claim_parts(USER_ID)) is True
    service, profiles, _, _ = _build(
        _profile(),
        change_claim=RuntimeClaim(
            lease, prefix=LOCALE_CLAIM_PREFIX, owner_id="replica-a",
        ),
    )

    with pytest.raises(LocaleChangeInProgress):
        await service.change_locale(USER_ID, timezone_id="Asia/Tokyo")

    assert (await profiles.get(USER_ID)).timezone_id == "Asia/Taipei"


@pytest.mark.asyncio
async def test_confirmation_takes_the_same_claim() -> None:
    """The free first-login correction writes the same two columns, so it
    has to sit behind the same door."""
    lease = InMemoryBackgroundCoordinatorLease()
    peer = RuntimeClaim(lease, prefix=LOCALE_CLAIM_PREFIX, owner_id="replica-b")
    await peer.acquire(*locale_claim_parts(USER_ID))
    service, _, _, _ = _build(
        _profile(),
        change_claim=RuntimeClaim(
            lease, prefix=LOCALE_CLAIM_PREFIX, owner_id="replica-a",
        ),
    )

    with pytest.raises(LocaleChangeInProgress):
        await service.confirm(USER_ID, timezone_id="Asia/Tokyo")


@pytest.mark.asyncio
async def test_the_claim_is_released_so_the_next_change_is_not_parked() -> None:
    """Held past the call, our own claim would lock the player out for a
    whole TTL after every legitimate change."""
    lease = InMemoryBackgroundCoordinatorLease()
    service, _, _, clock = _build(
        _profile(),
        change_claim=RuntimeClaim(
            lease, prefix=LOCALE_CLAIM_PREFIX, owner_id="replica-a",
        ),
    )

    await service.change_locale(USER_ID, timezone_id="Asia/Tokyo")

    other = RuntimeClaim(lease, prefix=LOCALE_CLAIM_PREFIX, owner_id="replica-b")
    assert await other.acquire(*locale_claim_parts(USER_ID)) is True


@pytest.mark.asyncio
async def test_a_refused_change_still_releases_the_claim() -> None:
    """The guardrail rejection is the common failure path; leaking the claim
    there would wedge the key for a TTL on every over-limit attempt."""
    lease = InMemoryBackgroundCoordinatorLease()
    service, _, _, clock = _build(
        _profile(),
        change_claim=RuntimeClaim(
            lease, prefix=LOCALE_CLAIM_PREFIX, owner_id="replica-a",
        ),
    )
    await service.change_locale(USER_ID, timezone_id="Asia/Tokyo")
    clock.advance(hours=1)
    await service.change_locale(USER_ID, primary_language="ja")
    clock.advance(hours=1)

    with pytest.raises(LocaleChangeLimitExceeded):
        await service.change_locale(USER_ID, timezone_id="Europe/Berlin")

    other = RuntimeClaim(lease, prefix=LOCALE_CLAIM_PREFIX, owner_id="replica-b")
    assert await other.acquire(*locale_claim_parts(USER_ID)) is True


@pytest.mark.asyncio
async def test_lifecycle_state_never_falls_back_to_a_global_row() -> None:
    """User-scoped lifecycle state must not inherit an unscoped default —
    one stray global row would otherwise mark every account confirmed."""
    service, _, preferences, _ = _build(_profile())
    await preferences.set("player_locale_confirmed", "2026-01-01T00:00:00+00:00")

    assert await service.needs_confirmation(USER_ID) is True
