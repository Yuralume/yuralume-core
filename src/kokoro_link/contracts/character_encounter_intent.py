"""Repository port for chat-extracted character encounter intents."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.character_encounter_intent import (
    CharacterEncounterIntent,
)


class CharacterEncounterIntentRepositoryPort(Protocol):
    async def get(self, intent_id: str) -> CharacterEncounterIntent | None:
        """Fetch one intent by id."""

    async def save(self, intent: CharacterEncounterIntent) -> None:
        """Upsert an intent."""

    async def add(self, intent: CharacterEncounterIntent) -> None:
        """Insert an intent."""

    async def find_pending_for_pair(
        self,
        character_a_id: str,
        character_b_id: str,
        *,
        now: datetime,
        horizon: datetime,
    ) -> CharacterEncounterIntent | None:
        """Return the oldest pending intent for this unordered pair."""

    async def list_pending_for_character(
        self, character_id: str, *, now: datetime, limit: int = 30,
    ) -> list[CharacterEncounterIntent]:
        """Return pending intents involving the character."""

    async def delete_for_character(self, character_id: str) -> int:
        """Delete intents involving the character."""

    async def delete_by_turn_record(
        self, character_id: str, turn_record_id: str,
    ) -> int:
        """Delete intents *recorded by* ``character_id`` under the turn
        ``turn_record_id``. Returns the number removed.

        Replaces a ``created_at >= turn_started_at`` window that was
        wrong twice over. ``_persist_peer_meet_intents`` runs in the
        background post-turn, so the window raced its own writer; and
        the window had no conversation scope, while this table has no
        conversation column to give it one — a character live in a web
        and a LINE thread at once had undo in one thread deleting the
        meeting the other thread had just agreed to. The anchor is
        immune to both: it names a turn, and a turn is one conversation's.

        Still scoped to ``character_id`` (the row's initiator) and never
        ``peer_character_id``: the table is shared by both characters in
        a pair, and the peer's own record of the meeting is not this
        turn's fact to delete. The anchor already implies the character,
        so this predicate is belt-and-braces rather than load-bearing.

        Rows with a ``NULL`` anchor — written before the column existed —
        match nothing here, by design: undo leaves them rather than
        falling back to a window whose failure mode is deleting another
        conversation's agreement."""
