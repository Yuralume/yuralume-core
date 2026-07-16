"""Unit tests for BackgroundTurnRecorder + InMemoryTurnRecordRepository.

The recorder is auxiliary — its failures must never bubble. These tests
hit three guarantees:

1. Successful path persists a row reachable via ``get(id)``.
2. Recorder swallows repository errors and never raises to the caller.
3. ``flush`` waits for in-flight fire-and-forget writes (deterministic
   test ordering).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.contracts.observability import (
    LatencyBucket,
    TurnRecordRepositoryPort,
    TurnRecordingDraft,
)
from kokoro_link.domain.entities.turn_record import TurnRecord
from kokoro_link.infrastructure.observability.turn_recorder import (
    BackgroundTurnRecorder,
    NullTurnRecorder,
    turn_recording_enabled,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_records import (
    InMemoryTurnRecordRepository,
)


pytestmark = pytest.mark.asyncio


class _ExplodingRepo(TurnRecordRepositoryPort):
    async def add(self, record: TurnRecord) -> None:
        raise RuntimeError("boom")

    async def get(self, record_id: str) -> TurnRecord | None:
        return None

    async def list_recent(self, **_kw) -> list[TurnRecord]:
        return []

    async def update_operator_feedback(
        self,
        record_id: str,
        feedback: dict[str, object],
    ) -> TurnRecord | None:
        return None

    async def latency_histogram(self, **_kw) -> list[LatencyBucket]:
        return []


async def test_recorder_persists_draft_to_repository():
    repo = InMemoryTurnRecordRepository()
    recorder = BackgroundTurnRecorder(repo)

    record_id = await recorder.record(TurnRecordingDraft(
        character_id="char-1",
        kind="chat",
        model_id="claude-opus-4-7",
        prompt_assembled="hello",
        response_text="world",
        latency_ms=123,
    ))
    await recorder.flush()

    assert record_id
    stored = await repo.get(record_id)
    assert stored is not None
    assert stored.character_id == "char-1"
    assert stored.response_text == "world"
    assert stored.latency_ms == 123
    assert stored.prompt_pack_hash


async def test_recorder_preserves_explicit_prompt_pack_hash():
    repo = InMemoryTurnRecordRepository()
    recorder = BackgroundTurnRecorder(repo)

    record_id = await recorder.record(TurnRecordingDraft(
        character_id="char-1",
        kind="chat",
        prompt_pack_hash="pack-explicit",
    ))
    await recorder.flush()

    stored = await repo.get(record_id)
    assert stored is not None
    assert stored.prompt_pack_hash == "pack-explicit"


async def test_recorder_honors_preallocated_record_id():
    repo = InMemoryTurnRecordRepository()
    recorder = BackgroundTurnRecorder(repo)

    record_id = await recorder.record(TurnRecordingDraft(
        id="turn-preallocated-1",
        character_id="char-1",
        kind="chat",
    ))
    await recorder.flush()

    assert record_id == "turn-preallocated-1"
    assert await repo.get("turn-preallocated-1") is not None


async def test_recorder_swallows_repository_errors():
    recorder = BackgroundTurnRecorder(_ExplodingRepo())

    # Must not raise even though the underlying repo throws.
    record_id = await recorder.record(TurnRecordingDraft(
        character_id="char-1", kind="chat",
    ))
    await recorder.flush()

    assert record_id  # id was still assigned at submit time


async def test_recorder_disabled_via_feature_flag(monkeypatch):
    monkeypatch.setenv("KOKORO_ENABLE_TURN_RECORDING", "0")
    repo = InMemoryTurnRecordRepository()
    recorder = BackgroundTurnRecorder(repo)

    record_id = await recorder.record(TurnRecordingDraft(
        character_id="char-1", kind="chat",
    ))
    await recorder.flush()

    assert record_id == ""
    assert await repo.list_recent() == []


async def test_feature_flag_defaults_on(monkeypatch):
    monkeypatch.delenv("KOKORO_ENABLE_TURN_RECORDING", raising=False)
    assert turn_recording_enabled() is True


async def test_feature_flag_off_values(monkeypatch):
    for raw in ("0", "false", "FALSE", "no", "off"):
        monkeypatch.setenv("KOKORO_ENABLE_TURN_RECORDING", raw)
        assert turn_recording_enabled() is False, f"raw={raw}"


async def test_null_recorder_returns_empty_string():
    recorder = NullTurnRecorder()
    assert await recorder.record(TurnRecordingDraft(
        character_id="c", kind="chat",
    )) == ""


async def test_recorder_truncates_oversized_prompt():
    repo = InMemoryTurnRecordRepository()
    recorder = BackgroundTurnRecorder(repo)

    huge_prompt = "x" * 250_000
    record_id = await recorder.record(TurnRecordingDraft(
        character_id="char-1",
        kind="chat",
        prompt_assembled=huge_prompt,
    ))
    await recorder.flush()

    stored = await repo.get(record_id)
    assert stored is not None
    # Hard cap (_MAX_PROMPT_CHARS) is 200_000 — anything larger gets a
    # truncation marker appended.
    assert len(stored.prompt_assembled) <= 200_500
    assert "truncated" in stored.prompt_assembled


async def test_latency_histogram_buckets_correctly():
    repo = InMemoryTurnRecordRepository()
    recorder = BackgroundTurnRecorder(repo)

    for latency in (10, 100, 300, 700, 1500, 5000):
        await recorder.record(TurnRecordingDraft(
            character_id="char-1",
            kind="chat",
            latency_ms=latency,
        ))
    await recorder.flush()

    buckets = await repo.latency_histogram(
        buckets_ms=(50, 200, 500, 1000, 3000),
    )
    counts = {(b.lower_ms, b.upper_ms): b.count for b in buckets}
    assert counts[(0, 50)] == 1     # 10
    assert counts[(50, 200)] == 1   # 100
    assert counts[(200, 500)] == 1  # 300
    assert counts[(500, 1000)] == 1  # 700
    assert counts[(1000, 3000)] == 1  # 1500
    assert counts[(3000, None)] == 1  # 5000


async def test_list_recent_filters_by_character_and_kind():
    repo = InMemoryTurnRecordRepository()
    recorder = BackgroundTurnRecorder(repo)

    for kind, char in (("chat", "a"), ("chat", "b"), ("proactive", "a")):
        await recorder.record(TurnRecordingDraft(
            character_id=char, kind=kind,
        ))
    await recorder.flush()

    a_only = await repo.list_recent(character_id="a")
    assert {r.character_id for r in a_only} == {"a"}
    proactive_only = await repo.list_recent(kind="proactive")
    assert {r.kind for r in proactive_only} == {"proactive"}


async def test_list_recent_excludes_content_mode_before_limit():
    repo = InMemoryTurnRecordRepository()
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)

    await repo.add(TurnRecord.new(
        id="latest-nsfw",
        character_id="char-1",
        kind="chat",
        post_turn_refs={"content_mode": "nsfw"},
        now=now + timedelta(minutes=1),
    ))
    await repo.add(TurnRecord.new(
        id="older-normal",
        character_id="char-1",
        kind="chat",
        post_turn_refs={"content_mode": "normal"},
        now=now,
    ))

    records = await repo.list_recent(
        character_id="char-1",
        exclude_content_mode="nsfw",
        limit=1,
    )

    assert [record.id for record in records] == ["older-normal"]


async def test_operator_feedback_update_and_filter():
    repo = InMemoryTurnRecordRepository()
    recorder = BackgroundTurnRecorder(repo)

    target_id = await recorder.record(TurnRecordingDraft(
        character_id="char-1",
        kind="chat",
        response_text="this felt off",
    ))
    other_id = await recorder.record(TurnRecordingDraft(
        character_id="char-1",
        kind="chat",
        response_text="this felt right",
    ))
    await recorder.flush()

    updated = await repo.update_operator_feedback(
        target_id,
        {"kind": "out_of_character", "note": "broke role"},
    )
    await repo.update_operator_feedback(
        other_id,
        {"kind": "felt_human", "note": "good hesitation"},
    )
    flagged = await repo.list_recent(operator_feedback_kind="out_of_character")

    assert updated is not None
    assert updated.operator_feedback["kind"] == "out_of_character"
    assert [record.id for record in flagged] == [target_id]
