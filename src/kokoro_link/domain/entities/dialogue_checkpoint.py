"""The cumulative summary of everything older than the raw tail.

One row per ``(character_id, operator_id)`` pair — the same key the
unified cross-source timeline uses, because the character is one person
across web / Telegram / LINE and their shared history is one story.

What replaced what
------------------
Before this entity the chat prompt re-summarised turns 4-8 with a fresh
LLM call **on every single turn**, threw the result away, and never
looked further back than eight messages. The checkpoint is the opposite
on all three counts: it is written at most once per post-turn, it is
persisted, and each merge folds the previous summary in rather than
starting over — so its coverage grows without its length doing the same.

The coverage cursor
-------------------
``covers_until_created_at`` plus ``covers_until_message_key`` name the
newest message the summary has absorbed.

The key is **not a database id**: the domain ``Message`` carries none
(see ``domain/entities/conversation.py``) — messages are positional rows
inside a conversation and the application layer never sees the row's
integer primary key. So the boundary is identified by a content
fingerprint, computed by :func:`checkpoint_cursor_key`. That is enough
for both jobs it has:

* **finding the boundary** in a freshly loaded window, exactly, even
  when several messages share a wall-clock second; and
* **compare-and-swap**, where the token only has to be equal to what
  the writer last read — its shape is irrelevant.

Two identical messages at the identical instant produce the same key.
That is a real collision and it is harmless: they are interchangeable
as a boundary, since everything at or before either of them is covered.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from kokoro_link.domain.entities.conversation import Message

CURSOR_KEY_LENGTH = 32
"""Hex characters kept from the boundary fingerprint. 128 bits — far
past the point where an accidental collision between two messages of the
same pair is worth a line of code to handle."""


def _as_utc(value: datetime) -> datetime:
    return (
        value.astimezone(timezone.utc)
        if value.tzinfo
        else value.replace(tzinfo=timezone.utc)
    )


def checkpoint_cursor_key(message: Message) -> str:
    """Fingerprint the message a checkpoint's coverage ends at.

    Deterministic across processes and restarts: it hashes only fields
    the message carries verbatim from the database (author, timestamp,
    text). Nothing here is a security boundary — a cheap digest of
    stable content is exactly what is wanted, and the whole value is
    opaque to every caller.
    """
    payload = "\x1f".join((
        message.role.value,
        _as_utc(message.created_at).isoformat(),
        message.content or "",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[
        :CURSOR_KEY_LENGTH
    ]


@dataclass(frozen=True, slots=True)
class DialogueCheckpoint:
    """One pair's rolling dialogue summary and how far it reaches."""

    character_id: str
    operator_id: str
    summary_text: str
    covers_until_message_key: str
    covers_until_created_at: datetime
    updated_at: datetime
    model: str = ""
    """Which model produced ``summary_text``.

    Recorded, never read by the runtime. It exists so that when a
    checkpoint reads badly the operator can tell whether it came from the
    model they think is configured — the merge prompt is one of the few
    places a cheap model's output is compounded rather than discarded."""

    stale: bool = False
    """Set when something the checkpoint absorbed may no longer be true.

    The turn-undo guard raises it, in two situations: the reversed turn
    is inside the covered region (which the coverage invariant says
    cannot happen), and — the ordinary one — a merge could be in flight
    right now over rows the undo is about to delete, in which case this
    flag is also the latch that makes that merge's compare-and-swap
    fail.

    A stale checkpoint is **not read**: the reader treats it as absent
    and the prompt degrades to raw turns until the next update rebuilds
    it. It is kept rather than deleted only so the rebuild has a row to
    compare-and-swap against; nothing reads its text again."""

    def __post_init__(self) -> None:
        character_id = (self.character_id or "").strip()
        operator_id = (self.operator_id or "").strip()
        if not character_id or not operator_id:
            raise ValueError(
                "Dialogue checkpoint requires character_id and operator_id",
            )
        if not (self.covers_until_message_key or "").strip():
            raise ValueError(
                "Dialogue checkpoint requires a coverage cursor key",
            )
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "operator_id", operator_id)
        object.__setattr__(
            self, "summary_text", (self.summary_text or "").strip(),
        )
        object.__setattr__(
            self,
            "covers_until_created_at",
            _as_utc(self.covers_until_created_at),
        )
        object.__setattr__(self, "updated_at", _as_utc(self.updated_at))

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        operator_id: str,
        summary_text: str,
        boundary: Message,
        now: datetime,
        model: str = "",
    ) -> "DialogueCheckpoint":
        """Build a checkpoint covering everything through ``boundary``."""
        return cls(
            character_id=character_id,
            operator_id=operator_id,
            summary_text=summary_text,
            covers_until_message_key=checkpoint_cursor_key(boundary),
            covers_until_created_at=boundary.created_at,
            updated_at=now,
            model=model,
        )

    def covers(self, message: Message) -> bool:
        """True when ``message`` is inside the summarised region.

        Timestamp first, fingerprint as the tie-break: a message stamped
        at the same instant as the boundary is only covered if it *is*
        the boundary. Erring towards "not covered" is the safe direction
        — an uncovered message is merely shown raw, whereas wrongly
        calling one covered would drop it from the prompt entirely.
        """
        moment = _as_utc(message.created_at)
        if moment < self.covers_until_created_at:
            return True
        if moment > self.covers_until_created_at:
            return False
        return checkpoint_cursor_key(message) == self.covers_until_message_key

    def marked_stale(self, *, now: datetime) -> "DialogueCheckpoint":
        """This checkpoint, flagged for a from-scratch rebuild."""
        return replace(self, stale=True, updated_at=_as_utc(now))


__all__ = [
    "CURSOR_KEY_LENGTH",
    "DialogueCheckpoint",
    "checkpoint_cursor_key",
]
