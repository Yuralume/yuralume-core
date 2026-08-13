"""What makes a character *managed*, carried as one argument.

A managed character is one installed from a cloud-exclusive official card:
``characters.origin_official_card_id`` is non-NULL and the licensed voice is
bound at birth. Both facts arrive together, from the same install, and both
are unreachable afterwards — the update allowlist protects
``voice_profile`` and nothing anywhere writes the origin a second time.

So they travel as one value rather than as two optional keywords on
:meth:`CharacterService.create_character`. Two keywords would make "origin
without its voice" and "voice without its origin" expressible states that
nothing should ever construct; one dataclass makes the licence and the voice
a single thing a caller either has or does not.
"""

from __future__ import annotations

from dataclasses import dataclass

from kokoro_link.domain.value_objects.voice_profile import VoiceProfile


@dataclass(frozen=True, slots=True)
class ManagedCharacterOrigin:
    """The IP-partner card a new character is being installed from."""

    official_card_id: str
    voice_profile: VoiceProfile | None = None
    """The card's licensed voice, or ``None`` when the catalog has no
    exclusive voice bound to this card yet.

    ``None`` is a legitimate operational state, not a failure: a voice may
    be published after the card it belongs to, and refusing the install over
    it would make an ordinary ordering of two admin actions look like an
    outage. The character then starts on the deployment's default voice and
    the install logs which card was missing one.
    """


__all__ = ["ManagedCharacterOrigin"]
