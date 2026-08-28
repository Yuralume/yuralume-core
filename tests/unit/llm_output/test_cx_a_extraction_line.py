"""CX-A: the four defects a second review found on the migrated layer.

Same discipline as ``test_fx1_hardening``: each section pins one
finding with the exact input it was found on, and each pins the
*counterfactual* too — that the shared layer still reads the same input
when asked to, so a "fix" that quietly disabled the whole path fails
here instead of passing.

- **A1** — truncation repair turns half a sentence into a well-formed
  value at three sites whose job is to *replace* durable records.
- **A2** — the array guard read a truncated array as an object, which
  is the one shape the guard exists to refuse and the shape a
  runs-too-long array reply actually arrives in.
- **A3** — a composer reply that split the schema across two objects
  published the post and dropped the picture.
- **A5** — the wide scanners re-scanned the tail once per unclosed
  opener, so a long reply full of them cost quadratic time.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from kokoro_link.application.services.memory_consolidation_service import (
    MemoryConsolidationService,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
    ProfileField,
)
from kokoro_link.infrastructure.feed.llm_composer import _parse_output
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.memory.llm_consolidator import (
    LLMMemoryConsolidator,
)
from kokoro_link.infrastructure.persona.llm_consolidator import (
    _parse_response as persona_parse_response,
)
from kokoro_link.infrastructure.social import (
    llm_peer_knowledge_consolidator as pkc_svc,
)
from kokoro_link.llm_output import (
    extract_object,
    first_region_is_array,
)
from kokoro_link.llm_output import extract as extract_module


# ======================================================================
# A1 — repair writing half a sentence over the whole one
# ======================================================================

_TRUNCATED_MERGE = '{"content": "上週跟朋友約好，下班後要一起去藍調酒吧'
"""A consolidator reply chopped by ``max_tokens`` mid-``content``. The
shared repairer closes the dangling string, so what reaches the merge
is an ordinary ``str`` — and the service writes it as the replacement
memory and then deletes every original in the cluster."""


class _FixedReplyModel:
    """Minimal ``ChatModelPort``: one canned reply, no provider."""

    provider_id = "test"
    supports_vision = False
    prefers_public_image_urls = False

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls = 0

    async def generate(self, prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        self.calls += 1
        return self._reply

    async def generate_stream(self, prompt: str, **kwargs: object):
        del prompt, kwargs
        yield self._reply

    async def list_models(self) -> list[str]:
        return ["test"]


class _LengthEmbedder:
    @property
    def dimension(self) -> int:
        return 3

    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str):
        raise NotImplementedError

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        return [(float(len(t)), 0.0, 0.0) for t in texts]


def _memory(content: str, embedding: tuple[float, ...]) -> MemoryItem:
    return MemoryItem(
        id=str(uuid4()),
        character_id="c1",
        conversation_id=None,
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=0.7,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
        access_count=0,
        embedding=embedding,
    )


@pytest.mark.asyncio
async def test_a_truncated_merge_does_not_eat_the_cluster_it_merges() -> None:
    """The whole point of A1: this is not a degraded read, it is a
    destructive write. The originals are deleted by the same call that
    stores the replacement, so a repaired half-sentence is not something
    a later pass can notice and correct — there is nothing left to
    compare it against."""
    repo = InMemoryMemoryRepository()
    first = _memory("上週跟朋友約好要去藍調酒吧聽現場演出。", (1.0, 0.0, 0.0))
    second = _memory("上週和朋友約了下班後去藍調酒吧。", (0.99, 0.0, 0.0))
    await repo.add(first)
    await repo.add(second)

    model = _FixedReplyModel(_TRUNCATED_MERGE)
    service = MemoryConsolidationService(
        memory_repository=repo,
        consolidator=LLMMemoryConsolidator(model=model),
        embedder=_LengthEmbedder(),
    )

    report = await service.consolidate("c1")

    assert model.calls == 1, "the cluster must actually have reached the LLM"
    assert report.clusters_found >= 1
    assert report.clusters_merged == 0
    surviving = {m.content for m in await repo.list_all_for_character("c1")}
    assert surviving == {first.content, second.content}


def test_the_shared_layer_would_still_have_repaired_that_reply() -> None:
    """Guard against "fixed" by accident — the refusal above is this
    site declining a repair that is on offer, not the extractor having
    lost the ability."""
    assert extract_object(_TRUNCATED_MERGE, repair_truncated=True) == {
        "content": "上週跟朋友約好，下班後要一起去藍調酒吧",
    }
    assert extract_object(_TRUNCATED_MERGE, repair_truncated=False) is None


_TRUNCATED_PEER_PROFILE = '{"summary": "他最近常在河堤慢跑，聊到工作時會突然沉默'
"""The peer consolidator's prompt says its output 「整份取代」 the existing
profile, so a repaired half-summary is written over the complete one."""


def test_peer_knowledge_refuses_a_truncated_profile_rewrite() -> None:
    assert pkc_svc._json_object(_TRUNCATED_PEER_PROFILE) is None
    assert extract_object(_TRUNCATED_PEER_PROFILE, repair_truncated=True) is not None


def test_peer_knowledge_still_reads_a_complete_profile() -> None:
    """The site did not become fail-closed on everything: an intact
    reply, trailing sign-off and all, still rewrites the profile."""
    complete = '{"summary": "他最近常在河堤慢跑。", "confidence": 0.6}\n以上。'
    assert pkc_svc._json_object(complete) == {
        "summary": "他最近常在河堤慢跑。", "confidence": 0.6,
    }


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        turn_id="msg-1",
        conversation_id="conv-1",
        quote="我最近都七點起床",
        extracted_at=datetime.now(timezone.utc),
    )


def _candidate(candidate_id: str) -> CandidateField:
    return CandidateField(
        candidate_id=candidate_id,
        field_key="routine",
        layer=2,
        proposed_value="七點起床",
        evidence_ref=_evidence(),
        raw_extractor_confidence=0.8,
        content_mode=MessageContentMode.NORMAL,
        character_id="char-A",
    )


def _confirmed(field_id: str) -> ProfileField:
    return ProfileField(
        field_id=field_id,
        field_key="routine",
        layer=2,
        value="每天九點起床，晚上一點才睡",
        confidence=0.9,
        evidence_refs=(_evidence(),),
        last_updated=datetime.now(timezone.utc),
        update_count=1,
        source="extraction",
        content_mode=MessageContentMode.NORMAL,
        character_id="char-A",
    )


_TRUNCATED_SUPERSEDE = (
    '{"actions": [{"type": "supersede", "superseded_field_id": "fld-1", '
    '"candidate_ids": ["cand-1", "cand-2"], "field_key": "routine", '
    '"layer": 2, "new_confidence": 0.85, "reason": "兩則新證據一致", '
    '"new_value": "每天七點起床，晚上十一點'
)
"""A dream pass cut mid-``new_value``. Every field the validation checks
arrived before the cut, so repair produces an action that passes all of
them — and ``supersede`` overwrites a confirmed Layer 2 fact."""


def test_persona_dream_refuses_a_truncated_supersede() -> None:
    result = persona_parse_response(
        _TRUNCATED_SUPERSEDE,
        candidate_by_id={
            "cand-1": _candidate("cand-1"),
            "cand-2": _candidate("cand-2"),
        },
        valid_field_ids=set(),
        confirmed_by_id={"fld-1": _confirmed("fld-1")},
    )

    assert result.supersedes == []
    # ...and the counterfactual: with repair on, the same reply yields a
    # fully-validated action carrying half a sentence as the new value.
    repaired = extract_object(_TRUNCATED_SUPERSEDE, repair_truncated=True)
    assert repaired is not None
    assert repaired["actions"][0]["new_value"] == "每天七點起床，晚上十一點"


def test_persona_dream_still_applies_a_complete_supersede() -> None:
    complete = _TRUNCATED_SUPERSEDE + '每天七點起床，晚上十一點睡"}]}'
    result = persona_parse_response(
        complete,
        candidate_by_id={
            "cand-1": _candidate("cand-1"),
            "cand-2": _candidate("cand-2"),
        },
        valid_field_ids=set(),
        confirmed_by_id={"fld-1": _confirmed("fld-1")},
    )

    assert len(result.supersedes) == 1
    assert result.supersedes[0].new_value.endswith("睡")


# ======================================================================
# A2 — a truncated array read as an object
# ======================================================================

_TRUNCATED_ARRAY = (
    '[{"summary": "A的側寫", "severity": 1}, '
    '{"summary": "B的側寫", "severity": 2}'
)
"""The reported input. A model asked for one peer's profile answers with
a list of peers and runs out of tokens before the closing ``]`` — the
two failure modes arrive together because they have the same cause."""


def test_a_truncated_array_is_still_an_array() -> None:
    assert first_region_is_array(_TRUNCATED_ARRAY) is True


def test_the_object_extractor_still_reaches_into_it() -> None:
    """Which is why the guard has to answer, not the extractor: without
    it the call site is handed peer **A**'s profile for peer B."""
    assert extract_object(_TRUNCATED_ARRAY) == {
        "summary": "A的側寫", "severity": 1,
    }


