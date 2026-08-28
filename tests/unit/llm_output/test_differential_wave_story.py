"""The regression gate for DH2-story: new >= old, site by site.

DH2-story moved eleven "model reply text -> JSON value" kernels onto the
shared ``kokoro_link.llm_output`` layer:

- ``infrastructure/story/llm_arc_completion_memory_writer.py``
- ``infrastructure/story/llm_arc_planner.py``
- ``infrastructure/story/llm_arc_template_translator.py``
- ``infrastructure/story/llm_beat_rechecker.py``
- ``infrastructure/story/llm_beat_scene_writer.py``
- ``infrastructure/story/llm_expander.py``
- ``infrastructure/story/llm_scene_chips.py``
- ``infrastructure/story/llm_scene_closer.py``
- ``infrastructure/story/llm_scene_opener.py``
- ``infrastructure/story/llm_season_decider.py``
- ``infrastructure/story/llm_story_seed_translator.py``

Same shape as ``test_differential_oracle.py`` (DH1) and
``test_differential_wave_persona.py`` (DH2-persona): a byte-for-byte
frozen copy of each old kernel lives in *this* file (not in
``_frozen_oracles.py`` — that file belongs to DH1's three sites), and
every corpus string is fed to both. Whatever the old kernel accepted,
the new layer must accept, with an equal value. The reverse — the new
layer reading something the old one dropped — is allowed and expected,
and is proved separately near the bottom of this file.

**Never "fix" anything in the frozen kernels below.** A bug preserved
here (the naive, non-balanced ``find('{')``/``rfind('}')`` slice used by
four of these sites) is the point: the record of what the old code
actually did must stay honest, so the widening claim stays checkable
against something.

Three extraction families are in play:

- **naive slice, no fence, no repair** — only
  ``llm_arc_completion_memory_writer.py``. Not balanced: a stray ``}``
  in trailing prose can grab the wrong span.
- **fence-regex strip + naive slice, no repair** — shared verbatim,
  pre-migration, by ``llm_arc_planner.py``, ``llm_beat_rechecker.py``
  and ``llm_season_decider.py``.
- **string/escape-aware balanced-brace scan, no repair** — shared
  verbatim, pre-migration, by ``llm_beat_scene_writer.py``,
  ``llm_expander.py``, ``llm_scene_chips.py`` and ``llm_scene_closer.py``
  — the same algorithm DH1 already froze as
  ``frozen_extract_first_object`` in ``_frozen_oracles.py``, reused here
  read-only rather than re-typed.

Two sites are one-offs:

- ``llm_scene_opener.py`` uses the balanced-brace scan above but decodes
  with ``json.loads(payload, strict=False)`` — load-bearing tolerance
  for models that put literal newlines inside a JSON string value
  instead of ``\\n`` escapes. The shared layer's decode is strict, so
  the migrated site tries the shared layer first (gaining truncation
  repair) and falls back to the old lenient decode only when that fails
  — see ``frozen_scene_opener_parse_payload`` below and the site's own
  ``_parse_payload``.
- ``llm_arc_template_translator.py`` and ``llm_story_seed_translator.py``
  share one ``_parse_json_object`` verbatim (also present, out of this
  wave's scope, in ``character_card/llm_translator.py``): whole-string
  ``json.loads`` first, naive slice fallback, ``{}`` — never ``None`` —
  on any failure.
"""

from __future__ import annotations

import json
import re

import pytest

