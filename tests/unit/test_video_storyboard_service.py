"""Unit tests for the ``video_storyboard`` service (CV3 / CV3-B).

Three things are load-bearing and pinned here:

* **Fail-open.** Every gate on the way to the model — no route, a
  text-only model, an unusable frame, a raising call, an answer that
  will not parse — has to land on the composer's blind ``video_prompt``,
  never on an exception and never on an empty prompt when a fallback
  exists. A storyboard is an upgrade to a post that was going to be
  published anyway.
* **The frame is the first frame.** The rendered image must reach the
  model as an image attachment, and the instruction must tell it the clip
  starts on that exact frame rather than treating it as a style
  reference.
* **Structured, and engine-neutral** (D12). What leaves this service is a
  serialised neutral storyboard carrying picture *and* audio intent — but
  never a line of any engine's own prompt format, and never so brittle
  that a deviating answer costs the post.

The shape and its parser have their own module,
``test_video_storyboard_shape``; what is pinned here is how the service
*chooses between* structure, prose and the blind fallback.
"""

from __future__ import annotations

import json

import pytest

from kokoro_link.application.services.feature_keys import (
    FEATURE_GROUP_MEMBERS,
    FEATURE_GROUP_MULTIMODAL_PERCEPTION,
    FEATURE_LABELS,
    FEATURE_VIDEO_STORYBOARD,
    GLOBAL_FEATURE_KEYS,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.video_storyboard_service import (
    MAX_STORYBOARD_CHARS,
    REASON_CALL_FAILED,
    REASON_NO_FRAME,
    REASON_NO_ROUTE,
    REASON_NO_VISION,
    REASON_PROMPT_FAILED,
    REASON_UNPARSEABLE,
    SOURCE_FALLBACK,
    SOURCE_LLM,
    VideoStoryboardRequest,
    VideoStoryboardService,
    build_storyboard_prompt,
    parse_storyboard_output,
    resolve_storyboard_output,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

_FRAME_URL = "https://cdn.example.test/feed/c1/frame.png"
_BASE_PROMPT = "Anime style, 5s clip, a girl on the sofa scrolling her phone"


def _storyboard_answer(**overrides) -> str:
    """A well-formed neutral storyboard, as the model would return it."""
    payload = {
        "shots": [
            {
                "visual": "A young woman sits by a rain-streaked window.",
                "style": "2D-animated, cinematic",
                "consistency_anchors": ["black hair", "grey hoodie"],
                "camera": {
                    "motion_type": "Push In",
                    "amplitude": "small",
                    "speed": "slow",
                },
            },
        ],
        "overall_soundscape": "Steady rain against the glass.",
        "non_diegetic_music": "N/A",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class _FakeVisionModel:
    supports_vision = True
    prefers_public_image_urls = False

    def __init__(self, payload: str | Exception) -> None:
        self._payload = payload
        self.calls: list[dict] = []

    async def generate(self, prompt, *, image_urls=None, model=None):
        self.calls.append(
            {"prompt": prompt, "image_urls": image_urls, "model": model},
        )
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeTextOnlyModel(_FakeVisionModel):
    supports_vision = False


class _FakeProvider:
    """Stands in for ``ActiveLLMProviderPort`` behind ``ModelResolver``."""

    def __init__(self, model, *, is_fake: bool = False, model_id="gpt-vision") -> None:
        self.model = model
        self._is_fake = is_fake
        self._model_id = model_id
        self.resolved_feature_keys: list[str | None] = []

    async def resolve(self, feature_key=None, **_kwargs):
        self.resolved_feature_keys.append(feature_key)
        return self.model

    async def resolve_model_id(self, feature_key=None, **_kwargs):
        return self._model_id

    async def is_fake(self, feature_key=None, **_kwargs):
        return self._is_fake


def _character() -> Character:
    return Character(
        id="c1",
        name="小夜",
        summary="喜歡窩在咖啡廳的插畫家",
        personality=["安靜", "觀察力強"],
        interests=[],
        speaking_style="",
        boundaries=[],
        appearance="黑色長髮、灰色連帽外套",
        state=CharacterState(
            emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
        ),
    )


def _request(**overrides) -> VideoStoryboardRequest:
    payload = {
        "character": _character(),
        "first_frame_url": _FRAME_URL,
        "post_text": "雨下不停，只好賴在窗邊。",
        "base_video_prompt": _BASE_PROMPT,
    }
    payload.update(overrides)
    return VideoStoryboardRequest(**payload)


def _service(model, *, is_fake: bool = False) -> tuple[VideoStoryboardService, _FakeProvider]:
    provider = _FakeProvider(model, is_fake=is_fake)
    service = VideoStoryboardService(
        ModelResolver(provider=provider, feature_key=FEATURE_VIDEO_STORYBOARD),
    )
    return service, provider


# -- registry ----------------------------------------------------------


def test_feature_key_is_registered_as_a_routable_llm_key() -> None:
    assert FEATURE_VIDEO_STORYBOARD == "video_storyboard"
    assert FEATURE_VIDEO_STORYBOARD in GLOBAL_FEATURE_KEYS
    assert FEATURE_LABELS[FEATURE_VIDEO_STORYBOARD].strip()


def test_feature_key_routes_through_the_multimodal_group() -> None:
    """Vision is a hard requirement, so the key must sit in the group
    operators pin a vision-capable preset to."""
    members = FEATURE_GROUP_MEMBERS[FEATURE_GROUP_MULTIMODAL_PERCEPTION]
    assert FEATURE_VIDEO_STORYBOARD in members


# -- happy path --------------------------------------------------------


@pytest.mark.asyncio
async def test_generates_a_structured_storyboard_from_the_rendered_frame() -> None:
    model = _FakeVisionModel(_storyboard_answer())
    service, provider = _service(model)

    result = await service.generate(_request())

    assert result.source == SOURCE_LLM
    assert result.used_fallback is False
    assert result.reason == ""
    assert result.is_structured is True
    # The wire form is a JSON string with a top-level ``shots`` array —
    # what the pipeline adapter sniffs for before rendering its own format.
    decoded = json.loads(result.prompt)
    assert decoded["shots"][0]["camera"]["motion_type"] == "Push In"
    assert decoded["overall_soundscape"]
    assert provider.resolved_feature_keys == [FEATURE_VIDEO_STORYBOARD]
    call = model.calls[0]
    assert call["image_urls"] == (_FRAME_URL,)
    assert call["model"] == "gpt-vision"


@pytest.mark.asyncio
async def test_core_never_emits_engine_specific_format(  # D12 red line
) -> None:
    """The anchor line, the three H3 field names and ``[Shot N]`` prefixes
    are the adapter's job. If Core ever starts emitting them, swapping the
    engine stops being a one-adapter change."""
    model = _FakeVisionModel(_storyboard_answer())
    service, _ = _service(model)

    result = await service.generate(_request())

    for marker in (
        "<Picture 1>",
        "[Shot 1]",
        "integrated_multimodal_description",
        "is fully referenced",
    ):
        assert marker not in result.prompt


@pytest.mark.asyncio
async def test_prompt_anchors_the_frame_and_carries_the_inputs() -> None:
    model = _FakeVisionModel(_storyboard_answer())
    service, _ = _service(model)

    await service.generate(_request(context_snippets=("剛結束一場久等的稿件",)))

    prompt = model.calls[0]["prompt"]
    # First frame, not a style reference.
    assert "第一幀" in prompt
    assert "風格參考" in prompt
    # The neutral structure, including the two audio blocks D12 added.
    assert '"shots"' in prompt
    assert "start_at_seconds" in prompt
    assert "consistency_anchors" in prompt
    assert "overall_soundscape" in prompt
    assert "non_diegetic_music" in prompt
    assert "5 秒" in prompt
    # The camera vocabulary is offered, not left to the model's invention.
    assert "Push In" in prompt
    assert "Static Shot" in prompt
    # Dialogue is opt-in and never translated.
    assert "預設不要給" in prompt
    assert "絕對不要翻成英文" in prompt
    # Character consistency + the character's own appearance context.
    assert "小夜" in prompt
    assert "黑色長髮" in prompt
    # Post draft + the composer's blind prompt as directional material.
    assert "雨下不停" in prompt
    assert _BASE_PROMPT in prompt
    assert "剛結束一場久等的稿件" in prompt


def test_prompt_forbids_engine_specific_scaffolding() -> None:
    """Whatever the model writes, it must not pre-render the adapter's
    format — the instruction has to say so explicitly."""
    prompt = build_storyboard_prompt(_request())
    assert "[Shot 1]" in prompt
    assert "<Picture 1>" in prompt
    assert "由下游組裝" in prompt


def test_prompt_refuses_to_anchor_a_collaged_first_frame() -> None:
    """A first frame that arrived as stacked panels is an upstream image
    defect, and the one thing this step must not do is promote it to a
    consistency anchor — that is how one bad still became a five-second
    three-tier contact sheet (job 5b725a0b: "preserving vertically
    stacked three-tier composition")."""
    prompt = build_storyboard_prompt(_request())
    assert "consistency_anchors" in prompt
    assert "拼貼排版" in prompt
    assert "stacked / tier / panel" in prompt


def test_prompt_states_the_budget_it_will_be_held_to() -> None:
    prompt = build_storyboard_prompt(_request())
    assert str(MAX_STORYBOARD_CHARS) in prompt


def test_prompt_without_a_base_video_prompt_omits_the_direction_block() -> None:
    prompt = build_storyboard_prompt(_request(base_video_prompt=""))
    assert "初版方向" not in prompt


def test_prompt_without_post_text_states_the_absence() -> None:
    prompt = build_storyboard_prompt(_request(post_text=""))
    assert "無貼文草稿" in prompt


# -- hosted frame delivery (audit GAP 6) -------------------------------


@pytest.mark.asyncio
async def test_hosted_frame_url_is_promoted_to_a_fetchable_public_url() -> None:
    """The hosted model fetches the frame itself; a relative URL is a 404.

    ``FeedComposerService._store_feed_image`` hands this service the
    ``StoredObject.url`` object storage minted, which in the hosted shape is
    the *relative* ``/v1/public/{key}`` form. The gateway-routed model sets
    ``prefers_public_image_urls`` because it cannot ingest a data URL of a
    multi-MB PNG — so unless that relative ref is promoted against
    ``public_base_url``, the storyboard step silently degrades to the blind
    prompt on every single hosted post.
    """
    key = "feed/c1/frame.png"
    storage = InMemoryObjectStorage(public_base_url="/v1/public")
    stored = await storage.put_bytes(
        object_key=key, content=b"\x89PNG frame", content_type="image/png",
    )
    assert stored.url == f"/v1/public/{key}", "hosted storage shape"

    model = _FakeVisionModel(_storyboard_answer())
    model.prefers_public_image_urls = True
    service = VideoStoryboardService(
        ModelResolver(
            provider=_FakeProvider(model), feature_key=FEATURE_VIDEO_STORYBOARD,
        ),
        object_storage=storage,
        public_base_url="https://api.yuralume.test",
    )

    result = await service.generate(_request(first_frame_url=stored.url))

    assert result.source == SOURCE_LLM
    assert model.calls[0]["image_urls"] == (
        f"https://api.yuralume.test/v1/public/{key}",
    )


@pytest.mark.asyncio
async def test_a_model_that_takes_inline_images_gets_the_frame_inline() -> None:
    """The other half of the same path: a vision model without the public-URL
    preference is handed the bytes, so a deployment whose storage is not
    publicly reachable still storyboards."""
    key = "feed/c1/frame.png"
    storage = InMemoryObjectStorage(public_base_url="/v1/public")
    stored = await storage.put_bytes(
        object_key=key, content=b"\x89PNG frame", content_type="image/png",
    )
    model = _FakeVisionModel(_storyboard_answer())
    service = VideoStoryboardService(
        ModelResolver(
            provider=_FakeProvider(model), feature_key=FEATURE_VIDEO_STORYBOARD,
        ),
        object_storage=storage,
        public_base_url="https://api.yuralume.test",
    )

    await service.generate(_request(first_frame_url=stored.url))

    assert model.calls[0]["image_urls"][0].startswith("data:image/png;base64,")


# -- fail-open gates ---------------------------------------------------


@pytest.mark.asyncio
async def test_no_resolver_falls_back_to_the_composer_prompt() -> None:
    result = await VideoStoryboardService().generate(_request())
    assert result.source == SOURCE_FALLBACK
    assert result.reason == REASON_NO_ROUTE
    assert result.prompt == _BASE_PROMPT


@pytest.mark.asyncio
async def test_fake_backend_falls_back_without_calling_the_model() -> None:
    model = _FakeVisionModel(_storyboard_answer())
    service, _ = _service(model, is_fake=True)

    result = await service.generate(_request())

    assert result.reason == REASON_NO_ROUTE
    assert result.prompt == _BASE_PROMPT
    assert model.calls == []


@pytest.mark.asyncio
async def test_text_only_model_falls_back_without_calling_it() -> None:
    """A storyboard written without seeing the frame is exactly the blind
    prompt we already hold — do not pay for it twice."""
    model = _FakeTextOnlyModel(json.dumps({"storyboard_prompt": "unused"}))
    service, _ = _service(model)

    result = await service.generate(_request())

    assert result.reason == REASON_NO_VISION
    assert result.prompt == _BASE_PROMPT
    assert model.calls == []


@pytest.mark.asyncio
async def test_missing_frame_url_falls_back() -> None:
    model = _FakeVisionModel(_storyboard_answer())
    service, _ = _service(model)

    result = await service.generate(_request(first_frame_url="  "))

    assert result.reason == REASON_NO_FRAME
    assert result.prompt == _BASE_PROMPT
    assert model.calls == []


@pytest.mark.asyncio
async def test_raising_call_falls_back() -> None:
    model = _FakeVisionModel(RuntimeError("upstream 503"))
    service, _ = _service(model)

    result = await service.generate(_request())

    assert result.reason == REASON_CALL_FAILED
    assert result.prompt == _BASE_PROMPT


@pytest.mark.asyncio
async def test_an_unrenderable_instruction_falls_back(monkeypatch) -> None:
    """A stale external prompt pack asking for a variable this release no
    longer supplies must cost the *storyboard*, not the post."""
    model = _FakeVisionModel(_storyboard_answer())
    service, _ = _service(model)
    monkeypatch.setattr(
        "kokoro_link.application.services.video_storyboard_service"
        ".build_storyboard_prompt",
        lambda request: (_ for _ in ()).throw(KeyError("clip_seconds")),
    )

    result = await service.generate(_request())

    assert result.reason == REASON_PROMPT_FAILED
    assert result.prompt == _BASE_PROMPT
    assert model.calls == []


@pytest.mark.asyncio
async def test_unparseable_answer_falls_back() -> None:
    model = _FakeVisionModel("")
    service, _ = _service(model)

    result = await service.generate(_request())

    assert result.reason == REASON_UNPARSEABLE
    assert result.prompt == _BASE_PROMPT


@pytest.mark.asyncio
async def test_fallback_with_no_base_prompt_yields_empty_not_an_error() -> None:
    service, _ = _service(_FakeVisionModel(""), is_fake=True)

    result = await service.generate(_request(base_video_prompt=""))

    assert result.prompt == ""
    assert result.used_fallback is True


# -- structure vs prose vs nothing -------------------------------------


@pytest.mark.asyncio
async def test_a_prose_answer_is_still_used_but_flagged_unstructured() -> None:
    """Prose is a perfectly good I2V prompt — every adapter takes it as
    the body of a single shot. It just means the soundscape and score the
    engine can render were never asked for, so the post is not worth
    losing over it."""
    raw = "Starts exactly on the given frame; slow push-in over 5 seconds."
    service, _ = _service(_FakeVisionModel(raw))

    result = await service.generate(_request())

    assert result.source == SOURCE_LLM
    assert result.is_structured is False
    assert result.prompt == raw


@pytest.mark.asyncio
async def test_a_stale_prompt_pack_still_degrades_to_prose() -> None:
    """An external prompt pack mounted before this release still asks for
    V0's single field. That must read as prose, not as nothing."""
    raw = json.dumps({"storyboard_prompt": "slow dolly-in over five seconds"})
    service, _ = _service(_FakeVisionModel(raw))

    result = await service.generate(_request())

    assert result.source == SOURCE_LLM
    assert result.prompt == "slow dolly-in over five seconds"


@pytest.mark.asyncio
async def test_a_half_serialised_envelope_falls_back_rather_than_shipping() -> None:
    """Forwarding this verbatim would render JSON keys into the clip."""
    service, _ = _service(_FakeVisionModel('{"shots": [{"visual": "cut off'))

    result = await service.generate(_request())

    assert result.source == SOURCE_FALLBACK
    assert result.reason == REASON_UNPARSEABLE
    assert result.prompt == _BASE_PROMPT


def test_resolve_returns_both_halves() -> None:
    text, storyboard = resolve_storyboard_output(
        _storyboard_answer(), clip_seconds=5,
    )
    assert storyboard is not None
    assert json.loads(text)["shots"]


def test_parses_fenced_json() -> None:
    raw = f"```json\n{_storyboard_answer()}\n```"
    assert json.loads(parse_storyboard_output(raw))["shots"]


def test_parses_json_object_wrapped_in_prose() -> None:
    raw = f"Sure!\n{_storyboard_answer()}\nHope that helps."
    assert json.loads(parse_storyboard_output(raw))["shots"]


def test_salvages_a_truncated_storyboard() -> None:
    raw = _storyboard_answer()
    truncated = raw[: raw.index('"overall_soundscape"')]
    assert json.loads(parse_storyboard_output(truncated))["shots"]


def test_salvages_a_truncated_legacy_object() -> None:
    raw = '{"storyboard_prompt": "static locked-off shot", "audio'
    assert parse_storyboard_output(raw) == "static locked-off shot"


def test_accepts_plain_prose_that_dropped_the_wrapper() -> None:
    raw = "Starts exactly on the given frame; slow push-in over 5 seconds."
    assert parse_storyboard_output(raw) == raw


def test_caps_a_runaway_structured_generation() -> None:
    raw = _storyboard_answer(shots=[
        {"visual": "She breathes slowly by the window. " * 200},
    ])
    output = parse_storyboard_output(raw)
    assert len(output) <= MAX_STORYBOARD_CHARS
    # Still valid JSON: a cut string would be read as prose downstream.
    assert json.loads(output)["shots"]


def test_caps_a_runaway_prose_generation() -> None:
    assert len(parse_storyboard_output("x" * (MAX_STORYBOARD_CHARS + 500))) == (
        MAX_STORYBOARD_CHARS
    )