def test_peer_knowledge_refuses_the_truncated_array() -> None:
    assert pkc_svc._json_object(_TRUNCATED_ARRAY) is None


def test_a_truncated_object_is_not_an_array() -> None:
    """The other side of the same branch: the guard reads the *first*
    opener, so an object reply that ran out of tokens inside a nested
    list is still an object reply."""
    truncated_object = '{"summary": "他最近很忙", "haunts": ["河堤", "咖啡店"'
    assert first_region_is_array(truncated_object) is False


def test_an_object_with_trailing_commentary_is_still_not_an_array() -> None:
    raw = '{"summary": "看起來沒問題", "severity": 0}\n以上，有需要再跟我說！'
    assert first_region_is_array(raw) is False


# ======================================================================
# A3 — the post published, the picture dropped
# ======================================================================

_COMPOSER_SPLIT_SCHEMA = (
    '{"content_text":"今晚的祭典好熱鬧！"}\n'
    '{"image_prompt":"1girl, yukata, festival, night"}'
)
"""The model answered with the schema spread over two objects. The first
region parses cleanly and carries a real post, so nothing fails — the
tags in the second region were simply never looked at."""


def test_feed_composer_recovers_an_image_prompt_from_the_second_object() -> None:
    out = _parse_output(_COMPOSER_SPLIT_SCHEMA, image_required=True)

    assert out.content_text == "今晚的祭典好熱鬧！"
    assert out.image_prompt == "1girl, yukata, festival, night"


