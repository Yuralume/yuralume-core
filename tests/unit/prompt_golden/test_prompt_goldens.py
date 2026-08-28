"""Byte-identical golden corpus for ``DefaultPromptContextBuilder.build()``.

Frozen **before** the DH4b/DH5 refactor so the refactor has a mechanical
oracle: ``build()`` may be rewritten into a section registry, but every
snapshot here must still come out byte for byte identical. When one of
these fails, the default assumption is that the refactor changed output —
not that the snapshot is stale.

Regenerating is deliberately awkward (``ALLOW_PROMPT_GOLDEN_UPDATE=1``,
see ``scripts/regen_prompt_goldens.py``) because a one-keystroke refresh
would let a regression overwrite the very evidence of itself.
"""

from __future__ import annotations

import pytest

from tests.unit.prompt_golden import harness
from tests.unit.prompt_golden.cases import (
    BRANCH_PAIRS,
    GOLDEN_CASES,
    GoldenCase,
)


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    """Render the whole corpus once, under a pinned prompt-pack env."""
    with harness.pinned_prompt_environment():
        return {case.name: harness.render_case(case) for case in GOLDEN_CASES}


def _case_id(case: GoldenCase) -> str:
    return case.name


# --- the oracle ------------------------------------------------------


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=_case_id)
def test_prompt_matches_frozen_snapshot(
    case: GoldenCase, rendered: dict[str, str],
) -> None:
    path = harness.snapshot_path(case)
    assert path.exists(), (
        f"missing snapshot for case {case.name!r} ({path}). Create it with "
        "ALLOW_PROMPT_GOLDEN_UPDATE=1 python scripts/regen_prompt_goldens.py"
    )
    expected = harness.read_snapshot(case)
    actual = rendered[case.name]
    if expected != actual:
        pytest.fail(harness.format_diff(case, expected, actual), pytrace=False)


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=_case_id)
def test_snapshot_bytes_are_utf8_lf(case: GoldenCase) -> None:
    """A CRLF-converted snapshot is not the same oracle it was frozen as."""
    raw = harness.snapshot_path(case).read_bytes()
    assert b"\r" not in raw, (
        f"{case.name}: snapshot contains CR bytes — it was rewritten in text "
        "mode or checked out with autocrlf. Snapshots are UTF-8 / LF."
    )
    raw.decode("utf-8")  # raises on any non-UTF-8 byte


# --- determinism -----------------------------------------------------


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=_case_id)
def test_rendering_is_stable_across_repeated_builds(case: GoldenCase) -> None:
    """Two builds of the same case must agree.

    Catches wall-clock reads, ``uuid4`` leaking into output, and any
    iteration order that depends on process state. Deliberately re-renders
    rather than reusing the module fixture — the fixture caches, and a
    cache cannot prove repeatability.
    """
    with harness.pinned_prompt_environment():
        first = harness.render_case(case)
        second = harness.render_case(case)
    assert first == second, (
        f"{case.name}: two consecutive builds of identical inputs differ — "
        "something in build() reads the clock, randomness, or process state."
    )


def test_no_case_leaks_a_random_identifier(rendered: dict[str, str]) -> None:
    """No ``uuid4``-shaped token may reach the prompt.

    ``build()`` prints ``對話 ID`` verbatim, so a case that forgot to pin
    its conversation id would produce a snapshot that can never match
    again. Every id in the corpus is a literal with a ``xxx-golden-`` stem.
    """
    import re

    uuid_re = re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    )
    offenders = {
        name: uuid_re.findall(text)
        for name, text in rendered.items()
        if uuid_re.search(text)
    }
    assert not offenders, f"unpinned identifiers reached the prompt: {offenders}"


# --- structural guards ----------------------------------------------


def test_case_names_are_unique() -> None:
    names = [case.name for case in GOLDEN_CASES]
    assert len(names) == len(set(names)), "duplicate golden case name"


def test_matrix_covers_both_sides_of_every_exclusive_branch() -> None:
    """The acceptance rule: no either/or path is guarded on one side only."""
    claimed = {branch for case in GOLDEN_CASES for branch in case.branches}
    known = {side for pair in BRANCH_PAIRS for side in pair}
    unknown = claimed - known
    assert not unknown, (
        f"cases claim branches missing from BRANCH_PAIRS: {sorted(unknown)}"
    )
    uncovered = sorted(side for side in known if side not in claimed)
    assert not uncovered, (
        "these branch sides have no fixture standing on them: "
        f"{uncovered}"
    )


def test_matrix_is_at_least_the_planned_size() -> None:
    """DH4 step 0 requires ≥14 fixtures; the floor is asserted, not assumed."""
    assert len(GOLDEN_CASES) >= 14


def test_no_orphan_snapshot_files() -> None:
    """A renamed case must not leave a stale snapshot pretending to be one."""
    orphans = harness.orphan_snapshots()
    assert not orphans, (
        "snapshot files with no matching case: "
        f"{[path.name for path in orphans]}"
    )


# --- marker claims ---------------------------------------------------


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=_case_id)
def test_case_still_exercises_the_branch_it_claims(
    case: GoldenCase, rendered: dict[str, str],
) -> None:
    """Markers keep a case honest about *why* it is in the corpus.

    Without this, an upstream default change could turn a branch case into
    a duplicate of ``minimal`` and the snapshot would happily re-freeze the
    weaker coverage.
    """
    prompt = rendered[case.name]
    for marker in case.markers:
        assert marker in prompt, (
            f"{case.name}: expected marker missing — {marker!r}"
        )
    for marker in case.absent_markers:
        assert marker not in prompt, (
            f"{case.name}: marker should be suppressed but rendered — "
            f"{marker!r}"
        )
