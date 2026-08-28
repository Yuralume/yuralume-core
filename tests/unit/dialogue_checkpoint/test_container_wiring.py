"""What the container builds for DH3, and — mostly — what it does not.

The flag is implemented as *absence*: off means the chat service, the
post-turn and the rollback get ``None`` and run their pre-DH3 code with
nothing to branch on. That makes this the file where "the feature is
off by default" is actually enforced, because every other flag-off test
in the suite passes ``None`` by hand.
"""

from __future__ import annotations

from kokoro_link.bootstrap.container import _build_dialogue_checkpoint
from kokoro_link.bootstrap.settings import DialogueCheckpointSettings
from kokoro_link.infrastructure.repositories.in_memory_dialogue_checkpoints import (
    InMemoryDialogueCheckpointRepository,
)


class _Provider:
    """Stands in for ``ActiveLLMProviderPort`` — never called here."""


class _Conversations:
    async def recent_messages_for_character(self, character_id, *, limit, **_):  # noqa: ANN001
        return []


def _build(settings, *, provider=_Provider(), db=None):
    return _build_dialogue_checkpoint(
        settings=settings,
        db_session_factory=db,
        conversation_repository=_Conversations(),
        active_provider=provider,
    )


def test_the_default_settings_wire_nothing() -> None:
    wiring = _build(DialogueCheckpointSettings())
    assert wiring.repository is None
    assert wiring.reader is None
    assert wiring.updater is None
    assert wiring.window_limit is None


def test_the_flag_on_wires_all_three_halves() -> None:
    wiring = _build(DialogueCheckpointSettings(enabled=True))
    assert wiring.repository is not None
    assert wiring.reader is not None
    assert wiring.updater is not None


def test_the_window_limit_reaches_the_chat_prompt() -> None:
    wiring = _build(
        DialogueCheckpointSettings(enabled=True, window_messages=42),
    )
    assert wiring.window_limit == 42


def test_no_database_falls_back_to_the_in_memory_store() -> None:
    wiring = _build(DialogueCheckpointSettings(enabled=True))
    assert isinstance(wiring.repository, InMemoryDialogueCheckpointRepository)


def test_a_deployment_with_no_real_model_wires_nothing() -> None:
    """The merge is the one LLM call whose output is persisted *and*
    compounded. A deployment with no resolvable provider must not start
    accumulating a checkpoint it cannot write — an empty summary saved
    once would then be merged onto forever."""
    wiring = _build(DialogueCheckpointSettings(enabled=True), provider=None)
    assert wiring.repository is None
    assert wiring.reader is None
    assert wiring.updater is None
