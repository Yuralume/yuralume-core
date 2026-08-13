"""Zip container read/write for character backups (``.lumebackup`` inner layer).

The container layout:

```
manifest.json           # BACKUP_SCHEMA_VERSION + stats (owned by DTO layer)
data/<table>.jsonl      # one file per table, one DTO per line
arc_templates/<id>.yaml # bundled arc-template blueprints (0..N)
assets/<object_key>     # media originals keyed by their object-storage key
```

This module knows nothing about characters or domain entities — it only
moves bytes in and out of the zip (the same stance as
``infrastructure.character_card.packager``, which stays untouched: cards
are small and in-memory, backups are GB-scale and must stream).

Differences from the card packager, both driven by scale:

* **Streaming, not in-memory** — the writer appends members straight to a
  caller-provided file-like; the reader hands out per-member streams. A
  multi-GB backup never materialises as one ``bytes``.
* **Enforced decompression budget** — the total-size cap is checked twice:
  fast-reject on the *declared* sizes up front (same as the card
  packager), then re-enforced on *actual* bytes decompressed while
  reading, because a hostile zip can lie in its central directory.
  Actual bytes are charged per member at most once (high-water marks):
  the restore legitimately walks every ``data/*.jsonl`` twice (scan
  pass + landing pass), and double-charging honest re-reads would fail
  archives whose single full decompression fits the budget.

Member-path safety mirrors ``_is_safe_member`` from the card packager
(absolute paths and ``..`` rejected, backslashes normalised) plus a ``:``
rejection so a Windows drive-qualified name can never sneak into a later
storage key. Unknown members are silently ignored by the readers —
forward-compatible with archives a newer exporter produced (same
convention as ``.lumecard``).
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from typing import IO

MANIFEST_NAME = "manifest.json"
DATA_DIR = "data/"
ARC_TEMPLATE_DIR = "arc_templates/"
ASSETS_DIR = "assets/"

# Zip-bomb defence for unpack. Backups legitimately dwarf the 64 MiB cap
# used for cards, so the limit is parameterised per call site.
# Owner confirmed 2026-08-05: 4 GiB is the production v1 total uncompressed cap.
DEFAULT_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024

# Per-member ceilings. The *total* budget alone lets one member declare
# almost the whole 4 GiB and OOM the process the instant a read-whole member
# (manifest / arc-template yaml) or an unbounded jsonl line buffer
# materialises it — 3× the member's size at the peak (chunk list + join +
# decode). These bound each such member well under the total so no single
# member can be the bomb; data files stay uncapped here (they are read line
# by line, bounded by ``_MAX_DATA_LINE_BYTES``) and assets stay uncapped
# (legitimately large media, bounded only by the total budget + streaming).
# Owner confirmed 2026-08-05: these are the production v1 per-member limits.
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_ARC_TEMPLATE_BYTES = 8 * 1024 * 1024
_MAX_DATA_LINE_BYTES = 16 * 1024 * 1024

_COPY_BUFFER = 1024 * 1024


class BackupArchiveError(Exception):
    """Base error for malformed / unreadable backup containers."""


class InvalidBackupArchiveError(BackupArchiveError):
    """The blob is not a readable backup container (bad zip, unsafe
    member path, missing/invalid manifest, undecodable text member)."""


class BackupArchiveTooLargeError(BackupArchiveError):
    """The archive's uncompressed payload exceeds the allowed budget —
    either declared up front or discovered while decompressing."""


class BackupArchiveWriter:
    """Streaming writer for the inner backup zip.

    Appends members straight to ``dest`` (any binary file-like; a real
    temp file for GB-scale exports). Use as a context manager; the zip
    central directory is finalised on close. ``dest`` itself is *not*
    closed — its lifecycle belongs to the caller.
    """

    def __init__(self, dest: IO[bytes]) -> None:
        self._zf = zipfile.ZipFile(
            dest,
            "w",
            zipfile.ZIP_DEFLATED,
            allowZip64=True,
        )

    def __enter__(self) -> BackupArchiveWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._zf.close()

    def write_manifest(self, manifest_json: str) -> None:
        self._zf.writestr(MANIFEST_NAME, manifest_json)

    def open_data_file(self, name: str) -> IO[bytes]:
        """Open ``data/<name>`` for streaming writes (e.g. line by line).

        The caller must close the returned handle (``with`` works) before
        opening the next member — zipfile only allows one open writer.
        """
        member = f"{DATA_DIR}{name}"
        self._require_safe(member)
        return self._zf.open(member, "w", force_zip64=True)

    def write_arc_template(self, filename: str, yaml_text: str) -> None:
        member = f"{ARC_TEMPLATE_DIR}{filename}"
        self._require_safe(member)
        self._zf.writestr(member, yaml_text)

    def write_asset(self, object_key: str, source: IO[bytes]) -> None:
        """Copy a media original into ``assets/<object_key>`` chunk-wise."""
        member = f"{ASSETS_DIR}{object_key}"
        self._require_safe(member)
        with self._zf.open(member, "w", force_zip64=True) as sink:
            while chunk := source.read(_COPY_BUFFER):
                sink.write(chunk)

    @staticmethod
    def _require_safe(member: str) -> None:
        if not _is_safe_member(member):
            raise InvalidBackupArchiveError(f"unsafe member path: {member!r}")


class BackupArchiveReader:
    """Streaming reader for the inner backup zip.

    ``source`` must be a *seekable* binary file-like (zip central
    directories live at the end of the file — decrypt to a temp file
    first). Construction validates every member path and fast-rejects
    archives whose declared uncompressed total exceeds
    ``max_uncompressed_bytes``; the same budget is re-enforced on actual
    bytes decompressed, so a lying central directory cannot bypass it.

    Streams handed out by the iterators are only valid while the reader
    is open and until the next member is requested. Members that do not
    belong to a known category are silently ignored.
    """

    def __init__(
        self,
        source: IO[bytes],
        *,
        max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    ) -> None:
        try:
            self._zf = zipfile.ZipFile(source)
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise InvalidBackupArchiveError("not a valid zip archive") from exc
        declared_total = 0
        declared_sizes: dict[str, int] = {}
        for info in self._zf.infolist():
            if info.is_dir():
                continue
            if not _is_safe_member(info.filename):
                raise InvalidBackupArchiveError(
                    f"unsafe member path: {info.filename!r}",
                )
            size = max(info.file_size, 0)
            member_cap = _member_declared_cap(info.filename)
            if member_cap is not None and size > member_cap:
                # A single member declaring near the whole total budget is
                # the one-member OOM (a read-whole manifest / yaml). Reject
                # the honest oversize up front; a *lying* central directory
                # (small declared, huge actual) is caught while streaming.
                raise BackupArchiveTooLargeError(
                    f"member {info.filename!r} declares {size} uncompressed "
                    f"bytes, over its {member_cap} byte per-member cap",
                )
            declared_sizes[info.filename] = size
            declared_total += size
        if declared_total > max_uncompressed_bytes:
            raise BackupArchiveTooLargeError(
                f"archive declares {declared_total} uncompressed bytes, "
                f"budget is {max_uncompressed_bytes}",
            )
        self._budget = _ReadBudget(max_uncompressed_bytes, declared_sizes)

    def __enter__(self) -> BackupArchiveReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._zf.close()

    def read_manifest(self) -> dict:
        """Read and parse ``manifest.json`` (not yet DTO-validated —
        the import service owns that, same stance as ``.lumecard``)."""
        if MANIFEST_NAME not in self._zf.namelist():
            raise InvalidBackupArchiveError("missing manifest.json")
        with self._zf.open(MANIFEST_NAME) as handle:
            raw = _BudgetedStream(handle, self._budget, MANIFEST_NAME).read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidBackupArchiveError(
                "manifest.json is not valid JSON",
            ) from exc
        if not isinstance(parsed, dict):
            raise InvalidBackupArchiveError(
                "manifest.json top-level must be an object",
            )
        return parsed

    def iter_data_files(self) -> Iterator[tuple[str, IO[bytes]]]:
        """Yield ``(name, stream)`` for each ``data/<name>`` jsonl member."""
        for info, relative in self._iter_category(DATA_DIR):
            if not relative.endswith(".jsonl"):
                continue
            with self._zf.open(info) as handle:
                yield relative, _BudgetedStream(
                    handle, self._budget, info.filename,
                )

    def iter_data_lines(self, filename: str) -> Iterator[bytes]:
        """Yield one JSONL row (newline stripped) of ``data/<filename>``.

        Random-access companion to :meth:`iter_data_files` — the restore
        (CB3) walks tables in registry CARRY order, not zip order, and
        scans some files twice (id-map pass, landing pass). A missing
        member yields nothing (a valid archive has one file per CARRY
        table, but forward-compatible readers must not insist).
        Budget-charged like every other member read.
        """
        member = f"{DATA_DIR}{filename}"
        try:
            info = self._zf.getinfo(member)
        except KeyError:
            return
        with self._zf.open(info) as handle:
            stream = _BudgetedStream(handle, self._budget, member)
            buffer = b""
            while chunk := stream.read(_COPY_BUFFER):
                buffer += chunk
                while (newline := buffer.find(b"\n")) >= 0:
                    line = buffer[:newline]
                    buffer = buffer[newline + 1:]
                    if line.strip():
                        yield line
                if len(buffer) > _MAX_DATA_LINE_BYTES:
                    # A single line with no newline is otherwise an unbounded
                    # buffer: a "one 4 GiB line" jsonl passes the total budget
                    # yet OOMs here. Cap the partial line (S3).
                    raise InvalidBackupArchiveError(
                        f"data line in {member!r} exceeds "
                        f"{_MAX_DATA_LINE_BYTES} bytes",
                    )
            if buffer.strip():
                yield buffer

    def asset_keys(self) -> list[str]:
        """Object keys of every bundled media original (no bytes read)."""
        return [
            relative for _info, relative in self._iter_category(ASSETS_DIR)
        ]

    def iter_arc_templates(self) -> Iterator[tuple[str, str]]:
        """Yield ``(filename, yaml_text)`` for each bundled arc template."""
        for info, relative in self._iter_category(ARC_TEMPLATE_DIR):
            if not relative.endswith(".yaml"):
                continue
            with self._zf.open(info) as handle:
                raw = _BudgetedStream(
                    handle, self._budget, info.filename,
                ).read()
            try:
                yield relative, raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidBackupArchiveError(
                    f"arc template {relative!r} is not valid UTF-8",
                ) from exc

    def iter_assets(self) -> Iterator[tuple[str, IO[bytes]]]:
        """Yield ``(object_key, stream)`` for each media original."""
        for info, relative in self._iter_category(ASSETS_DIR):
            with self._zf.open(info) as handle:
                yield relative, _BudgetedStream(
                    handle, self._budget, info.filename,
                )

    def _iter_category(
        self,
        prefix: str,
    ) -> Iterator[tuple[zipfile.ZipInfo, str]]:
        for info in self._zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not name.startswith(prefix):
                continue
            relative = name[len(prefix) :]
            if relative:
                yield info, relative
        # Members outside every known prefix are silently ignored —
        # forward-compatible with newer exporters (``.lumecard`` rule).


class _ReadBudget:
    """Shared decompression budget across all members of one archive.

    Charges by per-member high-water mark: re-reading a member costs
    nothing beyond the bytes already paid for — the restore legitimately
    walks every ``data/*.jsonl`` twice (scan pass + landing pass), and
    charging both passes would fail honest archives whose single full
    decompression fits the budget. The enforced invariant is therefore
    「one full decompression ≤ limit」, the same quantity the
    declared-size fast-reject checks up front.

    The ability to catch a lying central directory is kept: a member
    whose actual decompressed bytes exceed its own declared size raises
    immediately — that is the archive lying, not the archive being big,
    and the two error messages stay distinct on purpose.
    """

    __slots__ = ("_charged", "_declared", "_high_water", "_limit")

    def __init__(self, limit: int, declared_sizes: dict[str, int]) -> None:
        self._limit = limit
        self._declared = declared_sizes
        self._high_water: dict[str, int] = {}
        self._charged = 0

    def charge(self, member: str, position: int) -> None:
        """Account for ``member`` having been read up to ``position``."""
        declared = self._declared.get(member)
        if declared is not None and position > declared:
            raise BackupArchiveTooLargeError(
                f"member {member!r} decompressed to {position} bytes, "
                f"past its declared size of {declared} — the archive's "
                "central directory lied",
            )
        previous = self._high_water.get(member, 0)
        if position <= previous:
            return  # already paid for (scan pass / landing pass re-read)
        self._charged += position - previous
        self._high_water[member] = position
        if self._charged > self._limit:
            raise BackupArchiveTooLargeError(
                f"archive decompressed to {self._charged} bytes, over "
                f"the {self._limit} byte budget",
            )


class _BudgetedStream:
    """Read-only stream wrapper charging its member's bytes to a shared
    budget (at most once per byte, however often the member is reopened)."""

    __slots__ = ("_budget", "_inner", "_member", "_position")

    def __init__(
        self, inner: IO[bytes], budget: _ReadBudget, member: str,
    ) -> None:
        self._inner = inner
        self._budget = budget
        self._member = member
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        if size is not None and size >= 0:
            data = self._inner.read(size)
            self._position += len(data)
            self._budget.charge(self._member, self._position)
            return data
        # Whole-member read (``read()`` / ``read(-1)``): decompress in
        # bounded chunks, charging each so a lying central directory (small
        # declared size, huge actual payload) trips the per-member lie check
        # or the total budget *before* the whole member materialises. A
        # single ``.read()`` on a hostile member must never OOM the process.
        chunks: list[bytes] = []
        while piece := self._inner.read(_COPY_BUFFER):
            self._position += len(piece)
            self._budget.charge(self._member, self._position)
            chunks.append(piece)
        return b"".join(chunks)


def _member_declared_cap(name: str) -> int | None:
    """Per-member declared-size ceiling, or ``None`` when uncapped.

    Only the members read *whole* into memory get a ceiling: the manifest
    and each arc-template yaml. Data files are read line by line (bounded by
    :data:`_MAX_DATA_LINE_BYTES`) and assets are legitimately large media
    (bounded by the total budget while streaming) — capping either here
    would reject honest heavy backups.
    """
    if name == MANIFEST_NAME:
        return _MAX_MANIFEST_BYTES
    if name.startswith(ARC_TEMPLATE_DIR) and name.endswith(".yaml"):
        return _MAX_ARC_TEMPLATE_BYTES
    return None


def _is_safe_member(name: str) -> bool:
    """Reject absolute paths, ``..`` traversal and drive-qualified names.

    Same contract as the card packager's ``_is_safe_member`` (kept
    separate deliberately — that module is frozen precedent), plus a
    ``:`` rejection: member paths become object-storage keys on restore
    and a ``C:``-style segment must never reach a filesystem-backed
    ``put``.
    """
    if not name or name.startswith(("/", "\\")) or ":" in name:
        return False
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts
