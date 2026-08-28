"""Behaviour spec for `kokoro_link.llm_output.extract`.

The differential harness proves the layer did not *lose* anything. This
file pins what it deliberately does, including the two refusals that
are easy to mistake for bugs and "fix" into real ones:

* it takes the first region only — it does not go hunting for the
  region that happens to parse;
* it refuses to repair a region that is already structurally complete,
  so a syntax error stays an error instead of becoming a dict.
"""

from __future__ import annotations

import pytest

from kokoro_link.llm_output import (
    ARRAY_REGION,
    OBJECT_REGION,
    BalancedRegion,
    ParseReason,
    balanced_end,
    extract_array,
    extract_array_outcome,
    extract_object,
    extract_object_outcome,
    first_balanced_region,
    first_region_is_array,
    iter_embedded_json,
    strip_fences,
)
from tests.unit.llm_output.corpus import total_function_corpus


# --- strip_fences ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('```JSON5\n{"a": 1}\n```', '{"a": 1}'),
        ('  \n{"a": 1}\n  ', '{"a": 1}'),
        ("", ""),
        ("   ", ""),
        ("沒有 fence 的一般回覆", "沒有 fence 的一般回覆"),
    ],
)
def test_strip_fences_removes_only_the_wrapping_fence(raw: str, expected: str) -> None:
    assert strip_fences(raw) == expected


def test_strip_fences_leaves_a_mid_reply_code_block_alone() -> None:
    """A fence in the middle belongs to a code block the character wrote
    on purpose; eating it would corrupt the text around it."""
    raw = "你要的片段：\n```python\nprint(1)\n```\n跑跑看"
    assert strip_fences(raw) == raw


# --- balanced_end ------------------------------------------------------


def test_balanced_end_ignores_brackets_inside_strings() -> None:
    text = '{"content": "他說 {不要} 就是 [不要]"}'
    assert balanced_end(text, 0) == len(text) - 1


def test_balanced_end_ignores_escaped_quotes() -> None:
    text = '{"content": "她說「\\"好\\"」"}'
    assert balanced_end(text, 0) == len(text) - 1


def test_balanced_end_reports_an_unclosed_region() -> None:
    assert balanced_end('{"a": {"b": 1}', 0) is None


def test_balanced_end_reports_a_misnested_region() -> None:
    """Wrong closing order is unreadable, not merely unclosed — and
    collapsing the two is safe because neither could ever decode."""
    assert balanced_end('{"a": [ } ]', 0) is None


# --- extract_object / extract_array ------------------------------------


def test_extracts_an_object_from_a_noisy_reply() -> None:
    raw = '好的，我整理好了：\n```json\n{"tool": "echo", "args": {}}\n```\n有需要再說！'
    assert extract_object(raw) == {"tool": "echo", "args": {}}


def test_extracts_an_array_from_a_noisy_reply() -> None:
    raw = 'Here are the memories:\n[{"kind": "semantic", "content": "loves cats"}] cheers!'
    assert extract_array(raw) == [{"kind": "semantic", "content": "loves cats"}]


def test_takes_the_first_region_not_the_first_parseable_one() -> None:
    """Load-bearing refusal. Scanning for "the region that parses" would
    let a payload hidden behind a roleplay marker become a contract the
    prompt never asked for. Callers that want the wide scan reach for
    ``iter_embedded_json`` explicitly."""
    raw = '{微笑}好的：{"tool": "echo", "args": {}}'
    assert extract_object(raw) is None
    assert any(
        value == {"tool": "echo", "args": {}} for value in iter_embedded_json(raw)
    )


def test_takes_the_first_of_two_complete_objects() -> None:
    raw = '{"a": 1}\n{"b": 2}'
    assert extract_object(raw) == {"a": 1}


def test_each_extractor_anchors_on_its_own_opener_wherever_it_sits() -> None:
    """Both extractors look for their *own* delimiter and ignore the
    other, so an object nested in a list is still the first object.

    This is inherited behaviour, not a new choice, and it is load
    bearing: upstreams that think in batches emit ``[{"name": …}]`` for
    a single call, and the tool-call site has always read it.
    """
    assert extract_object('[{"a": 1}]') == {"a": 1}
    assert extract_array('{"a": [1, 2]}') == [1, 2]
    assert extract_array('{"a": 1}') is None


# --- truncation repair -------------------------------------------------


def test_repairs_a_cut_after_a_nested_object_closed() -> None:
    raw = '{"tool": "generate_image", "args": {"positive": "1girl", "aspect": "portrait"}'
    assert extract_object(raw) == {
        "tool": "generate_image",
        "args": {"positive": "1girl", "aspect": "portrait"},
    }


def test_repairs_a_cut_inside_a_string() -> None:
    raw = '{"tool": "echo", "args": {"caption": "走到窗邊，月光灑在臉'
    parsed = extract_object(raw)
    assert parsed is not None
    assert parsed["args"]["caption"].startswith("走到窗邊")


def test_repairs_a_cut_after_a_dangling_comma() -> None:
    raw = '{"tool": "echo", "args": {"a": 1},'
    assert extract_object(raw) == {"tool": "echo", "args": {"a": 1}}


def test_repairs_a_cut_inside_a_nested_array() -> None:
    """The old object repair counted braces only, so it could never
    close an array — a truncated list argument took the whole call down
    with it."""
    raw = '{"tool": "web_search", "args": {"queries": ["夏祭", "花火'
    parsed = extract_object(raw)
    assert parsed is not None
    assert parsed["args"]["queries"][0] == "夏祭"


