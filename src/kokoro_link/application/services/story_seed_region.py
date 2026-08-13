"""Operator language → story-seed region resolution.

Owner-ratified: the player's seed region
is derived from the operator's ``primary_language`` — zh* → ``tw``,
ja* → ``jp``, en* → ``west``. Anything else (or no language at all)
resolves to ``None``, which the gacha reads as "only ``global`` seeds",
so an unmapped locale never sees mismatched regional flavour.

Pure function, no I/O — the caller supplies whatever language string it
already has (``StoryEventService`` reads it off the operator profile).
"""

from __future__ import annotations

_LANGUAGE_PREFIX_TO_REGION: tuple[tuple[str, str], ...] = (
    ("zh", "tw"),
    ("ja", "jp"),
    ("en", "west"),
)


def resolve_seed_region(primary_language: str | None) -> str | None:
    """Map an operator's primary language tag to a seed region.

    Returns ``None`` for unknown / blank languages — the caller-side
    contract is "``None`` = global seeds only".
    """
    tag = (primary_language or "").strip().lower()
    if not tag:
        return None
    for prefix, region in _LANGUAGE_PREFIX_TO_REGION:
        if tag.startswith(prefix):
            return region
    return None
