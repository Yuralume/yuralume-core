from typing import Protocol

from kokoro_link.domain.value_objects.character_state import CharacterState


class StateEnginePort(Protocol):
    def on_user_message(self, current_state: CharacterState, user_message: str) -> CharacterState:
        """Update state after user message."""

    def on_assistant_reply(self, current_state: CharacterState, assistant_message: str) -> CharacterState:
        """Update state after assistant reply."""
