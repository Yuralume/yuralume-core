"""HTTP tests for the experiment admin endpoints (HUMANIZATION_ROADMAP §4.6)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.experiments import router
from kokoro_link.application.services.experiment_service import ExperimentService
from kokoro_link.infrastructure.repositories.in_memory_experiments import (
    InMemoryExperimentAssignmentRepository,
    InMemoryExperimentRepository,
)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    repo = InMemoryExperimentRepository()
    assignments = InMemoryExperimentAssignmentRepository()
    service = ExperimentService(
        experiment_repository=repo, assignment_repository=assignments,
    )

    class _StubContainer:
        experiment_service = service
        # §4.6 analysis endpoint reads this — leave None so the route
        # falls back to the "not wired" branch in unit tests; integration
        # tests wire a real ExperimentAnalysisService.
        experiment_analysis_service = None

    app.dependency_overrides[get_container] = lambda: _StubContainer()
    return app


@pytest.mark.asyncio
async def test_create_and_list_experiment() -> None:
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/v1/admin/experiments",
            json={
                "name": "opener-variant",
                "description": "A vs B opener prompt",
                "variant_ids": ["a", "b"],
            },
        )
        assert resp.status_code == 200
        created = resp.json()
        assert created["active"] is True
        assert len(created["variants"]) == 2

        listing = await client.get("/api/v1/admin/experiments")
        assert listing.status_code == 200
        assert len(listing.json()) == 1


@pytest.mark.asyncio
async def test_assign_then_report() -> None:
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        created = (await client.post(
            "/api/v1/admin/experiments",
            json={"name": "x", "variant_ids": ["a", "b"]},
        )).json()
        eid = created["id"]
        for i in range(5):
            await client.post(
                f"/api/v1/admin/experiments/{eid}/assign",
                json={"character_id": f"c{i}", "operator_id": "op"},
            )
        report = (await client.get(
            f"/api/v1/admin/experiments/{eid}/report",
        )).json()
        total = sum(b["assignment_count"] for b in report["buckets"])
        assert total == 5


@pytest.mark.asyncio
async def test_assign_inactive_returns_404() -> None:
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        created = (await client.post(
            "/api/v1/admin/experiments",
            json={"name": "x", "variant_ids": ["a", "b"]},
        )).json()
        eid = created["id"]
        toggle = await client.post(
            f"/api/v1/admin/experiments/{eid}/active",
            json={"active": False},
        )
        assert toggle.status_code == 200
        resp = await client.post(
            f"/api/v1/admin/experiments/{eid}/assign",
            json={"character_id": "c", "operator_id": "op"},
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_analyze_without_service_returns_unwired_message() -> None:
    """Without ``ExperimentAnalysisService`` wired, the endpoint falls
    back to a friendly "fetch the structured payload manually" message
    instead of crashing."""
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        created = (await client.post(
            "/api/v1/admin/experiments",
            json={"name": "x", "variant_ids": ["a", "b"]},
        )).json()
        resp = await client.post(
            f"/api/v1/admin/experiments/{created['id']}/analyze",
            json={"note": "owner says try high-tier model"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["queued"] is False
        assert "manually" in body["message"].lower()


@pytest.mark.asyncio
async def test_analyze_with_fake_provider_returns_skipped_narrative() -> None:
    """When the analysis service is wired but the resolver is fake, the
    LLM call short-circuits with a "skipped" narrative — the structured
    payload still comes back so the operator can use it externally."""
    from kokoro_link.application.services.experiment_analysis_service import (
        ExperimentAnalysisService,
    )
    from kokoro_link.infrastructure.llm.fake import FakeChatModel
    app = _make_app()
    repo = InMemoryExperimentRepository()
    assignments = InMemoryExperimentAssignmentRepository()
    service = ExperimentService(
        experiment_repository=repo, assignment_repository=assignments,
    )
    analysis_service = ExperimentAnalysisService(
        experiment_service=service,
        turn_record_repository=None,
        model=FakeChatModel(provider_id="fake"),
        feature_key="experiment_analysis",
    )

    class _Container:
        experiment_service = service
        experiment_analysis_service = analysis_service

    app.dependency_overrides[get_container] = lambda: _Container()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        created = (await client.post(
            "/api/v1/admin/experiments",
            json={"name": "x", "variant_ids": ["a", "b"]},
        )).json()
        resp = await client.post(
            f"/api/v1/admin/experiments/{created['id']}/analyze",
            json={"note": "test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # Structured payload always present
        assert body["structured_payload"]["experiment_id"] == created["id"]
        # Narrative either short-circuit-skipped (fake) or actually filled.
        assert isinstance(body["narrative"], str)
