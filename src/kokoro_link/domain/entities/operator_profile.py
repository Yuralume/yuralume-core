"""OperatorProfile — the human controlling Yuralume.

Phase 1 of the world-system roadmap (see ``docs/TODO.md`` §🟣). For
now this is effectively singleton: ``DEFAULT_OPERATOR_ID`` names the
single row most installs use. The entity nonetheless carries an ``id``
so multi-operator deployments later won't need a schema migration —
the ``operator_profiles`` table is already keyed by id.

Why an entity at all (vs. a couple of preference rows)?
- ``display_name`` and ``aliases`` flow into prompt rendering and the
  post-turn extractor. Centralising them in one type lets the prompt
  builder call ``operator.as_actor()`` rather than fish around for
  loose strings.
- ``pronouns`` is reserved for prompt-level grammar hints (e.g. "他"
  vs "她") later; we already shape the field so the migration doesn't
  need to land twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Final

from kokoro_link.domain.value_objects.actor import Actor
from kokoro_link.domain.value_objects.timezone import (
    DEFAULT_TIMEZONE_ID,
    normalise_timezone_id,
)


DEFAULT_OPERATOR_ID = "default"
"""The id of the singleton operator row in single-user installs.

Reads / writes that don't pin a specific operator should use this so
the rest of the system can switch to multi-operator without code
changes — only the resolver (which operator is "current") moves."""

DEFAULT_OPERATOR_DISPLAY_NAME = "操作者"
"""Fallback name when the operator hasn't filled in their profile yet.

We deliberately don't default to ``"使用者"`` — that's the role label
and pre-Phase-1 prompts already used it; we want the absence-of-name
state to look distinct so prompts can be tuned for it later."""


DEFAULT_PRIMARY_LANGUAGE = "zh-TW"
"""BCP 47 tag for the operator's chosen interaction language.

This is the **content** language for everything the LLM produces for
this operator (chat, memory, persona summaries, story output, feed
posts). It is fixed at registration / setup time — see
``docs/FRONTEND_I18N_PLAN.md`` §使用者主要語言. The frontend UI locale
is independent and may differ.

Stored as-is on the entity; light normalisation (trim, language part
lowercased, 2-letter region uppercased) happens in ``__post_init__``
so persisted values stay canonical even when callers feed mixed case.
``zh-TW`` is the default both because the project is TW-first and
because the alembic backfill uses the same value."""


DEFAULT_OPERATOR_TIMEZONE = DEFAULT_TIMEZONE_ID
"""IANA timezone id for user-facing civil dates/times.

DB/server instants remain UTC. This value only controls how date-only
fields and visible clock times are interpreted for this operator.
"""


class _Unset:
    pass


UNSET: Final = _Unset()
"""Sentinel for tri-state profile updates.

