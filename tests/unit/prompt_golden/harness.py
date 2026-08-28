"""Rendering + snapshot I/O for the prompt golden corpus.

Shared by the test module and ``scripts/regen_prompt_goldens.py`` so a
regenerated snapshot is produced by exactly the same code path that later
verifies it — a regenerator with its own rendering logic would be free to
drift away from the oracle it is supposed to refresh.

Two things are pinned here rather than in the cases:

* **Snapshot bytes.** Read and written as raw UTF-8 with LF endings, never
  through text mode. Text mode's universal-newline translation would make
  a CRLF-corrupted snapshot compare equal on Windows and unequal on CI —
  the exact class of false green a byte-identical oracle exists to avoid.
* **Prompt-pack environment.** ``build()`` renders its footer through the
  process-wide loader, which honours three override env vars. A developer
  with a hosted pack exported would otherwise regenerate the whole corpus
  against someone else's prompt text.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.prompts import reset_default_loader_for_tests

from tests.unit.prompt_golden.cases import GOLDEN_CASES, GoldenCase

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
SNAPSHOT_SUFFIX = ".prompt.txt"

PROMPT_PACK_ENV_VARS = (
    "YURALUME_PROMPT_PACK_DIR",
    "PROMPTS_DIR",
    "KOKORO_PROMPTS_DIR",
)
"""Every env var ``get_default_loader()`` consults, in its own order."""


@contextmanager
def pinned_prompt_environment() -> Iterator[None]:
    """Render against the in-repo baseline prompt pack, whatever the shell says.

    Restores both the env vars and the cached loader on the way out so a
    test session that mixes this corpus with pack-override tests is not
    left holding a loader built under the wrong roots.
    """
    saved = {name: os.environ.get(name) for name in PROMPT_PACK_ENV_VARS}
    for name in PROMPT_PACK_ENV_VARS:
        os.environ.pop(name, None)
    reset_default_loader_for_tests()
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        reset_default_loader_for_tests()


def render_case(case: GoldenCase) -> str:
    """Build the prompt for one case. Pure — no snapshot is touched."""
    builder = DefaultPromptContextBuilder(**dict(case.builder_kwargs))
    return builder.build(**dict(case.build_kwargs()))


def snapshot_path(case: GoldenCase) -> Path:
    return SNAPSHOT_DIR / f"{case.name}{SNAPSHOT_SUFFIX}"


def read_snapshot(case: GoldenCase) -> str:
    """Decode the stored bytes verbatim — no newline translation."""
    return snapshot_path(case).read_bytes().decode("utf-8")


def write_snapshot(case: GoldenCase, prompt: str) -> Path:
    """Write UTF-8 / LF bytes verbatim. Returns the path written."""
    path = snapshot_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prompt.encode("utf-8"))
    return path


def format_diff(case: GoldenCase, expected: str, actual: str) -> str:
    """Line-level unified diff, readable in a pytest failure report."""
    diff = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile=f"snapshot/{case.name}",
        tofile=f"rendered/{case.name}",
        lineterm="\n",
        n=3,
    )
    body = "".join(diff).rstrip("\n")
    if not body:
        # Equal line-by-line but unequal as strings ⇒ a trailing-newline or
        # line-ending difference the line diff cannot show.
        return (
            f"{case.name}: no line-level difference, but the bytes differ "
            f"(snapshot {len(expected.encode('utf-8'))} bytes, rendered "
            f"{len(actual.encode('utf-8'))} bytes) — check line endings and "
            "the trailing newline."
        )
    return (
        f"{case.name}: rendered prompt no longer matches its frozen snapshot.\n"
        "If the change is intentional, regenerate with "
        "ALLOW_PROMPT_GOLDEN_UPDATE=1 python scripts/regen_prompt_goldens.py\n"
        f"{body}"
    )


def regenerate_all() -> list[tuple[GoldenCase, Path, bool]]:
    """Re-render every case and write its snapshot.

    Returns ``(case, path, changed)`` per case so the caller can report
    what actually moved instead of claiming the whole corpus changed.
    """
    results: list[tuple[GoldenCase, Path, bool]] = []
    with pinned_prompt_environment():
        for case in GOLDEN_CASES:
            prompt = render_case(case)
            path = snapshot_path(case)
            previous = path.read_bytes() if path.exists() else None
            write_snapshot(case, prompt)
            results.append((case, path, previous != prompt.encode("utf-8")))
    return results


def orphan_snapshots() -> list[Path]:
    """Snapshot files with no case left claiming them."""
    if not SNAPSHOT_DIR.exists():
        return []
    expected = {snapshot_path(case) for case in GOLDEN_CASES}
    return sorted(
        path
        for path in SNAPSHOT_DIR.glob(f"*{SNAPSHOT_SUFFIX}")
        if path not in expected
    )