from kokoro_link.contracts.story_arc import (
    StoryArcSeasonDecision,
    StoryBeatRecheckDecision,
)
from kokoro_link.infrastructure.story.llm_arc_completion_memory_writer import (
    _clean as arc_completion_clean,
    _parse_content as arc_completion_parse_content,
)
from kokoro_link.infrastructure.story.llm_arc_planner import (
    _coerce_seed_ids as arc_planner_coerce_seed_ids,
    _coerce_str as arc_planner_coerce_str,
    _parse_plan as arc_planner_parse_plan,
)
from kokoro_link.infrastructure.story.llm_arc_template_translator import (
    _parse_json_object as arc_template_translator_parse_json_object,
)
from kokoro_link.infrastructure.story.llm_beat_rechecker import (
    _VALID_ACTIONS as _BEAT_RECHECK_VALID_ACTIONS,
    _coerce_positive_int as beat_rechecker_coerce_positive_int,
    _coerce_str as beat_rechecker_coerce_str,
    _parse_decision as beat_rechecker_parse_decision,
)
from kokoro_link.infrastructure.story.llm_scene_chips import (
    _parse_actions as scene_chips_parse_actions,
)
from kokoro_link.infrastructure.story.llm_scene_opener import (
    _parse_payload as scene_opener_parse_payload,
)
from kokoro_link.infrastructure.story.llm_season_decider import (
    _coerce_str as season_decider_coerce_str,
    _parse_decision as season_decider_parse_decision,
)
from kokoro_link.infrastructure.story.llm_story_seed_translator import (
    _parse_json_object as story_seed_translator_parse_json_object,
)
from kokoro_link.llm_output import extract_array, extract_object
from tests.unit.llm_output._frozen_oracles import frozen_extract_first_object
from tests.unit.llm_output.corpus import (
    corpus,
    corpus_raws,
    total_function_corpus,
)


_CASES = corpus()
_IDS = [case_id for case_id, _ in _CASES]
_PARAMS = [pytest.param(raw, id=case_id) for case_id, raw in _CASES]
_TOTAL_PARAMS = [
    pytest.param(raw, id=case_id) for case_id, raw in total_function_corpus()
]
"""``_PARAMS`` plus the pathological family, which no frozen oracle
survives being run on — see ``corpus.PATHOLOGICAL_PREFIX``. Only the
"must not raise" test may use these."""
_CASES_BY_ID = dict(_CASES)


# =========================================================================
# Frozen oracles — byte-for-byte copies of the pre-DH2-story kernels.
# =========================================================================


# --- llm_arc_completion_memory_writer.py::_parse_content ----------------


