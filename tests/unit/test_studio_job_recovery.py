"""BDD-style coverage for Creator Studio durable generation jobs (C0).

Walks the persistence + recovery contract end to end with in-memory
repositories and scripted LLM stages:

* creating a fusion story / branching drama records a ``running`` job
  and finalizes it (``succeeded`` / ``failed``) when the pipeline ends;
* startup recovery re-drives interrupted jobs from the stage checkpoint
  the pipeline last persisted (planning / writing / polishing for
  fusion; missing tree layers / images for branching) without redoing
  work that already landed;
* recovery gives up after the attempt cap, finalizes jobs whose target
  already reached a terminal state, fails jobs whose target vanished,
  and prunes old finished rows.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.fusion_character_brief import (
    FusionCharacterBriefBuilder,
)
from kokoro_link.application.services.branching_drama_planner import (
    NodeOutline,
)
from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
)
from kokoro_link.application.services.fusion_story_service import (
    FusionStoryService,
)
from kokoro_link.application.services.studio_job_recovery import (
    StudioJobRecoveryService,
)
from kokoro_link.contracts.studio_jobs import (
    JOB_KIND_BRANCHING_CREATE,
    JOB_KIND_FUSION_CREATE,
    JOB_KIND_FUSION_ITERATE_BEAT,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    MAX_JOB_ATTEMPTS,
    StudioGenerationJob,
)
from kokoro_link.domain.entities.branching_drama import (
    STATUS_READY as DRAMA_READY,
    TONE_DARK,
    TONE_NEUTRAL,
    TONE_SUNNY,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import (
    STATUS_FAILED,
    STATUS_POLISHING,
    STATUS_READY,
    STATUS_WRITING,
    FusionStory,
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
from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
    InMemoryBranchingDramaRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_fusion_stories import (
    InMemoryFusionStoryRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_studio_jobs import (
    InMemoryStudioJobRepository,
)


# ── shared stubs ──────────────────────────────────────────────────────


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
    beats = [
        FusionBeatPlan.create(
            sequence=i, act=act, title=f"幕{i}",
            hook=f"hook{i}", dramatic_question="",
            target_chars=500, focus_character_ids=("c-a", "c-b"),
        )
        for i, act in enumerate(
            (ACT_OPENING, ACT_RISING, ACT_TURN, ACT_RESOLUTION),
        )
    ]
    return FusionOutline.create(
        title="標題", premise="前提", theme="custom", beats=beats,
    )


class _ScriptedPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, *, prompt, briefs, previous_outline=None):  # noqa: ARG002
        self.calls += 1
        return _outline()


class _ScriptedWriter:
    def __init__(self, *, fail_sequences: set[int] | None = None) -> None:
        self.calls: list[int] = []
        self._fail_sequences = fail_sequences or set()

    async def write_beat(
        self,
        *,
        prompt,  # noqa: ARG002
        outline,  # noqa: ARG002
        beat: FusionBeatPlan,
        briefs,  # noqa: ARG002
        previously_summary="",  # noqa: ARG002
        previous_tail="",  # noqa: ARG002
        regenerate_hint=None,  # noqa: ARG002
    ) -> str:
        self.calls.append(beat.sequence)
        if beat.sequence in self._fail_sequences:
            raise RuntimeError(f"scripted writer failure seq={beat.sequence}")
        return f"PROSE-{beat.sequence} 內容。"


class _ScriptedPolisher:
    def __init__(self) -> None:
        self.calls = 0

    async def polish(
        self,
        *,
        prompt,  # noqa: ARG002
        outline,  # noqa: ARG002
        draft_text: str,
        briefs,  # noqa: ARG002
        critique=None,  # noqa: ARG002
        round_index: int = 0,
    ) -> str:
        self.calls += 1
        return f"POLISHED[r{round_index}]:{draft_text}"


class _ScriptedCritic:
    async def review(
        self,
        *,
        prompt,  # noqa: ARG002
        outline,  # noqa: ARG002
        draft_text,  # noqa: ARG002
        briefs,  # noqa: ARG002
        round_index=0,  # noqa: ARG002
        previous_critique=None,  # noqa: ARG002
    ) -> FusionStoryCritique:
        return FusionStoryCritique.clean()


class _NotifyRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def notify_studio_story(
        self,
        *,
        user_id: str,
        story_id: str,
        story_title: str,
        succeeded: bool,
        character=None,
    ) -> None:
        self.calls.append({
            "user_id": user_id,
            "story_id": story_id,
            "story_title": story_title,
            "succeeded": succeeded,
            "character": character,
        })


def _fusion_rig(
    *,
    jobs: InMemoryStudioJobRepository | None,
    planner=None,
    writer=None,
    notifications=None,
):
    repo = InMemoryFusionStoryRepository()
    chars = {"c-a": _make_character("a"), "c-b": _make_character("b")}
    planner = planner or _ScriptedPlanner()
    writer = writer or _ScriptedWriter()
    polisher = _ScriptedPolisher()
    service = FusionStoryService(
        repository=repo,
        character_service=_CharServiceStub(by_id=chars),  # type: ignore[arg-type]
        brief_builder=FusionCharacterBriefBuilder(memory_repository=None),
        planner=planner,  # type: ignore[arg-type]
        writer=writer,  # type: ignore[arg-type]
        polisher=polisher,  # type: ignore[arg-type]
        critic=_ScriptedCritic(),  # type: ignore[arg-type]
        jobs=jobs,
        notifications=notifications,
    )
    return repo, service, planner, writer, polisher


async def _await_story_terminal(service: FusionStoryService, story_id: str) -> None:
    for _ in range(300):
        story = await service.get(story_id)
        assert story is not None
        if story.is_terminal():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("fusion pipeline never reached terminal state")


async def _await_job_finished(
    jobs: InMemoryStudioJobRepository, job_id: str,
) -> StudioGenerationJob:
    for _ in range(300):
        job = await jobs.get(job_id)
        assert job is not None
        if job.status != JOB_STATUS_RUNNING:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("job never left running state")


async def _single_job(
    jobs: InMemoryStudioJobRepository,
) -> StudioGenerationJob:
    running = await jobs.list_running()
    if len(running) == 1:
        return running[0]
    raise AssertionError(f"expected exactly one running job, got {running}")


def _story_in_writing(*, filled_beats: int) -> FusionStory:
    """Simulate a service crash mid-``writing``: outline landed, the
    first ``filled_beats`` beats persisted, the process died."""
    story = FusionStory.create_pending(
        character_ids=["c-a", "c-b"], prompt="提示",
    )
    story = story.with_outline(_outline())
    for beat in story.beats[:filled_beats]:
        story = story.with_beat_content(
            beat_id=beat.id, content=f"PRE-{beat.sequence} 既有內容。",
        )
    assert story.status == STATUS_WRITING
    return story


# ── fusion: job bookkeeping around live pipelines ────────────────────


class TestFusionJobBookkeeping:
    @pytest.mark.asyncio
    async def test_create_records_running_job_then_succeeds(self) -> None:
        jobs = InMemoryStudioJobRepository()
        _, service, _, _, _ = _fusion_rig(jobs=jobs)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        job = await _single_job(jobs)
        assert job.kind == JOB_KIND_FUSION_CREATE
        assert job.target_id == story.id
        assert job.attempts == 1

        await _await_story_terminal(service, story.id)
        finished = await _await_job_finished(jobs, job.id)
        assert finished.status == JOB_STATUS_SUCCEEDED

    @pytest.mark.asyncio
    async def test_failed_pipeline_marks_job_failed(self) -> None:
        jobs = InMemoryStudioJobRepository()
        writer = _ScriptedWriter(fail_sequences={1})
        _, service, _, _, _ = _fusion_rig(jobs=jobs, writer=writer)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        job = await _single_job(jobs)
        await _await_story_terminal(service, story.id)
        final = await service.get(story.id)
        assert final is not None and final.status == STATUS_FAILED

        finished = await _await_job_finished(jobs, job.id)
        assert finished.status == JOB_STATUS_FAILED
        assert finished.error_message

    @pytest.mark.asyncio
    async def test_service_without_jobs_repo_behaves_as_before(self) -> None:
        _, service, _, _, _ = _fusion_rig(jobs=None)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await _await_story_terminal(service, story.id)
        final = await service.get(story.id)
        assert final is not None and final.status == STATUS_READY


class TestCompletionNotify:
    @pytest.mark.asyncio
    async def test_success_notifies_with_threaded_user(self) -> None:
        jobs = InMemoryStudioJobRepository()
        notify = _NotifyRecorder()
        _, service, _, _, _ = _fusion_rig(jobs=jobs, notifications=notify)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示", user_id="u-1",
        )
        await _await_story_terminal(service, story.id)
        for _i in range(300):
            if notify.calls:
                break
            await asyncio.sleep(0.01)
        assert len(notify.calls) == 1
        call = notify.calls[0]
        assert call["user_id"] == "u-1"
        assert call["story_id"] == story.id
        assert call["succeeded"] is True
        assert call["character"] is not None

    @pytest.mark.asyncio
    async def test_failure_notifies_failed(self) -> None:
        jobs = InMemoryStudioJobRepository()
        notify = _NotifyRecorder()
        writer = _ScriptedWriter(fail_sequences={0})
        _, service, _, _, _ = _fusion_rig(
            jobs=jobs, writer=writer, notifications=notify,
        )
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示", user_id="u-1",
        )
        await _await_story_terminal(service, story.id)
        for _i in range(300):
            if notify.calls:
                break
            await asyncio.sleep(0.01)
        assert len(notify.calls) == 1
        assert notify.calls[0]["succeeded"] is False

    @pytest.mark.asyncio
    async def test_no_user_id_skips_notify(self) -> None:
        jobs = InMemoryStudioJobRepository()
        notify = _NotifyRecorder()
        _, service, _, _, _ = _fusion_rig(jobs=jobs, notifications=notify)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await _await_story_terminal(service, story.id)
        await asyncio.sleep(0.05)
        assert notify.calls == []


# ── fusion: startup recovery ─────────────────────────────────────────


def _recovery(
    jobs: InMemoryStudioJobRepository,
    *,
    fusion: FusionStoryService | None = None,
    branching: BranchingDramaService | None = None,
) -> StudioJobRecoveryService:
    return StudioJobRecoveryService(
        jobs=jobs,
        fusion_story_service=fusion,
        branching_drama_service=branching,
    )


class TestFusionRecovery:
    @pytest.mark.asyncio
    async def test_resumes_interrupted_writing_and_skips_done_beats(
        self,
    ) -> None:
        jobs = InMemoryStudioJobRepository()
        repo, service, planner, writer, polisher = _fusion_rig(jobs=jobs)
        story = _story_in_writing(filled_beats=2)
        await repo.add(story)
        job = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_CREATE, target_id=story.id, params={},
        )
        await jobs.add(job)

        await _recovery(jobs, fusion=service).recover()
        await _await_story_terminal(service, story.id)

        final = await service.get(story.id)
        assert final is not None
        assert final.status == STATUS_READY
        # Interrupted-run beats were kept, only the missing ones written.
        assert writer.calls == [2, 3]
        assert final.beats[0].content.startswith("PRE-0")
        assert final.beats[1].content.startswith("PRE-1")
        assert final.beats[2].content.startswith("PROSE-2")
        # No replanning — the persisted outline is the checkpoint.
        assert planner.calls == 0

        finished = await _await_job_finished(jobs, job.id)
        assert finished.status == JOB_STATUS_SUCCEEDED
        assert finished.attempts == 2

    @pytest.mark.asyncio
    async def test_planning_stage_reruns_full_pipeline(self) -> None:
        jobs = InMemoryStudioJobRepository()
        repo, service, planner, writer, _ = _fusion_rig(jobs=jobs)
        story = FusionStory.create_pending(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await repo.add(story)
        job = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_CREATE, target_id=story.id, params={},
        )
        await jobs.add(job)

        await _recovery(jobs, fusion=service).recover()
        await _await_story_terminal(service, story.id)

        final = await service.get(story.id)
        assert final is not None and final.status == STATUS_READY
        assert planner.calls == 1
        assert writer.calls == [0, 1, 2, 3]

    @pytest.mark.asyncio
    async def test_polishing_stage_runs_polish_only(self) -> None:
        jobs = InMemoryStudioJobRepository()
        repo, service, planner, writer, polisher = _fusion_rig(jobs=jobs)
        story = _story_in_writing(filled_beats=4)
        story = story.with_status(STATUS_POLISHING)
        await repo.add(story)
        job = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_CREATE, target_id=story.id, params={},
        )
        await jobs.add(job)

        await _recovery(jobs, fusion=service).recover()
        await _await_story_terminal(service, story.id)

        final = await service.get(story.id)
        assert final is not None and final.status == STATUS_READY
        assert planner.calls == 0
        assert writer.calls == []
        assert final.full_text  # polish stage locked in the text

    @pytest.mark.asyncio
    async def test_beat_iteration_job_rewrites_target_beat(self) -> None:
        jobs = InMemoryStudioJobRepository()
        repo, service, planner, writer, _ = _fusion_rig(jobs=jobs)
        story = _story_in_writing(filled_beats=4)
        await repo.add(story)
        job = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_ITERATE_BEAT,
            target_id=story.id,
            params={"beat_index": 1, "hint": "更緊湊"},
        )
        await jobs.add(job)

        await _recovery(jobs, fusion=service).recover()
        await _await_story_terminal(service, story.id)

        final = await service.get(story.id)
        assert final is not None and final.status == STATUS_READY
        assert writer.calls == [1]
        assert planner.calls == 0

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self) -> None:
        jobs = InMemoryStudioJobRepository()
        repo, service, planner, writer, _ = _fusion_rig(jobs=jobs)
        story = _story_in_writing(filled_beats=1)
        await repo.add(story)
        job = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_CREATE, target_id=story.id, params={},
        )
        for _ in range(MAX_JOB_ATTEMPTS - 1):
            job = job.with_attempts(job.attempts + 1)
        await jobs.add(job)

        await _recovery(jobs, fusion=service).recover()

        final = await service.get(story.id)
        assert final is not None
        assert final.status == STATUS_FAILED
        assert final.error_message
        finished = await jobs.get(job.id)
        assert finished is not None
        assert finished.status == JOB_STATUS_FAILED
        assert planner.calls == 0
        assert writer.calls == []

    @pytest.mark.asyncio
    async def test_finalizes_job_for_already_terminal_story(self) -> None:
        jobs = InMemoryStudioJobRepository()
        repo, service, planner, writer, _ = _fusion_rig(jobs=jobs)
        story = _story_in_writing(filled_beats=4).with_full_text("完稿。")
        assert story.status == STATUS_READY
        await repo.add(story)
        job = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_CREATE, target_id=story.id, params={},
        )
        await jobs.add(job)

        await _recovery(jobs, fusion=service).recover()

        finished = await jobs.get(job.id)
        assert finished is not None
        assert finished.status == JOB_STATUS_SUCCEEDED
        assert planner.calls == 0 and writer.calls == []

    @pytest.mark.asyncio
    async def test_fails_job_when_target_missing(self) -> None:
        jobs = InMemoryStudioJobRepository()
        _, service, _, _, _ = _fusion_rig(jobs=jobs)
        job = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_CREATE, target_id="gone", params={},
        )
        await jobs.add(job)

        await _recovery(jobs, fusion=service).recover()

        finished = await jobs.get(job.id)
        assert finished is not None
        assert finished.status == JOB_STATUS_FAILED

    @pytest.mark.asyncio
    async def test_resumes_only_newest_job_per_target(self) -> None:
        """Two running rows for one story (double-click race or a
        transient finalize failure) must not re-drive the pipeline
        twice — only the newest job resumes, older ones are
        superseded."""
        jobs = InMemoryStudioJobRepository()
        repo, service, planner, writer, _ = _fusion_rig(jobs=jobs)
        story = _story_in_writing(filled_beats=2)
        await repo.add(story)
        stale_job = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_CREATE, target_id=story.id, params={},
        )
        object.__setattr__(
            stale_job,
            "created_at",
            stale_job.created_at - timedelta(minutes=5),
        )
        await jobs.add(stale_job)
        newest_job = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_CREATE, target_id=story.id, params={},
        )
        await jobs.add(newest_job)

        report = await _recovery(jobs, fusion=service).recover()
        await _await_story_terminal(service, story.id)

        assert report["superseded"] == 1
        assert report["resumed"] == 1
        superseded = await jobs.get(stale_job.id)
        assert superseded is not None
        assert superseded.status == JOB_STATUS_FAILED
        assert "superseded" in (superseded.error_message or "")
        finished = await _await_job_finished(jobs, newest_job.id)
        assert finished.status == JOB_STATUS_SUCCEEDED
        # The pipeline ran exactly once: only the missing beats written.
        assert writer.calls == [2, 3]

    @pytest.mark.asyncio
    async def test_prunes_old_finished_jobs(self) -> None:
        jobs = InMemoryStudioJobRepository()
        _, service, _, _, _ = _fusion_rig(jobs=jobs)
        old = StudioGenerationJob.create(
            kind=JOB_KIND_FUSION_CREATE, target_id="s1", params={},
        ).with_status(JOB_STATUS_SUCCEEDED)
        stale = datetime.now(timezone.utc) - timedelta(days=90)
        object.__setattr__(old, "updated_at", stale)
        await jobs.add(old)

        await _recovery(jobs, fusion=service).recover()

        assert await jobs.get(old.id) is None


# ── branching drama ──────────────────────────────────────────────────


class _ScriptedDramaPlanner:
    def __init__(self) -> None:
        self.root_calls = 0
        self.children_calls = 0

    async def plan_root(
        self, *, prompt, briefs, total_segments,  # noqa: ARG002
    ) -> tuple[str, NodeOutline]:
        self.root_calls += 1
        return "測試劇場", NodeOutline(
            title="序幕",
            summary="角色們在咖啡廳相遇。",
            appearing_character_ids=tuple(b.character_id for b in briefs),
        )

    async def plan_children(
        self,
        *,
        prompt,  # noqa: ARG002
        briefs,
        parent_summary,  # noqa: ARG002
        path_context,  # noqa: ARG002
        depth,
        total_segments,
    ) -> dict[str, NodeOutline]:
        self.children_calls += 1
        all_ids = tuple(b.character_id for b in briefs)
        suffix = "結局。" if depth == total_segments - 1 else "繼續。"
        return {
            tone: NodeOutline(
                title=f"{tone}-{depth}",
                summary=f"{tone} depth={depth}。{suffix}",
                appearing_character_ids=all_ids,
            )
            for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL)
        }


class _ScriptedDirector:
    async def narrate(self, *, node, briefs, previous_turns, player_input=""):  # noqa: ARG002
        return f"場景敘事：{node.title}"

    async def respond_in_scene(self, *, node, briefs, previous_turns, exchanges, player_input):  # noqa: ARG002
        return f"回應：{player_input}", None

    async def classify_tone(self, *, exchanges, children):  # noqa: ARG002
        return TONE_NEUTRAL


def _branching_rig(*, jobs: InMemoryStudioJobRepository | None):
    repo = InMemoryBranchingDramaRepository()
    chars = {"c-a": _make_character("a"), "c-b": _make_character("b")}
    planner = _ScriptedDramaPlanner()
    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(by_id=chars),  # type: ignore[arg-type]
        brief_builder=FusionCharacterBriefBuilder(memory_repository=None),
        planner=planner,  # type: ignore[arg-type]
        director=_ScriptedDirector(),  # type: ignore[arg-type]
        jobs=jobs,
    )
    return repo, service, planner


async def _await_drama_terminal(
    service: BranchingDramaService, drama_id: str,
) -> None:
    for _ in range(300):
        drama = await service.get(drama_id)
        assert drama is not None
        if drama.is_terminal():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("branching pipeline never reached terminal state")


class TestBranchingJobs:
    @pytest.mark.asyncio
    async def test_create_records_job_and_succeeds(self) -> None:
        jobs = InMemoryStudioJobRepository()
        _, service, _ = _branching_rig(jobs=jobs)
        drama = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示", total_segments=3,
        )
        job = await _single_job(jobs)
        assert job.kind == JOB_KIND_BRANCHING_CREATE
        assert job.target_id == drama.id

        await _await_drama_terminal(service, drama.id)
        finished = await _await_job_finished(jobs, job.id)
        assert finished.status == JOB_STATUS_SUCCEEDED

    @pytest.mark.asyncio
    async def test_recovery_fills_missing_layers_without_duplicating_root(
        self,
    ) -> None:
        jobs = InMemoryStudioJobRepository()
        repo, service, planner = _branching_rig(jobs=jobs)
        # Simulate a crash after the root landed but before layer 1.
        drama = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示", total_segments=3,
        )
        await _await_drama_terminal(service, drama.id)
        ready = await service.get(drama.id)
        assert ready is not None and ready.status == DRAMA_READY
        # Rewind: drop layer-1 children and flip back to generating.
        root = await repo.get_root_node(drama.id)
        assert root is not None
        for child in await repo.get_children(root.id):
            repo._nodes.pop(child.id, None)  # type: ignore[attr-defined]
        await repo.save(ready.with_status("generating_outlines"))
        planner.root_calls = 0
        planner.children_calls = 0
        for existing in await jobs.list_running():
            await jobs.save(existing.with_status(JOB_STATUS_SUCCEEDED))

        job = StudioGenerationJob.create(
            kind=JOB_KIND_BRANCHING_CREATE, target_id=drama.id, params={},
        )
        await jobs.add(job)

        await _recovery(jobs, branching=service).recover()
        await _await_drama_terminal(service, drama.id)

        final = await service.get(drama.id)
        assert final is not None and final.status == DRAMA_READY
        # Root was reused, not regenerated.
        assert planner.root_calls == 0
        assert planner.children_calls >= 1
        roots = await repo.get_nodes_at_depth(drama.id, 0)
        assert len(roots) == 1
        children = await repo.get_children(root.id)
        assert len(children) == 3

        finished = await _await_job_finished(jobs, job.id)
        assert finished.status == JOB_STATUS_SUCCEEDED

    @pytest.mark.asyncio
    async def test_recovery_without_root_reruns_tree(self) -> None:
        jobs = InMemoryStudioJobRepository()
        repo, service, planner = _branching_rig(jobs=jobs)
        from kokoro_link.domain.entities.branching_drama import BranchingDrama

        drama = BranchingDrama.create_pending(
            character_ids=["c-a", "c-b"], prompt="提示", total_segments=3,
        )
        await repo.add(drama)
        job = StudioGenerationJob.create(
            kind=JOB_KIND_BRANCHING_CREATE, target_id=drama.id, params={},
        )
        await jobs.add(job)

        await _recovery(jobs, branching=service).recover()
        await _await_drama_terminal(service, drama.id)

        final = await service.get(drama.id)
        assert final is not None and final.status == DRAMA_READY
        assert planner.root_calls == 1
        roots = await repo.get_nodes_at_depth(drama.id, 0)
        assert len(roots) == 1
