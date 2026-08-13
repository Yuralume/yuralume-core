"""EC5-C: ``GET /v1/voices`` origin attribution for the hosted TTS catalog.

``CloudGatewayTTSAdapter.synthesize`` already sends
``X-Yuralume-Character-Origin`` for a managed character (EC7). This pins the
same behaviour for ``list_voices`` — the call the ``/tts/assets`` route makes
to build the picker — which had no character context at all before EC5-C:

* No ``character_id`` → no origin header. The cloud TTS service's own default
  (EC5-A) then excludes every cloud-exclusive voice, matching this ticket's
  "excluded from the listing by default" requirement without Core needing to
  know which voices are exclusive.
* A resolvable, managed ``character_id`` → the origin header is sent, so the
  cloud TTS service also surfaces that character's own bound exclusive voice.
* An unresolvable ``character_id`` (repository miss) → same as no id: no
  header, no error. Ownership scoping against the *caller* happens one layer
  up, in the route (see ``test_tts_catalog_route.py``); this adapter only
  needs the id to exist.

EC5-D adds the second way in, for the caller that knows the card but has no
character to derive it from — the cloud-exclusive install, which is choosing
the voice the character will be *born* with. That path passes
``character_origin`` outright, and the binding it matches on has to survive
the wire, so the parse of ``exclusive_card_id`` is pinned here too.
"""

from __future__ import annotations

import httpx
import pytest

from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudResourceContext,
)
from kokoro_link.contracts.tts_catalog import TTSVoice
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.tts.cloud_gateway import CloudGatewayTTSAdapter


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
        )


class _IdentityResolver:
    def __init__(self, *, character_origin: str | None = None) -> None:
        self._character_origin = character_origin
        self.resolved_for: list[str] = []

    async def resolve_context(
        self, context: CloudResourceContext,
    ) -> CloudGatewayIdentity:
        assert context.character is not None
        self.resolved_for.append(context.character.id)
        return CloudGatewayIdentity(
            operator_id=context.operator_id,
            account_id="acct_1",
            tenant_id="tenant_1",
            character_ref="chr_abc",
            character_origin=self._character_origin,
        )


def _character(*, user_id: str = "cloud:acct_1") -> Character:
    return Character.create(
        name="Kokoro",
        summary="A helpful companion",
        user_id=user_id,
        personality=[],
        interests=[],
        speaking_style="gentle",
        boundaries=[],
        appearance="green hair",
        state=CharacterState(
            emotion="calm", affection=50, fatigue=0, trust=50, energy=80,
        ),
    )


def _voices_handler(seen: dict[str, object], *, voices: list[dict] | None = None):
    body = voices if voices is not None else [
        {"id": "marin", "label": "Marin", "is_complete": True},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"voices": body})

    return handler


@pytest.mark.asyncio
async def test_list_voices_sends_no_origin_header_without_character_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(_voices_handler(seen), **kwargs),
    )
    resolver = _IdentityResolver(character_origin="official-yumi")
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        character_repository=InMemoryCharacterRepository(),
        identity_resolver=resolver,
    )

    voices = await adapter.list_voices()

    assert [v.id for v in voices] == ["marin"]
    assert "x-yuralume-character-origin" not in seen["headers"]
    assert resolver.resolved_for == []


@pytest.mark.asyncio
async def test_list_voices_sends_origin_header_for_a_resolvable_managed_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(_voices_handler(seen), **kwargs),
    )
    repo = InMemoryCharacterRepository()
    character = _character()
    await repo.save(character)
    resolver = _IdentityResolver(character_origin="official-yumi")
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        character_repository=repo,
        identity_resolver=resolver,
    )

    voices = await adapter.list_voices(character_id=character.id)

    assert [v.id for v in voices] == ["marin"]
    assert seen["headers"]["x-yuralume-character-origin"] == "official-yumi"
    assert resolver.resolved_for == [character.id]


@pytest.mark.asyncio
async def test_list_voices_ignores_unresolvable_character_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A character id the repository has never heard of degrades to the
    same "no context" request as omitting the parameter — no crash, no
    header, no identity resolution attempted."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(_voices_handler(seen), **kwargs),
    )
    resolver = _IdentityResolver(character_origin="official-yumi")
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        character_repository=InMemoryCharacterRepository(),
        identity_resolver=resolver,
    )

    voices = await adapter.list_voices(character_id="does-not-exist")

    assert [v.id for v in voices] == ["marin"]
    assert "x-yuralume-character-origin" not in seen["headers"]
    assert resolver.resolved_for == []


@pytest.mark.asyncio
async def test_list_voices_declares_an_explicit_origin_without_any_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EC5-D: the cloud-exclusive install knows the card, and nothing else.

    It is choosing the voice a character will be created with, so there is no
    character to resolve an identity from — and requiring one would be
    circular. The card slug is the origin, declared directly, and the
    repository and identity resolver are never touched.
    """
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(_voices_handler(seen), **kwargs),
    )
    resolver = _IdentityResolver(character_origin="official-yumi")
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        character_repository=InMemoryCharacterRepository(),
        identity_resolver=resolver,
    )

    voices = await adapter.list_voices(character_origin="  mio-cafe  ")

    assert [v.id for v in voices] == ["marin"]
    assert seen["headers"]["x-yuralume-character-origin"] == "mio-cafe"
    assert resolver.resolved_for == []


@pytest.mark.asyncio
async def test_a_blank_explicit_origin_is_the_same_as_declaring_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(_voices_handler(seen), **kwargs),
    )
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        character_repository=InMemoryCharacterRepository(),
        identity_resolver=_IdentityResolver(character_origin="official-yumi"),
    )

    await adapter.list_voices(character_origin="   ")

    assert "x-yuralume-character-origin" not in seen["headers"]


@pytest.mark.asyncio
async def test_an_explicit_origin_wins_over_a_characters_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller that names the card outright has said the more specific thing.

    Nothing in the product sends both today; pinning the precedence keeps a
    future caller from getting a quietly different listing than it asked for.
    """
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(_voices_handler(seen), **kwargs),
    )
    repo = InMemoryCharacterRepository()
    character = _character()
    await repo.save(character)
    resolver = _IdentityResolver(character_origin="official-yumi")
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        character_repository=repo,
        identity_resolver=resolver,
    )

    await adapter.list_voices(
        character_id=character.id, character_origin="mio-cafe",
    )

    assert seen["headers"]["x-yuralume-character-origin"] == "mio-cafe"
    assert resolver.resolved_for == []


@pytest.mark.asyncio
async def test_list_voices_reads_the_exclusive_binding_off_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The install matches on this field; a dropped parse is a silent fallback."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _MockAsyncClient(
            _voices_handler(seen, voices=[
                {"id": "marin", "label": "Marin", "is_complete": True},
                {
                    "id": "mio-voice",
                    "label": "美緒",
                    "is_complete": True,
                    "exclusive_card_id": "  mio-cafe  ",
                },
            ]),
            **kwargs,
        ),
    )
    adapter = CloudGatewayTTSAdapter(
        base_url="https://gateway.example",
        deployment_token="ykl_deploy",
        character_repository=InMemoryCharacterRepository(),
        identity_resolver=_IdentityResolver(),
    )

    voices = await adapter.list_voices(character_origin="mio-cafe")

    # Absent on an ordinary voice — and on any catalog predating the field.
    assert {v.id: v.exclusive_card_id for v in voices} == {
        "marin": "",
        "mio-voice": "mio-cafe",
    }
