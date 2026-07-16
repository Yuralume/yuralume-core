from __future__ import annotations

from pathlib import Path

from scripts.check_clock_guard import find_violations


def test_clock_guard_blocks_new_direct_datetime_now_usage(tmp_path: Path) -> None:
    source = tmp_path / "src/kokoro_link/application/services/new_service.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from datetime import datetime, timezone\n"
        "def run():\n"
        "    return datetime.now(timezone.utc)\n",
        encoding="utf-8",
    )
    allowlist = tmp_path / "scripts/clock_guard_allowlist.txt"
    allowlist.parent.mkdir(parents=True)
    allowlist.write_text("", encoding="utf-8")

    assert find_violations([source], project_root=tmp_path) == [
        "src/kokoro_link/application/services/new_service.py",
    ]


def test_clock_guard_honors_existing_allowlist(tmp_path: Path) -> None:
    source = tmp_path / "src/kokoro_link/application/services/legacy.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from datetime import datetime, timezone\n"
        "def run():\n"
        "    return datetime.now(timezone.utc)\n",
        encoding="utf-8",
    )
    allowlist = tmp_path / "scripts/clock_guard_allowlist.txt"
    allowlist.parent.mkdir(parents=True)
    allowlist.write_text(
        "src/kokoro_link/application/services/legacy.py\n",
        encoding="utf-8",
    )

    assert find_violations([source], project_root=tmp_path) == []

