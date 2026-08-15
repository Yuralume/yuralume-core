"""Installing a cloud-exclusive (IP-partner) official card — EC4.

The two Cloud reads are faked at their ports, so what these pin is the set
of decisions Core makes with what came back: what a managed character is
born with, which language its prose lands in, where the official art goes,
and every place the install is asked to keep going rather than fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.services.character_image_service import (
    MAX_IMAGES_PER_CHARACTER,
    CharacterImageService,
)
from kokoro_link.application.services.character_service import (
    CharacterService,
    CharacterValidationError,
)
from kokoro_link.application.services.exclusive_official_card_install import (
    ExclusiveCardEntitlementRequiredError,
    ExclusiveOfficialCardInstaller,
)
from kokoro_link.application.services.official_character_art import (
    is_official_art_key,
    is_official_reference_key,
)
from kokoro_link.contracts.official_card_catalog import (
    OfficialCardDetail,
    OfficialCardImage,
)
from kokoro_link.contracts.official_card_exclusive import (
    ExclusiveCardPayload,
    ExclusiveCardTranslation,
    OfficialCardExclusivePayloadPort,
    TRANSLATION_STATUS_APPROVED,
    TRANSLATION_STATUS_MISSING,
)
from kokoro_link.contracts.tts_catalog import TTSVoice
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

_PLAYER = "player-1"
_CARD_ID = "mio-cafe"
_IMAGE_BASE = (
    "https://app.yuralume.com/api/user/v1/public/official-cards/mio-cafe/images"
)


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #


class _FakeCatalog:
    """Only the two methods the installer reaches for."""

    def __init__(self, *, image_bytes: bytes | None = b"\x89PNG-art") -> None:
        self.image_bytes = image_bytes
        self.downloaded: list[str] = []

    async def list_cards(self, *, locale: str):  # pragma: no cover — unused
        return []

    async def get_card(self, card_id, *, locale, fresh=False):  # pragma: no cover
        return None

    async def download_artifact(self, card_id: str):  # pragma: no cover
        return None

    async def download_image(self, *, url: str) -> bytes | None:
        self.downloaded.append(url)
        return self.image_bytes


class _FakeExclusivePayloads(OfficialCardExclusivePayloadPort):
    def __init__(self, payload: ExclusiveCardPayload | None) -> None:
        self._payload = payload
        # ``(card_id, tenant_id)``: the tenant is half of what Cloud checks
        # a tier fence against (TG3), so a double that dropped it would let
        # an install that names nobody pass here and 404 in production.
        self.requested: list[tuple[str, str | None]] = []

    async def fetch_payload(
        self, card_id: str, *, tenant_id: str | None = None,
    ) -> ExclusiveCardPayload | None:
        self.requested.append((card_id, tenant_id))
        return self._payload


class _FakeVoiceCatalog:
    """Stands in for the cloud catalog *including its origin filter* (EC5-A/D).

    An exclusive voice is only listed to a caller that has declared the card
    it belongs to, so a double that handed them out unconditionally would let
    an install that declares nothing pass here and bind nothing in
    production. Every call's declared origin is recorded.
    """

    def __init__(
        self,
        voices: list[TTSVoice] | None = None,
        *,
        fails: bool = False,
        unfiltered: bool = False,
    ) -> None:
        self._voices = voices or []
        self._fails = fails
        # ``unfiltered`` models a catalog that hands out a binding the caller
        # did not ask for, so the install's own match is pinned rather than
        # riding on the service's filter.
        self._unfiltered = unfiltered
        self.declared_origins: list[str] = []

    async def list_voices(
        self, *, character_id: str | None = None, character_origin: str = "",
    ) -> list[TTSVoice]:
        self.declared_origins.append(character_origin)
        if self._fails:
            raise RuntimeError("cloud TTS voice catalog unreachable")
        if self._unfiltered:
            return list(self._voices)
        return [
            voice for voice in self._voices
            if not voice.exclusive_card_id
            or voice.exclusive_card_id == character_origin
        ]


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _detail(*, image_count: int = 2) -> OfficialCardDetail:
    return OfficialCardDetail(
        id=_CARD_ID,
        title="美緒",
        description="官方卡",
        tags=("現代",),
        author="Yuralume",
        locale="zh-Hant",
        # A cloud-exclusive detail is masked: no prose, no artifact.
        profile={},
        artifact_published=False,
        images=tuple(
            OfficialCardImage(
                index=index,
                url=f"{_IMAGE_BASE}/{index}",
                content_type="image/png",
                width=1024,
                height=1536,
            )
            for index in range(image_count)
        ),
    )


def _payload(**overrides) -> ExclusiveCardPayload:
    base = dict(
        card_id=_CARD_ID,
        title="美緒",
        description="官方卡",
        tags=("現代",),
        author="Yuralume",
        note="建議搭配動漫風 profile",
        profile={
            "name": "美緒",
            "summary": "咖啡廳打工女大生",
            "personality": ["開朗", "好奇"],
            "speaking_style": "輕快",
            "appearance": "短髮",
            "companions": [
                {
                    "name": "小春",
                    "role": "同事",
                    "brief_profile": "同班同學",
                    "personality_sketch": ["穩重"],
                    "relationship_snippet": "從高中就認識",
                },
            ],
        },
        translations=(),
        reference_image_indexes=(1,),
    )
    base.update(overrides)
    return ExclusiveCardPayload(**base)


def _card_document(**character_overrides) -> dict:
    """The card's own ``manifest.json``, as Cloud reads it back out of the
    stored ``.lumecard`` (EC4-C).

    Every structural key here is set *away* from this build's default, which
    is the only way a test can tell "the card said so" from "nothing was
    applied and the defaults happen to match".
    """
    character = {
        "name": "美緒",
        "summary": "咖啡廳打工女大生",
        "world_frame": "school",
        "allowed_tools": ["generate_image"],
        "date_of_birth": "2004-03-09",
        "disposition": {"self_centeredness": "high", "candor": "low"},
        "visual_subject_type": "anthropomorphic",
        "proactive_daily_limit": 7,
        "proactive_cooldown_minutes": 90,
        "feed_daily_limit": 1,
        "accepts_web_proactive": False,
        "world_awareness_enabled": True,
        "subscribed_categories": ["culture"],
    }
    character.update(character_overrides)
    return {
        "schema_version": 1,
        "card": {"title": "", "author": "Yuralume"},
        "character": character,
        "stage_images": ["assets/stage/0.png"],
        "bundled_arc_templates": [],
    }


def _translation(
    locale: str,
    *,
    name: str,
    status: str = TRANSLATION_STATUS_APPROVED,
    stale: bool = False,
) -> ExclusiveCardTranslation:
    return ExclusiveCardTranslation(
        locale=locale,
        status=status,
        stale=stale,
        payload={
            "name": name,
            "summary": f"{name} summary",
            # Same length as the source list — the merge policy replaces a
            # list only when the lengths line up.
            "personality": [f"{name}-a", f"{name}-b"],
        },
    )


def _build(
    *,
    payload: ExclusiveCardPayload | None,
    catalog: _FakeCatalog | None = None,
    voices: _FakeVoiceCatalog | None = None,
) -> tuple[ExclusiveOfficialCardInstaller, InMemoryCharacterRepository,
           InMemoryObjectStorage, _FakeCatalog]:
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    catalog = catalog or _FakeCatalog()
    installer = ExclusiveOfficialCardInstaller(
        catalog=catalog,
        exclusive_payloads=_FakeExclusivePayloads(payload),
        character_service=CharacterService(
            repo,
            relationship_seed_repository=(
                InMemoryCharacterOperatorRelationshipSeedRepository()
            ),
        ),
        character_image_service=CharacterImageService(
            character_repository=repo,
            uploads_dir=Path("."),
            object_storage=storage,
        ),
        voice_catalog=voices,
    )
    return installer, repo, storage, catalog


# --------------------------------------------------------------------------- #
# what a managed character is born with
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_install_writes_the_card_binding_and_its_licensed_voice() -> None:
    voices = _FakeVoiceCatalog([
        TTSVoice(id="generic", label="Generic"),
        TTSVoice(id="mio-voice", label="美緒", exclusive_card_id=_CARD_ID),
    ])
    installer, repo, _storage, _catalog = _build(
        payload=_payload(), voices=voices,
    )

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    # The card being installed *is* the origin the catalog filters on — there
    # is no character yet to derive it from, so the install declares it
    # itself. Without the declaration the licensed voice is simply not in the
    # listing and every install silently takes the default voice (EC5-D).
    assert voices.declared_origins == [_CARD_ID]
    character = await repo.get(result.character.id)
    assert character is not None
    assert character.origin_official_card_id == _CARD_ID
    assert character.is_managed is True
    assert character.voice_profile is not None
    assert character.voice_profile.voice_id == "mio-voice"
    # The prose landed; the structural settings the payload does not carry
    # are this build's own defaults, not something invented for the card.
    assert character.name == "美緒"
    assert list(character.personality) == ["開朗", "好奇"]
    assert [c.name for c in character.companions] == ["小春"]
    assert character.world_frame == "modern"


# --------------------------------------------------------------------------- #
# the card's own structure (EC4-C)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_cards_manifest_supplies_every_setting_prose_cannot() -> None:
    """The fidelity half of an IP install.

    Cloud's prose payload is the *translatable* field set by definition, so
    a world frame, a tool list, disposition bands, cadence numbers, a visual
    subject type and a birthday were never in it. Before EC4-C the character
    a player received had the licensor's words and the installing build's
    settings — assertions below are precisely the fields where those two
    disagree.
    """
    installer, repo, _storage, _catalog = _build(
        payload=_payload(card_document=_card_document()),
    )

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.world_frame == "school"
    assert list(character.allowed_tools) == ["generate_image"]
    assert character.date_of_birth is not None
    assert character.date_of_birth.isoformat() == "2004-03-09"
    assert character.disposition.self_centeredness == "high"
    assert character.disposition.candor == "low"
    assert character.visual_subject_type == "anthropomorphic"
    assert character.proactive_daily_limit == 7
    assert character.proactive_cooldown_minutes == 90
    assert character.feed_daily_limit == 1
    assert character.accepts_web_proactive is False
    assert character.world_awareness_enabled is True
    assert list(character.subscribed_categories) == ["culture"]


@pytest.mark.asyncio
async def test_the_manifest_supplies_structure_and_the_catalog_still_supplies_words(
) -> None:
    """Neither half may quietly win the other's fields.

    The manifest carries a ``name`` and a ``summary`` too — it is a whole
    card — but those are the half that has been through the locale chain and
    a human's approval on the Cloud console, so the approved Japanese must
    survive contact with the zh-Hant manifest sitting right next to it.
    """
    installer, repo, _storage, _catalog = _build(payload=_payload(
        card_document=_card_document(),
        translations=(_translation("ja", name="ミオ"),),
    ))

    result = await installer.install(_detail(), user_id=_PLAYER, locale="ja")

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.name == "ミオ"
    assert character.summary == "ミオ summary"
    assert list(character.personality) == ["ミオ-a", "ミオ-b"]
    # …and the structural half is untouched by any of that.
    assert character.world_frame == "school"


@pytest.mark.asyncio
async def test_companions_come_from_the_catalog_not_the_manifest() -> None:
    # The manifest's companions carry ids from another deployment and no
    # translation; the catalog's are stripped and approved. Reading the
    # wrong one would hand a partner's character a stale cast.
    installer, repo, _storage, _catalog = _build(payload=_payload(
        card_document=_card_document(companions=[
            {"id": "not-ours", "name": "舊同事", "role": "前輩"},
        ]),
    ))

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert [c.name for c in character.companions] == ["小春"]
    assert all(c.id != "not-ours" for c in character.companions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document",
    [
        # Cloud could not read the stored artifact — it omits the key rather
        # than fail the install.
        {},
        # A manifest from an exporter newer than this build: the ordinary
        # import path refuses it, so it must not land through a laxer door
        # here either.
        _card_document() | {"schema_version": 99},
        # Structurally not a card at all.
        {"schema_version": 1, "character": "not-an-object"},
    ],
)
async def test_an_unusable_card_document_installs_on_the_old_defaults(
    document: dict,
) -> None:
    # The prose payload is still a complete, installable card. Refusing here
    # would turn a fidelity shortfall into a player-visible failure, which is
    # the wrong trade for the one thing EC4-C is fail-soft about.
    installer, repo, _storage, _catalog = _build(
        payload=_payload(card_document=document),
    )

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.name == "美緒"
    assert character.world_frame == "modern"
    assert character.origin_official_card_id == _CARD_ID


@pytest.mark.asyncio
async def test_a_manifest_arc_ref_is_not_carried_into_the_character() -> None:
    # The document is the manifest alone; the templates it names are zip
    # members that never leave Cloud. A ref pointing at one of them would
    # name something this deployment does not have.
    installer, repo, _storage, _catalog = _build(payload=_payload(
        card_document=_card_document(
            arc_template_ref="mio-cafe-arc",
            arc_series_ref="mio-cafe-series",
        ),
    ))

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.arc_template_id is None
    assert character.arc_series_id is None
    assert result.landed_arc_template_ids == []
    assert result.landed_arc_series_ids == []


@pytest.mark.asyncio
async def test_an_exclusive_card_lands_no_arc_material() -> None:
    # There is no `.lumecard` behind an exclusive card, so there is nothing
    # for bundled arcs to ride in. Empty is the answer, not a silent partial.
    installer, _repo, _storage, _catalog = _build(payload=_payload())

    result = await installer.install(_detail(), user_id=_PLAYER, locale="ja")

    assert result.landed_arc_template_ids == []
    assert result.landed_arc_series_ids == []


@pytest.mark.asyncio
async def test_the_players_own_character_slot_rules_still_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IP character is still a character (ticket §4).

    Pinned through the shared gate rather than by counting rows: the whole
    point is that this path did not grow its own create, so the wall an
    account hits on ``POST /characters`` is the same object here.
    """
    installer, repo, _storage, _catalog = _build(payload=_payload())

    async def _at_the_cap(*_args, **_kwargs):
        raise CharacterValidationError("Character slot limit reached")

    monkeypatch.setattr(
        installer._character_service,  # noqa: SLF001 — pinning the seam
        "ensure_character_create_allowed",
        _at_the_cap,
    )

    with pytest.raises(CharacterValidationError):
        await installer.install(_detail(), user_id=_PLAYER, locale="zh-Hant")

    assert await repo.list_for_user(_PLAYER) == []


