"""Hosted player locale / location lifecycle (plan G2).

Self-host is a single station owner who sets everything once; hosted has
to serve players from everywhere, whose timezone and content language are
guessed from GeoIP at first login and are sometimes simply wrong (VPN,
travelling, a shared exit node).

Owner-ratified design is
two-layered:

1. **First-login confirmation is the main line of defence.** Before any
   content exists, the player confirms or corrects the guessed place /
   timezone / language. Corrections at that moment are free — there is no
   history to reinterpret — so they do **not** consume the guardrail.
2. **Afterwards the change stays possible but guarded.** Locking it
   outright strands anyone who mis-confirmed or later moved abroad; the
   self-host repair CLI exists precisely because that need is real. So the
   change survives behind an explicit route, an unmistakable warning in the
   UI, and a rate guardrail of ``LOCALE_CHANGE_LIMIT`` changes per rolling
   ``LOCALE_CHANGE_WINDOW_DAYS`` days.

Separately, a re-login from a new country produces a **hint**, never an
overwrite: the player may have set their location by hand, and a business
trip must not silently relocate their character's world.

All three pieces of state live in ``app_preferences`` under user-scoped
keys (``scoped_preferences``) — no schema change, no new table.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable

from kokoro_link.application.services.runtime_claim import RuntimeClaim
from kokoro_link.application.services.scoped_preferences import (
    delete_user_preference,
    set_user_preference,
    user_preference_key,
)
from kokoro_link.contracts.geo_location import GeoLocation
from kokoro_link.contracts.repositories import (
    CharacterRepositoryPort,
    PreferencesRepositoryPort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.operator_profile import (
    UNSET,
    OperatorProfile,
    normalise_language_tag,
)
from kokoro_link.domain.value_objects.timezone import normalise_timezone_id

_LOGGER = logging.getLogger(__name__)

LOCALE_CONFIRMED_KEY = "player_locale_confirmed"
LOCALE_CHANGE_LOG_KEY = "player_locale_change_log"
LOCATION_HINT_KEY = "player_location_hint"

LOCALE_CHANGE_LIMIT = 2
LOCALE_CHANGE_WINDOW_DAYS = 30
"""Timezone and language changes share one counter.

They are one player-visible decision ("where I live / what language I
speak"), and counting them separately would let someone burn four
reinterpretations a month by alternating fields.
"""

LOCALE_CLAIM_PREFIX = "locale"
LOCALE_CLAIM_TTL_SECONDS = 15
"""The guarded section is three small writes. Long enough that no honest
request can outrun it, short enough that a replica dying mid-write parks the
player for seconds rather than minutes."""

_LOCALE_LOCK_SLOTS = 32
"""Fixed ring of in-process locks, indexed by a stable hash of the user id.

A ``dict[user_id, Lock]`` would grow for the life of the process; a ring is
bounded. Two players sharing a slot serialise against each other for the few
milliseconds a locale write takes — invisible, and locale changes are rare by
construction (two per 30 days)."""


class LocaleChangeLimitExceeded(Exception):
    """Raised when the rolling-window guardrail rejects a locale change."""

    def __init__(self, *, retry_at: datetime) -> None:
        super().__init__("locale change limit reached")
        self.retry_at = retry_at
        self.limit = LOCALE_CHANGE_LIMIT
        self.window_days = LOCALE_CHANGE_WINDOW_DAYS


class LocaleChangeInProgress(Exception):
    """Another request (here or on another replica) holds the player's claim.

    Transient by construction — the claim is held only for the duration of one
    write — so the caller should be told to retry, not that anything failed.
    """


class LocaleAlreadyConfirmed(Exception):
    """``confirm`` was asked to move the locale of an already-confirmed player.

    The free correction exists only for the first-login moment, before any
    content has been generated. Afterwards the same edit has to spend the
    rolling guardrail, i.e. go through ``change_locale`` — otherwise the
    confirmation endpoint is simply an unmetered second door onto the two
    columns the guardrail exists to protect.
    """


class OperatorProfileMissing(Exception):
    """The caller's operator row does not exist (nothing to change)."""


def locale_claim_parts(user_id: str) -> tuple[str, ...]:
    """Key parts identifying one player's locale-write slot.

    Exposed (like ``_plan_claim_parts``) so wiring and tests can address the
    same key the service claims, instead of rebuilding the string by hand.
    """
    return (user_id,)


@dataclass(frozen=True, slots=True)
class LocationHint:
    """A "you look like you're somewhere else now" suggestion, not a fact."""

    country_code: str
    label: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone_id: str | None = None
    detected_at: datetime | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "country_code": self.country_code,
            "label": self.label,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone_id": self.timezone_id,
            "detected_at": (
                self.detected_at.isoformat() if self.detected_at else None
            ),
        }


