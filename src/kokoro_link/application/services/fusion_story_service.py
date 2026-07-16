"""Fusion-story orchestrator.

Coordinates the four pipeline stages:

    brief builder → planner → per-beat writer → polisher

Each generation operation runs as a background ``asyncio.Task`` so the
HTTP layer can return a 202 immediately and the frontend polls
``GET /fusion-stories/{id}`` to track the ``status`` transition:

    planning → writing → polishing → ready

Iteration ops (``iterate_outline`` / ``iterate_beat`` / ``polish``) all
take the same shape: snapshot the prior head into the version chain,
flip status back to a non-terminal value, and run the relevant subset
of stages in the background. Anything that fails sets ``status =
failed`` + ``error_message`` so the UI can surface a retry hint.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kokoro_link.application.services.notification_service import (
        NotificationService,
    )

from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
    FusionCharacterBriefBuilder,
)
from kokoro_link.application.services.fusion_story_critic import (
    FusionStoryCritic,
)
from kokoro_link.application.services.fusion_story_planner import (
    FusionStoryPlanner,
)
from kokoro_link.application.services.fusion_story_polisher import (
    FusionStoryPolisher,
)
from kokoro_link.application.services.fusion_story_writer import (
    FusionStoryWriter,
)
from kokoro_link.contracts.fusion_story import FusionStoryRepositoryPort
from kokoro_link.contracts.studio_jobs import (
    JOB_KIND_FUSION_CREATE,
    JOB_KIND_FUSION_ITERATE_BEAT,
    JOB_KIND_FUSION_ITERATE_OUTLINE,
    JOB_KIND_FUSION_POLISH,
    JOB_STATUS_FAILED,
    JOB_STATUS_SUCCEEDED,
    MAX_JOB_ATTEMPTS,
    StudioGenerationJob,
    StudioJobRepositoryPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import (
    STATUS_FAILED,
    STATUS_PLANNING,
    STATUS_POLISHING,
    STATUS_READY,
    STATUS_WRITING,
    FusionStory,
    FusionStoryBeat,
    beats_from_snapshot_json,
    outline_from_snapshot_json,
)
from kokoro_link.domain.value_objects.fusion_critique import (
    FusionStoryCritique,
)
from kokoro_link.domain.value_objects.fusion_outline import (
    FusionBeatPlan,
    FusionOutline,
)


_LOGGER = logging.getLogger(__name__)
# Cast floor relaxed 2→1 (Creator Studio C1-5): a fusion short story can
# now star a single character, with a second+ cast member optional. The
# branching-drama creator keeps its own 2–5 floor elsewhere.
_MIN_CHARACTERS = 1
_MAX_CHARACTERS = 5
_PREVIOUSLY_SUMMARY_CHAR_LIMIT = 600
_MAX_POLISH_ROUNDS = 3
"""Hard cap on the critic→polish loop. Round 1 is always a blind polish
on the writer's concatenated output; subsequent rounds run only when
the critic asks for more. Three is enough for the LLM to converge — past
that we're paying tokens for diminishing returns and the orchestrator
just locks in whatever round 3 produced."""


@dataclass(slots=True)
class _PipelineContext:
    """Bundle the per-run inputs the stages share.

    Built once per generation/iterate call so we don't refetch the
    character entities or rebuild the briefs across the four stages.
    """

    story_id: str
    prompt: str
    characters: list[Character]
    briefs: list[CharacterBrief]
    operator_primary_language: str = "zh-TW"


class FusionStoryService:
    def __init__(
        self,
        *,
        repository: FusionStoryRepositoryPort,
        character_service: CharacterService,
        brief_builder: FusionCharacterBriefBuilder,
        planner: FusionStoryPlanner,
        writer: FusionStoryWriter,
        polisher: FusionStoryPolisher,
        critic: FusionStoryCritic,
        jobs: StudioJobRepositoryPort | None = None,
        notifications: "NotificationService | None" = None,
    ) -> None:
        self._repository = repository
        self._character_service = character_service
        self._brief_builder = brief_builder
        self._planner = planner
        self._writer = writer
        self._polisher = polisher
        self._critic = critic
        # Durable job ledger (C0). ``None`` keeps the pre-C0 fire-and-
        # forget behaviour for rigs that don't care about restarts.
        self._jobs = jobs
        # Web-push completion notify (C0 生成體驗). Fires from job
        # finalization so recovery-resumed pipelines notify too.
        self._notifications = notifications
        # Active background tasks per story id — keeping a strong ref
        # is required (asyncio's task registry holds only weak refs and
        # an unobserved exception would silently disappear).
        self._tasks: dict[str, asyncio.Task] = {}
        # Per-story locks gate concurrent iterate operations from a
        # double-clicking operator. The HTTP layer maps that to 409.
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- public read surface ----------------------------------------

    async def get(self, story_id: str) -> FusionStory | None:
        return await self._repository.get(story_id)

    async def list_recent(self, *, limit: int = 50) -> list[FusionStory]:
        return await self._repository.list_recent(limit=limit)

    async def delete(self, story_id: str) -> None:
        await self._repository.delete(story_id)
        self._locks.pop(story_id, None)

    # ---- create -----------------------------------------------------

    async def create(
        self,
        *,
        character_ids: Sequence[str],
        prompt: str,
        operator_primary_language: str = "zh-TW",
        user_id: str | None = None,
    ) -> FusionStory:
        """Persist the pending row and kick off the full pipeline.

        Returns the story in ``planning`` state so the HTTP layer can
        respond immediately with the id; the actual prose lands via
        repository updates on subsequent polls.
        """
        characters = await self._resolve_characters(character_ids)
        if len(characters) < _MIN_CHARACTERS:
            raise ValueError(
                f"fusion story needs at least {_MIN_CHARACTERS} characters",
            )
        if len(characters) > _MAX_CHARACTERS:
            raise ValueError(
                f"fusion story accepts at most {_MAX_CHARACTERS} characters",
            )
        story = FusionStory.create_pending(
            character_ids=[c.id for c in characters],
            prompt=prompt,
        )
        await self._repository.add(story)

        ctx = _PipelineContext(
            story_id=story.id,
            prompt=story.prompt,
            characters=characters,
            briefs=await self._brief_builder.build_many(characters),
            operator_primary_language=operator_primary_language,
        )
        await self._track_and_spawn(
            kind=JOB_KIND_FUSION_CREATE,
            target_id=story.id,
            params={
                "operator_primary_language": operator_primary_language,
                "user_id": user_id,
            },
            runner=self._run_full_pipeline(ctx),
        )
        return story

    # ---- iterate ----------------------------------------------------

    async def iterate_outline(
        self,
        story_id: str,
        *,
        hint: str | None = None,
        operator_primary_language: str = "zh-TW",
        user_id: str | None = None,
    ) -> FusionStory:
        """Re-plan the outline + auto-rewrite all beats + polish.

        Snapshots the prior head into the version chain so the operator
        can diff or rollback later. Reuses the original
        ``character_ids`` — fusion stories don't support cast changes
        post-creation (the briefs would diverge).
        """
        story = await self._require_terminal(story_id)
        characters = await self._resolve_characters(story.character_ids)
        new_prompt = _merge_prompt(story.prompt, hint)
        snapshot = story.snapshot_version(label="outline_regenerate")
        snapshot = snapshot.with_status(
            STATUS_PLANNING, error_message=None,
        )
        # Update prompt so future iterations carry forward the merged
        # direction; create_pending normalised it on first save.
        snapshot = _replace_prompt(snapshot, new_prompt)
        await self._repository.save(snapshot)

        ctx = _PipelineContext(
            story_id=story.id,
            prompt=new_prompt,
            characters=characters,
            briefs=await self._brief_builder.build_many(characters),
            operator_primary_language=operator_primary_language,
        )
        await self._track_and_spawn(
            kind=JOB_KIND_FUSION_ITERATE_OUTLINE,
            target_id=story.id,
            params={
                "hint": hint,
                "operator_primary_language": operator_primary_language,
                "user_id": user_id,
            },
            runner=self._run_full_pipeline(
                ctx, previous_outline=story.outline,
            ),
        )
        return snapshot

    async def iterate_beat(
        self,
        story_id: str,
        *,
        beat_index: int,
        hint: str | None = None,
        operator_primary_language: str = "zh-TW",
        user_id: str | None = None,
    ) -> FusionStory:
        """Rewrite a single beat + repolish; outline preserved."""
        story = await self._require_terminal(story_id)
        if story.outline is None:
            raise ValueError(
                "cannot iterate beat on a story without an outline",
            )
        if beat_index < 0 or beat_index >= len(story.beats):
            raise ValueError(
                f"beat_index {beat_index} out of range",
            )
        characters = await self._resolve_characters(story.character_ids)
        snapshot = story.snapshot_version(
            label=f"beat_{beat_index}_regenerate",
        )
        snapshot = snapshot.with_status(
            STATUS_WRITING, error_message=None,
        )
        await self._repository.save(snapshot)

        ctx = _PipelineContext(
            story_id=story.id,
            prompt=story.prompt,
            characters=characters,
            briefs=await self._brief_builder.build_many(characters),
            operator_primary_language=operator_primary_language,
        )
        await self._track_and_spawn(
            kind=JOB_KIND_FUSION_ITERATE_BEAT,
            target_id=story.id,
            params={
                "beat_index": beat_index,
                "hint": hint,
                "operator_primary_language": operator_primary_language,
                "user_id": user_id,
            },
            runner=self._run_beat_iteration(
                ctx,
                beat_index=beat_index,
                hint=hint,
            ),
        )
        return snapshot

    async def iterate_polish(
        self,
        story_id: str,
        *,
        operator_primary_language: str = "zh-TW",
        user_id: str | None = None,
    ) -> FusionStory:
        """Re-run only the polish stage on the existing beats."""
        story = await self._require_terminal(story_id)
        if not story.beats:
            raise ValueError(
                "cannot polish a story with no beats",
            )
        characters = await self._resolve_characters(story.character_ids)
        snapshot = story.snapshot_version(label="polish")
        snapshot = snapshot.with_status(
            STATUS_POLISHING, error_message=None,
        )
        await self._repository.save(snapshot)

        ctx = _PipelineContext(
            story_id=story.id,
            prompt=story.prompt,
            characters=characters,
            briefs=await self._brief_builder.build_many(characters),
            operator_primary_language=operator_primary_language,
        )
        await self._track_and_spawn(
            kind=JOB_KIND_FUSION_POLISH,
            target_id=story.id,
            params={
                "operator_primary_language": operator_primary_language,
                "user_id": user_id,
            },
            runner=self._run_polish_only(ctx),
        )
        return snapshot

    # ---- restore (C0-6 版本回溯) --------------------------------------

    async def restore_version(
        self,
        story_id: str,
        *,
        version_number: int,
    ) -> FusionStory:
        """Point the head back at an earlier version — pure data op.

        Snapshots the current head first so the chain keeps both
        directions (the restore itself becomes a new head; nothing is
        deleted and the operator can move forward again). No LLM call,
        no job row, returns synchronously."""
        story = await self._require_terminal(story_id)
        version = next(
            (
                v for v in story.versions
                if v.version_number == version_number
            ),
            None,
        )
        if version is None:
            raise KeyError(
                f"version {version_number} not found on story {story_id}",
            )
        beats = beats_from_snapshot_json(version.beats_json)
        if not version.full_text.strip() and not any(
            beat.content.strip() for beat in beats
        ):
            raise ValueError(
                "version has no restorable content",
            )
        outline = outline_from_snapshot_json(version.outline_json)
        snapshot = story.snapshot_version(
            label=f"restore_v{version_number}",
        )
        restored = snapshot.restored_from(
            version, outline=outline, beats=beats,
        )
        await self._repository.save(restored)
        return restored

    # ---- internal: pipeline runners --------------------------------

    async def _run_full_pipeline(
        self,
        ctx: _PipelineContext,
        *,
        previous_outline: FusionOutline | None = None,
    ) -> None:
        async with self._lock_for(ctx.story_id):
            try:
                await self._stage_plan(
                    ctx, previous_outline=previous_outline,
                )
                await self._stage_write_all(ctx)
                await self._stage_polish(ctx)
            except _PipelineAbort as abort:
                _LOGGER.warning(
                    "fusion pipeline aborted story=%s reason=%s",
                    ctx.story_id, abort.reason,
                )
                await self._mark_failed(ctx.story_id, reason=abort.reason)
            except Exception:
                _LOGGER.exception(
                    "fusion pipeline crashed story=%s", ctx.story_id,
                )
                await self._mark_failed(
                    ctx.story_id, reason="pipeline crashed",
                )

    async def _run_beat_iteration(
        self,
        ctx: _PipelineContext,
        *,
        beat_index: int,
        hint: str | None,
    ) -> None:
        async with self._lock_for(ctx.story_id):
            try:
                await self._stage_rewrite_beat(
                    ctx, beat_index=beat_index, hint=hint,
                )
                await self._stage_polish(ctx)
            except _PipelineAbort as abort:
                _LOGGER.warning(
                    "fusion beat iteration aborted story=%s reason=%s",
                    ctx.story_id, abort.reason,
                )
                await self._mark_failed(ctx.story_id, reason=abort.reason)
            except Exception:
                _LOGGER.exception(
                    "fusion beat iteration crashed story=%s", ctx.story_id,
                )
                await self._mark_failed(
                    ctx.story_id, reason="iteration crashed",
                )

    async def _run_polish_only(self, ctx: _PipelineContext) -> None:
        async with self._lock_for(ctx.story_id):
            try:
                await self._stage_polish(ctx)
            except _PipelineAbort as abort:
                await self._mark_failed(ctx.story_id, reason=abort.reason)
            except Exception:
                _LOGGER.exception(
                    "fusion polish crashed story=%s", ctx.story_id,
                )
                await self._mark_failed(
                    ctx.story_id, reason="polish crashed",
                )

    async def _run_write_and_polish(self, ctx: _PipelineContext) -> None:
        """Resume runner for a pipeline interrupted mid-``writing``.

        The persisted outline is the checkpoint — ``_stage_write_all``
        skips beats whose prose already landed, so only the missing
        beats cost LLM calls before the polish stage locks in the text.
        """
        async with self._lock_for(ctx.story_id):
            try:
                await self._stage_write_all(ctx)
                await self._stage_polish(ctx)
            except _PipelineAbort as abort:
                _LOGGER.warning(
                    "fusion resume aborted story=%s reason=%s",
                    ctx.story_id, abort.reason,
                )
                await self._mark_failed(ctx.story_id, reason=abort.reason)
            except Exception:
                _LOGGER.exception(
                    "fusion resume crashed story=%s", ctx.story_id,
                )
                await self._mark_failed(
                    ctx.story_id, reason="pipeline crashed",
                )

    # ---- internal: durable job ledger (C0) ---------------------------

    async def _track_and_spawn(
        self,
        *,
        kind: str,
        target_id: str,
        params: dict,
        runner,
    ) -> None:
        """Record a durable job row, then spawn the pipeline.

        Job bookkeeping is strictly fail-soft: if the ledger is absent
        or errors out, the pipeline still runs exactly as before C0 —
        losing restart durability must never cost a live generation.
        """
        job: StudioGenerationJob | None = None
        if self._jobs is not None:
            try:
                job = StudioGenerationJob.create(
                    kind=kind,
                    target_id=target_id,
                    params=params,
                )
                await self._jobs.add(job)
            except Exception:
                _LOGGER.exception(
                    "fusion: could not record studio job story=%s",
                    target_id,
                )
                job = None
        if job is None:
            self._spawn(runner)
        else:
            self._spawn(self._run_tracked(job, runner))

    async def _run_tracked(
        self, job: StudioGenerationJob, runner,
    ) -> None:
        await runner
        await self._finalize_job(job)

    async def _finalize_job(self, job: StudioGenerationJob) -> None:
        """Flip the job row to its terminal status from the story state.

        Runners swallow their own exceptions and persist ``failed`` on
        the story, so the story status after the runner returns is the
        single source of truth for the job outcome."""
        if self._jobs is None:
            return
        try:
            story = await self._repository.get(job.target_id)
            if story is None:
                updated = job.with_status(
                    JOB_STATUS_FAILED, error_message="target deleted",
                )
            elif story.status == STATUS_READY:
                updated = job.with_status(JOB_STATUS_SUCCEEDED)
            elif story.status == STATUS_FAILED:
                updated = job.with_status(
                    JOB_STATUS_FAILED,
                    error_message=story.error_message,
                )
            else:
                updated = job.with_status(
                    JOB_STATUS_FAILED,
                    error_message=(
                        f"pipeline ended non-terminal ({story.status})"
                    ),
                )
            await self._jobs.save(updated)
        except Exception:
            _LOGGER.exception(
                "fusion: could not finalize studio job id=%s", job.id,
            )
            return
        await self._notify_outcome(job, story)

    async def _notify_outcome(
        self, job: StudioGenerationJob, story: FusionStory | None,
    ) -> None:
        """C0 完成通知 — web push on terminal transitions, fail-soft."""
        if (
            self._notifications is None
            or story is None
            or not story.is_terminal()
        ):
            return
        user_id = dict(job.params).get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            return
        try:
            character = None
            if story.character_ids:
                character = await (
                    self._character_service.get_character_entity(
                        story.character_ids[0],
                    )
                )
            await self._notifications.notify_studio_story(
                user_id=user_id,
                story_id=story.id,
                story_title=story.title,
                succeeded=story.status == STATUS_READY,
                character=character,
            )
        except Exception:
            _LOGGER.exception(
                "fusion: completion notify failed story=%s", story.id,
            )

    async def resume_job(self, job: StudioGenerationJob) -> str:
        """Re-drive an interrupted job found at startup.

        Returns ``"resumed"`` / ``"finalized"`` / ``"failed"`` so the
        recovery service can log an honest summary."""
        if self._jobs is None:
            return "failed"
        story = await self._repository.get(job.target_id)
        if story is None:
            await self._jobs.save(job.with_status(
                JOB_STATUS_FAILED, error_message="target missing",
            ))
            return "failed"
        if story.is_terminal():
            await self._finalize_job(job)
            return "finalized"
        if job.attempts >= MAX_JOB_ATTEMPTS:
            await self._mark_failed(
                story.id, reason="generation interrupted repeatedly",
            )
            await self._jobs.save(job.with_status(
                JOB_STATUS_FAILED,
                error_message="attempt limit reached",
            ))
            return "failed"
        try:
            characters = await self._resolve_characters(
                story.character_ids,
            )
        except ValueError as exc:
            await self._mark_failed(story.id, reason=str(exc))
            await self._jobs.save(job.with_status(
                JOB_STATUS_FAILED, error_message=str(exc),
            ))
            return "failed"

        job = job.with_attempts(job.attempts + 1)
        await self._jobs.save(job)
        params = dict(job.params)
        ctx = _PipelineContext(
            story_id=story.id,
            prompt=story.prompt,
            characters=characters,
            briefs=await self._brief_builder.build_many(characters),
            operator_primary_language=str(
                params.get("operator_primary_language") or "zh-TW",
            ),
        )
        runner = self._resume_runner(job, ctx, story)
        if runner is None:
            await self._mark_failed(
                story.id, reason="generation interrupted",
            )
            await self._jobs.save(job.with_status(
                JOB_STATUS_FAILED,
                error_message=f"no resume path for kind={job.kind}",
            ))
            return "failed"
        self._spawn(self._run_tracked(job, runner))
        return "resumed"

    def _resume_runner(
        self,
        job: StudioGenerationJob,
        ctx: _PipelineContext,
        story: FusionStory,
    ):
        """Pick the cheapest runner that completes the interrupted job.

        Full-pipeline kinds dispatch on the persisted stage checkpoint;
        targeted kinds re-run their own operation."""
        params = dict(job.params)
        if job.kind == JOB_KIND_FUSION_ITERATE_BEAT:
            beat_index = params.get("beat_index")
            if (
                not isinstance(beat_index, int)
                or beat_index < 0
                or beat_index >= len(story.beats)
            ):
                return None
            hint = params.get("hint")
            return self._run_beat_iteration(
                ctx,
                beat_index=beat_index,
                hint=hint if isinstance(hint, str) else None,
            )
        if job.kind == JOB_KIND_FUSION_POLISH:
            return self._run_polish_only(ctx)
        if job.kind in (
            JOB_KIND_FUSION_CREATE, JOB_KIND_FUSION_ITERATE_OUTLINE,
        ):
            if story.status == STATUS_PLANNING:
                return self._run_full_pipeline(
                    ctx, previous_outline=story.outline,
                )
            if story.status == STATUS_WRITING:
                return self._run_write_and_polish(ctx)
            if story.status == STATUS_POLISHING:
                return self._run_polish_only(ctx)
        return None

    # ---- internal: stage implementations ---------------------------

    async def _stage_plan(
        self,
        ctx: _PipelineContext,
        *,
        previous_outline: FusionOutline | None,
    ) -> None:
        outline = await self._plan_with_language(
            prompt=ctx.prompt,
            briefs=ctx.briefs,
            previous_outline=previous_outline,
            operator_primary_language=ctx.operator_primary_language,
        )
        story = await self._must_load(ctx.story_id)
        story = story.with_outline(outline)
        await self._repository.save(story)

    async def _stage_write_all(self, ctx: _PipelineContext) -> None:
        story = await self._must_load(ctx.story_id)
        outline = story.outline
        if outline is None:
            raise _PipelineAbort("missing outline")
        # Track newly-written beats so each subsequent beat receives an
        # accurate ``previously_summary``. ``last_tail`` carries the
        # raw closing prose of the previous beat so the next beat can
        # land a real 承接. Cross-beat repetition + abstract drift are
        # caught downstream by the critic→polish loop, not per-beat.
        running_summary_parts: list[str] = []
        last_tail: str = ""
        for plan in outline.beats:
            beat_id = _find_beat_id(story.beats, plan)
            existing = _beat_content(story.beats, beat_id)
            if existing.strip():
                # Checkpoint resume: this beat already persisted before
                # an interruption — keep it and only thread its summary
                # / tail forward so later beats still 承接 correctly.
                # Fresh runs never hit this (``with_outline`` creates
                # empty shells).
                running_summary_parts.append(
                    _summarise_beat(plan, existing),
                )
                last_tail = _extract_tail(existing)
                continue
            content = await self._write_beat_with_language(
                prompt=ctx.prompt,
                outline=outline,
                beat=plan,
                briefs=ctx.briefs,
                previously_summary=_compose_summary(running_summary_parts),
                previous_tail=last_tail,
                operator_primary_language=ctx.operator_primary_language,
            )
            running_summary_parts.append(_summarise_beat(plan, content))
            last_tail = _extract_tail(content)
            story = await self._must_load(ctx.story_id)
            story = story.with_beat_content(beat_id=beat_id, content=content)
            await self._repository.save(story)

    async def _stage_rewrite_beat(
        self,
        ctx: _PipelineContext,
        *,
        beat_index: int,
        hint: str | None,
    ) -> None:
        story = await self._must_load(ctx.story_id)
        outline = story.outline
        if outline is None or not story.beats:
            raise _PipelineAbort("missing outline")
        if beat_index >= len(story.beats):
            raise _PipelineAbort("beat_index out of range")

        target_beat = story.beats[beat_index]
        plan = _find_plan(outline, target_beat)
        if plan is None:
            raise _PipelineAbort("beat plan missing for index")

        previously_parts: list[str] = []
        prior_contents: list[str] = []
        for prior in story.beats[:beat_index]:
            prior_plan = _find_plan(outline, prior)
            if prior_plan is None or not prior.content.strip():
                continue
            previously_parts.append(
                _summarise_beat(prior_plan, prior.content),
            )
            prior_contents.append(prior.content)

        previous_tail = (
            _extract_tail(prior_contents[-1]) if prior_contents else ""
        )

        content = await self._write_beat_with_language(
            prompt=ctx.prompt,
            outline=outline,
            beat=plan,
            briefs=ctx.briefs,
            previously_summary=_compose_summary(previously_parts),
            previous_tail=previous_tail,
            regenerate_hint=hint,
            operator_primary_language=ctx.operator_primary_language,
        )
        story = await self._must_load(ctx.story_id)
        story = story.with_beat_content(
            beat_id=target_beat.id, content=content,
        )
        await self._repository.save(story)

    async def _stage_polish(self, ctx: _PipelineContext) -> None:
        story = await self._must_load(ctx.story_id)
        if story.outline is None or not story.beats:
            raise _PipelineAbort("missing outline or beats")
        outline = story.outline
        story = story.with_status(STATUS_POLISHING, error_message=None)
        await self._repository.save(story)

        # Critic-first loop. We read what the writer produced, ask the
        # critic if anything needs fixing, then run polish (spot or
        # whole, dispatched by the polisher based on the findings). If
        # the writer's output is already clean we skip polish entirely
        # and just lock in the concatenation. Hard cap prevents runaway
        # rounds when the critic + polisher keep arguing.
        draft_text = _concat_beats(story.beats)
        critique: FusionStoryCritique | None = None
        for round_i in range(_MAX_POLISH_ROUNDS):
            critique = await self._critic.review(
                prompt=ctx.prompt,
                outline=outline,
                draft_text=draft_text,
                briefs=ctx.briefs,
                round_index=round_i,
                previous_critique=critique,
            )
            if not critique.has_issues() or not critique.should_continue:
                break
            draft_text = await self._polish_with_language(
                prompt=ctx.prompt,
                outline=outline,
                draft_text=draft_text,
                briefs=ctx.briefs,
                critique=critique,
                round_index=round_i,
                operator_primary_language=ctx.operator_primary_language,
            )

        story = await self._must_load(ctx.story_id)
        story = story.with_full_text(draft_text)
        await self._repository.save(story)

    async def _plan_with_language(
        self,
        *,
        prompt: str,
        briefs: Sequence[CharacterBrief],
        previous_outline: FusionOutline | None,
        operator_primary_language: str,
    ) -> FusionOutline:
        try:
            return await self._planner.plan(
                prompt=prompt,
                briefs=briefs,
                previous_outline=previous_outline,
                operator_primary_language=operator_primary_language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._planner.plan(
                prompt=prompt,
                briefs=briefs,
                previous_outline=previous_outline,
            )

    async def _write_beat_with_language(
        self,
        *,
        prompt: str,
        outline: FusionOutline,
        beat: FusionBeatPlan,
        briefs: Sequence[CharacterBrief],
        previously_summary: str,
        previous_tail: str,
        operator_primary_language: str,
        regenerate_hint: str | None = None,
    ) -> str:
        try:
            return await self._writer.write_beat(
                prompt=prompt,
                outline=outline,
                beat=beat,
                briefs=briefs,
                previously_summary=previously_summary,
                previous_tail=previous_tail,
                regenerate_hint=regenerate_hint,
                operator_primary_language=operator_primary_language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._writer.write_beat(
                prompt=prompt,
                outline=outline,
                beat=beat,
                briefs=briefs,
                previously_summary=previously_summary,
                previous_tail=previous_tail,
                regenerate_hint=regenerate_hint,
            )

    async def _polish_with_language(
        self,
        *,
        prompt: str,
        outline: FusionOutline,
        draft_text: str,
        briefs: Sequence[CharacterBrief],
        critique: FusionStoryCritique | None,
        round_index: int,
        operator_primary_language: str,
    ) -> str:
        try:
            return await self._polisher.polish(
                prompt=prompt,
                outline=outline,
                draft_text=draft_text,
                briefs=briefs,
                critique=critique,
                round_index=round_index,
                operator_primary_language=operator_primary_language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._polisher.polish(
                prompt=prompt,
                outline=outline,
                draft_text=draft_text,
                briefs=briefs,
                critique=critique,
                round_index=round_index,
            )

    # ---- internal: helpers ------------------------------------------

    async def _resolve_characters(
        self, character_ids: Sequence[str],
    ) -> list[Character]:
        """Fetch entities + reject empty / unknown / duplicate ids."""
        seen: set[str] = set()
        ordered: list[str] = []
        for cid in character_ids:
            cleaned = (cid or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            ordered.append(cleaned)
        out: list[Character] = []
        missing: list[str] = []
        for cid in ordered:
            entity = await self._character_service.get_character_entity(cid)
            if entity is None:
                missing.append(cid)
                continue
            out.append(entity)
        if missing:
            raise ValueError(
                "fusion story: unknown character ids: " + ", ".join(missing),
            )
        return out

    async def _require_terminal(self, story_id: str) -> FusionStory:
        story = await self._repository.get(story_id)
        if story is None:
            raise ValueError(f"fusion story {story_id} not found")
        if not story.is_terminal():
            raise ValueError(
                f"fusion story {story_id} is busy (status={story.status})",
            )
        return story

    async def _must_load(self, story_id: str) -> FusionStory:
        story = await self._repository.get(story_id)
        if story is None:
            raise _PipelineAbort(f"story {story_id} disappeared mid-pipeline")
        return story

    async def _mark_failed(self, story_id: str, *, reason: str) -> None:
        try:
            story = await self._repository.get(story_id)
        except Exception:
            _LOGGER.exception(
                "fusion: could not load story to mark failed id=%s",
                story_id,
            )
            return
        if story is None:
            return
        try:
            await self._repository.save(
                story.with_status(STATUS_FAILED, error_message=reason),
            )
        except Exception:
            _LOGGER.exception(
                "fusion: could not persist failed status id=%s", story_id,
            )

    def _spawn(self, coro) -> None:
        """Run ``coro`` as a tracked background task."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop in this context (sync test path) — execute eagerly
            # via a fresh loop so callers still see the final state.
            asyncio.run(coro)
            return
        task = loop.create_task(coro)
        self._tasks[id(task)] = task
        task.add_done_callback(lambda t: self._tasks.pop(id(t), None))

    def _lock_for(self, story_id: str) -> asyncio.Lock:
        lock = self._locks.get(story_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[story_id] = lock
        return lock


# --- module-level helpers --------------------------------------------


class _PipelineAbort(Exception):
    """Recoverable abort — orchestrator marks story failed and stops."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _concat_beats(beats: Sequence[FusionStoryBeat]) -> str:
    """Join the per-beat writer output into a single seed draft for the
    polish loop. Sorted by sequence so out-of-order persistence (which
    shouldn't happen but is cheap to defend against) still produces the
    right reading order."""
    parts: list[str] = []
    for beat in sorted(beats, key=lambda b: b.sequence):
        text = (beat.content or "").strip()
        if not text:
            continue
        parts.append(text)
    return "\n\n".join(parts)


def _find_beat_id(
    beats: Sequence[FusionStoryBeat], plan: FusionBeatPlan,
) -> str:
    for beat in beats:
        if beat.sequence == plan.sequence:
            return beat.id
    raise _PipelineAbort(
        f"beat shell missing for sequence {plan.sequence}",
    )


def _beat_content(
    beats: Sequence[FusionStoryBeat], beat_id: str,
) -> str:
    for beat in beats:
        if beat.id == beat_id:
            return beat.content or ""
    return ""


def _find_plan(
    outline: FusionOutline, beat: FusionStoryBeat,
) -> FusionBeatPlan | None:
    for plan in outline.beats:
        if plan.sequence == beat.sequence:
            return plan
    return None


def _summarise_beat(plan: FusionBeatPlan, content: str) -> str:
    """Compress a written beat into a context line for the next stage.

    Stays bounded so we don't blow the context window across 4+ beats:
    structural label + hook + (optional) entry/exit state from the
    outline + first 200 chars of prose. The *tail* prose is handed to
    the next beat separately via :func:`_extract_tail`; this summary
    is the high-level scaffolding."""
    preview = content.strip().replace("\n", " ")[:200]
    parts = [
        f"[第 {plan.sequence + 1} 幕｜{plan.act}｜{plan.title}] hook：{plan.hook}",
    ]
    if plan.entry_state:
        parts.append(f"開場狀態：{plan.entry_state}")
    if plan.exit_state:
        parts.append(f"結束狀態：{plan.exit_state}")
    parts.append(f"prose 摘錄：{preview}")
    return "；".join(parts)


_TAIL_CHAR_LIMIT = 280
"""Length of the raw closing prose fed to the next beat as the
承接 anchor. ~280 chars is roughly the final paragraph of a 600-char
beat — enough for the next beat to see the actual closing sentence
without paying for the whole act."""


def _extract_tail(content: str) -> str:
    """Return the trailing chunk of a beat for the next beat's prompt.

    We prefer to cut on a paragraph boundary so the LLM gets a clean
    closing scene; if the beat is single-paragraph we just slice the
    last N chars.
    """
    text = (content or "").strip()
    if not text:
        return ""
    if len(text) <= _TAIL_CHAR_LIMIT:
        return text
    # Try the last paragraph first.
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if paragraphs:
        last = paragraphs[-1].strip()
        if len(last) <= _TAIL_CHAR_LIMIT:
            return last
        return last[-_TAIL_CHAR_LIMIT:]
    return text[-_TAIL_CHAR_LIMIT:]


def _compose_summary(parts: Sequence[str]) -> str:
    """Join previously-written beat summaries within a char budget."""
    if not parts:
        return ""
    joined = "\n".join(parts)
    if len(joined) <= _PREVIOUSLY_SUMMARY_CHAR_LIMIT:
        return joined
    # Keep the latest beats — they're the closest context for the new
    # beat. Drop oldest entries until under budget.
    out = list(parts)
    while out and len("\n".join(out)) > _PREVIOUSLY_SUMMARY_CHAR_LIMIT:
        out.pop(0)
    return "\n".join(out)


def _merge_prompt(original: str, hint: str | None) -> str:
    if not hint or not hint.strip():
        return original
    extra = hint.strip()
    if extra in original:
        return original
    return f"{original}\n[重新規劃補充] {extra}"


def _replace_prompt(story: FusionStory, prompt: str) -> FusionStory:
    """Tiny helper to keep the prompt mutation in one place.

    ``FusionStory`` is frozen so we use ``dataclasses.replace`` directly
    rather than adding another ``with_*`` method for a one-shot use case.
    """
    from dataclasses import replace

    return replace(story, prompt=prompt.strip())
