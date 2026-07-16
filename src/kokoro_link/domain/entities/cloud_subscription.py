"""Persisted Cloud tenant subscription access state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CloudSubscriptionState:
    tenant_id: str
    locked: bool
    updated_at: datetime
