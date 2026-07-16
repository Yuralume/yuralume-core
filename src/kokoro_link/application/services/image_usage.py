"""Shared image usage helpers for provider-reported token accounting."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kokoro_link.contracts.image_provider import ImageTokenUsage
from kokoro_link.domain.entities.generation_usage import (
    CostEstimate,
    STATUS_FAILED,
    UsageQuantity,
)


@dataclass(frozen=True, slots=True)
class ImageUsageParts:
    provider_id: str
    model_id: str
    quantity: UsageQuantity
    metadata: dict[str, Any]
    cost: CostEstimate | None = None


def image_usage_parts_from_provider(
    *,
    provider: object,
    requested: int,
    returned: int,
    status: str,
    base_metadata: dict[str, Any],
    billable_quantity: int | None = None,
) -> ImageUsageParts:
    provider_id = str(
        getattr(provider, "last_provider_id", "")
        or getattr(provider, "provider_id", "")
        or "",
    )
    model_id = str(getattr(provider, "last_model_id", "") or "")
    metadata = dict(base_metadata)
    metadata.setdefault("artifact_quantity", returned)
    token_usage = getattr(provider, "last_usage", None)

    if isinstance(token_usage, ImageTokenUsage):
        metadata.update(token_usage.to_metadata())
        return ImageUsageParts(
            provider_id=provider_id,
            model_id=model_id,
            quantity=UsageQuantity(
                usage_unit="token",
                input_quantity=token_usage.input_tokens,
                output_quantity=token_usage.output_tokens,
                total_quantity=token_usage.total_tokens,
                billable_quantity=(
                    token_usage.total_tokens if status != STATUS_FAILED else 0
                ),
                usage_is_estimated=token_usage.estimated,
            ),
            metadata=metadata,
            cost=_provider_cost(provider),
        )

    return ImageUsageParts(
        provider_id=provider_id,
        model_id=model_id,
        quantity=UsageQuantity(
            usage_unit="image",
            input_quantity=requested,
            output_quantity=returned,
            total_quantity=returned,
            billable_quantity=(
                billable_quantity
                if billable_quantity is not None
                else returned if status != STATUS_FAILED else 0
            ),
        ),
        metadata=metadata,
        cost=_provider_cost(provider),
    )


def _provider_cost(provider: object) -> CostEstimate | None:
    amount = getattr(provider, "last_cost_amount_usd", None)
    if amount is None:
        return None
    try:
        decimal_amount = Decimal(str(amount))
    except Exception:
        return None
    return CostEstimate(
        amount=decimal_amount,
        is_estimated=True,
        pricing_source="provider_response",
    )
