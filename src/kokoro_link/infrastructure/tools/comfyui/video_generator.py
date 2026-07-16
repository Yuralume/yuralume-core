"""ComfyUI Wan2.2 :class:`VideoProviderPort` adapter.

Sibling of :class:`ComfyPortraitGenerator` for the video side. Same
ComfyUI HTTP client, different workflow + different output handling
(history entry stores video files under the ``videos`` / ``gifs`` key
depending on the SaveVideo node variant, not ``images``).

The prompt path is much simpler than the image side:

  * No danbooru-tag rewriter — Wan2.2 reads natural-language captions,
    so we concatenate the structured fields (appearance + runtime
    mood + scene) as labelled prose. Wan2.2 also benefits from
    explicit motion verbs and camera direction, which the LLM
    composer is responsible for emitting in ``video_prompt``.

  * No LoRA chain — Wan2.2 isn't fine-tuned on per-character LoRAs in
    our setup, and identity comes from the appearance description
    rather than a trained trigger.

Identity drift across multiple clips is a known weakness of pure-text
Wan2.2 — we don't try to solve it here. Each clip gets a fresh seed
so the operator at least sees motion variety; if drift becomes a
problem, the answer is i2v with a fixed reference image, not booru
tags.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from kokoro_link.contracts.video_provider import (
    VideoGenerationError,
    VideoNoOutputError,
    VideoProviderPort,
    VideoTimeoutError,
)
from kokoro_link.infrastructure.tools.comfyui.client import (
    AsyncComfyUiClient,
    ComfyUiError,
    ComfyUiTimeout,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    render_character_visual_subject_lines,
)
from kokoro_link.infrastructure.tools.comfyui.wan_video_workflow import (
    WanVideoSpec,
    WanVideoWorkflowBuilder,
)

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

# Wan2.2 native resolutions per aspect (matches the reference workflow's
# 832x480 portrait baseline). Operators with a different model variant
# can swap in their own workflow JSON and override via the per-call
# width/height in the spec — these defaults just give sensible aspects
# when the LLM picks an aspect rather than explicit pixels.
ASPECT_TO_WH: dict[str, tuple[int, int]] = {
    "portrait": (480, 832),
    "landscape": (832, 480),
    "square": (640, 640),
}
DEFAULT_ASPECT = "portrait"

# Wan2.2 frame count must satisfy ``(N - 1) % 4 == 0`` (the temporal
# autoencoder downsamples by 4). 81 ≈ 5 s @ 16 fps is the reference
# default; we clamp to a safe band and snap to the nearest valid value.
_MIN_FRAMES = 17
_MAX_FRAMES = 121

_VIDEO_EXTS = (".mp4", ".webm", ".gif", ".mov", ".webp", ".mkv", ".avi")


def _snap_frames(value: int) -> int:
    clamped = max(_MIN_FRAMES, min(int(value or 0) or 81, _MAX_FRAMES))
    # Round to nearest valid (N-1)%4==0.
    remainder = (clamped - 1) % 4
    if remainder == 0:
        return clamped
    return clamped - remainder if remainder <= 2 else clamped + (4 - remainder)


class ComfyVideoGenerator(VideoProviderPort):
    def __init__(
        self,
        *,
        client: AsyncComfyUiClient,
        workflow_builder: WanVideoWorkflowBuilder,
        fps: int = 16,
        default_length_frames: int = 81,
        default_width: int = 480,
        default_height: int = 832,
    ) -> None:
        self._client = client
        self._workflow_builder = workflow_builder
        self._fps = fps
        self._default_length_frames = _snap_frames(default_length_frames)
        self._default_width = default_width
        self._default_height = default_height

    async def generate(
        self,
        *,
        character: "Character",
        positive: str,
        aspect: str = DEFAULT_ASPECT,
        length_frames: int = 0,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
    ) -> bytes:
        positive_clean = (positive or "").strip()
        if not positive_clean:
            raise VideoGenerationError("缺少 video positive prompt")

        full_positive = self._compose_prompt(
            character=character,
            scene=positive_clean,
            recent_dialogue=recent_dialogue,
            use_runtime_state=use_runtime_state,
        )
        width, height = ASPECT_TO_WH.get(
            aspect.lower(), ASPECT_TO_WH[DEFAULT_ASPECT],
        )
        # Fall back to profile-level defaults when aspect mapping
        # produces dimensions that don't match the operator's actual
        # Wan2.2 model variant. Most deployments stick with the
        # reference 14B fp8 at 832×480 — keep that as the floor.
        if width <= 0 or height <= 0:
            width, height = self._default_width, self._default_height

        frames = _snap_frames(length_frames or self._default_length_frames)
        # Wan2.2 high/low noise handoff is sensitive to seed — fresh
        # per call so two adjacent clips don't share noise patterns.
        seed = secrets.randbelow(2**31 - 1)

        spec = WanVideoSpec(
            positive=full_positive,
            width=width, height=height,
            length_frames=frames,
            fps=self._fps,
            seed=seed,
            filename_prefix=f"kokoro/feed/{character.id}",
        )
        prompt = self._workflow_builder.build(spec)

        try:
            prompt_id = await self._client.queue_prompt(prompt)
            history = await self._client.wait_for_completion(prompt_id)
        except ComfyUiTimeout as exc:
            raise VideoTimeoutError(f"ComfyUI Wan2.2 逾時：{exc}") from exc
        except ComfyUiError as exc:
            raise VideoGenerationError(f"ComfyUI 錯誤：{exc}") from exc

        # SaveVideo variants disagree on which output key the mp4
        # entry lives under: newer nodes use ``videos`` / ``gifs``,
        # but the SaveVideo shipped with the reference Wan2.2
        # workflow puts the mp4 file dict under ``images`` and signals
        # it with a sibling ``animated: [true]`` flag. Filter by
        # filename extension instead of key name so the adapter
        # tolerates any variant.
        video_files: list[dict] = []
        for node_output in history.get("outputs", {}).values():
            for value in node_output.values():
                if not isinstance(value, list):
                    continue
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("filename") or "").lower()
                    if name.endswith(_VIDEO_EXTS):
                        video_files.append(item)

        if not video_files:
            raise VideoNoOutputError(
                "ComfyUI Wan2.2 沒有回傳任何影片檔",
            )

        # Two-stage workflow (Wan raw → Illustrious stylized) emits two
        # videos in one run. The stylized output is the deliverable —
        # the raw save is a debug artifact for the operator. Prefer it
        # by subfolder match; fall back to the first match for the
        # legacy single-output workflow.
        stylized = [
            f for f in video_files
            if "stylized" in str(f.get("subfolder") or "").lower()
        ]
        if stylized:
            primary = stylized[0]
            if len(video_files) > len(stylized):
                _LOGGER.debug(
                    "Wan2.2 emitted %d files; using stylized (%s) and "
                    "discarding %d raw artifact(s)",
                    len(video_files), primary.get("filename"),
                    len(video_files) - len(stylized),
                )
        else:
            # SaveVideo can also write multiple files per single node
            # (batch). Take the first — single-clip semantics are what
            # callers expect; logging lets us spot mis-configured graphs.
            if len(video_files) > 1:
                _LOGGER.info(
                    "Wan2.2 emitted %d files, keeping the first (%s)",
                    len(video_files), video_files[0].get("filename"),
                )
            primary = video_files[0]
        try:
            blob = await self._client.download_image(
                filename=primary["filename"],
                subfolder=primary.get("subfolder", ""),
                folder_type=primary.get("type", "output"),
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("ComfyUI Wan2.2 download failed")
            raise VideoGenerationError(
                f"下載影片失敗：{exc}",
            ) from exc
        return blob

    @staticmethod
    def _compose_prompt(
        *,
        character: "Character",
        scene: str,
        recent_dialogue: str,
        use_runtime_state: bool,
    ) -> str:
        """Layer identity + runtime mood + scene as labelled prose.

        Wan2.2 reads English captions; we frame each component with a
        leading label so the model can weigh identity vs scene the way
        a human caption-writer would. Identity-first ordering matters:
        the appearance description anchors the character, then scene
        adds the verbs / motion the LLM composed."""
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
        parts.append(f"Scene and motion: {scene}")
        dialogue = (recent_dialogue or "").strip()
        if dialogue:
            parts.append(
                "Recent chat dialogue (use ONLY to resolve pronouns / "
                "implicit references in Scene; do not invent new "
                "visual elements from it):\n" + dialogue,
            )
        return "\n\n".join(parts)
