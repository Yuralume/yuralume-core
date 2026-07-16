"""Local usage ledger rows for generation-producing requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4


GenerationCapability = str
GenerationStatus = str

CAPABILITY_LLM: GenerationCapability = "llm"
CAPABILITY_IMAGE: GenerationCapability = "image"
CAPABILITY_VIDEO: GenerationCapability = "video"
CAPABILITY_TTS: GenerationCapability = "tts"

STATUS_SUCCEEDED: GenerationStatus = "succeeded"
STATUS_FAILED: GenerationStatus = "failed"
STATUS_CACHED: GenerationStatus = "cached"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class UsageQuantity:
    usage_unit: str = ""
    input_quantity: int = 0
    output_quantity: int = 0
    total_quantity: int = 0
    billable_quantity: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_is_estimated: bool = True


@dataclass(frozen=True, slots=True)
class CostEstimate:
    currency: str = "USD"
    amount: Decimal = Decimal("0")
    is_estimated: bool = True
    pricing_source: str = "unknown"
    pricing_version: str = ""


@dataclass(frozen=True, slots=True)
class GenerationUsageEvent:
    id: str
    request_id: str
    upstream_request_id: str
    turn_record_id: str | None
    conversation_id: str | None
    character_id: str | None
    operator_id: str
    capability: GenerationCapability
    feature_key: str
    source_surface: str
    routing_mode: str
    provider_id: str
    model_id: str
    profile_id: str
    voice_id: str
    prompt_pack_hash: str
    quantity: UsageQuantity
    cached: bool
    cost: CostEstimate
    latency_ms: int | None
    status: GenerationStatus
    error_code: str | None
    error_message: str | None
    artifact_count: int
    output_bytes: int | None
    duration_seconds: Decimal | None
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
    completed_at: datetime | None = None

    @classmethod
    def new(
        cls,
        *,
        request_id: str | None = None,
        upstream_request_id: str = "",
        turn_record_id: str | None = None,
        conversation_id: str | None = None,
        character_id: str | None = None,
        operator_id: str = "",
        capability: GenerationCapability,
        feature_key: str = "",
        source_surface: str = "",
        routing_mode: str = "",
        provider_id: str = "",
        model_id: str = "",
        profile_id: str = "",
        voice_id: str = "",
        prompt_pack_hash: str = "",
        quantity: UsageQuantity | None = None,
        cached: bool = False,
        cost: CostEstimate | None = None,
        latency_ms: int | None = None,
        status: GenerationStatus = STATUS_SUCCEEDED,
        error_code: str | None = None,
        error_message: str | None = None,
        artifact_count: int = 0,
        output_bytes: int | None = None,
        duration_seconds: Decimal | float | str | None = None,
        content_hash: str = "",
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
        completed_at: datetime | None = None,
        id: str | None = None,
    ) -> "GenerationUsageEvent":
        if not str(capability).strip():
            raise ValueError("capability is required")
        if cached and status == STATUS_SUCCEEDED:
            status = STATUS_CACHED
        resolved_duration: Decimal | None
        if duration_seconds is None:
            resolved_duration = None
        else:
            resolved_duration = Decimal(str(duration_seconds))
        return cls(
            id=id or str(uuid4()),
            request_id=request_id or str(uuid4()),
            upstream_request_id=upstream_request_id,
            turn_record_id=turn_record_id,
            conversation_id=conversation_id,
            character_id=character_id,
            operator_id=operator_id,
            capability=str(capability),
            feature_key=feature_key,
            source_surface=source_surface,
            routing_mode=routing_mode,
            provider_id=provider_id,
            model_id=model_id,
            profile_id=profile_id,
            voice_id=voice_id,
            prompt_pack_hash=prompt_pack_hash,
            quantity=quantity or UsageQuantity(),
            cached=cached,
            cost=cost or CostEstimate(),
            latency_ms=latency_ms,
            status=status,
            error_code=error_code,
            error_message=error_message,
            artifact_count=artifact_count,
            output_bytes=output_bytes,
            duration_seconds=resolved_duration,
            content_hash=content_hash,
            metadata=dict(metadata or {}),
            created_at=now or _utcnow(),
            completed_at=completed_at,
        )
