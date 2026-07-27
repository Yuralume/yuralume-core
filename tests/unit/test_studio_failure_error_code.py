"""Structured refusal codes on the 202 + poll studio pipelines (U4b).

Fusion story and branching drama generation answer ``202`` and finish in a
background task, so the foreground ``insufficient_credits_guard`` that maps a
gateway refusal onto a structured HTTP 402 can never fire for them. Before
U4b the refusal collapsed into a free-text ``error_message`` and the polling
player saw "pipeline crashed" instead of "螢火不足".

These tests drive a *real* gateway 402 envelope through the real refusal
classifier (``refusal_from_response``) into each pipeline and pin:

* the failed row carries the gateway's code in ``error_code``,
* the status DTO the client polls exposes it,
* an ordinary crash leaves ``error_code`` ``NULL`` (a bug must not look like
  a policy decision), and
* a refusal failure is terminal — startup recovery never re-drives it, so a
  broke player cannot burn attempts on a call that provably cannot succeed.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from kokoro_link.application.dto.branching_drama import (
    BranchingDramaResponse,
    BranchingDramaSummaryResponse,
)
from kokoro_link.application.dto.fusion_story import (
    FusionStoryResponse,
    FusionStorySummaryResponse,
)
from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
)
from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
    FusionCharacterBriefBuilder,
)
from kokoro_link.application.services.fusion_story_service import (
    FusionStoryService,
)
from kokoro_link.application.services.studio_failure import (
    MAX_ERROR_CODE_CHARS,
    failure_error_code,
)
from kokoro_link.application.services.studio_job_recovery import (
    StudioJobRecoveryService,
)
from kokoro_link.contracts.studio_jobs import JOB_STATUS_FAILED
from kokoro_link.domain.entities.branching_drama import (
    STATUS_FAILED as DRAMA_STATUS_FAILED,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import STATUS_FAILED
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.cloud_refusal import (
    INSUFFICIENT_CREDITS_CODE,
    ExpectedCloudRefusal,
    refusal_from_response,
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


# ── fake gateway ──────────────────────────────────────────────────────


def _gateway_refusal(
    code: str = INSUFFICIENT_CREDITS_CODE,
    *,
    message: str = "螢火不足，請先儲值",
) -> ExpectedCloudRefusal:
    """The exact 402 envelope the hosted gateway sends, classified for real.

    Building this through ``refusal_from_response`` rather than constructing
    an ``ExpectedCloudRefusal`` by hand keeps the test honest: if the wire
    contract or the classifier drifts, this stops producing a refusal and
    every assertion below fails loudly.
    """
    request = httpx.Request("POST", "https://gateway.example/v1/chat")
    body = json.dumps(
        {"error": {"code": code, "message": message, "retryable": False}},
    )
    response = httpx.Response(402, request=request, text=body)
    refusal = refusal_from_response(response, body)
    assert refusal is not None, "fixture no longer models a gateway refusal"
    return refusal


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


class _CharServiceStub:
    def __init__(self, by_id: dict[str, Character]) -> None:
        self.by_id = by_id

    async def get_character_entity(
        self, character_id: str,
    ) -> Character | None:
        return self.by_id.get(character_id)


class _NullBriefBuilder:
    async def build_many(self, characters):
        return self.build_persona_only_many(characters)

    def build_persona_only_many(self, characters):
        return [
            CharacterBrief(
                character_id=c.id,
                name=c.name,
                summary=c.summary or "",
                text=f"brief for {c.name}",
            )
            for c in characters
        ]


class _RefusingFusionPlanner:
    """Planner whose first LLM call hits the gateway's refusal."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def plan(self, **_):
        raise self._exc


class _RefusingDramaPlanner:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def plan_root(self, **_):
        raise self._exc

    async def plan_children(self, **_):  # pragma: no cover - never reached
        raise self._exc


def _fusion_service(
    exc: BaseException,
    *,
    jobs: InMemoryStudioJobRepository | None = None,
) -> tuple[InMemoryFusionStoryRepository, FusionStoryService]:
    """Fusion rig that dies in the planner stage with ``exc``.

    Writer / polisher / critic are never reached, so placeholders keep the
    rig focused on the one thing under test.
    """
    repo = InMemoryFusionStoryRepository()
    chars = {"c-a": _make_character("a"), "c-b": _make_character("b")}
    service = FusionStoryService(
        repository=repo,
        character_service=_CharServiceStub(chars),  # type: ignore[arg-type]
        brief_builder=FusionCharacterBriefBuilder(memory_repository=None),
        planner=_RefusingFusionPlanner(exc),  # type: ignore[arg-type]
        writer=object(),  # type: ignore[arg-type]
        polisher=object(),  # type: ignore[arg-type]
        critic=object(),  # type: ignore[arg-type]
        jobs=jobs,
    )
    return repo, service


