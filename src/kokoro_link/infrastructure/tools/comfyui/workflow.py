"""ComfyUI workflow template + spec.

Ported from ``ComfyGenPicture.workflow`` but keeping zero dependency
on that package — the workflow JSON template is copied into our
project so we can ship it in the wheel and edit it independently.

The node IDs (3, 4, 5, 6, 7) hard-coded here come from the Illustrious
XL default workflow: KSampler / CheckpointLoaderSimple / EmptyLatent
Image / two CLIPTextEncode nodes. If the operator swaps in a
different workflow JSON, those IDs need to line up — the ``build``
function will raise ``KeyError`` on mismatch instead of silently
ignoring the spec.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

DEFAULT_NEGATIVE_PROMPT = (
    "lowres, bad anatomy, bad hands, text, error, missing finger, "
    "extra digits, fewer digits, cropped, worst quality, low quality, "
    "low score, bad score, average score, signature, watermark, "
    "username, blurry"
)


@dataclass(frozen=True, slots=True)
class LoraSpec:
    """A single LoRA to chain into the workflow.

    ``name`` is the filename as stored in ComfyUI's ``models/loras/``
    directory. ``strength`` applies to both model and CLIP paths.
    """

    name: str
    strength: float = 1.0


@dataclass(frozen=True, slots=True)
class PromptSpec:
    positive: str
    negative: str = DEFAULT_NEGATIVE_PROMPT
    width: int = 832
    height: int = 1216
    batch_count: int = 1
    steps: int = 22
    cfg: float = 5.5
    sampler: str = "euler_ancestral"
    scheduler: str = "normal"
    seed: int | None = None
    checkpoint: str = "waiNSFWIllustrious_v140.safetensors"
    loras: tuple[LoraSpec, ...] = ()


_LORA_NODE_ID_BASE = 100
"""Starting node id for injected ``LoraLoader`` nodes.

We use a high number so inserts never collide with the hand-authored
workflow template's ids (currently 3–9)."""


class WorkflowBuilder:
    def __init__(self, workflow_file: Path) -> None:
        self._workflow_file = Path(workflow_file)

    def build(self, spec: PromptSpec) -> dict:
        prompt = json.loads(self._workflow_file.read_text(encoding="utf-8"))

        prompt["4"]["inputs"]["ckpt_name"] = spec.checkpoint
        prompt["5"]["inputs"]["width"] = spec.width
        prompt["5"]["inputs"]["height"] = spec.height
        prompt["5"]["inputs"]["batch_size"] = spec.batch_count
        prompt["6"]["inputs"]["text"] = spec.positive
        prompt["7"]["inputs"]["text"] = spec.negative

        sampler_inputs = prompt["3"]["inputs"]
        sampler_inputs["steps"] = spec.steps
        sampler_inputs["cfg"] = spec.cfg
        sampler_inputs["sampler_name"] = spec.sampler
        sampler_inputs["scheduler"] = spec.scheduler
        sampler_inputs["seed"] = (
            spec.seed if spec.seed is not None else random.randint(0, 2**63 - 1)
        )

        if spec.loras:
            _inject_loras(prompt, spec.loras)
        return prompt


def _inject_loras(prompt: dict, loras: tuple[LoraSpec, ...]) -> None:
    """Chain ``LoraLoader`` nodes between checkpoint (4) and samplers.

    Before::

        4 (Checkpoint) ─┬─► 3.model
                        └─► 6.clip, 7.clip

    After (2 LoRAs)::

        4 ─► 100 (lora A) ─► 101 (lora B) ─┬─► 3.model
                                             └─► 6.clip, 7.clip

    The last LoraLoader node exposes model on output index 0 and clip
    on output index 1, same as CheckpointLoaderSimple, so upstream
    consumers can treat them interchangeably once rewired.
    """
    prev_model_src = ["4", 0]
    prev_clip_src = ["4", 1]
    for index, lora in enumerate(loras):
        node_id = str(_LORA_NODE_ID_BASE + index)
        prompt[node_id] = {
            "class_type": "LoraLoader",
            "inputs": {
                "lora_name": lora.name,
                "strength_model": float(lora.strength),
                "strength_clip": float(lora.strength),
                "model": prev_model_src,
                "clip": prev_clip_src,
            },
            "_meta": {"title": f"Lora {lora.name}"},
        }
        prev_model_src = [node_id, 0]
        prev_clip_src = [node_id, 1]

    # Rewire the consumers. KSampler takes model only; CLIPTextEncode
    # nodes take clip only. VAE stays on the checkpoint (node 4) —
    # LoRAs don't rewire VAE.
    prompt["3"]["inputs"]["model"] = prev_model_src
    prompt["6"]["inputs"]["clip"] = prev_clip_src
    prompt["7"]["inputs"]["clip"] = prev_clip_src


DEFAULT_WORKFLOW_FILE = Path(__file__).parent / "workflows" / "illustrious.json"
