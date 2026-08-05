from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageKind(str, Enum):
    """Category of a message for LLM-context gating.

    - ``CHAT`` — normal user / assistant turn with narrative content.
    - ``TOOL_ONLY`` — assistant turn whose only payload is a tool-produced
      artifact (e.g. a ``/pic`` image) and whose text content is empty.
      Filtered out of schedule / arc / proactive context because a bare
      image URL isn't useful dialogue.
    - ``SCENE_NARRATION`` — third-person narration that opens or closes a
      story scene (「起幕」, SC1). Not the character speaking, so the
      anti-repetition sampler (which reads back the character's own
      lines) skips it, and the frontend frames it as a scene card rather
      than a chat bubble. It is otherwise **ordinary history**: the
      narration states facts the scene established, and a later turn that
      could not see them would contradict the story it is inside.

    Values are persisted verbatim in a ``String(16)`` column; a new
    member must stay within that budget. Unknown values read back from
    the database degrade to ``CHAT`` (see the repositories' ``_coerce_
    kind``), which is the safe direction — a message of an unrecognised
    kind is still dialogue the model should see.
    """

    CHAT = "chat"
    TOOL_ONLY = "tool_only"
    SCENE_NARRATION = "scene_narration"


class MessageContentMode(str, Enum):
    """Write-time content-flow marker.

    This records which user-selected mode was active when the message was
    authored.  It is not inferred from message text and must not be
    backfilled by content scanning.
    """

    NORMAL = "normal"
    NSFW = "nsfw"


@dataclass(frozen=True, slots=True)
class MessageAttachment:
    """Non-text payload produced by a tool call and attached to a message.

    Kept minimal so every persistence layer can serialise it trivially
    (tuple of primitives). The URL points into the ``/uploads/*``
    static mount or any other frontend-addressable location.
    """

    kind: str
    url: str
    mime_type: str = "application/octet-stream"
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    role: MessageRole
    content: str
    attachments: tuple[MessageAttachment, ...] = field(default_factory=tuple)
    kind: MessageKind = MessageKind.CHAT
    content_mode: MessageContentMode = MessageContentMode.NORMAL
    safe_summary: str = ""
    created_at: datetime = field(default_factory=_now_utc)
    """Wall-clock timestamp the message was authored.

    Source of truth for cross-source history merging — the conversation
    repo merges messages from web / telegram / line threads by
    ``created_at`` so the LLM sees the character as **one person** with
    a single timeline, not a separate persona per channel. Persisted on
    the row; old rows back-filled to migration apply-time."""


SOURCE_WEB = "web"


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    character_id: str
    messages: list[Message] = field(default_factory=list)
    source: str = SOURCE_WEB
    """Where this conversation lives.

    ``"web"`` for the built-in UI chat panel, ``"telegram"`` / ``"line"``
    / ... for messaging channels. Channel-bound threads are still
    addressable by id but the web UI's "latest conversation" lookup
    filters on source so TG / LINE activity doesn't hijack the panel.
    """
    version: int = 0
    """Optimistic-concurrency version this snapshot was loaded at (B3).

    ``0`` for a freshly ``start()``-ed conversation that has never been
    persisted. The repository stamps the stored value on read; ``save()``
    compares it against the current DB row under a ``FOR UPDATE`` lock: an equal
    version means no concurrent writer touched the row, so the whole-conversation
    replace (including a legitimate truncation like undo) applies verbatim; a
    differing version means a concurrent CAS append grew the tail after this
    snapshot was read, so those tail rows are merged back instead of dropped."""
    loaded_message_count: int = -1
    """Number of messages present when this snapshot was READ (B3).

    The stale-merge boundary: only DB rows at ``position >= loaded_message_count``
    can be concurrent appends (a CAS append only ever grows the tail past the
    snapshot). Rows below this boundary are the snapshot prefix — always
    re-written by the whole-conversation replace, never inferred as concurrent
    even if the writer edited one. ``-1`` marks a snapshot with no known boundary
    (never read from a repository); the merge then falls back to the write
    length (the pre-B3 behaviour)."""

    @classmethod
    def start(
        cls, *, character_id: str, source: str = SOURCE_WEB,
    ) -> "Conversation":
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            messages=[],
            source=source,
        )

    def append(self, message: Message) -> "Conversation":
        return replace(self, messages=[*self.messages, message])

    def recent_messages(
        self, limit: int, *, exclude_tool_only: bool = False,
    ) -> list[Message]:
        if exclude_tool_only:
            filtered = [m for m in self.messages if m.kind is not MessageKind.TOOL_ONLY]
            return filtered[-limit:]
        return self.messages[-limit:]
