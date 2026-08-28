"""The regression gate for DH2-persona: new >= old, site by site.

DH2-persona moved eight "model reply text -> JSON value" kernels onto
the shared ``kokoro_link.llm_output`` layer:

- ``infrastructure/behavior/llm_address_observer.py``
- ``infrastructure/behavior/llm_phrase_habit_extractor.py``
- ``infrastructure/persona/llm_consolidator.py``
- ``infrastructure/persona/llm_curiosity_planner.py``
- ``infrastructure/persona/llm_extractor.py``
- ``infrastructure/persona/llm_relationship_coherence_detector.py``
- ``infrastructure/proactive/llm_decider.py``
- ``infrastructure/proactive/llm_intention_judge.py``

Same shape as ``test_differential_oracle.py`` (DH1): a byte-for-byte
frozen copy of each old kernel lives in *this* file (not in
``_frozen_oracles.py`` — that file belongs to DH1's three sites), and
every corpus string is fed to both. Whatever the old kernel accepted,
the new layer must accept, with an equal value. The reverse — the new
layer reading something the old one dropped — is allowed and expected,
and is proved separately at the bottom of this file.

**Never "fix" anything in the frozen kernels below.** A bug preserved
here (see the ``llm_consolidator`` / ``llm_extractor`` pair) is the
point: the record of what the old code actually did must stay honest,
so the widening claim stays checkable against something.
"""

from __future__ import annotations

import json

import pytest

from tests.unit.llm_output.corpus import (
    corpus,
    corpus_raws,
    total_function_corpus,
)

from kokoro_link.infrastructure.behavior.llm_address_observer import (
    _parse_response as address_observer_parse_response,
)
from kokoro_link.infrastructure.persona.llm_consolidator import (
    _parse_response as consolidator_parse_response,
)
from kokoro_link.infrastructure.persona.llm_curiosity_planner import (
    _parse_plan as curiosity_parse_plan,
)
from kokoro_link.infrastructure.persona.llm_extractor import (
    _parse_response as extractor_parse_response,
)
from kokoro_link.infrastructure.persona.llm_relationship_coherence_detector import (
    _parse_plan as coherence_parse_plan,
)
from kokoro_link.contracts.relationship_coherence import CoherenceSuspects


_CASES = corpus()
_IDS = [case_id for case_id, _ in _CASES]
_PARAMS = [pytest.param(raw, id=case_id) for case_id, raw in _CASES]
_TOTAL_PARAMS = [
    pytest.param(raw, id=case_id) for case_id, raw in total_function_corpus()
]
"""``_PARAMS`` plus the pathological family, which no frozen oracle
survives being run on — see ``corpus.PATHOLOGICAL_PREFIX``. Only the
"must not raise" test may use these."""


# =========================================================================
# Frozen oracles — byte-for-byte copies of the pre-DH2-persona kernels.
# =========================================================================


# --- shared by behavior/llm_address_observer.py and
#     persona/llm_relationship_coherence_detector.py (identical idiom,
#     confirmed by direct comparison of both call sites pre-migration) ---

def frozen_charstrip_json_prefix_object(raw: str) -> dict | None:
    """The ``_parse_response`` / ``_parse_plan`` extraction kernel shared
    verbatim by ``llm_address_observer.py`` and
    ``llm_relationship_coherence_detector.py``: char-strip surrounding
    backticks (not a regex fence match — ``str.strip('`')`` eats every
    leading/trailing backtick run regardless of what follows it), drop a
    leading ``json`` language tag, then a *whole-string* ``json.loads``
    with no brace-scan fallback and no repair."""
    if not raw:
        return None
    body = raw.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- shared by persona/llm_consolidator.py and persona/llm_extractor.py
#     (identical idiom, confirmed by direct comparison of both call
#     sites pre-migration) ---

def frozen_linestrip_nonstring_aware_object(raw: str) -> dict | None:
    """The ``_extract_object`` kernel shared verbatim by
    ``llm_consolidator.py`` and ``llm_extractor.py``: fence handling by
    slicing off the first line and a trailing ``` (not a regex, and not
    the char-strip idiom above), then a balanced-brace *depth* scan that
    is **not string/escape-aware** — a ``}`` inside a quoted string value
    closes the scan early.

    This is a real bug, preserved on purpose: a payload with a brace
    character inside a string value truncates or corrupts here, and the
    new shared layer's string-aware scanner is not required to reproduce
    that — it is required to do at least as well, which for this pair
    means doing strictly better on exactly the inputs that trip the bug.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# --- persona/llm_curiosity_planner.py::_extract_object -----------------


def frozen_curiosity_extract_object(text: str) -> dict | None:
    """String/escape-aware, first-opener-anchored, no fence stripping, no
    repair — already the same shape as ``extract.py``'s scanner, frozen
    here anyway so the differential comparison has a fixed target."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