# --------------------------------------------------------------------------- #
# language
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_approved_translation_for_the_players_locale_is_applied() -> None:
    installer, repo, _storage, _catalog = _build(payload=_payload(
        translations=(
            _translation("en", name="Mio"),
            _translation("ja", name="ミオ"),
        ),
    ))

    result = await installer.install(_detail(), user_id=_PLAYER, locale="ja")

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.name == "ミオ"


@pytest.mark.asyncio
async def test_an_unpublished_locale_falls_back_to_english_then_source() -> None:
    installer, repo, _storage, _catalog = _build(payload=_payload(
        translations=(
            _translation("en", name="Mio"),
            _translation("ja", status=TRANSLATION_STATUS_MISSING, name=""),
        ),
    ))

    result = await installer.install(_detail(), user_id=_PLAYER, locale="ja")

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.name == "Mio"


@pytest.mark.asyncio
async def test_a_stale_translation_is_skipped_rather_than_merged() -> None:
    # Stale means the words were approved against an older version of the
    # card; the merge pairs lists by position, so an equal-length drift
    # would land half of one version and half of another.
    installer, repo, _storage, _catalog = _build(payload=_payload(
        translations=(_translation("ja", name="ミオ", stale=True),),
    ))

    result = await installer.install(_detail(), user_id=_PLAYER, locale="ja")

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.name == "美緒"


