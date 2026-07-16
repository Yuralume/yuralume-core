from kokoro_link.contracts.state import StateEnginePort
from kokoro_link.domain.value_objects.character_state import CharacterState


class SimpleStateEngine(StateEnginePort):
    def on_user_message(self, current_state: CharacterState, user_message: str) -> CharacterState:
        lowered = user_message.lower()
        fatigue_delta = 2 if len(user_message) > 40 else 1
        affection_delta = 1 if any(token in lowered for token in ["謝謝", "喜欢", "喜歡", "開心"]) else 0
        emotion = "caring" if "累" in user_message or "難過" in user_message else current_state.emotion
        return current_state.adjust(
            emotion=emotion,
            affection_delta=affection_delta,
            fatigue_delta=fatigue_delta,
            trust_delta=1,
            energy_delta=-fatigue_delta,
        )

    def on_assistant_reply(self, current_state: CharacterState, assistant_message: str) -> CharacterState:
        emotion = current_state.emotion
        if "陪" in assistant_message or "收到" in assistant_message:
            emotion = "warm"
        return current_state.adjust(emotion=emotion)
