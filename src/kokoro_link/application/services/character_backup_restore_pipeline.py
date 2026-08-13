"""Row/media landing pipeline of a ``.lumebackup`` restore (CB3).

The data-moving half of the import, split from
:mod:`character_backup_import_service` (which keeps job lifecycle,
staging, preview, gates, recovery and cleanup):

- **scan pass** — one streaming read of every CARRY table builds D4's
  single id map (each carried row gets its fresh uuid up front, so
  forward references such as ``story_arc_beats.realized_event_id``
  rewrite correctly regardless of landing order), the set of source
  operator ids, and the media worklist *after* NSFW folding — a
  hosted-dropped message's attachment never even uploads;
- **landing pass** — rows land in registry CARRY order through the
  restore writer with the full rewrite rule set: FK-introspected columns
  strictly (missing target → NULL when nullable, row-skip otherwise,
  with the two shared-pool targets — global story seeds and pack arc
  series — resolved by batched existence checks), ``character_id`` /
  ``operator_id`` by name (several CARRY tables are deliberately
  FK-less), loose provenance refs through the id map, URLs through the
  media plan, and a uuid-token rewrite over every remaining string
  column (opaque JSON payloads included);
- **media transfer** — referenced originals re-``put`` under the new
  character's *real* prefixes so ``VariantAwareObjectStorage``
  regenerates the WebP renditions on its own (plan §3.2 零額外工作).
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from kokoro_link.application.dto.character_backup import (
    BackupTableRule,
    CharacterBackupManifest,
    carried_table_rules,
)
from kokoro_link.application.media_content_types import (
    coerce_served_image_content_type,
)
from kokoro_link.application.services.character_backup_restore_plan import (
    MESSAGE_FOLD_DROPPED,
    MESSAGE_FOLD_REPLACED,
    RestoreReport,
    TokenRewriter,
    fold_message_kwargs,
    memory_is_nsfw_flagged,
    operator_profile_field_is_nsfw_flagged,
    pending_follow_up_is_nsfw_flagged,
    rewrite_media_key,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_RESTORE,
    clamp_daily_limit,
    clamp_feed_daily_limit,
)
from kokoro_link.infrastructure.character_backup.packager import (
    BackupArchiveReader,
)
from kokoro_link.infrastructure.storage.keys import safe_key_segment

_INSERT_BATCH_ROWS = 500
_UPLOAD_CHUNK = 1024 * 1024

_MEDIA_FALLBACK_CONTENT_TYPE = "application/octet-stream"

#: Tables whose integer PK is DB-assigned on restore (autoincrement).
_AUTOINCREMENT_ID_TABLES = frozenset({"messages", "character_series_progress"})

#: Non-FK columns that may hold either a carried row id or an old
#: operator id (LumeGram reaction/comment authors).
_ACTOR_COLUMNS = ("liker_id", "author_id")

#: Non-FK columns that reference carried parents by id (the schema keeps
#: them FK-less on purpose — stale values fail soft); remapped through
#: the id map, kept verbatim when unmapped.
_LOOSE_REF_COLUMNS = (
    "conversation_id",
    "schedule_id",
    "arc_id",
    "beat_id",
    "post_id",
    "seed_id",
    "arc_beat_id",
    "activity_id",
    "realized_event_id",
    "last_arc_id",
    "weather_vet_activity_id",
)

_DIRECT_URL_COLUMNS = ("url", "image_url", "video_url")

#: Control characters a browser strips while parsing a URL for navigation
#: (tab / LF / CR anywhere, plus every C0 control and space when leading).
#: A scheme guard must strip them too, else ``"java\tscript:alert(1)"``
#: reads as scheme-less and slips past — the exact trick that turns a
#: restored ``<a :href>`` value into same-origin script execution.
_URL_STRIP_TABLE = {c: None for c in [*range(0x00, 0x21), 0x7F]}


def sanitise_landed_url(value: str) -> str:
    """Neutralise a restored URL that would be dangerous in an ``<a href>``.

    Restore rewrites media URLs through the media plan; a value the plan
    could not map (its bytes were not in the archive) is otherwise landed
    verbatim, and the manifest is fully attacker-controlled. A mapped
    value is always this deployment's own ``public_url`` (http/https) and
    passes untouched; an unmapped value is kept only when its scheme is
    http/https or it is scheme-less (a relative, same-origin path).
    Anything with another scheme — ``javascript:``, ``data:``,
    ``vbscript:``, … — is dropped to ``""`` so it can never execute when a
    player later clicks the link.
    """
    if not value:
        return value
    # Decide safety on a browser-canonicalised copy (controls/space removed,
    # lower-cased); the stored value itself is never rewritten beyond the
    # drop-to-empty decision below.
    canonical = value.translate(_URL_STRIP_TABLE).strip().lower()
    if not canonical:
        return value
    scheme_sep = canonical.find(":")
    if scheme_sep == -1:
        return value  # no scheme → relative / same-origin
    slash = canonical.find("/")
    if slash != -1 and slash < scheme_sep:
        return value  # the ':' lives inside a path segment, not a scheme
    scheme = canonical[:scheme_sep]
    if scheme in ("http", "https"):
        return value
    return ""


@dataclass(frozen=True, slots=True)
class MediaTransfer:
    """One media original's relocation (old key → landing key)."""

    old_key: str
    new_key: str
    content_type: str


