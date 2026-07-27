"""Region code normalisation shared by the world-event pipeline.

A *region code* is the geographic binding of a piece of world material
(``TW`` / ``JP`` / ``US`` …). It is deliberately a free-form short string
rather than an enum: new regions must not require a migration, and the
value is compared against ``operator_profiles.country_code``, which is
seeded from GeoIP and is itself free-form.

``None`` is the meaningful zero value — *global*, relevant to everyone —
so blank / whitespace-only input collapses to ``None`` and every
comparison is done on the upper-cased form.
"""

from __future__ import annotations


def normalise_region(value: str | None) -> str | None:
    """Upper-case a region code; blank or ``None`` → ``None`` (global)."""
    if value is None:
        return None
    cleaned = value.strip().upper()
    return cleaned or None
