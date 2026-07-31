"""Sync ``data/rss_sources.yaml`` into the ``rss_sources`` table.

Runs once at application startup. Only inserts new rows; existing
rows have their canonical fields (``name``, ``feed_url``, ``category``,
``locale``) refreshed but their *operator-controlled* fields
(``enabled``, and ``region`` once the operator touched it) preserved —
``region`` is backfilled only while the row is unlocked and has none.
Operator deletions are not re-created —
deleting a source via admin API and restarting must not bring it
back, so we never write rows whose id wasn't present in the DB
already after the first sync (we use the operator's flag to detect
that case).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import yaml

from kokoro_link.contracts.rss_source import RssSourceRepositoryPort
from kokoro_link.domain.entities.rss_source import RssSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EnvFeedSeed:
    """A world-event feed parsed from the deprecated env family.

    Structural bridge type so the application layer doesn't import the
    bootstrap ``WorldEventFeed`` — the caller adapts env settings into
    these before calling :meth:`RssSourceSyncService.seed_env_feeds`."""

    source_id: str
    url: str
    topic_tags: tuple[str, ...] = field(default_factory=tuple)


class RssSourceSyncService:
    def __init__(
        self,
        *,
        repository: RssSourceRepositoryPort,
        seed_path: Path,
        deployment_region: str | Callable[[], str | None] | None = None,
    ) -> None:
        self._repository = repository
        self._seed_path = seed_path
        # Deployment region code (e.g. ``TW`` / ``JP`` / ``US``) used to
        # gate region-bound emergency sources. An emergency feed that
        # declares a ``region`` different from the deployment's is seeded
        # DISABLED on first insert — geographic alerts (民生示警 ＝ Taiwan)
        # are meaningless to an overseas self-host, and binding to
        # region/timezone rather than language is the right axis (an
        # en-US operator in Taiwan still wants Taiwan alerts). Operators
        # can still enable a disabled source from the admin UI; the flag
        # is only the shipped default, never overwritten on later syncs.
        #
        # Accepts a plain code OR a zero-arg accessor. The container passes the
        # accessor so the region is resolved *when a sync runs* rather than
        # captured at wiring time — a process that booted before the site
        # calendar region was corrected would otherwise keep seeding the old
        # region's defaults for its whole life (G0).
        self._deployment_region = deployment_region

    async def sync(self) -> int:
        """Reconcile the YAML seed with the DB. Returns count touched."""
        if not self._seed_path.exists():
            logger.info("rss seed file missing, skipping sync: %s", self._seed_path)
            return 0
        try:
            payload = yaml.safe_load(self._seed_path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("rss seed parse failed: %s", self._seed_path)
            return 0
        entries = (payload or {}).get("sources", [])
        if not isinstance(entries, list):
            logger.warning("rss seed has no 'sources' list, skipping")
            return 0

        existing = {s.id: s for s in await self._repository.list_all()}
        touched = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            source_id = (entry.get("id") or "").strip()
            feed_url = (entry.get("feed_url") or "").strip()
            if not source_id or not feed_url:
                continue
            retired = bool(entry.get("retired", False))
            default_enabled = self._resolve_seed_enabled(entry) and not retired
            try:
                seeded = RssSource(
                    id=source_id,
                    name=(entry.get("name") or source_id),
                    feed_url=feed_url,
                    category=(entry.get("category") or "news"),
                    locale=(entry.get("locale") or "zh-TW"),
                    region=entry.get("region"),
                    enabled=default_enabled,
                )
            except ValueError:
                logger.warning(
                    "rss seed entry invalid source_id=%s",
                    source_id or "<missing>",
                )
                continue

            current = existing.get(source_id)
            if current is None:
                await self._repository.upsert(seeded)
                touched += 1
                continue
            # ``retired`` is a data-driven kill switch for a bundled endpoint
            # that no longer serves a feed. Existing rows still pointing at
            # that exact canonical URL are disabled on upgrade. If an
            # operator already replaced the URL under the same source id,
            # preserve the whole row: it is no longer the retired endpoint
            # and must not be overwritten or disabled by the seed.
            if retired and current.feed_url != seeded.feed_url:
                logger.info(
                    "retired rss seed preserved operator replacement "
                    "source_id=%s",
                    source_id,
                )
                continue
            # Existing — refresh canonical fields, preserve operator
            # flags + health columns. Retirement is the sole exception to
            # preserving ``enabled`` because leaving the known-dead canonical
            # URL active would keep every upgraded install in an error loop.
            merged = replace(
                current,
                name=seeded.name,
                feed_url=seeded.feed_url,
                category=seeded.category,
                locale=seeded.locale,
                enabled=False if retired else current.enabled,
                # ``region`` is operator-controlled once set (it's editable
                # from the admin World-events panel), so it follows the
                # ``enabled`` convention rather than the canonical one:
                # backfill only while the operator has never expressed an
                # intent, and never overwrite one they did.
                #
                # "Never expressed an intent" cannot be read off ``region``
                # alone: NULL means both "row predates the column" (backfill
                # it) and "operator cleared it to global" (leave it). The
                # admin API sets ``region_locked`` on any explicit set *or*
                # clear, which is the only signal that separates the two —
                # without it a cleared source silently got the YAML region
                # back on the next boot.
                region=(
                    current.region
                    if (current.region_locked or current.region)
                    else seeded.region
                ),
            )
            if merged != current:
                await self._repository.upsert(merged)
                touched += 1
        return touched

    async def seed_env_feeds(
        self, feeds: tuple[EnvFeedSeed, ...],
    ) -> int:
        """First-boot seed operator-supplied env feeds into rss_sources.

        Bridges the deprecated ``KOKORO_WORLD_EVENT_FEED_*`` env family
        (CORE_ENV_TO_ADMIN_CONFIG track 3) into the same table the admin
        World-events panel edits. Only inserts feeds whose id is absent —
        an operator who later deletes one from the admin UI won't have it
        re-created (gate is "id not present"). Returns count inserted."""
        if not feeds:
            return 0
        existing = {s.id for s in await self._repository.list_all()}
        inserted = 0
        for feed in feeds:
            if feed.source_id in existing:
                continue
            # topic_tags map to a category; fall back to "news" when none.
            category = feed.topic_tags[0] if feed.topic_tags else "news"
            try:
                source = RssSource(
                    id=feed.source_id,
                    name=feed.source_id,
                    feed_url=feed.url,
                    category=category,
                    enabled=True,
                )
            except ValueError:
                logger.warning("env world-event feed invalid: %s", feed.source_id)
                continue
            await self._repository.upsert(source)
            inserted += 1
        return inserted

    def _resolve_seed_enabled(self, entry: dict) -> bool:
        """Compute the shipped ``enabled`` default for a first insert.

        A region-bound emergency source is disabled by default when the
        deployment region doesn't match its declared ``region`` — a
        Taiwan-only civil-alert feed is noise for an overseas deploy. All
        other sources keep their YAML ``enabled`` (default true). This
        only affects the first insert; operator enable/disable and later
        syncs never re-apply it (existing rows preserve the operator flag),
        except for a seed explicitly marked ``retired`` by :meth:`sync`."""
        yaml_enabled = bool(entry.get("enabled", True))
        source_region = (entry.get("region") or "").strip().upper()
        category = (entry.get("category") or "").strip().lower()
        if category != "emergency" or not source_region:
            return yaml_enabled
        deployment_region = self._current_deployment_region()
        if not deployment_region:
            # Region unknown — keep the YAML default rather than guess.
            return yaml_enabled
        return yaml_enabled and source_region == deployment_region

    def _current_deployment_region(self) -> str:
        """Resolve the deployment region now (accessor or captured value)."""
        source = self._deployment_region
        if callable(source):
            try:
                value = source()
            except Exception:  # noqa: BLE001 — seeding must never fail on this
                logger.warning(
                    "deployment region lookup failed; seeding region-bound "
                    "sources with their YAML defaults",
                    exc_info=True,
                )
                value = None
        else:
            value = source
        return (value or "").strip().upper()
