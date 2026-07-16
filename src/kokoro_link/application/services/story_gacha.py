"""Pure seed-picking logic.

Given a character + today's date, decide *which* story seed(s) to fire
today. No LLM call — just filter (enabled, frame, cooldown, per-character
visibility) and weighted random sampling.

The actual narrative expansion is done by ``StoryEventExpander``
(Slice 2); this module returns the picked ``StorySeed``s so callers can
inspect / log / mock before committing to the expensive LLM step.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date as date_type, datetime, timedelta
from typing import Sequence

from kokoro_link.contracts.story import (
    StoryEventRepositoryPort,
    StorySeedRepositoryPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_seed import StorySeed


_LOGGER = logging.getLogger(__name__)
_DEFAULT_DRAW_COUNT = 1


@dataclass(frozen=True, slots=True)
class GachaResult:
    picked: tuple[StorySeed, ...]
    eligible_count: int
    """How many seeds passed all filters. Useful for telemetry — a low
    number for a long time means the operator should expand the pack."""
    reason_if_empty: str | None = None


class StoryGachaService:
    def __init__(
        self,
        *,
        seed_repository: StorySeedRepositoryPort,
        event_repository: StoryEventRepositoryPort,
        rng: random.Random | None = None,
    ) -> None:
        self._seeds = seed_repository
        self._events = event_repository
        self._rng = rng or random.Random()

    async def roll(
        self,
        *,
        character: Character,
        today: date_type,
        count: int = _DEFAULT_DRAW_COUNT,
    ) -> GachaResult:
        """Pick up to ``count`` seeds for the character today."""
        if count <= 0:
            return GachaResult(picked=(), eligible_count=0, reason_if_empty="count<=0")

        frame = character.world_frame or "modern"
        all_seeds = await self._seeds.list_for_character(
            character.id, include_global=True, enabled_only=True,
        )
        if not all_seeds:
            return GachaResult(
                picked=(), eligible_count=0,
                reason_if_empty="no seeds in pool (run import_story_seeds?)",
            )

        last_rolls = await self._events.last_roll_dates(character.id)
        today_str = today.isoformat()
        today_events = await self._events.get_for_day(character.id, today_str)
        already_picked_seed_ids = {e.seed_id for e in today_events}

        eligible = [
            s for s in all_seeds
            if _is_eligible(
                s, frame=frame, today=today,
                last_rolls=last_rolls,
                already_picked=already_picked_seed_ids,
            )
        ]
        if not eligible:
            return GachaResult(
                picked=(), eligible_count=0,
                reason_if_empty="all seeds on cooldown or frame-mismatched",
            )

        picked = _weighted_sample_without_replacement(
            eligible, min(count, len(eligible)), self._rng,
        )
        return GachaResult(picked=tuple(picked), eligible_count=len(eligible))


def _is_eligible(
    seed: StorySeed,
    *,
    frame: str,
    today: date_type,
    last_rolls: dict[str, str],
    already_picked: set[str],
) -> bool:
    if not seed.enabled:
        return False
    if not seed.fits_frame(frame):
        return False
    if seed.id in already_picked:
        # Already rolled today — don't duplicate within one day.
        return False
    last_iso = last_rolls.get(seed.id)
    if last_iso is None:
        return True
    try:
        last_date = datetime.strptime(last_iso, "%Y-%m-%d").date()
    except ValueError:
        return True
    if seed.cooldown_days <= 0:
        return True
    return (today - last_date) >= timedelta(days=seed.cooldown_days)


def _weighted_sample_without_replacement(
    seeds: Sequence[StorySeed],
    k: int,
    rng: random.Random,
) -> list[StorySeed]:
    """Draw ``k`` distinct seeds weighted by ``weight`` (Efraimidis-Spirakis).

    Uses the standard key trick ``u^(1/w)`` — higher weight = bigger key.
    Degenerate weights (zero / negative) get a tiny floor so they can
    still theoretically fire, but almost never.
    """
    if k >= len(seeds):
        # Shuffle so order isn't deterministic when we take all of them.
        out = list(seeds)
        rng.shuffle(out)
        return out
    keys: list[tuple[float, StorySeed]] = []
    for seed in seeds:
        w = max(1e-6, seed.weight)
        u = rng.random()
        # Larger key = higher rank.
        key = u ** (1.0 / w)
        keys.append((key, seed))
    keys.sort(key=lambda pair: pair[0], reverse=True)
    return [seed for _, seed in keys[:k]]
