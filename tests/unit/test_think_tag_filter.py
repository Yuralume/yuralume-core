"""BDD: streaming-safe ``<think>...</think>`` tag stripper.

Pins the behaviour Phase 3 of the reasoning-controls plan depends on:
strip complete think blocks whether they land in one chunk or straddle
several, pass non-think content through untouched, and fail open (never
silently eat content) when a tag is unbalanced.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from kokoro_link.infrastructure.llm.think_tag_filter import (
    _MAX_UNCLOSED_THINK_CHARS,
    strip_think_tags_stream,
    strip_think_tags_text,
)


async def _from(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


async def _collect(chunks: list[str]) -> str:
    out: list[str] = []
    async for piece in strip_think_tags_stream(_from(chunks)):
        out.append(piece)
    return "".join(out)


# ---- one-shot text ---------------------------------------------------


def test_text_strips_single_block() -> None:
    result = strip_think_tags_text("Hi <think>secret reasoning</think>there")
    assert result == "Hi there"


def test_text_strips_multiple_blocks() -> None:
    result = strip_think_tags_text(
        "<think>a</think>keep1<think>b</think>keep2",
    )
    assert result == "keep1keep2"


def test_text_without_tags_passes_through() -> None:
    text = "Just a normal reply with < and > and 1<2 comparisons."
    assert strip_think_tags_text(text) == text


def test_text_multiline_block_removed() -> None:
    result = strip_think_tags_text("A<think>line1\nline2\nline3</think>B")
    assert result == "AB"


def test_text_unbalanced_open_tag_kept_verbatim() -> None:
    # Model truncated mid-trace, or <think> used as prose. Must NOT eat
    # the rest of the reply.
    text = "Before <think>unfinished reasoning that never closes"
    assert strip_think_tags_text(text) == text


# ---- streaming: within a single chunk --------------------------------


@pytest.mark.asyncio
async def test_stream_block_within_one_chunk() -> None:
    result = await _collect(["Hi <think>x</think>there"])
    assert result == "Hi there"


# ---- streaming: tag straddles chunk boundaries -----------------------


@pytest.mark.asyncio
async def test_stream_open_tag_split_across_two_chunks() -> None:
    result = await _collect(["Hi <thi", "nk>x</think>there"])
    assert result == "Hi there"


@pytest.mark.asyncio
async def test_stream_close_tag_split_across_two_chunks() -> None:
    result = await _collect(["Hi <think>reasoning</thi", "nk>there"])
    assert result == "Hi there"


@pytest.mark.asyncio
async def test_stream_block_spread_over_many_chunks() -> None:
    # Simulate a server streaming one token at a time.
    chunks = list("Hello <think>hidden</think>world!")
    result = await _collect(chunks)
    assert result == "Hello world!"


@pytest.mark.asyncio
async def test_stream_tags_char_by_char() -> None:
    chunks = ["a", "<", "t", "h", "i", "n", "k", ">", "z", "z",
              "<", "/", "t", "h", "i", "n", "k", ">", "b"]
    result = await _collect(chunks)
    assert result == "ab"


# ---- streaming: no tags ----------------------------------------------


@pytest.mark.asyncio
async def test_stream_plain_content_passes_through() -> None:
    chunks = ["The value ", "of x < y ", "and a > b holds."]
    result = await _collect(chunks)
    assert result == "The value of x < y and a > b holds."


@pytest.mark.asyncio
async def test_stream_partial_open_marker_at_end_is_flushed() -> None:
    # A tail that looks like the start of <think> but never completes is
    # legitimate content and must be emitted at flush.
    result = await _collect(["done <thi"])
    assert result == "done <thi"


# ---- streaming: fail-open on unbalanced / oversized ------------------


@pytest.mark.asyncio
async def test_stream_unbalanced_open_tag_is_surfaced_on_flush() -> None:
    result = await _collect(["keep <think>never closes"])
    assert result == "keep <think>never closes"


@pytest.mark.asyncio
async def test_stream_oversized_unclosed_block_bails_open() -> None:
    # Way past the bailout budget with no close: rather than eat it all,
    # the filter surfaces the buffered content (open tag + text).
    huge = "z" * (_MAX_UNCLOSED_THINK_CHARS + 500)
    result = await _collect(["pre <think>" + huge])
    assert result.startswith("pre <think>zzz")
    assert huge in result
