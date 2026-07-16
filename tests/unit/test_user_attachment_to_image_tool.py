"""User-attached photos must reach the image-generation prompt rewriter.

Regression for the "幫角色換上這件衣服 + 附件照片" flow. Before this
fix the chain dropped the photo at ``_generate_reply_with_tools`` — the
chat LLM saw it but the image tool didn't. Now ``ToolContext`` carries
``user_attachment_urls`` end-to-end so the vision-capable rewriter can
extract outfit / scene cues from the picture itself.

Covers three layers in isolation:

1. ``ComfyImageTool`` forwards ``ctx.user_attachment_urls`` to the
   provider (proves the tool no longer drops them).
2. ``ComfyPortraitGenerator`` forwards them to the rewriter and prefixes
   the structured payload with a "user reference image" marker so the
   LLM knows to read the image.
3. ``LLMPromptRewriter`` passes them as ``image_urls`` to the underlying
   ``ChatModelPort.generate`` so a vision-capable model actually sees
   the bytes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.tool import ToolContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.tools.comfyui.generator import (
    ComfyPortraitGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.prompt_rewriter import (
    LLMPromptRewriter,
)
from kokoro_link.infrastructure.tools.comfyui.tool import ComfyImageTool
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_WORKFLOW_FILE,
    WorkflowBuilder,
)
from tests.unit._image_provider_stub import StaticActiveImageProvider
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage


# --- shared stubs ----------------------------------------------------

class _CaptureProvider:
    """Stand-in :class:`ImageProviderPort` that records what was asked
    without actually running ComfyUI. Only the kwargs we care about
    are inspected; the rest are forwarded to ``record``."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate(
        self,
        *,
        character,
        positive: str,
        aspect: str = "portrait",
        batch: int = 1,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        user_attachment_urls: Sequence[str] = (),
    ) -> list[bytes]:
        self.calls.append({
            "character_id": character.id,
            "positive": positive,
            "aspect": aspect,
            "user_attachment_urls": tuple(user_attachment_urls or ()),
        })
        return [b"\x89PNG\r\n\x1a\n" + b"\x00" * 16]


class _CaptureRewriter:
    """Stand-in :class:`PromptRewriterPort` that records text + images
    handed in."""

    def __init__(self, output: str = "1girl, white blouse, plaid skirt") -> None:
        self.text_calls: list[str] = []
        self.image_url_calls: list[tuple[str, ...]] = []
        self._output = output

    async def rewrite(
        self, text: str, *, character=None, image_urls=(),
    ) -> str:
        del character
        self.text_calls.append(text)
        self.image_url_calls.append(tuple(image_urls or ()))
        return self._output


class _CaptureModel(ChatModelPort):
    """ChatModelPort stub that records what ``image_urls`` it sees so
    we can prove the rewriter forwards them to the underlying LLM."""

    def __init__(self, response: str) -> None:
        self.provider_id = "fake"
        self.supports_vision = True
        self.calls: list[dict] = []
        self._response = response

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.calls.append({
            "prompt": prompt,
            "image_urls": tuple(image_urls or ()),
            "model": model,
        })
        return self._response

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        yield await self.generate(prompt, image_urls=image_urls, model=model)

    async def list_models(self) -> list[str]:
        return ["fake"]


class _FakeComfyClient:
    def __init__(self) -> None:
        self.queued_prompts: list[dict] = []

    async def queue_prompt(self, prompt: dict) -> str:
        self.queued_prompts.append(prompt)
        return "pid-1"

    async def wait_for_completion(self, prompt_id: str) -> dict:
        return {
            "outputs": {
                "9": {
                    "images": [
                        {"filename": "out.png", "subfolder": "", "type": "output"},
                    ],
                },
            },
        }

    async def download_image(
        self, *, filename: str, subfolder: str, folder_type: str,
    ) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _character() -> Character:
    return Character.create(
        name="Yuki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="smiling", affection=50, fatigue=0, trust=50, energy=100,
        ),
        appearance="long black hair, red ribbon, school uniform",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="androgynous teen",
        allowed_tools=["generate_image"],
    )


# --- tests -----------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_forwards_user_attachments_to_provider(
    tmp_path: Path,
) -> None:
    """``ComfyImageTool.invoke`` must propagate ``ctx.user_attachment_urls``
    onto ``provider.generate``. This is the layer the original bug
    sat at — the tool silently dropped attachments."""
    provider = _CaptureProvider()
    tool = ComfyImageTool(
        image_provider=StaticActiveImageProvider(provider),
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )

    ref_url = "data:image/png;base64,AAAA"
    await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "wearing this outfit"},
        user_attachment_urls=(ref_url,),
    ))

    assert provider.calls, "provider.generate was never called"
    assert provider.calls[0]["user_attachment_urls"] == (ref_url,), (
        "tool dropped user attachments before reaching the provider"
    )


@pytest.mark.asyncio
async def test_generator_payload_marks_user_reference_image() -> None:
    """The rewriter payload must contain a "User reference image"
    marker AND the URL list must reach the rewriter — that combo is
    what tells a vision LLM to read the photo for outfit cues."""
    client = _FakeComfyClient()
    rewriter = _CaptureRewriter()
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )

    ref_url = "data:image/png;base64,AAAA"
    await generator.generate(
        character=_character(),
        positive="幫我換上這件衣服",
        user_attachment_urls=(ref_url,),
    )

    assert rewriter.image_url_calls == [(ref_url,)], (
        "generator failed to forward user attachments to rewriter"
    )
    payload = rewriter.text_calls[0]
    assert "User reference image" in payload, (
        "payload missing the marker that tells the LLM to read the image"
    )
    # The marker must be directive about wardrobe override, otherwise
    # the rewriter LLM keeps appearance's outfit AND adds image's
    # outfit and produces a mash-up. Verify the override keywords.
    assert "WARDROBE OVERRIDE" in payload
    assert "DROP" in payload  # tells LLM to drop appearance wardrobe
    assert "Character gender identity: 非二元" in payload
    assert "Visual gender presentation: androgynous teen" in payload


