"""Port for optional arc-template prose translation.

Preview and bind/materialise can ask an LLM to render an arc template's
player-visible prose in the operator's primary language. Adapters must
be fail-soft: any provider, parsing, or validation issue returns the
original template so a translation problem never blocks binding a valid
arc.

Mirrors ``CharacterCardTranslatorPort`` — same fail-soft contract, same
"structural fields are immutable" red line. Only prose fields are ever
translated; structural fields (``theme`` / ``tone`` / ``tension`` /
``scene_type`` / ``day_offset`` / ``sequence`` / ``required`` /
``duration_days`` / ``world_frames`` / ``operator_position`` /
applicability / target ids) must remain byte-for-byte unchanged so the
model can never reinterpret an enum or reshape the arc.
``operator_position`` (OP0) is structural despite living next to a
prose field — it is a closed-vocabulary flag consumers branch on, not
language-bearing text.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kokoro_link.domain.entities.arc_template import ArcTemplate


class ArcTemplateTranslatorPort(ABC):
    @abstractmethod
    async def translate_template(
        self,
        template: ArcTemplate,
        *,
        target_language: str,
    ) -> ArcTemplate:
        """Translate only player-visible prose fields.

        Prose fields: ``title``, ``premise``, and each beat's ``title`` /
        ``summary`` / ``location`` / ``scene_characters`` /
        ``dramatic_question`` / ``operator_note``. Everything else is
        structural and must be preserved unchanged — including
        ``operator_position``, which sits right next to ``operator_note``
        on the same beat but is never translated (see module docstring).
        Returning ``template`` means translation was skipped or failed
        (fail-soft). The returned template's ``language`` should reflect
        ``target_language`` when a real translation happened so the
        picker badge is honest.
        """
