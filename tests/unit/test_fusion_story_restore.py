"""C0-6 version restore: head points back, chain intact, iterable after."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

import pytest

from kokoro_link.application.services.fusion_character_brief import (
    FusionCharacterBriefBuilder,
)
from kokoro_link.application.services.fusion_story_service import (
    FusionStoryService,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import (
    STATUS_READY,
    FusionStory,
    beats_from_snapshot_json,
    serialize_beats,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.fusion_critique import (
    FusionStoryCritique,
)
from kokoro_link.domain.value_objects.fusion_outline import (
    ACT_OPENING,
    ACT_RESOLUTION,
    ACT_RISING,
    ACT_TURN,
    FusionBeatPlan,
    FusionOutline,
)
from kokoro_link.infrastructure.repositories.in_memory_fusion_stories import (
    InMemoryFusionStoryRepository,
)


def _make_character(letter: str) -> Character:
    char = Character.create(
        name=f"Char-{letter}",
        summary=f"summary {letter}",
        personality=["calm"],
        interests=["coffee"],
        speaking_style="quiet",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    object.__setattr__(char, "id", f"c-{letter}")
    return char


@dataclass
class _CharServiceStub:
    by_id: dict[str, Character]

    async def get_character_entity(
        self, character_id: str,
    ) -> Character | None:
        return self.by_id.get(character_id)


def _outline() -> FusionOutline:
    acts = (ACT_OPENING, ACT_RISING, ACT_TURN, ACT_RESOLUTION)
    return FusionOutline.create(
        title="標題", premise="前提", theme="custom",
        beats=[
            FusionBeatPlan.create(
                sequence=i, act=acts[i], title=f"幕{i}",
                hook=f"hook{i}", dramatic_question="",
                target_chars=500, focus_character_ids=("c-a", "c-b"),
            )
            for i in range(4)
        ],
    )


class _ScriptedPlanner:
    async def plan(self, *, prompt, briefs, previous_outline=None):  # noqa: ARG002
        return _outline()


class _ScriptedWriter:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def write_beat(self, *, prompt, outline, beat, briefs, previously_summary="", previous_tail="", regenerate_hint=None):  # noqa: ARG002
        self.calls.append(beat.sequence)
        return f"NEW-{beat.sequence} 內容。"


class _ScriptedPolisher:
    async def polish(self, *, prompt, outline, draft_text, briefs, critique=None, round_index=0):  # noqa: ARG002
        return f"POLISHED:{draft_text}"


class _ScriptedCritic:
    async def review(self, *, prompt, outline, draft_text, briefs, round_index=0, previous_critique=None):  # noqa: ARG002
        return FusionStoryCritique.clean()


def _service():
    repo = InMemoryFusionStoryRepository()
    chars = {"c-a": _make_character("a"), "c-b": _make_character("b")}
    writer = _ScriptedWriter()
    service = FusionStoryService(
        repository=repo,
        character_service=_CharServiceStub(by_id=chars),  # type: ignore[arg-type]
        brief_builder=FusionCharacterBriefBuilder(memory_repository=None),
        planner=_ScriptedPlanner(),  # type: ignore[arg-type]
        writer=writer,  # type: ignore[arg-type]
        polisher=_ScriptedPolisher(),  # type: ignore[arg-type]
        critic=_ScriptedCritic(),  # type: ignore[arg-type]
    )
    return repo, service, writer


def _v1_story() -> FusionStory:
    """A ready story with filled beats — the v1 head."""
    story = FusionStory.create_pending(
        character_ids=["c-a", "c-b"], prompt="提示",
    )
    story = story.with_outline(_outline())
    for beat in story.beats:
        story = story.with_beat_content(
            beat_id=beat.id, content=f"V1-{beat.sequence} 內容。",
        )
    return story.with_full_text("V1 完稿。")


def _iterated(story: FusionStory) -> FusionStory:
    """Simulate one iterate: snapshot v1, mutate to a v2 head."""
    snapshot = story.snapshot_version(label="polish")
    return snapshot.with_full_text("V2 完稿（改壞了）。")


class TestSnapshotBeats:
    def test_snapshot_captures_beat_prose(self) -> None:
        story = _v1_story()
        snapshot = story.snapshot_version(label="polish")
        version = snapshot.versions[-1]
        beats = beats_from_snapshot_json(version.beats_json)
        assert [b.content for b in beats] == [
            f"V1-{i} 內容。" for i in range(4)
        ]

    def test_serialize_beats_roundtrip(self) -> None:
        story = _v1_story()
        decoded = beats_from_snapshot_json(serialize_beats(story.beats))
        assert [b.title for b in decoded] == [b.title for b in story.beats]
        assert [b.content for b in decoded] == [
            b.content for b in story.beats
        ]


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_points_head_back_and_keeps_chain(self) -> None:
        repo, service, _ = _service()
        story = _iterated(_v1_story())
        await repo.add(story)
        assert story.full_text.startswith("V2")

        restored = await service.restore_version(
            story.id, version_number=1,
        )
        assert restored.full_text == "V1 完稿。"
        assert restored.status == STATUS_READY
        # Beats came back from the snapshot, not the v2 head.
        assert [b.content for b in restored.beats] == [
            f"V1-{i} 內容。" for i in range(4)
        ]
        # The chain kept both directions: v1 snapshot + the pre-restore
        # v2 head, and head_version advanced.
        labels = [v.iteration_label for v in restored.versions]
        assert "polish" in labels
        assert "restore_v1" in labels
        assert restored.head_version == 3

    @pytest.mark.asyncio
    async def test_restore_then_iterate_beat_uses_restored_material(
        self,
    ) -> None:
        repo, service, writer = _service()
        story = _iterated(_v1_story())
        await repo.add(story)
        await service.restore_version(story.id, version_number=1)

        await service.iterate_beat(story.id, beat_index=1)
        for _ in range(300):
            current = await service.get(story.id)
            assert current is not None
            if current.is_terminal():
                break
            await asyncio.sleep(0.01)
        final = await service.get(story.id)
        assert final is not None and final.status == STATUS_READY
        assert writer.calls == [1]
        # Untouched beats keep the restored v1 prose.
        assert final.beats[0].content == "V1-0 內容。"
        assert final.beats[1].content.startswith("NEW-1")

    @pytest.mark.asyncio
    async def test_restore_rejects_busy_story(self) -> None:
        repo, service, _ = _service()
        story = _iterated(_v1_story()).with_status("writing")
        await repo.add(story)
        with pytest.raises(ValueError):
            await service.restore_version(story.id, version_number=1)

    @pytest.mark.asyncio
    async def test_restore_unknown_version_raises_key_error(self) -> None:
        repo, service, _ = _service()
        story = _iterated(_v1_story())
        await repo.add(story)
        with pytest.raises(KeyError):
            await service.restore_version(story.id, version_number=9)

    @pytest.mark.asyncio
    async def test_legacy_version_without_beats_restores_text_only(
        self,
    ) -> None:
        repo, service, _ = _service()
        story = _iterated(_v1_story())
        # Simulate a pre-C0-6 row: blank out the stored beats snapshot.
        legacy_versions = tuple(
            replace(v, beats_json="[]") for v in story.versions
        )
        story = replace(story, versions=legacy_versions)
        await repo.add(story)

        restored = await service.restore_version(
            story.id, version_number=1,
        )
        assert restored.full_text == "V1 完稿。"
        assert restored.beats == ()
        # Per-beat iteration is honestly unavailable on a text-only
        # restore; full outline re-plan remains possible.
        with pytest.raises(ValueError):
            await service.iterate_beat(story.id, beat_index=0)
