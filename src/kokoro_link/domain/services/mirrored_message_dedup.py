"""Collapse cross-channel mirror copies of the same delivered message.

A character is one person on every channel, so LLM context is built from
``ConversationRepositoryPort.recent_messages_for_character`` — the
**merged** timeline across web / telegram / line / …. That merge is
correct for inbound turns (each user message exists once, on the channel
it arrived through) but wrong for **outbound fan-out**: a proactive push
is delivered to the web conversation *and* to the bound messaging
conversation, so the same sentence is persisted as two rows under two
conversations, seconds apart.

Observed damage (2026-07-29 dump, P7a): one proactive line appeared four
times inside a single chat prompt — twice in the 「近期對話」 transcript
and twice again in the 「你本對話最近自己說過的話」 anti-repetition rail —
and the diversity statistics measured a 1.000 self-similarity because the
embedding of a line was compared against the embedding of its own mirror
copy. That false signal then drives the anti-repetition machinery to
"correct" a repetition that never happened.

**The rule here is structural, never semantic.** Two entries collapse
only when they are byte-identical after whitespace normalisation, carry
the same role / kind / attachments, and land within a short window of
each other. No fuzzy similarity, no keyword matching — this is a
schema-level operation on duplicated rows, which the project's LLM-first
directive explicitly permits (unlike rewriting user-visible prose).

Nothing is deleted: both rows stay in the database, on their own
conversations, exactly as delivered. Only the *prompt material* is
collapsed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.conversation import Message

#: Fan-out delivery to a second channel happens within the same dispatch,
#: so the two rows are normally seconds apart; the observed pair was 53s.
#: Ten minutes is generous enough to absorb a retrying / queued external
#: adapter while staying far below "the character said this again later".
DEFAULT_MIRROR_WINDOW = timedelta(minutes=10)

_MessageKey = tuple[str, str, str, tuple[str, ...]]
_Identity = tuple[_MessageKey, datetime]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _normalize_text(raw: str) -> str:
    """Trim + collapse internal whitespace. Nothing else.

    Deliberately the weakest normalisation that still survives a channel
    adapter re-wrapping newlines — anything stronger would start guessing
    at meaning.
    """
    return " ".join((raw or "").split())


def _attachment_urls(message: Message) -> tuple[str, ...]:
    return tuple(a.url for a in message.attachments)


def _message_key(message: Message) -> _MessageKey | None:
    """Structural identity of a message, or ``None`` when not dedupable.

    A message with neither text nor attachments (a degenerate tool-only
    row) carries no payload to compare, so it is never a mirror candidate
    — collapsing those would silently merge unrelated placeholders.
    """
    text = _normalize_text(message.content)
    urls = _attachment_urls(message)
    if not text and not urls:
        return None
    return (message.role.value, message.kind.value, text, urls)


def _identity(message: Message) -> _Identity | None:
    key = _message_key(message)
    if key is None:
        return None
    return key, _as_utc(message.created_at)


def preferred_identities(messages: Iterable[Message]) -> frozenset[_Identity]:
    """Build the "keep this copy" set from one conversation's own rows.

    Callers pass the conversation the prompt is being built for; when a
    mirror pair is found, the copy that lives on *this* conversation wins
    so the transcript keeps the timestamps and ordering the user is
    actually looking at.
    """
    out: set[_Identity] = set()
    for message in messages:
        identity = _identity(message)
        if identity is not None:
            out.add(identity)
    return frozenset(out)


def dedupe_mirrored_messages(
    messages: Sequence[Message],
    *,
    preferred: Iterable[Message] = (),
    window: timedelta = DEFAULT_MIRROR_WINDOW,
) -> list[Message]:
    """Return ``messages`` with cross-channel mirror copies collapsed.

    Two entries are the same delivery when they share role, kind,
    whitespace-normalised text and attachment URLs, **and** their
    timestamps fall inside ``window`` of the first copy in the cluster.
    The surviving copy is the one belonging to ``preferred`` (the
    conversation being rendered) when present, otherwise the earliest —
    the original delivery rather than the echo.

    Relative order of the survivors is preserved exactly as given; this
    never reorders, rewrites or merges content.
    """
    if len(messages) < 2:
        return list(messages)
    preferred_keys = preferred_identities(preferred)

    grouped: dict[_MessageKey, list[int]] = {}
    for index, message in enumerate(messages):
        key = _message_key(message)
        if key is None:
            continue
        grouped.setdefault(key, []).append(index)

    dropped: set[int] = set()
    for key, indices in grouped.items():
        if len(indices) < 2:
            continue
        ordered = sorted(
            indices, key=lambda i: (_as_utc(messages[i].created_at), i),
        )
        for cluster in _cluster_by_window(messages, ordered, window):
            if len(cluster) < 2:
                continue
            winner = _pick_survivor(messages, cluster, key, preferred_keys)
            dropped.update(i for i in cluster if i != winner)

    if not dropped:
        return list(messages)
    return [m for i, m in enumerate(messages) if i not in dropped]


def _cluster_by_window(
    messages: Sequence[Message],
    ordered_indices: Sequence[int],
    window: timedelta,
) -> list[list[int]]:
    """Split same-key indices into windows anchored on the first copy.

    Anchoring on the cluster head (rather than chaining off the previous
    member) bounds every cluster to ``window`` in total, so a line the
    character legitimately repeats hours later starts a fresh cluster
    instead of being chained into the earlier one.
    """
    clusters: list[list[int]] = []
    current: list[int] = []
    anchor: datetime | None = None
    for index in ordered_indices:
        created = _as_utc(messages[index].created_at)
        if anchor is None or created - anchor > window:
            if current:
                clusters.append(current)
            current = [index]
            anchor = created
            continue
        current.append(index)
    if current:
        clusters.append(current)
    return clusters


def _pick_survivor(
    messages: Sequence[Message],
    cluster: Sequence[int],
    key: _MessageKey,
    preferred_keys: frozenset[_Identity],
) -> int:
    if preferred_keys:
        for index in cluster:
            if (key, _as_utc(messages[index].created_at)) in preferred_keys:
                return index
    return cluster[0]
