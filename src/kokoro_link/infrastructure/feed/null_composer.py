"""Fail-soft null feed composer.

Returns an empty output unconditionally — used when no LLM is wired
or in tests that don't want to spend a model call composing feed
posts. The service treats an empty body as "skip this tick", so
plumbing a Null composer cleanly disables feed publishing without
extra feature flags.
"""

from __future__ import annotations

from kokoro_link.contracts.feed import (
    FeedComposerInput,
    FeedComposerOutput,
    FeedComposerPort,
)


class NullFeedComposer(FeedComposerPort):
    async def compose(
        self, payload: FeedComposerInput,
    ) -> FeedComposerOutput:
        return FeedComposerOutput(content_text="")
