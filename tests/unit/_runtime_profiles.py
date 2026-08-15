"""A maximally-restrictive :class:`AccountRuntimeProfile` for tests.

Most tests that need "an account whose tier caps something" do not care
*which* tier it is — they care that a limit is set. They used to reach for
``DEMO_ACCOUNT_RUNTIME_PROFILE``, the one hardcoded tier->knob table Core
carried; that constant went away with the demo site (retired 2026-08-06),
and every tier's knobs now arrive from the cloud control-plane instead.

The values below are the ones that constant carried, kept verbatim so the
behaviour those tests pin is unchanged. Treat them as an arbitrary tight
tier, not as anybody's production configuration: a test that needs one
specific knob should build its own profile rather than lean on this.
"""

from __future__ import annotations

from datetime import timedelta

from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

RESTRICTIVE_ACCOUNT_RUNTIME_PROFILE = AccountRuntimeProfile(
    name="restricted",
    proactive_tick_multiplier=6,
    background_activity_multiplier=6,
    character_ttl=timedelta(days=3),
    max_characters=1,
    daily_character_create_limit=1,
    max_messages_per_session=80,
    strict_no_fallback=True,
    daily_chat_image_limit=1,
    daily_feed_post_limit=1,
    album_generation_enabled=False,
    video_generation_enabled=False,
    tts_enabled=False,
)