def test_feed_composer_leaves_a_deliberate_empty_image_prompt_empty() -> None:
    """An empty ``image_prompt`` is the composer saying "no picture" —
    the recovery must not invent one out of the rest of the reply."""
    raw = '{"content_text": "只是想說一句話。", "image_prompt": ""}'
    out = _parse_output(raw, image_required=True)

    assert out.content_text == "只是想說一句話。"
    assert out.image_prompt == ""


def test_feed_composer_does_not_redirect_a_complete_envelope() -> None:
    """Additive only: when the anchored envelope already carries tags,
    a later object cannot overwrite them."""
    raw = (
        '{"content_text": "第一則", "image_prompt": "a"}\n'
        '{"content_text": "第二則", "image_prompt": "b"}'
    )
    assert _parse_output(raw, image_required=True).image_prompt == "a"


def test_feed_composer_recovery_is_off_when_no_image_is_wanted() -> None:
    out = _parse_output(_COMPOSER_SPLIT_SCHEMA, image_required=False)
    assert out.image_prompt == ""


# ======================================================================
# A5 — quadratic rescanning of unclosed openers
# ======================================================================

_SPARSE_OPENERS = ("[" + "x" * 65536) * 200
"""The reported input: ~13 MB in which every ``[`` opens a region that
never closes. Each one used to trigger its own scan to the end of the
string — ~1.3 billion character steps, measured at 81 s on the
reporter's machine."""


def _count_scans(text: str, call) -> int:
    """Run ``call`` with the region scanner instrumented."""
    original = extract_module._scan_region
    calls = 0

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    extract_module._scan_region = counting
    try:
        call(text)
    finally:
        extract_module._scan_region = original
    return calls


def test_sparse_unclosed_openers_cost_one_scan_not_two_hundred() -> None:
    """The structural assertion, independent of any machine's clock: one
    pass settles every opener it walks past, so 200 of them cost one
    scan rather than 200."""
    assert _count_scans(_SPARSE_OPENERS, extract_module.first_balanced_region) == 1
    assert _count_scans(_SPARSE_OPENERS, extract_module.first_region_is_array) == 1


def test_the_reported_payload_no_longer_pins_the_worker() -> None:
    """The wall-clock half, with a margin wide enough that only a
    reintroduction of the quadratic behaviour can trip it: the reported
    81 s is two orders of magnitude above this bound."""
    start = time.perf_counter()
    assert first_region_is_array(_SPARSE_OPENERS) is True
    assert extract_module.first_balanced_region(_SPARSE_OPENERS) is None
    assert time.perf_counter() - start < 10.0


def test_the_memo_agrees_with_a_per_opener_scan() -> None:
    """The memo is an optimisation, not a new answer. On a reply mixing
    closed, unclosed, mis-nested and inside-a-string brackets, every
    opener's memoised end must equal what ``balanced_end`` says on its
    own."""
    text = (
        '前言 [ 標記 {"a": [1, 2], "b": "]中括號在字串裡["} 尾巴 '
        '{"c": {"d": 3}} [ 沒關 {"e": 4} { "f": [ } ]'
    )
    index = extract_module._RegionIndex(text)
    for position, char in enumerate(text):
        if char not in ("{", "["):
            continue
        assert index.end_of(position) == extract_module.balanced_end(text, position), (
            f"memo disagreed with balanced_end at index {position}"
        )


def test_iter_embedded_json_still_finds_the_payload_behind_a_stray_opener() -> None:
    """The scan-sharing must not skip regions: a roleplay marker that
    opens and never closes still cannot hide the real payload."""
    raw = '{微笑 好的，我查一下：{"tool": "web_search", "args": {}}'
    assert list(extract_module.iter_embedded_json(raw)) == [
        {"tool": "web_search", "args": {}},
    ]