def _drama_service(
    exc: BaseException,
    *,
    jobs: InMemoryStudioJobRepository | None = None,
) -> tuple[InMemoryBranchingDramaRepository, BranchingDramaService]:
    repo = InMemoryBranchingDramaRepository()
    chars = {"c-a": _make_character("a"), "c-b": _make_character("b")}
    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(chars),  # type: ignore[arg-type]
        brief_builder=_NullBriefBuilder(),  # type: ignore[arg-type]
        planner=_RefusingDramaPlanner(exc),  # type: ignore[arg-type]
        director=object(),  # type: ignore[arg-type]
        jobs=jobs,
    )
    return repo, service


async def _await_terminal(getter, target_id: str):
    for _ in range(300):
        target = await getter(target_id)
        assert target is not None
        if target.is_terminal():
            return target
        await asyncio.sleep(0.01)
    raise AssertionError("pipeline never reached a terminal state")


# ── the code-extraction helper ────────────────────────────────────────


class TestFailureErrorCode:
    def test_reads_the_gateway_code_off_a_direct_refusal(self) -> None:
        assert (
            failure_error_code(_gateway_refusal())
            == INSUFFICIENT_CREDITS_CODE
        )

    def test_follows_the_raise_from_chain(self) -> None:
        # Pipeline stages re-wrap upstream errors into their own types, so
        # the refusal is usually a link or two down ``__cause__``.
        refusal = _gateway_refusal()
        try:
            try:
                raise refusal
            except ExpectedCloudRefusal as exc:
                raise RuntimeError("planner failed") from exc
        except RuntimeError as wrapped:
            assert (
                failure_error_code(wrapped) == INSUFFICIENT_CREDITS_CODE
            )

    def test_ordinary_fault_has_no_code(self) -> None:
        # Inventing a code for a crash would make every bug look like a
        # deliberate policy decision to the player.
        assert failure_error_code(RuntimeError("boom")) is None
        assert failure_error_code(None) is None

    def test_passes_unknown_refusal_codes_through_verbatim(self) -> None:
        # Generic mechanism: core does not special-case insufficient_credits,
        # so a refusal code added by the cloud later needs no core change.
        assert (
            failure_error_code(_gateway_refusal("entitlement_denied"))
            == "entitlement_denied"
        )

    def test_clamps_an_oversized_code_to_the_column_width(self) -> None:
        # A hostile / buggy gateway must not turn a refusal into a
        # persistence error that would lose the failed status entirely.
        code = failure_error_code(_gateway_refusal("x" * 500))
        assert code is not None
        assert len(code) == MAX_ERROR_CODE_CHARS


# ── fusion story pipeline ─────────────────────────────────────────────


class TestFusionStoryFailureCode:
    @pytest.mark.asyncio
    async def test_gateway_refusal_lands_on_the_failed_story(self) -> None:
        _, service = _fusion_service(_gateway_refusal())
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        final = await _await_terminal(service.get, story.id)

        assert final.status == STATUS_FAILED
        assert final.error_code == INSUFFICIENT_CREDITS_CODE

    @pytest.mark.asyncio
    async def test_polling_dtos_expose_the_code(self) -> None:
        _, service = _fusion_service(_gateway_refusal())
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        final = await _await_terminal(service.get, story.id)

        detail = FusionStoryResponse.from_domain(final)
        summary = FusionStorySummaryResponse.from_domain(final)
        assert detail.error_code == INSUFFICIENT_CREDITS_CODE
        assert summary.error_code == INSUFFICIENT_CREDITS_CODE

    @pytest.mark.asyncio
    async def test_ordinary_crash_leaves_the_code_null(self) -> None:
        _, service = _fusion_service(RuntimeError("planner died"))
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        final = await _await_terminal(service.get, story.id)

        assert final.status == STATUS_FAILED
        assert final.error_message  # the free-text hint is still there
        assert final.error_code is None
        assert FusionStoryResponse.from_domain(final).error_code is None

    @pytest.mark.asyncio
    async def test_refusal_wrapped_by_a_stage_still_lands(self) -> None:
        refusal = _gateway_refusal()
        wrapped = RuntimeError("outline stage failed")
        wrapped.__cause__ = refusal
        _, service = _fusion_service(wrapped)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        final = await _await_terminal(service.get, story.id)

        assert final.error_code == INSUFFICIENT_CREDITS_CODE

    @pytest.mark.asyncio
    async def test_rerunning_clears_a_stale_code(self) -> None:
        # A player who tops up and retries must not keep seeing the
        # top-up prompt on a story that is no longer refused.
        repo, service = _fusion_service(_gateway_refusal())
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        failed = await _await_terminal(service.get, story.id)
        assert failed.error_code == INSUFFICIENT_CREDITS_CODE

        reset = failed.with_status("planning")
        await repo.save(reset)
        assert (await repo.get(story.id)).error_code is None


