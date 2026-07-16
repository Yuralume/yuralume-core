"""One-off admin script: drive a single Wan2.2 video generation.

Bypasses the LLM composer and feed pipeline so we can isolate
"video generation actually works" from "LLM picks media_kind=video".
Builds the real container against the configured DB, picks a character
(by id, or the first one if omitted), resolves the active video
provider via ``FEATURE_VIDEO_FEED`` and calls ``generate`` with a
canned prompt. Reports elapsed time + writes the mp4 next to the
script so you can eyeball it.

Run:  uv run python scripts/trigger_video_generate.py [character_id]
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings

_DEFAULT_PROMPT = (
    "Anime style, cinematic short clip. A young woman in a cozy "
    "cafe, sitting by a window with afternoon light. She lifts a "
    "ceramic mug to her lips, then sets it down and tilts her head "
    "thoughtfully. Medium close-up, slow handheld drift, soft "
    "natural lighting, shallow depth of field, 24fps, 5 seconds."
)


async def main(character_id: str | None) -> None:
    settings = AppSettings.from_env()
    container = build_container(settings)

    registry = container.video_profile_registry
    print(f"[setup] video profiles registered: {registry.profile_ids}")
    if not registry.profile_ids:
        print("[abort] no video profiles — set KOKORO_VIDEO_PROFILES")
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

    # Bypass the preference-routing layer (ActiveVideoProvider isn't
    # surfaced on ServiceContainer) — drive the registry directly,
    # which is the same VideoProviderPort the feed tick eventually
    # resolves to. Good enough for a probe.
    profile_id = registry.profile_ids[0]
    print(f"[setup] using profile_id: {profile_id!r}")
    provider = registry.resolve(profile_id)
    if provider is None:
        print("[abort] registry.resolve() returned None")
        return
    print(f"[setup] provider class: {type(provider).__name__}")

    print(f"[run] generating with prompt:\n{_DEFAULT_PROMPT}\n")
    started = time.monotonic()
    try:
        blob = await provider.generate(
            character=character,
            positive=_DEFAULT_PROMPT,
            aspect="portrait",
            use_runtime_state=True,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - started
        print(f"[fail] after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        raise
    elapsed = time.monotonic() - started
    print(f"[ok] generated {len(blob):,} bytes in {elapsed:.1f}s")

    out_path = Path(__file__).resolve().parent.parent / "_video_probe.mp4"
    out_path.write_bytes(blob)
    print(f"[ok] wrote {out_path}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(main(arg))
