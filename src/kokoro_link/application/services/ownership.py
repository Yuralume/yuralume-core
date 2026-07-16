"""Owner check helpers for per-user isolation.

Single source of truth for "does this user own that character?". The
guard is service-layer, not route-layer, because background loops
(proactive scheduler, world-event scheduler, dream pass) also touch
characters and they don't go through HTTP — we want them to fail the
same way if a feature ever misroutes a character_id from user A to
user B's loop.
"""

from __future__ import annotations

from kokoro_link.application.exceptions import CharacterNotOwned
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character


async def ensure_character_owned(
    repo: CharacterRepositoryPort,
    character_id: str,
    user_id: str,
) -> Character:
    """Fetch the character and verify ``user_id`` owns it.

    Raises :class:`CharacterNotOwned` for both "doesn't exist" and
    "exists but belongs to someone else" — see the exception docstring
    for the enumeration-prevention rationale.
    """
    character = await repo.get(character_id)
    if character is None or character.user_id != user_id:
        raise CharacterNotOwned(character_id)
    return character