``None`` is a meaningful value for some mutable profile fields (for
example clearing ``current_status``), so callers that want "leave this
field untouched" use this sentinel instead.
"""


@dataclass(frozen=True, slots=True)
class OperatorProfile:
    """The person on the other side of the chat window.

    All fields are conceptually mutable via ``update`` (returns a new
    instance — entity is frozen for hashability). ``id`` is set once
    at creation; reassigning it would mean a different operator.

    ``email`` / ``password_hash`` / ``is_admin`` carry the auth state
    introduced by MULTI_USER_AUTH_PLAN Batch 1. They're optional on the
    domain object so the in-memory repo (used by tests) doesn't have to
    fabricate them; pre-setup default user has both auth fields empty.
    """

    id: str
    display_name: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    pronouns: str | None = None
    email: str | None = None
    password_hash: str | None = None
    is_admin: bool = False
    display_name_locked: bool = False
    primary_language: str = DEFAULT_PRIMARY_LANGUAGE
    timezone_id: str = DEFAULT_OPERATOR_TIMEZONE
    current_status: str | None = None
    current_status_set_at: datetime | None = None
    country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_label: str | None = None
    cloud_account_id: str | None = None
    cloud_tenant_id: str | None = None
    cloud_tenant_tier: str = "standard"
    auth_provider: str = "local"

    def __post_init__(self) -> None:
        operator_id = (self.id or "").strip()
        if not operator_id:
            raise ValueError("OperatorProfile.id must be non-empty")
        object.__setattr__(self, "id", operator_id)
        name = (self.display_name or "").strip()
        if not name:
            raise ValueError("OperatorProfile.display_name must be non-empty")
        object.__setattr__(self, "display_name", name)
        cleaned_aliases = tuple(
            alias.strip() for alias in self.aliases if alias and alias.strip()
        )
        object.__setattr__(self, "aliases", cleaned_aliases)
        if self.pronouns is not None:
            trimmed = self.pronouns.strip()
            object.__setattr__(self, "pronouns", trimmed or None)
        if self.email is not None:
            normalised_email = self.email.strip().lower()
            object.__setattr__(self, "email", normalised_email or None)
        if self.password_hash is not None:
            stripped_hash = self.password_hash.strip()
            object.__setattr__(self, "password_hash", stripped_hash or None)
        object.__setattr__(
            self, "primary_language",
            normalise_language_tag(self.primary_language),
        )
        object.__setattr__(
            self, "timezone_id",
            normalise_timezone_id(self.timezone_id),
        )
        if self.current_status is not None:
            status = self.current_status.strip()
            object.__setattr__(self, "current_status", status or None)
        if self.current_status is None:
            object.__setattr__(self, "current_status_set_at", None)
        elif self.current_status_set_at is not None:
            set_at = self.current_status_set_at
            if set_at.tzinfo is None:
                set_at = set_at.replace(tzinfo=timezone.utc)
            object.__setattr__(self, "current_status_set_at", set_at)
        object.__setattr__(
            self, "country_code", _normalise_country_code(self.country_code),
        )
        object.__setattr__(
            self, "latitude", _normalise_latitude(self.latitude),
        )
        object.__setattr__(
            self, "longitude", _normalise_longitude(self.longitude),
        )
        object.__setattr__(
            self, "location_label", _normalise_location_label(self.location_label),
        )
        object.__setattr__(
            self, "cloud_account_id", _normalise_cloud_id(self.cloud_account_id),
        )
        object.__setattr__(
            self, "cloud_tenant_id", _normalise_cloud_id(self.cloud_tenant_id),
        )
        object.__setattr__(
            self,
            "cloud_tenant_tier",
            _normalise_cloud_tier(self.cloud_tenant_tier),
        )
        provider = (self.auth_provider or "local").strip().lower()
        if provider not in {"local", "cloud"}:
            raise ValueError(f"invalid auth provider: {self.auth_provider!r}")
        object.__setattr__(self, "auth_provider", provider)

    def has_password(self) -> bool:
        """``True`` after :meth:`setup` / a fresh ``create_user`` writes
        a hash. Used by the front-end ``GET /auth/config`` flow to
        decide whether to route to /setup."""
        return bool(self.password_hash)

    @classmethod
    def default(cls) -> "OperatorProfile":
        """Construct the placeholder profile used before the operator
        has filled anything in. Prompt rendering and memory extraction
        use ``DEFAULT_OPERATOR_DISPLAY_NAME`` as a sentinel for "no
        real name yet"; downstream consumers can detect that and fall
        back to the legacy "使用者" wording."""
        return cls(id=DEFAULT_OPERATOR_ID, display_name=DEFAULT_OPERATOR_DISPLAY_NAME)

    def has_real_name(self) -> bool:
        """Return ``True`` once the operator picked an actual name.

        Prompt builder uses this to decide whether to switch from
        legacy "使用者" wording to a name-based reference. A naive
        equality check on ``DEFAULT_OPERATOR_DISPLAY_NAME`` would
        pass through if a real operator happened to be named "操作者",
        but that collision is acceptable — the only consequence is the
        prompts staying in the legacy wording for that one user."""
        return self.display_name != DEFAULT_OPERATOR_DISPLAY_NAME

    def as_actor(self) -> Actor:
        """Project to the generic ``Actor`` shape used by prompt /
        extractor / participant references. Always operator-kind."""
        return Actor(
            kind="operator",
            id=self.id,
            display_name=self.display_name,
            aliases=self.aliases,
        )

    def update(
        self,
        *,
        display_name: str | None = None,
        aliases: tuple[str, ...] | list[str] | None = None,
        pronouns: str | None = None,
        email: str | None = None,
        password_hash: str | None = None,
        is_admin: bool | None = None,
        display_name_locked: bool | None = None,
        current_status: str | None | _Unset = UNSET,
        current_status_set_at: datetime | None = None,
        country_code: str | None | _Unset = UNSET,
        latitude: float | None | _Unset = UNSET,
        longitude: float | None | _Unset = UNSET,
        location_label: str | None | _Unset = UNSET,
        cloud_account_id: str | None | _Unset = UNSET,
        cloud_tenant_id: str | None | _Unset = UNSET,
        cloud_tenant_tier: str | None = None,
        auth_provider: str | None = None,
    ) -> "OperatorProfile":
        """Copy-on-write update. ``None`` means "leave alone"; pass an
        explicit empty tuple / empty string to clear a field.

        Note ``pronouns=None`` cannot distinguish "leave alone" from
        "clear" — clearing is rare, so we accept the asymmetry; if
        clearing becomes important we can switch to a sentinel like
        ``Character.update`` did for ``arc_template_id``. Same caveat
        for the new auth fields — operators don't unset email or
        password_hash after they're set, so we don't bother with
        sentinel tri-state.

        ``current_status`` uses ``UNSET`` for "leave alone" so
        explicit ``None`` can clear both status text and set-at time.

        Location fields use the same sentinel because they are mutable:
        omitting them leaves the current geographic fact unchanged,
        while explicit ``None`` clears the per-operator override so
        external fact providers can fall back to deployment defaults.

        ``primary_language`` is intentionally **not** exposed here:
        it is fixed at creation / setup time and cannot be updated
        later (changing it would desynchronise historical LLM content
        from new content). ``timezone_id`` is the same class of pinned
        identity setting: changing it later would reinterpret memories,
        schedules, birthdays, daily caps, and date-only history.
        """
        next_aliases: tuple[str, ...]
        if aliases is None:
            next_aliases = self.aliases
        else:
            next_aliases = tuple(aliases)
        next_current_status = self.current_status
        next_current_status_set_at = self.current_status_set_at
        if current_status is not UNSET:
            status = current_status.strip() if isinstance(current_status, str) else ""
            if status:
                next_current_status = status
                next_current_status_set_at = current_status_set_at
            else:
                next_current_status = None
                next_current_status_set_at = None
        return replace(
            self,
            display_name=self.display_name if display_name is None else display_name,
            aliases=next_aliases,
            pronouns=self.pronouns if pronouns is None else pronouns,
            email=self.email if email is None else email,
            password_hash=(
                self.password_hash if password_hash is None else password_hash
            ),
            is_admin=self.is_admin if is_admin is None else is_admin,
            display_name_locked=(
                self.display_name_locked
                if display_name_locked is None
                else display_name_locked
            ),
            current_status=next_current_status,
            current_status_set_at=next_current_status_set_at,
            country_code=(
                self.country_code if country_code is UNSET else country_code
            ),
            latitude=self.latitude if latitude is UNSET else latitude,
            longitude=self.longitude if longitude is UNSET else longitude,
            location_label=(
                self.location_label if location_label is UNSET else location_label
            ),
            cloud_account_id=(
                self.cloud_account_id
                if cloud_account_id is UNSET
                else cloud_account_id
            ),
            cloud_tenant_id=(
                self.cloud_tenant_id if cloud_tenant_id is UNSET else cloud_tenant_id
            ),
            cloud_tenant_tier=(
                self.cloud_tenant_tier
                if cloud_tenant_tier is None
                else cloud_tenant_tier
            ),
            auth_provider=(
                self.auth_provider if auth_provider is None else auth_provider
            ),
        )


# ----------------------------------------------------------------------
# module helpers
# ----------------------------------------------------------------------


def normalise_language_tag(raw: str | None) -> str:
    """Light BCP 47 normaliser used by the entity and DTOs.

    Trims whitespace, lowercases the language subtag, uppercases a
    2-letter region subtag, capitalises a 4-letter script subtag, and
    leaves other subtags untouched. Returns the project default when
    the input is empty / None — callers that need stricter behaviour
    should check for empty before calling.

    Raises ``ValueError`` only when the language subtag is structurally
    invalid (not 2-3 alpha letters) — we want to refuse obviously
    broken input like ``"123"`` or ``"chinese (traditional)"`` rather
    than silently corrupt the LLM prompt-fact layer downstream.
    """
    if raw is None:
        return DEFAULT_PRIMARY_LANGUAGE
    trimmed = raw.strip()
    if not trimmed:
        return DEFAULT_PRIMARY_LANGUAGE
    parts = [p for p in trimmed.split("-") if p]
    if not parts:
        return DEFAULT_PRIMARY_LANGUAGE
    head = parts[0].lower()
    if not (2 <= len(head) <= 3) or not head.isalpha():
        raise ValueError(f"invalid language tag: {raw!r}")
    out = [head]
    for sub in parts[1:]:
        if len(sub) == 2 and sub.isalpha():
            out.append(sub.upper())
        elif len(sub) == 4 and sub.isalpha():
            out.append(sub.capitalize())
        else:
            out.append(sub)
    return "-".join(out)


def _normalise_country_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    code = raw.strip().upper()
    if not code:
        return None
    if len(code) != 2 or not code.isalpha():
        raise ValueError(f"invalid country code: {raw!r}")
    return code


def _normalise_latitude(raw: float | str | int | None) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid latitude: {raw!r}") from exc
    if value < -90.0 or value > 90.0:
        raise ValueError(f"invalid latitude: {raw!r}")
    return value


def _normalise_longitude(raw: float | str | int | None) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid longitude: {raw!r}") from exc
    if value < -180.0 or value > 180.0:
        raise ValueError(f"invalid longitude: {raw!r}")
    return value


def _normalise_location_label(raw: str | None) -> str | None:
    if raw is None:
        return None
    label = raw.strip()
    return label or None


def _normalise_cloud_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _normalise_cloud_tier(raw: str | None) -> str:
    tier = (raw or "standard").strip().lower()
    return tier or "standard"
