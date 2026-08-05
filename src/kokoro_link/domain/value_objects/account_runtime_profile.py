from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

# The effective-multiplier clamp is evaluated on every gate check of every
# scheduler tick; an over-cap tier would otherwise repeat the same warning
# hundreds of times per hour. Warn once per distinct (profile, activity,
# effective) combination — a config change produces a fresh warning.
_WARNED_CLAMPS: set[tuple[str, str, int]] = set()

#: ``tier_profile.billing_shape`` — how the tier's foreground calls are priced.
BILLING_SHAPE_TOKEN_FLOATING = "token_floating"
"""Today's shape: every Gateway call reserves/settles its own token cost."""
BILLING_SHAPE_ACTION_FIXED = "action_fixed"
"""AP2 shape: the player action is the billing unit, at a fixed back-office
price; the calls it makes are covered by that one charge."""

_BILLING_SHAPES = frozenset({
    BILLING_SHAPE_TOKEN_FLOATING,
    BILLING_SHAPE_ACTION_FIXED,
})

DEFAULT_DAILY_OVERAGE_LIMIT = 5
"""Per-item daily ceiling on player-authorised quota overage purchases."""


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
    background_activity_multiplier: int = 1
    idle_downshift_days: int | None = None
    idle_multiplier: int = 1
    character_ttl: timedelta | None = None
    max_characters: int | None = None
    daily_character_create_limit: int | None = None
    max_messages_per_session: int | None = None
    background_judge_model_pin: str | None = None
    strict_no_fallback: bool = False
    daily_chat_image_limit: int | None = None
    daily_feed_post_limit: int | None = None
    story_scene_daily_limit: int | None = None
    """SC3-B — per-tier daily ceiling on 起幕 (story scene) openings.

    ``None`` (the default) means unlimited, and is the *only* spelling of
    unlimited: unlike ``daily_overage_limit``, this knob has no neutral
    nonzero default to fall back to, so the control-plane column is nullable
    and a ``0`` is rejected there rather than reinterpreted (STORY_SCENE_PLAN
    §3.3 / §4.1 SC3-B). Enforced by :class:`StorySceneQuotaGuard`
    (``application/services/story_scene_quota.py``), not read directly by
    any billing path — hosted pacing only."""
    album_generation_enabled: bool = True
    video_generation_enabled: bool = True
    tts_enabled: bool = True
    billing_shape: str = BILLING_SHAPE_TOKEN_FLOATING
    overage_enabled: bool = False
    daily_overage_limit: int = DEFAULT_DAILY_OVERAGE_LIMIT

    @property
    def uses_action_pricing(self) -> bool:
        """True when player actions carry a fixed back-office price.

        The default is deliberately ``False`` for every un-migrated tier and
        for self-host, so the action-charging path is opt-in per tier.
        """
        return self.billing_shape == BILLING_SHAPE_ACTION_FIXED

    def effective_proactive_multiplier(self, *, idle: bool) -> int:
        return self._effective_multiplier(
            self.proactive_tick_multiplier, idle=idle, activity="proactive",
        )

    def effective_background_multiplier(self, *, idle: bool) -> int:
        return self._effective_multiplier(
            self.background_activity_multiplier,
            idle=idle,
            activity="background",
        )

    def _effective_multiplier(
        self, base: int, *, idle: bool, activity: str,
    ) -> int:
        effective = base * (self.idle_multiplier if idle else 1)
        clamped = min(288, max(1, effective))
        if clamped != effective:
            key = (self.name, activity, effective)
            if key not in _WARNED_CLAMPS:
                _WARNED_CLAMPS.add(key)
                _LOGGER.warning(
                    "account runtime profile %r: clamped effective %s "
                    "multiplier from %d to %d",
                    self.name, activity, effective, clamped,
                )
        return clamped

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
                maximum=288,
                default=default.proactive_tick_multiplier,
                nullable=False, name=name,
            ),
            background_activity_multiplier=_int_knob(
                data, "background_activity_multiplier", minimum=1,
                maximum=288,
                default=default.background_activity_multiplier,
                nullable=False, name=name,
            ),
            idle_downshift_days=_int_knob(
                data, "idle_downshift_days", minimum=1, maximum=3650,
                default=default.idle_downshift_days, nullable=True, name=name,
            ),
            idle_multiplier=_int_knob(
                data, "idle_multiplier", minimum=1, maximum=288,
                default=default.idle_multiplier, nullable=False, name=name,
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
            # SC3-B: minimum=1, not 0 — the control-plane CHECK is
            # ``story_scene_daily_limit IS NULL OR ... > 0``, the mirror image
            # of daily_overage_limit's (NOT NULL, 0 rejected). A stray 0 here
            # is therefore exactly as invalid as a negative value, not a second
            # spelling of "unlimited".
            story_scene_daily_limit=_int_knob(
                data, "story_scene_daily_limit", minimum=1,
                default=default.story_scene_daily_limit,
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
            billing_shape=_choice_knob(
                data, "billing_shape", allowed=_BILLING_SHAPES,
                default=default.billing_shape, name=name,
            ),
            overage_enabled=_bool_knob(
                data, "overage_enabled",
                default=default.overage_enabled, name=name,
            ),
            # Not nullable: "no limit" is not an option an operator should be
            # able to configure by omission on a knob whose whole job is to
            # bound how much a background actor may spend.
            daily_overage_limit=_int_knob(
                data, "daily_overage_limit", minimum=0, maximum=1000,
                default=default.daily_overage_limit, nullable=False,
                name=name,
            ),
        )


DEFAULT_ACCOUNT_RUNTIME_PROFILE = AccountRuntimeProfile(name="default")

DEMO_ACCOUNT_RUNTIME_PROFILE = AccountRuntimeProfile(
    name="demo",
    proactive_tick_multiplier=6,
    background_activity_multiplier=6,
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
    maximum: int | None = None,
    default: int | None,
    nullable: bool,
    name: str,
) -> int | None:
    """Resolve a bounded integer knob with per-knob fail-open fallback."""
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
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
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


def _choice_knob(
    payload: dict[str, Any],
    key: str,
    *,
    allowed: frozenset[str],
    default: str,
    name: str,
) -> str:
    """Resolve a closed-vocabulary string knob.

    An unrecognised value falls back to the default rather than being passed
    through: ``billing_shape`` decides whether the player is charged per action
    or per token, and an unknown third shape has no defined behaviour at all.
    """
    if key not in payload:
        return default
    value = payload[key]
    if isinstance(value, str) and value.strip() in allowed:
        return value.strip()
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
