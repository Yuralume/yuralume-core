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

**Action-level billing (AP2 second wave).** Under a hosted
``billing_shape=action_fixed`` tier the whole pipeline is one player action:
``fusion_story`` for a create, ``fusion_story_iterate`` for each re-run. The
charge is raised *before* the job is spawned, so an out-of-credits refusal or a
moved price reaches the player as a synchronous 402/409 instead of surfacing
minutes later as a failed job; the resulting interaction scope wraps the
background runner so every stage's Gateway call is attributed to that one
charge; and the charge is closed from the same place the job row is finalized,
because the story's terminal status is the only honest signal of whether the
player got what they paid for. The charge ids ride along in the job params, so
a process that dies mid-pipeline can pick the same charge back up at recovery
instead of stranding the player's credits.

Three rules keep that lifecycle honest, and each of them is a failure mode we
have already paid for once:

1. **State before money.** A terminal job row is written first and the charge
   closed only once it lands. Money closed against a row that stayed
   ``running`` is money decided twice, once here and once by the next recovery
   pass.
2. **A lost lease closes nothing.** Losing the target's lease means another
   replica is generating this story; settling would bill for its work and
   releasing would refund it mid-flight. The row keeps the charge ids, and
   whoever finalizes the row closes the charge. The single exception is a run
   whose job row was never written at all — nobody can inherit a charge that
   exists only in this process's memory, so that one closes here.
3. **A rebuilt handle never guesses.** A handle reconstructed from job params
   has an empty usage record no matter what the dead process consumed, *and*
   the story it finalizes may have been produced by a different charge
   entirely. It therefore asserts nothing — every outcome goes to the User
   service's ``settle_if_probed`` verdict, which answers from the covered-call
   probe that actually names this charge id.
4. **One row transition, one verdict on the money.** The terminal write is a
   compare-and-swap on ``running``; only the caller that wins it closes the
   charge. Recovery, a late in-process finalize and ``supersede_job`` all race
   for the same row with handles that cannot see each other, so the row is the
   only place that race can be settled once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kokoro_link.application.services.notification_service import (
        NotificationService,
    )

