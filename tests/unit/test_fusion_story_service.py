"""BDD-style end-to-end smoke for the FusionStoryService pipeline.

Wires the in-memory repo + a stub character service + scripted planner /
writer / polisher / critic and walks the four pipeline stages from
``planning`` → ``ready`` so a regression in the orchestration glue
fails the suite even when no LLM is reachable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
    FusionCharacterBriefBuilder,
)
from kokoro_link.application.services.fusion_story_service import (
    FusionStoryService,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import (
    STATUS_FAILED,
    STATUS_READY,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.fusion_critique import (
    FusionCritiqueFinding,
    FusionStoryCritique,
    SEVERITY_CLEAN,
    SEVERITY_MAJOR,
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


def _make_character(cid_letter: str) -> Character:
    char = Character.create(
        name=f"Char-{cid_letter}",
        summary=f"summary {cid_letter}",
        personality=["calm"],
        interests=["coffee"],
        speaking_style="quiet",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    # Force the id so tests can reference it deterministically.
    object.__setattr__(char, "id", f"c-{cid_letter}")
    return char


@dataclass
class _CharServiceStub:
    """Minimal stand-in for ``CharacterService.get_character_entity``."""

    by_id: dict[str, Character]

    async def get_character_entity(self, character_id: str) -> Character | None:
        return self.by_id.get(character_id)


class _ScriptedPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(
        self,
        *,
        prompt: str,  # noqa: ARG002
        briefs,
        previous_outline=None,  # noqa: ARG002
    ) -> FusionOutline:
        self.calls += 1
        focus = tuple(b.character_id for b in briefs)
        beats = [
            FusionBeatPlan.create(
                sequence=i, act=act, title=f"幕{i}",
                hook=f"hook{i}", dramatic_question="",
                target_chars=500, focus_character_ids=focus,
            )
            for i, act in enumerate(
                (ACT_OPENING, ACT_RISING, ACT_TURN, ACT_RESOLUTION),
            )
        ]
        return FusionOutline.create(
            title=f"標題-{self.calls}",
            premise="前提",
            theme="custom",
            beats=beats,
        )


class _ScriptedWriter:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.call_kwargs: list[dict[str, str]] = []

    async def write_beat(
        self,
        *,
        prompt,  # noqa: ARG002
        outline,  # noqa: ARG002
        beat: FusionBeatPlan,
        briefs,  # noqa: ARG002
        previously_summary="",
        previous_tail="",
        regenerate_hint=None,  # noqa: ARG002
    ) -> str:
        self.calls.append(beat.sequence)
        self.call_kwargs.append({
            "previously_summary": previously_summary,
            "previous_tail": previous_tail,
        })
        return (
            f"PROSE-{beat.sequence} 兩人走進場景。"
            f"場景結束時走出門口。"
        )


@dataclass
class _PolishCall:
    draft_text: str
    critique: FusionStoryCritique | None
    round_index: int


class _ScriptedPolisher:
    """Default polisher just tags the draft so tests can see the loop count."""

    def __init__(self) -> None:
        self.calls: list[_PolishCall] = []

    async def polish(
        self,
        *,
        prompt,  # noqa: ARG002
        outline,  # noqa: ARG002
        draft_text: str,
        briefs,  # noqa: ARG002
        critique: FusionStoryCritique | None = None,
        round_index: int = 0,
    ) -> str:
        self.calls.append(_PolishCall(
            draft_text=draft_text,
            critique=critique,
            round_index=round_index,
        ))
        return f"POLISHED[r{round_index}]:{draft_text}"


@dataclass
class _ScriptedCritic:
    """Returns a scripted sequence of critiques, one per call.

    Defaults to a single CLEAN verdict so the loop stops after the first
    polish — keeps the simple tests boring.
    """

    verdicts: list[FusionStoryCritique] = field(
        default_factory=lambda: [FusionStoryCritique.clean()],
    )
    calls: list[dict] = field(default_factory=list)

    async def review(
        self,
        *,
        prompt,  # noqa: ARG002
        outline,  # noqa: ARG002
        draft_text: str,
        briefs,  # noqa: ARG002
        round_index: int = 0,
        previous_critique: FusionStoryCritique | None = None,
    ) -> FusionStoryCritique:
        self.calls.append({
            "draft_text": draft_text,
            "round_index": round_index,
            "previous_critique": previous_critique,
        })
        if self.verdicts:
            return self.verdicts.pop(0)
        return FusionStoryCritique.clean()


def _dirty_critique() -> FusionStoryCritique:
    """Shared factory for "needs another polish round" verdicts.

    Helper for tests that want polish to actually fire under the new
    critic-first loop. Anchorless finding keeps the polisher on the
    whole-rewrite path (no need to construct paragraph indices)."""
    return FusionStoryCritique.create(
        severity=SEVERITY_MAJOR,
        summary="第一段抽象",
        findings=[FusionCritiqueFinding.create(
            kind="抽象", issue="第一段全是形容詞",
        )],
        should_continue=True,
    )


def _service(
    *,
    planner=None, writer=None, polisher=None, critic=None,
):
    repo = InMemoryFusionStoryRepository()
    chars = {
        "c-a": _make_character("a"),
        "c-b": _make_character("b"),
    }
    char_service = _CharServiceStub(by_id=chars)
    return repo, FusionStoryService(
        repository=repo,
        character_service=char_service,  # type: ignore[arg-type]
        brief_builder=FusionCharacterBriefBuilder(memory_repository=None),
        planner=planner or _ScriptedPlanner(),  # type: ignore[arg-type]
        writer=writer or _ScriptedWriter(),  # type: ignore[arg-type]
        polisher=polisher or _ScriptedPolisher(),  # type: ignore[arg-type]
        critic=critic or _ScriptedCritic(),  # type: ignore[arg-type]
    )


async def _await_terminal(service: FusionStoryService, story_id: str) -> None:
    """Drain background tasks until the story lands in a terminal state."""
    for _ in range(200):
        story = await service.get(story_id)
        assert story is not None
        if story.is_terminal():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("pipeline never reached terminal state")


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_create_runs_planner_writer_polisher_in_order(self) -> None:
        # Critic flags one issue on the first pass then declares CLEAN
        # so the polish stage actually fires — otherwise critic-first
        # ordering would short-circuit and we'd never exercise the
        # polisher in this happy-path test.
        dirty = FusionStoryCritique.create(
            severity=SEVERITY_MAJOR,
            summary="抽象描寫",
            findings=[FusionCritiqueFinding.create(
                kind="抽象", issue="第一段全是形容詞",
            )],
            should_continue=True,
        )
        planner, writer, polisher, critic = (
            _ScriptedPlanner(), _ScriptedWriter(), _ScriptedPolisher(),
            _ScriptedCritic(verdicts=[dirty, FusionStoryCritique.clean()]),
        )
        repo, service = _service(
            planner=planner, writer=writer,
            polisher=polisher, critic=critic,
        )
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await _await_terminal(service, story.id)
        final = await service.get(story.id)
        assert final is not None
        assert final.status == STATUS_READY
        assert final.full_text.startswith("POLISHED[r0]:")
        assert writer.calls == [0, 1, 2, 3]
        # Critic-first: dirty → 1 polish → clean → stop.
        assert len(polisher.calls) == 1
        assert polisher.calls[0].critique is dirty
        assert all(b.content.startswith("PROSE-") for b in final.beats)

    @pytest.mark.asyncio
    async def test_create_with_single_character_reaches_ready(self) -> None:
        # C1-5: a solo cast must walk the full pipeline to ``ready`` just
        # like a multi-character story. Default critic returns CLEAN so
        # the polish stage short-circuits and full_text = concatenated
        # beats — the point here is the solo cast is accepted end-to-end.
        _, service = _service()
        story = await service.create(
            character_ids=["c-a"], prompt="獨角戲",
        )
        await _await_terminal(service, story.id)
        final = await service.get(story.id)
        assert final is not None
        assert final.status == STATUS_READY
        assert final.character_ids == ("c-a",)
        assert final.full_text
        assert all(b.content.startswith("PROSE-") for b in final.beats)


class TestIterateBeat:
    @pytest.mark.asyncio
    async def test_rewrites_only_target_beat_and_repolishes(self) -> None:
        # Queue dirty+clean twice so both the initial create() and the
        # iterate_beat() reach the polisher under the critic-first loop.
        dirty = _dirty_critique()
        planner, writer, polisher, critic = (
            _ScriptedPlanner(), _ScriptedWriter(), _ScriptedPolisher(),
            _ScriptedCritic(verdicts=[
                dirty, FusionStoryCritique.clean(),
                dirty, FusionStoryCritique.clean(),
            ]),
        )
        _, service = _service(
            planner=planner, writer=writer,
            polisher=polisher, critic=critic,
        )
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await _await_terminal(service, story.id)

        writer.calls.clear()
        polisher.calls.clear()
        await service.iterate_beat(story.id, beat_index=2, hint="重點放在轉")
        await _await_terminal(service, story.id)

        final = await service.get(story.id)
        assert final is not None
        assert writer.calls == [2]
        assert len(polisher.calls) == 1
        assert final.head_version == 2
        assert len(final.versions) == 1
        assert final.versions[0].iteration_label == "beat_2_regenerate"


class TestIterateOutline:
    @pytest.mark.asyncio
    async def test_outline_iterate_rewrites_all_beats(self) -> None:
        # Same critic-first queueing trick as the iterate_beat test —
        # we need dirty+clean for both pipeline runs.
        dirty = _dirty_critique()
        planner, writer, polisher, critic = (
            _ScriptedPlanner(), _ScriptedWriter(), _ScriptedPolisher(),
            _ScriptedCritic(verdicts=[
                dirty, FusionStoryCritique.clean(),
                dirty, FusionStoryCritique.clean(),
            ]),
        )
        _, service = _service(
            planner=planner, writer=writer,
            polisher=polisher, critic=critic,
        )
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await _await_terminal(service, story.id)

        writer.calls.clear()
        polisher.calls.clear()
        planner_calls_before = planner.calls
        await service.iterate_outline(story.id, hint="改寫成喜劇")
        await _await_terminal(service, story.id)

        final = await service.get(story.id)
        assert final is not None
        assert planner.calls == planner_calls_before + 1
        assert writer.calls == [0, 1, 2, 3]
        assert len(polisher.calls) == 1
        assert final.head_version == 2
        assert "改寫成喜劇" in final.prompt


class TestContinuityWiring:
    """Pin that the orchestrator threads the right continuity signals
    between stages: previous beat's tail flows into the writer, and the
    critic→polish loop terminates correctly on the critic's verdict.
    """

    @pytest.mark.asyncio
    async def test_writer_receives_previous_tail_after_first_beat(self) -> None:
        writer = _ScriptedWriter()
        _, service = _service(writer=writer)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await _await_terminal(service, story.id)
        # First beat: nothing before it.
        assert writer.call_kwargs[0]["previous_tail"] == ""
        # Subsequent beats: the prior beat's prose lands as the tail.
        for kwargs in writer.call_kwargs[1:]:
            assert kwargs["previous_tail"].strip() != ""

    @pytest.mark.asyncio
    async def test_iterate_beat_reconstructs_previous_tail(self) -> None:
        writer = _ScriptedWriter()
        _, service = _service(writer=writer)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await _await_terminal(service, story.id)
        writer.call_kwargs.clear()
        writer.calls.clear()
        await service.iterate_beat(story.id, beat_index=2, hint="重點放在轉")
        await _await_terminal(service, story.id)
        assert len(writer.call_kwargs) == 1
        assert writer.call_kwargs[0]["previous_tail"].strip() != ""


class TestPolishLoop:
    """The critic→polish loop is the readability mechanism — these tests
    pin its termination rules so a regression doesn't silently turn the
    pipeline into a single-shot polish again (or worse, an infinite
    loop). The loop is **critic-first**: we never run polish without
    a critique driving it.
    """

    @pytest.mark.asyncio
    async def test_clean_first_verdict_skips_polish_entirely(self) -> None:
        # Writer output already passes the critic → polish never fires.
        polisher = _ScriptedPolisher()
        critic = _ScriptedCritic(verdicts=[FusionStoryCritique.clean()])
        _, service = _service(polisher=polisher, critic=critic)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="p",
        )
        await _await_terminal(service, story.id)
        assert len(polisher.calls) == 0
        assert len(critic.calls) == 1

    @pytest.mark.asyncio
    async def test_dirty_then_clean_runs_exactly_one_polish(self) -> None:
        # Round 0: critic flags issues → polish runs with that critique.
        # Round 1: critic returns CLEAN → loop stops.
        finding = FusionCritiqueFinding.create(
            kind="抽象", issue="第三段全是形容詞", quote="她感到憂傷",
        )
        dirty = FusionStoryCritique.create(
            severity=SEVERITY_MAJOR,
            summary="第三段需要具體化",
            findings=[finding],
            should_continue=True,
        )
        polisher = _ScriptedPolisher()
        critic = _ScriptedCritic(verdicts=[dirty, FusionStoryCritique.clean()])
        _, service = _service(polisher=polisher, critic=critic)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="p",
        )
        await _await_terminal(service, story.id)
        assert len(polisher.calls) == 1
        # The single polish call received the critique from round 0.
        assert polisher.calls[0].critique is dirty
        assert polisher.calls[0].round_index == 0
        # Two critic calls: the dirty verdict + the follow-up CLEAN.
        assert len(critic.calls) == 2

    @pytest.mark.asyncio
    async def test_loop_respects_should_continue_false(self) -> None:
        # Critic says "issues exist but don't keep polishing" → loop
        # ends without firing the polisher at all.
        finding = FusionCritiqueFinding.create(
            kind="節奏", issue="第二幕過長",
        )
        give_up = FusionStoryCritique.create(
            severity=SEVERITY_MAJOR,
            summary="再修也不會更好",
            findings=[finding],
            should_continue=False,
        )
        polisher = _ScriptedPolisher()
        # The follow-up CLEAN verdict should never be consumed.
        critic = _ScriptedCritic(verdicts=[give_up, FusionStoryCritique.clean()])
        _, service = _service(polisher=polisher, critic=critic)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="p",
        )
        await _await_terminal(service, story.id)
        assert len(polisher.calls) == 0
        assert len(critic.calls) == 1

    @pytest.mark.asyncio
    async def test_loop_obeys_hard_round_cap(self) -> None:
        # Critic always flags issues. Loop must still terminate at
        # _MAX_POLISH_ROUNDS (3). Under critic-first ordering each
        # iteration is critic → (maybe) polish, so 3 iterations gives
        # 3 critics + 3 polishes.
        dirty = FusionStoryCritique.create(
            severity=SEVERITY_MAJOR,
            summary="還有問題",
            findings=[FusionCritiqueFinding.create(
                kind="重複", issue="同一個比喻出現三次",
            )],
            should_continue=True,
        )
        polisher = _ScriptedPolisher()
        critic = _ScriptedCritic(verdicts=[dirty, dirty, dirty, dirty])
        _, service = _service(polisher=polisher, critic=critic)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="p",
        )
        await _await_terminal(service, story.id)
        assert len(polisher.calls) == 3
        assert len(critic.calls) == 3
        # Every polish call carries the dirty critique from its round's
        # critic verdict — confirms critic-first wiring.
        assert all(c.critique is dirty for c in polisher.calls)
        assert [c.round_index for c in polisher.calls] == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_previous_critique_threaded_into_next_critic_call(self) -> None:
        # Round 0's critique must be handed to round 1's critic so it
        # can spot "polisher didn't fix this last round" patterns.
        dirty = _dirty_critique()
        polisher = _ScriptedPolisher()
        critic = _ScriptedCritic(verdicts=[dirty, FusionStoryCritique.clean()])
        _, service = _service(polisher=polisher, critic=critic)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="p",
        )
        await _await_terminal(service, story.id)
        assert len(critic.calls) == 2
        assert critic.calls[0]["previous_critique"] is None
        assert critic.calls[1]["previous_critique"] is dirty


class TestValidationGuards:
    @pytest.mark.asyncio
    async def test_rejects_empty_cast(self) -> None:
        # C1-5 dropped the floor to a single character, but an empty cast
        # is still invalid — the service enforces the ≥1 floor for
        # non-HTTP callers (the DTO mirrors it at the schema edge).
        _, service = _service()
        with pytest.raises(ValueError):
            await service.create(character_ids=[], prompt="提示")

    @pytest.mark.asyncio
    async def test_rejects_too_many_characters(self) -> None:
        _, service = _service()
        with pytest.raises(ValueError):
            await service.create(
                character_ids=[
                    "c-a", "c-b", "c-c", "c-d", "c-e", "c-f",
                ],
                prompt="p",
            )

    @pytest.mark.asyncio
    async def test_rejects_unknown_character(self) -> None:
        _, service = _service()
        with pytest.raises(ValueError):
            await service.create(
                character_ids=["c-a", "ghost"], prompt="p",
            )


class TestFailureMode:
    @pytest.mark.asyncio
    async def test_planner_crash_marks_story_failed(self) -> None:
        class _Boom:
            async def plan(self, **_):
                raise RuntimeError("planner died")

        _, service = _service(planner=_Boom())  # type: ignore[arg-type]
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="p",
        )
        await _await_terminal(service, story.id)
        final = await service.get(story.id)
        assert final is not None
        assert final.status == STATUS_FAILED
        assert final.error_message