@pytest.mark.asyncio
async def test_the_source_locale_needs_no_translation_at_all() -> None:
    installer, repo, _storage, _catalog = _build(payload=_payload(
        translations=(_translation("en", name="Mio"),),
    ))

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.name == "美緒"


# --------------------------------------------------------------------------- #
# official art
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_official_art_lands_locally_with_the_likeness_lock_marked(
) -> None:
    installer, repo, storage, catalog = _build(payload=_payload())

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    # Every stage image is downloaded once, in carousel order, and lands in
    # this deployment's own storage — the generation path must never have to
    # reach Cloud for its inputs.
    assert catalog.downloaded == [f"{_IMAGE_BASE}/0", f"{_IMAGE_BASE}/1"]
    keys = [storage.object_key_from_url(url) for url in character.image_urls]
    assert keys == [
        f"characters/{character.id}/official/0.png",
        f"characters/{character.id}/official-reference/1.png",
    ]
    assert all(is_official_art_key(key) for key in keys)
    # Only the index Cloud marked is the lock — EC6 reads exactly these.
    assert [key for key in keys if is_official_reference_key(key)] == [
        f"characters/{character.id}/official-reference/1.png",
    ]
    assert await storage.get_bytes(object_key=keys[0]) == b"\x89PNG-art"


