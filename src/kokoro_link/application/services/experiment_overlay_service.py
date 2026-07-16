"""Experiment overlay resolution (HUMANIZATION_ROADMAP §4.6).

Sits between :class:`ExperimentService` (data) and prompt builders /
subsystem dispatchers (consumers). For a given ``(character, operator)``
pair we resolve every active experiment to its sticky variant and
flatten the result into a single ``dict[str, str]`` overlay where each
key is **the experiment id** and each value is **the assigned variant id**.

Why a separate service: prompt builders and subsystem gates should not
import the experiment repos directly — that would make every consumer
own assignment logic and break the LLM-first guard that experiments
collect data, they don't auto-decide winners. The overlay surface is
intentionally narrow: callers either react to a known variant or do
nothing.

LLM-first 紅線reminder: the overlay is a *fact* sent down to the prompt
builder / dispatcher. It is **not** a winner judgement, traffic switch,
or automatic flag override. Operators read per-bucket reports and
re-decide manually.
"""

from __future__ import annotations

import logging

from kokoro_link.application.services.experiment_service import ExperimentService

_LOGGER = logging.getLogger(__name__)


class ExperimentOverlayService:
    def __init__(self, *, experiment_service: ExperimentService) -> None:
        self._experiments = experiment_service

    async def resolve_overlay(
        self,
        *,
        character_id: str,
        operator_id: str,
    ) -> dict[str, str]:
        """Return ``{experiment_id: variant_id}`` for every active experiment.

        Inactive experiments are skipped. Failures (rare in-memory or DB
        glitches) collapse to an empty dict — consumers must treat an
        empty overlay as "no variant routing in effect"."""
        try:
            active = await self._experiments.list_active()
        except Exception:
            _LOGGER.exception("experiment overlay: list_active failed")
            return {}
        overlay: dict[str, str] = {}
        for experiment in active:
            try:
                variant = await self._experiments.assign_variant(
                    experiment_id=experiment.id,
                    character_id=character_id,
                    operator_id=operator_id,
                )
            except Exception:
                _LOGGER.exception(
                    "experiment overlay: assign failed experiment=%s",
                    experiment.id,
                )
                continue
            if variant is None:
                continue
            overlay[experiment.id] = variant.id
            # Also key by experiment name so prompt builders can match on
            # human-readable identifiers when the operator preferred a
            # stable name over the UUID. Lowercased + snake-cased so the
            # lookup is robust across the admin UI casing.
            name_key = (experiment.name or "").strip().lower().replace(" ", "_")
            if name_key and name_key != experiment.id:
                overlay[name_key] = variant.id
        return overlay