@dataclass(frozen=True, slots=True)
class _DeferredRef:
    """FK value whose target may pre-exist on this instance (global seed
    pool / pack series) — resolved by an existence check at flush."""

    column: str
    referenced_table: str
    value: str


@dataclass(frozen=True, slots=True)
class SourceFrozenState:
    """The exported character's freeze columns, as they should read once
    the restore succeeds.

    The landing pass overrides them with the ``restore`` freeze (A3: the
    row is visible minutes before its data follows), so the exported
    state has to be carried aside and written back at the success
    finalize. A source that was itself mid-restore when exported (a
    crashed run's leftovers) is sanitised to unfrozen — landing a
    permanently chat-blocked character would be strictly worse."""

    frozen: bool
    frozen_at: datetime | None
    frozen_reason: str | None


@dataclass
class RestoreContext:
    """Everything the row-rewrite rules need, built by the scan pass."""

    new_character_id: str
    old_character_id: str
    importer_id: str
    id_map: dict[str, str]
    old_operator_ids: set[str]
    url_map: dict[str, str]
    transfers: dict[str, MediaTransfer]
    template_id_map: dict[str, str] = field(default_factory=dict)
    landed_template_ids: list[str] = field(default_factory=list)
    series_id_map: dict[str, str] = field(default_factory=dict)
    landed_series_ids: list[str] = field(default_factory=list)
    report: RestoreReport = field(default_factory=RestoreReport)
    source_frozen: SourceFrozenState | None = None
    """Captured by the landing pass from the ``characters`` row (A3);
    the import service writes it back when the restore succeeds."""

    def __post_init__(self) -> None:
        self.tokens = TokenRewriter(self.id_map)


