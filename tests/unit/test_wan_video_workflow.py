"""Tests for the Wan2.2 + Illustrious vid2vid workflow builder.

Pins the substitution path so a refactor (or an upstream workflow
update) doesn't silently leave one of the dynamic fields hard-coded
to the template default — which would manifest as "every video uses
the same seed / dimensions / prompt" in production.

Covers both the new two-stage workflow (Wan + Illustrious vid2vid) and
the legacy single-stage ``wan22_t2v.json`` via the title-alias sets so
operators carrying custom workflow JSONs keep working across the
upgrade.
"""

from __future__ import annotations

from pathlib import Path

from kokoro_link.infrastructure.tools.comfyui.wan_video_workflow import (
    DEFAULT_WAN_VIDEO_WORKFLOW_FILE,
    WanVideoSpec,
    WanVideoWorkflowBuilder,
)

_LEGACY_WORKFLOW_FILE = (
    DEFAULT_WAN_VIDEO_WORKFLOW_FILE.parent / "wan22_t2v.json"
)


def _find_by_title(workflow: dict, title: str) -> dict:
    for node in workflow.values():
        meta = node.get("_meta") or {}
        if meta.get("title") == title:
            return node
    raise AssertionError(f"node with title {title!r} not in workflow")


def _find_optional(workflow: dict, title: str) -> dict | None:
    for node in workflow.values():
        meta = node.get("_meta") or {}
        if meta.get("title") == title:
            return node
    return None


# ---------- New two-stage workflow ----------


def test_default_workflow_is_two_stage_vid2vid() -> None:
    """Default file points at the operator's optimized two-stage graph
    (Wan motion → Illustrious vid2vid stylization)."""
    assert DEFAULT_WAN_VIDEO_WORKFLOW_FILE.name == "wan_illustrious_vid2vid.json"


def test_builder_substitutes_wan_and_illustrious_prompts() -> None:
    builder = WanVideoWorkflowBuilder()
    spec = WanVideoSpec(
        positive="a girl scrolling her phone, slow camera drift",
        width=480, height=832, length_frames=49, fps=16, seed=12345,
        filename_prefix="kokoro/feed/test",
    )
    wf = builder.build(spec)

    # Wan side gets the full caption (whole-text replacement).
    wan_pos = _find_by_title(wf, "Wan Positive (motion)")["inputs"]["text"]
    assert wan_pos == "a girl scrolling her phone, slow camera drift"

    # Illustrious side keeps the quality prefix and embeds the caption
    # where the placeholder token used to be.
    ill_pos = _find_by_title(wf, "Illustrious Positive")["inputs"]["text"]
    assert "masterpiece" in ill_pos
    assert "ILLUSTRIOUS_POSITIVE_PLACEHOLDER" not in ill_pos
    assert "a girl scrolling" in ill_pos


def test_builder_substitutes_latent_and_seeds() -> None:
    builder = WanVideoWorkflowBuilder()
    spec = WanVideoSpec(
        positive="anything", width=512, height=768,
        length_frames=49, fps=16, seed=4242,
        filename_prefix="kokoro/feed/seed-probe",
    )
    wf = builder.build(spec)

    latent = _find_by_title(wf, "Empty Wan latent")["inputs"]
    assert (latent["width"], latent["height"]) == (512, 768)
    assert latent["length"] == 49

    # KSamplerAdvanced (Wan) uses ``noise_seed``
    assert _find_by_title(wf, "Wan sample high-noise")["inputs"]["noise_seed"] == 4242
    assert _find_by_title(wf, "Wan sample low-noise")["inputs"]["noise_seed"] == 4242
    # KSampler (Illustrious) uses plain ``seed`` — different field name
    assert _find_by_title(wf, "Illustrious repaint sampler")["inputs"]["seed"] == 4242


def test_builder_writes_both_save_prefixes() -> None:
    """Raw debug output goes under ``{prefix}/raw``, the stylized
    deliverable under ``{prefix}/stylized`` — the consumer uses that
    subfolder split to pick the right file."""
    builder = WanVideoWorkflowBuilder()
    spec = WanVideoSpec(
        positive="anything", width=480, height=832,
        length_frames=49, fps=16, seed=7,
        filename_prefix="kokoro/feed/char-x",
    )
    wf = builder.build(spec)

    raw = _find_by_title(wf, "Save raw Wan video")["inputs"]["filename_prefix"]
    stylized = _find_by_title(wf, "Save stylized video")["inputs"]["filename_prefix"]
    assert raw == "kokoro/feed/char-x/raw"
    assert stylized == "kokoro/feed/char-x/stylized"


def test_builder_sets_fps_on_both_create_nodes() -> None:
    builder = WanVideoWorkflowBuilder()
    spec = WanVideoSpec(
        positive="anything", width=480, height=832,
        length_frames=49, fps=24, seed=1,
    )
    wf = builder.build(spec)

    assert _find_by_title(wf, "Create raw Wan video")["inputs"]["fps"] == 24
    assert _find_by_title(wf, "Create stylized video")["inputs"]["fps"] == 24


def test_builder_returns_fresh_dict_each_call() -> None:
    """Concurrent generations must not see each other's prompt — the
    deepcopy is what makes that safe."""
    builder = WanVideoWorkflowBuilder()
    first = builder.build(WanVideoSpec(
        positive="first", width=480, height=832,
        length_frames=49, fps=16, seed=1,
    ))
    second = builder.build(WanVideoSpec(
        positive="second", width=480, height=832,
        length_frames=49, fps=16, seed=2,
    ))
    assert _find_by_title(first, "Wan Positive (motion)")["inputs"]["text"] == "first"
    assert _find_by_title(second, "Wan Positive (motion)")["inputs"]["text"] == "second"
    assert _find_by_title(first, "Wan sample high-noise")["inputs"]["noise_seed"] == 1
    assert _find_by_title(second, "Wan sample high-noise")["inputs"]["noise_seed"] == 2


# ---------- Legacy single-stage workflow (backwards compat) ----------


def test_legacy_workflow_still_substitutes() -> None:
    """Operators with the older ``wan22_t2v.json`` shouldn't have to
    rename node titles; the alias sets carry both names."""
    builder = WanVideoWorkflowBuilder(_LEGACY_WORKFLOW_FILE)
    spec = WanVideoSpec(
        positive="legacy probe", width=480, height=832,
        length_frames=81, fps=16, seed=9999,
        filename_prefix="kokoro/feed/legacy",
    )
    wf = builder.build(spec)

    assert _find_by_title(wf, "Positive Prompt")["inputs"]["text"] == "legacy probe"
    latent = _find_by_title(wf, "Empty Wan/Hunyuan latent")["inputs"]
    assert (latent["width"], latent["height"], latent["length"]) == (480, 832, 81)
    assert _find_by_title(wf, "Sample high-noise")["inputs"]["noise_seed"] == 9999
    assert _find_by_title(wf, "Sample low-noise")["inputs"]["noise_seed"] == 9999
    assert _find_by_title(wf, "Create Video")["inputs"]["fps"] == 16
    # No stylized save node in the legacy workflow → raw save uses the
    # bare prefix (no ``/raw`` suffix) so the single deliverable lands
    # where downstream consumers expect.
    assert _find_optional(wf, "Save stylized video") is None
    save = _find_by_title(wf, "Save Video")["inputs"]["filename_prefix"]
    assert save == "kokoro/feed/legacy"
