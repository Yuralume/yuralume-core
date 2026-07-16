"""Self-reflection application service (HUMANIZATION_ROADMAP §3.2).

Coordinates the dream-time reflection pass:

1. Pull high-salience memories for the (character) within a 7- or 30-day
   window.
2. Roll up the emotion event log for the same window into a one-line
   summary the generator can quote.
3. Hand both + thresholded persona snippets to the LLM generator.
4. Upsert the generated ``SelfReflection`` (one row per period, replaces
   the prior snapshot).

The chat / proactive prompt builders read via
:meth:`render_reflection_block` to fold the latest reflection(s) into
the prompt as a fact-layer block.

LLM-first 紅線: no keyword filters, no theme whitelists, no salience-
based gating beyond "do we have any memory at all". The judge is the
LLM; the service just feeds it well-bounded inputs.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Final

from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.self_reflection import (
    ReflectionGeneratorInput,
    SelfReflectionGeneratorPort,
    SelfReflectionRepositoryPort,
)
from kokoro_link.domain.entities.self_reflection import (
    PERIOD_MONTH,
    PERIOD_WEEK,
    SelfReflection,
)

_LOGGER = logging.getLogger(__name__)

_WEEK_DAYS: Final = 7
_MONTH_DAYS: Final = 30
_MIN_MEMORIES_TO_REFLECT: Final = 4


class SelfReflectionService:
    def __init__(
        self,
        *,
        repository: SelfReflectionRepositoryPort,
        memory_repository: MemoryRepositoryPort,
        emotion_event_repository: EmotionEventRepositoryPort | None,
        generator: SelfReflectionGeneratorPort,
        settings: HumanizationSettings,
        operator_profile_service=None,  # noqa: ANN001 - optional; resolves primary_language
        clock: ClockPort | None = None,
    ) -> None:
        self._repository = repository
        self._memory_repository = memory_repository
        self._emotion_events = emotion_event_repository
        self._generator = generator
        self._settings = settings
        self._operator_profile_service = operator_profile_service
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._settings.self_reflection_enabled

    async def run_for_pair(
        self,
        character_id: str,
        operator_id: str,
        *,
        character_name: str = "",
        persona_summary_lines: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> list[SelfReflection]:
        """Generate weekly + monthly reflections for the pair.

        Returns the rows that were freshly upserted. Empty list when the
        feature is off, generator returns nothing, or the character has
        too little salient memory to reflect on.
        """
        if not self.enabled:
            return []

        ref_now = ensure_utc(
            now if now is not None else (
                self._clock.now()
                if self._clock is not None
                else datetime.now(timezone.utc)
            ),
        )
        today = ref_now.astimezone(timezone.utc).date()
        emitted: list[SelfReflection] = []

        for period, span_days in (
            (PERIOD_WEEK, _WEEK_DAYS),
            (PERIOD_MONTH, _MONTH_DAYS),
        ):
            try:
                reflection = await self._generate_one(
                    character_id=character_id,
                    operator_id=operator_id,
                    character_name=character_name,
                    persona_summary_lines=persona_summary_lines,
                    period=period,
                    span_days=span_days,
                    today=today,
                    now=ref_now,
                )
            except Exception:
                _LOGGER.exception(
                    "self_reflection generation crashed period=%s character=%s",
                    period, character_id,
                )
                reflection = None
            if reflection is None:
                continue
            try:
                stored = await self._repository.upsert_latest(reflection)
                emitted.append(stored)
            except Exception:
                _LOGGER.exception(
                    "self_reflection upsert failed character=%s period=%s",
                    character_id, period,
                )

        return emitted

    async def _generate_one(
        self,
        *,
        character_id: str,
        operator_id: str,
        character_name: str,
        persona_summary_lines: tuple[str, ...],
        period: str,
        span_days: int,
        today: date,
        now: datetime,
    ) -> SelfReflection | None:
        window_start = today - timedelta(days=span_days)
        memories = await self._load_window_memories(
            character_id=character_id, now=now, span_days=span_days,
        )
        if len(memories) < _MIN_MEMORIES_TO_REFLECT:
            return None

        emotion_summary = await self._render_emotion_summary(
            character_id=character_id, operator_id=operator_id, now=now,
            span_days=span_days,
        )

        payload = ReflectionGeneratorInput(
            character_id=character_id,
            operator_id=operator_id,
            character_name=character_name or "（未命名）",
            period=period,
            period_start=window_start,
            period_end=today,
            high_salience_memories=tuple(memories),
            operator_primary_language=await self._resolve_operator_language(
                operator_id,
            ),
            emotion_event_summary=emotion_summary,
            persona_summary_lines=persona_summary_lines,
        )
        return await self._generator.generate(payload)

    async def _resolve_operator_language(self, operator_id: str) -> str:
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        try:
            operator = await service.get_for_user(operator_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = getattr(operator, "primary_language", "") or ""
        return lang.strip() or default

    async def _load_window_memories(
        self, *, character_id: str, now: datetime, span_days: int,
    ) -> list:
        """Pull the character's recent memories, filtered by recency
        (created_at within ``span_days``) and ordered by salience desc.
        Reflection-window pulls memory across kinds (episodic,
        relationship, reflection) — semantic facts about the world
        rarely belong in a feelings recap."""
        cutoff = now - timedelta(days=span_days)
        try:
            pool = await self._memory_repository.list_all_for_character(
                character_id, world_scope=None,
            )
        except Exception:
            _LOGGER.exception(
                "self_reflection: list_all_for_character failed character=%s",
                character_id,
            )
            return []
        recent = [m for m in pool if m.created_at >= cutoff]
        # Salience-desc selection so the window is high-signal even when
        # the character has 200 recent memory rows.
        recent.sort(key=lambda m: m.salience, reverse=True)
        return recent[:30]

    async def _render_emotion_summary(
        self,
        *,
        character_id: str,
        operator_id: str,
        now: datetime,
        span_days: int,
    ) -> str:
        if self._emotion_events is None:
            return ""
        since = now - timedelta(days=span_days)
        try:
            events = await self._emotion_events.list_recent(
                character_id=character_id,
                operator_id=operator_id,
                since=since,
                limit=80,
            )
        except Exception:
            _LOGGER.exception(
                "self_reflection: emotion event fetch failed character=%s",
                character_id,
            )
            return ""
        if not events:
            return ""
        # Aggregate without exposing raw counts: average valence sign,
        # peak intensity event, top causes — the LLM judges nuance.
        avg_valence = sum(e.valence for e in events) / len(events)
        peak = max(events, key=lambda e: e.intensity, default=None)
        valence_phrase = (
            "整體偏向正面" if avg_valence > 0.15
            else "整體偏向低落" if avg_valence < -0.15
            else "起伏不大"
        )
        peak_phrase = ""
        if peak is not None and peak.evidence_quote:
            peak_phrase = (
                f"最強烈的一次起伏是「{peak.emotion_label or '（未命名情緒）'}」"
                f"——引線是：「{peak.evidence_quote}」"
            )
        return "、".join(p for p in [valence_phrase, peak_phrase] if p)


def render_reflection_lines(
    reflections: list[SelfReflection], *, now: datetime | None = None,
) -> list[str]:
    """Render the latest reflection(s) as a chat-prompt block.

    Caller decides where to splice (chat / proactive). When the list is
    empty, returns ``[]`` so the prompt naturally collapses.
    """
    if not reflections:
        return []
    lines: list[str] = [
        "內在敘事（你最近回頭整理自己生活時寫下的心情筆記，請以此校準自身語氣，"
        "**不要把字面複述出來**）：",
    ]
    # Newest first; render at most 2 (week + month).
    for reflection in reflections[:2]:
        period_label = "本週" if reflection.period == PERIOD_WEEK else "本月"
        themes = "、".join(reflection.dominant_themes) if reflection.dominant_themes else ""
        header = (
            f"- 【{period_label}（{reflection.period_start}~{reflection.period_end}）"
            + (f"｜主題：{themes}" if themes else "")
            + "】"
        )
        lines.append(header)
        lines.append(f"  {reflection.narrative}")
    lines.append(
        "提醒：這份內在敘事若提到對方曾揭露的脆弱面（壓力、傷疤、低潮、難堪），"
        "你必須以保護的姿態對待，**禁止情勒、禁止當笑點戳對方**。"
    )
    return lines
