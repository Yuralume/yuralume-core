"""Tests for the LLM feed composer's media_kind / video_prompt path.

The composer can route a post toward video, image, or text-only based
on what the LLM emits. These tests pin the parse path so a regression
can't silently downgrade every post to text-only (or, worse, smuggle
in a video_prompt the deployment can't render)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.contracts.feed import FeedComposerInput
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.infrastructure.feed.llm_composer import LLMFeedComposer


class _StaticActiveLLM:
    """Resolver stub: returns a model that echoes fixed answers.

    ``payload`` may be one blob (every call gets it) or a list, which is
    consumed in order — the composer makes a second call of its own when
    a video pick arrives without the still it has to start on, and the
    two answers have different shapes."""

    def __init__(
        self, payload: str | list[str], *, is_fake: bool = False,
    ) -> None:
        self._payload = payload
        self._fake = is_fake
        self.captured_prompt: str | None = None
        self.captured_prompts: list[str] = []

    def _next_payload(self) -> str:
        if isinstance(self._payload, list):
            if not self._payload:
                return ""
            return self._payload.pop(0)
        return self._payload

    async def resolve(self, feature_key=None, *, character=None):
        return _EchoModel(self)

    async def resolve_model_id(self, feature_key=None, *, character=None):
        return None

    async def is_fake(self, feature_key=None, *, character=None):
        return self._fake


class _EchoModel:
    def __init__(self, owner: _StaticActiveLLM) -> None:
        self._owner = owner

    async def generate(self, prompt, *, model=None, character=None):
        self._owner.captured_prompt = prompt
        self._owner.captured_prompts.append(prompt)
        return self._owner._next_payload()


def _character() -> Character:
    return Character(
        id="c1", name="Y", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
        ),
    )


def _input() -> FeedComposerInput:
    return FeedComposerInput(
        character=_character(),
        kind=FeedKind.from_string("daily"),
        source=FeedSource(kind="manual", ref_id=None),
        hint="無聊地滑手機",
    )


@pytest.mark.asyncio
async def test_video_enabled_parses_video_pick() -> None:
    payload = json.dumps({
        "content_text": "嘖，誰要回啊。",
        "media_kind": "video",
        "image_prompt": "1girl, holding phone",
        "video_prompt": "Anime style, 5s clip, a girl on bed scrolling phone",
    })
    composer = LLMFeedComposer(
        provider=_StaticActiveLLM(payload), video_enabled=True,
    )
    out = await composer.compose(_input())
    assert out.media_kind == "video"
    assert "scrolling phone" in out.video_prompt
    assert out.image_prompt.startswith("1girl")


@pytest.mark.asyncio
async def test_video_disabled_ignores_media_kind() -> None:
    """Even if the model emits media_kind, a deployment without video
    wiring must demote the post to the image path so video_prompt
    isn't smuggled through to a non-existent renderer."""
    payload = json.dumps({
        "content_text": "嘖。",
        "media_kind": "video",
        "image_prompt": "1girl, holding phone",
        "video_prompt": "shouldn't reach the service",
    })
    composer = LLMFeedComposer(
        provider=_StaticActiveLLM(payload), video_enabled=False,
    )
    out = await composer.compose(_input())
    assert out.media_kind == "image"
    assert out.video_prompt == ""
    assert out.image_prompt.startswith("1girl")


@pytest.mark.asyncio
async def test_video_pick_with_empty_prompt_demotes_to_image() -> None:
    """Model picked video but forgot the prompt — fall back to image
    instead of skipping the visual entirely."""
    payload = json.dumps({
        "content_text": "嘖。",
        "media_kind": "video",
        "image_prompt": "1girl, holding phone",
        "video_prompt": "",
    })
    composer = LLMFeedComposer(
        provider=_StaticActiveLLM(payload), video_enabled=True,
    )
    out = await composer.compose(_input())
    assert out.media_kind == "image"
    assert out.video_prompt == ""
    assert out.image_prompt.startswith("1girl")


