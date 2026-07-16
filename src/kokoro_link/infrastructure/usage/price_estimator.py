"""Static price-catalog cost estimator for generation usage events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from kokoro_link.contracts.generation_usage import UsageEventDraft
from kokoro_link.domain.entities.generation_usage import CostEstimate


@dataclass(frozen=True, slots=True)
class PriceCatalogEntry:
    provider_id: str
    capability: str
    usage_unit: str
    model_id: str = ""
    profile_id: str = ""
    voice_id: str = ""
    input_unit_price: Decimal = Decimal("0")
    output_unit_price: Decimal = Decimal("0")
    input_text_unit_price: Decimal = Decimal("0")
    input_image_unit_price: Decimal = Decimal("0")
    output_text_unit_price: Decimal = Decimal("0")
    output_image_unit_price: Decimal = Decimal("0")
    flat_unit_price: Decimal = Decimal("0")
    currency: str = "USD"
    pricing_version: str = ""

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "PriceCatalogEntry":
        return cls(
            provider_id=str(raw.get("provider_id", "") or ""),
            capability=str(raw.get("capability", "") or ""),
            usage_unit=str(raw.get("usage_unit", "") or ""),
            model_id=str(raw.get("model_id", "") or ""),
            profile_id=str(raw.get("profile_id", "") or ""),
            voice_id=str(raw.get("voice_id", "") or ""),
            input_unit_price=Decimal(str(raw.get("input_unit_price", "0") or "0")),
            output_unit_price=Decimal(str(raw.get("output_unit_price", "0") or "0")),
            input_text_unit_price=Decimal(
                str(raw.get("input_text_unit_price", "0") or "0"),
            ),
            input_image_unit_price=Decimal(
                str(raw.get("input_image_unit_price", "0") or "0"),
            ),
            output_text_unit_price=Decimal(
                str(raw.get("output_text_unit_price", "0") or "0"),
            ),
            output_image_unit_price=Decimal(
                str(raw.get("output_image_unit_price", "0") or "0"),
            ),
            flat_unit_price=Decimal(str(raw.get("flat_unit_price", "0") or "0")),
            currency=str(raw.get("currency", "USD") or "USD"),
            pricing_version=str(raw.get("pricing_version", "") or ""),
        )


class StaticPriceEstimator:
    def __init__(self, entries: list[PriceCatalogEntry] | None = None) -> None:
        self._entries = list(entries or [])

    @classmethod
    def from_json_file(cls, path: str | Path | None) -> "StaticPriceEstimator":
        if not path:
            return cls()
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            return cls()
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            raw_entries = payload.get("prices", [])
        else:
            raw_entries = payload
        entries = [
            PriceCatalogEntry.from_mapping(item)
            for item in raw_entries
            if isinstance(item, dict)
        ]
        return cls(entries)

    async def estimate(self, draft: UsageEventDraft) -> CostEstimate:
        entry = self._match(draft)
        if entry is None:
            return CostEstimate()
        quantity = draft.quantity
        if quantity is None:
            return CostEstimate(
                currency=entry.currency,
                pricing_source="catalog",
                pricing_version=entry.pricing_version,
            )
        if quantity.usage_unit == "token":
            amount = self._token_amount(entry, draft)
        elif quantity.usage_unit in {"image", "second", "frame", "character"}:
            amount = entry.flat_unit_price * Decimal(quantity.billable_quantity)
        else:
            amount = entry.flat_unit_price * Decimal(
                quantity.billable_quantity or quantity.total_quantity or 1,
            )
        return CostEstimate(
            currency=entry.currency,
            amount=amount,
            is_estimated=True,
            pricing_source="catalog",
            pricing_version=entry.pricing_version,
        )

    def _match(self, draft: UsageEventDraft) -> PriceCatalogEntry | None:
        candidates = [
            entry for entry in self._entries
            if entry.provider_id == draft.provider_id
            and entry.capability == draft.capability
            and (
                not entry.usage_unit
                or draft.quantity is None
                or entry.usage_unit == draft.quantity.usage_unit
            )
            and (not entry.model_id or entry.model_id == draft.model_id)
            and (not entry.profile_id or entry.profile_id == draft.profile_id)
            and (not entry.voice_id or entry.voice_id == draft.voice_id)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda entry: sum(
                1 for value in (entry.model_id, entry.profile_id, entry.voice_id)
                if value
            ),
            reverse=True,
        )
        return candidates[0]

    @staticmethod
    def _token_amount(entry: PriceCatalogEntry, draft: UsageEventDraft) -> Decimal:
        quantity = draft.quantity
        if quantity is None:
            return Decimal("0")
        metadata = draft.metadata or {}
        input_text = Decimal(str(metadata.get("input_text_tokens", 0) or 0))
        input_image = Decimal(str(metadata.get("input_image_tokens", 0) or 0))
        output_text = Decimal(str(metadata.get("output_text_tokens", 0) or 0))
        output_image = Decimal(str(metadata.get("output_image_tokens", 0) or 0))

        has_detail_prices = any((
            entry.input_text_unit_price,
            entry.input_image_unit_price,
            entry.output_text_unit_price,
            entry.output_image_unit_price,
        ))
        if not has_detail_prices:
            return (
                entry.input_unit_price * Decimal(quantity.input_quantity)
                + entry.output_unit_price * Decimal(quantity.output_quantity)
            )

        generic_input = max(
            Decimal(quantity.input_quantity) - input_text - input_image,
            Decimal("0"),
        )
        generic_output = max(
            Decimal(quantity.output_quantity) - output_text - output_image,
            Decimal("0"),
        )
        return (
            input_text * entry.input_text_unit_price
            + input_image * entry.input_image_unit_price
            + output_text * entry.output_text_unit_price
            + output_image * entry.output_image_unit_price
            + generic_input * entry.input_unit_price
            + generic_output * entry.output_unit_price
        )