def test_repairs_a_truncated_array() -> None:
    raw = '[{"kind": "semantic", "content": "likes jazz"}, {"kind": "episodic",'
    parsed = extract_array(raw)
    assert parsed is not None
    assert parsed[0]["content"] == "likes jazz"


def test_repair_does_not_rescue_a_structurally_complete_region() -> None:
    """A balanced region that will not decode has a syntax problem, and
    appending closers to it would only manufacture a value nobody
    wrote."""
    assert extract_object("{'tool': 'echo'}") is None
    assert extract_object('{"tool": "echo",}') is None


def test_repair_can_be_refused() -> None:
    raw = '{"tool": "echo", "args": {"a": 1}'
    assert extract_object(raw, repair_truncated=True) is not None
    assert extract_object(raw, repair_truncated=False) is None


# --- reason codes ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"a": 1}', ParseReason.OK),
        ("", ParseReason.NO_JSON),
        ("完全沒有 JSON 的回覆", ParseReason.NO_JSON),
        ('{"a": ', ParseReason.UNBALANCED),
        ("{'a': 1}", ParseReason.DECODE_ERROR),
        ('{"a": 1', ParseReason.REPAIRED),
    ],
)
def test_object_outcome_reasons(raw: str, reason: ParseReason) -> None:
    assert extract_object_outcome(raw).reason is reason


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("[1, 2]", ParseReason.OK),
        ("沒有陣列", ParseReason.NO_JSON),
        ("[not valid json", ParseReason.UNBALANCED),
        ("['a']", ParseReason.DECODE_ERROR),
        ('[{"a": 1}', ParseReason.REPAIRED),
    ],
)
def test_array_outcome_reasons(raw: str, reason: ParseReason) -> None:
    assert extract_array_outcome(raw).reason is reason


def test_outcome_carries_length_but_never_the_text() -> None:
    """The reason travels to a log line; the payload must not. Model
    output is player content."""
    raw = '{"secret": "森森的地址"'
    outcome = extract_object_outcome(raw)
    assert outcome.raw_length == len(raw)
    assert raw not in repr(outcome.reason)


def test_a_failed_outcome_is_falsy_and_a_repaired_one_is_not() -> None:
    assert extract_object_outcome("prose").failed is True
    assert extract_object_outcome("prose").ok is False
    assert extract_object_outcome('{"a": 1').ok is True


# --- contamination is not supposed to matter ---------------------------


_PREFIXES = ["", "```json\n", "好的：\n", "## 輸出\n\n", "\n\n  ", "Here you go:\n"]
_SUFFIXES = ["", "\n```", "\n以上！", " // done", "  \n"]


@pytest.mark.parametrize("prefix", _PREFIXES)
@pytest.mark.parametrize("suffix", _SUFFIXES)
def test_wrapping_a_payload_never_changes_what_comes_out(
    prefix: str, suffix: str,
) -> None:
    """The property the whole layer exists for: whatever the model wraps
    around a well-formed payload, the value read out is the same one."""
    obj = '{"tool": "web_search", "args": {"query": "夏祭", "limit": 3}}'
    arr = '[{"kind": "semantic", "content": "森森住在東京"}]'

    assert extract_object(prefix + obj + suffix) == {
        "tool": "web_search", "args": {"query": "夏祭", "limit": 3},
    }
    assert extract_array(prefix + arr + suffix) == [
        {"kind": "semantic", "content": "森森住在東京"},
    ]


def test_every_corpus_case_returns_a_value_or_none_and_nothing_else() -> None:
    """Total-function property, so it runs the *whole* corpus including
    the pathological family the differential comparison has to leave out
    (see ``corpus.PATHOLOGICAL_PREFIX``) — that family exists precisely
    to hold this property down."""
    for _, raw in total_function_corpus():
        obj = extract_object(raw)
        arr = extract_array(raw)
        assert obj is None or isinstance(obj, dict)
        assert arr is None or isinstance(arr, list)


# --- which family is this reply? --------------------------------------


def test_first_balanced_region_reports_kind_and_span() -> None:
    assert first_balanced_region('好的：{"a": 1} 就這樣') == BalancedRegion(
        OBJECT_REGION, 3, 10,
    )
    assert first_balanced_region("[1, 2]") == BalancedRegion(ARRAY_REGION, 0, 5)
    assert first_balanced_region("沒有任何結構") is None


def test_first_balanced_region_does_not_need_the_region_to_decode() -> None:
    """The whole reason this exists next to ``extract_object``: a shape
    question must still have an answer when the payload is malformed."""
    region = first_balanced_region("[{'kind': 'semantic'}]")

    assert region is not None
    assert region.kind == ARRAY_REGION
    assert extract_array("[{'kind': 'semantic'}]") is None


def test_first_balanced_region_skips_an_opener_that_never_closes() -> None:
    assert first_balanced_region('抽選日期 [7/1 {"a": 1}') == BalancedRegion(
        OBJECT_REGION, 10, 17,
    )


def test_first_region_is_array_survives_a_reply_that_does_not_parse() -> None:
    assert first_region_is_array('[{"a": 1}, {"b": 2}]\n以上，有需要再跟我說！')
    assert not first_region_is_array('{"a": 1}\n以上，有需要再跟我說！')
    assert not first_region_is_array("完全沒有 JSON")
    assert not first_region_is_array("")
