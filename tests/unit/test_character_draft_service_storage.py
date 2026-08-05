"""VP4 — the draft reference image is staged to object storage, then dropped.

Three properties carry the whole ticket, and all three are about the *edges*:

1. the staged object's lifetime is exactly the call — it is deleted whether the
   draft succeeded or the generator exploded. A missed delete on the failure
   paths is how a "scratch" prefix quietly becomes permanent storage;
2. staging happens *inside* the billing gate, so a refused request never
   touches storage at all. A 402 is the response a client retries, and the
   ordering decides whether an empty wallet costs one wasted object write per
   attempt or none;
3. staging is an optimisation, never a precondition. A storage that refuses the
   write, a media ref that cannot be promoted to an absolute URL, or a
   deployment with no storage at all must all still return a draft — with the
   inline bytes the pre-VP4 code always sent.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import pytest

from kokoro_link.application.services.character_draft_service import (
    CharacterDraftService,
)
from kokoro_link.contracts.character_draft import CharacterDraft, ImageInput
from kokoro_link.contracts.cloud_action_billing import ACTION_KIND_USER_ACTION
from kokoro_link.contracts.object_storage import (
    EPHEMERAL_OBJECT_KEY_PREFIXES,
    ObjectStorageError,
    StoredObject,
)
from kokoro_link.infrastructure.character_draft.stub import (
    StubCharacterDraftGenerator,
)
from kokoro_link.infrastructure.storage.keys import validate_object_key

pytestmark = pytest.mark.asyncio

PUBLIC_BASE_URL = "https://app.example.test"
IMAGE = ImageInput(data=b"\x89PNG-not-really", mime_type="image/png")


# --- doubles ---------------------------------------------------------------


class _RecordingStorage:
    """Records every put/delete; can be told to fail either one.

    Only the two methods this service touches are implemented — a draft never
    reads an object back, and a double that grew the rest of the port would
    hide that.
    """

    def __init__(
        self,
        *,
        put_error: Exception | None = None,
        delete_error: Exception | None = None,
        stored_url: str | None = None,
    ) -> None:
        self.puts: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self._put_error = put_error
        self._delete_error = delete_error
        # ``None`` = the adapter's normal app-relative media ref. A literal
        # value lets a test hand back something unpromotable.
        self._stored_url = stored_url

    async def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: Any = None,
    ) -> StoredObject:
        self.puts.append(
            {
                "object_key": object_key,
                "content": content,
                "content_type": content_type,
                "metadata": dict(metadata) if metadata else None,
            },
        )
        if self._put_error is not None:
            raise self._put_error
        url = (
            self._stored_url
            if self._stored_url is not None
            else f"/v1/public/{object_key}"
        )
        return StoredObject(
            object_key=object_key,
            url=url,
            content_type=content_type,
            size_bytes=len(content),
        )

    async def delete(self, *, object_key: str) -> None:
        self.deletes.append(object_key)
        if self._delete_error is not None:
            raise self._delete_error


class _RecordingGenerator:
    """Remembers the :class:`ImageInput` it was handed; can explode instead."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.images: list[ImageInput | None] = []
        self._error = error
        self._stub = StubCharacterDraftGenerator()

    async def generate(
        self,
        *,
        prompt: str | None,
        image: ImageInput | None,
        operator_primary_language: str = "zh-TW",
        operator_id: str | None = None,
    ) -> CharacterDraft:
        self.images.append(image)
        if self._error is not None:
            raise self._error
        return await self._stub.generate(
            prompt=prompt,
            image=image,
            operator_primary_language=operator_primary_language,
            operator_id=operator_id,
        )

    @property
    def image(self) -> ImageInput | None:
        assert self.images, "generator was never called"
        return self.images[-1]


class _OutOfCredits(RuntimeError):
    """Stand-in for the structured 402 the wallet raises at the gate."""


class _RefusingActionBilling:
    """Billing service whose ``action`` context refuses on entry.

    Same protocol as :class:`NullActionBillingService` (the null object every
    uninstrumented caller gets), except the ``async with`` never reaches its
    body — the shape of an ``insufficient_credits`` refusal, which is a
    decision the service deliberately propagates rather than swallowing.
    """

    def __init__(self) -> None:
        self.entered = 0

    async def begin(self, action_key: str, **kwargs: Any) -> None:
        raise _OutOfCredits(action_key)

    async def settle(self, handle: Any, **kwargs: Any) -> None:
        return None

    async def release(self, handle: Any, **kwargs: Any) -> None:
        return None

    async def wait_for_pending_closes(self) -> None:
        return None

    @asynccontextmanager
    async def action(
        self,
        action_key: str,
        *,
        operator_id: str,
        action_kind: str = ACTION_KIND_USER_ACTION,
        quoted_price_cr: float | None = None,
        interaction_id: str | None = None,
    ) -> Any:
        self.entered += 1
        raise _OutOfCredits(action_key)
        yield None  # pragma: no cover — unreachable; keeps this a generator


def _service(
    generator: _RecordingGenerator,
    *,
    storage: _RecordingStorage | None = None,
    public_base_url: str = PUBLIC_BASE_URL,
    action_billing: Any = None,
) -> CharacterDraftService:
    return CharacterDraftService(
        generator=generator,  # type: ignore[arg-type]
        action_billing=action_billing,
        object_storage=storage,  # type: ignore[arg-type]
        public_base_url=public_base_url,
    )


# --- happy path ------------------------------------------------------------


async def test_reference_image_is_staged_then_deleted() -> None:
    storage = _RecordingStorage()
    generator = _RecordingGenerator()
    service = _service(generator, storage=storage)

    response = await service.generate(
        prompt="一個溫柔的圖書館員", image=IMAGE, operator_id="u-1",
    )

    assert response.name
    assert len(storage.puts) == 1
    put = storage.puts[0]
    object_key = put["object_key"]
    # Asserted against the shared constant, not a literal: the TTL sweep and
    # the variant decorator's skip list key off the same tuple, so a writer
    # that drifted out of it would leak objects nothing cleans up — and a
    # literal here would keep passing while it did.
    assert object_key.startswith(EPHEMERAL_OBJECT_KEY_PREFIXES)
    assert object_key.startswith(f"{EPHEMERAL_OBJECT_KEY_PREFIXES[0]}u-1/")
    assert object_key.endswith(".png")
    assert put["content"] == IMAGE.data
    assert put["content_type"] == "image/png"

    staged = generator.image
    assert staged is not None
    assert staged.data == IMAGE.data
    assert staged.public_url == f"{PUBLIC_BASE_URL}/v1/public/{object_key}"

    # Deleted once the draft is done — the object's lifetime is this call.
    assert storage.deletes == [object_key]


async def test_staged_key_survives_the_shared_key_validator() -> None:
    # The storage service rejects unsafe keys at the edge; a key this service
    # builds must never be one the adapter would refuse.
    storage = _RecordingStorage()
    service = _service(_RecordingGenerator(), storage=storage)

    await service.generate(prompt="x", image=IMAGE, operator_id="u-1")

    key = storage.puts[0]["object_key"]
    assert validate_object_key(key) == key


# --- deletion on every exit ------------------------------------------------


async def test_generator_failure_still_deletes_the_staged_image() -> None:
    storage = _RecordingStorage()
    generator = _RecordingGenerator(error=RuntimeError("model refused"))
    service = _service(generator, storage=storage)

    with pytest.raises(RuntimeError, match="model refused"):
        await service.generate(prompt="x", image=IMAGE, operator_id="u-1")

    assert storage.deletes == [storage.puts[0]["object_key"]]


async def test_billing_refusal_never_reaches_storage_at_all() -> None:
    """The credit gate is upstream of staging, not downstream of it.

    A 402 is the one refusal a client retries, so this ordering is the
    difference between an empty wallet costing nothing and it costing one
    object write plus one delete on *every* attempt — work that, by
    definition, nobody is paying for.

    Nothing is stranded by putting the gate first: ``_stage_image`` is
    fail-soft and never raises, so it cannot fail after the reservation.
    """
    storage = _RecordingStorage()
    billing = _RefusingActionBilling()
    generator = _RecordingGenerator()
    service = _service(generator, storage=storage, action_billing=billing)

    with pytest.raises(_OutOfCredits):
        await service.generate(prompt="x", image=IMAGE, operator_id="u-1")

    assert billing.entered == 1
    assert storage.puts == []
    assert storage.deletes == []
    assert generator.images == []


# --- fail-soft -------------------------------------------------------------


