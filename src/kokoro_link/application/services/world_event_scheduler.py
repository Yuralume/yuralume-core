"""Background loop driving RSS ingestion and per-character curation.

Two independent passes share one asyncio task so we don't burn an
extra event-loop slot:

* **Ingest pass** (default every 30 min): pull every enabled RSS
  source, embed new entries, upsert into ``world_events``.
* **Curate pass** (default every 60 min, offset by 5 min from
  ingest): for every character with ``world_awareness_enabled``,
  refresh their inbox against the current event window.

Failures in either pass are logged and swallowed — the loop must keep
ticking. Lifespan-managed by the FastAPI app (start in startup,
``stop()`` in shutdown).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from kokoro_link.application.services.event_curator_service import (
    EventCuratorService,
)
from kokoro_link.application.services.rss_ingestion_service import (
    RssIngestionService,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort

_LOGGER = logging.getLogger(__name__)
_DEFAULT_INGEST_INTERVAL = 30 * 60.0
_DEFAULT_CURATE_INTERVAL = 60 * 60.0
_DEFAULT_INITIAL_DELAY = 30.0


class WorldEventScheduler:
    def __init__(
        self,
        *,
        ingestion_service: RssIngestionService,
        curator_service: EventCuratorService,
        character_repository: CharacterRepositoryPort,
        ingest_interval_seconds: float = _DEFAULT_INGEST_INTERVAL,
        curate_interval_seconds: float = _DEFAULT_CURATE_INTERVAL,
        initial_delay_seconds: float = _DEFAULT_INITIAL_DELAY,
    ) -> None:
        self._ingest = ingestion_service
        self._curator = curator_service
        self._characters = character_repository
        self._ingest_interval = max(60.0, ingest_interval_seconds)
        self._curate_interval = max(60.0, curate_interval_seconds)
        self._initial_delay = max(0.0, initial_delay_seconds)
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._next_ingest_at: float = 0.0
        self._next_curate_at: float = 0.0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name="world-event-scheduler",
        )

    async def stop(self) -> None:
        if self._task is None or self._stop_event is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        finally:
            self._task = None
            self._stop_event = None

    async def _run(self) -> None:
        assert self._stop_event is not None
        _LOGGER.info(
            "world event scheduler started (ingest=%.0fs curate=%.0fs)",
            self._ingest_interval, self._curate_interval,
        )
        loop = asyncio.get_running_loop()
        # Stagger initial passes so a cold start isn't immediately
        # blocked on N feeds + N character embeds.
        self._next_ingest_at = loop.time() + self._initial_delay
        self._next_curate_at = (
            loop.time() + self._initial_delay + self._ingest_interval / 2
        )
        try:
            while not self._stop_event.is_set():
                now_t = loop.time()
                next_due = min(self._next_ingest_at, self._next_curate_at)
                wait_for = max(1.0, next_due - now_t)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait_for,
                    )
                    break  # stop signalled
                except asyncio.TimeoutError:
                    pass

                now_t = loop.time()
                if now_t >= self._next_ingest_at:
                    await self._safe_ingest()
                    self._next_ingest_at = now_t + self._ingest_interval
                if now_t >= self._next_curate_at:
                    await self._safe_curate()
                    self._next_curate_at = now_t + self._curate_interval
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("world event scheduler crashed")
        _LOGGER.info("world event scheduler stopped")

    async def _safe_ingest(self) -> None:
        try:
            report = await self._ingest.ingest_all()
            _LOGGER.info(
                "rss ingest pass: sources=%d/%d persisted=%d "
                "dedup=%d embed_skipped=%d errors=%d",
                report.sources_succeeded, report.sources_attempted,
                report.events_persisted, report.events_skipped_dedup,
                report.events_skipped_embed, len(report.errors),
            )
        except Exception:
            _LOGGER.exception("rss ingest pass failed")
        try:
            await self._ingest.gc()
        except Exception:
            _LOGGER.exception("rss gc failed")

    async def _safe_curate(self) -> None:
        try:
            # Frozen characters halt all background activity
            # (CHARACTER_FREEZE_PLAN): skip per-character event curation
            # (embedding-ranked inbox fill) for them. The manual admin
            # ``trigger_curate_now`` hook below still covers every
            # character on explicit request.
            characters = await self._characters.list_active()
        except Exception:
            _LOGGER.exception("curate pass: list characters failed")
            return
        for character in characters:
            try:
                added = await self._curator.curate(character)
                if added:
                    _LOGGER.info(
                        "curated %d events for character %s",
                        added, character.id,
                    )
            except Exception:
                _LOGGER.exception(
                    "curate failed character=%s", character.id,
                )

    async def trigger_ingest_now(self):
        """Admin / test hook — run an ingest pass synchronously.

        Returns the ``IngestionReport`` so admin endpoints can surface
        per-source counts instead of having to parse the log."""
        report = await self._ingest.ingest_all()
        try:
            await self._ingest.gc()
        except Exception:
            _LOGGER.exception("rss gc failed")
        return report

    async def trigger_curate_now(self) -> list[dict]:
        """Admin / test hook — run a curate pass synchronously.

        Returns one row per character with the count added to its
        inbox, so admin endpoints can show what changed."""
        characters = await self._characters.list()
        results: list[dict] = []
        for character in characters:
            try:
                added = await self._curator.curate(character)
            except Exception:
                _LOGGER.exception(
                    "curate failed character=%s", character.id,
                )
                added = -1
            results.append({
                "character_id": character.id,
                "name": character.name,
                "added": added,
            })
        return results