# ── branching drama pipeline ──────────────────────────────────────────


class TestBranchingDramaFailureCode:
    @pytest.mark.asyncio
    async def test_gateway_refusal_lands_on_the_failed_drama(self) -> None:
        _, service = _drama_service(_gateway_refusal())
        drama = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示", total_segments=3,
        )
        final = await _await_terminal(service.get, drama.id)

        assert final.status == DRAMA_STATUS_FAILED
        assert final.error_code == INSUFFICIENT_CREDITS_CODE

    @pytest.mark.asyncio
    async def test_polling_dtos_expose_the_code(self) -> None:
        _, service = _drama_service(_gateway_refusal())
        drama = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示", total_segments=3,
        )
        final = await _await_terminal(service.get, drama.id)

        detail = BranchingDramaResponse.from_domain(final)
        summary = BranchingDramaSummaryResponse.from_domain(final)
        assert detail.error_code == INSUFFICIENT_CREDITS_CODE
        assert summary.error_code == INSUFFICIENT_CREDITS_CODE

    @pytest.mark.asyncio
    async def test_ordinary_crash_leaves_the_code_null(self) -> None:
        _, service = _drama_service(RuntimeError("planner died"))
        drama = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示", total_segments=3,
        )
        final = await _await_terminal(service.get, drama.id)

        assert final.status == DRAMA_STATUS_FAILED
        assert final.error_message
        assert final.error_code is None


# ── retry semantics ───────────────────────────────────────────────────


class TestRefusalIsTerminal:
    """A refusal cannot succeed on retry until the player tops up.

    The durable ledger (C0) only re-drives jobs still ``running`` at
    startup, and a refused pipeline finalizes its job to ``failed`` — so a
    broke player never crash-loops the gateway. These tests pin that
    property rather than adding a refusal-specific stop, which would be a
    second mechanism doing the same job.
    """

    @pytest.mark.asyncio
    async def test_fusion_refusal_finalizes_the_job_and_stops(self) -> None:
        jobs = InMemoryStudioJobRepository()
        _, service = _fusion_service(_gateway_refusal(), jobs=jobs)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await _await_terminal(service.get, story.id)

        for _ in range(300):
            if not await jobs.list_running():
                break
            await asyncio.sleep(0.01)
        assert await jobs.list_running() == []

        recovery = StudioJobRecoveryService(
            jobs=jobs, fusion_story_service=service,
        )
        report = await recovery.recover()
        assert report["resumed"] == 0

    @pytest.mark.asyncio
    async def test_branching_refusal_finalizes_the_job_and_stops(
        self,
    ) -> None:
        jobs = InMemoryStudioJobRepository()
        _, service = _drama_service(_gateway_refusal(), jobs=jobs)
        drama = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示", total_segments=3,
        )
        await _await_terminal(service.get, drama.id)

        for _ in range(300):
            if not await jobs.list_running():
                break
            await asyncio.sleep(0.01)
        running = await jobs.list_running()
        assert running == []

        recovery = StudioJobRecoveryService(
            jobs=jobs, branching_drama_service=service,
        )
        report = await recovery.recover()
        assert report["resumed"] == 0

    @pytest.mark.asyncio
    async def test_refused_job_row_records_the_failure(self) -> None:
        jobs = InMemoryStudioJobRepository()
        _, service = _fusion_service(_gateway_refusal(), jobs=jobs)
        story = await service.create(
            character_ids=["c-a", "c-b"], prompt="提示",
        )
        await _await_terminal(service.get, story.id)
        for _ in range(300):
            if not await jobs.list_running():
                break
            await asyncio.sleep(0.01)

        rows = [
            job for job in jobs._jobs.values()  # noqa: SLF001
            if job.target_id == story.id
        ]
        assert len(rows) == 1
        assert rows[0].status == JOB_STATUS_FAILED
        # No attempt was burned re-driving a call that provably fails.
        assert rows[0].attempts == 1
