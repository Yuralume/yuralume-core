"""Migration smoke for ``player_persona_notes`` (PP2)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / ".."
    / "alembic"
    / "versions"
    / "n5c1w8z10040_player_persona_notes.py"
).resolve()


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "player_persona_notes", MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.created: list[tuple[str, tuple[Any, ...]]] = []
        self.dropped: list[str] = []

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> None:
        self.created.append((name, columns))

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)


def test_revision_chains_to_the_deferred_intent_head() -> None:
    migration = _load_migration()

    assert migration.revision == "n5c1w8z10040"
    assert migration.down_revision == "k3b0v7y10039"


def test_upgrade_creates_the_pair_keyed_table(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [name for name, _ in recorder.created] == ["player_persona_notes"]
    _name, columns = recorder.created[0]
    primary_keys = {
        column.name for column in columns
        if getattr(column, "primary_key", False)
    }
    assert primary_keys == {"character_id", "operator_id"}


def test_downgrade_drops_the_table(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped == ["player_persona_notes"]
