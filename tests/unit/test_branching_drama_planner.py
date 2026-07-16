"""BranchingDramaPlanner synthetic-fallback localization tests.

``_synthetic_root`` / ``_synthetic_children`` are the LLM-free fallback
path (fake provider, LLM error, unparseable output) used whenever
``plan_root`` / ``plan_children`` cannot get a real model response. They
used to hardcode zh-TW strings (序幕 / 暗潮湧動 / 黑暗結局 /
未命名的邂逅 / （未命名劇場）) regardless of the operator's
``primary_language``. This mirrors the already-landed
``llm_arc_planner._SYNTHETIC_ARC_TEMPLATES`` pattern: a static,
intentionally LLM-free three-language template pack (zh-TW / en-US /
ja-JP) resolved exact-tag -> language-family -> zh-TW fallback.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.branching_drama_planner import (
    BranchingDramaPlanner,
)
from kokoro_link.domain.entities.branching_drama import (
    TONE_DARK,
    TONE_NEUTRAL,
    TONE_SUNNY,
)


class _UnparseableModel:
    """Model stub that always returns non-JSON output, forcing
    ``plan_root`` / ``plan_children`` down the synthetic-fallback path
    (mirrors ``test_fusion_story_planner``'s
    ``test_unparseable_falls_back_to_synthetic``)."""

    supports_vision = False
    provider_id = "not-fake"

    async def generate(
        self,
        prompt: str,
        *,
        image_urls=None,  # noqa: ARG002
        model=None,  # noqa: ARG002
    ) -> str:
        return "not json at all"

    def generate_stream(
        self, prompt: str, *, image_urls=None, model=None,  # noqa: ARG002
    ):
        async def _empty():
            if False:
                yield ""
        return _empty()

    async def list_models(self) -> list[str]:
        return []


def _planner() -> BranchingDramaPlanner:
    return BranchingDramaPlanner(model=_UnparseableModel())


def _briefs() -> list[CharacterBrief]:
    return [
        CharacterBrief(
            character_id="a",
            name="Alice",
            summary="A summary",
            text="## Character: Alice (id=a)\n- summary: A",
        ),
        CharacterBrief(
            character_id="b",
            name="Bob",
            summary="B summary",
            text="## Character: Bob (id=b)\n- summary: B",
        ),
    ]


def _has_han(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _has_kana(text: str) -> bool:
    return any("぀" <= ch <= "ヿ" for ch in text)


class TestSyntheticRootLocalization:
    @pytest.mark.asyncio
    async def test_root_falls_back_to_zh_tw_by_default(self) -> None:
        planner = _planner()
        drama_title, outline = await planner.plan_root(
            prompt="",
            briefs=_briefs(),
            total_segments=6,
        )
        assert _has_han(outline.title)
        assert _has_han(outline.summary)
        assert _has_han(drama_title) or drama_title.strip()

    @pytest.mark.asyncio
    async def test_root_localizes_to_english(self) -> None:
        planner = _planner()
        _drama_title, outline = await planner.plan_root(
            prompt="",
            briefs=_briefs(),
            total_segments=6,
            operator_primary_language="en-US",
        )
        blob = outline.title + " " + outline.summary
        assert not _has_han(blob), blob

    @pytest.mark.asyncio
    async def test_root_localizes_to_japanese(self) -> None:
        planner = _planner()
        _drama_title, outline = await planner.plan_root(
            prompt="",
            briefs=_briefs(),
            total_segments=6,
            operator_primary_language="ja-JP",
        )
        blob = outline.title + " " + outline.summary
        assert _has_kana(blob), blob

    @pytest.mark.asyncio
    async def test_root_falls_back_to_zh_tw_for_unknown_language(self) -> None:
        planner = _planner()
        _drama_title, outline = await planner.plan_root(
            prompt="",
            briefs=_briefs(),
            total_segments=6,
            operator_primary_language="fr-FR",
        )
        blob = outline.title + " " + outline.summary
        assert _has_han(blob), blob

    @pytest.mark.asyncio
    async def test_root_default_title_localizes_when_prompt_blank(self) -> None:
        # When prompt is blank, the drama_title fallback ("未命名的邂逅")
        # must also localize, not just the outline body.
        planner = _planner()
        drama_title, _outline = await planner.plan_root(
            prompt="",
            briefs=_briefs(),
            total_segments=6,
            operator_primary_language="en-US",
        )
        assert not _has_han(drama_title), drama_title


class TestSyntheticChildrenLocalization:
    @pytest.mark.asyncio
    async def test_children_falls_back_to_zh_tw_by_default(self) -> None:
        planner = _planner()
        children = await planner.plan_children(
            prompt="",
            briefs=_briefs(),
            parent_summary="",
            path_context="",
            depth=1,
            total_segments=6,
        )
        for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL):
            assert _has_han(children[tone].title)
            assert _has_han(children[tone].summary)

    @pytest.mark.asyncio
    async def test_children_localizes_to_english(self) -> None:
        planner = _planner()
        children = await planner.plan_children(
            prompt="",
            briefs=_briefs(),
            parent_summary="",
            path_context="",
            depth=1,
            total_segments=6,
            operator_primary_language="en-US",
        )
        for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL):
            blob = children[tone].title + " " + children[tone].summary
            assert not _has_han(blob), blob

    @pytest.mark.asyncio
    async def test_children_localizes_to_japanese(self) -> None:
        planner = _planner()
        children = await planner.plan_children(
            prompt="",
            briefs=_briefs(),
            parent_summary="",
            path_context="",
            depth=1,
            total_segments=6,
            operator_primary_language="ja-JP",
        )
        for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL):
            blob = children[tone].title + " " + children[tone].summary
            assert _has_kana(blob), blob

    @pytest.mark.asyncio
    async def test_children_falls_back_to_zh_tw_for_unknown_language(self) -> None:
        planner = _planner()
        children = await planner.plan_children(
            prompt="",
            briefs=_briefs(),
            parent_summary="",
            path_context="",
            depth=1,
            total_segments=6,
            operator_primary_language="fr-FR",
        )
        for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL):
            blob = children[tone].title + " " + children[tone].summary
            assert _has_han(blob), blob

    @pytest.mark.asyncio
    async def test_children_ending_titles_localize_english(self) -> None:
        # depth == total_segments - 1 triggers the "ending" title variants
        # (黑暗結局/溫馨結局/平淡結局) which must also localize.
        planner = _planner()
        children = await planner.plan_children(
            prompt="",
            briefs=_briefs(),
            parent_summary="",
            path_context="",
            depth=5,
            total_segments=6,
            operator_primary_language="en-US",
        )
        for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL):
            blob = children[tone].title + " " + children[tone].summary
            assert not _has_han(blob), blob
