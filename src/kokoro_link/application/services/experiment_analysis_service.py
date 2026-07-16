"""Experiment manual-analysis service (HUMANIZATION_ROADMAP §4.6).

Owner decision (2026-05-21): the system collects structured A/B data
per bucket, but the **winner judgement is always manual** — operators
explicitly invoke this analysis when they want a high-tier model to
write a comparison narrative. **No auto invocation, no auto traffic
switching, no automatic decision.**

What this service does:

1. Compile a structured snapshot from :class:`ExperimentService` (per-
   variant assignment counts, optional metadata).
2. Pull lightweight per-bucket subsystem-health slices from the
   ``observability`` layer (turn counts + recent emotion-event volume
   per bucket).
3. Hand the structured payload to a high-tier ``ChatModelPort`` with
   a prompt that asks for a written comparison report — strengths,
   risks, possible next-steps. The model is **explicitly told it is
   not deciding a winner**; that wording is part of the rail.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from kokoro_link.application.services.experiment_service import (
    ExperimentService,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.nsfw_mode import CONTENT_MODE_NSFW
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.observability import TurnRecordRepositoryPort
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExperimentAnalysisResult:
    experiment_id: str
    invoked_model: str
    narrative: str
    structured_payload: dict[str, Any]
    error: str | None = None


class ExperimentAnalysisService:
    def __init__(
        self,
        *,
        experiment_service: ExperimentService,
        turn_record_repository: TurnRecordRepositoryPort | None = None,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str = "experiment_analysis",
    ) -> None:
        self._experiments = experiment_service
        self._turn_records = turn_record_repository
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def analyze(
        self,
        *,
        experiment_id: str,
        operator_note: str = "",
        since_hours: int = 168,
        operator_primary_language: str = "zh-TW",
    ) -> ExperimentAnalysisResult | None:
        """Pull report + slice + run the LLM. Returns ``None`` when the
        experiment doesn't exist; other failures return a result with
        ``error`` populated and an empty narrative."""
        report = await self._experiments.compile_report(experiment_id)
        if report is None:
            return None
        slice_metadata = await self._slice_subsystem_health_per_bucket(
            experiment_id=experiment_id, since_hours=since_hours,
        )
        # Merge slice into the report's free-form metadata bag.
        report_metadata = dict(report.metadata)
        report_metadata.update(slice_metadata)
        structured: dict[str, Any] = {
            "experiment_id": report.experiment_id,
            "name": report.name,
            "description": report.description,
            "salt": report.salt,
            "active": report.active,
            "buckets": [
                {
                    "variant_id": b.variant_id,
                    "label": b.label,
                    "assignment_count": b.assignment_count,
                }
                for b in report.buckets
            ],
            "metadata": report_metadata,
            "operator_note": operator_note,
            "since_hours": since_hours,
        }
        if await self._resolver.is_fake():
            return ExperimentAnalysisResult(
                experiment_id=experiment_id,
                invoked_model="(fake-provider; no LLM call)",
                narrative=(
                    "Structured payload prepared; no LLM call dispatched "
                    "because the default provider is the fake provider. "
                    "Run with a real provider to get a high-tier narrative."
                ),
                structured_payload=structured,
            )
        prompt = _build_prompt(
            structured, operator_primary_language=operator_primary_language,
        )
        try:
            narrative = await self._resolver.generate(prompt)
        except Exception as exc:
            _LOGGER.exception(
                "experiment analysis LLM call failed experiment=%s",
                experiment_id,
            )
            return ExperimentAnalysisResult(
                experiment_id=experiment_id,
                invoked_model="(resolved by feature key 'experiment_analysis')",
                narrative="",
                structured_payload=structured,
                error=f"LLM error: {type(exc).__name__}: {exc}",
            )
        return ExperimentAnalysisResult(
            experiment_id=experiment_id,
            invoked_model="(resolved by feature key 'experiment_analysis')",
            narrative=(narrative or "").strip(),
            structured_payload=structured,
        )

    async def _slice_subsystem_health_per_bucket(
        self, *, experiment_id: str, since_hours: int,
    ) -> dict[str, Any]:
        """Per-bucket turn count over the analysis window.

        Tiny slice — just turn counts grouped by variant — because the
        full subsystem health dashboard is per-character and the
        report already lives at experiment level. Future iterations
        can attach richer slices (cross-channel judge scores, etc.)
        without changing the analysis surface.
        """
        if self._turn_records is None:
            return {}
        try:
            # Fetch all turns in window once; partition by character
            # assignment client-side. This is fine for the manual-trigger
            # cadence (every 2-4 weeks per owner decision).
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            records = await self._turn_records.list_recent(
                character_id=None,
                kind=None,
                since=cutoff,
                exclude_content_mode=CONTENT_MODE_NSFW,
                limit=5000,
            )
        except Exception:
            _LOGGER.exception(
                "experiment analysis: turn slice fetch failed",
            )
            return {}
        # We rely on the experiment's assignments table to map character
        # → variant. Use the service helper which respects sticky bucket
        # cache.
        report = await self._experiments.compile_report(experiment_id)
        if report is None:
            return {}
        # Reconstruct character → variant mapping. The report's bucket
        # objects only carry counts, not the individual character ids;
        # we re-derive by querying assign_variant for each character we
        # see in the turn records (sticky so this is just a hash lookup
        # for known pairs).
        per_variant_counts: dict[str, int] = {b.variant_id: 0 for b in report.buckets}
        for record in records:
            try:
                variant = await self._experiments.assign_variant(
                    experiment_id=experiment_id,
                    character_id=record.character_id,
                    operator_id="default",
                )
            except Exception:
                continue
            if variant is None:
                continue
            per_variant_counts[variant.id] = per_variant_counts.get(
                variant.id, 0,
            ) + 1
        return {
            "subsystem_health_slice": {
                "window_hours": since_hours,
                "turn_count_by_variant": per_variant_counts,
                "total_turns_in_window": sum(per_variant_counts.values()),
            },
        }


def _build_prompt(
    payload: dict[str, Any],
    *,
    operator_primary_language: str = "zh-TW",
) -> str:
    """High-tier comparison prompt.

    Explicitly tells the LLM "this is not a winner declaration". The
    rail matters because operators copy parts of the narrative into
    decisions, and we don't want phrasing like "variant A wins" to
    leak from the model into downstream framing.

    The report is operator-visible in the admin UI, so its output
    language follows the operator's primary language rather than a
    hardcoded Traditional-Chinese mandate (bug B2 class)."""
    json_payload = json.dumps(payload, ensure_ascii=False, indent=2)
    language_hint = render_operator_language_hint(operator_primary_language)
    language_line = f"{language_hint}\n\n" if language_hint else ""
    return (
        f"{language_line}"
        "你是一位資深的 A/B 實驗顧問。下面是一個結構化的 bucket assignment "
        "與輕量 turn-count 切片。請寫一份**對比分析報告**：\n"
        "1. 兩個 variant 觀察到的差異（流量分布是否平均？turn 數量是否成比例？）\n"
        "2. 從本批資料能合理懷疑的「值得人眼進一步看哪些 dimension」（提示 3~5 條）\n"
        "3. 風險與盲點（樣本量不足、人為偏差、跨 bucket 污染等）\n"
        "**絕對禁止**寫「variant X 是贏家 / 採用 X」這類結論性語句。"
        "這份報告是給操作員自行判斷的素材，不是自動決策。\n\n"
        "結構化資料：\n"
        f"```json\n{json_payload}\n```\n\n"
        "整篇報告控制在 800 字以內；報告內容請使用上方指定的操作者可見輸出語言。"
    )