@pytest.mark.asyncio
async def test_video_pick_without_a_still_asks_for_the_first_frame() -> None:
    """The clip has to start on a picture, and only ``image_prompt``
    describes one.

    Leaving it empty used to hand the *video* prompt to the image model
    as the opening frame. That prompt is mandated to read
    "A → then B → finally C", the image model drew all three beats at
    once as stacked panels, and the storyboard step pinned that layout as
    a consistency anchor — one still turned the whole clip into a
    three-tier contact sheet."""
    composer_answer = json.dumps({
        "content_text": "寫著寫著就發起呆了。",
        "media_kind": "video",
        "image_prompt": "",
        "video_prompt": (
            "Anime style, 5s clip, a girl writes in her notebook, then the "
            "pen slows, finally she looks toward the window."
        ),
    })
    provider = _StaticActiveLLM([
        composer_answer, "1girl, solo, sitting at desk, holding pen, night",
    ])
    composer = LLMFeedComposer(provider=provider, video_enabled=True)

    out = await composer.compose(_input())

    assert out.media_kind == "video"
    assert out.image_prompt == "1girl, solo, sitting at desk, holding pen, night"
    assert "looks toward the window" in out.video_prompt

    assert len(provider.captured_prompts) == 2
    second = provider.captured_prompts[1]
    assert "the pen slows" in second, "the beats it must collapse"
    assert "第一幀" in second
    assert "split screen" in second


@pytest.mark.asyncio
async def test_first_frame_pass_that_answers_with_junk_leaves_it_empty() -> None:
    """Fail closed: no still means the video branch posts text rather
    than animating something nobody described."""
    composer_answer = json.dumps({
        "content_text": "嘖。",
        "media_kind": "video",
        "image_prompt": "",
        "video_prompt": "Anime style, 5s clip, she turns then smiles.",
    })
    provider = _StaticActiveLLM([composer_answer, "   "])
    composer = LLMFeedComposer(provider=provider, video_enabled=True)

    out = await composer.compose(_input())

    assert out.media_kind == "video"
    assert out.image_prompt == ""
    assert out.video_prompt


@pytest.mark.asyncio
async def test_first_frame_pass_unwraps_a_json_envelope() -> None:
    """Same model, same habit of wrapping answers — an envelope rendered
    verbatim would put JSON keys into the picture."""
    composer_answer = json.dumps({
        "content_text": "嘖。",
        "media_kind": "video",
        "image_prompt": "",
        "video_prompt": "Anime style, 5s clip, she turns then smiles.",
    })
    provider = _StaticActiveLLM([
        composer_answer,
        '```json\n{"image_prompt": "1girl, solo, smiling, window light"}\n```',
    ])
    composer = LLMFeedComposer(provider=provider, video_enabled=True)

    out = await composer.compose(_input())

    assert out.image_prompt == "1girl, solo, smiling, window light"


