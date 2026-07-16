"""RSS sync region-bound emergency gating (SHIPPED_CONTENT_LOCALIZATION #5).

A region-bound ``emergency`` source is seeded ENABLED only when the
deployment region matches its declared ``region``; otherwise it lands
disabled. The gate binds to region, not language, and only affects the
first insert (operator flags + later syncs preserve the operator's
choice). Non-emergency sources ignore ``region``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kokoro_link.application.services.rss_source_sync_service import (
    RssSourceSyncService,
)
from kokoro_link.infrastructure.repositories.in_memory_rss_sources import (
    InMemoryRssSourceRepository,
)


def _seed(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "rss_sources.yaml"
    p.write_text(body, encoding="utf-8")
    return p


_EMERGENCY_TW = """
sources:
  - id: ncdr-all-alerts
    name: 民生示警
    feed_url: https://example.com/alerts
    category: emergency
    locale: zh-TW
    region: TW
    enabled: true
  - id: bbc-world
    name: BBC World
    feed_url: https://example.com/bbc
    category: news
    locale: en-GB
    enabled: true
"""


@pytest.mark.asyncio
async def test_region_match_enables_emergency(tmp_path: Path) -> None:
    repo = InMemoryRssSourceRepository()
    service = RssSourceSyncService(
        repository=repo, seed_path=_seed(tmp_path, _EMERGENCY_TW),
        deployment_region="TW",
    )
    await service.sync()
    alert = await repo.get("ncdr-all-alerts")
    assert alert is not None and alert.enabled is True


@pytest.mark.asyncio
async def test_region_mismatch_disables_emergency(tmp_path: Path) -> None:
    repo = InMemoryRssSourceRepository()
    service = RssSourceSyncService(
        repository=repo, seed_path=_seed(tmp_path, _EMERGENCY_TW),
        deployment_region="JP",
    )
    await service.sync()
    alert = await repo.get("ncdr-all-alerts")
    assert alert is not None and alert.enabled is False
    # Non-emergency source is unaffected by the region gate.
    bbc = await repo.get("bbc-world")
    assert bbc is not None and bbc.enabled is True


@pytest.mark.asyncio
async def test_unknown_deployment_region_keeps_yaml_default(tmp_path: Path) -> None:
    repo = InMemoryRssSourceRepository()
    service = RssSourceSyncService(
        repository=repo, seed_path=_seed(tmp_path, _EMERGENCY_TW),
        deployment_region="",
    )
    await service.sync()
    alert = await repo.get("ncdr-all-alerts")
    assert alert is not None and alert.enabled is True


@pytest.mark.asyncio
async def test_region_gate_only_affects_first_insert(tmp_path: Path) -> None:
    repo = InMemoryRssSourceRepository()
    seed = _seed(tmp_path, _EMERGENCY_TW)
    # First sync on a JP deployment lands it disabled.
    await RssSourceSyncService(
        repository=repo, seed_path=seed, deployment_region="JP",
    ).sync()
    # Operator manually enables it.
    alert = await repo.get("ncdr-all-alerts")
    await repo.upsert(replace(alert, enabled=True))
    # A later sync (still JP) must NOT re-disable the operator's choice.
    await RssSourceSyncService(
        repository=repo, seed_path=seed, deployment_region="JP",
    ).sync()
    alert = await repo.get("ncdr-all-alerts")
    assert alert.enabled is True
