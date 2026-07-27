"""Authoritative external-chat roster projection (LH2).

The hosted LINE official-channel Cloud service asks Core "which characters
may this ``(tenant_id, account_id)`` pair chat with, and how do I render
them?" before it fans an inbound message out to a picker. This service is
the single read-only answer.

Design invariants (``HOSTED_OFFICIAL_LINE_CHANNEL_LH2_DESIGN``):

- **Paired identity, fail-closed.** The tenant and account must both point
  at the *same* local cloud operator projection. Any mismatch resolves to
  ``None`` so the route can answer an opaque 404 — a caller can never probe
  which half was wrong, nor cross a tenant boundary by pinning a foreign
  ``account_id``.
- **Subscription is authorization, freeze is presentation.** A locked
  tenant (``SubscriptionAccessGuard``) or a legacy ``subscription_lapse``
  freeze *removes* the character from the roster entirely. An idle / manual
  (cost-control) freeze keeps the character listed with
  ``background_frozen=True`` — foreground chat still auto-thaws it, so it is
  a live chat target, merely dormant in the background. We deliberately do
  **not** use ``list_active()`` as the authorization source: that flag mixes
  cost-control dormancy with access and would silently hide manually-frozen
  but still-chattable characters.
- **Stable presentation version.** ``presentation_version`` changes iff the
  display name or avatar key changes, so the Cloud side can cache renders
  and invalidate precisely.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_SUBSCRIPTION_LAPSE,
    Character,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile

_PROVIDER_CLOUD = "cloud"
_PRESENTATION_VERSION_LENGTH = 16


async def resolve_cloud_operator(
    operator_repository: OperatorProfileRepositoryPort,
    *,
    tenant_id: str,
    account_id: str,
) -> OperatorProfile | None:
    """Resolve a ``(tenant_id, account_id)`` pair to the single cloud operator
    projection it points at, or ``None`` when it does not.

    Single source of the paired, fail-closed identity check shared by every
    internal external-chat surface (roster read, attachment ingest, …): both
    the account *and* the tenant stamped on the projection must match, the
    operator must be a ``cloud`` operator, and blank halves never resolve. A
    caller can never probe which half was wrong, nor cross a tenant boundary
    by pinning a foreign ``account_id``."""
    tenant = (tenant_id or "").strip()
    account = (account_id or "").strip()
    if not tenant or not account:
        return None
    operator = await operator_repository.get_by_cloud_account_id(account)
    if operator is None or operator.auth_provider != _PROVIDER_CLOUD:
        return None
    # Compare the account too (not just trusting the lookup key) so the check
    # stays symmetric and future-proof against a repository that ever loosens
    # its index.
    if (operator.cloud_account_id or "").strip() != account:
        return None
    if (operator.cloud_tenant_id or "").strip() != tenant:
        return None
    return operator


@dataclass(frozen=True, slots=True)
class RosterCharacterView:
    """One roster row, aligned field-for-field with the OpenAPI
    ``RosterCharacter`` schema (snake_case is the wire shape)."""

    character_id: str
    display_name: str
    background_frozen: bool
    avatar_object_ref: str | None
    presentation_version: str


@dataclass(frozen=True, slots=True)
class RosterView:
    """Resolved roster for one paired ``(tenant_id, account_id)``.

    ``characters`` is already sorted by ``character_id`` so the route can
    serialise it verbatim and callers get a stable ordering."""

    tenant_id: str
    account_id: str
    characters: tuple[RosterCharacterView, ...]


class ExternalChatRosterService:
    """Resolve + project the authoritative roster for a cloud pair.

    Read-only: no method here writes, freezes, or mutates any entity."""

    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        operator_profile_repository: OperatorProfileRepositoryPort,
        subscription_access_guard: SubscriptionAccessGuard,
        object_storage: ObjectStoragePort,
    ) -> None:
        self._characters = character_repository
        self._operators = operator_profile_repository
        self._guard = subscription_access_guard
        self._object_storage = object_storage

    async def resolve_roster(
        self, *, tenant_id: str, account_id: str,
    ) -> RosterView | None:
        """Return the roster for the pair, or ``None`` when the pair does
        not resolve to a single cloud operator (route → opaque 404)."""
        operator = await self._resolve_operator(tenant_id, account_id)
        if operator is None:
            return None

        # Tenant lock is an operator-wide (tenant-wide) decision: the
        # operator's whole roster shares one tenant, so resolve it once.
        # A locked tenant yields an empty roster rather than a 404 — the
        # pair is valid, it simply has no chattable characters right now.
        tenant_allowed = await self._guard.is_operator_allowed(operator.id)

        characters = await self._characters.list_for_user(operator.id)
        views: list[RosterCharacterView] = []
        for character in characters:
            view = self._project(character, tenant_allowed=tenant_allowed)
            if view is not None:
                views.append(view)
        views.sort(key=lambda item: item.character_id)

        return RosterView(
            tenant_id=(operator.cloud_tenant_id or "").strip(),
            account_id=(operator.cloud_account_id or "").strip(),
            characters=tuple(views),
        )

    async def _resolve_operator(
        self, tenant_id: str, account_id: str,
    ) -> OperatorProfile | None:
        # Delegates to the shared module-level resolver so the paired,
        # fail-closed identity semantics live in exactly one place.
        return await resolve_cloud_operator(
            self._operators, tenant_id=tenant_id, account_id=account_id,
        )

    def _project(
        self, character: Character, *, tenant_allowed: bool,
    ) -> RosterCharacterView | None:
        # Subscription denial removes the character from the roster entirely:
        # a locked tenant, or a legacy subscription-lapse freeze provenance.
        if not tenant_allowed:
            return None
        if (
            character.frozen
            and character.frozen_reason == FREEZE_REASON_SUBSCRIPTION_LAPSE
        ):
            return None

        # Everything still listed: idle / manual / legacy-None freezes are a
        # dormancy flag, not an access removal.
        background_frozen = bool(character.frozen)
        avatar_key = self._avatar_object_key(character)
        display_name = character.name
        return RosterCharacterView(
            character_id=character.id,
            display_name=display_name,
            background_frozen=background_frozen,
            avatar_object_ref=avatar_key,
            presentation_version=_presentation_version(display_name, avatar_key),
        )

    def _avatar_object_key(self, character: Character) -> str | None:
        """Normalise the character's canonical portrait to a bare object key.

        ``image_urls[0]`` is the canonical portrait. It may be an app-relative
        media ref (``/v1/public/{key}`` / ``/uploads/{key}``); reverse it to
        the opaque key the Cloud side stores. No image, or a URL this storage
        adapter cannot reverse, yields ``None``."""
        if not character.image_urls:
            return None
        primary = character.image_urls[0]
        if not primary or not primary.strip():
            return None
        return self._object_storage.object_key_from_url(primary)


def _presentation_version(display_name: str, avatar_key: str | None) -> str:
    """Stable hash over the rendered identity: name + avatar key.

    Changes iff the display name or avatar key changes; every other
    character edit leaves it untouched so caches invalidate precisely."""
    payload = f"{display_name}\n{avatar_key or ''}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:_PRESENTATION_VERSION_LENGTH]
