"""The regression gate for the shared extraction layer: new ⊇ old.

DH1 deleted three hand-rolled extractors and pointed their sites at one
shared implementation. The failure mode that would not show up in any
existing test is *narrowing* — some contaminated or truncated reply the
old code could read stops parsing, and the site silently extracts
nothing from then on.

So the assertion here is mechanical, not enumerated: for every string in
the corpus, whatever a frozen copy of the old implementation returned,
the new layer must return the same value. Widening is allowed and
expected (truncated payloads now survive at sites that used to drop
them) and is proved separately at the bottom of this file, so that a
harness which accidentally became all-equal cannot pass unnoticed.

Nothing here asserts a *hardcoded* expected value. The expectations come
from `_frozen_oracles`, which is why editing a fixture cannot bless a
regression.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.tool_call_parser import (
    looks_like_object_literal,
    looks_like_tool_call_attempt,
    looks_like_tool_call_shape,
    parse_tool_call,
)
from kokoro_link.domain.value_objects.tool_call import ToolCall
from kokoro_link.infrastructure.memory.json_parser import parse_memory_payload
from kokoro_link.llm_output import (
    extract_array,
    extract_object,
    iter_embedded_json,
    strip_fences,
)
from tests.unit.llm_output._frozen_oracles import (
    FROZEN_FENCE_RE,
    FROZEN_MAX_SHAPE_SCAN_CHARS,
    frozen_extract_array,
    frozen_iter_embedded_json,
    frozen_looks_like_object_literal,
    frozen_looks_like_tool_call_attempt,
    frozen_looks_like_tool_call_shape,
    frozen_parse_memory_payload,
    frozen_post_turn_extract_object,
    frozen_tool_call_object,
)
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


def _frozen_parse_tool_call(raw: str) -> ToolCall | None:
    """The whole pre-DH1 ``parse_tool_call``, extraction plus contract.

    The contract tail was not touched by the migration; reproducing it
    here anyway means the comparison is against the function players
    actually hit, not against an intermediate value.
    """
    if not raw or not raw.strip():
        return None
    obj = frozen_tool_call_object(raw)
    if obj is None:
        return None
    name = obj.get("tool")
    if not isinstance(name, str) or not name.strip():
        return None
    args_raw = obj.get("args", {})
    if not isinstance(args_raw, dict):
        return None
    try:
        return ToolCall(name=name.strip(), arguments=args_raw)
    except ValueError:
        return None


# --- the three migrated sites ------------------------------------------


@pytest.mark.parametrize("raw", _PARAMS)
def test_tool_call_site_reads_everything_it_used_to(raw: str) -> None:
    """Site 1: chat tool calls.

    The gate on truncation repair (only rescue text that announced
    itself as our contract) is policy and stayed at the site, so the
    comparison covers it: a reply the old code refused to rescue must
    still be refused.
    """
    old = _frozen_parse_tool_call(raw)
    if old is None:
        return
    new = parse_tool_call(raw)
    assert new is not None, "the shared layer stopped reading a call it used to read"
    assert new.name == old.name
    assert new.arguments == old.arguments


@pytest.mark.parametrize("raw", _PARAMS)
def test_memory_site_reads_everything_it_used_to(raw: str) -> None:
    """Site 2: memory / schedule / weather-drift array payloads.

    When the old scanner found *and decoded* an array, the new layer
    must agree item for item — including the cases where the decoded
    array legitimately contains no objects and both return ``[]``.
    """
    if frozen_extract_array(raw) is None:
        return
    assert extract_array(raw) == frozen_extract_array(raw)
    assert parse_memory_payload(raw) == frozen_parse_memory_payload(raw)


@pytest.mark.parametrize("raw", _PARAMS)
def test_post_turn_site_reads_everything_it_used_to(raw: str) -> None:
    """Site 3: the post-turn five-in-one object."""
    old = frozen_post_turn_extract_object(raw)
    if old is None:
        return
    assert extract_object(raw) == old


@pytest.mark.parametrize("raw", _PARAMS)
def test_shared_object_extraction_covers_the_old_tool_call_kernel(raw: str) -> None:
    """The layer itself, independent of any site's gating."""
    old = frozen_tool_call_object(raw)
    if old is None:
        return
    assert extract_object(raw, repair_truncated=True) == old


