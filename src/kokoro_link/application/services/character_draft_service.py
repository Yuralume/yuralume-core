"""Character draft application service.

Thin wrapper around the generator port — keeps the API layer free of
domain knowledge about image handling and response shaping.

It is also one of the four AP2 action-priced entry points: a draft is a single
player-visible action ("suggest me a character"), so under
``billing_shape=action_fixed`` it costs one fixed price regardless of how the
generator splits the work internally.

**Reference images are staged, not stored (VP4).** When a draft carries an
image, the bytes land in Object Storage under ``draft-uploads/`` *inside* the
billing gate but before the model call, so the generator can hand the provider
a fetchable public URL instead of an inline base64 data URL — hosted Cloud
routes through a gateway with a bounded serialized request budget, and an
inlined photo blows that envelope. Those objects are scratch space, not durable
media: the public URL only has to be reachable for the duration of the upstream
call, so the draft deletes it as soon as it finishes, success or failure (owner
ruling: 生完即丟, a draft reference image is never kept). Whatever a crash or a
failed delete leaves behind is swept by the storage service's TTL on that
prefix.

Staging sits *inside* ``action(...)`` and not before it: a request the wallet
refuses must not reach storage at all, or a player with no credits pays for
nothing while still costing one object write (and its delete) per retry. The
reverse hazard — a staging failure stranding a reservation — cannot happen,
because staging never raises: it is fail-soft end to end, and no storage, no
public base URL, or a storage that refuses the write all fall back to the
pre-VP4 behaviour (inline bytes). Losing the URL carrier degrades a hosted
draft; raising would lose the draft outright.
"""

from __future__ import annotations

import dataclasses
import logging
import mimetypes
from uuid import uuid4

from kokoro_link.application.dto.character_draft import CharacterDraftResponse
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.application.services.vision_media import (
    absolute_public_vision_url,
)
from kokoro_link.contracts.character_draft import (
    CharacterDraftGeneratorPort,
    ImageInput,
)
from kokoro_link.contracts.cloud_action_billing import ACTION_CHARACTER_DRAFT
from kokoro_link.contracts.object_storage import (
    EPHEMERAL_OBJECT_KEY_PREFIXES,
    ObjectStoragePort,
)
from kokoro_link.infrastructure.storage.keys import safe_key_segment

_LOGGER = logging.getLogger(__name__)

#: Key prefix for staged draft reference images. Derived from the port's
#: :data:`~kokoro_link.contracts.object_storage.EPHEMERAL_OBJECT_KEY_PREFIXES`
#: — the single source of truth shared with the storage service's TTL sweep and
#: the variant decorator's skip list — so this writer cannot drift out of step
#: with the components that clean up after it.
DRAFT_UPLOAD_PREFIX = EPHEMERAL_OBJECT_KEY_PREFIXES[0].rstrip("/")


def _safe_operator_dir(operator_id: str | None) -> str:
    """Restrict ``operator_id`` to a single key-safe path segment.

    Thin wrapper over the shared
    :func:`~kokoro_link.infrastructure.storage.keys.safe_key_segment` — the
    allow-list and its two traps (CJK that passes ``isalnum`` but fails the
    key validator; ids that sanitise down to nothing but dots) are documented
    there.

    The empty fallback is ``anonymous`` rather than ``unknown``: a draft is
    reachable before the operator has a profile, so "no id" is a normal state
    here, not a missing one.
    """
    return safe_key_segment(operator_id, fallback="anonymous")


