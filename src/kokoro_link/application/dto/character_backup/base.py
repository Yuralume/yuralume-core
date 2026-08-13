"""Shared base for ``.lumebackup`` per-table DTOs (CB0).

Every DTO in this package is a row-level projection of exactly one ORM
table: **field names match column names one-to-one** (the registry pin
test enforces ``dto fields == table columns − excluded columns``), and
values are the portable form of the column value —

- ``datetime`` / ``date`` fields stay typed; the JSONL boundary uses
  pydantic's JSON serialization (ISO 8601) and parsing, so in-process
  code keeps real datetimes while the archive carries strings,
- JSON-in-Text columns (``*_json``, ``tags``, ``world_frames``, …) are
  carried verbatim as their raw string — the backup does not re-parse
  content it doesn't own.

Version tolerance (plan §4): the archive-level gate rejects a
``BACKUP_SCHEMA_VERSION`` newer than this build; *older* archives are
tolerated by pydantic defaults — every field added after v1 must ship
with a default, and unknown fields from same-major drift are ignored
(``extra="ignore"``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict

from kokoro_link.contracts.clock import ensure_utc


class BackupRecord(BaseModel):
    """One exported table row.

    ``from_row`` reads attributes named exactly like the DTO's fields
    from an ORM row (datetimes normalised to aware UTC — SQLite loses
    tzinfo on round-trip); ``to_row_kwargs`` is the restore-direction
    mapping: a column-name → value dict ready to feed the ORM row
    constructor *after* the restore job has rewritten the remapped ids
    (D4: all ids are reissued on import).
    """

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def from_row(cls, row: Any) -> Self:
        data: dict[str, Any] = {}
        for name in cls.model_fields:
            value = getattr(row, name)
            if isinstance(value, datetime):
                value = ensure_utc(value)
            data[name] = value
        return cls(**data)

    def to_row_kwargs(self) -> dict[str, Any]:
        return self.model_dump()
