"""AP2 — the three non-chat action-priced entry points.

``image_portrait``, ``character_draft`` and ``image_chat_tool`` are each one
player-visible action. What is worth testing is not that the charge happens
(the billing service already owns that) but that each entry point closes it on
the right signal: the portrait raises on failure, the draft raises, and the
image tool reports failure as a *value* — so a naive ``async with`` would have
billed a picture that never appeared.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_draft_service import (
    CharacterDraftService,
)
from kokoro_link.application.services.character_image_service import (
    MAX_IMAGES_PER_CHARACTER,
    CharacterImageError,
    CharacterImageService,
    GenerationDisabledError,
    GenerationFailedError,
    TooManyImagesError,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_IMAGE_CHAT_TOOL,
    ActionCharge,
    client_quoted_price_scope,
    prepaid_action_scope,
)
from kokoro_link.contracts.image_provider import ImageGenerationError
from kokoro_link.contracts.interaction_context import (
    BILLING_COVERED_HEADER_NAME,
    InteractionContext,
    covered_call_receipt,
    current_interaction,
    mark_interaction_call_served,
)
from kokoro_link.contracts.tool import ToolContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
    AccountRuntimeProfile,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.character_draft.stub import (
    StubCharacterDraftGenerator,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.tools.comfyui.tool import ComfyImageTool
from tests.unit._image_provider_stub import StaticActiveImageProvider

pytestmark = pytest.mark.asyncio

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_ACTION_TIER = AccountRuntimeProfile(
    name="standard", billing_shape=BILLING_SHAPE_ACTION_FIXED,
)


class _RecordingClient:
    def __init__(self) -> None:
        self.charges: list[dict] = []
        self.settled: list[str] = []
        self.released: list[str] = []
        self.probed_releases: list[bool] = []
        """``settle_if_probed`` as sent per release — the flag that tells
        the ledger "Core cannot know what was served, you decide"."""

    async def charge(self, **kwargs) -> ActionCharge:
        self.charges.append(kwargs)
        return ActionCharge(charge_id="chg-1", price_cr=3.0)

    async def settle(self, charge_id: str) -> None:
        self.settled.append(charge_id)

    async def release(
        self, charge_id: str, *, settle_if_probed: bool = False,
    ) -> None:
        self.released.append(charge_id)
        self.probed_releases.append(settle_if_probed)


class _StubProfiles:
    async def resolve_for_operator(self, operator_id: str):
        return _ACTION_TIER


class _StubOperatorRepository:
    async def get(self, operator_id: str):
        class _Operator:
            cloud_tenant_id = "tenant-1"

        return _Operator()


def _billing() -> tuple[CloudActionBillingService, _RecordingClient]:
    client = _RecordingClient()
    return (
        CloudActionBillingService(
            client=client,
            profile_resolver=_StubProfiles(),
            operator_profiles=_StubOperatorRepository(),
        ),
        client,
    )


class _StubImageProvider:
    """Stands in for the Cloud Gateway image provider, receipt behaviour and all.

    The receipt is *deferred* rather than marked, exactly as the real adapter
    does: bytes in memory are not what the player bought, so the charge is only
    consumed once the caller has persisted them (C4').
    """

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.scopes: list[object] = []

    async def generate(self, **kwargs) -> list[bytes]:  # noqa: ANN003
        self.scopes.append(current_interaction())
        if self._error is not None:
            raise self._error
        covered_call_receipt(
            {BILLING_COVERED_HEADER_NAME: "1"},
        ).defer_delivery()
        return [_PNG_BYTES]


class _BrokenObjectStorage:
    """Object storage that answers every write with a failure."""

    async def put_bytes(self, **kwargs):  # noqa: ANN003
        raise RuntimeError("bucket unreachable")


def _character() -> Character:
    return Character.create(
        name="Yuki",
        summary="",
        personality=[], interests=[], speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        allowed_tools=["generate_image"],
    )


# --- image_portrait ---------------------------------------------------------


async def _portrait_service(
    tmp_path: Path, *, error: Exception | None = None,
) -> tuple[CharacterImageService, CharacterService, _RecordingClient, _StubImageProvider]:
    repo = InMemoryCharacterRepository()
    billing, client = _billing()
    provider = _StubImageProvider(error=error)
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=StaticActiveImageProvider(provider),  # type: ignore[arg-type]
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        action_billing=billing,
    )
    return service, CharacterService(repo), client, provider


async def test_portrait_charges_once_and_settles(tmp_path: Path) -> None:
    service, chars, client, provider = await _portrait_service(tmp_path)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    await service.generate_portrait(created.id, positive="cafe")

    assert [c["action_key"] for c in client.charges] == ["image_portrait"]
    assert client.settled == ["chg-1"]
    # The Gateway call is stamped with the charge that covers it.
    assert provider.scopes[0] is not None


async def test_a_portrait_that_never_persists_is_refunded(tmp_path: Path) -> None:
    """C4': the deliverable is the stored picture, not the bytes in memory.

    Marking the covered call as delivered when the Gateway answered would bill
    a player whose portrait never reached object storage — money for an image
    that does not exist anywhere they can see it.
    """
    repo = InMemoryCharacterRepository()
    billing, client = _billing()
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=StaticActiveImageProvider(_StubImageProvider()),  # type: ignore[arg-type]
        object_storage=_BrokenObjectStorage(),  # type: ignore[arg-type]
        action_billing=billing,
    )
    created = await CharacterService(repo).create_character(
        CreateCharacterRequest(name="Yui"),
    )

    with pytest.raises(Exception):
        await service.generate_portrait(created.id, positive="cafe")

    assert client.settled == []
    assert client.released == ["chg-1"]


async def test_portrait_binds_the_price_the_player_was_quoted(
    tmp_path: Path,
) -> None:
    """R9 close-out: the portrait is billed at the number on *this* screen.

    Core's process cache is only a proxy for what was displayed — a second
    replica, or a cache refreshed between the quote and the press, holds a
    price this player never saw. The SPA therefore sends its own quote and the
    charge binds to that.
    """
    service, chars, client, _ = await _portrait_service(tmp_path)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    await service.generate_portrait(
        created.id, positive="cafe", quoted_price_cr=7.5,
    )

    assert client.charges[0]["expected_price_cr"] == 7.5


async def test_portrait_without_a_client_quote_charges_as_before(
    tmp_path: Path,
) -> None:
    # An older client (and the background primary-portrait path) sends nothing;
    # the charge must still happen, unbound, exactly as it did pre-R9.
    service, chars, client, _ = await _portrait_service(tmp_path)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    await service.generate_portrait(created.id, positive="cafe")

    assert client.charges[0]["expected_price_cr"] is None


async def test_failed_portrait_releases_the_charge(tmp_path: Path) -> None:
    service, chars, client, _ = await _portrait_service(
        tmp_path, error=ImageGenerationError("gpu on fire"),
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    with pytest.raises(GenerationFailedError):
        await service.generate_portrait(created.id, positive="cafe")

    assert client.settled == []
    assert client.released == ["chg-1"]


# --- image_portrait, as the player actually reaches it ----------------------
#
# ``/images/generate`` is API surface; the picture button in
# ``CharacterImagesPanel`` posts a *candidate batch*. Leaving that path
# uncharged made the only path real players walk the free one, which is why
# these live next to the single-portrait cases rather than in a corner.


async def test_a_candidate_batch_is_one_charge(tmp_path: Path) -> None:
    service, chars, client, provider = await _portrait_service(tmp_path)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    _, urls = await service.generate_candidates(
        created.id, positive="cafe", count=4,
    )

    assert urls  # the batch produced something the player can pick from
    assert [c["action_key"] for c in client.charges] == ["image_portrait"]
    assert client.settled == ["chg-1"]
    # ...and the batch's upstream call ran inside the charge's scope, so the
    # Gateway waived it instead of billing the render per call on top.
    assert provider.scopes[0] is not None


async def test_a_candidate_batch_charges_once_not_once_per_image(
    tmp_path: Path,
) -> None:
    """One press of the button is one action, whatever ``count`` says.

    The price hint above the button shows a single number; charging per
    returned candidate would bill four times what the player agreed to.
    """
    service, chars, client, _ = await _portrait_service(tmp_path)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    await service.generate_candidates(created.id, positive="cafe", count=4)

    assert len(client.charges) == 1
    assert len(client.settled) == 1


async def test_a_candidate_batch_that_never_persists_is_refunded(
    tmp_path: Path,
) -> None:
    """Same C4' rule as the single portrait: the deliverable is the stored file.

    Candidates the player can never open are not a picture they bought.
    """
    repo = InMemoryCharacterRepository()
    billing, client = _billing()
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=StaticActiveImageProvider(_StubImageProvider()),  # type: ignore[arg-type]
        object_storage=_BrokenObjectStorage(),  # type: ignore[arg-type]
        action_billing=billing,
    )
    created = await CharacterService(repo).create_character(
        CreateCharacterRequest(name="Yui"),
    )

    with pytest.raises(Exception):
        await service.generate_candidates(created.id, positive="cafe")

    assert client.settled == []
    assert client.released == ["chg-1"]


async def test_a_refused_candidate_batch_is_never_charged(
    tmp_path: Path,
) -> None:
    """The cheap refusals come first, so no money moves and comes back.

    A full album is a "no" this service already knows without asking the
    wallet; charging and refunding it would show up on the player's ledger as
    a spend for a batch that never rendered.
    """
    service, chars, client, _ = await _portrait_service(tmp_path)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    character = await service._load_character(created.id)  # noqa: SLF001
    await service._character_repository.save(  # noqa: SLF001
        character.with_image_urls(
            tuple(f"/uploads/{i}.png" for i in range(MAX_IMAGES_PER_CHARACTER)),
        ),
    )

    with pytest.raises(TooManyImagesError):
        await service.generate_candidates(created.id, positive="cafe")

    assert client.charges == []
    assert client.settled == [] and client.released == []


async def test_a_candidate_batch_with_no_active_provider_is_never_charged(
    tmp_path: Path,
) -> None:
    """"No image profile is active" is a refusal, not a failed render.

    It is a *configuration* answer — same for every press until somebody fixes
    the profile — so raising the reservation first would print a charge and a
    refund on the player's ledger for each attempt, for pictures nothing ever
    tried to generate. Resolving the provider therefore sits with the other
    cheap refusals, ahead of the charge.
    """
    repo = InMemoryCharacterRepository()
    billing, client = _billing()
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=StaticActiveImageProvider(None),  # type: ignore[arg-type]
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        action_billing=billing,
    )
    created = await CharacterService(repo).create_character(
        CreateCharacterRequest(name="Yui"),
    )

    with pytest.raises(GenerationDisabledError):
        await service.generate_candidates(created.id, positive="cafe")

    assert client.charges == []
    assert client.settled == [] and client.released == []


async def test_a_candidate_batch_that_cannot_be_recorded_is_refunded(
    tmp_path: Path,
) -> None:
    """Stored bytes nobody can find are not a picture the player bought.

    Object storage has no list-by-prefix, so the batch row is the only way the
    picker can ever reach these candidates: without it they are orphan keys and
    the album stays empty. The delivery confirmation therefore comes *after*
    the row lands — confirming before it would leave the player charged for a
    gallery the next request cannot open.
    """
    class _BrokenBatches:
        async def get(self, character_id: str) -> tuple[str, ...]:
            return ()

        async def replace(self, character_id, object_keys, **kwargs):  # noqa: ANN001, ANN003
            raise RuntimeError("batch table unreachable")

        async def clear(self, character_id, **kwargs):  # noqa: ANN001, ANN003
            return True

    repo = InMemoryCharacterRepository()
    billing, client = _billing()
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=StaticActiveImageProvider(_StubImageProvider()),  # type: ignore[arg-type]
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        candidate_batch_repository=_BrokenBatches(),  # type: ignore[arg-type]
        action_billing=billing,
    )
    created = await CharacterService(repo).create_character(
        CreateCharacterRequest(name="Yui"),
    )

    with pytest.raises(CharacterImageError):
        await service.generate_candidates(created.id, positive="cafe")

    assert client.settled == []
    assert client.released == ["chg-1"]
    # A plain refund, not a probed one: the covered call was never confirmed,
    # so the handle's own empty usage record is the honest answer here.
    assert client.probed_releases == [False]


async def test_a_candidate_batch_binds_the_price_the_player_was_quoted(
    tmp_path: Path,
) -> None:
    service, chars, client, _ = await _portrait_service(tmp_path)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    await service.generate_candidates(
        created.id, positive="cafe", quoted_price_cr=7.5,
    )

    assert client.charges[0]["expected_price_cr"] == 7.5


# --- character_draft --------------------------------------------------------


async def test_character_draft_charges_once_and_settles() -> None:
    class _CoveredGenerator(StubCharacterDraftGenerator):
        async def generate(self, **kwargs):  # noqa: ANN003
            mark_interaction_call_served()  # the Gateway waived the draft call
            return await super().generate(**kwargs)

    billing, client = _billing()
    service = CharacterDraftService(
        generator=_CoveredGenerator(), action_billing=billing,
    )

    await service.generate(prompt="一個溫柔的圖書館員", image=None, operator_id="op-1")

    assert [c["action_key"] for c in client.charges] == ["character_draft"]
    assert client.settled == ["chg-1"]


async def test_character_draft_binds_the_price_the_player_was_quoted() -> None:
    # R9 close-out, draft half: same rule as the portrait — the quote that
    # binds the charge is the one the player's own client displayed.
    billing, client = _billing()
    service = CharacterDraftService(
        generator=StubCharacterDraftGenerator(), action_billing=billing,
    )

    await service.generate(
        prompt="一個溫柔的圖書館員",
        image=None,
        operator_id="op-1",
        quoted_price_cr=2.25,
    )

    assert client.charges[0]["expected_price_cr"] == 2.25


async def test_character_draft_without_a_client_quote_charges_as_before() -> None:
    billing, client = _billing()
    service = CharacterDraftService(
        generator=StubCharacterDraftGenerator(), action_billing=billing,
    )

    await service.generate(prompt="x", image=None, operator_id="op-1")

    assert client.charges[0]["expected_price_cr"] is None


async def test_failed_character_draft_releases_the_charge() -> None:
    class _ExplodingGenerator:
        async def generate(self, **kwargs):  # noqa: ANN003
            raise RuntimeError("model refused")

    billing, client = _billing()
    service = CharacterDraftService(
        generator=_ExplodingGenerator(), action_billing=billing,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError):
        await service.generate(prompt="x", image=None, operator_id="op-1")

    assert client.released == ["chg-1"]
    assert client.settled == []


# --- image_chat_tool --------------------------------------------------------


def _tool(
    tmp_path: Path, *, error: Exception | None = None,
) -> tuple[ComfyImageTool, _RecordingClient, _StubImageProvider]:
    billing, client = _billing()
    provider = _StubImageProvider(error=error)
    tool = ComfyImageTool(
        image_provider=StaticActiveImageProvider(provider),  # type: ignore[arg-type]
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        action_billing=billing,
    )
    return tool, client, provider


async def test_image_tool_charges_its_own_action_and_settles(
    tmp_path: Path,
) -> None:
    tool, client, provider = _tool(tmp_path)

    result = await tool.invoke(
        ToolContext(character=_character(), arguments={"positive": "cafe"}),
    )

    assert result.ok is True
    assert [c["action_key"] for c in client.charges] == ["image_chat_tool"]
    assert client.settled == ["chg-1"]
    assert provider.scopes[0] is not None


async def test_image_tool_releases_when_generation_reports_failure(
    tmp_path: Path,
) -> None:
    """The tool reports failure as a value so the character can apologise —
    the charge has to follow the result, not the control flow."""
    tool, client, _ = _tool(tmp_path, error=ImageGenerationError("gpu on fire"))

    result = await tool.invoke(
        ToolContext(character=_character(), arguments={"positive": "cafe"}),
    )

    assert result.ok is False
    assert client.settled == []
    assert client.released == ["chg-1"]


async def test_an_image_tool_picture_that_never_stores_is_refunded(
    tmp_path: Path,
) -> None:
    """C4': the picture the player paid for is the one in object storage."""
    billing, client = _billing()
    tool = ComfyImageTool(
        image_provider=StaticActiveImageProvider(_StubImageProvider()),  # type: ignore[arg-type]
        uploads_dir=tmp_path,
        object_storage=_BrokenObjectStorage(),  # type: ignore[arg-type]
        action_billing=billing,
    )

    result = await tool.invoke(
        ToolContext(character=_character(), arguments={"positive": "cafe"}),
    )

    assert result.ok is False
    assert client.settled == []
    assert client.released == ["chg-1"]


async def test_the_image_tool_binds_the_price_the_player_was_quoted(
    tmp_path: Path,
) -> None:
    """R9: the picture is billed at the number the turn's client had on screen.

    The model decides to draw mid-turn, so the quote has to arrive with the
    turn — there is no second request to attach it to.
    """
    tool, client, _ = _tool(tmp_path)

    with client_quoted_price_scope({ACTION_IMAGE_CHAT_TOOL: 4.5}):
        await tool.invoke(
            ToolContext(character=_character(), arguments={"positive": "cafe"}),
        )

    assert client.charges[0]["expected_price_cr"] == 4.5


async def test_a_prepaid_image_tool_does_not_charge_a_second_time(
    tmp_path: Path,
) -> None:
    """The over-quota picture is bought once, at the overage price.

    ``chat_image_overage`` already paid for this picture, so raising
    ``image_chat_tool`` on top would bill one picture twice and put two rows in
    /credits for a single player action.
    """
    tool, client, provider = _tool(tmp_path)
    prepaid = InteractionContext(interaction_id="int-9", charge_id="chg-overage")

    with prepaid_action_scope(ACTION_IMAGE_CHAT_TOOL, prepaid):
        result = await tool.invoke(
            ToolContext(character=_character(), arguments={"positive": "cafe"}),
        )

    assert result.ok is True
    assert client.charges == []
    assert client.settled == []
    assert client.released == []
    # The generation still has to name the charge that DID pay for it, or the
    # Gateway would bill the image call per token on top of the overage price.
    assert provider.scopes[0] == prepaid
