"""Loader for the `llm_output` differential corpus.

Reads `tests/fixtures/llm_output_corpus/` and expands the degradation
matrix. See that directory's README for what the corpus is for and how
to add to it.

Cases are `(case_id, raw)` pairs; ids are stable and unique so a pytest
failure names the exact input.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


_FIXTURES = (
    Path(__file__).resolve().parents[2] / "fixtures" / "llm_output_corpus"
)


def _load(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _literal_text(entry: dict) -> str:
    """The case's raw string, expanding the optional repeat form.

    ``{"prefix": …, "raw": "[", "repeat": 3000}`` beats pasting three
    thousand brackets into the fixture: the pathological cases (a model
    looping on one opener) are only interesting at a length that would
    make the file unreadable, and spelling the shape out keeps the
    *reason* for the length visible next to the ``note``.
    """
    return (
        entry.get("prefix", "")
        + entry["raw"] * entry.get("repeat", 1)
        + entry.get("suffix", "")
    )


def _literal_cases() -> list[tuple[str, str]]:
    raw = _load("literal_cases.json")
    assert isinstance(raw, list)
    return [(entry["id"], _literal_text(entry)) for entry in raw]


def _matrix_cases() -> list[tuple[str, str]]:
    """Cross-product the seeds with contamination and truncation.

    Two families, kept apart because they model different upstream
    failures:

    * ``prefix + seed + suffix`` — the model wrapped a complete payload
      in a fence, a preamble, or trailing commentary.
    * ``prefix + seed[:cut]`` — the reply was cut off. No suffix: a
      truncated response has nothing after it, and appending one would
      quietly turn the case into a *complete* payload with junk on the
      end, which the other family already covers.

    The fractions are of the seed body only, so the cut lands in the
    same structural position regardless of how long the prefix is.
    """
    spec = _load("degradation_matrix.json")
    assert isinstance(spec, dict)
    seeds = [*spec["objects"], *spec["arrays"]]
    prefixes = spec["prefixes"]
    suffixes = spec["suffixes"]
    fractions = spec["truncate_fractions"]

    cases: list[tuple[str, str]] = []
    for seed in seeds:
        body = seed["text"]
        for prefix in prefixes:
            for suffix in suffixes:
                cases.append((
                    f"matrix.{seed['id']}.{prefix['id']}.{suffix['id']}",
                    prefix["text"] + body + suffix["text"],
                ))
            for fraction in fractions:
                cut = max(1, int(len(body) * fraction))
                cases.append((
                    f"trunc.{seed['id']}.{prefix['id']}.{int(fraction * 100)}",
                    prefix["text"] + body[:cut],
                ))
    return cases


PATHOLOGICAL_PREFIX = "pathological."
"""Id prefix for cases no frozen oracle can be *run* on (FX1/DH-1).

The pathological family is deep unclosed nesting — the shape a model
stuck in a repetition loop emits. ``json.loads`` answers that with a
``RecursionError``, so the pre-migration extractors, which caught only
``JSONDecodeError``, crash outright on these inputs.

That makes them useless to the differential comparison and essential to
the total-function one. "New reads everything old read" has no content
when old raised instead of reading; "no site raises on any input" is
precisely the property these cases exist to hold down. So
:func:`corpus` leaves them out and :func:`total_function_corpus`
includes them. That the old code really does crash on them is not left
implicit — ``test_fx1_hardening.py`` asserts it directly, so the fact
lives somewhere a reader will find it rather than in a silent skip.
"""


@lru_cache(maxsize=1)
def total_function_corpus() -> tuple[tuple[str, str], ...]:
    """Every corpus case as ``(case_id, raw)``, ids guaranteed unique.

    Use this for "must not raise" assertions. For differential
    comparisons against a frozen oracle use :func:`corpus` — see
    :data:`PATHOLOGICAL_PREFIX`.
    """
    cases = [*_literal_cases(), *_matrix_cases()]
    ids = [case_id for case_id, _ in cases]
    assert len(ids) == len(set(ids)), "duplicate corpus case id"
    return tuple(cases)


@lru_cache(maxsize=1)
def corpus() -> tuple[tuple[str, str], ...]:
    """The cases a frozen oracle can be run on, as ``(case_id, raw)``."""
    return tuple(
        case for case in total_function_corpus()
        if not case[0].startswith(PATHOLOGICAL_PREFIX)
    )


def corpus_ids() -> list[str]:
    return [case_id for case_id, _ in corpus()]


def corpus_raws() -> list[str]:
    return [raw for _, raw in corpus()]
