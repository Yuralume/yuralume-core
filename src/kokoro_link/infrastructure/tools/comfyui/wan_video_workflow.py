"""Wan2.2 + Illustrious vid2vid workflow builder.

Loads the reference ``wan_illustrious_vid2vid.json`` (ComfyUI API
format) and substitutes the per-call knobs across both pipeline stages:

  1. Wan2.2 T2V generates raw motion frames (high-noise → low-noise
     KSamplerAdvanced pair, ``noise_seed``).
  2. Illustrious SDXL vid2vid (with OpenPose + Depth ControlNets +
     AnimateDiff) restyles those frames into anime-aesthetic output.
     KSampler with the regular ``seed`` field — same value as the Wan
     samplers so the two stages stay coherent if the operator wants to
     reproduce a clip.

Substitution is by node-meta title (set of acceptable aliases per role)
so:

  * The JSON can be re-exported from ComfyUI and re-wired internally
    without us chasing renumbered node ids.
  * Operators sticking with the older single-stage ``wan22_t2v.json``
    template still get full substitution (legacy titles are kept in the
    alias sets).

Two SaveVideo nodes are present in the new workflow. The builder writes
``{prefix}/raw`` to the Wan-only save and ``{prefix}/stylized`` to the
Illustrious-stylized save; the consumer (:class:`ComfyVideoGenerator`)
prefers ``stylized/`` outputs and falls back to any video file when only
one save node exists (legacy workflow path).
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

DEFAULT_WAN_VIDEO_WORKFLOW_FILE = (
    Path(__file__).resolve().parent
    / "workflows" / "wan_illustrious_vid2vid.json"
)

# --- Node-title aliases ------------------------------------------------
# Each role accepts both the legacy single-stage names and the new
# two-stage names so operators with custom workflow JSONs aren't forced
# to rename nodes when the package upgrades.

_WAN_POSITIVE_TITLES = {
    "Positive Prompt",          # legacy wan22_t2v.json
    "Wan Positive (motion)",    # new wan_illustrious_vid2vid.json
}
_ILLUSTRIOUS_POSITIVE_TITLES = {
    "Illustrious Positive",
}
_LATENT_TITLES = {
    "Empty Wan/Hunyuan latent",
    "Empty Wan latent",
}
_WAN_SAMPLER_TITLES = {
    "Sample high-noise", "Sample low-noise",                # legacy
    "Wan sample high-noise", "Wan sample low-noise",        # new
}
_ILLUSTRIOUS_SAMPLER_TITLES = {
    "Illustrious repaint sampler",
}
_CREATE_VIDEO_TITLES = {
    "Create Video",                  # legacy
    "Create raw Wan video",          # new — Wan stage output
    "Create stylized video",         # new — Illustrious stage output
}
_SAVE_RAW_TITLES = {
    "Save Video",                    # legacy single-output
    "Save raw Wan video",            # new — debug artifact
}
_SAVE_STYLIZED_TITLES = {
    "Save stylized video",           # new — the deliverable
}

# Token inside the Illustrious positive prompt template — operator
# leaves the quality prefix ("masterpiece, best quality, ...") in place
# and we only swap this token for the per-call scene description.
_ILLUSTRIOUS_PROMPT_TOKEN = "ILLUSTRIOUS_POSITIVE_PLACEHOLDER"


@dataclass(frozen=True, slots=True)
class WanVideoSpec:
    positive: str
    width: int
    height: int
    length_frames: int
    fps: int
    seed: int
    filename_prefix: str = "kokoro/feed"


def _title_of(node: dict) -> str | None:
    return (node.get("_meta") or {}).get("title")


class WanVideoWorkflowBuilder:
    def __init__(self, workflow_file: Path = DEFAULT_WAN_VIDEO_WORKFLOW_FILE) -> None:
        self._template = json.loads(
            Path(workflow_file).read_text(encoding="utf-8"),
        )

    def build(self, spec: WanVideoSpec) -> dict:
        """Return a fresh workflow dict ready to POST to /prompt.

        Defensive deep-copy: the template is shared mutable state across
        calls, and substitution writes into nested dicts. Without the
        copy, a concurrent generation would see another call's prompt.
        """
        workflow = copy.deepcopy(self._template)

        for node in workflow.values():
            title = _title_of(node)
            if title is None:
                continue
            inputs = node.setdefault("inputs", {})

            if title in _WAN_POSITIVE_TITLES:
                # Wan side: full natural-language caption replaces the
                # template placeholder (Wan2.2 reads English prose, not
                # booru tags).
                inputs["text"] = spec.positive

            elif title in _ILLUSTRIOUS_POSITIVE_TITLES:
                # Illustrious side: operator's template keeps a quality
                # prefix and an explicit placeholder token. Replacing
                # the token (instead of overwriting the whole field)
                # preserves the operator-tuned styling lead-in.
                current = str(inputs.get("text") or "")
                if _ILLUSTRIOUS_PROMPT_TOKEN in current:
                    inputs["text"] = current.replace(
                        _ILLUSTRIOUS_PROMPT_TOKEN, spec.positive,
                    )
                else:
                    # Operator deleted the placeholder — best effort:
                    # overwrite entirely so the per-call positive still
                    # lands. Logging once would be noisy, so silent.
                    inputs["text"] = spec.positive

            elif title in _LATENT_TITLES:
                inputs["width"] = spec.width
                inputs["height"] = spec.height
                inputs["length"] = spec.length_frames

            elif title in _WAN_SAMPLER_TITLES:
                # KSamplerAdvanced uses ``noise_seed``. Both Wan stages
                # share the same seed so the high→low denoise handoff
                # stays coherent.
                inputs["noise_seed"] = spec.seed

            elif title in _ILLUSTRIOUS_SAMPLER_TITLES:
                # KSampler (vanilla) uses ``seed`` — different field
                # name from KSamplerAdvanced. Same value as Wan so the
                # full pipeline is deterministic given one seed.
                inputs["seed"] = spec.seed

            elif title in _CREATE_VIDEO_TITLES:
                inputs["fps"] = spec.fps

            elif title in _SAVE_RAW_TITLES:
                # In the single-stage legacy workflow this IS the
                # deliverable, so write the bare prefix. In the new
                # two-stage workflow there's also a stylized save, so
                # suffix the raw one to keep it segregated.
                inputs["filename_prefix"] = (
                    f"{spec.filename_prefix}/raw"
                    if self._has_stylized_save()
                    else spec.filename_prefix
                )

            elif title in _SAVE_STYLIZED_TITLES:
                inputs["filename_prefix"] = f"{spec.filename_prefix}/stylized"

        return workflow

    def _has_stylized_save(self) -> bool:
        """Whether the template has a separate stylized save node.

        Determines how the raw-save filename prefix is laid out — see
        the ``_SAVE_RAW_TITLES`` branch in :meth:`build`."""
        for node in self._template.values():
            if _title_of(node) in _SAVE_STYLIZED_TITLES:
                return True
        return False
