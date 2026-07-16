"""LLMStoryEventExpander scene-prompt selection.

Covers Phase 1 of ``docs/SCENE_BEAT_PLAN.md`` — when ``scene`` is set
and meaningful, the expander must switch to the "play this scene"
prompt. Empty / ``None`` scene context falls back to the legacy
"private journal" prompt so gacha-driven events are unchanged.
"""

from __future__ import annotations

import pytest

from kokoro_link.contracts.story import SceneContext
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.infrastructure.story.llm_expander import (
    LLMStoryEventExpander,
    NullStoryEventExpander,
)


class _CapturingModel:
    """Records the prompt for assertion + returns a canned JSON reply."""

    supports_vision = False

    def __init__(self, payload: str = '{"narrative": "靜靜走進場景。", "tone": null}') -> None:
        self._payload = payload
        self.last_prompt: str | None = None

    async def generate(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        self.last_prompt = prompt
        return self._payload

    def generate_stream(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


def _seed(text: str = "她在咖啡廳讀完了那本書。") -> StorySeed:
    return StorySeed.create(
        seed_text=text,
        tags=["daily"],
        world_frames=["modern"],
        weight=1.0,
        cooldown_days=7,
    )


class _DuckSeed:
    """Mimics arc-beat -> expander shape (no ``tags`` attribute).

    The real path uses ``_BeatAsSeed`` from story_event_service; this
    minimal stand-in keeps the test independent of that internal class.
    """

    def __init__(self, seed_text: str) -> None:
        self.id = "arc-beat:test"
        self.seed_text = seed_text


@pytest.mark.asyncio
async def test_no_scene_uses_journal_prompt() -> None:
    model = _CapturingModel()
    expander = LLMStoryEventExpander(model=model)
    await expander.expand(
        seed=_seed(),
        character_name="Aki",
        character_summary="插畫家",
        speaking_style="溫柔",
        world_frame="modern",
    )
    assert model.last_prompt is not None
    # Journal prompt has the seed-style framing.
    assert "私人手記" in model.last_prompt
    # Scene-style markers should NOT be present.
    assert "演出這場戲" not in model.last_prompt


@pytest.mark.asyncio
async def test_meaningful_scene_switches_to_scene_prompt() -> None:
    model = _CapturingModel()
    expander = LLMStoryEventExpander(model=model)
    scene = SceneContext(
        scene_type="conflict",
        location="音樂教室",
        scene_characters=("指導老師",),
        dramatic_question="她要承認自己練得不夠嗎？",
        required=True,
    )
    await expander.expand(
        seed=_DuckSeed("她踏進教室時，老師已經在等。"),
        character_name="Aki",
        character_summary="插畫家",
        speaking_style="溫柔",
        world_frame="modern",
        scene=scene,
    )
    assert model.last_prompt is not None
    prompt = model.last_prompt
    # Scene prompt markers.
    assert "演出這場戲" in prompt
    assert "音樂教室" in prompt
    assert "指導老師" in prompt
    assert "她要承認自己練得不夠嗎？" in prompt
    # Should NOT pick up the journal framing.
    assert "私人手記" not in prompt


@pytest.mark.asyncio
async def test_empty_scene_falls_back_to_journal() -> None:
    # All structured fields empty — `is_meaningful()` returns False so
    # the expander stays on the journal prompt instead of producing
    # an empty scene block.
    model = _CapturingModel()
    expander = LLMStoryEventExpander(model=model)
    empty_scene = SceneContext(
        scene_type="encounter",
        location=None,
        scene_characters=(),
        dramatic_question=None,
        required=True,
    )
    await expander.expand(
        seed=_seed(),
        character_name="Aki",
        character_summary="",
        speaking_style="",
        world_frame="modern",
        scene=empty_scene,
    )
    assert model.last_prompt is not None
    assert "私人手記" in model.last_prompt
    assert "演出這場戲" not in model.last_prompt


@pytest.mark.asyncio
async def test_scene_with_only_question_still_triggers_scene_prompt() -> None:
    # Single populated field is enough — useful for templates that
    # only know the dramatic question without committing to a location.
    model = _CapturingModel()
    expander = LLMStoryEventExpander(model=model)
    scene = SceneContext(
        scene_type="revelation",
        location=None,
        scene_characters=(),
        dramatic_question="她真正想要的是什麼？",
        required=False,
    )
    await expander.expand(
        seed=_DuckSeed("一個人坐在公園長椅。"),
        character_name="Aki",
        character_summary="",
        speaking_style="",
        world_frame="modern",
        scene=scene,
    )
    assert model.last_prompt is not None
    prompt = model.last_prompt
    assert "演出這場戲" in prompt
    assert "她真正想要的是什麼？" in prompt
    # `required=False` shows up as the gentler "輔助場景" hint.
    assert "輔助場景" in prompt


@pytest.mark.asyncio
async def test_null_expander_accepts_scene_kwarg() -> None:
    # Null expander is the universal fallback — it must accept the
    # new kwarg even though it ignores it.
    expander = NullStoryEventExpander()
    narrative, tone = await expander.expand(
        seed=_seed("a"),
        character_name="Aki",
        character_summary="",
        speaking_style="",
        world_frame="modern",
        scene=SceneContext(scene_type="conflict"),
    )
    assert narrative
    assert tone is None


# ---------- Tone-aware scene prompts (Phase 2.7 — wizard work) -----


@pytest.mark.asyncio
async def test_daily_tone_uses_baseline_framing() -> None:
    model = _CapturingModel()
    expander = LLMStoryEventExpander(model=model)
    await expander.expand(
        seed=_DuckSeed("公告欄上有新海報。"),
        character_name="Aki",
        character_summary="",
        speaking_style="",
        world_frame="modern",
        scene=SceneContext(
            scene_type="encounter",
            location="學校公告欄",
            tone="daily",
        ),
    )
    assert model.last_prompt is not None
    prompt = model.last_prompt
    # Daily mode: baseline framing, no extra style constraints
    # specific to the heavier tones.
    assert "整體調性：daily" in prompt
    assert "進入時刻" in prompt
    assert "不要迴避" not in prompt  # mature-only marker
    assert "心理層次" not in prompt   # dark-only marker
    assert "自我吐槽" not in prompt   # lighthearted-only marker


@pytest.mark.asyncio
async def test_mature_tone_inserts_unflinching_directives() -> None:
    model = _CapturingModel()
    expander = LLMStoryEventExpander(model=model)
    await expander.expand(
        seed=_DuckSeed("戰場上的最後一回合。"),
        character_name="Torban",
        character_summary="征服軍主帥",
        speaking_style="冷峻",
        world_frame="fantasy",
        scene=SceneContext(
            scene_type="conflict",
            location="戰場",
            scene_characters=("敵將",),
            tone="mature",
        ),
    )
    prompt = model.last_prompt
    assert prompt is not None
    assert "整體調性：mature" in prompt
    # Mature profile gives explicit permission for unfiltered
    # detail — the expander shouldn't soften back into daily mode.
    assert "不要迴避" in prompt
    assert "童書語言" in prompt
    # Other tone profiles should NOT leak in.
    assert "心理層次" not in prompt
    assert "自我吐槽" not in prompt


@pytest.mark.asyncio
async def test_dark_tone_targets_psychological_register() -> None:
    model = _CapturingModel()
    expander = LLMStoryEventExpander(model=model)
    await expander.expand(
        seed=_DuckSeed("一個人在窗邊喝著冷掉的咖啡。"),
        character_name="Aki",
        character_summary="",
        speaking_style="",
        world_frame="modern",
        scene=SceneContext(
            scene_type="revelation",
            # Tone alone doesn't trigger the scene prompt — at least one
            # structural field needs populating. Passing a location
            # mirrors what wizard-authored templates will look like in
            # practice (the wizard always asks for at least a location
            # or dramatic question).
            location="自家窗邊",
            tone="dark",
        ),
    )
    prompt = model.last_prompt
    assert prompt is not None
    assert "整體調性：dark" in prompt
    assert "心理層次" in prompt
    assert "感官錯位" in prompt
    # No accidental crossover to other tones.
    assert "不要迴避" not in prompt
    assert "自我吐槽" not in prompt


@pytest.mark.asyncio
async def test_lighthearted_tone_allows_humour_register() -> None:
    model = _CapturingModel()
    expander = LLMStoryEventExpander(model=model)
    await expander.expand(
        seed=_DuckSeed("早餐不小心打翻了奶油。"),
        character_name="Aki",
        character_summary="",
        speaking_style="",
        world_frame="modern",
        scene=SceneContext(
            scene_type="encounter",
            location="廚房",
            tone="lighthearted",
        ),
    )
    prompt = model.last_prompt
    assert prompt is not None
    assert "整體調性：lighthearted" in prompt
    assert "自我吐槽" in prompt or "輕喜劇" in prompt
    assert "不要迴避" not in prompt


@pytest.mark.asyncio
async def test_unknown_tone_falls_back_to_daily() -> None:
    """Wizard might author a tone label we haven't hardcoded yet —
    e.g. ``"romantic"`` or ``"existential"``. The expander must keep
    working (default daily framing) instead of crashing or producing
    a bare prompt with no constraints."""
    model = _CapturingModel()
    expander = LLMStoryEventExpander(model=model)
    await expander.expand(
        seed=_DuckSeed("一段對話的尾音。"),
        character_name="Aki",
        character_summary="",
        speaking_style="",
        world_frame="modern",
        scene=SceneContext(
            scene_type="encounter",
            location="教室",
            tone="existential",
        ),
    )
    prompt = model.last_prompt
    assert prompt is not None
    # Tone label is still echoed (so LLM can still take a hint),
    # but profile-specific markers fall back to daily defaults.
    assert "整體調性：existential" in prompt
    assert "進入時刻" in prompt
    assert "不要迴避" not in prompt
    assert "心理層次" not in prompt
