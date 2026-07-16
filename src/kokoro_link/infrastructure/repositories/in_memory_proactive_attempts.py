"""In-process proactive-attempt audit log for dev/tests."""

from datetime import datetime

from kokoro_link.contracts.proactive import ProactiveAttemptRepositoryPort
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome

# Outcomes where the gate stopped the attempt before any expensive work
# happened. Keeping the cooldown anchored to "last time we passed the
# gate" means gate-blocked ticks don't extend the cooldown forever.
_NON_GATE_PASSING_OUTCOMES = {
    ProactiveOutcome.DISABLED,
    ProactiveOutcome.GATE_BLOCKED,
}


class InMemoryProactiveAttemptRepository(ProactiveAttemptRepositoryPort):
    def __init__(self) -> None:
        self._rows: list[ProactiveAttempt] = []

    async def add(self, attempt: ProactiveAttempt) -> None:
        self._rows.append(attempt)

    async def list_for_character(
        self, character_id: str, *, limit: int = 50,
    ) -> list[ProactiveAttempt]:
        matches = [r for r in self._rows if r.character_id == character_id]
        matches.sort(key=lambda r: r.decided_at, reverse=True)
        return matches[:limit]

    async def list_recent_sent(
        self, character_id: str, *, limit: int = 8,
    ) -> list[ProactiveAttempt]:
        matches = [
            r for r in self._rows
            if r.character_id == character_id
            and r.outcome == ProactiveOutcome.SENT
        ]
        matches.sort(key=lambda r: r.decided_at, reverse=True)
        return matches[:limit]

    async def count_sent_today(
        self, character_id: str, *, now: datetime,
    ) -> int:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return sum(
            1 for r in self._rows
            if r.character_id == character_id
            and r.outcome == ProactiveOutcome.SENT
            and r.decided_at >= start
        )

    async def latest_for_character(
        self, character_id: str,
    ) -> ProactiveAttempt | None:
        matches = [r for r in self._rows if r.character_id == character_id]
        if not matches:
            return None
        return max(matches, key=lambda r: r.decided_at)

    async def latest_passing_gate_for_character(
        self, character_id: str,
    ) -> ProactiveAttempt | None:
        matches = [
            r for r in self._rows
            if r.character_id == character_id
            and r.outcome not in _NON_GATE_PASSING_OUTCOMES
        ]
        if not matches:
            return None
        return max(matches, key=lambda r: r.decided_at)

    async def delete_for_character(self, character_id: str) -> int:
        before = len(self._rows)
        self._rows = [r for r in self._rows if r.character_id != character_id]
        return before - len(self._rows)
