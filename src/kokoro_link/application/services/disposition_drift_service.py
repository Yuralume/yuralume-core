"""Disposition drift service (HUMANIZATION_ROADMAP §3.1).

Dream-time orchestrator that asks the LLM judge whether one
``CharacterDisposition`` dimension should nudge, enforces the per-
dimension cooldown, applies the single-band shift to the character,
and persists an audit row.

LLM-first 紅線: every decision (which dimension, which direction, why)
is the judge's. The service handles cooldown / extreme-band guard /
persistence — these are pure mechanics, not behavioural branches.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Final

from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.disposition_drift import (
    DispositionDriftHistoryRepositoryPort,
    DispositionDriftInput,
    DispositionDriftJudgePort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.disposition_drift_record import (
    DispositionDriftRecord,
)
from kokoro_link.domain.value_objects.disposition import CharacterDisposition

_LOGGER = logging.getLogger(__name__)

_WINDOW_DAYS: Final = 30
_COOLDOWN_DAYS: Final = 30
_MIN_MEMORIES: Final = 4

_BAND_ORDER: Final = ("low", "medium", "high")


def _shift_band(current: str, direction: str) -> str | None:
    """Move one step along the ``low → medium → high`` axis.

    Returns ``None`` when the move would push past an extreme — the
    judge attempted ``high + up`` or ``low + down``. The service drops
    the proposal in that case (per-pass 1-band constraint).
    """
    try:
        idx = _BAND_ORDER.index(current)
    except ValueError:
        return None
    if direction == "up":
        if idx >= len(_BAND_ORDER) - 1:
            return None
        return _BAND_ORDER[idx + 1]
    if direction == "down":
        if idx <= 0:
            return None
        return _BAND_ORDER[idx - 1]
    return None


class DispositionDriftService:
    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        history_repository: DispositionDriftHistoryRepositoryPort,
        memory_repository: MemoryRepositoryPort,
        emotion_event_repository: EmotionEventRepositoryPort | None,
        judge: DispositionDriftJudgePort,
        settings: HumanizationSettings,
        cooldown_days: int = _COOLDOWN_DAYS,
        clock: ClockPort | None = None,
    ) -> None:
        self._characters = character_repository
        self._history = history_repository
        self._memories = memory_repository
        self._emotion_events = emotion_event_repository
        self._judge = judge
        self._settings = settings
        self._cooldown_days = max(1, int(cooldown_days))
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._settings.disposition_drift_enabled

    async def run_for_character(
        self,
        character_id: str,
        *,
        operator_id: str = "default",
        persona_summary_lines: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> DispositionDriftRecord | None:
        """Run one drift pass; return the audit row when a shift landed.

        Returns ``None`` for any benign skip (feature off, cooldown,
        too few memories, judge declines, extreme-band guard rejects).
        Logged + absorbed exceptions also collapse to ``None`` — the
        dream pass must not fail because of an auxiliary signal.
        """
        if not self.enabled:
            return None

        ref = ensure_utc(
            now if now is not None else (
                self._clock.now()
                if self._clock is not None
                else datetime.now(timezone.utc)
            ),
        )
        try:
            character = await self._characters.get(character_id)
        except Exception:
            _LOGGER.exception(
                "disposition_drift: character fetch failed character=%s",
                character_id,
            )
            return None
        if character is None:
            return None

        try:
            memories = await self._load_window_memories(character_id, ref)
        except Exception:
            _LOGGER.exception(
                "disposition_drift: memory load failed character=%s",
                character_id,
            )
            return None
        if len(memories) < _MIN_MEMORIES:
            return None

        try:
            emotion_summary = await self._render_emotion_summary(
                character_id=character_id,
                operator_id=operator_id,
                now=ref,
            )
        except Exception:
            _LOGGER.exception(
                "disposition_drift: emotion summary failed character=%s",
                character_id,
            )
            emotion_summary = ""

        payload = DispositionDriftInput(
            character_id=character_id,
            character_name=character.name,
            disposition=character.disposition,
            emotion_event_summary=emotion_summary,
            high_salience_memories=tuple(memories),
            window_days=_WINDOW_DAYS,
            persona_summary_lines=persona_summary_lines,
        )

        try:
            proposal = await self._judge.judge(payload)
        except Exception:
            _LOGGER.exception(
                "disposition_drift judge crashed character=%s",
                character_id,
            )
            return None
        if proposal is None:
            return None

        # Cooldown — last shift for this dimension must be older than
        # cooldown window. The 30-day default mirrors real personality
        # drift cadence (you wouldn't notice somebody's candor swinging
        # every day; you'd notice over weeks).
        try:
            latest = await self._history.latest_for_dimension(
                character_id, proposal.dimension,
            )
        except Exception:
            _LOGGER.exception(
                "disposition_drift: cooldown lookup failed character=%s",
                character_id,
            )
            return None
        if latest is not None and (ref - latest.decided_at) < timedelta(
            days=self._cooldown_days,
        ):
            return None

        current_band = getattr(character.disposition, proposal.dimension, None)
        if current_band is None:
            return None
        new_band = _shift_band(current_band, proposal.direction)
        if new_band is None:
            return None

        # Apply the shift in-memory + persist character.
        try:
            new_disposition = character.disposition.with_overrides(
                **{proposal.dimension: new_band},
            )
            # Use dataclasses.replace directly — Character.update()'s
            # massive required-kwargs signature is awkward to feed when
            # only one field changes. The entity is frozen so replace()
            # respects the same immutability invariant.
            updated = replace(character, disposition=new_disposition)
            await self._characters.save(updated)
        except Exception:
            _LOGGER.exception(
                "disposition_drift: character save failed character=%s",
                character_id,
            )
            return None

        record = DispositionDriftRecord.new(
            character_id=character_id,
            dimension=proposal.dimension,
            from_band=current_band,
            to_band=new_band,
            reason=proposal.reason,
            evidence_quote=proposal.evidence_quote,
            now=ref,
        )
        try:
            await self._history.add(record)
        except Exception:
            _LOGGER.exception(
                "disposition_drift: history write failed character=%s",
                character_id,
            )
        return record

    async def _load_window_memories(
        self, character_id: str, now: datetime,
    ) -> list:
        cutoff = now - timedelta(days=_WINDOW_DAYS)
        pool = await self._memories.list_all_for_character(
            character_id, world_scope=None,
        )
        recent = [m for m in pool if m.created_at >= cutoff]
        recent.sort(key=lambda m: m.salience, reverse=True)
        return recent[:25]

    async def _render_emotion_summary(
        self, *, character_id: str, operator_id: str, now: datetime,
    ) -> str:
        if self._emotion_events is None:
            return ""
        since = now - timedelta(days=_WINDOW_DAYS)
        events = await self._emotion_events.list_recent(
            character_id=character_id,
            operator_id=operator_id,
            since=since,
            limit=80,
        )
        if not events:
            return ""
        avg_valence = sum(e.valence for e in events) / len(events)
        peak = max(events, key=lambda e: e.intensity, default=None)
        sign = "整體偏向正面" if avg_valence > 0.15 else (
            "整體偏向低落" if avg_valence < -0.15 else "起伏不大"
        )
        peak_phrase = ""
        if peak is not None and peak.evidence_quote:
            peak_phrase = (
                f"最強烈一次：「{peak.emotion_label or '（未命名）'}」"
                f"——引線「{peak.evidence_quote}」"
            )
        return "、".join(p for p in [sign, peak_phrase] if p)