@pytest.mark.asyncio
async def test_art_that_cannot_be_downloaded_does_not_sink_the_install() -> None:
    installer, repo, _storage, _catalog = _build(
        payload=_payload(), catalog=_FakeCatalog(image_bytes=None),
    )

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.image_urls == ()
    assert character.origin_official_card_id == _CARD_ID


@pytest.mark.asyncio
async def test_official_art_stops_at_the_per_character_image_cap() -> None:
    installer, repo, _storage, _catalog = _build(payload=_payload())

    result = await installer.install(
        _detail(image_count=MAX_IMAGES_PER_CHARACTER + 3),
        user_id=_PLAYER,
        locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert len(character.image_urls) == MAX_IMAGES_PER_CHARACTER


# --------------------------------------------------------------------------- #
# whose install this is (TG3)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_installing_tenant_is_forwarded_to_the_payload_read() -> None:
    """The tier fence is checked on the Cloud side, against this id.

    Nothing on this side compares tiers — Core caches one on the operator
    profile and that copy drifts — so the only thing that has to be true
    here is that the tenant actually travels. Dropped silently, every
    tier-locked install would 404 and look like a withdrawn card.
    """
    installer, _repo, _storage, _catalog = _build(payload=_payload())

    await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant", tenant_id="tenant-42",
    )

    payloads = installer._exclusive_payloads  # noqa: SLF001 — pinning the seam
    assert payloads.requested == [(_CARD_ID, "tenant-42")]


