from __future__ import annotations

"""``Character.world_frame`` vocabulary and the TR1 world-awareness default.

``Character.world_frame`` is stored as a free-form string, but every
producer of it — the creation UI (``CharacterCreateModal.vue``), the AI
draft generator (``character_draft/llm_generator.py``), and the arc
template intake wizard — only ever emits one of the values below. This
mirrors the "known-values dict, not string guessing" contract already
used by ``visual_subject.normalise_visual_subject_type``: a structural
lookup against a closed vocabulary, never a keyword/regex guess on free
text.
"""

WORLD_FRAMES: tuple[str, ...] = ("modern", "fantasy", "school", "custom")

# Frames modern/contemporary enough that the RSS world-event section
# fits the persona's own bubble by default
# (TRIAL_INSIGHTS_DEFAULTS_PLAN.md §1, TR1). ``any`` is not a real
# ``Character.world_frame`` value today (it is a ``StorySeed.world_frames``
# wildcard) — it is included here only so a frame that ever adopts that
# convention resolves the same way as ``modern``.
_MODERN_WORLD_FRAMES: frozenset[str] = frozenset({"modern", "any"})


def default_world_awareness_enabled(frame: str | None) -> bool:
    """TR1: the creation-time default for ``world_awareness_enabled``.

    Modern/contemporary frames default the RSS world-event section on;
    fantasy/school/custom — and anything unrecognised, blank, or not a
    string at all — default it off, so non-modern personas keep the
    original opt-in bubble behaviour. Only used to fill in a *missing*
    ``world_awareness_enabled`` at character-creation time: an explicit
    value from the client (including an explicit ``false``) always wins,
    and this never touches an existing character's stored value.
    """
    if not isinstance(frame, str):
        return False
    candidate = frame.strip().lower()
    return candidate in _MODERN_WORLD_FRAMES
