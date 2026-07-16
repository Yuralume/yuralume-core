"""User-scoped temporary NSFW mode state.

The mode is a routing and data-flow fact, not content detection.  It is
enabled manually by the user and expires lazily by idle TTL. The community
routing target is installation-wide Admin configuration, while the active
state remains per-user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from kokoro_link.application.services.scoped_preferences import (
    delete_user_preference,
    set_user_preference,
    user_preference_key,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.repositories import PreferencesRepositoryPort

_LOGGER = logging.getLogger(__name__)

NSFW_MODE_PREFERENCE_KEY = "nsfw_mode"
NSFW_MODE_TARGET_PREFERENCE_KEY = "nsfw_mode_target"
NSFW_MODE_DEFAULT_TTL_SECONDS = 30 * 60
NSFW_MODE_EVENT_LOG_LIMIT = 50

CONTENT_MODE_NORMAL = "normal"
CONTENT_MODE_NSFW = "nsfw"
MEMORY_TAG_NSFW_MODE = "content_mode:nsfw"


class NsfwModeTargetError(ValueError):
    """Raised when callers try to enable the mode without full targets."""


@dataclass(frozen=True, slots=True)
class NsfwModeTarget:
    llm_provider_id: str
    llm_model_id: str
    image_profile_id: str


@dataclass(frozen=True, slots=True)
class NsfwModeStatus:
    active: bool
    configured: bool
    ttl_seconds: int
    last_activity_at: datetime | None = None
    expires_at: datetime | None = None
    llm_provider_id: str | None = None
    llm_model_id: str | None = None
    image_profile_id: str | None = None

    @property
    def target(self) -> NsfwModeTarget | None:
        if not self.active or not self.configured:
            return None
        return self.configured_target

    @property
    def configured_target(self) -> NsfwModeTarget | None:
        if not self.configured:
            return None
        assert self.llm_provider_id is not None
        assert self.llm_model_id is not None
        assert self.image_profile_id is not None
        return NsfwModeTarget(
            llm_provider_id=self.llm_provider_id,
            llm_model_id=self.llm_model_id,
            image_profile_id=self.image_profile_id,
        )


@dataclass(frozen=True, slots=True)
class NsfwModeUsageMetrics:
    active: bool
    configured: bool
    ttl_seconds: int
    enable_count: int
    manual_disable_count: int
    idle_expired_count: int
    average_active_seconds: int | None = None
    last_enabled_at: datetime | None = None
    last_disabled_at: datetime | None = None
    last_expired_at: datetime | None = None


class NsfwModeService:
    def __init__(
        self,
        *,
        preferences: PreferencesRepositoryPort,
        ttl_seconds: int = NSFW_MODE_DEFAULT_TTL_SECONDS,
        clock: ClockPort | None = None,
    ) -> None:
        self._preferences = preferences
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._clock = clock

    async def get_status(self, *, user_id: str) -> NsfwModeStatus:
        raw = await self._read(user_id=user_id)
        configured_target = await self._configured_target_for_user(
            user_id=user_id,
            raw=raw,
        )
        now = self._now()
        status = self._status_from_raw(
            raw,
            configured_target=configured_target,
            now=now,
        )
        if _needs_expiration_event(raw, status):
            await self._append_event(
                raw=raw,
                user_id=user_id,
                kind="expired",
                at=now,
            )
        return status

    async def active_target(self, *, user_id: str | None) -> NsfwModeTarget | None:
        if not user_id:
            return None
        return (await self.get_status(user_id=user_id)).target

    async def configured_target(
        self, *, user_id: str | None,
    ) -> NsfwModeTarget | None:
        if not user_id:
            return None
        raw = await self._read(user_id=user_id)
        return await self._configured_target_for_user(user_id=user_id, raw=raw)

    async def get_global_target(self) -> NsfwModeTarget | None:
        raw = await self._read_global_target()
        if not isinstance(raw, dict):
            return None
        return _target_from_raw(raw)

    async def set_global_target(
        self,
        *,
        llm_provider_id: str,
        llm_model_id: str,
        image_profile_id: str,
    ) -> NsfwModeTarget:
        target = _target_or_raise(
            llm_provider_id=llm_provider_id,
            llm_model_id=llm_model_id,
            image_profile_id=image_profile_id,
        )
        await self._preferences.set(
            NSFW_MODE_TARGET_PREFERENCE_KEY,
            _target_to_raw(target),
        )
        return target

    async def enable(
        self,
        *,
        user_id: str,
    ) -> NsfwModeStatus:
        raw = await self._read(user_id=user_id)
        target = await self._configured_target_for_user(user_id=user_id, raw=raw)
        if target is None:
            raise NsfwModeTargetError(
                "nsfw mode requires an admin-configured routing target",
            )
        now = self._now()
        value = {
            "active": True,
            "last_activity_at": now.isoformat(),
            "events": _append_event_to_raw(raw, kind="enabled", at=now),
        }
        await set_user_preference(
            self._preferences,
            NSFW_MODE_PREFERENCE_KEY,
            value,
            user_id=user_id,
        )
        return self._status_from_raw(value, configured_target=target, now=now)

    async def disable(self, *, user_id: str) -> NsfwModeStatus:
        raw = await self._read(user_id=user_id)
        target = await self._configured_target_for_user(user_id=user_id, raw=raw)
        if target is None:
            await delete_user_preference(
                self._preferences,
                NSFW_MODE_PREFERENCE_KEY,
                user_id=user_id,
            )
            return self._status_from_raw(
                None,
                configured_target=None,
                now=self._now(),
            )
        if not isinstance(raw, dict):
            return self._status_from_raw(
                None,
                configured_target=target,
                now=self._now(),
            )
        now = self._now()
        last_activity_at = (
            raw.get("last_activity_at")
            if isinstance(raw.get("last_activity_at"), str)
            else now.isoformat()
        )
        value = {
            "active": False,
            "last_activity_at": last_activity_at,
            "events": _append_event_to_raw(raw, kind="disabled", at=now),
        }
        await set_user_preference(
            self._preferences,
            NSFW_MODE_PREFERENCE_KEY,
            value,
            user_id=user_id,
        )
        return self._status_from_raw(value, configured_target=target, now=now)

    async def refresh_activity(self, *, user_id: str) -> NsfwModeStatus:
        raw = await self._read(user_id=user_id)
        configured_target = await self._configured_target_for_user(
            user_id=user_id,
            raw=raw,
        )
        status = self._status_from_raw(
            raw,
            configured_target=configured_target,
            now=self._now(),
        )
        if status.target is None or not isinstance(raw, dict):
            return status
        refreshed = dict(raw)
        now = self._now()
        refreshed["last_activity_at"] = now.isoformat()
        await set_user_preference(
            self._preferences,
            NSFW_MODE_PREFERENCE_KEY,
            refreshed,
            user_id=user_id,
        )
        return self._status_from_raw(
            refreshed,
            configured_target=configured_target,
            now=now,
        )

    async def content_mode_for_write(self, *, user_id: str | None) -> str:
        target = await self.active_target(user_id=user_id)
        return CONTENT_MODE_NSFW if target is not None else CONTENT_MODE_NORMAL

    async def usage_metrics(self, *, user_id: str) -> NsfwModeUsageMetrics:
        status = await self.get_status(user_id=user_id)
        raw = await self._read(user_id=user_id)
        events = _events_from_raw(raw)
        return _usage_metrics_from_events(
            events,
            status=status,
            now=self._now(),
        )

    async def _read(self, *, user_id: str) -> Any:
        try:
            return await self._preferences.get(
                user_preference_key(user_id, NSFW_MODE_PREFERENCE_KEY),
            )
        except Exception:
            _LOGGER.exception("nsfw mode: preferences read failed user_id=%s", user_id)
            return None

    async def _read_global_target(self) -> Any:
        try:
            return await self._preferences.get(NSFW_MODE_TARGET_PREFERENCE_KEY)
        except Exception:
            _LOGGER.exception("nsfw mode: target preferences read failed")
            return None

    async def _configured_target_for_user(
        self,
        *,
        user_id: str,
        raw: object,
    ) -> NsfwModeTarget | None:
        global_target = await self.get_global_target()
        if global_target is not None:
            return global_target
        if isinstance(raw, dict):
            return _target_from_raw(raw)
        return None

    def _now(self) -> datetime:
        value = self._clock.now() if self._clock is not None else datetime.now(timezone.utc)
        return ensure_utc(value)

    async def _append_event(
        self,
        *,
        raw: object,
        user_id: str,
        kind: str,
        at: datetime,
    ) -> None:
        if not isinstance(raw, dict):
            return
        value = dict(raw)
        value["events"] = _append_event_to_raw(raw, kind=kind, at=at)
        try:
            await set_user_preference(
                self._preferences,
                NSFW_MODE_PREFERENCE_KEY,
                value,
                user_id=user_id,
            )
        except Exception:
            _LOGGER.exception(
                "nsfw mode: event write failed user_id=%s kind=%s",
                user_id,
                kind,
            )

    def _status_from_raw(
        self,
        raw: object,
        *,
        configured_target: NsfwModeTarget | None,
        now: datetime,
    ) -> NsfwModeStatus:
        if not isinstance(raw, dict):
            return NsfwModeStatus(
                active=False,
                configured=configured_target is not None,
                ttl_seconds=self._ttl_seconds,
                llm_provider_id=(
                    configured_target.llm_provider_id if configured_target else None
                ),
                llm_model_id=(
                    configured_target.llm_model_id if configured_target else None
                ),
                image_profile_id=(
                    configured_target.image_profile_id if configured_target else None
                ),
            )
        last_activity_at = _parse_datetime(raw.get("last_activity_at"))
        expires_at = (
            last_activity_at + timedelta(seconds=self._ttl_seconds)
            if last_activity_at is not None
            else None
        )
        active_flag = bool(raw.get("active", False))
        active = (
            active_flag
            and configured_target is not None
            and expires_at is not None
            and now < expires_at
        )
        return NsfwModeStatus(
            active=active,
            configured=configured_target is not None,
            ttl_seconds=self._ttl_seconds,
            last_activity_at=last_activity_at,
            expires_at=expires_at,
            llm_provider_id=(
                configured_target.llm_provider_id if configured_target else None
            ),
            llm_model_id=(
                configured_target.llm_model_id if configured_target else None
            ),
            image_profile_id=(
                configured_target.image_profile_id if configured_target else None
            ),
        )


def _target_or_raise(
    *,
    llm_provider_id: str,
    llm_model_id: str,
    image_profile_id: str,
) -> NsfwModeTarget:
    target = NsfwModeTarget(
        llm_provider_id=llm_provider_id.strip(),
        llm_model_id=llm_model_id.strip(),
        image_profile_id=image_profile_id.strip(),
    )
    if (
        not target.llm_provider_id
        or not target.llm_model_id
        or not target.image_profile_id
    ):
        raise NsfwModeTargetError(
            "nsfw mode requires llm_provider_id, llm_model_id, and image_profile_id",
        )
    return target


def _target_from_raw(raw: dict[str, Any]) -> NsfwModeTarget | None:
    llm = raw.get("llm")
    if not isinstance(llm, dict):
        return None
    try:
        return _target_or_raise(
            llm_provider_id=str(llm.get("provider_id") or ""),
            llm_model_id=str(llm.get("model_id") or ""),
            image_profile_id=str(raw.get("image_profile_id") or ""),
        )
    except NsfwModeTargetError:
        return None


def _target_to_raw(target: NsfwModeTarget) -> dict[str, object]:
    return {
        "llm": {
            "provider_id": target.llm_provider_id,
            "model_id": target.llm_model_id,
        },
        "image_profile_id": target.image_profile_id,
    }


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ensure_utc(parsed)


def _append_event_to_raw(
    raw: object,
    *,
    kind: str,
    at: datetime,
) -> list[dict[str, str]]:
    events = [dict(event) for event in _events_from_raw(raw)]
    if (
        kind == "expired"
        and events
        and events[-1].get("kind") == "expired"
        and events[-1].get("last_activity_at")
        == (raw.get("last_activity_at") if isinstance(raw, dict) else None)
    ):
        return events
    event = {
        "kind": kind,
        "at": ensure_utc(at).isoformat(),
    }
    if isinstance(raw, dict) and isinstance(raw.get("last_activity_at"), str):
        event["last_activity_at"] = str(raw["last_activity_at"])
    events.append(event)
    return events[-NSFW_MODE_EVENT_LOG_LIMIT:]


def _events_from_raw(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, dict):
        return []
    events = raw.get("events")
    if not isinstance(events, list):
        return []
    normalized: list[dict[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        at = event.get("at")
        if not isinstance(kind, str) or not isinstance(at, str):
            continue
        normalized.append({
            "kind": kind,
            "at": at,
            **(
                {"last_activity_at": str(event["last_activity_at"])}
                if isinstance(event.get("last_activity_at"), str)
                else {}
            ),
        })
    return normalized[-NSFW_MODE_EVENT_LOG_LIMIT:]


def _needs_expiration_event(raw: object, status: NsfwModeStatus) -> bool:
    if not isinstance(raw, dict):
        return False
    if not bool(raw.get("active", False)) or status.active:
        return False
    if not status.configured or status.expires_at is None:
        return False
    events = _events_from_raw(raw)
    if not events:
        return True
    last_activity_at = raw.get("last_activity_at")
    return not (
        events[-1].get("kind") == "expired"
        and events[-1].get("last_activity_at") == last_activity_at
    )


def _usage_metrics_from_events(
    events: list[dict[str, str]],
    *,
    status: NsfwModeStatus,
    now: datetime,
) -> NsfwModeUsageMetrics:
    parsed = [
        (event["kind"], at)
        for event in events
        if (at := _parse_datetime(event.get("at"))) is not None
    ]
    enable_count = sum(1 for kind, _ in parsed if kind == "enabled")
    manual_disable_count = sum(1 for kind, _ in parsed if kind == "disabled")
    idle_expired_count = sum(1 for kind, _ in parsed if kind == "expired")

    durations: list[int] = []
    active_started_at: datetime | None = None
    last_enabled_at: datetime | None = None
    last_disabled_at: datetime | None = None
    last_expired_at: datetime | None = None
    for kind, at in parsed:
        if kind == "enabled":
            active_started_at = at
            last_enabled_at = at
        elif kind in {"disabled", "expired"}:
            if active_started_at is not None:
                durations.append(max(0, int((at - active_started_at).total_seconds())))
                active_started_at = None
            if kind == "disabled":
                last_disabled_at = at
            else:
                last_expired_at = at
    if status.active and active_started_at is not None:
        durations.append(max(0, int((ensure_utc(now) - active_started_at).total_seconds())))

    return NsfwModeUsageMetrics(
        active=status.active,
        configured=status.configured,
        ttl_seconds=status.ttl_seconds,
        enable_count=enable_count,
        manual_disable_count=manual_disable_count,
        idle_expired_count=idle_expired_count,
        average_active_seconds=(
            int(sum(durations) / len(durations)) if durations else None
        ),
        last_enabled_at=last_enabled_at,
        last_disabled_at=last_disabled_at,
        last_expired_at=last_expired_at,
    )
