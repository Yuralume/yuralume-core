"""Sync ``data/rss_sources.yaml`` into the ``rss_sources`` table.

Runs once at application startup. Only inserts new rows; existing
rows have their canonical fields (``name``, ``feed_url``, ``category``,
``locale``) refreshed but their *operator-controlled* fields
(``enabled``) preserved. Operator deletions are not re-created —
deleting a source via admin API and restarting must not bring it
back, so we never write rows whose id wasn't present in the DB
already after the first sync (we use the operator's flag to detect
that case).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

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
        deployment_region: str | None = None,
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
        self._deployment_region = (deployment_region or "").strip().upper()

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
            default_enabled = self._resolve_seed_enabled(entry)
            try:
                seeded = RssSource(
                    id=source_id,
                    name=(entry.get("name") or source_id),
                    feed_url=feed_url,
                    category=(entry.get("category") or "news"),
                    locale=(entry.get("locale") or "zh-TW"),
                    enabled=default_enabled,
                )
            except ValueError:
                logger.warning("rss seed entry invalid: %s", entry)
                continue

            current = existing.get(source_id)
            if current is None:
                await self._repository.upsert(seeded)
                touched += 1
                continue
            # Existing — refresh canonical fields, preserve operator
            # flags + health columns.
            merged = replace(
                current,
                name=seeded.name,
                feed_url=seeded.feed_url,
                category=seeded.category,
                locale=seeded.locale,
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
        syncs never re-apply it (existing rows preserve the operator
        flag)."""
        yaml_enabled = bool(entry.get("enabled", True))
        source_region = (entry.get("region") or "").strip().upper()
        category = (entry.get("category") or "").strip().lower()
        if category != "emergency" or not source_region:
            return yaml_enabled
        if not self._deployment_region:
            # Region unknown — keep the YAML default rather than guess.
            return yaml_enabled
        return yaml_enabled and source_region == self._deployment_region
