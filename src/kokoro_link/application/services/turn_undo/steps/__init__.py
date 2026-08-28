"""One file per undo step.

Importing a step from here rather than reaching into its module keeps
the registry's import block short and gives a step room to grow into a
package of its own without touching any caller.
"""

from __future__ import annotations

from kokoro_link.application.services.turn_undo.steps.address_preference import (
    AddressPreferenceRestoreStep,
)
from kokoro_link.application.services.turn_undo.steps.arc import ArcRestoreStep
from kokoro_link.application.services.turn_undo.steps.character_state import (
    CharacterStateRestoreStep,
)
from kokoro_link.application.services.turn_undo.steps.conversation import (
    ConversationTruncateStep,
)
from kokoro_link.application.services.turn_undo.steps.created_arc import (
    CreatedArcDeleteStep,
)
from kokoro_link.application.services.turn_undo.steps.dialogue_checkpoint import (
    DialogueCheckpointGuardStep,
)
from kokoro_link.application.services.turn_undo.steps.emotion_events import (
    EmotionEventDeleteStep,
)
from kokoro_link.application.services.turn_undo.steps.encounter_intents import (
    EncounterIntentDeleteStep,
)
from kokoro_link.application.services.turn_undo.steps.goals import (
    GoalRestoreStep,
)
from kokoro_link.application.services.turn_undo.steps.material_digest_cache import (
    MaterialDigestCacheInvalidateStep,
)
from kokoro_link.application.services.turn_undo.steps.memories import (
    MemoryDeleteStep,
)
from kokoro_link.application.services.turn_undo.steps.pending_follow_ups import (
    PendingFollowUpRestoreStep,
)
from kokoro_link.application.services.turn_undo.steps.persona_curiosity import (
    PersonaCuriosityDeleteStep,
)
from kokoro_link.application.services.turn_undo.steps.persona_evidence import (
    PersonaEvidenceRejectStep,
)
from kokoro_link.application.services.turn_undo.steps.scene_session import (
    SceneSessionRestoreStep,
)
from kokoro_link.application.services.turn_undo.steps.schedule import (
    ScheduleRestoreStep,
)
from kokoro_link.application.services.turn_undo.steps.state_snapshots import (
    StateSnapshotDeleteStep,
)
from kokoro_link.application.services.turn_undo.steps.story_events import (
    StoryEventDeleteStep,
)
from kokoro_link.application.services.turn_undo.steps.tombstone import (
    TombstoneStep,
)

__all__ = [
    "AddressPreferenceRestoreStep",
    "ArcRestoreStep",
    "CharacterStateRestoreStep",
    "ConversationTruncateStep",
    "CreatedArcDeleteStep",
    "DialogueCheckpointGuardStep",
    "EmotionEventDeleteStep",
    "EncounterIntentDeleteStep",
    "GoalRestoreStep",
    "MaterialDigestCacheInvalidateStep",
    "MemoryDeleteStep",
    "PendingFollowUpRestoreStep",
    "PersonaCuriosityDeleteStep",
    "PersonaEvidenceRejectStep",
    "SceneSessionRestoreStep",
    "ScheduleRestoreStep",
    "StateSnapshotDeleteStep",
    "StoryEventDeleteStep",
    "TombstoneStep",
]
