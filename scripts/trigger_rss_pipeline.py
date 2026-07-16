"""One-off admin script: trigger RSS ingest + per-character curate.

Builds a real container against the configured database and runs the
two passes synchronously, printing what changed. Use when you want to
poke the pipeline without waiting for the 30/60-min scheduler tick.

Run:  uv run python scripts/trigger_rss_pipeline.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings


async def main(mode: str) -> None:
    settings = AppSettings.from_env()
    container = build_container(settings)
    scheduler = container.world_event_scheduler
    if scheduler is None:
        print("world_event_scheduler unavailable — RSS pipeline not wired")
        return

    sync = container.rss_source_sync_service
    if sync is not None:
        touched = await sync.sync()
        print(f"rss_source_sync touched {touched} rows")

    if mode in ("ingest", "all"):
        report = await scheduler.trigger_ingest_now()
        print("[ingest]", json.dumps({
            "sources_attempted": report.sources_attempted,
            "sources_succeeded": report.sources_succeeded,
            "events_persisted": report.events_persisted,
            "events_skipped_dedup": report.events_skipped_dedup,
            "events_skipped_embed": report.events_skipped_embed,
            "errors": list(report.errors),
        }, ensure_ascii=False, indent=2))

    if mode in ("curate", "all"):
        rows = await scheduler.trigger_curate_now()
        print("[curate]", json.dumps({
            "characters": len(rows),
            "total_added": sum(r["added"] for r in rows if r["added"] >= 0),
            "per_character": rows,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode not in {"ingest", "curate", "all"}:
        print(f"unknown mode {mode!r}; use ingest | curate | all")
        sys.exit(2)
    asyncio.run(main(mode))
