"""What a managed character's picture must be drawn *from* (EC6).

An IP partner's card ships reference art that Cloud marked as the likeness
lock, and it landed in this deployment's own object storage at install time
(EC4). Every picture drawn of that character — the settings-page portrait,
the gacha batch, the one the character draws mid-chat, and the feed post's
background alike — has to be anchored to that art, or the same partner
character comes out looking like four different people.

The four generation call sites already had a parameter for "images the model
should look at" (``user_attachment_urls``, built for the player's own chat
photos), so the anchoring is an *addition to that sequence* rather than a
fifth argument threaded through four services: whatever an entry point was
already passing keeps flowing, the reference art goes in front of it.

Reference art first is the point of the ordering. When a player attaches
their own photo to a managed character's draw request, the two inputs
compete — the photo says "this outfit, this place", the card says "this
face". Leading with the card is what keeps the partner's character
recognisable in a picture the player still gets to direct.

A character the player made themselves is not managed, so this returns the
caller's own sequence unchanged and its generation payload is byte-for-byte
what it was before EC6 existed.
"""

from __future__ import annotations

from collections.abc import Sequence

from kokoro_link.application.services.official_character_art import (
    select_official_reference_urls,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.domain.entities.character import Character


def official_reference_attachments(
    character: Character,
    *,
    object_storage: ObjectStoragePort | None,
    user_attachment_urls: Sequence[str] = (),
) -> tuple[str, ...]:
    """Reference art for ``character`` ahead of ``user_attachment_urls``.

    ``object_storage`` is what turns a gallery URL back into the object key
    that carries the mark; a deployment without it cannot have installed a
    cloud-exclusive card in the first place, so its absence is the absence of
    the thing being injected rather than a gap.
    """
    existing = tuple(user_attachment_urls or ())
    if object_storage is None or not character.is_managed:
        return existing
    references = select_official_reference_urls(
        character.image_urls, object_storage.object_key_from_url,
    )
    if not references:
        return existing
    already_referenced = set(references)
    return (
        *references,
        *(url for url in existing if url not in already_referenced),
    )


__all__ = ["official_reference_attachments"]