async def test_storage_failure_falls_back_to_inline_bytes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = _RecordingStorage(put_error=ObjectStorageError("backend down"))
    generator = _RecordingGenerator()
    service = _service(generator, storage=storage)

    with caplog.at_level(logging.WARNING):
        response = await service.generate(
            prompt="x", image=IMAGE, operator_id="u-1",
        )

    assert response.name
    staged = generator.image
    assert staged is not None
    assert staged.public_url is None
    assert staged.data == IMAGE.data
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    # Nothing landed, so there is nothing to delete — and asking would only
    # add a second failing round-trip to an already-broken backend.
    assert storage.deletes == []


async def test_unpromotable_media_ref_falls_back_but_still_cleans_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The write landed; only the URL promotion failed. The bytes exist, so the
    # cleanup obligation exists too.
    storage = _RecordingStorage(stored_url="s3://internal-bucket/opaque")
    generator = _RecordingGenerator()
    service = _service(generator, storage=storage)

    with caplog.at_level(logging.WARNING):
        response = await service.generate(
            prompt="x", image=IMAGE, operator_id="u-1",
        )

    assert response.name
    staged = generator.image
    assert staged is not None and staged.public_url is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert storage.deletes == [storage.puts[0]["object_key"]]


async def test_delete_failure_does_not_break_the_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = _RecordingStorage(delete_error=ObjectStorageError("gone weird"))
    generator = _RecordingGenerator()
    service = _service(generator, storage=storage)

    with caplog.at_level(logging.WARNING):
        response = await service.generate(
            prompt="x", image=IMAGE, operator_id="u-1",
        )

    assert response.name
    assert storage.deletes == [storage.puts[0]["object_key"]]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# --- opt-out: self-host and any deployment without both halves -------------


async def test_no_storage_keeps_the_pre_vp4_inline_path() -> None:
    generator = _RecordingGenerator()
    service = CharacterDraftService(generator=generator)  # type: ignore[arg-type]

    await service.generate(prompt="x", image=IMAGE, operator_id="u-1")

    staged = generator.image
    assert staged is IMAGE
    assert staged.public_url is None


async def test_missing_public_base_url_never_touches_storage() -> None:
    # An app-relative ref no provider can fetch is the same as not staging —
    # except it would also write (and have to delete) an object for nothing.
    storage = _RecordingStorage()
    generator = _RecordingGenerator()
    service = _service(generator, storage=storage, public_base_url="   ")

    await service.generate(prompt="x", image=IMAGE, operator_id="u-1")

    assert storage.puts == []
    assert storage.deletes == []
    assert generator.image is IMAGE


async def test_a_draft_without_an_image_never_touches_storage() -> None:
    storage = _RecordingStorage()
    generator = _RecordingGenerator()
    service = _service(generator, storage=storage)

    await service.generate(prompt="x", image=None, operator_id="u-1")

    assert storage.puts == []
    assert storage.deletes == []
    assert generator.image is None


# --- key sanitisation ------------------------------------------------------


@pytest.mark.parametrize(
    "operator_id",
    ["../evil", "..", ".", "", None, "a/b/c", "op 1;rm -rf /", "使用者"],
)
async def test_operator_id_cannot_shape_the_object_key(
    operator_id: str | None,
) -> None:
    storage = _RecordingStorage()
    service = _service(_RecordingGenerator(), storage=storage)

    await service.generate(prompt="x", image=IMAGE, operator_id=operator_id)

    key = storage.puts[0]["object_key"]
    parts = key.split("/")
    assert parts[0] == "draft-uploads"
    # Exactly three segments: prefix / operator bucket / filename. Anything
    # else means the id smuggled a separator through.
    assert len(parts) == 3
    assert parts[1] not in ("", ".", "..")
    assert validate_object_key(key) == key


async def test_unknown_mime_type_falls_back_to_a_png_suffix() -> None:
    storage = _RecordingStorage()
    service = _service(_RecordingGenerator(), storage=storage)

    await service.generate(
        prompt="x",
        image=ImageInput(data=b"bytes", mime_type="image/not-a-real-type"),
        operator_id="u-1",
    )

    assert storage.puts[0]["object_key"].endswith(".png")
    # The declared type still travels with the object — only the key's cosmetic
    # suffix is guessed.
    assert storage.puts[0]["content_type"] == "image/not-a-real-type"