@dataclass(frozen=True, slots=True)
class LocaleChangeResult:
    profile: OperatorProfile
    remaining_changes: int


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class PlayerLocaleService:
    """Guardrail + confirmation + location-hint state for one deployment.

    Deliberately mode-agnostic: the cloud-only restriction is a routing
    concern (the routes 404 in self-host), so this service stays testable
    and reusable without an ``AppSettings`` dependency.
    """

    def __init__(
        self,
        *,
        profiles: OperatorProfileRepositoryPort,
        preferences: PreferencesRepositoryPort,
        clock: Callable[[], datetime] = _default_clock,
        characters: CharacterRepositoryPort | None = None,
        change_claim: RuntimeClaim | None = None,
    ) -> None:
        self._profiles = profiles
        self._preferences = preferences
        self._clock = clock
        # Used only to tell "brand-new player" apart from "account that
        # predates the confirmation gate" — see ``_resolve_confirmed``.
        self._characters = characters
        # Two halves of one guard. The lock ring serialises THIS process; the
        # claim serialises the fleet (hosted 2×api). The claim alone is not
        # enough: ``RuntimeClaim.acquire`` renews for the same owner id, so two
        # concurrent requests on the same replica would both hold it.
        self._change_claim = change_claim
        self._locks: tuple[asyncio.Lock, ...] = tuple(
            asyncio.Lock() for _ in range(_LOCALE_LOCK_SLOTS)
        )

    # -- confirmation ------------------------------------------------

    async def is_confirmed(self, user_id: str) -> bool:
        return await self._resolve_confirmed(user_id)

    async def needs_confirmation(self, user_id: str) -> bool:
        return not await self.is_confirmed(user_id)

    async def confirm(
        self,
        user_id: str,
        *,
        timezone_id: str | None = None,
        primary_language: str | None = None,
        country_code: str | None | object = UNSET,
        latitude: float | None | object = UNSET,
        longitude: float | None | object = UNSET,
        location_label: str | None | object = UNSET,
        dismiss_location_hint: bool = False,
    ) -> OperatorProfile:
        """Accept (and optionally correct) the seeded locale, once.

        Corrections here are **free**: the confirmation happens before the
        player has generated any content, so there is nothing to
        reinterpret and charging the 30-day guardrail would only punish
        someone for fixing a bad GeoIP guess.

        That freedom lasts exactly one confirmation. Re-posting is still
        idempotent for the flag, the place fields and the hint dismissal —
        but a locale value that would actually *move* an already-confirmed
        player raises :class:`LocaleAlreadyConfirmed`, because otherwise the
        onboarding endpoint is an unmetered bypass of ``change_locale``. The
        state check and the write share one critical section, so the answer
        cannot be overtaken between them.
        """
        async with self._locale_guard(user_id):
            profile = await self._require_profile(user_id)
            if await self._resolve_confirmed(user_id):
                desired = self._apply_locale(
                    profile,
                    timezone_id=timezone_id,
                    primary_language=primary_language,
                )
                # Equal values are a re-submit, not an evasion — let them pass.
                if desired != profile:
                    raise LocaleAlreadyConfirmed(user_id)
                updated = profile
            else:
                updated = await self._persist_locale(
                    profile,
                    timezone_id=timezone_id,
                    primary_language=primary_language,
                )
            located = self._apply_location(
                updated,
                country_code=country_code,
                latitude=latitude,
                longitude=longitude,
                location_label=location_label,
            )
            if located != updated:
                await self._profiles.save(located)
            updated = located
            await set_user_preference(
                self._preferences,
                LOCALE_CONFIRMED_KEY,
                self._clock().isoformat(),
                user_id=user_id,
            )
            location_changed = updated.country_code != profile.country_code or (
                updated.location_label != profile.location_label
            )
            if dismiss_location_hint or location_changed:
                await self.clear_location_hint(user_id)
            return updated

    # -- guarded change ----------------------------------------------

    async def remaining_changes(self, user_id: str) -> int:
        stamps = await self._recent_change_stamps(user_id)
        return max(0, LOCALE_CHANGE_LIMIT - len(stamps))

    async def change_locale(
        self,
        user_id: str,
        *,
        timezone_id: str | None = None,
        primary_language: str | None = None,
    ) -> LocaleChangeResult:
        """Apply a deliberate, guardrail-consuming locale change.

        A no-op request (values equal to what is stored) neither consumes a
        change nor rewrites the row — otherwise a double-submitting form
        would eat the player's monthly budget.

        Read-window → verify → write happens inside one critical section
        (:meth:`_locale_guard`). Without it, concurrent requests each read a
        log with a free slot, each passed the check and each appended a
        stamp: the budget was spent twice over and the losing writer's list
        overwrote the winner's.
        """
        async with self._locale_guard(user_id):
            profile = await self._require_profile(user_id)
            target = self._apply_locale(
                profile,
                timezone_id=timezone_id,
                primary_language=primary_language,
            )
            if target == profile:
                return LocaleChangeResult(
                    profile=profile,
                    remaining_changes=await self.remaining_changes(user_id),
                )
            stamps = await self._recent_change_stamps(user_id)
            if len(stamps) >= LOCALE_CHANGE_LIMIT:
                oldest = min(stamps)
                raise LocaleChangeLimitExceeded(
                    retry_at=oldest + timedelta(days=LOCALE_CHANGE_WINDOW_DAYS),
                )
            updated = await self._persist_locale(
                profile,
                timezone_id=timezone_id,
                primary_language=primary_language,
            )
            now = self._clock()
            await set_user_preference(
                self._preferences,
                LOCALE_CHANGE_LOG_KEY,
                [stamp.isoformat() for stamp in (*stamps, now)],
                user_id=user_id,
            )
            return LocaleChangeResult(
                profile=updated,
                remaining_changes=max(0, LOCALE_CHANGE_LIMIT - len(stamps) - 1),
            )

    # -- location hint -----------------------------------------------

    async def record_location_hint(
        self,
        user_id: str,
        *,
        location: GeoLocation | None,
        profile: OperatorProfile,
    ) -> LocationHint | None:
        """Note a login from a country other than the stored one.

        Never writes the profile: the stored location may be a deliberate
        manual choice, and a trip abroad must not relocate the player's
        world behind their back. Returns ``None`` (and clears any stale
        hint) when the login country already matches.
        """
        if location is None:
            return None
        detected = (location.country_code or "").strip().upper()
        if not detected:
            return None
        current = (profile.country_code or "").strip().upper()
        if current and detected == current:
            await self.clear_location_hint(user_id)
            return None
        if not current:
            # No stored country at all — the GeoIP seed path owns this case
            # and writes the profile directly; a hint would be noise.
            return None
        hint = LocationHint(
            country_code=detected,
            label=location.label,
            latitude=location.latitude,
            longitude=location.longitude,
            timezone_id=location.timezone_id,
            detected_at=self._clock(),
        )
        await set_user_preference(
            self._preferences,
            LOCATION_HINT_KEY,
            hint.as_payload(),
            user_id=user_id,
        )
        return hint

    async def get_location_hint(
        self, user_id: str, *, profile: OperatorProfile | None = None,
    ) -> LocationHint | None:
        """Read the pending hint, self-healing once it becomes moot.

        If the player has since moved their stored country onto the hinted
        one — through the existing location API, the confirmation flow, or
        anything else — the hint is dropped on read instead of lingering
        until an explicit dismiss.
        """
        raw = await self._read(user_id, LOCATION_HINT_KEY)
        hint = _hint_from_payload(raw)
        if hint is None:
            return None
        current = (
            (profile.country_code or "").strip().upper() if profile else ""
        )
        if current and current == hint.country_code:
            await self.clear_location_hint(user_id)
            return None
        return hint

    async def clear_location_hint(self, user_id: str) -> bool:
        return await delete_user_preference(
            self._preferences, LOCATION_HINT_KEY, user_id=user_id,
        )

    # -- internals ---------------------------------------------------

    def _lock_for(self, user_id: str) -> asyncio.Lock:
        digest = hashlib.sha256(user_id.encode("utf-8")).digest()
        return self._locks[digest[0] % _LOCALE_LOCK_SLOTS]

    @asynccontextmanager
    async def _locale_guard(self, user_id: str) -> AsyncIterator[None]:
        """Serialise every write to one player's locale state.

        Both halves are needed. The in-process lock covers requests landing on
        the same replica (the claim would renew for our own owner id and let
        them through); the TTL claim covers the other replicas (the lock is
        invisible to them). Released in ``finally`` so a rejection — the
        common path once the budget is spent — never parks the key for a TTL.
        """
        async with self._lock_for(user_id):
            if not await self._acquire_claim(user_id):
                raise LocaleChangeInProgress(user_id)
            try:
                yield
            finally:
                await self._release_claim(user_id)

    async def _acquire_claim(self, user_id: str) -> bool:
        if self._change_claim is None:
            return True
        return await self._change_claim.acquire(
            *locale_claim_parts(user_id),
            ttl_seconds=LOCALE_CLAIM_TTL_SECONDS,
        )

    async def _release_claim(self, user_id: str) -> None:
        if self._change_claim is None:
            return
        await self._change_claim.release(*locale_claim_parts(user_id))

    async def _resolve_confirmed(self, user_id: str) -> bool:
        """Confirmed state, self-healing accounts older than the gate itself.

        The preference row only exists for players who have *been through* the
        confirmation dialog. Everyone who signed up before it shipped has no
        row, and reading that as "brand new" strands them behind an onboarding
        gate they already outgrew — and, worse, hands them an unmetered free
        locale correction. Owning at least one character is the evidence that
        the account predates the gate, so the flag is back-filled on the spot
        and no later read pays for the lookup again.

        Deliberately not a migration: it costs one query per pre-existing
        account, once, and works for rows that arrive by any other route.
        """
        if await self._read(user_id, LOCALE_CONFIRMED_KEY):
            return True
        if not await self._owns_any_character(user_id):
            return False
        await set_user_preference(
            self._preferences,
            LOCALE_CONFIRMED_KEY,
            self._clock().isoformat(),
            user_id=user_id,
        )
        _LOGGER.info(
            "Back-filled locale confirmation for pre-existing account %s",
            user_id,
        )
        return True

    async def _owns_any_character(self, user_id: str) -> bool:
        """Fail-soft existence probe.

        ``None`` (self-host wiring, tests) and a store that cannot answer both
        read as "no characters" — i.e. the player is asked to confirm. That is
        the harmless side of the guess: a redundant dialog, never a locked-out
        account.
        """
        if self._characters is None:
            return False
        try:
            return bool(await self._characters.list_for_user(user_id))
        except Exception as exc:  # noqa: BLE001 - probe must never propagate
            _LOGGER.info(
                "Character probe failed for %s; treating as unconfirmed: %s",
                user_id,
                exc,
            )
            return False

    async def _require_profile(self, user_id: str) -> OperatorProfile:
        profile = await self._profiles.get(user_id)
        if profile is None:
            raise OperatorProfileMissing(user_id)
        return profile

    def _apply_locale(
        self,
        profile: OperatorProfile,
        *,
        timezone_id: str | None,
        primary_language: str | None,
    ) -> OperatorProfile:
        next_timezone = (
            normalise_timezone_id(timezone_id)
            if timezone_id is not None
            else None
        )
        next_language = (
            normalise_language_tag(primary_language)
            if primary_language is not None
            else None
        )
        if next_timezone is None and next_language is None:
            return profile
        return profile.update_identity_locale(
            timezone_id=next_timezone,
            primary_language=next_language,
        )

    async def _persist_locale(
        self,
        profile: OperatorProfile,
        *,
        timezone_id: str | None,
        primary_language: str | None,
    ) -> OperatorProfile:
        """Write the locale through the dedicated repository seam.

        Not ``save``: the generic upsert pins these two columns on purpose
        (an incidental profile write must never move them), so the
        sanctioned change goes through ``set_identity_locale``.
        """
        target = self._apply_locale(
            profile,
            timezone_id=timezone_id,
            primary_language=primary_language,
        )
        if target == profile:
            return profile
        stored = await self._profiles.set_identity_locale(
            profile.id,
            timezone_id=target.timezone_id,
            primary_language=target.primary_language,
        )
        return stored or target

    def _apply_location(
        self,
        profile: OperatorProfile,
        *,
        country_code: str | None | object,
        latitude: float | None | object,
        longitude: float | None | object,
        location_label: str | None | object,
    ) -> OperatorProfile:
        if all(
            value is UNSET
            for value in (country_code, latitude, longitude, location_label)
        ):
            return profile
        return profile.update(
            country_code=country_code,  # type: ignore[arg-type]
            latitude=latitude,  # type: ignore[arg-type]
            longitude=longitude,  # type: ignore[arg-type]
            location_label=location_label,  # type: ignore[arg-type]
        )

    async def _recent_change_stamps(self, user_id: str) -> tuple[datetime, ...]:
        """Return in-window change timestamps, oldest first.

        The window slides on read — nothing prunes the stored list on a
        schedule — so a player who changed twice 31 days ago regains both
        slots without any background job.
        """
        raw = await self._read(user_id, LOCALE_CHANGE_LOG_KEY)
        if not isinstance(raw, list):
            return ()
        cutoff = self._clock() - timedelta(days=LOCALE_CHANGE_WINDOW_DAYS)
        stamps = [
            parsed
            for parsed in (_parse_timestamp(item) for item in raw)
            if parsed is not None and parsed > cutoff
        ]
        return tuple(sorted(stamps))

    async def _read(self, user_id: str, key: str) -> Any:
        """Read a user-scoped preference **without** the global fallback.

        ``get_preference_with_user_fallback`` intentionally falls back to
        the unscoped key; for per-player lifecycle state that would let one
        stray global row mark every account confirmed.
        """
        return await self._preferences.get(user_preference_key(user_id, key))


def _hint_from_payload(raw: Any) -> LocationHint | None:
    if not isinstance(raw, dict):
        return None
    country = str(raw.get("country_code") or "").strip().upper()
    if len(country) != 2 or not country.isalpha():
        return None
    return LocationHint(
        country_code=country,
        label=_optional_str(raw.get("label")),
        latitude=_optional_float(raw.get("latitude")),
        longitude=_optional_float(raw.get("longitude")),
        timezone_id=_optional_str(raw.get("timezone_id")),
        detected_at=_parse_timestamp(raw.get("detected_at")),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
