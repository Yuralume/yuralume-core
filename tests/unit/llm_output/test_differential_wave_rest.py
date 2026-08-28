"""DH2-rest: the regression gate for the second extraction-migration wave.

Fifteen call sites across ``infrastructure/`` swapped their hand-rolled
"raw text -> JSON value" step for the shared
:mod:`kokoro_link.llm_output` layer. Every one of those hand-rolled
extractors fell into exactly one of four shapes; this file freezes one
oracle per shape (byte-for-byte copies of the pre-migration code, taken
from the site listed in each oracle's docstring) and proves — over the
shared corpus plus a handful of site-specific literal cases — that the
new layer reads everything the old shape read, with an identical value.

**Never "fix" anything in this file.** A bug preserved in an oracle is
the point: the assertion is one-directional (new superset-or-equal old),
and the record of what the old code actually did must stay honest.

Group -> sites (see ``LLM_RUNTIME_MODERNIZATION_PLAN.md`` ticket DH2-rest
for the full per-site audit):

- **Group A** (``frozen_group_a_parse_json_object``): whole-string
  ``json.loads`` first, naive ``find('{')``/``rfind('}')`` slice as
  fallback, ``{}`` (never ``None``) on any failure. Five byte-identical
  copies: ``character_card/llm_translator.py``,
  ``character_card/sillytavern_normalizer.py``,
  ``memoir/llm_localizer.py``, ``story/llm_arc_template_translator.py``,
  ``story/llm_story_seed_translator.py``.
- **Group B** (``frozen_group_b_extract_object``): the string/escape-aware
  balanced-brace scanner, object only, anchored on the first opener,
  commits to the first region (no retry past a decode failure). Nine
  sites, two of them (``disposition/llm_drift_judge.py``,
  ``reflection/llm_generator.py``) split the scan and the
  ``json.loads`` across two statements but are behaviourally identical
  to the composite: ``character_draft/llm_generator.py``,
  ``character_personality_type/llm_analyzer.py``,
  ``disposition/llm_drift_judge.py``, ``goal/llm_reviewer.py``,
  ``memory/llm_consolidator.py``, ``prompt/llm_material_digester.py``,
  ``prompt/llm_novelty_gate.py``, ``reflection/llm_generator.py``,
  ``register/llm_register_profiler.py``.
- **Group C** (``frozen_group_c_extract_array``): the same scanner
  family, array flavour. One site:
  ``character_draft/llm_companion_generator.py``.
- **Group E** (``frozen_group_e_extract_object``): the feed composer's
  own shape — a custom fence strip, then whole-string ``json.loads``,
  then a greedy ``re.compile(r"\\{.*\\}", re.DOTALL)`` block search as
  fallback. One site: ``feed/llm_composer.py``, migrated with
  ``repair_truncated=False`` (see that site's inline comment — the
  field-level salvage staying in production is this site's own,
  narrower truncation recovery, and turning on the shared repairer
  would auto-close a garbled mid-multibyte-character truncation into a
  postable string, which ``test_llm_feed_composer_parse_robustness.py``
  pins as fail-closed).

Two application/services sites the DH2-rest ticket also names —
``character_encounter_service.py`` and
``social/llm_peer_knowledge_consolidator.py`` — arrived at this branch
already migrated by a concurrent "DH2-services" wave (own
``_json_object`` docstring says as much); nothing to freeze here.
``feed/llm_composer.py``'s field-level salvage
(``_salvage_string_field`` and friends) is untouched production code,
not migrated, so it has no oracle here either.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import pytest

from kokoro_link.llm_output import extract_array, extract_object
from tests.unit.llm_output.corpus import corpus, corpus_raws


# --- Group A: whole-parse, naive slice fallback, {} sentinel -----------

_GROUP_A_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def frozen_group_a_parse_json_object(raw: str) -> Mapping[str, Any]:
    """Byte-for-byte pre-migration ``_parse_json_object`` (five sites)."""
    text = (raw or "").strip()
    if not text:
        return {}
    text = _GROUP_A_FENCE_RE.sub("", text).strip()
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
    return data if isinstance(data, Mapping) else {}


# --- Group B: balanced-brace scanner, object, first-opener-anchored ----


def frozen_group_b_extract_object(text: str) -> dict[str, Any] | None:
    """Byte-for-byte pre-migration ``_extract_object`` (nine sites)."""
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
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


# --- Group C: balanced-bracket scanner, array flavour -------------------


def frozen_group_c_extract_array(text: str) -> list[Any] | None:
    """Byte-for-byte pre-migration ``_extract_array``
    (``character_draft/llm_companion_generator.py``)."""
    start = text.find("[")
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
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, list) else None
    return None


# --- Group E: feed composer's fence-strip + whole-parse + greedy block -


_GROUP_E_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_GROUP_E_FENCE_LEAD_RE = re.compile(r"^```(?:json)?\s*")
_GROUP_E_FENCE_TAIL_RE = re.compile(r"```$")


def frozen_group_e_extract_object(raw: str) -> dict[str, Any] | None:
    """Byte-for-byte pre-migration object-extraction step of
    ``feed/llm_composer.py::_parse_output`` (the field-level salvage
    that runs when this returns ``None`` is unchanged production code,
    not reproduced here — it never was the "text -> JSON value" step)."""
    text = (raw or "").strip()
    if not text:
        return None
    candidate = text
    if candidate.startswith("```"):
        candidate = _GROUP_E_FENCE_LEAD_RE.sub("", candidate)
        candidate = _GROUP_E_FENCE_TAIL_RE.sub("", candidate)
        candidate = candidate.strip()
    parsed: Any = None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = _GROUP_E_JSON_BLOCK_RE.search(candidate)
        if match is not None:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = None
    return parsed if isinstance(parsed, dict) else None


# --- corpus-wide differential: new superset-or-equal old ----------------


_CASES = corpus()
_PARAMS = [pytest.param(raw, id=case_id) for case_id, raw in _CASES]


@pytest.mark.parametrize("raw", _PARAMS)
def test_group_a_sites_read_everything_they_used_to(raw: str) -> None:
    old = frozen_group_a_parse_json_object(raw)
    if not old:
        return
    new = extract_object(raw)
    assert new == dict(old), "a Group A site stopped reading a payload it used to read"


@pytest.mark.parametrize("raw", _PARAMS)
def test_group_b_sites_read_everything_they_used_to(raw: str) -> None:
    old = frozen_group_b_extract_object(raw)
    if old is None:
        return
    new = extract_object(raw, repair_truncated=True)
    assert new == old, "a Group B site stopped reading a payload it used to read"


@pytest.mark.parametrize("raw", _PARAMS)
def test_group_c_companion_generator_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_group_c_extract_array(raw)
    if old is None:
        return
    new = extract_array(raw, repair_truncated=True)
    assert new == old, "the companion generator stopped reading an array it used to read"


@pytest.mark.parametrize("raw", _PARAMS)
def test_group_e_feed_composer_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_group_e_extract_object(raw)
    if old is None:
        return
    # The site fence-strips *before* calling the shared layer (unchanged
    # production behaviour) and migrated with repair off — see the
    # module docstring for why. Reproduce exactly that call shape.
    text = (raw or "").strip()
    candidate = text
    if candidate.startswith("```"):
        candidate = _GROUP_E_FENCE_LEAD_RE.sub("", candidate)
        candidate = _GROUP_E_FENCE_TAIL_RE.sub("", candidate)
        candidate = candidate.strip()
    new = extract_object(candidate, repair_truncated=False)
    assert new == old, "feed composer stopped reading a payload it used to read"


# --- site-specific literal cases: shapes the shared matrix doesn't hit --


_GROUP_B_LABELLED_CASES = (
    # goal/llm_reviewer.py's real shape: verdicts + new_goals lists.
    '{"verdicts": [{"id": "g1", "status": "done", "notes": "ok"}], '
    '"new_goals": [{"content": "learn guitar", "priority": 2, "tags": []}]}',
    # character_personality_type/llm_analyzer.py's real shape.
    '{"suggested_code": "ISTJ", "confidence": 0.8, "source": "llm_inferred", '
    '"is_consistent": true, "conflict_level": "none", "rationale": "fits", '
    '"conflict_notes": [], "user_questions": []}',
    # disposition/llm_drift_judge.py's real shape (split scan+loads site).
    '{"dimension": "candor", "direction": "up", "reason": "opened up more", '
    '"evidence_quote": "我最近壓力好大"}',
    # register/llm_register_profiler.py's real shape (nested axes dict).
    '{"axes": {"emotional_intensity": 0.6, "seriousness": 0.4}, '
    '"confidence": 0.7, "vulnerable_disclosure": false, "note": "casual"}',
    # A fenced reply with an uppercase language tag — Group A's IGNORECASE
    # fence regex accepts it; the shared scanner is fence-agnostic either
    # way, so this is really a Group B/general regression check.
    '```JSON\n{"bullets": ["先做甲", "再做乙"]}\n```',
)


@pytest.mark.parametrize("raw", _GROUP_B_LABELLED_CASES)
def test_group_b_site_shaped_labelled_objects_round_trip(raw: str) -> None:
    old = frozen_group_b_extract_object(raw)
    assert old is not None, "test fixture itself should be well-formed JSON"
    assert extract_object(raw, repair_truncated=True) == old


_GROUP_A_LABELLED_CASES = (
    # character_card/llm_translator.py's real shape.
    '{"name": "translated name", "summary": "a short bio", '
    '"personality": ["kind", "curious"], "companions": []}',
    # story/llm_story_seed_translator.py's real shape (labelled list).
    '{"translated": ["今天天氣真好", "我們去公園走走吧"]}',
    # Whole-string parse succeeds only after stripping a lowercase fence.
    '```json\n{"name": "同上"}\n```',
)


@pytest.mark.parametrize("raw", _GROUP_A_LABELLED_CASES)
def test_group_a_site_shaped_labelled_objects_round_trip(raw: str) -> None:
    old = frozen_group_a_parse_json_object(raw)
    assert old, "test fixture itself should be well-formed JSON"
    assert extract_object(raw) == dict(old)


_GROUP_C_LABELLED_CASES = (
    # character_draft/llm_companion_generator.py's real shape.
    '[{"name": "葉澄", "role": "室友", "brief_profile": "都市設計系學生", '
    '"personality_sketch": ["細心", "毒舌"], "relationship_snippet": "兩年室友"}]',
    # Truncated mid-second-element — the widening this migration buys back.
    '[{"name": "葉澄", "role": "室友"}, {"name": "阿哲", "role": "同事',
)


@pytest.mark.parametrize("raw", _GROUP_C_LABELLED_CASES)
def test_group_c_site_shaped_arrays(raw: str) -> None:
    old = frozen_group_c_extract_array(raw)
    if old is None:
        return
    assert extract_array(raw, repair_truncated=True) == old


_GROUP_E_LABELLED_CASES = (
    # feed/llm_composer.py's real two-field and video-schema shapes.
    '{"content_text": "今天天氣真好", "image_prompt": "1girl, outdoors, sunny"}',
    '{"content_text": "在家耍廢", "media_kind": "video", "image_prompt": "1girl, sofa", '
    '"video_prompt": "A girl stretches, then reaches for a remote, finally presses play."}',
    # Two objects back to back in noisy text — old's whole-parse fails,
    # old's greedy {.*} block also fails (spans across both), so old
    # returns None here and the site falls through to field salvage.
    # The shared scanner's escape-aware first-region anchor succeeds —
    # proved separately below as a widening, not asserted equal here.
)


@pytest.mark.parametrize("raw", _GROUP_E_LABELLED_CASES)
def test_group_e_site_shaped_objects_round_trip(raw: str) -> None:
    old = frozen_group_e_extract_object(raw)
    assert old is not None, "test fixture itself should be well-formed JSON"
    assert extract_object(raw, repair_truncated=False) == old


# --- proof the corpus actually exercises each group's widening ---------


def test_group_a_corpus_contains_inputs_the_old_code_could_not_read() -> None:
    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if not frozen_group_a_parse_json_object(raw) and extract_object(raw) is not None
    ]
    assert len(gains) >= 20, (
        "Group A's escape-aware scan + repair should read back many "
        f"payloads the naive slice fallback dropped, got {len(gains)}"
    )


def test_group_b_corpus_contains_inputs_the_old_code_could_not_read() -> None:
    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_group_b_extract_object(raw) is None
        and extract_object(raw, repair_truncated=True) is not None
    ]
    assert len(gains) >= 20, (
        "Group B's truncation repair should read back many payloads the "
        f"old first-opener-commits scan dropped, got {len(gains)}"
    )


def test_group_c_corpus_contains_inputs_the_old_code_could_not_read() -> None:
    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_group_c_extract_array(raw) is None
        and extract_array(raw, repair_truncated=True) is not None
    ]
    assert len(gains) >= 10, (
        "the companion generator's truncation repair should read back "
        f"arrays the old scan dropped, got {len(gains)}"
    )


def test_group_e_corpus_contains_inputs_the_old_code_could_not_read() -> None:
    """Group E kept repair off, so its widening comes only from the
    escape-aware balanced scan beating the naive greedy block regex on
    noisy multi-object text — proved directly rather than via the shared
    corpus's truncation axis (which this site deliberately does not
    benefit from)."""
    noisy_multi_object = 'garbled prefix {"content_text": "ok"} trailing {"x": 1} more junk'
    assert frozen_group_e_extract_object(noisy_multi_object) is None
    assert extract_object(noisy_multi_object, repair_truncated=False) == {
        "content_text": "ok",
    }


def test_group_e_never_widens_via_truncation_repair() -> None:
    """Pin the one deliberate non-widening in this wave: a
    mid-``content_text``-string truncation must stay unrecoverable by the
    shared layer's object extraction at this site (repair is off), so
    the only path back to a post is the field-level salvage already in
    production — unchanged by this migration."""
    truncated = '{"content_text": "hello there, this got cut of'
    assert frozen_group_e_extract_object(truncated) is None
    assert extract_object(truncated, repair_truncated=False) is None
    # With repair on (i.e. what this site does *not* do) the same input
    # would be recoverable — confirming the guard is the flag, not a
    # coincidence of this particular input.
    assert extract_object(truncated, repair_truncated=True) is not None


def test_corpus_is_big_enough_to_be_worth_running() -> None:
    assert len(_CASES) >= 400
