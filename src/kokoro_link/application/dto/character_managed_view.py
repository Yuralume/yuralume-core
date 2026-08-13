"""Player-facing projection of a *managed* (IP-partner) character.

A managed character's persona is the partner's property: it exists on
the server so the model can play the character, but it is not the
player's to read back or edit. The masking rule lives here — one field
map, applied inside :meth:`CharacterResponse.from_domain` — because
that DTO is the single projection all four character endpoints (roster,
detail, create response, PATCH response) return. Masking any lower
(per route) would leave the next endpoint to remember it; masking any
higher (a middleware) would lose the field types.

Two things this module deliberately does **not** do:

- It never touches the domain entity. Prompt assembly, image prompts and
  every other server-side consumer keep reading the full persona; only
  the HTTP projection is thinner.
- It carries no behaviour for ordinary characters. ``origin`` is NULL on
  every self-host row, the caller skips this module entirely, and the
  response is byte-for-byte what it was before the EC series (including
  the absence of the ``managed`` key — see
  ``CharacterResponse`` for why it is dropped when false).

What survives the mask is the display set a player still needs to see
their character on stage and to keep tuning the parts that *are* theirs:
``id`` / ``name`` / ``summary`` / ``image_urls`` / ``state`` /
``companions`` / ``disposition`` / ``body_state`` / the proactive cadence
fields / ``feed_daily_limit`` / ``operator_pace_preference`` /
``voice_profile`` (read-only display — the write path rejects changes).

The write side of the same boundary is
:mod:`kokoro_link.application.services.managed_character_update_policy`;
a pin test asserts every field masked here is also refused there, so a
field can never become invisible-but-writable.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Callable, Mapping

from kokoro_link.domain.value_objects.visual_subject import (
    DEFAULT_VISUAL_SUBJECT_TYPE,
)


#: Masked field → factory for the value that replaces it. Factories (not
#: literals) so no two responses can share a mutable list, and so the
#: empty value always matches the DTO's own field type: ``list`` → ``[]``,
#: ``str`` → ``""``, ``dict`` → ``{}`` (pydantic builds the payload
#: model's own defaults from it).
_MASKED_VALUE_FACTORIES: Mapping[str, Callable[[], Any]] = MappingProxyType({
    # Persona prose — the partner's writing.
    "personality": list,
    "interests": list,
    "speaking_style": str,
    "boundaries": list,
    "aspirations": list,
    "personality_type": dict,
    # Character facts that are part of the authored persona.
    "gender_identity": str,
    "third_person_pronoun": str,
    "date_of_birth": lambda: None,
    # Visual-generation inputs: everything that would let a client
    # reconstruct the official art direction from the API alone.
    "appearance": str,
    "visual_gender_presentation": str,
    "visual_subject_type": lambda: DEFAULT_VISUAL_SUBJECT_TYPE,
    "visual_generation_style": str,
    "loras": list,
    # World-awareness curation the partner authored for this character.
    "world_topics": list,
    "excluded_topics": list,
})


MANAGED_MASKED_RESPONSE_FIELDS: frozenset[str] = frozenset(
    _MASKED_VALUE_FACTORIES,
)
"""Response fields a managed character never exposes."""


def apply_managed_projection(values: dict[str, Any]) -> dict[str, Any]:
    """Mask the protected fields in ``values`` and flag it as managed.

    ``values`` is the kwargs dict :meth:`CharacterResponse.from_domain`
    is about to construct the model from; it is mutated in place and
    returned for call-site readability.
    """
    for name, factory in _MASKED_VALUE_FACTORIES.items():
        values[name] = factory()
    values["managed"] = True
    return values
