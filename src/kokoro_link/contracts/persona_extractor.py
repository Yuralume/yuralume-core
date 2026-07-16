"""Persona extractor port — pulls structured operator facts from a
completed chat turn.

Runs in its own LLM call (not folded into post-turn) for two reasons:

1. Different system prompt — extraction here is "observe user, never
   guess", whereas post-turn already balances memory + state + arc;
   mixing them in one call diluted accuracy in early experiments.
2. The output schema is open-ended (any of 25-ish field_keys across
   four layers) and benefits from a focused JSON-mode prompt.

Implementations MUST honour the LLM-first project rule: no keyword
matching, no regex special-cases. Hallucination defence lives in two
places — the prompt (asking for verbatim quotes) and the application
service (substring-checking those quotes against real user messages).
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.profile_field import CandidateField
from kokoro_link.domain.value_objects.resolved_address import ResolvedAddress


class PersonaExtractorPort(Protocol):
    async def extract(
        self,
        *,
        character_id: str,
        operator: OperatorProfile,
        current_persona: OperatorPersona,
        conversation_id: str,
        user_message_id: str,
        user_message: str,
        assistant_message: str,
        recent_messages: list[Message] | None = None,
        resolved_player_address: ResolvedAddress | None = None,
    ) -> list[CandidateField]:
        """Return zero or more staged candidates **scoped to this
        character**.

        Empty list is the correct answer when the turn revealed
        nothing about the operator. Implementations MUST NOT raise on
        bad LLM output — degrade to an empty result so the chat path
        stays alive.

        ``character_id`` stamps the resulting ``CandidateField`` so a
        different character won't inherit what this one observed.
        ``current_persona`` is passed so the prompt can list what
        *this character* already knows and discourage re-extracting
        known facts (note: a sibling character's persona is invisible
        — that's the point).
        ``user_message_id`` is the DB id of the user's turn message;
        it lands inside each returned ``CandidateField.evidence_ref``
        so the dream job can audit the source later.
        """
