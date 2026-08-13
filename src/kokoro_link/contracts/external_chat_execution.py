"""Execution seam for driving one external-chat turn through :class:`ChatService`.

DR-LH0-004 makes a hosted external-chat turn a *recoverable state machine*, not a
single row written after the fact. :class:`ChatService.send_message` owns the
prompt / memory / tool pipeline and the one LLM call; it must NOT own the durable
turn spine (claim / lease / CAS append / snapshot / post-turn intent). This port
is the thin seam the turn service injects so ChatService can hand every
*conversation write* and every *stable identity / anchor* decision to the durable
layer — while the LLM pipeline itself is untouched.

Design contract ("Seam 介面"):

* ``external_turn=None`` → ChatService is bit-identical to the pre-LH2 web path.
  Every hook below fires only inside ``if external_turn is not None:``.
* The port NEVER holds a DB transaction across the LLM call, and the external
  conversation write never goes through the whole-conversation ``save()`` replace
  (that lost-update path must not become an external concurrency primitive).
* Every method is idempotent under recovery: a re-driven turn that already
  persisted its user / assistant row returns the existing ref and does NOT
  rewrite it.

This is a **pure abstraction** — it does not import the receipt repository. The
turn service (Core-C2) wires a concrete adapter that fences every write on
``owner_id`` + ``lease_version`` and CAS-appends via
``ConversationRepositoryPort.append_messages``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from kokoro_link.domain.entities.conversation import Conversation, Message


@dataclass(frozen=True, slots=True)
class PersistedTurnRef:
    """Durable coordinates of the user turn after a CAS append.

    ``row_id`` is the message row's autoincrement primary key;
    ``position`` its per-conversation positional index. On recovery
    (the turn already persisted its user row) the same ref is returned
    unchanged — the row is never rewritten.
    """

    row_id: int
    position: int


@dataclass(frozen=True, slots=True)
class CommittedTurnRef:
    """Durable coordinates of the assistant turn after its exactly-once CAS
    append (``COMMITTED``). ``position`` is the stable positional anchor the
    post-turn rebuild reads."""

    row_id: int
    position: int


@dataclass(frozen=True, slots=True)
class AttachmentSnapshot:
    """Minimal serialisable view of a tool-produced attachment, carried in the
    ``GENERATED`` checkpoint so recovery can rebuild the assistant message
    without re-running the LLM / tool."""

    kind: str
    url: str
    mime_type: str = "application/octet-stream"
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class GeneratedTurnSnapshot:
    """The minimal-but-sufficient assistant snapshot durably checkpointed
    (``PROCESSING`` → ``GENERATED``) after the LLM completes but BEFORE the
    conversation commit, so an expired-lease takeover finalises from the
    snapshot and NEVER re-runs the LLM.

    Holds the assistant text, its tool-artifact attachments (the tool trace's
    load-bearing residue), the classified message kind and the write-time
    content mode — everything needed to rebuild the response Message.
    """

    turn_id: str
    assistant_text: str
    attachments: tuple[AttachmentSnapshot, ...] = ()
    assistant_kind: str = "chat"
    content_mode: str = "normal"


@dataclass(frozen=True, slots=True)
class PostTurnAnchors:
    """Exact durable anchors for the post-turn job.

    ``user_message_row_id`` / ``assistant_message_row_id`` are the message
    rows' autoincrement ids (durable receipt anchors for exact-anchor
    recovery). ``assistant_position`` is the assistant turn's positional index
    — the anchor the current post-turn rebuild consumes (append-only
    conversations never shift an earlier index).
    """

    user_message_row_id: int
    assistant_message_row_id: int
    assistant_position: int


@runtime_checkable
class ExternalChatTurnExecutionPort(Protocol):
    """Durable execution seam for one external-chat turn.

    Injected per-request into :meth:`ChatService.send_message` as
    ``external_turn``. When absent, ChatService uses its legacy
    whole-conversation ``save()`` path unchanged.
    """

    async def persist_user_turn(
        self, conversation: Conversation, message: Message,
    ) -> PersistedTurnRef:
        """CAS-append the user turn before the LLM call and return its durable
        ref. If a prior attempt already persisted this turn's user row
        (recovery), return the existing ref and do NOT rewrite it."""
        ...

    async def checkpoint_generated(
        self, generated: GeneratedTurnSnapshot,
    ) -> None:
        """``PROCESSING`` → ``GENERATED``: durably checkpoint the assistant /
        response snapshot after the LLM completes and BEFORE the conversation
        commit, so recovery finalises without re-running the LLM."""
        ...

    async def commit_assistant_turn(
        self, conversation: Conversation, assistant_message: Message,
    ) -> CommittedTurnRef:
        """CAS-append the assistant turn exactly once (``GENERATED`` →
        ``COMMITTED``) and return its durable ref. ``conversation`` is the
        pre-assistant state (user already appended)."""
        ...

    async def commit_deferred_reply(
        self,
        conversation: Conversation,
        user_message: Message,
        assistant_message: Message,
    ) -> CommittedTurnRef:
        """Busy-defer finalize: the sole conversation write-exit of the
        busy-defer branch. Durably GENERATED-checkpoints the brief ack, then
        CAS-finalises the user + assistant (brief) turns; the pending
        follow-up is deduplicated by the turn's stable ``turn_id``. The user
        turn is idempotent when :meth:`persist_user_turn` already ran."""
        ...

    def stable_turn_id(self) -> str:
        """The turn's stable id (from the durable receipt), reused across
        retries — replaces the per-call ``uuid4()`` so the post-turn job key
        and turn record are deterministic under recovery."""
        ...

    def post_turn_anchors(self) -> PostTurnAnchors:
        """Exact anchors for the post-turn job — sourced from the durable
        commit rather than an in-memory ``len(messages) - 1`` guess."""
        ...

    async def heartbeat(self) -> None:
        """Extend the turn lease during a long operation. ChatService calls
        this immediately before and after the LLM generation AND periodically
        while it runs (see :meth:`heartbeat_interval_seconds`); the concrete
        adapter owns any throttling so redundant calls are cheap. A lost lease
        raises so the caller aborts every subsequent write."""
        ...

    def heartbeat_interval_seconds(self) -> float:
        """How often ChatService should :meth:`heartbeat` while the LLM / tool
        pipeline runs — comfortably inside the lease TTL (a third of it) so a
        long generation never lets the lease lapse into a takeover."""
        ...
