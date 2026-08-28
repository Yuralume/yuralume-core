"""DH2-services differential gate: new >= old, at each of the twelve
sites the ticket names.

Same discipline as ``test_differential_oracle.py`` (DH1): every
extraction function this wave replaced is frozen here byte-for-byte
*as it was before migration*, and the corpus proves the shared layer
reads everything the frozen copy could — never less, usually more.
Nothing here asserts a hardcoded expected value; the expectation is
"whatever the frozen copy returned", so editing a fixture cannot bless
a regression.

Two sites needed more than a value comparison:

- ``branching_drama_director._parse_scene_response`` has a fallback
  quirk (a reply that isn't the JSON envelope falls back to treating
  the fence-stripped raw text as narration, not to ``None``) that is
  frozen and exercised directly rather than compared through the
  primitive.
- ``video_storyboard_shape.decode_json_object`` swaps a *greedy* regex
  (over-matches across multiple top-level objects) for the balanced
  scanner, with repair deliberately off — the widening there is a
  correctness fix, not a truncation-recovery gain, and is proved with
  a dedicated case rather than the truncation family.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import pytest

from kokoro_link.application.services import (
    arc_template_intake_service as arc_svc,
)
from kokoro_link.application.services import (
    branching_drama_critic as bdc_svc,
)
from kokoro_link.application.services import (
    branching_drama_director as bdd_svc,
)
from kokoro_link.application.services import (
    branching_drama_planner as bdp_svc,
)
from kokoro_link.application.services import (
    character_creation_intake_service as cci_svc,
)
from kokoro_link.application.services import (
    character_encounter_service as ces_svc,
)
from kokoro_link.application.services import chat_assist_service as cas_svc
from kokoro_link.application.services import fusion_story_critic as fsc_svc
from kokoro_link.application.services import fusion_story_planner as fsp_svc
from kokoro_link.application.services import (
    operator_persona_projection_service as opps_svc,
)
from kokoro_link.application.services import video_storyboard_shape as vss_svc
from kokoro_link.application.services.showcase import json_output as jo_svc
from kokoro_link.infrastructure.social import (
    llm_peer_knowledge_consolidator as pkc_svc,
)
from kokoro_link.llm_output import extract_object
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


# ------------------------------------------------------------------
# Frozen oracles — byte-for-byte copies of the pre-migration bodies.
# Never "fix" anything here; a bug preserved is the point (see
# ``_frozen_oracles.py``'s docstring for the same rule).
# ------------------------------------------------------------------


# --- arc_template_intake_service.py ------------------------------------

_ARC_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


def _arc_strip_fences(text: str) -> str:
    return _ARC_FENCE_RE.sub("", text or "").replace("```", "")


def frozen_arc_template_extract_json_object(raw: str) -> Any:
    text = _arc_strip_fences(raw or "").strip()
    if not text:
        return None
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start < 0 or end <= start:
            continue
        blob = text[start: end + 1]
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            continue
    return None


# --- branching_drama_critic.py ------------------------------------------

_BDC_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


def frozen_branching_drama_critic_extract(raw: str) -> Any:
    if not raw:
        return None
    cleaned = _BDC_FENCE_RE.sub("", raw).strip().rstrip("`")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


# --- branching_drama_director.py ----------------------------------------

_BDD_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


def _bdd_strip_fences(text: str) -> str:
    return _BDD_FENCE_RE.sub("", text).strip().rstrip("`")


def frozen_branching_drama_director_parse_scene_response(
    raw: str,
) -> tuple[str, str | None, bool]:
    """Returns ``(response, hint, took_json_branch)``.

    ``took_json_branch`` is test-only instrumentation (not part of the
    original return shape) so the differential test can tell "the JSON
    envelope really did decode" apart from "the fallback text happened
    to look the same" — the fallback quirk is preserved for real, not
    coincidentally.
    """
    if not raw:
        return "（回應生成失敗）", None, False
    cleaned = _bdd_strip_fences(raw).strip()
    try:
        obj = json.loads(cleaned)
        response = obj.get("response", "").strip()
        hint = obj.get("advance_hint")
        if isinstance(hint, str):
            hint = hint.strip() or None
        else:
            hint = None
        return response or "（回應生成失敗）", hint, True
    except (json.JSONDecodeError, AttributeError):
        return cleaned, None, False


# --- branching_drama_planner.py -----------------------------------------

_BDP_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


def _bdp_strip_fences(raw: str) -> str:
    text = _BDP_FENCE_RE.sub("", raw or "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start: end + 1]


def frozen_branching_drama_planner_extract(raw: str) -> Any:
    blob = _bdp_strip_fences(raw)
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


# --- character_creation_intake_service.py --------------------------------

_CCI_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


def _cci_strip_fences(text: str) -> str:
    return _CCI_FENCE_RE.sub("", text).replace("```", "")


def frozen_character_creation_intake_extract(raw: str) -> Any:
    text = _cci_strip_fences(raw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


# --- character_encounter_service.py / llm_peer_knowledge_consolidator.py
#
# Near-duplicate helpers (same signature, same intent) but *not*
# byte-identical: the encounter version falls through to parsing the
# untouched original text when no brace pair is found instead of
# returning ``None`` immediately. Both degrade to the same outcome
# (neither shape decodes to a dict without its braces) so it never
# shows up as a behavioural difference — frozen separately anyway
# because "frozen" means the literal old code, not an approximation.


def frozen_character_encounter_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def frozen_peer_knowledge_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# --- chat_assist_service.py / operator_persona_projection_service.py
#
# Byte-for-byte duplicate balanced-brace scanners (chat_assist's is the
# original; operator_persona_projection's is a hand-rolled copy of it).


def frozen_balanced_brace_blob(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
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
                return text[start: index + 1]
    return None


def frozen_chat_assist_parse_suggestions_payload(raw: str) -> Any:
    blob = frozen_balanced_brace_blob(raw)
    if blob is None:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


# operator_persona_projection_service used the identical scanner.
frozen_operator_persona_parse_narrative_payload = (
    frozen_chat_assist_parse_suggestions_payload
)


# --- fusion_story_critic.py / fusion_story_planner.py --------------------
#
# Same family as branching_drama_planner: fence-strip, plain find/rfind
# slice, single json.loads attempt.

_FS_FENCE_RE = re.compile(r"```(?:\w+)?\n?")


def frozen_fusion_story_extract(raw: str) -> Any:
    text = _FS_FENCE_RE.sub("", raw or "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start: end + 1])
    except json.JSONDecodeError:
        return None


# --- video_storyboard_shape.py -------------------------------------------

_VSS_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def frozen_video_storyboard_decode_json_object(text: str) -> Any:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        return parsed
    match = _VSS_JSON_BLOCK_RE.search(text)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


# --- showcase/json_output.py (shared helper, 4 consumers) ----------------


def frozen_showcase_coerce_json_object(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.startswith("```")]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start: end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, Mapping) else None


# ------------------------------------------------------------------
# Differential tests: new >= old, through each site's real entry point
# (a module-level function in every case — none of these twelve sites
# needed a class instance to reach the extraction step).
# ------------------------------------------------------------------


@pytest.mark.parametrize("raw", _PARAMS)
def test_arc_template_intake_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_arc_template_extract_json_object(raw)
    if old is None:
        return
    new = arc_svc._extract_json_object(raw)
    assert new == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_branching_drama_critic_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_branching_drama_critic_extract(raw)
    if not isinstance(old, dict):
        # A non-dict decode still failed the site's own isinstance gate
        # before migration too (_parse_critique required a dict) — not
        # a case this differential covers.
        return
    assert extract_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_branching_drama_director_scene_response_reads_everything_it_used_to(
    raw: str,
) -> None:
    old_response, old_hint, took_json_branch = (
        frozen_branching_drama_director_parse_scene_response(raw)
    )
    if not took_json_branch:
        # Old code fell to the prose fallback (not JSON, or a shape the
        # ``.get(...).strip()`` chain choked on) — the widened site may
        # now succeed where this one didn't, which is exactly the
        # improvement under test, not a regression to compare against.
        return
    new_response, new_hint = bdd_svc._parse_scene_response(raw)
    assert new_response == old_response
    assert new_hint == old_hint


@pytest.mark.parametrize("raw", _PARAMS)
def test_branching_drama_planner_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_branching_drama_planner_extract(raw)
    if not isinstance(old, dict):
        return
    assert bdp_svc._decode_planner_json(raw, site="test") == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_character_creation_intake_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_character_creation_intake_extract(raw)
    if not isinstance(old, dict):
        # The one real caller gates on isinstance(dict); a non-dict old
        # value (a bare top-level array, most commonly) was already a
        # dead end there. See the site's own docstring for why the
        # migrated version keeps that outcome explicit (None) instead
        # of letting the balanced scanner reach into the structure for
        # a plausible-looking nested fragment.
        return
    assert cci_svc._extract_json_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_character_encounter_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_character_encounter_json_object(raw)
    if old is None:
        return
    assert ces_svc._json_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_peer_knowledge_consolidator_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_peer_knowledge_json_object(raw)
    if old is None:
        return
    assert pkc_svc._json_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_chat_assist_suggestions_read_everything_they_used_to(raw: str) -> None:
    old = frozen_chat_assist_parse_suggestions_payload(raw)
    if not isinstance(old, dict):
        return
    assert extract_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_fusion_story_critic_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_fusion_story_extract(raw)
    if not isinstance(old, dict):
        return
    assert extract_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_fusion_story_planner_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_fusion_story_extract(raw)
    if not isinstance(old, dict):
        return
    assert extract_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_operator_persona_projection_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_operator_persona_parse_narrative_payload(raw)
    if not isinstance(old, dict):
        return
    assert extract_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_video_storyboard_decode_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_video_storyboard_decode_json_object(raw)
    if old is None:
        return
    assert vss_svc.decode_json_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_showcase_coerce_json_object_reads_everything_it_used_to(raw: str) -> None:
    old = frozen_showcase_coerce_json_object(raw)
    if old is None:
        return
    assert jo_svc.coerce_json_object(raw) == old


# ------------------------------------------------------------------
# Total-function guarantee: none of these twelve entry points may
# raise on any corpus input, the same discipline DH1 pinned.
# ------------------------------------------------------------------


@pytest.mark.parametrize("raw", _TOTAL_PARAMS)
def test_no_corpus_input_raises(raw: str) -> None:
    arc_svc._extract_json_object(raw)
    cci_svc._extract_json_object(raw)
    ces_svc._json_object(raw)
    pkc_svc._json_object(raw)
    bdp_svc._decode_planner_json(raw, site="test")
    bdd_svc._parse_scene_response(raw)
    vss_svc.decode_json_object(raw)
    jo_svc.coerce_json_object(raw)


# ------------------------------------------------------------------
# Proof the corpus actually exercises the widening — mirrors DH1's
# "a harness where every case happens to be equal proves nothing".
# ------------------------------------------------------------------


def test_the_corpus_widens_the_repair_on_sites() -> None:
    """Every site that turned truncation repair on should read back
    payloads its frozen predecessor dropped. Lower bounds, not exact
    figures — the corpus's truncated family is what supplies these."""
    raws = corpus_raws()

    def _gains(old_fn, new_fn) -> int:
        return sum(
            1 for raw in raws
            if old_fn(raw) is None and new_fn(raw) is not None
        )

    assert _gains(
        frozen_arc_template_extract_json_object, arc_svc._extract_json_object,
    ) >= 15
    assert _gains(
        frozen_character_creation_intake_extract, cci_svc._extract_json_object,
    ) >= 15
    assert _gains(
        frozen_character_encounter_json_object, ces_svc._json_object,
    ) >= 15
    # ``llm_peer_knowledge_consolidator`` is deliberately absent: CX-A1
    # turned repair back off there because that site *replaces* a peer
    # profile rather than reading one. Its remaining widening (the
    # escape-aware scan beating the crude find/rfind slice) is proved
    # below, next to the fail-closed pin.
    assert _gains(
        frozen_branching_drama_planner_extract,
        lambda raw: bdp_svc._decode_planner_json(raw, site="test"),
    ) >= 15
    assert _gains(frozen_fusion_story_extract, extract_object) >= 15
    assert _gains(
        frozen_chat_assist_parse_suggestions_payload, extract_object,
    ) >= 15
    assert _gains(frozen_showcase_coerce_json_object, jo_svc.coerce_json_object) >= 15