class CharacterRestorePipeline:
    """Stateless per-deployment pipeline; per-run state lives in
    :class:`RestoreContext` and the writer's landing handle."""

    def __init__(
        self,
        *,
        object_storage: ObjectStoragePort,
        cloud_mode: bool,
    ) -> None:
        self._object_storage = object_storage
        self._cloud_mode = cloud_mode

    # ---- scan pass ----------------------------------------------------

    async def scan(
        self,
        reader: BackupArchiveReader,
        manifest: CharacterBackupManifest,
        *,
        operator_id: str,
    ) -> RestoreContext:
        """Pass 1: assign every new id and build the media worklist.

        The table sweep (decompress + per-line pydantic over potentially
        GB of jsonl) is pure CPU/file work with zero awaits, so it runs
        in a worker thread (A5) — inline it would freeze this process's
        event loop, including ``/health``, for the whole pass. The
        reader has no concurrent user during this stage.
        """
        new_character_id = str(uuid4())
        id_map: dict[str, str] = {manifest.character_id: new_character_id}
        old_operator_ids: set[str] = set()
        media_urls: list[str] = []
        report = RestoreReport()

        await asyncio.to_thread(
            self._scan_tables, reader, id_map, old_operator_ids, media_urls,
        )

        url_map, transfers = await self._build_media_plan(
            reader,
            manifest,
            media_urls,
            old_character_id=manifest.character_id,
            new_character_id=new_character_id,
            operator_id=operator_id,
            report=report,
        )
        return RestoreContext(
            new_character_id=new_character_id,
            old_character_id=manifest.character_id,
            importer_id=operator_id,
            id_map=id_map,
            old_operator_ids=old_operator_ids,
            url_map=url_map,
            transfers=transfers,
            report=report,
        )

    def _scan_tables(
        self,
        reader: BackupArchiveReader,
        id_map: dict[str, str],
        old_operator_ids: set[str],
        media_urls: list[str],
    ) -> None:
        """Synchronous body of the scan pass (runs on a worker thread).

        Mutates the caller-owned collections in place; ``id_map`` arrives
        pre-seeded with the character's own id so the ``characters`` row's
        ``setdefault`` keeps the fresh character id."""
        for rule in carried_table_rules():
            for line in reader.iter_data_lines(f"{rule.table}.jsonl"):
                record = rule.dto.model_validate_json(line)
                if rule.table == "messages":
                    self._scan_message(record, media_urls)
                    continue
                if (
                    rule.table == "memory_items"
                    and self._cloud_mode
                    and memory_is_nsfw_flagged(
                        getattr(record, "tags", "[]") or "[]",
                    )
                ):
                    # Deliberately NOT in the id map: memoir pins that
                    # point at a skipped memory must dangle-and-drop.
                    continue
                if (
                    rule.table == "operator_profile_fields"
                    and self._cloud_mode
                    and operator_profile_field_is_nsfw_flagged(
                        getattr(record, "content_mode", "") or "",
                    )
                ):
                    # Whole-row skip on hosted (D5): the raw operator fact
                    # has no safe form. Kept out of the id map for the same
                    # dangle-and-drop reason as nsfw memories (nothing
                    # carried references it, but the stance stays uniform).
                    continue
                if (
                    rule.table == "pending_follow_ups"
                    and self._cloud_mode
                    and pending_follow_up_is_nsfw_flagged(
                        getattr(record, "messages_json", "[]") or "[]",
                    )
                ):
                    # Whole-row skip on hosted (D5): the queued turns AND
                    # the row's reply free-text are the nsfw exchange.
                    continue
                old_id = getattr(record, "id", None)
                if isinstance(old_id, str) and old_id:
                    id_map.setdefault(old_id, str(uuid4()))
                for column in ("operator_id", "user_id"):
                    value = getattr(record, column, None)
                    if isinstance(value, str) and value:
                        old_operator_ids.add(value)
                self.scan_media_columns(rule.table, record, media_urls)

    def _scan_message(self, record: Any, media_urls: list[str]) -> None:
        """Collect attachment media of the messages that will survive
        folding — a hosted-dropped/folded NSFW message's media never
        uploads (D5: 零 NSFW 原文落地 covers the pixels too)."""
        outcome, _ = fold_message_kwargs(
            {
                "content_mode": getattr(record, "content_mode", ""),
                "safe_summary": getattr(record, "safe_summary", ""),
            },
            cloud_mode=self._cloud_mode,
        )
        if outcome in (MESSAGE_FOLD_DROPPED, MESSAGE_FOLD_REPLACED):
            return
        for attachment in self.parse_attachments(
            getattr(record, "attachments_json", "[]"),
        ):
            url = attachment.get("url")
            if isinstance(url, str) and url:
                media_urls.append(url)

    def scan_media_columns(
        self, table: str, record: Any, media_urls: list[str],
    ) -> None:
        """Fold one row's media URL columns into ``media_urls``. Public:
        the import service reuses it for crash-cleanup reverse-lookup."""
        if table == "characters":
            try:
                urls = json.loads(getattr(record, "image_urls", "[]") or "[]")
            except ValueError:
                urls = []
            if isinstance(urls, list):
                media_urls.extend(u for u in urls if isinstance(u, str) and u)
        elif table == "feed_posts":
            for column in ("image_url", "video_url"):
                value = getattr(record, column, None)
                if value:
                    media_urls.append(value)
        elif table == "character_album_items":
            value = getattr(record, "url", "")
            if value:
                media_urls.append(value)

    async def _build_media_plan(
        self,
        reader: BackupArchiveReader,
        manifest: CharacterBackupManifest,
        media_urls: list[str],
        *,
        old_character_id: str,
        new_character_id: str,
        operator_id: str,
        report: RestoreReport,
    ) -> tuple[dict[str, str], dict[str, MediaTransfer]]:
        asset_keys = set(reader.asset_keys())
        manifest_types = {
            entry.key: entry.content_type for entry in manifest.media
        }
        # The EXPORTING adapter's url→key verdicts, pinned in the manifest.
        # Consulted before this deployment's own reverse derivation: rows
        # minted under an old ``public_base_url`` carry absolute URLs this
        # adapter cannot parse, yet their bytes are right in ``assets/`` —
        # deriving locally first would mark them all missing. Older
        # archives have no ``source_urls`` (empty default) and fall
        # through to local derivation exactly as before.
        manifest_key_by_url: dict[str, str] = {}
        for entry in manifest.media:
            for source_url in entry.source_urls:
                if source_url:
                    manifest_key_by_url.setdefault(source_url, entry.key)
        importer_segment = safe_key_segment(operator_id, fallback="operator")
        url_map: dict[str, str] = {}
        transfers: dict[str, MediaTransfer] = {}
        missing: list[str] = []
        for url in media_urls:
            if url in url_map:
                continue
            old_key = manifest_key_by_url.get(url)
            if old_key is None:
                old_key = self._object_storage.object_key_from_url(url)
            if old_key is None or old_key not in asset_keys:
                marker = old_key or url
                if marker not in missing:
                    missing.append(marker)
                continue
            transfer = transfers.get(old_key)
            if transfer is None:
                new_key = rewrite_media_key(
                    old_key,
                    old_character_id=old_character_id,
                    new_character_id=new_character_id,
                    importer_segment=importer_segment,
                )
                # The manifest content type AND the extension guess are both
                # attacker-controlled (a ``.html`` key guesses ``text/html``);
                # anything outside the served-image allow-list — SVG included —
                # is re-``put`` as inert ``application/octet-stream`` so a
                # restored original can never come back as a same-origin
                # executable document (S1). Same allow-list the upload guard
                # uses; the constant lives in one place.
                content_type = coerce_served_image_content_type(
                    manifest_types.get(old_key)
                    or mimetypes.guess_type(old_key)[0],
                    fallback=_MEDIA_FALLBACK_CONTENT_TYPE,
                )
                transfer = MediaTransfer(
                    old_key=old_key,
                    new_key=new_key,
                    content_type=content_type,
                )
                transfers[old_key] = transfer
            url_map[url] = await self._object_storage.public_url(
                object_key=transfer.new_key,
            )
        report.media_missing = missing
        return url_map, transfers

    # ---- landing pass -------------------------------------------------

    async def land_rows(
        self,
        reader: BackupArchiveReader,
        ctx: RestoreContext,
        landing: Any,
    ) -> None:
        """Pass 2: rewrite + insert every CARRY table through the restore
        writer's landing handle, in registry order.

        The flushes are this loop's natural awaits, but a stretch of rows
        that all fold away (hosted NSFW drops) fills no batch and would
        starve the event loop for the whole file — hence the explicit
        yield every ``_INSERT_BATCH_ROWS`` *lines seen* (A5), not rows
        kept."""
        lines_seen = 0
        for rule in carried_table_rules():
            refs = landing.reference_columns(rule.table)
            batch: list[tuple[dict, list[_DeferredRef]]] = []
            for line in reader.iter_data_lines(f"{rule.table}.jsonl"):
                lines_seen += 1
                if lines_seen % _INSERT_BATCH_ROWS == 0:
                    await asyncio.sleep(0)
                record = rule.dto.model_validate_json(line)
                prepared = self._prepare_row(
                    rule, record.to_row_kwargs(), refs, ctx,
                )
                if prepared is None:
                    continue
                batch.append(prepared)
                if len(batch) >= _INSERT_BATCH_ROWS:
                    await self._flush_rows(rule.table, batch, landing, ctx)
                    batch = []
            if batch:
                await self._flush_rows(rule.table, batch, landing, ctx)

    def _prepare_row(
        self,
        rule: BackupTableRule,
        kwargs: dict,
        refs: dict,
        ctx: RestoreContext,
    ) -> tuple[dict, list[_DeferredRef]] | None:
        table = rule.table
        report = ctx.report

        if table == "messages":
            outcome, kwargs = fold_message_kwargs(
                kwargs, cloud_mode=self._cloud_mode,
            )
            if outcome == MESSAGE_FOLD_DROPPED:
                report.nsfw_messages_dropped += 1
                return None
            if outcome == MESSAGE_FOLD_REPLACED:
                report.nsfw_messages_replaced += 1
        elif table == "memory_items":
            if self._cloud_mode and memory_is_nsfw_flagged(
                kwargs.get("tags") or "[]",
            ):
                report.nsfw_memories_skipped += 1
                return None
        elif table == "operator_profile_fields":
            # D5: the extracted operator fact lands verbatim in ``value``
            # (evidence quotes in ``evidence_json``) with no safe form —
            # skip the whole row on hosted, before it lands (at-rest, not
            # merely read-gated). Self-host keeps it verbatim.
            if self._cloud_mode and operator_profile_field_is_nsfw_flagged(
                kwargs.get("content_mode") or "",
            ):
                report.nsfw_operator_profile_fields_skipped += 1
                return None
        elif table == "pending_follow_ups":
            # D5: the queued turns (messages_json) plus the row's reply
            # free-text belong to one nsfw exchange with no safe form —
            # skip the whole row on hosted, before it lands.
            if self._cloud_mode and pending_follow_up_is_nsfw_flagged(
                kwargs.get("messages_json") or "[]",
            ):
                report.nsfw_pending_follow_ups_skipped += 1
                return None
        elif table == "memoir_pins":
            # A pin whose source row didn't land (NSFW-skipped memory,
            # or anything else outside the map) must not dangle.
            if kwargs.get("entry_id") not in ctx.id_map:
                report.skip(table)
                return None
        elif table == "story_seeds":
            # ``external_id`` is globally unique (pack import key); a
            # restored private seed must never collide with — or claim —
            # a pack identity on this instance.
            kwargs["external_id"] = None
        elif table == "story_arcs":
            source_template = kwargs.get("source_template_id")
            if source_template:
                kwargs["source_template_id"] = ctx.template_id_map.get(
                    source_template, source_template,
                )
        elif table == "characters":
            for column, collision_map in (
                ("arc_template_id", ctx.template_id_map),
                ("arc_series_id", ctx.series_id_map),
            ):
                value = kwargs.get(column)
                if value:
                    # Collision-remapped → follow the remap; otherwise
                    # keep the id (either just landed under it, or it
                    # may exist as a pack on this instance — both
                    # columns are deliberately non-FK and fail-soft).
                    kwargs[column] = collision_map.get(value, value)
            # A3: the row becomes visible the moment this batch commits,
            # minutes before memories / media / conversations follow —
            # land it frozen under the ``restore`` provenance (blocks
            # chat AND background ticks) and carry the exported freeze
            # state aside for the success finalize. A source that was
            # itself mid-restore is sanitised to unfrozen.
            source_reason = kwargs.get("frozen_reason")
            if source_reason == FREEZE_REASON_RESTORE:
                ctx.source_frozen = SourceFrozenState(
                    frozen=False, frozen_at=None, frozen_reason=None,
                )
            else:
                ctx.source_frozen = SourceFrozenState(
                    frozen=bool(kwargs.get("frozen")),
                    frozen_at=kwargs.get("frozen_at"),
                    frozen_reason=source_reason,
                )
            kwargs["frozen"] = True
            kwargs["frozen_at"] = datetime.now(timezone.utc)
            kwargs["frozen_reason"] = FREEZE_REASON_RESTORE
            # Restore lands rows straight through the Core insert, bypassing
            # ``Character.create``'s domain clamp — so an attacker's backup
            # DTO carrying ``proactive_daily_limit=100000`` would otherwise
            # uncap this account's daily proactive-message LLM spend. Reuse
            # the exact domain clamp (single source of truth, ≤50) rather
            # than restate the ceiling here (S5).
            if "proactive_daily_limit" in kwargs:
                kwargs["proactive_daily_limit"] = clamp_daily_limit(
                    kwargs["proactive_daily_limit"],
                )
            if "feed_daily_limit" in kwargs:
                kwargs["feed_daily_limit"] = clamp_feed_daily_limit(
                    kwargs["feed_daily_limit"],
                )

        assigned: set[str] = set()

        if table in _AUTOINCREMENT_ID_TABLES:
            kwargs.pop("id", None)
        else:
            old_id = kwargs.get("id")
            if isinstance(old_id, str) and old_id:
                new_id = ctx.id_map.get(old_id)
                if new_id is None:
                    new_id = str(uuid4())
                    ctx.id_map[old_id] = new_id
                kwargs["id"] = new_id
                assigned.add("id")

        # ``character_id`` by NAME, not only by FK: several CARRY tables
        # (``conversations``, ``turn_journals``, …) deliberately carry no
        # FK to ``characters``, and every carried row belongs to the
        # restored character by construction (the export scoped it).
        if table == "characters":
            kwargs["id"] = ctx.new_character_id
            assigned.add("id")
        elif "character_id" in kwargs:
            kwargs["character_id"] = ctx.new_character_id
            assigned.add("character_id")

        deferred: list[_DeferredRef] = []
        for column, ref in refs.items():
            if column not in kwargs or column in assigned:
                continue
            value = kwargs[column]
            if ref.referenced_table == "characters":
                kwargs[column] = ctx.new_character_id
            elif ref.referenced_table == "operator_profiles":
                kwargs[column] = ctx.importer_id
            elif value is None:
                pass
            elif ref.referenced_table == "arc_series":
                mapped = ctx.series_id_map.get(value, value)
                if mapped in ctx.landed_series_ids:
                    kwargs[column] = mapped
                else:
                    deferred.append(
                        _DeferredRef(column, "arc_series", mapped),
                    )
            elif value in ctx.id_map:
                kwargs[column] = ctx.id_map[value]
            elif ref.referenced_table == "story_seeds":
                # Not carried → it referenced the global pool; keep it
                # only if the same seed exists here (existence check at
                # flush), else clear the provenance.
                deferred.append(_DeferredRef(column, "story_seeds", value))
            elif ref.nullable:
                kwargs[column] = None
            else:
                report.skip(table)
                return None
            assigned.add(column)

        # Loose (non-FK) references to carried parents — same map, keep
        # unmapped values as-is (the columns are non-FK precisely so a
        # stale pointer fails soft downstream).
        for column in _LOOSE_REF_COLUMNS:
            if column in kwargs and column not in assigned:
                value = kwargs[column]
                if isinstance(value, str) and value:
                    kwargs[column] = ctx.id_map.get(value, value)
                assigned.add(column)
        for column in ("operator_id", "user_id"):
            if column in kwargs and column not in assigned:
                kwargs[column] = ctx.importer_id
                assigned.add(column)
        for column in _ACTOR_COLUMNS:
            if column in kwargs and column not in assigned:
                value = kwargs[column]
                if isinstance(value, str) and value:
                    if value in ctx.id_map:
                        kwargs[column] = ctx.id_map[value]
                    elif value in ctx.old_operator_ids:
                        kwargs[column] = ctx.importer_id
                assigned.add(column)
        for column in _DIRECT_URL_COLUMNS:
            if column in kwargs and column not in assigned:
                value = kwargs[column]
                if isinstance(value, str) and value:
                    # Map through the media plan, then scheme-guard the
                    # result: an unmapped ``javascript:``/``data:`` URL from
                    # the attacker's manifest must never land verbatim into
                    # a column a player's <a :href> renders (S2).
                    kwargs[column] = sanitise_landed_url(
                        ctx.url_map.get(value, value),
                    )
                assigned.add(column)
        if table == "characters" and "image_urls" in kwargs:
            kwargs["image_urls"] = self._rewrite_image_urls(
                kwargs["image_urls"], ctx,
            )
            assigned.add("image_urls")
        if table == "messages" and "attachments_json" in kwargs:
            kwargs["attachments_json"] = self._rewrite_attachments(
                kwargs["attachments_json"], ctx,
            )
            assigned.add("attachments_json")

        for column, value in kwargs.items():
            if column in assigned or not isinstance(value, str):
                continue
            kwargs[column] = ctx.tokens.rewrite(value)

        return kwargs, deferred

    async def _flush_rows(
        self,
        table: str,
        batch: list[tuple[dict, list[_DeferredRef]]],
        landing: Any,
        ctx: RestoreContext,
    ) -> None:
        # Resolve deferred existence checks per referenced table.
        wanted: dict[str, set[str]] = {}
        for _row, deferred in batch:
            for ref in deferred:
                wanted.setdefault(ref.referenced_table, set()).add(ref.value)
        existing: dict[str, set[str]] = {}
        for referenced_table, values in wanted.items():
            existing[referenced_table] = await landing.existing_ids(
                referenced_table, sorted(values),
            )
        rows: list[dict] = []
        for row, deferred in batch:
            keep = True
            for ref in deferred:
                if ref.value in existing.get(ref.referenced_table, set()):
                    row[ref.column] = ref.value
                    continue
                if ref.referenced_table == "story_seeds":
                    row[ref.column] = None
                    ctx.report.seed_refs_cleared += 1
                else:
                    # Non-null FK whose target neither landed nor
                    # exists (e.g. series progress without its series).
                    ctx.report.skip(table)
                    keep = False
                    break
            if keep:
                rows.append(row)
        inserted, duplicates = await landing.insert_rows(table, rows)
        ctx.report.rows_landed[table] = (
            ctx.report.rows_landed.get(table, 0) + inserted
        )
        if duplicates:
            ctx.report.skip(table, duplicates)

    def _rewrite_image_urls(self, raw: str, ctx: RestoreContext) -> str:
        try:
            parsed = json.loads(raw or "[]")
        except ValueError:
            return "[]"
        if not isinstance(parsed, list):
            return "[]"
        rewritten = [
            ctx.url_map[url]
            for url in parsed
            if isinstance(url, str) and url in ctx.url_map
        ]
        return json.dumps(rewritten, ensure_ascii=False)

    def _rewrite_attachments(self, raw: str, ctx: RestoreContext) -> str:
        attachments = self.parse_attachments(raw)
        if not attachments:
            return raw if raw else "[]"
        rewritten = []
        for attachment in attachments:
            updated = dict(attachment)
            url = updated.get("url")
            if isinstance(url, str) and url:
                # Same rule as the direct URL columns: map through the plan,
                # then scheme-guard so a hostile ``javascript:`` attachment
                # URL cannot ride an <a :href> into same-origin script (S2).
                updated["url"] = sanitise_landed_url(
                    ctx.url_map.get(url, url),
                )
            rewritten.append(updated)
        return json.dumps(rewritten, ensure_ascii=False)

    def parse_attachments(self, raw: str) -> list[dict]:
        """Tolerant ``attachments_json`` parse. Public: the import
        service reuses it for crash-cleanup reverse-lookup."""
        try:
            parsed = json.loads(raw or "[]")
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    # ---- media transfer ----------------------------------------------

    async def transfer_media(
        self,
        reader: BackupArchiveReader,
        ctx: RestoreContext,
        uploaded_keys: list[str],
    ) -> None:
        """Re-``put`` each referenced original under its new key.

        Real (non-ephemeral) prefixes on purpose: the variant decorator
        wrapped around the container's storage port sees an image
        content type and fans out the WebP renditions by itself (§3.2
        零額外工作)."""
        for old_key, stream in reader.iter_assets():
            transfer = ctx.transfers.get(old_key)
            if transfer is None:
                continue  # unreferenced after folding — never lands
            # Decompressing a large asset is CPU-bound with no awaits —
            # off-thread so the event loop keeps serving (A5). Same
            # whole-bytes buffering as before (the port has no streaming
            # write).
            content = await asyncio.to_thread(
                _read_asset_bytes, stream,
            )
            await self._object_storage.put_bytes(
                object_key=transfer.new_key,
                content=content,
                content_type=transfer.content_type,
            )
            uploaded_keys.append(transfer.new_key)
            ctx.report.media_transferred += 1


def _read_asset_bytes(stream: Any) -> bytes:
    """Chunked whole-file read of one archive asset (worker-thread body)."""
    chunks: list[bytes] = []
    while chunk := stream.read(_UPLOAD_CHUNK):
        chunks.append(chunk)
    return b"".join(chunks)
