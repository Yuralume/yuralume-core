"""One-off admin probe: drive a full video feed post end-to-end.

Sibling of ``trigger_video_generate.py`` but one layer up — that script
only verifies the ComfyUI adapter renders bytes; this one exercises the
full ``FeedComposerService._materialise()`` pipeline so we get a real
``feed_posts`` row with ``video_url`` populated.

What it bypasses:

  * **LLM compose** — monkey-patches the service's composer to return a
    fixed ``FeedComposerOutput(media_kind="video", video_prompt=...)``
    so we don't depend on the model rolling a 1-in-3 video pick.
  * **Cooldown / daily-limit gates** — calls ``_materialise()`` directly
    instead of ``tick()``. The gates exist to throttle automated posting
    cadence and aren't relevant when an operator is asking "does video
    actually persist?".

What it still goes through:

  * Active video provider resolution (``FEATURE_VIDEO_FEED`` routing).
  * Real Wan2.2 generation via the configured ComfyUI server.
  * Disk write under ``uploads/feed/<char>/<uuid>.mp4``.
  * Repository insert → SSE publish → self-memorialisation.

Use this when you want to verify the *plumbing* (LLM-picks-video →
generate → persist → URL on row) is intact without waiting for the
scheduler tick to roll the dice.

Run:  uv run python scripts/trigger_video_feed_post.py [character_id]
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone

from kokoro_link.application.services.feed_candidates import FeedCandidate
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.contracts.feed import FeedComposerOutput
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource

_FORCED_TEXT = (
    "下午的咖啡店窗邊，光斜斜灑進來。今天想把這一刻記下來。"
)
_FORCED_VIDEO_PROMPT = (
    "Anime style, cinematic short clip. A young woman in a cozy "
    "cafe, sitting by a window with afternoon light. She lifts a "
    "ceramic mug to her lips, then sets it down and tilts her head "
    "thoughtfully. Medium close-up, slow handheld drift, soft "
    "natural lighting, shallow depth of field."
)


class _ForcedComposer:
    """Stub :class:`FeedComposerPort` that always emits a video pick.

    We swap this onto ``FeedComposerService._composer`` so the real LLM
    adapter doesn't get called — that bypass is the whole point of the
    probe."""

    async def compose(self, payload):  # noqa: ANN001 — duck-typed port
        return FeedComposerOutput(
            content_text=_FORCED_TEXT,
            image_prompt="",
            video_prompt=_FORCED_VIDEO_PROMPT,
            media_kind="video",
        )


async def main(character_id: str | None) -> None:
    settings = AppSettings.from_env()
    container = build_container(settings)

    service = container.feed_composer_service
    if service is None:
        print("[abort] feed composer service not wired — check FEATURE flags")
        return
    if service._video_provider is None:  # noqa: SLF001 — probe-only
        print("[abort] no active video provider on the service")
        return

    char_svc = container.character_service
    if character_id:
        character = await char_svc.get_character_entity(character_id)
        if character is None:
            print(f"[abort] character {character_id!r} not found")
            return
    else:
        listing = await char_svc.list_characters()
        if not listing:
            print("[abort] no characters in DB — create one first")
            return
        character = await char_svc.get_character_entity(listing[0].id)
        assert character is not None
    print(f"[setup] using character {character.id} ({character.name})")

    # Synthetic candidate — silence source with a unique ref_id so the
    # repo-level (character_id, source) dedup doesn't reject the insert
    # if we run the probe twice in a row.
    marker = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    candidate = FeedCandidate(
        kind=FeedKind.MOOD,
        source=FeedSource(kind="probe", ref_id=f"video-{marker}"),
        hint="(probe) force a video post for plumbing verification",
        score=1.0,
        context_snippets=(),
        image_required=True,
        claim_token=None,
    )

    # Monkey-patch the composer for this single call. Restore after so
    # any background ticks resumed via the same process don't pick up
    # the stub (probe is short-lived but defensive doesn't cost much).
    real_composer = service._composer  # noqa: SLF001
    service._composer = _ForcedComposer()  # noqa: SLF001
    try:
        print("[run] calling _materialise with forced media_kind=video")
        started = time.monotonic()
        try:
            post = await service._materialise(  # noqa: SLF001
                character, candidate, datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - started
            print(f"[fail] after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
            raise
    finally:
        service._composer = real_composer  # noqa: SLF001
    elapsed = time.monotonic() - started

    if post is None:
        print(
            f"[fail] _materialise returned None after {elapsed:.1f}s — "
            "check logs (likely composer returned empty or persist raced)",
        )
        return
    print(f"[ok] post created in {elapsed:.1f}s")
    print(f"     id:          {post.id}")
    print(f"     kind:        {post.kind.value}")
    print(f"     source:      {post.source.kind}/{post.source.ref_id}")
    print(f"     video_url:   {post.video_url}")
    print(f"     image_url:   {post.image_url}")
    print(f"     video_prompt set: {bool(post.video_prompt)}")
    if post.video_url is None:
        print(
            "[warn] video_url is NULL — generation failed and the post "
            "fell back to image. This is the exact bug fingerprint the "
            "video_generator.py fix was meant to clear.",
        )


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(main(arg))
