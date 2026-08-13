"""``manifest.json`` schema for the ``.lumebackup`` inner zip (plan §4).

Everything the import preview needs at zero decompression cost lives
here: per-table counts, NSFW collapse statistics (pre-computed at export
so hosted preview never has to scan data files), the media inventory,
and the export fail-soft report.

Version convention mirrors the two existing precedents (card import /
official catalog client): a declared ``schema_version`` newer than this
build is rejected (422), never read downlevel; older versions are
tolerated through pydantic defaults.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from kokoro_link.application.dto.character_card import (
    CharacterCardArcSeriesBundle,
)

BACKUP_SCHEMA_VERSION = 1


class BackupNsfwCollapseStats(BaseModel):
    """Counts a hosted import preview shows before folding (D5)."""

    model_config = ConfigDict(extra="ignore")

    messages_replaced: int = 0
    """``content_mode='nsfw'`` messages that carry a ``safe_summary``
    and would be replaced by it on a hosted import."""
    messages_dropped: int = 0
    """``content_mode='nsfw'`` messages without a ``safe_summary`` —
    skipped entirely on a hosted import."""
    memories_skipped: int = 0
    """NSFW-flagged memory items skipped on a hosted import."""
    operator_profile_fields_skipped: int = 0
    """``operator_profile_fields`` rows whose ``content_mode == 'nsfw'`` —
    skipped whole on a hosted import. The extracted operator fact
    (``value``) and its quoted evidence have no safe representation, so the
    row cannot land at rest (D5); the read-side projection gate only
    blocked prompt injection, not at-rest storage."""
    pending_follow_ups_skipped: int = 0
    """``pending_follow_ups`` rows with any ``content_mode == 'nsfw'``
    queued message — skipped whole on a hosted import. The row's other
    free-text (``brief_reply`` / ``resolved_message`` / ``promise_intent``)
    is the character's side of that nsfw exchange, unmarked and without a
    safe summary, so the whole row is dropped rather than partially
    folded."""


class BackupMediaEntry(BaseModel):
    """One collected media original (plan §3.1 media bullet)."""

    model_config = ConfigDict(extra="ignore")

    key: str
    """Original object key; also the ``assets/<key>`` member path."""
    size: int = 0
    sha256: str = ""
    content_type: str = ""
    """Stored content type of the original (from the exporting adapter's
    ``stat``). The restore re-``put`` needs it so the variant decorator
    can recognise images and regenerate WebP renditions; empty means
    unknown and the importer falls back to an extension guess."""
    source_urls: list[str] = Field(default_factory=list)
    """Every DB URL that resolved to this original at export time — the
    url→key mapping is the EXPORTING adapter's verdict. The restore
    consults this map before its own adapter's reverse derivation, so
    absolute URLs minted under an old ``public_base_url`` (opaque to the
    importing deployment) still find their bytes in ``assets/``. Absent
    on older archives (schema stays v1) — the importer then falls back
    to local derivation exactly as before."""


class BackupExportReport(BaseModel):
    """Fail-soft outcomes surfaced to the user after export."""

    model_config = ConfigDict(extra="ignore")

    skipped_media_keys: list[str] = Field(default_factory=list)
    """Media referenced by the DB that could not be read at export time
    (already-lost originals etc.) — skipped, not fatal."""
    notes: list[str] = Field(default_factory=list)


class CharacterBackupManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int = BACKUP_SCHEMA_VERSION
    character_id: str
    """The exporter's original character id (restore remap anchor)."""
    character_name: str = ""
    app_version: str = ""
    exported_at: str = ""
    """ISO 8601 UTC instant."""
    table_counts: dict[str, int] = Field(default_factory=dict)
    """Row count per ``data/<table>.jsonl`` member, keyed by table name
    (registry names). Import verifies loaded counts against these."""
    nsfw_collapse: BackupNsfwCollapseStats = Field(
        default_factory=BackupNsfwCollapseStats,
    )
    media: list[BackupMediaEntry] = Field(default_factory=list)
    export_report: BackupExportReport = Field(
        default_factory=BackupExportReport,
    )
    bundled_arc_template_ids: list[str] = Field(default_factory=list)
    """Ids of the ``arc_templates/<id>.yaml`` members (bound template +
    series members), in bundle order — the same declaration
    ``.lumecard`` makes so import code can share its landing shape."""
    bundled_arc_series: list[CharacterCardArcSeriesBundle] = Field(
        default_factory=list,
    )
    """Arc-series projections riding the `.lumecard` mechanism verbatim
    (plan §3.1: series CARRY_YAML「沿 .lumecard 既有機制」): the bundle
    model is *reused*, not copied, so the two formats cannot drift and
    CB3 can feed these straight into the existing ``_save_with_remap``
    landing path."""
