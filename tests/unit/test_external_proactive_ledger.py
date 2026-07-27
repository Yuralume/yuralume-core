"""Contract suite for the external proactive pre-send ledger (LH4, DR-LH0-005).

Exercises the in-memory adapter — the first-class test twin of the SA one —
across every documented invariant: pre-send idempotency, hash-conflict, the
sticky terminal-ish transitions, the pending-due validity window, plus the
envelope canonicalization / hashing the ledger keys on.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_delivery.envelope_hash import (
    compute_envelope_hash,
)
from kokoro_link.contracts.external_proactive import (
    ENVELOPE_KIND_PROACTIVE,
    ProactiveEnvelope,
    ProactiveSegment,
    envelope_to_payload,
)
from kokoro_link.contracts.external_proactive_ledger import (
    ExternalProactiveEventConflict,
    ExternalProactiveEventState,
)
from kokoro_link.contracts.messaging import OutboundAttachment
from kokoro_link.infrastructure.repositories.in_memory_external_proactive_events import (
    InMemoryExternalProactiveEventRepository,
)

_NOW = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


def _envelope(*, event_id: str = "evt-1", text: str = "嗨") -> ProactiveEnvelope:
    return ProactiveEnvelope(
        event_id=event_id,
        tenant_id="tenant-a",
        account_id="acct-a",
        character_id="char-a",
        kind=ENVELOPE_KIND_PROACTIVE,
        segments=(ProactiveSegment(text=text),),
        attachments=(),
        locale="zh-TW",
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
    )


async def _save(repo, envelope: ProactiveEnvelope, *, now: datetime = _NOW):
    return await repo.save_pre_send(
        event_id=envelope.event_id,
        tenant_id=envelope.tenant_id,
        account_id=envelope.account_id,
        character_id=envelope.character_id,
        kind=envelope.kind,
        envelope_json=json.dumps(envelope_to_payload(envelope), sort_keys=True),
        payload_hash=compute_envelope_hash(envelope),
        expires_at=envelope.expires_at,
        now=now,
    )


# ---- envelope value object -------------------------------------------------


def test_envelope_text_rejoins_single_segment_verbatim() -> None:
    env = _envelope(text="早安～今天也加油")
    assert env.text == "早安～今天也加油"
    assert env.contract_version == 1


def test_envelope_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        _envelope().__class__(
            event_id="x", tenant_id="t", account_id="a", character_id="c",
            kind="broadcast", segments=(), attachments=(), locale="zh-TW",
            created_at=_NOW, expires_at=_NOW,
        )


def test_envelope_rejects_empty_event_id() -> None:
    with pytest.raises(ValueError):
        _envelope(event_id="")


def test_hash_is_stable_and_content_sensitive() -> None:
    a = compute_envelope_hash(_envelope(text="hi"))
    a_again = compute_envelope_hash(_envelope(text="hi"))
    b = compute_envelope_hash(_envelope(text="bye"))
    assert a == a_again
    assert a != b
    assert len(a) == 64


def test_payload_carries_attachment_shape() -> None:
    env = ProactiveEnvelope(
        event_id="e", tenant_id="t", account_id="a", character_id="c",
        kind=ENVELOPE_KIND_PROACTIVE,
        segments=(ProactiveSegment(text="hi"),),
        attachments=(
            OutboundAttachment(kind="image", url="https://x/y.png", mime_type="image/png"),
        ),
        locale="zh-TW", created_at=_NOW, expires_at=_NOW + timedelta(hours=1),
    )
    payload = envelope_to_payload(env)
    assert payload["attachments"] == [
        {"kind": "image", "url": "https://x/y.png", "mime_type": "image/png", "caption": None},
    ]


# ---- ledger repository -----------------------------------------------------


@pytest.mark.asyncio
async def test_save_pre_send_inserts_pending() -> None:
    repo = InMemoryExternalProactiveEventRepository()
    event = await _save(repo, _envelope())
    assert event.state is ExternalProactiveEventState.PENDING
    assert event.accept_attempts == 0
    assert event.last_error is None


@pytest.mark.asyncio
async def test_save_pre_send_same_hash_is_idempotent() -> None:
    repo = InMemoryExternalProactiveEventRepository()
    first = await _save(repo, _envelope())
    # Same id + same hash → returns the existing row, no duplicate/overwrite.
    second = await _save(repo, _envelope(), now=_NOW + timedelta(minutes=5))
    assert second.event_id == first.event_id
    assert second.created_at == first.created_at
    assert second.state is ExternalProactiveEventState.PENDING


@pytest.mark.asyncio
async def test_save_pre_send_hash_conflict_raises() -> None:
    repo = InMemoryExternalProactiveEventRepository()
    await _save(repo, _envelope(text="original"))
    with pytest.raises(ExternalProactiveEventConflict):
        await _save(repo, _envelope(text="tampered"))


@pytest.mark.asyncio
async def test_mark_accepted_transition_and_stickiness() -> None:
    repo = InMemoryExternalProactiveEventRepository()
    await _save(repo, _envelope())
    assert await repo.mark_accepted("evt-1", now=_NOW) is True
    stored = await repo.get("evt-1")
    assert stored.state is ExternalProactiveEventState.ACCEPTED
    assert stored.accept_attempts == 1
    # Already accepted → further marks are no-ops (sticky terminal-ish).
    assert await repo.mark_accepted("evt-1", now=_NOW) is False
    assert await repo.mark_terminal("evt-1", reason="x", now=_NOW) is False


@pytest.mark.asyncio
async def test_mark_terminal_records_reason() -> None:
    repo = InMemoryExternalProactiveEventRepository()
    await _save(repo, _envelope())
    assert await repo.mark_terminal("evt-1", reason="no endpoint", now=_NOW) is True
    stored = await repo.get("evt-1")
    assert stored.state is ExternalProactiveEventState.TERMINAL
    assert stored.last_error == "no endpoint"


@pytest.mark.asyncio
async def test_mark_missing_event_returns_false() -> None:
    repo = InMemoryExternalProactiveEventRepository()
    assert await repo.mark_accepted("ghost", now=_NOW) is False


@pytest.mark.asyncio
async def test_list_pending_due_excludes_expired_and_non_pending() -> None:
    repo = InMemoryExternalProactiveEventRepository()
    await _save(repo, _envelope(event_id="live"))
    await _save(repo, _envelope(event_id="accepted"))
    await repo.mark_accepted("accepted", now=_NOW)
    # An event whose validity window already passed relative to the query now.
    await _save(repo, _envelope(event_id="stale"))

    later = _NOW + timedelta(minutes=30)
    due = await repo.list_pending_due(now=later, limit=10)
    ids = {event.event_id for event in due}
    assert ids == {"live", "stale"}  # both still within the 1h window

    # Past every expiry → nothing due (a sweeper would mark_expired these).
    past_all = _NOW + timedelta(hours=2)
    assert await repo.list_pending_due(now=past_all, limit=10) == []


@pytest.mark.asyncio
async def test_mark_expired_transition() -> None:
    repo = InMemoryExternalProactiveEventRepository()
    await _save(repo, _envelope())
    assert await repo.mark_expired("evt-1", now=_NOW + timedelta(hours=2)) is True
    stored = await repo.get("evt-1")
    assert stored.state is ExternalProactiveEventState.EXPIRED
