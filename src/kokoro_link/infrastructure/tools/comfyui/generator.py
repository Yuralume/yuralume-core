"""Shared ComfyUI portrait generator.

Factors out "given a character + user positive prompt, produce image
bytes" so both paths can reuse it:

1. ``ComfyImageTool.invoke`` — character asks to paint herself mid-
   chat; bytes get written to Object Storage under ``characters/{id}/tools/`` and
   returned as a message attachment.
2. ``CharacterImageService.generate_portrait`` — operator clicks
   "生成一張" in the character settings panel; bytes go through the
   normal image-upload path so they sit alongside manually-uploaded
   portraits and are eligible for stage rotation.

Both paths share the same prompt-building logic (character
``appearance`` + current ``emotion`` + caller-provided positive), the
same aspect→WH mapping, and the same LoRA injection via ``PromptSpec``.
Keeping that in one place means a tweak to the portrait recipe (e.g.
stronger quality boilerplate) lands for both surfaces at once.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageProviderPort,
    ImageTimeoutError,
)
from kokoro_link.contracts.prompt_rewriter import (
    PromptRewriteError,
    PromptRewriterPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.tools.comfyui.client import (
    AsyncComfyUiClient,
    ComfyUiError,
    ComfyUiTimeout,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    build_visual_subject_prompt,
    render_character_visual_subject_lines,
    visual_subject_negative_tags,
    visual_subject_positive_tags,
)
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_NEGATIVE_PROMPT,
    LoraSpec,
    PromptSpec,
    WorkflowBuilder,
)

_LOGGER = logging.getLogger(__name__)

ASPECT_TO_WH: dict[str, tuple[int, int]] = {
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "square": (1024, 1024),
}
DEFAULT_ASPECT = "portrait"


# Backwards-compat aliases: keep the old ``Portrait*`` names exported
# from this module so any straggling imports keep working until the
# rename has propagated everywhere. New code should import the
# ``Image*`` names from ``contracts.image_provider`` directly.
PortraitGenerationError = ImageGenerationError
PortraitTimeoutError = ImageTimeoutError
PortraitNoOutputError = ImageNoOutputError


class ComfyPortraitGenerator(ImageProviderPort):
    def __init__(
        self,
        *,
        client: AsyncComfyUiClient,
        workflow_builder: WorkflowBuilder,
        checkpoint: str | None = None,
        prompt_rewriter: PromptRewriterPort | None = None,
    ) -> None:
        self._client = client
        self._workflow_builder = workflow_builder
        self._checkpoint = checkpoint
        self._prompt_rewriter = prompt_rewriter

    async def generate(
        self,
        *,
        character: Character,
        positive: str,
        aspect: str = DEFAULT_ASPECT,
        batch: int = 1,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        user_attachment_urls: Sequence[str] = (),
    ) -> list[bytes]:
        """Queue a generation and return the produced image bytes.

        ``batch`` drives ComfyUI's latent ``batch_size`` — N images are
        produced in one diffusion run with distinct noise seeds. This
        is ~1.5-2× the cost of a single image (not N×), so it's the
        right primitive for a "gacha" candidate picker where the
        operator wants a few variants to choose from.

        Raises ``PortraitGenerationError`` subclasses on failure so
        callers can apologise to their user (chat) or surface an HTTP
        error (REST) without re-parsing the exception chain.
        """
        positive_clean = positive.strip()
        if not positive_clean:
            raise ImageGenerationError("缺少 positive prompt")

        width, height = ASPECT_TO_WH.get(
            aspect.lower(), ASPECT_TO_WH[DEFAULT_ASPECT],
        )
        # Let an optional LLM rewrite the *entire* payload (appearance
        # + mood + scene) into danbooru-style English tags before we
        # concatenate. Doing this as a single call matters: Illustrious
        # CLIP can't read Chinese, so translating scene alone while
        # leaving the operator's Chinese ``appearance`` in the prompt
        # makes the character drift every shot. The rewriter returns
        # a unified tag line; we only prepend the quality boilerplate.
        # On rewriter failure / absence we fall back to the old per-
        # field concatenation so generation still proceeds.
        rewritten = await self._rewrite_full_or_none(
            character, positive_clean,
            recent_dialogue=recent_dialogue,
            use_runtime_state=use_runtime_state,
            user_attachment_urls=tuple(user_attachment_urls or ()),
        )
        if rewritten is not None:
            positive_parts = [
                "masterpiece, best quality, amazing quality, "
                "very aesthetic, absurdres",
            ]
            subject_tags = visual_subject_positive_tags(character)
            if subject_tags:
                positive_parts.append(subject_tags)
            positive_parts.append(rewritten)
            full_positive = ", ".join(positive_parts)
        else:
            full_positive = self._compose_positive(
                character, positive_clean,
                use_runtime_state=use_runtime_state,
            )
        loras = tuple(
            LoraSpec(name=l.name, strength=l.strength)
            for l in character.loras
        )
        negative = DEFAULT_NEGATIVE_PROMPT
        subject_negative = visual_subject_negative_tags(character)
        if subject_negative:
            negative = f"{negative}, {subject_negative}"
        spec = PromptSpec(
            positive=full_positive,
            negative=negative,
            width=width,
            height=height,
            batch_count=max(1, batch),
            checkpoint=self._checkpoint or PromptSpec.__dataclass_fields__["checkpoint"].default,
            loras=loras,
        )
        prompt = self._workflow_builder.build(spec)

        try:
            prompt_id = await self._client.queue_prompt(prompt)
            history = await self._client.wait_for_completion(prompt_id)
        except ComfyUiTimeout as exc:
            raise ImageTimeoutError(f"ComfyUI 逾時：{exc}") from exc
        except ComfyUiError as exc:
            raise ImageGenerationError(f"ComfyUI 錯誤：{exc}") from exc

        # Walk the output nodes and download the rendered images. We
        # fetch bytes directly (not ``save_images`` → disk) so the
        # caller decides where the file ultimately lives — tool dir,
        # portrait dir, or neither.
        images: list[bytes] = []
        for node_output in history.get("outputs", {}).values():
            for image_meta in node_output.get("images", []) or []:
                try:
                    content = await self._client.download_image(
                        filename=image_meta["filename"],
                        subfolder=image_meta.get("subfolder", ""),
                        folder_type=image_meta.get("type", "output"),
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.exception("ComfyUI download failed")
                    raise ImageGenerationError(
                        f"下載圖片失敗：{exc}",
                    ) from exc
                images.append(content)

        if not images:
            raise ImageNoOutputError("ComfyUI 沒有回傳任何圖片")
        return images

    async def _rewrite_full_or_none(
        self,
        character: Character,
        scene: str,
        *,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        user_attachment_urls: tuple[str, ...] = (),
    ) -> str | None:
        """Build a structured payload and ask the rewriter to translate
        everything (appearance [+ mood + intent] + scene) into danbooru
        tags, with conflict resolution (sleeping → closed eyes, no
        held items mid-sleep, etc.).

        Returns the rewritten string on success, ``None`` when the
        rewriter isn't wired or the call fails. ``None`` signals the
        caller to fall back to the raw per-field concatenation so
        image generation still proceeds on any failure.

        ``use_runtime_state`` gates whether the character's **current**
        ``emotion`` and ``current_intent`` are fed to the rewriter.
        Chat-path generation wants them (mood + intent resolve conflicts
        against a vague scene — "holding wand" appearance + intent
        "準備睡覺" → rewriter drops the wand + adds closed eyes).
        Settings-page generation passes ``False`` — the operator typed
        an explicit scene and doesn't want a stale chat-induced mood
        ("sleepy" from the last turn) or intent ("準備睡覺") hijacking
        their portrait. Only the stable ``appearance`` and the scene
        flow through.
        """
        if self._prompt_rewriter is None:
            return None
        parts: list[str] = []
        appearance = (character.appearance or "").strip()
        if appearance:
            parts.append(f"Character appearance: {appearance}")
        parts.extend(render_character_visual_identity_lines(character))
        parts.extend(render_character_visual_subject_lines(character))
        if use_runtime_state:
            emotion = (character.state.emotion or "").strip()
            if emotion:
                parts.append(f"Current mood: {emotion}")
            intent = (character.state.current_intent or "").strip()
            if intent:
                parts.append(f"Current focus: {intent}")
        parts.append(f"Scene: {scene}")
        # Recent chat turns let the rewriter resolve pronouns and
        # implicit references in SCENE ("那樣的感覺", "剛剛說的那個") —
        # crucial for the command-forced trigger where the user's scene
        # string was typed inline during a conversation and leans on
        # prior turns for meaning. Offered as a hint only: the rewriter
        # is told to use it to disambiguate SCENE, not to invent new
        # content.
        dialogue = (recent_dialogue or "").strip()
        if dialogue:
            parts.append(
                "Recent chat dialogue (use ONLY to resolve pronouns / "
                "implicit references in SCENE; ignore unrelated content, "
                "do not invent new elements from it):\n" + dialogue
            )
        if user_attachment_urls:
            # Mark the payload so a vision-capable rewriter applies the
            # "User reference image attached" override rule from the
            # system prompt: drop the wardrobe half of 'Character
            # appearance' and replace it with what's visible in the
            # image. Without this marker the rewriter would see the
            # appearance line ("school uniform, red ribbon") AND the
            # image and try to keep both, producing a contradictory
            # outfit mash-up.
            parts.append(
                f"User reference image(s) attached this turn: "
                f"{len(user_attachment_urls)}. WARDROBE OVERRIDE in "
                "effect: the image is the source of truth for "
                "clothing / outfit / props / location for this shot. "
                "DROP every wardrobe tag from 'Character appearance' "
                "(uniforms, dresses, ribbons, accessories) and "
                "REPLACE with concrete danbooru tags read from the "
                "image. Keep ONLY identity tags (hair colour, eye "
                "colour, body type) from appearance. Do NOT carry "
                "both the original outfit and the new one — emit "
                "only what the image shows."
            )
        payload = "\n".join(parts)

        try:
            rewritten = await self._prompt_rewriter.rewrite(
                payload, character=character,
                image_urls=tuple(user_attachment_urls or ()),
            )
        except PromptRewriteError:
            _LOGGER.warning(
                "prompt rewriter failed; falling back to raw concatenation",
            )
            return None
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "prompt rewriter crashed; falling back to raw concatenation",
            )
            return None
        cleaned = rewritten.strip()
        return cleaned or None

    @staticmethod
    def _compose_positive(
        character: Character,
        user_positive: str,
        *,
        use_runtime_state: bool = True,
    ) -> str:
        parts = [
            "masterpiece, best quality, amazing quality, very aesthetic, absurdres",
        ]
        subject_tags = visual_subject_positive_tags(character)
        if subject_tags:
            parts.append(subject_tags)
        identity = (character.appearance or "").strip()
        if identity:
            parts.append(identity)
        visual_identity = _visual_identity_positive_hint(character)
        if visual_identity:
            parts.append(visual_identity)
        if use_runtime_state:
            emotion = character.state.emotion or ""
            if emotion:
                parts.append(emotion)
        parts.append(user_positive)
        return ", ".join(p for p in parts if p)


def _visual_identity_positive_hint(character: Character) -> str:
    if build_visual_subject_prompt(character).is_non_human_animal:
        return ""
    seen: set[str] = set()
    values: list[str] = []
    for raw in (
        character.gender_identity,
        character.visual_gender_presentation,
    ):
        text = (raw or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            values.append(text)
    return ", ".join(values)
