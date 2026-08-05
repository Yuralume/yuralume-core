"""Wire shapes for 起幕 (story scene) — the contract SC2 builds against.

Three endpoints share these models: open, state, and end. The shapes are
defined once here so the end route can ship its final response shape in
SC1-A even though its behaviour lands in SC1-D — a frontend written
against this file will not need reshaping when the wrap-up arrives.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kokoro_link.application.dto.chat import (
    ChatMessageResponse,
    StorySceneSessionResponse,
    SuggestedActionResponse,
)

# Re-exported, not redefined: in-scene chips ride back on ordinary chat
# replies too (SC1-C), and the closed session rides back on the turn that
# ended the scene (SC1-D). ``dto/chat`` is the module both surfaces can
# import without a cycle. Importers of these names are unaffected.
__all__ = [
    "EndStorySceneRequest",
    "EndStorySceneResponse",
    "OpenStorySceneRequest",
    "OpenStorySceneResponse",
    "StorySceneSessionResponse",
    "SuggestedActionResponse",
]


class OpenStorySceneRequest(BaseModel):
    """Optional body of ``POST /story-scene`` — the quote, and nothing else.

    The whole model is optional and so is the request body: a client that
    posts nothing still opens a scene, exactly as SC1-A shipped it. What it
    gives up is only the price binding, which is the same trade every other
    money surface makes for an older build.
    """

    quoted_price_cr: float | None = Field(default=None, ge=0)
    """The 起幕 price this player's own screen was showing when they pressed
    the button (R-9 quote binding).

    Core's process-local price cache is only a proxy for what was displayed —
    under several replicas, or straight after a back-office edit, it can hold
    a number this player never saw — so the client's own quote wins where it
    sends one. Quoting low buys nothing: the User service answers ``409
    price_changed`` rather than charging less than the published price."""


class OpenStorySceneResponse(BaseModel):
    """Everything needed to render the curtain going up."""

    session: StorySceneSessionResponse
    narration: ChatMessageResponse
    """``kind == "scene_narration"`` — render as the scene card, not a
    chat bubble."""
    character_message: ChatMessageResponse
    """The character's first performed line; an ordinary ``chat``
    message, so it is styled like any other reply."""
    suggested_actions: list[SuggestedActionResponse] = Field(
        default_factory=list,
    )
    """Opening moves offered alongside the curtain going up.

    Empty when the chips writer is unwired or failed — chips are an aid,
    never a precondition for the scene (SC1-C fail-soft)."""


class EndStorySceneRequest(BaseModel):
    session_id: str | None = None
    """Which scene to end. Omit to end whichever scene the character
    currently has open — the only one the UI can be showing."""


class EndStorySceneResponse(BaseModel):
    """The scene after 「結束場景」 — always closed, sometimes narrated."""

    session: StorySceneSessionResponse
    closing_narration: ChatMessageResponse | None = None
    """The wrap-up narration appended to the thread, when the closer
    produced one.

    ``null`` is a success, not a partial failure: the writer may be
    unwired, unavailable, or its draft may have been refused for
    inventing player actions (§3.4 #1). The scene is closed either way —
    the client re-reads the thread rather than assuming this is there."""