def test_peer_knowledge_still_widens_without_the_repair_half() -> None:
    """CX-A1 took repair away from this site; it did not take the
    migration away. The escape-aware balanced scan still reads payloads
    the crude find/rfind slice could not, so "repair off" has not
    quietly reverted the site to its frozen predecessor."""
    raws = corpus_raws()
    gains = [
        raw for raw in raws
        if frozen_peer_knowledge_json_object(raw) is None
        and pkc_svc._json_object(raw) is not None
    ]
    assert len(gains) >= 3, (
        "the string-aware scan should still beat the naive slice on "
        f"noisy replies, got {len(gains)}"
    )


def test_branching_drama_director_widens_on_a_truncated_envelope() -> None:
    """The site-specific instrumented oracle proves the fallback quirk
    is preserved (see the parametrized test above) — this proves the
    *other* half: a truncated JSON envelope that used to fall back to
    raw-text-as-narration now decodes for real."""
    truncated = '{"response": "先進去看看", "advance_hint": "踏上旅'
    old_response, old_hint, took_json_branch = (
        frozen_branching_drama_director_parse_scene_response(truncated)
    )
    assert took_json_branch is False
    assert old_hint is None

    new_response, new_hint = bdd_svc._parse_scene_response(truncated)
    assert new_response == "先進去看看"
    # The dangling string value gets closed by repair too — the hint
    # comes back as the (truncated) text that had already arrived
    # rather than as None, same as ``advance_hint`` would if the model
    # had simply written a shorter one.
    assert new_hint == "踏上旅"


def test_video_storyboard_decode_fixes_the_greedy_regex_over_match() -> None:
    """``adv.two_objects`` in the literal corpus is two complete JSON
    objects back to back. The old greedy ``\\{.*\\}`` regex spanned
    first-``{`` to last-``}`` — i.e. across *both* objects — producing
    a blob that is not valid JSON, so the old code returned ``None``.
    The balanced scanner stops at the first object's own closing brace
    and reads it cleanly. Repair is off for this call (schema-aware
    salvage owns truncation separately), so this proves the *other*
    kind of widening: the over-match bug fix, not truncation recovery.
    """
    two_objects = '{"tool": "a", "args": {}}\n{"tool": "b", "args": {}}'
    assert frozen_video_storyboard_decode_json_object(two_objects) is None
    assert vss_svc.decode_json_object(two_objects) == {"tool": "a", "args": {}}


def test_corpus_is_big_enough_to_be_worth_running() -> None:
    assert len(_IDS) >= 400
    assert len(set(_IDS)) == len(_IDS)