@pytest.mark.asyncio
async def test_rewriter_system_prompt_has_wardrobe_override_rule() -> None:
    """The system prompt must list 'User reference image attached' as a
    first-class conflict-resolution override that DROPS the appearance
    wardrobe. Without this rule the LLM keeps the original outfit
    alongside the image's outfit (mash-up bug)."""
    from kokoro_link.infrastructure.tools.comfyui.prompt_rewriter import (
        _SYSTEM_PROMPT,
    )
    # Override clause exists in the conflict resolution section.
    assert "User reference image attached" in _SYSTEM_PROMPT
    assert "visual gender presentation" in _SYSTEM_PROMPT
    # And it's explicit about dropping wardrobe.
    assert "SOURCE OF TRUTH" in _SYSTEM_PROMPT
    # No-mash-up guidance is present so the LLM doesn't carry both
    # the original and new outfit.
    assert "do NOT carry both" in _SYSTEM_PROMPT or "do not carry both" in _SYSTEM_PROMPT.lower()


@pytest.mark.asyncio
async def test_generator_no_attachments_omits_marker_and_passes_empty_urls() -> None:
    """When no attachments are present, the rewriter must NOT see a
    misleading marker and must receive an empty tuple — otherwise we'd
    confuse the LLM into hallucinating a reference."""
    client = _FakeComfyClient()
    rewriter = _CaptureRewriter()
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )

    await generator.generate(
        character=_character(), positive="cafe scene",
    )

    assert rewriter.image_url_calls == [()]
    assert "User reference image" not in rewriter.text_calls[0]


@pytest.mark.asyncio
async def test_llm_rewriter_forwards_image_urls_to_underlying_model() -> None:
    """``LLMPromptRewriter`` must hand ``image_urls`` straight to the
    underlying ``ChatModelPort.generate`` so a vision model actually
    sees the user's photo."""
    model = _CaptureModel("1girl, white blouse, plaid skirt, school")
    rewriter = LLMPromptRewriter(model=model)

    ref_url = "data:image/png;base64,AAAA"
    out = await rewriter.rewrite(
        "Character appearance: long black hair\nScene: wearing this outfit",
        image_urls=(ref_url,),
    )

    assert out
    assert len(model.calls) == 1
    assert model.calls[0]["image_urls"] == (ref_url,), (
        "rewriter dropped image_urls before reaching the model"
    )


@pytest.mark.asyncio
async def test_llm_rewriter_logs_input_payload_and_output(
    caplog,
) -> None:
    """Operator-visible INPUT / OUTPUT log lines must surround every
    rewriter call. INPUT carries the structured payload + a compact
    image descriptor (data: URLs collapsed to byte counts so a 5 MB
    photo doesn't drown the log). OUTPUT shows raw vs cleaned so we
    can tell whether the LLM or the cleanup pass mangled the tags."""
    model = _CaptureModel("1girl, white blouse, plaid skirt")
    rewriter = LLMPromptRewriter(model=model)

    long_b64 = "A" * 600
    ref_url = f"data:image/png;base64,{long_b64}"
    payload = (
        "Character appearance: long black hair\n"
        "Scene: 換上這件\n"
        "User reference image(s) attached this turn: 1"
    )
    with caplog.at_level(
        "INFO",
        logger="kokoro_link.infrastructure.tools.comfyui.prompt_rewriter",
    ):
        await rewriter.rewrite(payload, image_urls=(ref_url,))

    text = caplog.text
    assert "prompt rewriter INPUT" in text
    assert "image_count=1" in text
    # Full base64 must NOT leak into the log.
    assert long_b64 not in text
    # Collapsed descriptor must be there.
    assert "data:image/png;base64,<" in text and "bytes>" in text
    # Payload visible verbatim.
    assert "Character appearance: long black hair" in text
    assert "換上這件" in text
    # OUTPUT line with raw + cleaned.
    assert "prompt rewriter OUTPUT" in text
    assert "cleaned=" in text
    assert "raw=" in text


@pytest.mark.asyncio
async def test_llm_rewriter_omits_image_urls_kwarg_when_empty() -> None:
    """Legacy fake/mock ChatModel adapters whose ``generate`` doesn't
    accept ``image_urls`` must still work on the no-image path. We
    achieve that by only forwarding the kwarg when non-empty."""

    class _NoKwargModel:
        provider_id = "fake"
        supports_vision = False

        def __init__(self) -> None:
            self.received_kwargs: dict | None = None

        async def generate(self, prompt: str) -> str:
            # If the rewriter forwarded image_urls=() to this, the
            # call would TypeError before we get here. Reaching this
            # line proves the kwarg was dropped on the empty path.
            self.received_kwargs = {}
            return "1girl, cafe, indoors"

        async def generate_stream(self, prompt: str):  # pragma: no cover
            yield "x"

        async def list_models(self) -> list[str]:  # pragma: no cover
            return []

    model = _NoKwargModel()
    rewriter = LLMPromptRewriter(model=model)  # type: ignore[arg-type]

    out = await rewriter.rewrite("Scene: cafe")

    assert out
    assert model.received_kwargs == {}