@pytest.mark.asyncio
async def test_image_post_without_an_image_prompt_makes_no_second_call() -> None:
    """An image post with no ``image_prompt`` is the composer saying "no
    picture" — a legitimate text-only post, not a gap to fill."""
    provider = _StaticActiveLLM(json.dumps({
        "content_text": "只想寫幾個字。",
        "media_kind": "image",
        "image_prompt": "",
        "video_prompt": "",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=True)

    out = await composer.compose(_input())

    assert out.image_prompt == ""
    assert len(provider.captured_prompts) == 1


@pytest.mark.asyncio
async def test_composer_prompt_requires_a_still_for_every_video_pick() -> None:
    provider = _StaticActiveLLM(json.dumps({
        "content_text": "嘖。", "image_prompt": "1girl", "media_kind": "image",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=True)

    await composer.compose(_input())

    prompt = provider.captured_prompt or ""
    assert "image_prompt 是**必填**的" in prompt
    assert "這張圖就是影片的第一幀" in prompt
    assert "split screen" in prompt


@pytest.mark.asyncio
async def test_media_kind_none_clears_prompts() -> None:
    payload = json.dumps({
        "content_text": "今天就只想躺著想事情。",
        "media_kind": "none",
        "image_prompt": "ignored",
        "video_prompt": "ignored",
    })
    composer = LLMFeedComposer(
        provider=_StaticActiveLLM(payload), video_enabled=True,
    )
    out = await composer.compose(_input())
    assert out.media_kind == "none"
    assert out.image_prompt == ""
    assert out.video_prompt == ""
    assert out.content_text.startswith("今天")


@pytest.mark.asyncio
async def test_legacy_payload_without_media_kind_treated_as_image() -> None:
    payload = json.dumps({
        "content_text": "已習慣只發圖。",
        "image_prompt": "1girl, smile",
    })
    composer = LLMFeedComposer(
        provider=_StaticActiveLLM(payload), video_enabled=True,
    )
    out = await composer.compose(_input())
    assert out.media_kind == "image"
    assert out.image_prompt.startswith("1girl")
    assert out.video_prompt == ""


@pytest.mark.asyncio
async def test_prompt_includes_role_knowledge_boundary() -> None:
    provider = _StaticActiveLLM(json.dumps({
        "content_text": "我看不太懂，但大家好像都在討論。",
        "image_prompt": "",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=False)

    await composer.compose(_input())

    prompt = provider.captured_prompt or ""
    assert "認知範圍與誠實表達" in prompt
    assert "不要假裝專家" in prompt
    assert "依角色設定" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_feed_player_knowledge_boundary() -> None:
    """KB9: a post can name someone/something the player hasn't heard of
    (an arc snippet, a memory from a beat he wasn't in) — pin the
    first-time-sharing rider, not just role_boundary's separate concern."""
    from kokoro_link.infrastructure.prompt.player_knowledge_lines import (
        render_feed_post_knowledge_line,
    )

    provider = _StaticActiveLLM(json.dumps({
        "content_text": "今天發生了一件有趣的事。",
        "image_prompt": "",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=False)

    await composer.compose(_input())

    prompt = provider.captured_prompt or ""
    assert render_feed_post_knowledge_line() in prompt


@pytest.mark.asyncio
async def test_prompt_includes_world_event_locale_and_user_location() -> None:
    provider = _StaticActiveLLM(json.dumps({
        "content_text": "我看到這件事了，先觀望一下。",
        "image_prompt": "",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=False)
    payload = replace(
        _input(),
        context_snippets=("來源地區：zh-TW",),
        operator_location_context="使用者所在地：San Francisco / US",
    )

    await composer.compose(payload)

    prompt = provider.captured_prompt or ""
    assert "來源地區：zh-TW" in prompt
    assert "使用者所在地：San Francisco / US" in prompt


@pytest.mark.asyncio
async def test_prompt_weather_carries_freshness_authority() -> None:
    """A LumeGram post (text + image) must follow the *current* weather
    fact, not the rainy weather still implied by recent snippets / memory.

    This is the feed-side fix for "連 LumeGram 的圖片都還在下雨天": the
    composer fetches fresh weather, but without an authority directive the
    model lets the rainy context_snippets drive both the caption and the
    image_prompt."""
    provider = _StaticActiveLLM(json.dumps({
        "content_text": "今天天氣真好。",
        "image_prompt": "1girl, sunny day",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=False)
    payload = replace(
        _input(),
        weather_context="台北目前天氣：晴朗，氣溫 28°C",
        context_snippets=("昨天還在下大雨，記得帶傘",),
    )

    await composer.compose(payload)

    prompt = provider.captured_prompt or ""
    assert "此刻真實世界天氣：" in prompt
    assert "以此刻天氣事實為準" in prompt


@pytest.mark.asyncio
async def test_prompt_without_weather_omits_freshness_authority() -> None:
    provider = _StaticActiveLLM(json.dumps({
        "content_text": "隨手記一下。", "image_prompt": "",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=False)

    await composer.compose(_input())

    prompt = provider.captured_prompt or ""
    assert "以此刻天氣事實為準" not in prompt


@pytest.mark.asyncio
async def test_prompt_includes_operator_local_current_time() -> None:
    provider = _StaticActiveLLM(json.dumps({
        "content_text": "早上的空氣還有點涼。",
        "image_prompt": "",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=False)
    payload = replace(
        _input(),
        now=datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc),
        local_tz=ZoneInfo("Asia/Taipei"),
    )

    await composer.compose(payload)

    prompt = provider.captured_prompt or ""
    assert "現在時間：2026-06-20 07:30" in prompt
    assert "清晨" in prompt


@pytest.mark.asyncio
async def test_prompt_includes_recent_media_cadence() -> None:
    provider = _StaticActiveLLM(json.dumps({
        "content_text": "新的貼文。",
        "media_kind": "none",
        "image_prompt": "",
        "video_prompt": "",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=True)
    payload = replace(
        _input(),
        recent_media_kinds=("image", "none", "image", "video"),
    )

    await composer.compose(payload)

    prompt = provider.captured_prompt or ""
    assert "近期貼文媒體節奏（由新到舊）" in prompt
    assert "圖片、純文字、圖片、影片" in prompt
    assert "最近連續 3 篇沒有影片" in prompt


@pytest.mark.asyncio
async def test_prompt_marks_empty_recent_media_history() -> None:
    provider = _StaticActiveLLM(json.dumps({
        "content_text": "第一篇。",
        "media_kind": "none",
        "image_prompt": "",
        "video_prompt": "",
    }))
    composer = LLMFeedComposer(provider=provider, video_enabled=True)

    await composer.compose(_input())

    prompt = provider.captured_prompt or ""
    assert "近期貼文媒體節奏：尚無貼文紀錄" in prompt