# --- the policy functions that must not have moved at all --------------


@pytest.mark.parametrize("raw", _PARAMS)
def test_shape_policy_is_unchanged_in_both_directions(raw: str) -> None:
    """The three width judgements are policy: equal, not merely ⊇.

    They decide whether a reply is shown to a player. Widening them
    would start eating messages; narrowing them would leak machine
    output into a chat bubble. Either direction is a regression, so
    this is the one place the assertion is two-way.
    """
    assert looks_like_tool_call_attempt(raw) == frozen_looks_like_tool_call_attempt(raw)
    assert looks_like_object_literal(raw) == frozen_looks_like_object_literal(raw)
    assert looks_like_tool_call_shape(raw) == frozen_looks_like_tool_call_shape(raw)


@pytest.mark.parametrize("raw", _PARAMS)
def test_text_helpers_are_unchanged(raw: str) -> None:
    assert strip_fences(raw) == FROZEN_FENCE_RE.sub("", raw.strip())
    scanned = strip_fences(raw)[:FROZEN_MAX_SHAPE_SCAN_CHARS]
    assert list(iter_embedded_json(scanned)) == list(
        frozen_iter_embedded_json(scanned),
    )


@pytest.mark.parametrize("raw", _TOTAL_PARAMS)
def test_no_corpus_input_raises(raw: str) -> None:
    """Total functions. A malformed reply is a ``None``, never an
    exception in the middle of someone's turn."""
    strip_fences(raw)
    list(iter_embedded_json(raw))
    for repair in (True, False):
        extract_object(raw, repair_truncated=repair)
        extract_array(raw, repair_truncated=repair)
    parse_tool_call(raw)
    parse_memory_payload(raw)
    looks_like_object_literal(raw)
    looks_like_tool_call_shape(raw)


# --- proof the corpus actually exercises the widening ------------------


def test_the_corpus_contains_inputs_the_old_code_could_not_read() -> None:
    """A harness where every case happens to be equal proves nothing.

    Each count below is a class of reply that used to be dropped
    whole. They are lower bounds, not exact figures: adding corpus
    cases should only push them up.
    """
    raws = corpus_raws()

    object_gains = [
        raw for raw in raws
        if frozen_post_turn_extract_object(raw) is None
        and extract_object(raw) is not None
    ]
    array_gains = [
        raw for raw in raws
        if frozen_extract_array(raw) is None and extract_array(raw) is not None
    ]

    assert len(object_gains) >= 20, (
        "the truncation family should give the post-turn site back "
        f"many payloads it used to drop, got {len(object_gains)}"
    )
    assert len(array_gains) >= 10, (
        "the truncation family should give the memory site back many "
        f"payloads it used to drop, got {len(array_gains)}"
    )


def test_truncation_repair_is_off_when_the_caller_says_so() -> None:
    """The widening is opt-out, not unavoidable — a site that must not
    guess (a strict contract, a security decision) can still refuse."""
    truncated = [
        raw for raw in corpus_raws()
        if extract_object(raw, repair_truncated=True) is not None
        and extract_object(raw, repair_truncated=False) is None
    ]
    assert len(truncated) >= 20


def test_repair_never_invents_a_value_out_of_prose() -> None:
    """Widening has a floor: text with no JSON in it stays unreadable,
    however hard we try to repair it. Otherwise brace soup in a
    roleplay reply would become a phantom tool call."""
    for raw in (
        "今天天氣很好{但我有點累",
        "{微笑}我查到了，抽選七月一號開始。",
        "抽選在 {7/1} 開始，我幫你標起來了。",
        "[微笑]我查到了",
        "just a normal chat reply",
    ):
        assert extract_object(raw, repair_truncated=True) is None
        assert extract_array(raw, repair_truncated=True) is None
        assert parse_tool_call(raw) is None


def test_corpus_is_big_enough_to_be_worth_running() -> None:
    """Guards against a loader change that silently empties the corpus
    and turns every differential test into a no-op."""
    assert len(_IDS) >= 400
    assert len(set(_IDS)) == len(_IDS)