@pytest.mark.asyncio
async def test_an_install_with_no_tenant_asks_exactly_what_it_used_to() -> None:
    # Self-host and any account with no projected tenant: the pre-TG
    # request, unchanged, which is what keeps every unfenced card
    # installing exactly as before.
    installer, _repo, _storage, _catalog = _build(payload=_payload())

    await installer.install(_detail(), user_id=_PLAYER, locale="zh-Hant")

    payloads = installer._exclusive_payloads  # noqa: SLF001 — pinning the seam
    assert payloads.requested == [(_CARD_ID, None)]


# --------------------------------------------------------------------------- #
# the two refusals and the two shrugs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_card_behind_an_entitlement_is_refused_not_installed() -> None:
    # v1 publishes no ``required_product``; if one ever appears this build
    # has no port to verify it with, and installing anyway would put a
    # partner's IP behind a licence nobody checked.
    installer, repo, _storage, _catalog = _build(
        payload=_payload(required_product="ip-premium"),
    )

    with pytest.raises(ExclusiveCardEntitlementRequiredError):
        await installer.install(_detail(), user_id=_PLAYER, locale="zh-Hant")

    assert await repo.list_for_user(_PLAYER) == []


@pytest.mark.asyncio
async def test_a_card_with_no_exclusive_voice_yet_still_installs() -> None:
    # A voice published after its card is an ordinary order for two admin
    # actions, not an outage.
    installer, repo, _storage, _catalog = _build(
        payload=_payload(),
        voices=_FakeVoiceCatalog([TTSVoice(id="generic", label="Generic")]),
    )

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.voice_profile is None
    assert character.origin_official_card_id == _CARD_ID


@pytest.mark.asyncio
async def test_a_voice_catalog_outage_still_installs() -> None:
    installer, repo, _storage, _catalog = _build(
        payload=_payload(), voices=_FakeVoiceCatalog(fails=True),
    )

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.voice_profile is None


@pytest.mark.asyncio
async def test_another_cards_exclusive_voice_is_never_borrowed() -> None:
    """Two independent reasons this cannot happen, and this pins the local one.

    The catalog already filters by the declared origin, so an unrelated
    partner's voice should never reach this code at all — which is exactly
    why the double here is told to ignore that filter. A voice bound to
    somebody else's card must be refused on the strength of the id match,
    not on the strength of the service having been well behaved.
    """
    installer, repo, _storage, _catalog = _build(
        payload=_payload(),
        voices=_FakeVoiceCatalog(
            [TTSVoice(id="other-voice", label="他社", exclusive_card_id="other")],
            unfiltered=True,
        ),
    )

    result = await installer.install(
        _detail(), user_id=_PLAYER, locale="zh-Hant",
    )

    character = await repo.get(result.character.id)
    assert character is not None
    assert character.voice_profile is None
