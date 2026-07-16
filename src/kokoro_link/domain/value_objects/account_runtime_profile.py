from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AccountRuntimeProfile:
    """Runtime policy for an operator account.

    The default profile is intentionally permissive so self-host installs
    keep existing behavior. Hosted demo accounts opt into the restrictive
    profile through their cloud tenant tier projection. Paid hosted tiers
    get their profile from the control-plane (see
    ``from_control_plane_payload``) rather than any hardcoded tier->knob
    mapping in Core.
    """

    name: str
    proactive_tick_multiplier: int = 1
    character_ttl: timedelta | None = None
    max_characters: int | None = None
    daily_character_create_limit: int | None = None
    max_messages_per_session: int | None = None
    background_judge_model_pin: str | None = None
    strict_no_fallback: bool = False
    daily_chat_image_limit: int | None = None
    daily_feed_post_limit: int | None = None
    album_generation_enabled: bool = True
    video_generation_enabled: bool = True
    tts_enabled: bool = True

    @property
    def is_demo(self) -> bool:
        return self.name == "demo"

    @classmethod
    def from_control_plane_payload(
        cls, name: str, payload: Any,
    ) -> "AccountRuntimeProfile":
        """Build a per-tier profile from a control-plane knob payload.

        Fail-open per knob: a missing key falls back to the permissive
        ``DEFAULT_ACCOUNT_RUNTIME_PROFILE`` value; an invalid-typed / out-of
        range value is ignored (also falls back to the default) and logged.
        Unknown keys are ignored. This keeps a malformed control-plane
        response from silently over-restricting a paying tenant."""
        data = payload if isinstance(payload, dict) else {}
        default = DEFAULT_ACCOUNT_RUNTIME_PROFILE
        ttl_days = _int_knob(
            data, "character_ttl_days", minimum=1, default=None,
            nullable=True, name=name,
        )
        return cls(
            name=name,
            proactive_tick_multiplier=_int_knob(
                data, "proactive_tick_multiplier", minimum=1,
                default=default.proactive_tick_multiplier,
                nullable=False, name=name,
            ),
            character_ttl=(
                timedelta(days=ttl_days) if ttl_days is not None else None
            ),
            max_characters=_int_knob(
                data, "max_characters", minimum=1,
                default=default.max_characters, nullable=True, name=name,
            ),
            daily_character_create_limit=_int_knob(
                data, "daily_character_create_limit", minimum=0,
                default=default.daily_character_create_limit,
                nullable=True, name=name,
            ),
            max_messages_per_session=_int_knob(
                data, "max_messages_per_session", minimum=1,
                default=default.max_messages_per_session,
                nullable=True, name=name,
            ),
            daily_chat_image_limit=_int_knob(
                data, "daily_chat_image_limit", minimum=0,
                default=default.daily_chat_image_limit,
                nullable=True, name=name,
            ),
            daily_feed_post_limit=_int_knob(
                data, "daily_feed_post_limit", minimum=0,
                default=default.daily_feed_post_limit,
                nullable=True, name=name,
            ),
            album_generation_enabled=_bool_knob(
                data, "album_generation_enabled",
                default=default.album_generation_enabled, name=name,
            ),
            video_generation_enabled=_bool_knob(
                data, "video_generation_enabled",
                default=default.video_generation_enabled, name=name,
            ),
            tts_enabled=_bool_knob(
                data, "tts_enabled", default=default.tts_enabled, name=name,
            ),
            strict_no_fallback=_bool_knob(
                data, "strict_no_fallback",
                default=default.strict_no_fallback, name=name,
            ),
            background_judge_model_pin=_str_knob(
                data, "background_judge_model_pin",
                default=default.background_judge_model_pin, name=name,
            ),
        )


DEFAULT_ACCOUNT_RUNTIME_PROFILE = AccountRuntimeProfile(name="default")

DEMO_ACCOUNT_RUNTIME_PROFILE = AccountRuntimeProfile(
    name="demo",
    proactive_tick_multiplier=6,
    character_ttl=timedelta(days=3),
    max_characters=1,
    daily_character_create_limit=1,
    max_messages_per_session=80,
    strict_no_fallback=True,
    daily_chat_image_limit=1,
    daily_feed_post_limit=1,
    album_generation_enabled=False,
    video_generation_enabled=False,
    tts_enabled=False,
)


def _int_knob(
    payload: dict[str, Any],
    key: str,
    *,
    minimum: int,
    default: int | None,
    nullable: bool,
    name: str,
) -> int | None:
    """Resolve an integer knob. Missing -> ``default``; explicit ``null`` ->
    ``None`` when ``nullable`` else invalid; a real ``int`` (bools rejected)
    >= ``minimum`` -> that int; anything else -> ``default`` + warning."""
    if key not in payload:
        return default
    value = payload[key]
    if value is None:
        if nullable:
            return None
        _warn_invalid(name, key, value)
        return default
    # ``bool`` is a subclass of ``int`` — reject it so a stray ``true`` isn't
    # silently read as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _warn_invalid(name, key, value)
        return default
    return value


def _bool_knob(
    payload: dict[str, Any], key: str, *, default: bool, name: str,
) -> bool:
    if key not in payload:
        return default
    value = payload[key]
    if isinstance(value, bool):
        return value
    _warn_invalid(name, key, value)
    return default


def _str_knob(
    payload: dict[str, Any], key: str, *, default: str | None, name: str,
) -> str | None:
    """Resolve a non-empty string knob. Missing -> ``default``; ``null`` or a
    blank/whitespace string -> ``None`` ("no pin"); a non-string -> ``default``
    + warning."""
    if key not in payload:
        return default
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        _warn_invalid(name, key, value)
        return default
    cleaned = value.strip()
    return cleaned or None


def _warn_invalid(name: str, key: str, value: Any) -> None:
    _LOGGER.warning(
        "account runtime profile %r: ignoring invalid %s=%r", name, key, value,
    )
