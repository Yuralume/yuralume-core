"""ChatService persists post-turn peer meet intents."""

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.contracts.post_turn import PeerMeetIntent
from kokoro_link.infrastructure.repositories.in_memory_character_encounter_intents import (
    InMemoryCharacterEncounterIntentRepository,
)


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_chat_service_persists_peer_meet_intents() -> None:
    repo = InMemoryCharacterEncounterIntentRepository()
    service = ChatService.__new__(ChatService)
    service._character_encounter_intent_repository = repo  # noqa: SLF001
    service._clock = _Clock()  # noqa: SLF001

    await service._persist_peer_meet_intents(  # noqa: SLF001
        character_id="char-a",
        intents=[
            PeerMeetIntent(
                peer_character_id="char-b",
                desired_after_iso="2026-05-18T00:00",
                topic="聊使用者交代的明天碰面",
                source_text="明天去找小鈴",
            ),
        ],
    )

    rows = await repo.list_pending_for_character(
        "char-a",
        now=datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0].peer_character_id == "char-b"
    assert rows[0].topic == "聊使用者交代的明天碰面"
    assert rows[0].desired_after == datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc)