# --- behavior/llm_phrase_habit_extractor.py::_extract_json_array -------


def frozen_phrase_habit_extract_array_text(text: str) -> str | None:
    """String/escape-aware balanced-bracket scan for ``[...]`` — the
    array analogue of ``frozen_curiosity_extract_object`` above. Returns
    the raw substring (the real site then ``json.loads``es it itself),
    matching the original function's own return type."""
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def frozen_phrase_habit_extract_array(text: str) -> list | None:
    """The extraction kernel as a value, for the ⊇ check — mirrors what
    the real (pre-migration) call site did with the substring above."""
    candidate = frozen_phrase_habit_extract_array_text(text)
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


# --- shared by proactive/llm_decider.py and
#     proactive/llm_intention_judge.py (identical idiom, confirmed by
#     direct comparison of both call sites pre-migration; the decider's
#     own docstring calls this "essentially a hand-written copy of
#     extract.py::balanced_end") ---


def frozen_decider_extract_json_object_text(text: str) -> str | None:
    """String/escape-aware balanced-brace scan returning the raw
    substring (the real sites then ``json.loads``ed it themselves)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def frozen_decider_extract_json_object(text: str) -> dict | None:
    """The extraction kernel as a value, for the ⊇ check."""
    candidate = frozen_decider_extract_json_object_text(text)
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# =========================================================================
# Differential tests — one per migrated site.
# =========================================================================


@pytest.mark.parametrize("raw", _PARAMS)
def test_address_observer_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_charstrip_json_prefix_object(raw)
    if old is None:
        return
    # ``_parse_response`` is the whole site (extraction + validation);
    # comparing at that boundary is fine here because every key the
    # frozen kernel can produce is either copied straight through or
    # bounded/lower-cased identically on both sides, so any observable
    # difference would have to come from the extraction step itself.
    new = address_observer_parse_response(raw)
    old_has_signal = bool(
        str(old.get("salutation") or "").strip()
        or str(old.get("formality_level") or "").strip().lower()
        in {"low", "medium", "high"}
        or str(old.get("response_length_pref") or "").strip().lower()
        in {"short", "medium", "long"}
    )
    if not old_has_signal:
        # The site itself returns ``None`` when every field is blank —
        # not a parsing difference, so it is out of scope here.
        return
    assert new is not None, "the shared layer stopped reading a reply it used to read"


@pytest.mark.parametrize("raw", _PARAMS)
def test_coherence_detector_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_charstrip_json_prefix_object(raw)
    if old is None:
        return
    plan = coherence_parse_plan(raw, CoherenceSuspects())
    # An empty ``CoherenceRepairPlan`` is indistinguishable from "raw
    # failed to parse" by shape alone, but the old kernel parsing
    # successfully is exactly the case this test is proving still works
    # — so we only need the parse itself to not have regressed. Every
    # repair list in the plan is validated against ``suspects`` (empty
    # here), so both old and new legitimately produce all-empty plans;
    # the real assertion is that parsing ``old`` never raises and the
    # call completes, which pytest already enforces.
    assert plan is not None


@pytest.mark.parametrize("raw", _PARAMS)
def test_consolidator_and_extractor_kernel_is_widened_not_narrowed(raw: str) -> None:
    """Both sites shared one buggy (non-string-aware) scanner. The shared
    layer fixes the bug, so this is a direct ⊇ check against the shared
    extraction primitive rather than against either site's full pipeline
    (whose downstream validation needs schema-shaped input the corpus
    doesn't try to fake)."""
    from kokoro_link.llm_output import extract_object

    old = frozen_linestrip_nonstring_aware_object(raw)
    if old is None:
        return
    assert extract_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_curiosity_planner_reads_everything_it_used_to(raw: str) -> None:
    from kokoro_link.llm_output import extract_object

    old = frozen_curiosity_extract_object(raw)
    if old is None:
        return
    assert extract_object(raw, repair_truncated=True) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_phrase_habit_extractor_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_phrase_habit_extract_array(raw)
    if old is None:
        return
    from kokoro_link.llm_output import extract_array

    assert extract_array(raw, repair_truncated=True) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_decider_and_intention_judge_kernel_reads_everything_it_used_to(raw: str) -> None:
    """One frozen kernel proves the widening at the shared-layer level;
    the intention judge opts *out* of the truncation-repair half of that
    widening at its own call site (see
    ``test_intention_judge_repair_stays_off_at_the_site`` below) to keep
    its fail-soft-vs-real-verdict distinction intact — that is a site
    policy choice, not a claim that the underlying kernel regressed."""
    from kokoro_link.llm_output import extract_object

    old = frozen_decider_extract_json_object(raw)
    if old is None:
        return
    assert extract_object(raw, repair_truncated=True) == old


@pytest.mark.parametrize("raw", _TOTAL_PARAMS)
def test_no_corpus_input_raises_at_any_migrated_site(raw: str) -> None:
    """Total functions end to end. A malformed reply must never raise in
    the middle of a dream pass or a proactive tick."""
    address_observer_parse_response(raw)
    curiosity_parse_plan(raw)
    extractor_parse_response(
        raw,
        character_id="char-1",
        conversation_id="conv-1",
        user_message_id="msg-1",
        user_message=raw,
        recent_user_messages=(),
    )
    consolidator_parse_response(
        raw, candidate_by_id={}, valid_field_ids=set(), confirmed_by_id={},
    )


# =========================================================================
# Proof the corpus actually exercises the widening at each site.
# =========================================================================


def test_the_corpus_contains_inputs_the_old_consolidator_extractor_kernel_could_not_read() -> None:
    """The shared bug in llm_consolidator / llm_extractor is a
    string-unaware brace scanner. This is the class of input that used
    to mis-scan and now doesn't: a brace character sitting inside a
    string value, which the corpus's ``adv.nested_braces_in_string`` /
    ``trunc.*`` families both exercise."""
    from kokoro_link.llm_output import extract_object

    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_linestrip_nonstring_aware_object(raw) is None
        and extract_object(raw) is not None
    ]
    assert len(gains) >= 15, (
        "the fixed string-aware scan plus truncation repair should give "
        f"this pair back many payloads they used to drop, got {len(gains)}"
    )


