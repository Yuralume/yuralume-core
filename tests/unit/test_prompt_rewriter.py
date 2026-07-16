"""BDD for the LLM prompt rewriter + its integration with the
ComfyPortraitGenerator.

The rewriter turns a Chinese / natural-language description into
danbooru-style English tags before the generator concatenates the
quality boilerplate and character appearance. Failure paths must
fall back to the raw input — image generation shouldn't die because
the rewriter had a bad day.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.prompt_rewriter import PromptRewriteError
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.tools.comfyui.generator import (
    ComfyPortraitGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.prompt_rewriter import (
    LLMPromptRewriter,
)
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_WORKFLOW_FILE,
    WorkflowBuilder,
)


class _ScriptedModel(ChatModelPort):
    def __init__(
        self,
        response: str,
        *,
        raise_exc: Exception | None = None,
    ) -> None:
        self.provider_id = "fake"
        self._response = response
        self._raise = raise_exc
        self.captured_prompt: str | None = None

    async def generate(self, prompt: str) -> str:
        self.captured_prompt = prompt
        if self._raise is not None:
            raise self._raise
        return self._response

    async def generate_stream(self, prompt: str) -> AsyncIterator[str]:
        yield await self.generate(prompt)


# ---- rewriter unit tests --------------------------------------------

@pytest.mark.asyncio
async def test_rewrite_returns_clean_tag_line() -> None:
    model = _ScriptedModel("1girl, cafe, reading book, soft lighting")
    rewriter = LLMPromptRewriter(model=model)

    result = await rewriter.rewrite("咖啡店裡看書")

    assert result == "1girl, cafe, reading book, soft lighting"
    # Prompt includes the source text so the model has something to
    # translate.
    assert "咖啡店裡看書" in (model.captured_prompt or "")


@pytest.mark.asyncio
async def test_rewrite_strips_code_fences_and_labels() -> None:
    model = _ScriptedModel(
        "```\npositive: 1girl, outdoors, mountain, dynamic pose\n```",
    )
    rewriter = LLMPromptRewriter(model=model)

    result = await rewriter.rewrite("爬山")

    assert result == "1girl, outdoors, mountain, dynamic pose"


@pytest.mark.asyncio
async def test_rewrite_strips_surrounding_quotes() -> None:
    model = _ScriptedModel('"1girl, sitting, indoors"')
    rewriter = LLMPromptRewriter(model=model)

    result = await rewriter.rewrite("坐在室內")

    assert result == "1girl, sitting, indoors"


@pytest.mark.asyncio
async def test_rewrite_takes_only_first_nonempty_line() -> None:
    model = _ScriptedModel(
        "1girl, portrait, window light\n(I also considered adding ...)\n",
    )
    rewriter = LLMPromptRewriter(model=model)

    result = await rewriter.rewrite("窗邊肖像")

    assert result == "1girl, portrait, window light"


@pytest.mark.asyncio
async def test_rewrite_empty_input_returns_empty() -> None:
    model = _ScriptedModel("should not be called")
    rewriter = LLMPromptRewriter(model=model)

    assert await rewriter.rewrite("") == ""
    assert await rewriter.rewrite("   ") == ""
    assert model.captured_prompt is None


@pytest.mark.asyncio
async def test_rewrite_llm_error_becomes_rewrite_error() -> None:
    model = _ScriptedModel("", raise_exc=RuntimeError("boom"))
    rewriter = LLMPromptRewriter(model=model)

    with pytest.raises(PromptRewriteError):
        await rewriter.rewrite("some input")


@pytest.mark.asyncio
async def test_rewrite_empty_output_raises() -> None:
    model = _ScriptedModel("")
    rewriter = LLMPromptRewriter(model=model)

    with pytest.raises(PromptRewriteError):
        await rewriter.rewrite("something")


# ---- generator integration ------------------------------------------

class _FakeClient:
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
            emotion="smiling", affection=0, fatigue=0, trust=0, energy=100,
        ),
        appearance="long black hair, red ribbon",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="androgynous teen",
    )


def _animal_character() -> Character:
    return Character.create(
        name="Mochi", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="relaxed", affection=0, fatigue=0, trust=0, energy=100,
        ),
        appearance="一隻短毛橘貓，四足姿態，圓眼睛，戴著小鈴鐺",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="可愛寵物貓",
        visual_subject_type="animal",
    )


class _Rewriter:
    def __init__(self, output: str, *, raise_exc: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.image_url_calls: list[tuple[str, ...]] = []
        self._output = output
        self._raise = raise_exc

    async def rewrite(
        self, text: str, *, character=None, image_urls=(),
    ) -> str:
        del character  # picker forwards it; this stub doesn't care
        self.calls.append(text)
        self.image_url_calls.append(tuple(image_urls or ()))
        if self._raise is not None:
            raise self._raise
        return self._output


@pytest.mark.asyncio
async def test_generator_uses_rewritten_prompt_in_positive_node() -> None:
    client = _FakeClient()
    rewriter = _Rewriter(
        "1girl, long black hair, red ribbon, smiling, cafe, reading book",
    )
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )

    await generator.generate(
        character=_character(), positive="咖啡店裡看書",
    )

    # Generator now hands a structured payload (appearance + mood +
    # scene) to the rewriter in one shot so identity tags get
    # translated along with the scene.
    assert len(rewriter.calls) == 1
    payload = rewriter.calls[0]
    assert "Character appearance" in payload
    assert "long black hair" in payload  # from character.appearance
    assert "Character gender identity: 非二元" in payload
    assert "Visual gender presentation: androgynous teen" in payload
    assert "smiling" in payload  # from state.emotion
    assert "咖啡店裡看書" in payload  # scene original

    positive_text = client.queued_prompts[0]["6"]["inputs"]["text"]
    # Rewriter output is what flows into the workflow — raw Chinese
    # appearance / scene should NOT appear in the final prompt.
    assert "1girl, long black hair" in positive_text
    assert "cafe, reading book" in positive_text
    assert "咖啡店裡看書" not in positive_text
    # Quality boilerplate still prepended exactly once.
    assert positive_text.startswith("masterpiece")


@pytest.mark.asyncio
async def test_generator_rewritten_prompt_keeps_non_human_animal_tags() -> None:
    client = _FakeClient()
    rewriter = _Rewriter("domestic cat, windowsill, sunlight")
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )

    await generator.generate(
        character=_animal_character(), positive="窗台上的午後陽光",
    )

    payload = rewriter.calls[0]
    assert "Visual subject type: non-human animal." in payload
    assert "Species/body plan: domestic cat." in payload
    assert "Do NOT anthropomorphize" in payload
    assert "可愛寵物貓" in payload

    queued = client.queued_prompts[0]
    positive_text = queued["6"]["inputs"]["text"]
    negative_text = queued["7"]["inputs"]["text"]
    assert "no humans, domestic cat, non-human animal" in positive_text
    assert "domestic cat, windowsill" in positive_text
    assert "human face" in negative_text
    assert "1girl" in negative_text
    assert "anthro" in negative_text


@pytest.mark.asyncio
async def test_generator_falls_back_on_rewriter_error() -> None:
    client = _FakeClient()
    rewriter = _Rewriter("", raise_exc=PromptRewriteError("no model"))
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )

    images = await generator.generate(
        character=_character(), positive="咖啡店裡看書",
    )

    # Generation still succeeded using raw per-field concatenation.
    # That's obviously not ideal when fields are Chinese, but it beats
    # failing the whole turn.
    assert len(images) == 1
    positive_text = client.queued_prompts[0]["6"]["inputs"]["text"]
    assert "咖啡店裡看書" in positive_text
    # Appearance / emotion also flow through in the fallback path.
    assert "long black hair" in positive_text
    assert "非二元" in positive_text
    assert "androgynous teen" in positive_text
    assert "smiling" in positive_text


@pytest.mark.asyncio
async def test_generator_animal_fallback_omits_visual_gender_hint() -> None:
    client = _FakeClient()
    rewriter = _Rewriter("", raise_exc=PromptRewriteError("no model"))
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )

    await generator.generate(
        character=_animal_character(), positive="窗台上的午後陽光",
    )

    positive_text = client.queued_prompts[0]["6"]["inputs"]["text"]
    assert "no humans, domestic cat, non-human animal" in positive_text
    assert "一隻短毛橘貓" in positive_text
    assert "relaxed" in positive_text
    assert "可愛寵物貓" not in positive_text
    assert "非二元" not in positive_text


@pytest.mark.asyncio
async def test_generator_without_rewriter_uses_raw() -> None:
    client = _FakeClient()
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
    )

    await generator.generate(
        character=_character(), positive="1girl, cafe",
    )

    positive_text = client.queued_prompts[0]["6"]["inputs"]["text"]
    assert "1girl, cafe" in positive_text


@pytest.mark.asyncio
async def test_generator_payload_includes_current_intent() -> None:
    """current_intent is an extra conflict-resolution signal for the
    rewriter (e.g. ``準備睡覺`` telling it to drop the wand even if
    scene is vague). Must appear in the structured payload."""
    client = _FakeClient()
    rewriter = _Rewriter("1girl, sleeping, closed eyes")
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )
    # Character with an intent set on state.
    character = Character.create(
        name="Yuki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="sleepy", affection=0, fatigue=80, trust=0, energy=20,
            current_intent="準備睡覺",
        ),
        appearance="long black hair, red ribbon, holding wand",
    )

    await generator.generate(character=character, positive="床上")

    payload = rewriter.calls[0]
    assert "Current focus" in payload
    assert "準備睡覺" in payload
    # Appearance still included so the rewriter sees the wand it must
    # drop based on the intent + scene.
    assert "holding wand" in payload


@pytest.mark.asyncio
async def test_generator_payload_omits_runtime_state_when_caller_opts_out() -> None:
    """Settings-page portrait generation passes ``use_runtime_state=False``
    because the operator typed an explicit scene; a stale post-turn
    ``current_intent`` like ``準備睡覺`` or ``emotion`` like ``sleepy``
    would otherwise hijack the rewriter (e.g. force closed eyes + tired
    expression even though the operator asked for a cafe portrait)."""
    client = _FakeClient()
    rewriter = _Rewriter("1girl, cafe, reading book")
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )
    character = Character.create(
        name="Yuki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="sleepy", affection=0, fatigue=80, trust=0, energy=20,
            current_intent="準備睡覺",
        ),
        appearance="long black hair, holding wand",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="androgynous teen",
    )

    await generator.generate(
        character=character, positive="咖啡店看書",
        use_runtime_state=False,
    )

    payload = rewriter.calls[0]
    # Runtime state (both intent AND mood) gated out.
    assert "Current focus" not in payload
    assert "準備睡覺" not in payload
    assert "Current mood" not in payload
    assert "sleepy" not in payload
    # Stable appearance + operator's scene still flow through.
    assert "long black hair" in payload
    assert "androgynous teen" in payload
    assert "咖啡店看書" in payload


@pytest.mark.asyncio
async def test_generator_payload_omits_intent_when_unset() -> None:
    client = _FakeClient()
    rewriter = _Rewriter("1girl, cafe")
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )

    await generator.generate(character=_character(), positive="cafe")

    payload = rewriter.calls[0]
    # _character() has no current_intent set → no Focus line.
    assert "Current focus" not in payload


@pytest.mark.asyncio
async def test_rewriter_system_prompt_mentions_conflict_rules() -> None:
    """Smoke test that the system prompt actually teaches the LLM about
    conflict resolution. We don't control what the LLM outputs — but we
    can verify the instruction is present, which is the whole point of
    this layer."""
    from kokoro_link.infrastructure.tools.comfyui.prompt_rewriter import (
        _SYSTEM_PROMPT,
    )
    for keyword in (
        "closed eyes",
        "sleeping",
        "Conflict resolution",
        "held items",
        "visual gender presentation",
        "Do NOT infer visual gender",
        "Visual subject type / body plan",
        "non-human animal",
        "human face",
        "furry humanoid",
    ):
        assert keyword in _SYSTEM_PROMPT, f"missing {keyword!r} in system prompt"


@pytest.mark.asyncio
async def test_generator_falls_back_when_rewriter_returns_empty() -> None:
    client = _FakeClient()
    rewriter = _Rewriter("   ")  # whitespace-only
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        prompt_rewriter=rewriter,
    )

    await generator.generate(
        character=_character(), positive="爬山",
    )

    # Empty rewrite → fall back to per-field concatenation so nothing
    # is silently dropped. Raw Chinese still beats empty prompt.
    positive_text = client.queued_prompts[0]["6"]["inputs"]["text"]
    assert "爬山" in positive_text
    assert "long black hair" in positive_text
    assert "androgynous teen" in positive_text
