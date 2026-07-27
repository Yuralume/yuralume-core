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


_REGIONED = """
sources:
  - id: cna-life
    name: 中央社生活
    feed_url: https://example.com/cna
    category: news
    locale: zh-TW
    region: tw
    enabled: true
  - id: npr-top
    name: NPR
    feed_url: https://example.com/npr
    category: news
    locale: en-US
    enabled: true
"""


@pytest.mark.asyncio
async def test_seed_persists_region_and_leaves_global_sources_null(
    tmp_path: Path,
) -> None:
    repo = InMemoryRssSourceRepository()
    await RssSourceSyncService(
        repository=repo, seed_path=_seed(tmp_path, _REGIONED),
    ).sync()

    tagged = await repo.get("cna-life")
    assert tagged is not None and tagged.region == "TW"
    # An untagged source is global on purpose — omitting the key must not
    # invent a region, or self-host pools would start filtering.
    glob = await repo.get("npr-top")
    assert glob is not None and glob.region is None


@pytest.mark.asyncio
async def test_seed_backfills_region_on_rows_that_have_none(
    tmp_path: Path,
) -> None:
    """Rows written before the column existed carry NULL; the seed is the
    only thing that knows what they should be."""
    repo = InMemoryRssSourceRepository()
    seed = _seed(tmp_path, _REGIONED)
    await RssSourceSyncService(repository=repo, seed_path=seed).sync()
    legacy = replace(await repo.get("cna-life"), region=None)
    await repo.upsert(legacy)

    await RssSourceSyncService(repository=repo, seed_path=seed).sync()

    assert (await repo.get("cna-life")).region == "TW"


@pytest.mark.asyncio
async def test_seed_never_overwrites_an_operator_chosen_region(
    tmp_path: Path,
) -> None:
    """``region`` follows the ``enabled`` convention, not the canonical
    one: it is admin-editable, so a later sync must not stomp it."""
    repo = InMemoryRssSourceRepository()
    seed = _seed(tmp_path, _REGIONED)
    await RssSourceSyncService(repository=repo, seed_path=seed).sync()
    await repo.upsert(replace(await repo.get("cna-life"), region="JP"))
    # …and the operator pinned a region onto a shipped global source.
    await repo.upsert(replace(await repo.get("npr-top"), region="US"))

    await RssSourceSyncService(repository=repo, seed_path=seed).sync()

    assert (await repo.get("cna-life")).region == "JP"
    assert (await repo.get("npr-top")).region == "US"


@pytest.mark.asyncio
async def test_seed_still_refreshes_canonical_fields_alongside_region(
    tmp_path: Path,
) -> None:
    repo = InMemoryRssSourceRepository()
    seed = _seed(tmp_path, _REGIONED)
    await RssSourceSyncService(repository=repo, seed_path=seed).sync()
    await repo.upsert(
        replace(await repo.get("cna-life"), name="stale", region="JP"),
    )

    await RssSourceSyncService(repository=repo, seed_path=seed).sync()

    refreshed = await repo.get("cna-life")
    assert refreshed.name == "中央社生活"
    assert refreshed.region == "JP"


@pytest.mark.asyncio
async def test_seed_does_not_reapply_region_after_the_operator_cleared_it(
    tmp_path: Path,
) -> None:
    """"Clear to global" has to survive the next boot. NULL alone can't say
    whether a row was never tagged or was deliberately untagged, so the
    admin edit sets ``region_locked`` and the seed stops backfilling."""
    repo = InMemoryRssSourceRepository()
    seed = _seed(tmp_path, _REGIONED)
    await RssSourceSyncService(repository=repo, seed_path=seed).sync()
    # What the admin PATCH writes when the operator clears the field.
    await repo.upsert(
        replace(await repo.get("cna-life"), region=None, region_locked=True),
    )

    await RssSourceSyncService(repository=repo, seed_path=seed).sync()

    assert (await repo.get("cna-life")).region is None
    assert (await repo.get("cna-life")).region_locked is True


@pytest.mark.asyncio
async def test_seed_backfill_still_runs_on_rows_the_operator_never_touched(
    tmp_path: Path,
) -> None:
    """The upgrade path: rows predating the column are NULL *and* unlocked,
    and must still pick the seed's region up."""
    repo = InMemoryRssSourceRepository()
    seed = _seed(tmp_path, _REGIONED)
    await RssSourceSyncService(repository=repo, seed_path=seed).sync()
    await repo.upsert(
        replace(await repo.get("cna-life"), region=None, region_locked=False),
    )

    await RssSourceSyncService(repository=repo, seed_path=seed).sync()

    assert (await repo.get("cna-life")).region == "TW"


@pytest.mark.asyncio
async def test_seed_preserves_the_lock_flag_when_refreshing_canonical_fields(
    tmp_path: Path,
) -> None:
    repo = InMemoryRssSourceRepository()
    seed = _seed(tmp_path, _REGIONED)
    await RssSourceSyncService(repository=repo, seed_path=seed).sync()
    await repo.upsert(
        replace(
            await repo.get("cna-life"),
            name="stale", region="JP", region_locked=True,
        ),
    )

    await RssSourceSyncService(repository=repo, seed_path=seed).sync()

    refreshed = await repo.get("cna-life")
    assert refreshed.name == "中央社生活"
    assert refreshed.region == "JP"
    assert refreshed.region_locked is True


@pytest.mark.asyncio
async def test_first_insert_is_never_locked(tmp_path: Path) -> None:
    """A shipped source the operator has not touched must stay open to the
    seed, otherwise a later YAML region correction can never land."""
    repo = InMemoryRssSourceRepository()
    await RssSourceSyncService(
        repository=repo, seed_path=_seed(tmp_path, _REGIONED),
    ).sync()

    assert (await repo.get("cna-life")).region_locked is False


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