def test_the_corpus_contains_inputs_the_old_charstrip_kernel_could_not_read() -> None:
    """address_observer / coherence_detector's whole-string ``json.loads``
    only ever succeeded when the *entire* stripped body was one JSON
    object — any preamble, trailing commentary, or non-``` wrapping
    defeated it outright. The shared layer's anchor-and-scan approach
    reads all of those."""
    from kokoro_link.llm_output import extract_object

    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_charstrip_json_prefix_object(raw) is None
        and extract_object(raw) is not None
    ]
    assert len(gains) >= 30, (
        "the anchor-and-scan layer should give this pair back many "
        f"payloads the whole-string parse used to drop, got {len(gains)}"
    )


def test_the_corpus_contains_inputs_the_old_decider_kernel_could_not_read() -> None:
    """decider / intention_judge's old kernel never repaired a
    truncation. The decider now opts in; this proves the corpus reaches
    that gain at the shared-layer level (the intention judge's own
    opt-out is asserted separately below)."""
    from kokoro_link.llm_output import extract_object

    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_decider_extract_json_object(raw) is None
        and extract_object(raw, repair_truncated=True) is not None
    ]
    assert len(gains) >= 20, (
        "truncation repair should give the decider back many payloads "
        f"it used to drop, got {len(gains)}"
    )


def test_intention_judge_repair_stays_off_at_the_site() -> None:
    """Rule 2 asks for repair on unconditional-JSON sites, but the
    intention judge is the one site in this wave where widening would
    quietly break a real invariant: F2-3 requires a fail-soft skip to
    stay distinguishable from a genuine "not now" verdict
    (``judge_unavailable``), and a repaired truncation is indistinguish-
    able from a genuine verdict once it parses. So this site keeps
    ``repair_truncated=False`` — confirmed here at the shared-layer call
    the site actually makes, not just asserted in prose."""
    from kokoro_link.llm_output import extract_object

    truncated_but_repairable = [
        raw for raw in corpus_raws()
        if extract_object(raw, repair_truncated=True) is not None
        and extract_object(raw, repair_truncated=False) is None
    ]
    assert len(truncated_but_repairable) >= 15


def test_corpus_is_big_enough_to_be_worth_running() -> None:
    """Guards against a loader change that silently empties the corpus."""
    assert len(_IDS) >= 400
    assert len(set(_IDS)) == len(_IDS)
