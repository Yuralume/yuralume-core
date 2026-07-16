"""Streaming-safe ``<think>...</think>`` tag stripper.

Local thinking models (Qwen3, DeepSeek-R1 distills) sometimes emit their
reasoning trace inline in the ``content`` field wrapped in
``<think>...</think>`` even when the server-side thinking switch is off,
or when there is no switch at all. This module removes those blocks so
the raw reasoning never leaks into a character reply.

Two entry points, sharing one state machine:

* :func:`strip_think_tags_text` — one-shot pass over a fully-materialised
  string (the non-streaming ``generate`` path).
* :func:`strip_think_tags_stream` — an async generator that wraps a chunk
  stream and yields cleaned chunks. The ``<think>`` / ``</think>`` markers
  routinely straddle chunk boundaries when a server streams one token at a
  time, so a naive per-chunk regex would miss them. The state machine
  buffers only the minimum needed to recognise a marker that spans chunks.

Fail-open contract: an *unbalanced* opening tag (model truncated before
``</think>``, or ``<think>`` used as ordinary prose) must never silently
swallow the rest of the reply. Once the in-think buffer exceeds
:data:`_MAX_UNCLOSED_THINK_CHARS` without a closing tag, we abandon the
strip and emit the buffered text verbatim — losing a little formatting is
strictly better than losing legitimate content.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"

_MAX_UNCLOSED_THINK_CHARS = 100_000
"""Give up stripping once an open ``<think>`` runs this long without a
close. Real reasoning traces are large but bounded; crossing this line
means the tag was almost certainly not a genuine think block (or the
stream was truncated mid-trace), so we fail open and surface the text
rather than eat the rest of the reply."""


class _ThinkTagStripper:
    """Incremental ``<think>`` block remover.

    Feed text via :meth:`feed` (returns the safe-to-emit prefix) and call
    :meth:`flush` once at end-of-stream to drain whatever is still held
    back. The instance carries all cross-chunk state so the same object
    can process either a single string or a long token stream.
    """

    def __init__(self) -> None:
        # Text held back because it *might* be the start of a marker that
        # will complete in a later chunk (outside think), or the tail that
        # might be the start of a closing marker (inside think).
        self._pending = ""
        self._in_think = False
        # Reasoning text accumulated since the current <think> opened. We
        # retain (rather than drop) it so an unbalanced open tag can be
        # surfaced verbatim on flush — losing content is worse than losing
        # a strip. Bounded by _MAX_UNCLOSED_THINK_CHARS to cap memory.
        self._think_buffer = ""

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._pending += text
        out: list[str] = []
        while self._pending:
            if self._in_think:
                if not self._consume_inside(out):
                    break
            else:
                if not self._consume_outside(out):
                    break
        return "".join(out)

    def flush(self) -> str:
        """Drain held-back text at end-of-stream.

        Outside a think block, a leftover partial-marker prefix was never
        a real tag, so it is legitimate content and must be emitted. Inside
        a think block with no close ever arriving, fail open and surface
        the buffered reasoning rather than dropping it silently.
        """
        pending = self._pending
        self._pending = ""
        if self._in_think:
            buffered = self._think_buffer
            self._think_buffer = ""
            self._in_think = False
            # Unbalanced open tag: surface the whole buffered block (open
            # tag + reasoning + any partial-close tail) so nothing is lost.
            return _OPEN_TAG + buffered + pending
        return pending

    # ---- internal state transitions ----------------------------------

    def _consume_outside(self, out: list[str]) -> bool:
        """Handle text while *not* inside a think block.

        Returns ``True`` when there may be more to process in ``_pending``,
        ``False`` when we must wait for the next chunk.
        """
        idx = self._pending.find(_OPEN_TAG)
        if idx != -1:
            # Everything before the tag is safe; drop the tag and switch in.
            out.append(self._pending[:idx])
            self._pending = self._pending[idx + len(_OPEN_TAG):]
            self._in_think = True
            self._think_buffer = ""
            return True
        # No complete open tag. Emit everything that cannot be the prefix
        # of an open tag; hold back a possible partial marker at the tail.
        hold = _partial_suffix_len(self._pending, _OPEN_TAG)
        if hold:
            out.append(self._pending[:-hold])
            self._pending = self._pending[-hold:]
        else:
            out.append(self._pending)
            self._pending = ""
        return False

    def _consume_inside(self, out: list[str]) -> bool:
        idx = self._pending.find(_CLOSE_TAG)
        if idx != -1:
            # Balanced block: drop the accumulated reasoning + the closing
            # tag and resume normal flow.
            self._pending = self._pending[idx + len(_CLOSE_TAG):]
            self._in_think = False
            self._think_buffer = ""
            return True
        # No close yet. Hold back a possible partial close marker at the
        # tail; everything before it is settled reasoning we retain in the
        # buffer (so an unbalanced open tag can be surfaced on flush).
        hold = _partial_suffix_len(self._pending, _CLOSE_TAG)
        if hold:
            self._think_buffer += self._pending[:-hold]
            self._pending = self._pending[-hold:]
        else:
            self._think_buffer += self._pending
            self._pending = ""
        if len(self._think_buffer) > _MAX_UNCLOSED_THINK_CHARS:
            # Bail out: this is almost certainly not a genuine think block
            # (or the stream was truncated). Fail open — surface the open
            # tag + all buffered text so no content is silently eaten.
            out.append(_OPEN_TAG + self._think_buffer + self._pending)
            self._think_buffer = ""
            self._pending = ""
            self._in_think = False
            return False
        return False


def _partial_suffix_len(text: str, marker: str) -> int:
    """Length of the longest suffix of ``text`` that is a proper prefix of
    ``marker`` — i.e. how much of the tail could still grow into ``marker``
    once more characters arrive. Returns 0 when nothing needs holding back.
    """
    max_len = min(len(text), len(marker) - 1)
    for size in range(max_len, 0, -1):
        if marker.startswith(text[-size:]):
            return size
    return 0


def strip_think_tags_text(text: str) -> str:
    """Remove complete ``<think>...</think>`` blocks from a whole string.

    Unbalanced tags are left intact (fail-open) via the shared stripper's
    flush semantics."""
    stripper = _ThinkTagStripper()
    return stripper.feed(text) + stripper.flush()


async def strip_think_tags_stream(
    chunks: AsyncIterator[str],
) -> AsyncIterator[str]:
    """Wrap ``chunks`` and yield the same text with ``<think>`` blocks
    removed, correctly handling tags that straddle chunk boundaries."""
    stripper = _ThinkTagStripper()
    async for chunk in chunks:
        cleaned = stripper.feed(chunk)
        if cleaned:
            yield cleaned
    tail = stripper.flush()
    if tail:
        yield tail