class CharacterDraftService:
    def __init__(
        self,
        generator: CharacterDraftGeneratorPort,
        *,
        action_billing: (
            CloudActionBillingService | NullActionBillingService | None
        ) = None,
        object_storage: ObjectStoragePort | None = None,
        public_base_url: str = "",
    ) -> None:
        self._generator = generator
        self._action_billing = action_billing or NullActionBillingService()
        # Both are required to stage: bytes with no absolute base URL yield a
        # media ref no provider can fetch, which is the same as not staging.
        self._object_storage = object_storage
        self._public_base_url = public_base_url

    async def generate(
        self,
        *,
        prompt: str | None,
        image: ImageInput | None,
        operator_primary_language: str = "zh-TW",
        operator_id: str | None = None,
        quoted_price_cr: float | None = None,
    ) -> CharacterDraftResponse:
        """Draft one character, charged as a single ``character_draft`` action.

        R9: ``quoted_price_cr`` is the price the player's own client had on
        screen when they pressed the button. It binds the charge to what was
        displayed instead of to Core's process-local cache, which can hold a
        number this player never saw (a second replica, or a cache refreshed
        between the quote and the send). ``None`` — an older client, self-host,
        or nothing unambiguous to quote — falls back to the cache, which is the
        pre-R9 behaviour.

        A reference image is staged to Object Storage *inside* the billing gate
        and deleted in ``finally``, so the object's lifetime is exactly the
        part of this call the player actually paid for. Refused requests (empty
        wallet, moved quote) never reach storage: they are the ones a client
        retries, and one object write plus one delete per refusal is work
        nobody is paying for. Nothing is stranded by the ordering either —
        staging never raises, so it cannot fail *after* the reservation.
        """
        staged_key: str | None = None
        try:
            async with self._action_billing.action(
                ACTION_CHARACTER_DRAFT,
                operator_id=operator_id or "",
                quoted_price_cr=quoted_price_cr,
            ):
                staged_image, staged_key = await self._stage_image(
                    image, operator_id=operator_id,
                )
                draft = await self._generator.generate(
                    prompt=prompt,
                    image=staged_image,
                    operator_primary_language=operator_primary_language,
                    operator_id=operator_id,
                )
            return CharacterDraftResponse.from_domain(draft)
        finally:
            # Deletion only — a billing or generator failure keeps propagating.
            if staged_key is not None:
                await self._discard_staged_image(staged_key)

    async def _stage_image(
        self, image: ImageInput | None, *, operator_id: str | None,
    ) -> tuple[ImageInput | None, str | None]:
        """Persist the reference image and return it carrying a public URL.

        Returns ``(image_to_send, key_to_delete)``. ``key_to_delete`` is the
        cleanup obligation this call created, as far as it can be known: a
        write that landed but could not be promoted to an absolute URL still
        has to be deleted, so the key comes back on that path too.

        ``None`` means *nothing is known to have been written* — not that
        nothing was. A ``put_bytes`` that raises may have already stored the
        bytes (a backend that wrote and then failed to answer is
        indistinguishable here from one that never wrote), and there is no key
        this call can trust enough to delete. That residue is exactly what the
        storage service's TTL sweep of the ephemeral prefixes exists to catch.
        """
        if (
            image is None
            or self._object_storage is None
            or not self._public_base_url.strip()
        ):
            return image, None

        safe_dir = _safe_operator_dir(operator_id)
        suffix = mimetypes.guess_extension(image.mime_type or "") or ".png"
        object_key = (
            f"{DRAFT_UPLOAD_PREFIX}/{safe_dir}/{uuid4().hex}{suffix}"
        )
        try:
            stored = await self._object_storage.put_bytes(
                object_key=object_key,
                content=image.data,
                content_type=image.mime_type or "application/octet-stream",
                metadata={"user_id": safe_dir, "kind": "character-draft"},
            )
        except Exception:  # noqa: BLE001 — staging is an optimisation
            _LOGGER.warning(
                "could not stage the character-draft reference image to object "
                "storage (key=%s) — continuing with inline image bytes; on "
                "hosted this may exceed the gateway request budget and "
                "downgrade the draft to text-only",
                object_key, exc_info=True,
            )
            return image, None

        public_url = absolute_public_vision_url(
            stored.url, public_base_url=self._public_base_url,
        )
        if public_url is None:
            _LOGGER.warning(
                "staged character-draft image %s has no provider-fetchable "
                "public URL (stored ref=%r, public base=%r) — continuing with "
                "inline image bytes",
                stored.object_key, stored.url, self._public_base_url,
            )
            return image, stored.object_key or object_key

        return (
            dataclasses.replace(image, public_url=public_url),
            stored.object_key or object_key,
        )

    async def _discard_staged_image(self, object_key: str) -> None:
        """Best-effort delete of the staged reference image.

        Never raises: the draft is already produced (or already failing for a
        reason the caller cares about more), and the storage service TTL-sweeps
        the ``draft-uploads/`` prefix, so a leaked object is bounded.
        """
        storage = self._object_storage
        if storage is None:  # pragma: no cover — a key implies a storage
            return
        try:
            await storage.delete(object_key=object_key)
        except Exception:  # noqa: BLE001 — cleanup must not shape the response
            _LOGGER.warning(
                "could not delete the staged character-draft image %s — the "
                "storage service TTL sweep is the backstop",
                object_key, exc_info=True,
            )
