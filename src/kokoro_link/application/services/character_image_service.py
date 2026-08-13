"""Character image upload / delete service.

Route handlers pass bytes + mime in; the service writes to
``ObjectStoragePort`` and returns a stable public URL. The character
entity tracks the ordered list of URLs; this service keeps DB + object
storage in sync on mutations.

Why a service layer at all: the route would otherwise juggle
storage/file metadata/character-repo at once, and the delete path has two
fail modes we want to handle deliberately (URL not on the character's
list → 404; object missing → warn but still update DB, since
the DB is the source of truth the UI trusts).
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_IMAGE_PORTRAIT,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.application.services.image_usage import image_usage_parts_from_provider
from kokoro_link.application.services.official_character_art import (
    is_official_art_key,
    official_art_object_key,
)
from kokoro_link.application.services.official_reference_attachments import (
    official_reference_attachments,
)
from kokoro_link.contracts.active_image import ActiveImageProviderPort
from kokoro_link.contracts.cloud_action_billing import ACTION_IMAGE_PORTRAIT
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.generation_usage import (
    UsageEventDraft,
    UsageEventRecorderPort,
)
from kokoro_link.contracts.interaction_context import (
    confirm_deferred_deliveries,
)
from kokoro_link.contracts.object_storage import (
    ObjectStoragePort,
    ObjectStorageUnavailableError,
)
from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageTimeoutError,
)
from kokoro_link.contracts.character_image_candidate_batch import (
    CharacterImageCandidateBatchRepositoryPort,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.infrastructure.repositories.in_memory_character_image_candidate_batches import (  # noqa: E501
    InMemoryCharacterImageCandidateBatchRepository,
)
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_IMAGE,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.application.services.visual_generation_style import (
    VisualGenerationStyleService,
)

_LOGGER = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB — same as character draft upload
MAX_IMAGES_PER_CHARACTER = 12
MAX_CANDIDATES_PER_BATCH = 4
"""Upper bound on gacha batch size — keeps GPU memory (1024x1024 SDXL
latents × N) + wait time reasonable. Four is a sweet spot visually:
enough variety to pick from, not enough to decision-fatigue."""

_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_ALLOWED_MIME_PREFIX = "image/"
_CANDIDATES_SUBDIR = "candidates"


@dataclass(frozen=True, slots=True)
class OfficialCardArtImage:
    """One stage image an official-card install is landing (EC4).

    ``index`` is the card's own carousel position, which becomes part of
    the object key so a re-install overwrites rather than accumulates.
    ``reference`` marks the images Cloud designated as the likeness lock —
    the flag is carried in the key too, because ``image_urls`` is a list of
    bare strings with nowhere else to put it.
    """

    index: int
    data: bytes
    content_type: str
    reference: bool = False


@dataclass(frozen=True, slots=True)
class CommittedAlbumCandidate:
    """A candidate the operator sent straight to the album.

    Returned from ``commit_candidates`` so the route layer can register
    each file as an ``AlbumItem`` without re-stat'ing. The file is
    already at its permanent URL by the time this reaches the caller.
    """

    url: str
    byte_size: int | None


class CharacterImageError(Exception):
    """Base for image-service errors that the route layer converts to HTTP."""


class ImageTooLargeError(CharacterImageError):
    pass


class UnsupportedImageTypeError(CharacterImageError):
    pass


class TooManyImagesError(CharacterImageError):
    pass


class ImageNotFoundError(CharacterImageError):
    pass


class OfficialArtProtectedError(CharacterImageError):
    """Refusal to delete art that came with an IP-partner card (EC4).

    Distinct from :class:`ImageNotFoundError` because the image very much
    exists — the answer is "not yours", not "not there" — and the route
    layer owes the caller a different status for it (409, the same one an
    image cap already answers with) than a 404 that would suggest retrying.
    """


class GenerationDisabledError(CharacterImageError):
    """Raised when the portrait generator is not configured."""


class GenerationFailedError(CharacterImageError):
    """ComfyUI / generator-level failure the route should forward verbatim."""


class StorageUnavailableError(CharacterImageError):
    """Object storage backend unreachable — an ops problem, not a bad request.

    Reclassified from the storage adapter's
    :class:`ObjectStorageUnavailableError` so the route layer can answer
    503 (service unavailable) instead of the misleading 400 the generic
    ``CharacterImageError`` mapping would produce.
    """


class CharacterImageService:
    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        uploads_dir: Path,
        url_prefix: str = "/uploads",
        image_provider: ActiveImageProviderPort | None = None,
        object_storage: ObjectStoragePort | None = None,
        visual_style_service: VisualGenerationStyleService | None = None,
        usage_recorder: UsageEventRecorderPort | None = None,
        account_runtime_profile_resolver: (
            AccountRuntimeProfileResolverPort | None
        ) = None,
        subscription_access_guard: SubscriptionAccessGuard | None = None,
        candidate_batch_repository: (
            CharacterImageCandidateBatchRepositoryPort | None
        ) = None,
        action_billing: (
            CloudActionBillingService | NullActionBillingService | None
        ) = None,
    ) -> None:
        self._character_repository = character_repository
        _ = uploads_dir, url_prefix
        self._image_provider = image_provider
        self._object_storage = object_storage
        self._visual_style_service = visual_style_service
        self._usage_recorder = usage_recorder
        self._account_runtime_profile_resolver = (
            account_runtime_profile_resolver
            or PermissiveAccountRuntimeProfileResolver()
        )
        self._subscription_access_guard = subscription_access_guard
        # Shared, not per-process: generate and commit are two requests and
        # hosted load-balances several api replicas, so a process-local map
        # would drop the player's picks (and leak the objects) whenever the
        # two landed on different replicas. The in-memory fallback is only
        # for single-process dev / unit tests.
        self._candidate_batches: CharacterImageCandidateBatchRepositoryPort = (
            candidate_batch_repository
            or InMemoryCharacterImageCandidateBatchRepository()
        )
        # AP2: ``image_portrait`` is an action-priced entry. The null object
        # keeps self-host and token-billed tiers on the original path.
        self._action_billing: (
            CloudActionBillingService | NullActionBillingService
        ) = action_billing or NullActionBillingService()

    def set_usage_recorder(self, recorder: UsageEventRecorderPort | None) -> None:
        self._usage_recorder = recorder

    async def add_image(
        self,
        character_id: str,
        *,
        data: bytes,
        mime_type: str | None,
        original_filename: str | None = None,
    ) -> Character:
        character = await self._load_character(character_id)
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageTooLargeError(
                f"Image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit",
            )
        if len(character.image_urls) >= MAX_IMAGES_PER_CHARACTER:
            raise TooManyImagesError(
                f"Character already has {MAX_IMAGES_PER_CHARACTER} images",
            )
        extension = self._pick_extension(mime_type, original_filename)
        if extension is None:
            raise UnsupportedImageTypeError(
                "Unsupported image type; use PNG, JPG, GIF or WEBP",
            )

        filename = f"{uuid4().hex}{extension}"
        object_key = f"characters/{character_id}/{filename}"
        if self._object_storage is None:
            raise CharacterImageError("Object storage is not configured")
        try:
            stored = await self._object_storage.put_bytes(
                object_key=object_key,
                content=data,
                content_type=mime_type or "application/octet-stream",
                metadata={"character_id": character_id, "kind": "stage"},
            )
        except ObjectStorageUnavailableError as exc:
            _LOGGER.exception(
                "add_image: object storage unreachable (character=%s)",
                character_id,
            )
            raise StorageUnavailableError(str(exc)) from exc
        url = stored.url
        new_urls = tuple([*character.image_urls, url])
        updated = character.with_image_urls(new_urls)
        await self._character_repository.save(updated)
        return updated

    async def attach_official_card_art(
        self,
        character_id: str,
        *,
        images: Sequence[OfficialCardArtImage],
    ) -> Character:
        """Land an IP-partner card's stage images on a fresh character (EC4).

        Deliberately **not** :meth:`add_image` in a loop. That path mints a
        random key per upload, which is right for a player's own art and
        wrong here twice over: the key is where "this is the partner's" and
        "this one is the likeness lock" are recorded, and a deterministic
        key makes a repeated install idempotent instead of leaving orphans
        behind. What it shares with the upload path is everything about
        limits and fail-softness — same per-image size cap, same carousel
        ceiling, and one unusable image skipped rather than the whole set
        refused, because a character short one portrait is recoverable and a
        half-created one is not.

        Appends in the order given, which is the card's carousel order.
        """
        character = await self._load_character(character_id)
        if self._object_storage is None:
            raise CharacterImageError("Object storage is not configured")
        urls = list(character.image_urls)
        for image in images:
            if len(urls) >= MAX_IMAGES_PER_CHARACTER:
                _LOGGER.warning(
                    "official card art: character %s is at the %d image cap "
                    "— dropping the rest of the card's art",
                    character_id, MAX_IMAGES_PER_CHARACTER,
                )
                break
            if len(image.data) > MAX_IMAGE_BYTES:
                _LOGGER.warning(
                    "official card art %s/%s exceeds the %d MB limit — "
                    "skipping", character_id, image.index,
                    MAX_IMAGE_BYTES // (1024 * 1024),
                )
                continue
            extension = self._pick_extension(image.content_type, None)
            if extension is None:
                _LOGGER.warning(
                    "official card art %s/%s has an unsupported type %r — "
                    "skipping", character_id, image.index, image.content_type,
                )
                continue
            object_key = official_art_object_key(
                character_id,
                index=image.index,
                extension=extension,
                reference=image.reference,
            )
            try:
                stored = await self._object_storage.put_bytes(
                    object_key=object_key,
                    content=image.data,
                    content_type=image.content_type,
                    metadata={
                        "character_id": character_id,
                        "kind": "official-card-art",
                    },
                )
            except ObjectStorageUnavailableError as exc:
                # Unlike a per-image skip, storage being down means none of
                # the rest will land either — stop and let the caller report
                # a character installed without its art.
                _LOGGER.exception(
                    "official card art: object storage unreachable "
                    "(character=%s)", character_id,
                )
                raise StorageUnavailableError(str(exc)) from exc
            urls.append(stored.url)
        if list(character.image_urls) == urls:
            return character
        updated = character.with_image_urls(tuple(urls))
        await self._character_repository.save(updated)
        return updated

    async def remove_image(
        self,
        character_id: str,
        *,
        url: str,
    ) -> Character:
        character = await self._load_character(character_id)
        if url not in character.image_urls:
            raise ImageNotFoundError("Image URL not associated with this character")
        if self._is_official_art(url):
            # EC4: a managed character's official art is the partner's
            # identity, landed by the install, and the one thing the player
            # is looking at that is not theirs. The edit panel hides the
            # delete control for it; this is the half that a direct API call
            # walks into. Anything the player added themselves — a generated
            # portrait, a gacha pick — still deletes normally.
            raise OfficialArtProtectedError(
                "Official card art cannot be removed from a managed character",
            )

        new_urls = tuple(u for u in character.image_urls if u != url)
        updated = character.with_image_urls(new_urls)
        await self._character_repository.save(updated)

        # Object deletion is best-effort — the DB state the UI trusts has
        # already dropped the URL, so a stale file on disk is only
        # wasted space, not a correctness problem.
        if self._object_storage is None:
            return updated
        try:
            object_key = self._object_storage.object_key_from_url(url)
            if object_key is not None:
                await self._object_storage.delete(object_key=object_key)
        except Exception:
            _LOGGER.exception(
                "Failed to delete image object %s", url,
            )
        return updated

    async def generate_portrait(
        self,
        character_id: str,
        *,
        positive: str,
        aspect: str = "portrait",
        is_primary_init: bool = False,
        quoted_price_cr: float | None = None,
    ) -> Character:
        """Generate a portrait via the wired image provider and append it
        to ``image_urls``.

        Same permanent home as a manual upload (``uploads/characters/
        {id}/<file>.png``) so the stage rotation picks it up. Honours
        the per-character image cap so runaway generation can't
        swallow disk. Raises ``GenerationDisabledError`` when no image
        provider is wired for this deployment.

        ``is_primary_init`` marks the one-time portrait generated when a
        character is created. It is bounded by the account's
        character-creation limits (``max_characters`` /
        ``daily_character_create_limit``), so it is exempt from the
        ``album_generation_enabled`` gate — that gate exists to close the
        *repeatable* manual album path (which a demo could otherwise spam),
        not the bounded first portrait that is an important onboarding moment.
        """
        if self._image_provider is None:
            raise GenerationDisabledError(
                "Portrait generation disabled — configure an image "
                "profile (KOKORO_IMAGE_PROFILES).",
            )
        character = await self._load_character(character_id)
        await self._ensure_subscription_access(character)
        # AP2: charged before any generation happens, so an out-of-credits
        # player is refused up-front rather than after the GPU work. Every
        # failure below leaves the ``async with`` by exception, which releases.
        #
        # R9: ``quoted_price_cr`` is the number this player's own screen showed
        # (the SPA sends it with the request). It binds the charge to what was
        # displayed rather than to Core's process-local cache, which under
        # several replicas — or straight after a back-office edit — can hold a
        # price this player never saw. ``None`` (an older client, or the
        # one-time primary-portrait path, which has no screen to quote from)
        # falls back to the cache exactly as before.
        #
        # Known residual: the ``is_primary_init=True`` call from
        # ``CharacterPrimaryImageInitializer`` runs inline on the character-
        # create request, i.e. under a ``user_action`` trigger, so it *is*
        # charged — at a price the create modal never displayed. Whether that
        # first portrait is bundled into character creation or priced on its
        # own is an owner pricing call, not something to decide here.
        async with self._action_billing.action(
            ACTION_IMAGE_PORTRAIT,
            operator_id=getattr(character, "user_id", "") or "",
            quoted_price_cr=quoted_price_cr,
            character_origin=character.origin_official_card_id,
        ):
            return await self._generate_portrait(
                character,
                positive=positive,
                aspect=aspect,
                is_primary_init=is_primary_init,
            )

    async def _generate_portrait(
        self,
        character: Character,
        *,
        positive: str,
        aspect: str,
        is_primary_init: bool,
    ) -> Character:
        """Portrait generation proper, with the character already loaded and
        its access + action charge already settled by :meth:`generate_portrait`."""
        character_id = character.id
        if not is_primary_init:
            await self._ensure_image_generation_enabled(character)
        if len(character.image_urls) >= MAX_IMAGES_PER_CHARACTER:
            raise TooManyImagesError(
                f"Character already has {MAX_IMAGES_PER_CHARACTER} images",
            )
        provider = await self._image_provider.resolve(
            FEATURE_IMAGE_PORTRAIT, character=character,
        )
        profile_id = await self._image_provider.resolve_profile_id(
            FEATURE_IMAGE_PORTRAIT, character=character,
        )
        if provider is None:
            raise GenerationDisabledError(
                "No image profile is currently active for portrait "
                "generation — check KOKORO_IMAGE_PROFILES / preferences.",
            )
        started_at = datetime.now(timezone.utc)
        try:
            # Settings-page generation: explicit scene from the operator.
            # Don't let stale chat-induced runtime state (emotion /
            # current_intent) bleed into the rewriter — the operator
            # typed what they want, a "sleepy" mood from the last
            # conversation turn shouldn't override that.
            styled_positive = await self._styled_prompt(
                positive, character=character,
            )
            images = await provider.generate(
                character=character, positive=styled_positive, aspect=aspect,
                use_runtime_state=False,
                # EC6: a managed character is always drawn from the partner's
                # reference art; a player's own character passes ``()``.
                user_attachment_urls=official_reference_attachments(
                    character, object_storage=self._object_storage,
                ),
            )
        except ImageTimeoutError as exc:
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_portrait",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=1,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            raise GenerationFailedError(str(exc)) from exc
        except ImageNoOutputError as exc:
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_portrait",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=1,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            raise GenerationFailedError(str(exc)) from exc
        except ImageGenerationError as exc:
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_portrait",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=1,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            raise GenerationFailedError(str(exc)) from exc

        updated = character
        stored_count = 0
        try:
            for data in images:
                if len(updated.image_urls) >= MAX_IMAGES_PER_CHARACTER:
                    break
                updated = await self.add_image(
                    character_id,
                    data=data,
                    mime_type="image/png",
                    original_filename="generated.png",
                )
                stored_count += 1
        except StorageUnavailableError as exc:
            # Already logged (and reclassified) by add_image — keep the
            # type so the route can answer 503 instead of 400.
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_portrait",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=1,
                returned=len(images),
                artifact_count=stored_count,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
                billable_quantity=len(images),
            )
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "generate_portrait: storing generated image failed "
                "(character=%s)",
                character_id,
            )
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_portrait",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=1,
                returned=len(images),
                artifact_count=stored_count,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
                billable_quantity=len(images),
            )
            raise CharacterImageError(str(exc)) from exc
        if stored_count:
            # The portrait now exists where the player can see it, so the
            # covered Gateway call has finally delivered. Confirming here (and
            # not in the provider, which only ever held bytes in memory) is
            # what keeps a storage failure refundable.
            confirm_deferred_deliveries()
        await self._record_image_usage_safely(
            character=character,
            feature_key="character_portrait",
            provider=provider,
            profile_id=profile_id or "",
            aspect=aspect,
            requested=1,
            returned=len(images),
            artifact_count=stored_count,
            status=STATUS_SUCCEEDED,
            started_at=started_at,
        )
        return updated

    async def generate_candidates(
        self,
        character_id: str,
        *,
        positive: str,
        aspect: str = "portrait",
        count: int = MAX_CANDIDATES_PER_BATCH,
        quoted_price_cr: float | None = None,
    ) -> tuple[Character, list[str]]:
        """Gacha flow: render N candidate images but don't commit yet.

        Objects land under ``characters/{id}/candidates/{batch_id}/``
        and are returned as URLs. The character's ``image_urls`` stays
        untouched — the operator decides which candidates to keep via
        ``commit_candidates``. The ``(MAX_IMAGES_PER_CHARACTER -
        current_count)`` headroom is checked *before* generating so we
        don't waste a pass producing images we can't store.

        Returns ``(character, candidate_urls)`` — the character comes
        back unchanged but lets the route echo a consistent shape.

        **This is where ``image_portrait`` is actually charged.** The player's
        picture button (``CharacterImagesPanel``) posts one batch per press, so
        one batch is one action: the charge wraps the whole batch, its
        interaction scope covers every upstream call the batch makes (the
        prompt styler and the single N-image render alike), and it settles or
        refunds once for the press rather than once per returned candidate.
        Charging per image instead would bill a number the price hint never
        showed. ``commit_candidates`` is free by construction — picking which
        of the pictures you already paid for to keep is not a second action.

        The charge is raised after **every** cheap refusal — no album access,
        no headroom, and no usable image provider — precisely so those never
        take the player's money and hand it straight back. Resolving the
        provider is part of that list rather than an implementation detail of
        the batch: "no image profile is currently active" is a configuration
        answer, reachable on every press, and raising a reservation the very
        next line refunds turns a stable misconfiguration into a stream of
        charge/refund pairs across the player's ledger for pictures that were
        never even attempted.
        """
        if self._image_provider is None:
            raise GenerationDisabledError(
                "Portrait generation disabled — configure an image "
                "profile (KOKORO_IMAGE_PROFILES).",
            )
        clamped_count = max(1, min(count, MAX_CANDIDATES_PER_BATCH))
        character = await self._load_character(character_id)
        await self._ensure_subscription_access(character)
        await self._ensure_image_generation_enabled(character)
        headroom = MAX_IMAGES_PER_CHARACTER - len(character.image_urls)
        if headroom <= 0:
            raise TooManyImagesError(
                f"Character already has {MAX_IMAGES_PER_CHARACTER} images — "
                "remove some before generating more.",
            )
        provider = await self._image_provider.resolve(
            FEATURE_IMAGE_PORTRAIT, character=character,
        )
        if provider is None:
            raise GenerationDisabledError(
                "No image profile is currently active for portrait "
                "generation — check KOKORO_IMAGE_PROFILES / preferences.",
            )
        profile_id = await self._image_provider.resolve_profile_id(
            FEATURE_IMAGE_PORTRAIT, character=character,
        )
        # R9: bound to the number this player's own screen quoted, exactly as
        # the single-portrait path does; ``None`` falls back to Core's cache.
        async with self._action_billing.action(
            ACTION_IMAGE_PORTRAIT,
            operator_id=getattr(character, "user_id", "") or "",
            quoted_price_cr=quoted_price_cr,
            character_origin=character.origin_official_card_id,
        ):
            return await self._generate_candidates(
                character, positive=positive, aspect=aspect,
                clamped_count=clamped_count,
                provider=provider, profile_id=profile_id,
            )

    async def _generate_candidates(
        self,
        character: Character,
        *,
        positive: str,
        aspect: str,
        clamped_count: int,
        provider,
        profile_id: str | None,
    ) -> tuple[Character, list[str]]:
        """Batch generation proper — character loaded, provider resolved, and
        access + charge held by :meth:`generate_candidates`. Every exception
        leaves that ``async with`` by raising, which refunds."""
        character_id = character.id
        # Still render the full requested batch even if headroom is
        # smaller; commit handles the cap. This lets the operator
        # pick their favourite N out of a larger pool.

        started_at = datetime.now(timezone.utc)
        try:
            # Same reasoning as generate_portrait: operator's explicit
            # scene shouldn't be polluted by the chat path's runtime
            # mood / current_intent.
            styled_positive = await self._styled_prompt(
                positive, character=character,
            )
            images = await provider.generate(
                character=character,
                positive=styled_positive,
                aspect=aspect,
                batch=clamped_count,
                use_runtime_state=False,
                # EC6: every candidate in the batch is anchored to the same
                # partner reference art, so the gacha picker is a choice of
                # scene rather than a choice of which face the card has.
                user_attachment_urls=official_reference_attachments(
                    character, object_storage=self._object_storage,
                ),
            )
        except ImageTimeoutError as exc:
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_album_candidate",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=clamped_count,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            raise GenerationFailedError(str(exc)) from exc
        except ImageNoOutputError as exc:
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_album_candidate",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=clamped_count,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            raise GenerationFailedError(str(exc)) from exc
        except ImageGenerationError as exc:
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_album_candidate",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=clamped_count,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            raise GenerationFailedError(str(exc)) from exc

        urls: list[str] = []
        if self._object_storage is None:
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_album_candidate",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=clamped_count,
                returned=len(images),
                artifact_count=0,
                status=STATUS_FAILED,
                error_code="CharacterImageError",
                error_message="Object storage is not configured",
                started_at=started_at,
                billable_quantity=len(images),
            )
            raise CharacterImageError("Object storage is not configured")
        # Read-then-clear before touching storage: the superseded batch must
        # stop being commit-able the moment we start deleting its objects,
        # otherwise a commit racing this generate could copy keys we are in
        # the middle of removing. The clear is conditional on the batch we
        # just read still being the live one — if another replica already
        # superseded (or committed) it, those objects belong to whoever did,
        # and deleting them here could yank the source out of its in-flight
        # copy. Our own batch is written unconditionally at the end.
        previous_keys = await self._candidate_batches.get(character_id)
        if previous_keys:
            if await self._candidate_batches.clear(
                character_id, expected_keys=previous_keys,
            ):
                await self._delete_candidate_objects(previous_keys)
            else:
                _LOGGER.info(
                    "generate_candidates: previous batch already superseded "
                    "(character=%s, %d stale candidates left to its owner)",
                    character_id,
                    len(previous_keys),
                )
        batch_id = uuid4().hex
        object_keys: list[str] = []
        try:
            for data in images:
                object_key = (
                    f"characters/{character_id}/{_CANDIDATES_SUBDIR}/"
                    f"{batch_id}/{uuid4().hex}.png"
                )
                stored = await self._object_storage.put_bytes(
                    object_key=object_key,
                    content=data,
                    content_type="image/png",
                    metadata={
                        "character_id": character_id,
                        "kind": "candidate",
                        "batch_id": batch_id,
                    },
                )
                urls.append(stored.url)
                object_keys.append(stored.object_key)
        except ObjectStorageUnavailableError as exc:
            _LOGGER.exception(
                "generate_candidates: object storage unreachable "
                "(character=%s)",
                character_id,
            )
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_album_candidate",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=clamped_count,
                returned=len(images),
                artifact_count=len(urls),
                status=STATUS_FAILED,
                # Normalized to the service-level type so the ledger shows
                # one code for "storage down" regardless of which entry
                # point hit it (generate_portrait records the same).
                error_code=StorageUnavailableError.__name__,
                error_message=str(exc),
                started_at=started_at,
                billable_quantity=len(images),
            )
            raise StorageUnavailableError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "generate_candidates: storing candidate image failed "
                "(character=%s)",
                character_id,
            )
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_album_candidate",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=clamped_count,
                returned=len(images),
                artifact_count=len(urls),
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
                billable_quantity=len(images),
            )
            raise CharacterImageError(str(exc)) from exc
        # The batch registration is the last thing that has to land before the
        # candidates are *reachable*: object storage has no list-by-prefix, so
        # a batch nobody recorded is a set of orphan keys the picker can never
        # commit from — the player paid and got a dead gallery. It therefore
        # sits ahead of the delivery confirmation, not after it: an unconfirmed
        # covered call is a refundable one, and a confirmation issued a line
        # too early would charge for pictures the next request cannot find.
        try:
            registered = await self._candidate_batches.replace(
                character_id, object_keys,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "generate_candidates: recording the candidate batch failed "
                "(character=%s)",
                character_id,
            )
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_album_candidate",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=clamped_count,
                returned=len(images),
                artifact_count=len(urls),
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
                billable_quantity=len(images),
            )
            raise CharacterImageError(str(exc)) from exc
        if urls and not registered:
            # A conditional write is the only way to get here (an unconditional
            # replace always applies), but "the keys are not the live batch"
            # has the same consequence as a failed write, so it gets the same
            # refundable answer rather than a silent success.
            _LOGGER.warning(
                "generate_candidates: candidate batch was superseded before "
                "it could be recorded (character=%s)", character_id,
            )
            await self._record_image_usage_safely(
                character=character,
                feature_key="character_album_candidate",
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                requested=clamped_count,
                returned=len(images),
                artifact_count=len(urls),
                status=STATUS_FAILED,
                error_code=CharacterImageError.__name__,
                error_message="candidate batch superseded before it landed",
                started_at=started_at,
                billable_quantity=len(images),
            )
            raise CharacterImageError(
                "Candidate batch could not be recorded — please try again.",
            )
        if urls:
            # The candidates now exist at URLs the player's picker can load
            # *and* the batch that makes them commit-able is stored, so the
            # covered Gateway call has finally delivered. Same rule as the
            # single-portrait path: confirming here rather than in the provider
            # (which only ever held bytes) is what keeps a storage failure
            # refundable instead of charging for pictures nobody can see.
            confirm_deferred_deliveries()
        await self._record_image_usage_safely(
            character=character,
            feature_key="character_album_candidate",
            provider=provider,
            profile_id=profile_id or "",
            aspect=aspect,
            requested=clamped_count,
            returned=len(images),
            artifact_count=len(urls),
            status=STATUS_SUCCEEDED,
            started_at=started_at,
        )
        return character, urls

    async def commit_candidates(
        self,
        character_id: str,
        *,
        keep_urls: list[str],
        album_urls: list[str] | None = None,
    ) -> tuple[Character, list["CommittedAlbumCandidate"]]:
        """Commit selected candidates to their target destinations; drop the rest.

        Two destination buckets:
        - ``keep_urls`` — move into ``character.image_urls`` (stage carousel).
        - ``album_urls`` — move into the main directory but DO NOT append
          to ``image_urls``. The route layer pairs each entry with an
          ``AlbumService.add_from_candidate`` call to register the row.

        Any file in the candidates directory not named in either list is
        deleted. "Discard all" is ``keep_urls=[], album_urls=[]``.

        The stage cap (``MAX_IMAGES_PER_CHARACTER``) only applies to
        ``keep_urls``; album has its own higher ceiling enforced in
        ``AlbumService``, so overflow stage picks simply get treated as
        album picks when both lists reference the same file (``keep_urls``
        takes precedence; collisions are impossible since each filename
        appears at most once on disk).

        Returns ``(updated_character, album_entries)`` — ``album_entries``
        carries the post-move URL + byte_size for each album-bound
        candidate so the route doesn't need to re-stat the file.
        """
        character = await self._load_character(character_id)
        if self._object_storage is None:
            raise CharacterImageError("Object storage is not configured")
        return await self._commit_storage_candidates(
            character,
            keep_urls=keep_urls,
            album_urls=album_urls or [],
        )

    async def _commit_storage_candidates(
        self,
        character: Character,
        *,
        keep_urls: list[str],
        album_urls: list[str],
    ) -> tuple[Character, list[CommittedAlbumCandidate]]:
        assert self._object_storage is not None
        # Read first, delete last: the batch row is the only record of which
        # objects are in flight, so it must outlive every failure mode that
        # leaves work undone (see the handlers at the end of the loop).
        #
        # ``candidate_keys`` is also this request's claim on the row: every
        # write below passes it as ``expected_keys``, so a generate that lands
        # on another replica while we work cannot have its brand-new batch
        # deleted (success / non-retryable paths) or overwritten with our stale
        # leftovers (outage path).
        candidate_keys = await self._candidate_batches.get(character.id)
        if not candidate_keys:
            return character, []

        known = set(candidate_keys)
        keep_keys = self._candidate_keys_from_urls(
            character.id, keep_urls, known,
        )
        album_keys = self._candidate_keys_from_urls(
            character.id, album_urls, known,
        )
        album_keys -= keep_keys

        headroom = MAX_IMAGES_PER_CHARACTER - len(character.image_urls)
        moved_urls: list[str] = []
        album_entries: list[CommittedAlbumCandidate] = []
        processed_keys: set[str] = set()

        try:
            for source_key in candidate_keys:
                wants_stage = source_key in keep_keys and headroom > 0
                wants_album = source_key in album_keys
                if wants_stage or wants_album:
                    destination_key = self._candidate_destination_key(
                        character.id,
                        source_key,
                    )
                    stored = await self._object_storage.copy(
                        source_key=source_key,
                        destination_key=destination_key,
                        metadata={
                            "character_id": character.id,
                            "kind": "stage" if wants_stage else "album",
                            "source": "candidate",
                        },
                    )
                    await self._object_storage.delete(object_key=source_key)
                    if wants_stage:
                        moved_urls.append(stored.url)
                        headroom -= 1
                    else:
                        album_entries.append(
                            CommittedAlbumCandidate(
                                url=stored.url,
                                byte_size=stored.size_bytes,
                            ),
                        )
                else:
                    await self._object_storage.delete(object_key=source_key)
                processed_keys.add(source_key)
        except ObjectStorageUnavailableError as exc:
            # The 503 this becomes tells the player to retry — so the
            # not-yet-processed candidates must survive for that retry,
            # and in the shared store rather than in this process, because
            # the retry can be served by a different replica.
            remaining = tuple(
                key for key in candidate_keys if key not in processed_keys
            )
            shrunk = await self._candidate_batches.replace(
                character.id, remaining, expected_keys=candidate_keys,
            )
            self._log_superseded_batch(
                character.id, applied=shrunk, orphan_count=len(remaining),
            )
            _LOGGER.exception(
                "commit_candidates: object storage unreachable "
                "(character=%s, %d/%d candidates uncommitted)",
                character.id,
                len(remaining),
                len(candidate_keys),
            )
            raise StorageUnavailableError(str(exc)) from exc
        except Exception:
            # Any other failure is not retryable at the object level (a
            # missing source key stays missing), so drop the batch instead
            # of leaving a row that would make every retry fail the same
            # way. Unprocessed objects become best-effort orphans.
            dropped = await self._candidate_batches.clear(
                character.id, expected_keys=candidate_keys,
            )
            self._log_superseded_batch(
                character.id,
                applied=dropped,
                orphan_count=len(candidate_keys) - len(processed_keys),
            )
            raise

        consumed = await self._candidate_batches.clear(
            character.id, expected_keys=candidate_keys,
        )
        self._log_superseded_batch(
            character.id, applied=consumed, orphan_count=0,
        )
        if not moved_urls:
            return character, album_entries

        updated = character.with_image_urls(
            tuple([*character.image_urls, *moved_urls]),
        )
        await self._character_repository.save(updated)
        return updated, album_entries

    async def reorder_images(
        self,
        character_id: str,
        *,
        url_order: list[str],
    ) -> Character:
        """Persist a new ordering. ``url_order`` must be the exact existing
        set of URLs (no adds / removes) — this prevents a client bug from
        silently nuking an image.
        """
        character = await self._load_character(character_id)
        current = set(character.image_urls)
        requested = set(url_order)
        if current != requested:
            raise ImageNotFoundError(
                "Reorder list must match existing image URLs exactly",
            )
        updated = character.with_image_urls(tuple(url_order))
        await self._character_repository.save(updated)
        return updated

    def _is_official_art(self, url: str) -> bool:
        """Whether ``url`` points at art an official card install landed.

        Answered from the object key rather than from a flag on the
        character, because the fact is per-image and ``image_urls`` is a
        list of bare strings: the install writes official art under its own
        key prefix precisely so this question has an answer later (see
        :mod:`official_character_art`).

        A deployment with no object storage wired cannot have installed a
        cloud-exclusive card in the first place, so ``False`` there is not a
        gap — it is the absence of the thing being guarded.
        """
        if self._object_storage is None:
            return False
        try:
            object_key = self._object_storage.object_key_from_url(url)
        except Exception:  # noqa: BLE001 — adapters differ on malformed urls
            return False
        return is_official_art_key(object_key)

    async def _load_character(self, character_id: str) -> Character:
        character = await self._character_repository.get(character_id)
        if character is None:
            raise ImageNotFoundError("Character not found")
        return character

    async def _ensure_subscription_access(self, character: Character) -> None:
        if self._subscription_access_guard is not None:
            await self._subscription_access_guard.ensure_character_allowed(character)

    async def _ensure_image_generation_enabled(self, character: Character) -> None:
        try:
            profile = await self._account_runtime_profile_resolver.resolve_for_operator(
                character.user_id,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "image generation runtime profile lookup failed character=%s",
                character.id,
            )
            raise GenerationDisabledError(
                "Image generation is disabled because the account runtime "
                "profile is unavailable.",
            ) from exc
        if not profile.album_generation_enabled:
            raise GenerationDisabledError(
                "Image generation is disabled for this account runtime profile.",
            )

    async def _styled_prompt(
        self,
        positive: str,
        *,
        character: Character,
    ) -> str:
        if self._visual_style_service is None:
            return positive
        return await self._visual_style_service.styled_prompt(
            positive, character=character,
        )

    def _pick_extension(
        self, mime_type: str | None, original_filename: str | None,
    ) -> str | None:
        if mime_type and mime_type.lower().startswith(_ALLOWED_MIME_PREFIX):
            guess = mimetypes.guess_extension(mime_type.lower())
            if guess and guess.lower() in _ALLOWED_EXTENSIONS:
                # guess_extension returns ``.jpe`` for image/jpeg on
                # some stdlib versions; normalise to the common form.
                return ".jpg" if guess.lower() == ".jpe" else guess.lower()
        if original_filename:
            ext = Path(original_filename).suffix.lower()
            if ext in _ALLOWED_EXTENSIONS:
                return ".jpg" if ext == ".jpeg" else ext
        return None

    def _candidate_keys_from_urls(
        self,
        character_id: str,
        urls: list[str],
        known_keys: set[str],
    ) -> set[str]:
        prefix = f"characters/{character_id}/{_CANDIDATES_SUBDIR}/"
        keys: set[str] = set()
        for url in urls:
            object_key = self._object_storage.object_key_from_url(url)
            if (
                object_key is not None
                and object_key in known_keys
                and object_key.startswith(prefix)
            ):
                keys.add(object_key)
        return keys

    def _candidate_destination_key(
        self,
        character_id: str,
        source_key: str,
    ) -> str:
        filename = source_key.rsplit("/", 1)[-1]
        return f"characters/{character_id}/{filename}"

    def _log_superseded_batch(
        self,
        character_id: str,
        *,
        applied: bool,
        orphan_count: int,
    ) -> None:
        """Note a batch write that a newer owner of the row refused.

        Every commit-path write is conditional on the batch this request read
        at its start. ``applied=False`` means another replica generated a fresh
        batch in between: that batch keeps the row (deleting it would strand
        candidates the player can see but never commit), this request walks
        away, and whatever objects it had not consumed become orphans nobody
        will reference again — cheap, and the alternative is losing a live
        batch.
        """
        if applied:
            return
        _LOGGER.warning(
            "commit_candidates: batch superseded mid-commit "
            "(character=%s, %d candidate objects orphaned)",
            character_id,
            orphan_count,
        )

    async def _delete_candidate_objects(self, object_keys: tuple[str, ...]) -> None:
        assert self._object_storage is not None
        for object_key in object_keys:
            try:
                await self._object_storage.delete(object_key=object_key)
            except Exception:
                _LOGGER.warning(
                    "generate_candidates: could not delete stale candidate %s",
                    object_key,
                )

    async def _record_image_usage_safely(
        self,
        *,
        character: Character,
        feature_key: str,
        provider: object,
        profile_id: str,
        aspect: str,
        requested: int,
        returned: int,
        artifact_count: int,
        status: str,
        started_at: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
        billable_quantity: int | None = None,
    ) -> None:
        if self._usage_recorder is None:
            return
        completed_at = datetime.now(timezone.utc)
        usage_parts = image_usage_parts_from_provider(
            provider=provider,
            requested=requested,
            returned=returned,
            status=status,
            billable_quantity=billable_quantity,
            base_metadata={"aspect": aspect, "batch": requested},
        )
        try:
            await self._usage_recorder.record(UsageEventDraft(
                capability=CAPABILITY_IMAGE,
                character_id=character.id,
                operator_id=getattr(character, "user_id", ""),
                feature_key=feature_key,
                source_surface="character_image_service",
                upstream_request_id=str(
                    getattr(provider, "last_request_id", "") or "",
                ),
                provider_id=usage_parts.provider_id,
                model_id=usage_parts.model_id,
                profile_id=profile_id,
                quantity=usage_parts.quantity,
                cost=usage_parts.cost,
                latency_ms=int((completed_at - started_at).total_seconds() * 1000),
                status=status,
                error_code=error_code,
                error_message=error_message,
                artifact_count=artifact_count,
                metadata=usage_parts.metadata,
                completed_at=completed_at,
            ))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("character_image: usage recorder dispatch failed")