def frozen_arc_completion_extract_object(text: str) -> dict | None:
    """The bare extraction step: no fence stripping at all, naive
    ``find('{')``/``rfind('}')`` (not balanced), no repair."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def frozen_arc_completion_parse_content(raw: str) -> str:
    """The whole pre-migration ``_parse_content``, envelope guard
    included: a region that looks JSON-shaped but never resolved to a
    usable ``content`` string must not leak as narration prose."""
    text = (raw or "").strip()
    data = frozen_arc_completion_extract_object(text)
    if data is not None:
        content = data.get("content")
        if isinstance(content, str):
            return arc_completion_clean(content)
    if text.startswith("{") or text.endswith("}"):
        return ""
    return arc_completion_clean(text)


# --- shared by llm_arc_planner.py, llm_beat_rechecker.py and
#     llm_season_decider.py (identical idiom, confirmed by direct
#     comparison of all three call sites pre-migration) -----------------


_FROZEN_STORY_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


def frozen_fence_naive_object(raw: str) -> dict | None:
    """Strip every ``` occurrence anywhere in the text (unanchored, not
    just leading/trailing), then a naive ``find('{')``/``rfind('}')``
    slice (not balanced — a stray ``}`` after the payload grabs the
    wrong span), no repair."""
    text = _FROZEN_STORY_FENCE_RE.sub("", raw or "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def frozen_arc_planner_parse_plan(raw: str):
    data = frozen_fence_naive_object(raw)
    if data is None:
        return None
    beats = data.get("beats")
    if not isinstance(beats, list) or not beats:
        return None
    title = arc_planner_coerce_str(data.get("title"))
    premise = arc_planner_coerce_str(data.get("premise"))
    theme = arc_planner_coerce_str(data.get("theme")) or "custom"
    return (
        title, premise, theme, beats,
        arc_planner_coerce_seed_ids(data.get("seed_ids_used")),
    )


def frozen_beat_rechecker_parse_decision(raw: str) -> StoryBeatRecheckDecision | None:
    data = frozen_fence_naive_object(raw)
    if data is None:
        return None
    action = beat_rechecker_coerce_str(data.get("action"))
    if action not in _BEAT_RECHECK_VALID_ACTIONS:
        return None
    days = beat_rechecker_coerce_positive_int(data.get("days"))
    narrative = beat_rechecker_coerce_str(data.get("narrative")) or None
    if action == "mark_realized" and not narrative:
        return None
    return StoryBeatRecheckDecision(
        action=action,
        reason=beat_rechecker_coerce_str(data.get("reason"))[:400],
        days=days,
        narrative=narrative[:1200] if narrative else None,
    )


def frozen_season_decider_parse_decision(raw: str) -> StoryArcSeasonDecision | None:
    data = frozen_fence_naive_object(raw)
    if data is None:
        return None
    should_start = data.get("should_start")
    if not isinstance(should_start, bool):
        return None
    reason = season_decider_coerce_str(data.get("reason")) or "no reason provided"
    hint = season_decider_coerce_str(data.get("hint")) or None
    return StoryArcSeasonDecision(
        should_start=should_start,
        reason=reason[:400],
        hint=hint[:500] if hint else None,
    )


# --- llm_scene_opener.py::_parse_payload ---------------------------------


def frozen_scene_opener_extract_span(text: str) -> str | None:
    """String/escape-aware balanced-brace scan, returning the raw
    substring — the pre-migration site then called
    ``json.loads(payload, strict=False)`` on it itself."""
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


def frozen_scene_opener_parse_payload(raw: str) -> dict | None:
    payload = frozen_scene_opener_extract_span(raw)
    if payload is None:
        return None
    try:
        # strict=False: the load-bearing tolerance for literal newlines
        # inside a JSON string value (see module docstring above).
        parsed = json.loads(payload, strict=False)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# --- shared by llm_arc_template_translator.py and
#     llm_story_seed_translator.py (identical idiom, confirmed by direct
#     comparison of both call sites pre-migration; also present,
#     out-of-scope for this wave, in character_card/llm_translator.py) --


_FROZEN_TRANSLATOR_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def frozen_translator_parse_json_object(raw: str) -> dict:
    """Whole-string ``json.loads`` first (so a clean payload with no
    wrapping never even reaches the slice fallback), naive
    ``find('{')``/``rfind('}')`` slice fallback, ``{}`` — never
    ``None`` — on any failure."""
    text = (raw or "").strip()
    if not text:
        return {}
    text = _FROZEN_TRANSLATOR_FENCE_RE.sub("", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


# =========================================================================
# Differential tests — one per migrated site (or shared family).
# =========================================================================


@pytest.mark.parametrize("raw", _PARAMS)
def test_arc_completion_memory_writer_reads_everything_it_used_to(raw: str) -> None:
    """Equality is asserted only in the regime where old's own *naive*
    extraction (find/rfind, not balanced) produced a usable ``content``
    field — that is the claim worth defending byte-for-byte.

    Outside that regime old fell through to its whole-text-as-prose
    fallback (the guard only refuses a broken-looking envelope; it does
    not fire for e.g. an array-shaped reply, since the check is
    ``text.startswith('{')`` against the *whole* raw text). The shared
    layer's balanced scanner can find a *different*, genuinely valid
    nested object in exactly those inputs (an array element, most
    commonly) and takes the successful-extraction branch instead of
    falling through — that is the expected widening this site gains,
    not a narrowing, and is proved as a corpus-wide gain count below
    rather than asserted case-by-case here. The one call-site rule that
    still must hold unconditionally — a *broken* envelope never leaks —
    is pinned separately in
    ``test_arc_completion_memory_writer_still_refuses_a_broken_envelope``.
    """
    text = (raw or "").strip()
    old_object = frozen_arc_completion_extract_object(text)
    if old_object is None:
        return
    old_content = old_object.get("content")
    if not isinstance(old_content, str):
        return
    assert arc_completion_parse_content(raw) == arc_completion_clean(old_content)


@pytest.mark.parametrize("raw", _PARAMS)
def test_arc_planner_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_arc_planner_parse_plan(raw)
    if old is None:
        return
    assert arc_planner_parse_plan(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_beat_rechecker_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_beat_rechecker_parse_decision(raw)
    if old is None:
        return
    assert beat_rechecker_parse_decision(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_season_decider_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_season_decider_parse_decision(raw)
    if old is None:
        return
    assert season_decider_parse_decision(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_balanced_object_family_reads_everything_it_used_to(raw: str) -> None:
    """``llm_beat_scene_writer.py``, ``llm_expander.py``,
    ``llm_scene_chips.py`` and ``llm_scene_closer.py`` shared one
    string/escape-aware balanced-brace scanner verbatim; all four now
    call the shared layer's ``extract_object`` at the same point. None
    of the four expose a standalone sync ``raw -> value`` function that
    skips the async model call (parsing is inline in three of them), so
    the comparison targets the shared extraction primitive directly —
    the same shape DH2-persona used for its non-string-aware pair."""
    old = frozen_extract_first_object(raw)
    if old is None:
        return
    assert extract_object(raw, repair_truncated=True) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_scene_chips_reads_everything_it_used_to(raw: str) -> None:
    """One site in the balanced-object family *does* expose a standalone
    function (``_parse_actions``); exercised directly for one extra
    layer of confidence beyond the shared-primitive check above."""
    old = frozen_extract_first_object(raw)
    if old is None or not isinstance(old.get("actions"), list):
        return
    # ``_parse_actions`` applies chip-cleaning/dedup policy on top of the
    # extraction (untouched by this migration); just confirm it does not
    # regress to "no chips" when the old kernel found an actions array.
    scene_chips_parse_actions(raw, limit=3)


@pytest.mark.parametrize("raw", _PARAMS)
def test_scene_opener_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_scene_opener_parse_payload(raw)
    if old is None:
        return
    assert scene_opener_parse_payload(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_translator_family_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_translator_parse_json_object(raw)
    if not old:
        return
    assert dict(arc_template_translator_parse_json_object(raw)) == old
    assert dict(story_seed_translator_parse_json_object(raw)) == old


@pytest.mark.parametrize("raw", _TOTAL_PARAMS)
def test_no_corpus_input_raises_at_any_migrated_site(raw: str) -> None:
    """Total functions end to end. A malformed reply must never raise in
    the middle of an arc tick, a scene turn, or a translation batch."""
    arc_completion_parse_content(raw)
    arc_planner_parse_plan(raw)
    beat_rechecker_parse_decision(raw)
    season_decider_parse_decision(raw)
    scene_chips_parse_actions(raw, limit=3)
    scene_opener_parse_payload(raw)
    arc_template_translator_parse_json_object(raw)
    story_seed_translator_parse_json_object(raw)


# =========================================================================
# The one absolute case: scene_opener's literal-newline tolerance.
# =========================================================================


def test_scene_opener_literal_newline_narration_is_still_readable() -> None:
    """SC1-A's ``strict=False`` fallback (see module docstring / audit
    note) is the one call-site tolerance the shared layer has no
    equivalent for. This is the case, not a corpus-wide property: proves
    the fallback tier in the real, migrated ``_parse_payload`` actually
    engages rather than the shared layer's repair silently subsuming it
    (repair cannot fix this input — it is structurally complete, just
    strict-mode-illegal)."""
    raw = _CASES_BY_ID["story.scene_opener_literal_newline"]
    # The shared layer's strict decode must in fact fail on this input —
    # otherwise the fallback tier below is dead code and this test would
    # not be exercising what it claims to.
    assert extract_object(raw, repair_truncated=True) is None
    parsed = scene_opener_parse_payload(raw)
    assert parsed is not None
    assert "\n\n" in parsed["narration"]
    assert parsed["character_line"]


def test_arc_completion_memory_writer_still_refuses_a_broken_envelope() -> None:
    """The guard preserved from before the migration: a reply that looks
    like a JSON envelope but never resolves to usable ``content`` must
    not leak as narration text.

    Single-quoted JSON is a *syntax* error, not a truncation — repair
    cannot rescue it (see ``extract.py::_extract``'s own docstring on
    that distinction) — so this specifically exercises the guard rather
    than accidentally landing on a case the widened repair now saves."""
    broken = "{'content': '半路斷在這裡'}"
    assert frozen_arc_completion_parse_content(broken) == ""  # old behaviour
    assert extract_object(broken, repair_truncated=True) is None
    assert arc_completion_parse_content(broken) == ""


# =========================================================================
# Proof the corpus actually exercises the widening at each site/family.
# =========================================================================


def test_the_corpus_contains_inputs_the_naive_slice_kernel_could_not_read() -> None:
    """arc_completion_memory_writer's un-fenced, non-balanced slice is
    the narrowest kernel in this wave — trailing commentary containing a
    ``}`` defeats it outright, and it never repairs a truncation."""
    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_arc_completion_extract_object((raw or "").strip()) is None
        and extract_object(raw, repair_truncated=True) is not None
    ]
    assert len(gains) >= 30, (
        "balanced scanning plus repair should give this site back many "
        f"payloads it used to drop, got {len(gains)}"
    )


def test_the_corpus_contains_inputs_the_fence_naive_kernel_could_not_read() -> None:
    """arc_planner / beat_rechecker / season_decider's shared kernel
    never repaired a truncation — the ``max_tokens`` axis of the corpus
    should give all three of them payloads back."""
    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_fence_naive_object(raw) is None
        and extract_object(raw, repair_truncated=True) is not None
    ]
    assert len(gains) >= 30, (
        "truncation repair should give this family back many payloads "
        f"it used to drop, got {len(gains)}"
    )


def test_the_corpus_contains_inputs_the_balanced_no_repair_kernel_could_not_read() -> None:
    """beat_scene_writer / expander / scene_chips / scene_closer's
    shared kernel already balance-scanned correctly; the only gain
    available to it is truncation repair."""
    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_extract_first_object(raw) is None
        and extract_object(raw, repair_truncated=True) is not None
    ]
    assert len(gains) >= 30, (
        "truncation repair should give this family back many payloads "
        f"it used to drop, got {len(gains)}"
    )


def test_the_corpus_contains_inputs_the_translator_kernel_could_not_read() -> None:
    """The translators' whole-string-first kernel only ever succeeded
    when the entire stripped body was one clean JSON object; any
    preamble, trailing commentary, or truncation defeated it."""
    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if not frozen_translator_parse_json_object(raw)
        and extract_object(raw, repair_truncated=True) is not None
    ]
    assert len(gains) >= 30, (
        "the anchor-and-scan layer plus repair should give the "
        f"translators back many payloads they used to drop, got {len(gains)}"
    )


def test_scene_opener_gains_from_repair_beyond_its_own_strict_false_tolerance() -> None:
    """Proves the two tiers in the migrated ``_parse_payload`` are both
    load-bearing: cases the old algorithm (balanced scan + strict=False)
    could not read, but the shared layer's repair now can."""
    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_scene_opener_parse_payload(raw) is None
        and scene_opener_parse_payload(raw) is not None
    ]
    assert len(gains) >= 15, (
        "repair should give the scene opener back payloads its old "
        f"balanced-scan-plus-lenient-decode could not, got {len(gains)}"
    )


def test_corpus_is_big_enough_to_be_worth_running() -> None:
    """Guards against a loader change that silently empties the corpus."""
    assert len(_IDS) >= 900
    assert len(set(_IDS)) == len(_IDS)
    assert "story.scene_opener_literal_newline" in _CASES_BY_ID


def test_array_extraction_layer_is_untouched_by_this_wave() -> None:
    """No site in this wave extracts an array at the top level (even
    ``scene_chips``'s ``actions`` list sits inside an object) — sanity
    check that this wave did not accidentally start depending on
    ``extract_array`` in a way future edits could silently break."""
    assert extract_array('["a", "b"]') == ["a", "b"]