from kokoro_link.application.services.character_activity_anchor import (
    CharacterActivityAnchor,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.cloud_action_billing_service import (
    ActionChargeHandle,
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.application.services.studio_execution_lease import (
    LEASE_ABANDONED,
    StudioExecutionLease,
    StudioLeaseLost,
    StudioLeaseSession,
)
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
from kokoro_link.application.services.studio_failure import (
    failure_error_code,
)
from kokoro_link.application.services.fusion_story_writer import (
    FusionStoryWriter,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_FUSION_STORY,
    ACTION_FUSION_STORY_ITERATE,
    client_quoted_price,
)
from kokoro_link.contracts.fusion_story import FusionStoryRepositoryPort
from kokoro_link.contracts.interaction_context import interaction_scope
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
_LOAD_FAILED = object()
"""Sentinel for "the story could not be read", distinct from "deleted".

A deleted story is a decision (refund, fail the job); an unreadable one is an
outage, and treating the two alike would refund stories that are alive and
well the next time the database answers."""

_CHARGE_PARAM_KEYS = ("charge_id", "interaction_id", "action_key")
"""Job-param keys that let a restarted process re-attach the action charge."""

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
        execution_lease: StudioExecutionLease | None = None,
        lease_heartbeat_interval_seconds: float | None = None,
        action_billing: (
            CloudActionBillingService | NullActionBillingService | None
        ) = None,
        # NF4 — a fusion create / iterate is a paid foreground press naming
        # this cast, so it moves their foreground-interaction anchor for the
        # same reason a 分歧劇場 press does. Optional (``None`` → not tracked)
        # so rigs without a character repository keep working unchanged.
        activity_anchor: CharacterActivityAnchor | None = None,
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
        # Per-target cross-replica execution lease (Phase 4 前置). ``None`` →
        # the historical lock-only path (self-host / lease-less rigs).
        self._execution_lease = execution_lease
        self._lease_heartbeat_interval = lease_heartbeat_interval_seconds
        # Web-push completion notify (C0 生成體驗). Fires from job
        # finalization so recovery-resumed pipelines notify too.
        self._notifications = notifications
        # AP2 second wave: a fusion create / iterate is one player action on a
        # hosted ``action_fixed`` tier. ``None`` (self-host, every token-billed
        # tier) resolves to the null object, so the entry points below stay
        # branch-free and byte-identical to the pre-AP2 behaviour.
        self._action_billing: (
            CloudActionBillingService | NullActionBillingService
        ) = action_billing or NullActionBillingService()
        self._activity_anchor = activity_anchor
        # Active background tasks per story id — keeping a strong ref
        # is required (asyncio's task registry holds only weak refs and
        # an unobserved exception would silently disappear).
        self._tasks: dict[str, asyncio.Task] = {}
        # Per-story locks gate concurrent iterate operations from a
        # double-clicking operator. The HTTP layer maps that to 409.
        self._locks: dict[str, asyncio.Lock] = {}

    async def _mark_cast_engaged(self, characters: Sequence[Character]) -> None:
        """NF4: this press was a foreground interaction with the whole cast.

        After the work landed — a press that raised delivered nothing and was
        refunded, so it is not evidence of anything. Fail-soft inside the
        anchor: bookkeeping must never turn a delivered story into an error."""
        if self._activity_anchor is None:
            return
        await self._activity_anchor.touch_all(characters)

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

        The ``fusion_story`` charge is raised before anything is written, so a
        refusal (402 out of credits, 409 moved price) reaches the caller ahead
        of the 202 and leaves no half-created story behind.
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
        charge = await self._begin_action_charge(
            ACTION_FUSION_STORY, user_id=user_id,
        )
        try:
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
                charge=charge,
            )
        except BaseException:
            # Nothing was generated, so nothing was covered — a full refund.
            await self._action_billing.release(charge)
            raise
        await self._mark_cast_engaged(characters)
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
        charge = await self._begin_action_charge(
            ACTION_FUSION_STORY_ITERATE, user_id=user_id,
        )
        try:
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
                charge=charge,
            )
        except BaseException:
            await self._action_billing.release(charge)
            raise
        await self._mark_cast_engaged(characters)
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
        charge = await self._begin_action_charge(
            ACTION_FUSION_STORY_ITERATE, user_id=user_id,
        )
        try:
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
                charge=charge,
            )
        except BaseException:
            await self._action_billing.release(charge)
            raise
        await self._mark_cast_engaged(characters)
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
        charge = await self._begin_action_charge(
            ACTION_FUSION_STORY_ITERATE, user_id=user_id,
        )
        try:
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
                charge=charge,
            )
        except BaseException:
            await self._action_billing.release(charge)
            raise
        await self._mark_cast_engaged(characters)
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
    ) -> str | None:
        async with self._lock_for(ctx.story_id):
            async with self._lease_session(ctx.story_id) as lease:
                if not lease.acquired:
                    self._log_lease_skip(ctx.story_id)
                    return LEASE_ABANDONED
                try:
                    await self._stage_plan(
                        ctx, previous_outline=previous_outline, lease=lease,
                    )
                    lease.raise_if_lost()
                    await self._stage_write_all(ctx, lease=lease)
                    lease.raise_if_lost()
                    await self._stage_polish(ctx, lease=lease)
                except StudioLeaseLost:
                    self._log_lease_lost(ctx.story_id)
                    return LEASE_ABANDONED
                except _PipelineAbort as abort:
                    _LOGGER.warning(
                        "fusion pipeline aborted story=%s reason=%s",
                        ctx.story_id, abort.reason,
                    )
                    await self._mark_failed(
                        ctx.story_id,
                        reason=abort.reason,
                        error_code=failure_error_code(abort),
                    )
                except Exception as exc:
                    _LOGGER.exception(
                        "fusion pipeline crashed story=%s", ctx.story_id,
                    )
                    await self._mark_failed(
                        ctx.story_id,
                        reason="pipeline crashed",
                        error_code=failure_error_code(exc),
                    )

    async def _run_beat_iteration(
        self,
        ctx: _PipelineContext,
        *,
        beat_index: int,
        hint: str | None,
    ) -> str | None:
        async with self._lock_for(ctx.story_id):
            async with self._lease_session(ctx.story_id) as lease:
                if not lease.acquired:
                    self._log_lease_skip(ctx.story_id)
                    return LEASE_ABANDONED
                try:
                    await self._stage_rewrite_beat(
                        ctx, beat_index=beat_index, hint=hint, lease=lease,
                    )
                    lease.raise_if_lost()
                    await self._stage_polish(ctx, lease=lease)
                except StudioLeaseLost:
                    self._log_lease_lost(ctx.story_id)
                    return LEASE_ABANDONED
                except _PipelineAbort as abort:
                    _LOGGER.warning(
                        "fusion beat iteration aborted story=%s reason=%s",
                        ctx.story_id, abort.reason,
                    )
                    await self._mark_failed(
                        ctx.story_id,
                        reason=abort.reason,
                        error_code=failure_error_code(abort),
                    )
                except Exception as exc:
                    _LOGGER.exception(
                        "fusion beat iteration crashed story=%s", ctx.story_id,
                    )
                    await self._mark_failed(
                        ctx.story_id,
                        reason="iteration crashed",
                        error_code=failure_error_code(exc),
                    )

    async def _run_polish_only(self, ctx: _PipelineContext) -> str | None:
        async with self._lock_for(ctx.story_id):
            async with self._lease_session(ctx.story_id) as lease:
                if not lease.acquired:
                    self._log_lease_skip(ctx.story_id)
                    return LEASE_ABANDONED
                try:
                    await self._stage_polish(ctx, lease=lease)
                except StudioLeaseLost:
                    self._log_lease_lost(ctx.story_id)
                    return LEASE_ABANDONED
                except _PipelineAbort as abort:
                    await self._mark_failed(
                        ctx.story_id,
                        reason=abort.reason,
                        error_code=failure_error_code(abort),
                    )
                except Exception as exc:
                    _LOGGER.exception(
                        "fusion polish crashed story=%s", ctx.story_id,
                    )
                    await self._mark_failed(
                        ctx.story_id,
                        reason="polish crashed",
                        error_code=failure_error_code(exc),
                    )

    async def _run_write_and_polish(self, ctx: _PipelineContext) -> str | None:
        """Resume runner for a pipeline interrupted mid-``writing``.

        The persisted outline is the checkpoint — ``_stage_write_all``
        skips beats whose prose already landed, so only the missing
        beats cost LLM calls before the polish stage locks in the text.
        """
        async with self._lock_for(ctx.story_id):
            async with self._lease_session(ctx.story_id) as lease:
                if not lease.acquired:
                    self._log_lease_skip(ctx.story_id)
                    return LEASE_ABANDONED
                try:
                    await self._stage_write_all(ctx, lease=lease)
                    lease.raise_if_lost()
                    await self._stage_polish(ctx, lease=lease)
                except StudioLeaseLost:
                    self._log_lease_lost(ctx.story_id)
                    return LEASE_ABANDONED
                except _PipelineAbort as abort:
                    _LOGGER.warning(
                        "fusion resume aborted story=%s reason=%s",
                        ctx.story_id, abort.reason,
                    )
                    await self._mark_failed(
                        ctx.story_id,
                        reason=abort.reason,
                        error_code=failure_error_code(abort),
                    )
                except Exception as exc:
                    _LOGGER.exception(
                        "fusion resume crashed story=%s", ctx.story_id,
                    )
                    await self._mark_failed(
                        ctx.story_id,
                        reason="pipeline crashed",
                        error_code=failure_error_code(exc),
                    )

    # ---- internal: durable job ledger (C0) ---------------------------

    async def _track_and_spawn(
        self,
        *,
        kind: str,
        target_id: str,
        params: dict,
        runner,
        charge: ActionChargeHandle | None = None,
    ) -> None:
        """Record a durable job row, then spawn the pipeline.

        Job bookkeeping is strictly fail-soft: if the ledger is absent
        or errors out, the pipeline still runs exactly as before C0 —
        losing restart durability must never cost a live generation.

        ``charge`` rides in the params rather than only in memory: whichever
        replica ends up finalizing this job is the one that must close the
        reservation, and after a restart the params are all it has.
        """
        params = {**params, **_charge_params(charge)}
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
        if job is not None:
            self._spawn(self._run_tracked(job, runner, charge=charge))
        elif charge is not None:
            # No job row to hang the charge off, but the player has still been
            # billed — the pipeline runs under its own charge lifecycle.
            self._spawn(
                self._run_billed(runner, target_id=target_id, charge=charge),
            )
        else:
            self._spawn(runner)

    async def _run_tracked(
        self,
        job: StudioGenerationJob,
        runner,
        *,
        charge: ActionChargeHandle | None = None,
        fresh_handle: bool = False,
    ) -> None:
        result = await self._run_scoped(runner, charge)
        if result == LEASE_ABANDONED:
            # The target's lease is held by / was reclaimed by another replica —
            # that owner finalizes the job. Do NOT touch it here or we would race
            # the legitimate writer. The charge is deliberately left open for the
            # same reason: it travels in the job params, so whoever finalizes
            # THIS row closes it (a recovery pass re-drives the newest running
            # row per target, and hands a duplicate to ``supersede_job``), and
            # closing it here would either refund work that replica is still
            # producing or bill for work it abandoned.
            return
        await self._finalize_job(job, charge=charge, fresh_handle=fresh_handle)

    async def _run_billed(
        self, runner, *, target_id: str, charge: ActionChargeHandle,
    ) -> None:
        """Run a charged pipeline whose job row could not be recorded.

        The one place a lost lease still closes the charge here: with no job
        row there are no params to hand it over with, so "leave it to whoever
        finalizes the row" (:meth:`_run_tracked`) has no row to leave it to and
        the reservation would simply never be closed. The handle is live and
        this process's own, so its usage record is the honest answer.
        """
        result = await self._run_scoped(runner, charge)
        if result == LEASE_ABANDONED:
            await self._action_billing.release(charge)
            return
        story = await self._load_for_finalize(target_id)
        if story is _LOAD_FAILED:
            # Deliberately open, exactly as in ``_finalize_job``: without the
            # story neither settle nor release can be justified, and the
            # User-side sweeper decides from its own covered-call probe.
            return
        await self._close_charge_for(charge, story)  # type: ignore[arg-type]

    async def _run_scoped(self, runner, charge: ActionChargeHandle | None):
        """Await ``runner`` inside the charge's interaction scope.

        Entered even when there is no charge — as an explicit *clear*, so a
        pipeline that could not be billed cannot inherit an unrelated scope
        left behind by whatever spawned it and have its calls waived for free.
        """
        with interaction_scope(charge.context if charge else None):
            return await runner

    async def _finalize_job(
        self,
        job: StudioGenerationJob,
        *,
        charge: ActionChargeHandle | None = None,
        fresh_handle: bool = False,
    ) -> None:
        """Persist the job's terminal status, then close the charge on it.

        Runners swallow their own exceptions and persist ``failed`` on
        the story, so the story status after the runner returns is the
        single source of truth for the job outcome — and, for the same
        reason, for the action charge's outcome too.

        The order is load-bearing (R1). The job row is the only durable record
        that this run ended; the charge is closed *after* it lands, so a
        crashed / failed write leaves a ``running`` row **and** an open charge —
        one consistent state that recovery re-drives. Closing first would let a
        failed row write strand a story recovery believes is still in flight
        while its money has already been settled or refunded.

        That write is also the concurrency token (R4): only the caller whose
        compare-and-swap flipped the row out of ``running`` closes the charge
        or sends the completion notification.
        """
        story = await self._load_for_finalize(job.target_id)
        if story is _LOAD_FAILED:
            # Without the story neither settle nor release can be justified;
            # the charge stays open and the User-side sweeper decides.
            return
        if not await self._store_terminal_job(job, story):  # type: ignore[arg-type]
            return
        await self._close_charge_for(
            charge, story, fresh_handle=fresh_handle,  # type: ignore[arg-type]
        )
        await self._notify_outcome(job, story)  # type: ignore[arg-type]

    async def _store_terminal_job(
        self, job: StudioGenerationJob, story: FusionStory | None,
    ) -> bool:
        """Claim the job's terminal transition; ``False`` ⇒ do not touch money.

        Two distinct "no" answers share that ``False`` because they call for
        the same restraint:

        * the write could not land — the row is still ``running`` and recovery
          owns both halves (see the ordering note in :meth:`_finalize_job`);
        * the row was already terminal — another finalizer (a recovery pass, a
          late in-process task, :meth:`supersede_job`) won the race and has
          either closed this charge or is about to. Closing it here as well
          would be a second ledger movement on one reservation, and the
          handles cannot see each other's ``closed`` flags because each is
          rebuilt from the same job params.

        The transition itself is the concurrency token (R4), so exactly one
        caller ever gets ``True``. A ledger-less rig has nothing to write and
        trivially succeeds.
        """
        if self._jobs is None:
            return True
        try:
            return await self._jobs.save_terminal_if_running(
                _terminal_job_row(job, story),
            )
        except Exception:
            _LOGGER.exception(
                "fusion: could not finalize studio job id=%s — leaving the "
                "action charge open for recovery", job.id,
            )
            return False

    async def _load_for_finalize(self, target_id: str):
        """The story, ``None`` if deleted, or ``_LOAD_FAILED`` if unreadable.

        The three cases have to stay distinct: a deleted story is a definite
        refund, an unreadable one is "we do not know" and must not move money.
        """
        try:
            return await self._repository.get(target_id)
        except Exception:
            _LOGGER.exception(
                "fusion: could not read story to finalize id=%s", target_id,
            )
            return _LOAD_FAILED

    async def _close_charge_for(
        self,
        charge: ActionChargeHandle | None,
        story: FusionStory | None,
        *,
        fresh_handle: bool = False,
    ) -> None:
        """Settle a delivered story, refund anything else — unless the handle
        was rebuilt, in which case the *server* decides (R2).

        ``fresh_handle=True`` marks a handle rebuilt from the job params rather
        than held live by this process (recovery, resume, supersede). Such a
        handle knows two things less than it looks like it does, and both cost
        the player money:

        * its usage record is empty by construction — the covered calls were
          served by the process that died — so the billing service's local
          "nothing was served ⇒ full refund" check would give away work the
          provider was already paid for;
        * **the story is not this charge's receipt.** Two ``running`` rows for
          one story mean two charges, and the loser's row is finalized against
          the *winner's* finished story. Reading ``status == ready`` as "this
          reservation was delivered" therefore settles the loser too, and the
          player pays twice for one story — the exact double-charge this
          branch used to produce.

        So a rebuilt handle never asserts an outcome at all. It always
        releases with ``settle_if_probed``, and the User service answers from
        the covered-call probe it still holds — the only record that ties
        *this* charge id to work actually served. A charge that really did the
        work has stamped calls and settles; a loser's orphan charge has none
        and is refunded. Same call, and the server's fact decides which.
        """
        if charge is None:
            return
        if fresh_handle:
            await self._close_fresh_charge(charge)
            return
        if story is not None and story.status == STATUS_READY:
            await self._action_billing.settle(charge)
            return
        await self._action_billing.release(charge)

    async def _begin_action_charge(
        self, action_key: str, *, user_id: str | None,
    ) -> ActionChargeHandle | None:
        """Charge one fusion action before any of its work is scheduled.

        Raised ahead of the HTTP layer's ``202`` on purpose: out of credits
        (402) and a moved price (409 ``price_changed``) are answers the player
        can act on, and discovering them as a failed job minutes later would
        make the Studio look broken instead of asking for a top-up.

        ``None`` means "not billed here" — self-host, a token-billed tier, a
        wallet outage — and the pipeline proceeds unchanged.
        """
        return await self._action_billing.begin(
            action_key,
            operator_id=(user_id or ""),
            # R9: bind to the number this player's own screen quoted; the
            # route puts it in scope from the request body.
            quoted_price_cr=client_quoted_price(action_key),
        )

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
        # The charge that paid for this job survived the restart in the params;
        # rebuilding the handle is what stops an interrupted generation from
        # stranding the player's credits until the upstream sweeper expires them.
        #
        # Every close below goes through :meth:`_close_fresh_charge`: this
        # handle knows nothing about the covered calls the *dead* process made,
        # so a plain refund here would give away work that was already served.
        charge = _charge_from_params(job.params)
        story = await self._repository.get(job.target_id)
        if story is None:
            await self._jobs.save(job.with_status(
                JOB_STATUS_FAILED, error_message="target missing",
            ))
            await self._close_fresh_charge(charge)
            return "failed"
        if story.is_terminal():
            # The pipeline finished before (or during) the restart — but not
            # necessarily *this* charge's pipeline: a story finished by the
            # replica that won the lease looks exactly the same from here. So
            # the outcome is never read off the story; the fresh-handle path
            # hands every case to the ledger's covered-call probe.
            await self._finalize_job(job, charge=charge, fresh_handle=True)
            return "finalized"
        if job.attempts >= MAX_JOB_ATTEMPTS:
            await self._mark_failed(
                story.id, reason="generation interrupted repeatedly",
            )
            await self._jobs.save(job.with_status(
                JOB_STATUS_FAILED,
                error_message="attempt limit reached",
            ))
            await self._close_fresh_charge(charge)
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
            await self._close_fresh_charge(charge)
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
            await self._close_fresh_charge(charge)
            return "failed"
        # The resumed run keeps generating under the *original* charge and its
        # scope, but the handle is still a rebuilt one: whatever it finishes,
        # the pre-crash covered calls are a fact only the ledger remembers, so
        # its finalize stays on the fresh-handle path.
        self._spawn(
            self._run_tracked(job, runner, charge=charge, fresh_handle=True),
        )
        return "resumed"

    async def _close_fresh_charge(
        self, charge: ActionChargeHandle | None,
    ) -> None:
        """Close a params-rebuilt charge — by asking the User service, always.

        The single close for every rebuilt handle, whatever the story says
        (R2). Neither a plain refund nor a plain settle can be justified from
        here: the handle's usage record is empty because this process is not
        the one that made the calls, and the story it was finalized against
        may belong to a competing charge. ``settle_if_probed`` asks the User
        service to answer from the covered-call probe it still holds, which is
        the only fact that names *this* charge id.
        """
        if charge is None:
            return
        await self._action_billing.release(charge, settle_if_probed=True)

    async def supersede_job(self, job: StudioGenerationJob) -> None:
        """Fail a duplicate running row — and close the charge it still holds.

        Two ``running`` rows for one story mean two charges. It is reachable
        without a restart: a second entry-point call that loses the target's
        lease leaves its row (and its own reservation, ids in the params) for
        recovery, while the replica holding the lease finalizes a *different*
        row under a *different* charge. Recovery re-drives only the newest row
        per target, so this is the single place the loser's money is decided —
        failing the row without it would strand the player's credits until the
        User-side sweeper expired them, and that sweeper's covered-call probe
        can read as delivered when an earlier call under the same charge
        stamped it.

        The row is failed **first** and the charge closed after (R1): the row is
        the durable record that this duplicate is done with, and a close that
        landed against a row still sitting ``running`` would invite the next
        recovery pass to decide the same money a second time. The write is a
        compare-and-swap (R4) for the mirror-image reason: the row may have
        been finalized in the meantime by the task that still owns it in
        process, and that finalize already closed this same charge. Losing the
        swap means "somebody else owns this verdict" — leave the money alone.

        The loser produced no story, but "produced nothing at all" is not
        something this process can assert: the handle is rebuilt from the
        params, so its usage record is empty whatever the losing replica
        actually served. ``settle_if_probed`` therefore hands the verdict to
        the User service's covered-call probe rather than guessing a refund.
        """
        if self._jobs is None:
            return
        claimed = await self._jobs.save_terminal_if_running(job.with_status(
            JOB_STATUS_FAILED, error_message="superseded by a newer job",
        ))
        if not claimed:
            _LOGGER.info(
                "fusion: duplicate job id=%s was already finalized elsewhere "
                "— leaving its action charge to that finalizer", job.id,
            )
            return
        await self._close_fresh_charge(_charge_from_params(job.params))

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
        lease: StudioLeaseSession | None = None,
    ) -> None:
        outline = await self._plan_with_language(
            prompt=ctx.prompt,
            briefs=ctx.briefs,
            previous_outline=previous_outline,
            operator_primary_language=ctx.operator_primary_language,
        )
        # Lease re-verify after the planner await, before persisting the outline —
        # a stolen lease must not clobber the reclaiming owner's outline / status.
        if lease is not None:
            lease.raise_if_lost()
        story = await self._must_load(ctx.story_id)
        story = story.with_outline(outline)
        await self._repository.save(story)

    async def _stage_write_all(
        self,
        ctx: _PipelineContext,
        *,
        lease: StudioLeaseSession | None = None,
    ) -> None:
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
            # Bounded cross-replica abort: a lease stolen mid-run stops the loop
            # before the next beat's LLM call, so the reclaiming replica owns the
            # writes from here on (at most one beat overlaps).
            if lease is not None:
                lease.raise_if_lost()
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
            # Re-verify the lease AFTER the writer's provider await and BEFORE the
            # save: a lease stolen during the (long) beat-writing round-trip must
            # not let this losing replica overwrite the reclaiming owner's beat.
            # The loop-top check only covers a lease lost between beats.
            if lease is not None:
                lease.raise_if_lost()
            story = await self._must_load(ctx.story_id)
            story = story.with_beat_content(beat_id=beat_id, content=content)
            await self._repository.save(story)

    async def _stage_rewrite_beat(
        self,
        ctx: _PipelineContext,
        *,
        beat_index: int,
        hint: str | None,
        lease: StudioLeaseSession | None = None,
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
        # Lease re-verify after the writer await, before persisting the beat.
        if lease is not None:
            lease.raise_if_lost()
        story = await self._must_load(ctx.story_id)
        story = story.with_beat_content(
            beat_id=target_beat.id, content=content,
        )
        await self._repository.save(story)

    async def _stage_polish(
        self, ctx: _PipelineContext, *,
        lease: StudioLeaseSession | None = None,
    ) -> None:
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

        # Lease re-verify after the critic/polish awaits, before locking in the
        # final text — a stolen lease must not overwrite the reclaiming owner.
        if lease is not None:
            lease.raise_if_lost()
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

    async def _mark_failed(
        self,
        story_id: str,
        *,
        reason: str,
        error_code: str | None = None,
    ) -> None:
        """Persist the terminal ``failed`` state for ``story_id``.

        ``error_code`` carries the cloud gateway's machine-readable refusal
        (see :mod:`~kokoro_link.application.services.studio_failure`) so the
        polling client can distinguish "you're out of 螢火" from a genuine
        fault; it stays ``None`` for ordinary crashes.
        """
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
                story.with_status(
                    STATUS_FAILED,
                    error_message=reason,
                    error_code=error_code,
                ),
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

    def _lease_session(self, story_id: str) -> StudioLeaseSession:
        return StudioLeaseSession(
            self._execution_lease,
            story_id,
            heartbeat_interval_seconds=self._lease_heartbeat_interval,
        )

    def _log_lease_skip(self, story_id: str) -> None:
        # Another replica holds the target's lease → do NOT drive or clobber it;
        # the owner finalizes both story and job. (Distributed-only; the
        # in-memory lease in a single process always acquires.)
        _LOGGER.info(
            "fusion: target leased by another replica, skipping story=%s",
            story_id,
        )

    def _log_lease_lost(self, story_id: str) -> None:
        _LOGGER.warning(
            "fusion: lease lost mid-run story=%s — abandoning to the "
            "reclaiming replica (no story/job write)",
            story_id,
        )


# --- module-level helpers --------------------------------------------


class _PipelineAbort(Exception):
    """Recoverable abort — orchestrator marks story failed and stops."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _terminal_job_row(
    job: StudioGenerationJob, story: FusionStory | None,
) -> StudioGenerationJob:
    """The terminal job row a finished pipeline should leave behind.

    Runners swallow their own exceptions and persist ``failed`` on the story,
    so the story's status after the runner returns is the single source of
    truth for the job's outcome.
    """
    if story is None:
        return job.with_status(
            JOB_STATUS_FAILED, error_message="target deleted",
        )
    if story.status == STATUS_READY:
        return job.with_status(JOB_STATUS_SUCCEEDED)
    if story.status == STATUS_FAILED:
        return job.with_status(
            JOB_STATUS_FAILED, error_message=story.error_message,
        )
    return job.with_status(
        JOB_STATUS_FAILED,
        error_message=f"pipeline ended non-terminal ({story.status})",
    )


def _charge_params(charge: ActionChargeHandle | None) -> dict[str, str]:
    """The action charge, flattened into JSON-safe job params."""
    if charge is None:
        return {}
    return {
        "charge_id": charge.charge_id,
        "interaction_id": charge.interaction_id,
        "action_key": charge.action_key,
    }


def _charge_from_params(params: Mapping) -> ActionChargeHandle | None:
    """Rebuild the handle a previous process opened, if the params carry one.

    All three fields are required — a handle missing any of them could close
    the wrong reservation or stamp calls with an id no charge answers to, both
    of which are worse than leaving the charge to the upstream sweeper.

    The rebuilt handle starts with a fresh (empty) usage record, and that
    emptiness is a *gap in knowledge*, not evidence: every close driven from
    one of these handles therefore travels the fresh-handle path (see
    :meth:`FusionStoryService._close_charge_for`), which settles a ready story
    explicitly and asks the User service's probe about everything else.
    """
    values: list[str] = []
    for key in _CHARGE_PARAM_KEYS:
        raw = params.get(key)
        if not isinstance(raw, str) or not raw.strip():
            return None
        values.append(raw.strip())
    charge_id, interaction_id, action_key = values
    return ActionChargeHandle(
        action_key=action_key,
        charge_id=charge_id,
        interaction_id=interaction_id,
    )


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
