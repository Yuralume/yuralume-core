"""Application service for the operator's profile.

Phase 1 of the world-system roadmap: a thin wrapper around the
repository so callers (REST routes, prompt builder, post-turn
extractor) get a single place to ask "who is the operator". The
service lazily falls back to ``OperatorProfile.default()`` when no
row is stored — that way prompt rendering and memory extraction can
unconditionally call ``get_current()`` without each one re-implementing
the fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    UNSET,
    OperatorProfile,
    _Unset,
)


class OperatorProfileService:
    def __init__(self, repository: OperatorProfileRepositoryPort) -> None:
        self._repository = repository

    async def get_current(self) -> OperatorProfile:
        """Return the active operator profile, falling back to the
        placeholder default when nothing is stored yet.

        Never returns ``None`` — callers can use the result directly
        in prompts. ``OperatorProfile.has_real_name()`` lets them tell
        the placeholder apart from a saved profile."""
        stored = await self._repository.get_default()
        if stored is not None:
            return stored
        return OperatorProfile.default()

    async def get_for_user(self, user_id: str) -> OperatorProfile:
        """Return the operator profile for a specific ``user_id``.

        Post-auth replacement for ``get_current`` — chat / proactive /
        post-turn pipelines call this with the user resolved from the
        bearer token. Falls back to the singleton default profile when
        the row is missing so callers don't need to guard the result.
        """
        stored = await self._repository.get(user_id)
        if stored is not None:
            return stored
        # Asked-for user has no row — fall back to default singleton so
        # downstream prompt rendering still has *some* operator entity
        # rather than crash. Caller already trusts the id (token-derived)
        # so we never leak another user's row here.
        default = await self._repository.get_default()
        if default is not None:
            return default
        return OperatorProfile.default()

    async def update_default(
        self,
        *,
        display_name: str | None = None,
        aliases: tuple[str, ...] | list[str] | None = None,
        pronouns: str | None = None,
        current_status: str | None | _Unset = UNSET,
        country_code: str | None | _Unset = UNSET,
        latitude: float | None | _Unset = UNSET,
        longitude: float | None | _Unset = UNSET,
        location_label: str | None | _Unset = UNSET,
    ) -> OperatorProfile:
        """Upsert the default operator profile.

        Loads the current profile (or creates a default placeholder),
        applies the partial update, and saves it back. Returns the
        post-update entity for the caller to surface in the response."""
        existing = await self._repository.get_default()
        status, status_set_at = _normalise_current_status_update(current_status)
        if existing is None:
            base = OperatorProfile(
                id=DEFAULT_OPERATOR_ID,
                display_name_locked=bool(display_name and display_name.strip()),
                display_name=(
                    display_name.strip()
                    if display_name and display_name.strip()
                    else OperatorProfile.default().display_name
                ),
                aliases=tuple(aliases) if aliases is not None else (),
                pronouns=(
                    pronouns.strip() if pronouns and pronouns.strip() else None
                ),
                current_status=status,
                current_status_set_at=status_set_at,
                country_code=None if country_code is UNSET else country_code,
                latitude=None if latitude is UNSET else latitude,
                longitude=None if longitude is UNSET else longitude,
                location_label=(
                    None if location_label is UNSET else location_label
                ),
            )
            await self._repository.save(base)
            return base
        rename_aliases, display_name_locked = _resolve_rename(
            existing, display_name, aliases,
        )
        updated = existing.update(
            display_name=display_name,
            aliases=rename_aliases,
            pronouns=pronouns,
            display_name_locked=display_name_locked,
            current_status=current_status,
            current_status_set_at=status_set_at,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            location_label=location_label,
        )
        await self._repository.save(updated)
        return updated

    async def update_for_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        aliases: tuple[str, ...] | list[str] | None = None,
        pronouns: str | None = None,
        current_status: str | None | _Unset = UNSET,
        country_code: str | None | _Unset = UNSET,
        latitude: float | None | _Unset = UNSET,
        longitude: float | None | _Unset = UNSET,
        location_label: str | None | _Unset = UNSET,
    ) -> OperatorProfile:
        """Upsert ``user_id``'s operator profile (partial update).

        Post-auth replacement for :meth:`update_default` — each
        authenticated user updates only their own row. Falls back to
        creating the row from scratch when nothing's stored yet so a
        fresh user can update their profile without a follow-up
        endpoint. Operator-row absence means the user was created by
        AuthService but never saved a profile."""
        existing = await self._repository.get(user_id)
        status, status_set_at = _normalise_current_status_update(current_status)
        if existing is None:
            # Synthesise a placeholder row keyed by the real user_id.
            # ``display_name`` may be empty here — Pydantic accepts that
            # because the entity treats display_name as a free-form
            # string; the prompt builder falls back to legacy "使用者"
            # wording until the user picks a name.
            base = OperatorProfile(
                id=user_id,
                display_name_locked=bool(display_name and display_name.strip()),
                display_name=(
                    display_name.strip()
                    if display_name and display_name.strip()
                    else OperatorProfile.default().display_name
                ),
                aliases=tuple(aliases) if aliases is not None else (),
                pronouns=(
                    pronouns.strip() if pronouns and pronouns.strip() else None
                ),
                current_status=status,
                current_status_set_at=status_set_at,
                country_code=None if country_code is UNSET else country_code,
                latitude=None if latitude is UNSET else latitude,
                longitude=None if longitude is UNSET else longitude,
                location_label=(
                    None if location_label is UNSET else location_label
                ),
            )
            await self._repository.save(base)
            return base
        rename_aliases, display_name_locked = _resolve_rename(
            existing, display_name, aliases,
        )
        updated = existing.update(
            display_name=display_name,
            aliases=rename_aliases,
            pronouns=pronouns,
            display_name_locked=display_name_locked,
            current_status=current_status,
            current_status_set_at=status_set_at,
            country_code=country_code,
            latitude=latitude,
            longitude=longitude,
            location_label=location_label,
        )
        await self._repository.save(updated)
        return updated


_MAX_STORED_ALIASES = 8
"""Upper bound on stored aliases so repeated renames can't grow the row
unbounded. The prompt resolver renders fewer than this anyway."""


def _resolve_rename(
    existing: OperatorProfile,
    display_name: str | None,
    aliases: tuple[str, ...] | list[str] | None,
) -> tuple[tuple[str, ...] | list[str] | None, bool | None]:
    """Apply the alias-bridge + lock when the player changes their
    display name.

    Returns ``(aliases_for_update, display_name_locked_for_update)``:

    - When no name change is requested, the caller's ``aliases`` value
      passes through untouched and the lock is left alone (``None``).
    - When the player sets a name, the old display name is folded into
      the alias pool (so memories under the old name still resolve to
      the same person), deduped and capped, and the row is locked so a
      cloud OAuth re-login can't clobber the edit.
    """
    if not (display_name and display_name.strip()):
        return aliases, None
    new_name = display_name.strip()
    base = list(aliases) if aliases is not None else list(existing.aliases)
    old = existing.display_name
    if existing.has_real_name() and old and old != new_name and old not in base:
        base.insert(0, old)
    return _dedupe_cap(base, exclude=new_name), True


def _dedupe_cap(items: list[str], *, exclude: str) -> tuple[str, ...]:
    seen: set[str] = {exclude}
    out: list[str] = []
    for item in items:
        text = (item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out[:_MAX_STORED_ALIASES])


def _normalise_current_status_update(
    current_status: str | None | _Unset,
) -> tuple[str | None, datetime | None]:
    if current_status is UNSET:
        return None, None
    if isinstance(current_status, str) and current_status.strip():
        return current_status.strip(), datetime.now(timezone.utc)
    return None, None
