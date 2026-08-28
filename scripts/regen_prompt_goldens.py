"""Regenerate the frozen prompt golden corpus.

The goldens in ``tests/unit/prompt_golden/snapshots/`` are the
byte-identical oracle the DH4b/DH5 prompt refactors are measured against.
Refreshing them is therefore a *deliberate* act: it throws away the
evidence that the old behaviour existed. So this script refuses to run
unless ``ALLOW_PROMPT_GOLDEN_UPDATE`` is set — the same shape as the
baseline prompt-pack guard's ``ALLOW_BASELINE_PROMPT_UPDATE``.

Legitimate reasons to regenerate:

* a deliberate, reviewed prompt change (new wording, new block, DH5's
  reordering) — regenerate in its **own commit** so the diff is only the
  intended change;
* a change to the baseline pack under ``src/kokoro_link/data/prompts/``
  that the footer renders from.

"the test is red" is not, by itself, one of them.

Usage (from the Core repo root)::

    ALLOW_PROMPT_GOLDEN_UPDATE=1 python scripts/regen_prompt_goldens.py
    ALLOW_PROMPT_GOLDEN_UPDATE=1 python scripts/regen_prompt_goldens.py --check
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ALLOW_ENV = "ALLOW_PROMPT_GOLDEN_UPDATE"
_TRUTHY_VALUES = frozenset({"1", "true", "yes", "y", "on"})

REPO_ROOT = Path(__file__).resolve().parents[1]


def is_truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUTHY_VALUES


def _ensure_importable() -> None:
    """Allow ``python scripts/regen_prompt_goldens.py`` from the repo root.

    Mirrors the ``pythonpath = ["src", "."]`` pytest setting so the script
    and the test import the same modules.
    """
    for entry in (REPO_ROOT / "src", REPO_ROOT):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the prompt golden snapshots.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report which snapshots would change without writing them",
    )
    args = parser.parse_args(argv)

    if not is_truthy(os.environ.get(ALLOW_ENV)):
        print(
            f"refusing to touch the prompt goldens: {ALLOW_ENV} is not set.\n"
            "These snapshots are the byte-identical oracle for the prompt "
            "refactor; regenerating them discards the evidence of the old "
            "behaviour. If the change is intentional, re-run as:\n"
            f"  {ALLOW_ENV}=1 python scripts/regen_prompt_goldens.py",
            file=sys.stderr,
        )
        return 2

    _ensure_importable()
    from tests.unit.prompt_golden import harness  # noqa: PLC0415

    if args.check:
        changed = _report_check(harness)
        return 1 if changed else 0

    results = harness.regenerate_all()
    changed = [case.name for case, _path, did_change in results if did_change]
    print(f"regenerated {len(results)} snapshot(s) in {harness.SNAPSHOT_DIR}")
    if changed:
        print("changed: " + ", ".join(sorted(changed)))
        print("review the diff — it must contain only the change you intended.")
    else:
        print("no snapshot changed.")

    orphans = harness.orphan_snapshots()
    if orphans:
        print(
            "orphan snapshot file(s) with no matching case — delete them: "
            + ", ".join(path.name for path in orphans),
        )
    return 0


def _report_check(harness) -> bool:  # noqa: ANN001 - module object
    """Dry run: render everything, report drift, write nothing."""
    changed: list[str] = []
    with harness.pinned_prompt_environment():
        for case in harness.GOLDEN_CASES:
            path = harness.snapshot_path(case)
            rendered = harness.render_case(case).encode("utf-8")
            stored = path.read_bytes() if path.exists() else None
            if stored != rendered:
                changed.append(case.name if stored is not None
                               else f"{case.name} (missing)")
    if changed:
        print("would change: " + ", ".join(sorted(changed)))
    else:
        print("all snapshots up to date.")
    return bool(changed)


if __name__ == "__main__":
    raise SystemExit(main())
