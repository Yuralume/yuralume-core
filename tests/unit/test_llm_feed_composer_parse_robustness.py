"""Parse-robustness for the LLM feed composer.

The composer asks the model for a JSON object (``content_text`` +
``image_prompt`` [+ ``media_kind`` / ``video_prompt``]). Real models
occasionally emit *structurally broken* JSON — a stray un-keyed element,
or a response cut off mid-object by a ``max_tokens`` ceiling. Before the
hardening these fell through to a fallback that published
``candidate[:280]`` verbatim, so the raw ``{"content_text": "..."``
envelope leaked straight onto the player-facing feed.

These tests pin the recovery contract: salvage the caption when it's
intact, drop the post when only unparseable JSON remains, and keep
publishing genuine wrapper-less prose.
"""

from __future__ import annotations

import json

import pytest

from kokoro_link.contracts.feed import FeedComposerInput
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.feed.llm_composer import LLMFeedComposer


class _StaticActiveLLM:
    """Resolver stub: returns a model that echoes a fixed blob."""

    def __init__(self, payload: str, *, is_fake: bool = False) -> None:
        self._payload = payload
        self._fake = is_fake

    async def resolve(self, feature_key=None, *, character=None):
        return _EchoModel(self._payload)

    async def resolve_model_id(self, feature_key=None, *, character=None):
        return None

    async def is_fake(self, feature_key=None, *, character=None):
        return self._fake


class _EchoModel:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    async def generate(self, prompt, *, model=None, character=None):
        return self._payload


def _character() -> Character:
    return Character(
        id="c1", name="Y", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
        ),
    )


def _input(*, image_required: bool = True) -> FeedComposerInput:
    return FeedComposerInput(
        character=_character(),
        kind=FeedKind.from_string("daily"),
        source=FeedSource(kind="manual", ref_id=None),
        hint="夏夜祭",
        image_required=image_required,
    )


# The real report: model broke JSON by emitting an un-keyed hashtag
# element, then got truncated mid ``image_prompt`` by max_tokens (ends on
# "gold", no closing quote/brace).
_REPORTED_TRUNCATED = (
    '{"content_text":"剛剛的夜空，差一點就被我弄哭了嗚～明明想把最漂亮的星星送給'
    "大家，結果一緊張，光就亂跑了。可是有人叫我的名字、有人陪我一起守住願望，那一"
    '瞬間我突然不怕了。原來不是我一個人也可以把星空點亮耶✨今晚好想再多相信一點點。"'
    ',"#夏夜祭 #願望會發光","image_prompt":"anthropomorphic fox girl, solo, '
    "petite, shrine maiden, miko outfit, red hakama, white kimono top, "
    "fox ears, fluffy tail, gold"
)


@pytest.mark.asyncio
async def test_reported_truncated_payload_salvages_caption_only() -> None:
    composer = LLMFeedComposer(provider=_StaticActiveLLM(_REPORTED_TRUNCATED))

    out = await composer.compose(_input())

    # Caption recovered intact...
    assert out.content_text.startswith("剛剛的夜空")
    assert out.content_text.endswith("今晚好想再多相信一點點。")
    # ...and none of the JSON envelope / other-field noise leaked through.
    assert "content_text" not in out.content_text
    assert "image_prompt" not in out.content_text
    assert "{" not in out.content_text
    # The image_prompt tail was truncated (no closing quote), so it can't
    # be trusted — degrade to a text-only post.
    assert out.image_prompt == ""


@pytest.mark.asyncio
async def test_rogue_element_but_complete_image_prompt_keeps_both() -> None:
    """Same un-keyed-element break, but this time nothing is truncated:
    the caption and a fully quote-closed image_prompt both survive."""
    payload = (
        '{"content_text":"今晚的祭典好熱鬧！","#夏夜祭",'
        '"image_prompt":"1girl, yukata, festival, night"}'
    )
    composer = LLMFeedComposer(provider=_StaticActiveLLM(payload))

    out = await composer.compose(_input())

    assert out.content_text == "今晚的祭典好熱鬧！"
    assert out.image_prompt.startswith("1girl")
    assert "content_text" not in out.content_text


@pytest.mark.asyncio
async def test_unsalvageable_schema_leak_is_dropped() -> None:
    """Truncated before ``content_text`` even closes: there's no clean
    caption to recover and the text is obviously a JSON envelope, so the
    post is skipped (empty body) rather than shipping raw braces."""
    payload = '{"content_text":"這句話還沒講完就被切'
    composer = LLMFeedComposer(provider=_StaticActiveLLM(payload))

    out = await composer.compose(_input())

    assert out.content_text == ""


@pytest.mark.asyncio
async def test_plain_prose_without_wrapper_still_published() -> None:
    """Regression guard for the documented fallback: a model that drops
    the JSON wrapper and writes a plain paragraph must still be published
    verbatim — the leak guard only fires on JSON-envelope text."""
    payload = "今天心情很好，想跟大家分享一下傍晚的天空。"
    composer = LLMFeedComposer(provider=_StaticActiveLLM(payload))

    out = await composer.compose(_input())

    assert out.content_text == payload


@pytest.mark.asyncio
async def test_salvaged_caption_respects_body_cap() -> None:
    """A recovered caption is still clamped to the published-body cap."""
    long_caption = "字" * 400
    payload = f'{{"content_text":"{long_caption}","#tag",'
    composer = LLMFeedComposer(provider=_StaticActiveLLM(payload))

    out = await composer.compose(_input())

    assert out.content_text == "字" * 280


@pytest.mark.asyncio
async def test_wellformed_json_unaffected() -> None:
    """The happy path is untouched by the salvage additions."""
    payload = json.dumps({
        "content_text": "傍晚的風好舒服。",
        "image_prompt": "1girl, sunset, breeze",
    })
    composer = LLMFeedComposer(provider=_StaticActiveLLM(payload))

    out = await composer.compose(_input())

    assert out.content_text == "傍晚的風好舒服。"
    assert out.image_prompt.startswith("1girl")
