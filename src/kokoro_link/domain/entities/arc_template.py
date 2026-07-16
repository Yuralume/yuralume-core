"""Hand-written story-arc templates (Phase 2 of SCENE_BEAT_PLAN).

A ``StoryArc`` is the *runtime* arc — concrete dates, mutable beats, can
be modified by post-turn LLM signals or operator UI. An ``ArcTemplate``
is the *blueprint* — pure values authored as YAML in
``src/kokoro_link/data/arc_templates/``. The service layer materialises
a template into a fresh arc on demand (binding ``character_id`` and
turning ``day_offset`` into ``scheduled_date``).

Templates are deliberately **read-only at runtime**:

- No DB rows. The source of truth is the YAML file in repo, so authoring
  is git-diffable and templates ship with the deploy.
- No mutation API. Once a template materialises an arc, the arc lives on
  its own — the template can change without affecting in-flight arcs.
- ``binding`` lives on the template, not the character. A character can
  point at any template; the template tells us which world_frames it
  fits and any required character traits.

Symmetric with ``StoryArcBeat``: same scene-structure fields, same
tension scale, plus ``day_offset`` (0-based) instead of an absolute
date so the same template can be reused across characters / dates.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING

from kokoro_link.domain.entities.story_arc import (
    SCENE_ENCOUNTER,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
    _VALID_SCENE_TYPES,  # noqa: F401 — kept for clarity; we don't enforce here
)

if TYPE_CHECKING:  # pragma: no cover
    pass


_DEFAULT_DURATION_DAYS = 14
_DEFAULT_BEAT_COUNT = 8
DEFAULT_TONE = "daily"
"""Free-string tone hint surfaced to the expander so the same scene
structure can read as gentle slice-of-life or grim military drama
depending on the template's intent. Known values:

- ``daily`` — gentle slice-of-life, quiet emotional beats (default)
- ``dramatic`` — heightened tension, clear stakes, willing to be heavy
- ``mature`` — adult themes; doesn't avoid violence / intimacy / cruelty
- ``dark`` — psychological weight, moral ambiguity, uncomfortable truths
- ``lighthearted`` — comic relief, banter, low-stakes mishaps

Unknown values fall through to ``daily`` framing in the expander —
authors can introduce shades without a code change."""

ARC_TEMPLATE_SCOPE_GENERIC = "generic"
ARC_TEMPLATE_SCOPE_CHARACTER_BOUND = "character_bound"
ARC_TEMPLATE_CHARACTER_REF_SELF = "self"

DEFAULT_ARC_TEMPLATE_LANGUAGE = "zh-TW"
"""BCP-47-ish language tag for the *authored* prose of a template.

Bundled templates ship in ``zh-TW``; the field is pure metadata — the
picker surfaces it as a source-language badge and the materialise path
compares it against the operator's primary language to decide whether
an LLM translation is warranted. It is deliberately **not** a filter:
the shipped catalogue is small, so filtering by language would leave an
en/ja operator's picker empty. Structural behaviour never keys off it."""
_VALID_APPLICABILITY_SCOPES = {
    ARC_TEMPLATE_SCOPE_GENERIC,
    ARC_TEMPLATE_SCOPE_CHARACTER_BOUND,
}


def _normalise_str_tuple(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        cleaned = (raw or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ArcTemplateBinding:
    """Constraints on which characters this template fits.

    Empty fields = unconstrained. We keep this loose on purpose —
    templates are picked by the operator (UI dropdown), not auto-matched
    by some scoring heuristic, so the binding is only a sanity hint
    surfaced in the picker (e.g. "this template assumes a school
    setting"). The service layer doesn't reject a mismatched pick;
    operator gets to choose.
    """

    world_frames: tuple[str, ...] = ()
    """World frames this template fits. ``()`` = any frame."""

    required_traits: tuple[str, ...] = ()
    """Personality / interest tags that should be present for the arc
    to feel natural. Currently advisory only — the picker can dim
    mismatched templates but doesn't gate."""


@dataclass(frozen=True, slots=True)
class ArcTemplateBeat:
    """One scripted scene in a template.

    ``day_offset`` is days from arc start (0 = first day). Materialising
    converts it to ``scheduled_date = start_date + day_offset``. We also
    cap it at the arc's duration so a template author who wrote
    ``day_offset: 30`` on a 14-day arc still produces a usable beat
    on the last day instead of past the end.
    """

    sequence: int
    day_offset: int
    title: str
    summary: str
    tension: str = TENSION_SETUP
    scene_type: str = SCENE_ENCOUNTER
    location: str | None = None
    scene_characters: tuple[str, ...] = ()
    dramatic_question: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("ArcTemplateBeat.sequence must be >= 0")
        if self.day_offset < 0:
            raise ValueError("ArcTemplateBeat.day_offset must be >= 0")
        if not self.title.strip():
            raise ValueError("ArcTemplateBeat.title must be non-empty")
        if not self.summary.strip():
            raise ValueError("ArcTemplateBeat.summary must be non-empty")
        if not self.scene_type or not self.scene_type.strip():
            raise ValueError("ArcTemplateBeat.scene_type must be non-empty")
        for entry in self.scene_characters:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    "ArcTemplateBeat.scene_characters entries must be "
                    "non-empty strings",
                )

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        day_offset: int,
        title: str,
        summary: str,
        tension: str = TENSION_SETUP,
        scene_type: str = SCENE_ENCOUNTER,
        location: str | None = None,
        scene_characters: Iterable[str] = (),
        dramatic_question: str | None = None,
        required: bool = True,
    ) -> "ArcTemplateBeat":
        # Same normalisation rules as StoryArcBeat — keep both sides
        # symmetric so a template author and a planner author don't
        # have to remember two different conventions.
        seen: set[str] = set()
        deduped: list[str] = []
        for raw in scene_characters:
            label = (raw or "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            deduped.append(label)
        return cls(
            sequence=sequence,
            day_offset=day_offset,
            title=title.strip(),
            summary=summary.strip(),
            tension=(tension or "").strip() or TENSION_SETUP,
            scene_type=(scene_type or "").strip() or SCENE_ENCOUNTER,
            location=(location or "").strip() or None,
            scene_characters=tuple(deduped),
            dramatic_question=(dramatic_question or "").strip() or None,
            required=bool(required),
        )


@dataclass(frozen=True, slots=True)
class ArcTemplate:
    id: str
    """Stable identifier (filename stem). Used by ``Character.arc_template_id``
    to pick this template; must be unique across all loaded templates."""
    title: str
    premise: str
    theme: str
    language: str = DEFAULT_ARC_TEMPLATE_LANGUAGE
    """Language tag of the authored prose (title / premise / beat text).

    Metadata only — surfaced to the picker as a source-language badge and
    read by the materialise path to decide whether to translate into the
    operator's primary language. See ``DEFAULT_ARC_TEMPLATE_LANGUAGE``."""
    duration_days: int = _DEFAULT_DURATION_DAYS
    beats: tuple[ArcTemplateBeat, ...] = ()
    binding: ArcTemplateBinding = field(default_factory=ArcTemplateBinding)
    tone: str = DEFAULT_TONE
    """Tonal register that surfaces to the expander so the same scene
    structure can read as gentle slice-of-life or grim military drama
    depending on the template. See ``DEFAULT_TONE`` docstring for the
    canonical set; unknown values degrade to ``daily`` framing."""
    applicability_scope: str = ARC_TEMPLATE_SCOPE_GENERIC
    """Whether this blueprint is reusable or pinned to local characters.

    ``generic`` templates can be offered to any character. A
    ``character_bound`` template is only usable by its
    ``target_character_ids`` once it has landed in this deployment.
    Portable character-card YAML uses ``target_character_refs`` such as
    ``self`` and import maps them to fresh local ids before saving.
    """
    target_character_ids: tuple[str, ...] = ()
    target_character_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("ArcTemplate.id must be non-empty")
        if not self.title.strip():
            raise ValueError("ArcTemplate.title must be non-empty")
        if not self.premise.strip():
            raise ValueError("ArcTemplate.premise must be non-empty")
        if self.duration_days <= 0:
            raise ValueError("ArcTemplate.duration_days must be > 0")
        if not self.beats:
            raise ValueError("ArcTemplate.beats must be non-empty")
        if not self.tone or not self.tone.strip():
            raise ValueError("ArcTemplate.tone must be non-empty")
        if self.applicability_scope not in _VALID_APPLICABILITY_SCOPES:
            raise ValueError(
                "ArcTemplate.applicability_scope must be one of: "
                + ", ".join(sorted(_VALID_APPLICABILITY_SCOPES)),
            )
        for entry in (*self.target_character_ids, *self.target_character_refs):
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    "ArcTemplate target character entries must be "
                    "non-empty strings",
                )

    @classmethod
    def create(
        cls,
        *,
        id: str,
        title: str,
        premise: str,
        theme: str = "custom",
        language: str = DEFAULT_ARC_TEMPLATE_LANGUAGE,
        duration_days: int = _DEFAULT_DURATION_DAYS,
        beats: Iterable[ArcTemplateBeat] = (),
        binding: ArcTemplateBinding | None = None,
        tone: str = DEFAULT_TONE,
        applicability_scope: str = ARC_TEMPLATE_SCOPE_GENERIC,
        target_character_ids: Iterable[str] = (),
        target_character_refs: Iterable[str] = (),
    ) -> "ArcTemplate":
        ordered = tuple(
            sorted(beats, key=lambda b: (b.day_offset, b.sequence))
        )
        scope = (applicability_scope or "").strip() or ARC_TEMPLATE_SCOPE_GENERIC
        target_ids = _normalise_str_tuple(target_character_ids)
        target_refs = _normalise_str_tuple(target_character_refs)
        if scope == ARC_TEMPLATE_SCOPE_GENERIC:
            target_ids = ()
            target_refs = ()
        return cls(
            id=id.strip(),
            title=title.strip(),
            premise=premise.strip(),
            theme=(theme or "").strip() or "custom",
            language=(language or "").strip() or DEFAULT_ARC_TEMPLATE_LANGUAGE,
            duration_days=int(duration_days),
            beats=ordered,
            binding=binding or ArcTemplateBinding(),
            tone=(tone or "").strip() or DEFAULT_TONE,
            applicability_scope=scope,
            target_character_ids=target_ids,
            target_character_refs=target_refs,
        )

    @property
    def beat_count(self) -> int:
        return len(self.beats)

    def is_applicable_to(self, character_id: str) -> bool:
        """Return whether this template may be bound to ``character_id``."""
        if self.applicability_scope == ARC_TEMPLATE_SCOPE_GENERIC:
            return True
        target_id = (character_id or "").strip()
        if not target_id:
            return False
        return target_id in self.target_character_ids

    def with_target_character_ids(
        self,
        target_character_ids: Iterable[str],
        *,
        target_character_refs: Iterable[str] = (),
    ) -> "ArcTemplate":
        if self.applicability_scope == ARC_TEMPLATE_SCOPE_GENERIC:
            target_character_ids = ()
            target_character_refs = ()
        return replace(
            self,
            target_character_ids=_normalise_str_tuple(target_character_ids),
            target_character_refs=_normalise_str_tuple(target_character_refs),
        )

    def materialise(
        self,
        *,
        character_id: str,
        start_date: date,
    ) -> StoryArc:
        """Turn the template into a runtime ``StoryArc`` for ``character_id``.

        ``day_offset`` is converted to ``scheduled_date = start_date +
        day_offset`` and capped at ``start_date + duration_days`` so
        late-day beats authored past the duration still land on the
        arc's final day rather than after ``end_date``. The returned
        arc has ``status=ARC_ACTIVE`` and is ready to be persisted.
        """
        end_date = start_date + timedelta(days=self.duration_days)
        arc = StoryArc.create(
            character_id=character_id,
            title=self.title,
            premise=self.premise,
            theme=self.theme,
            start_date=start_date,
            end_date=end_date,
            tone=self.tone,
            source_template_id=self.id,
        )
        beats: list[StoryArcBeat] = []
        for tpl_beat in self.beats:
            offset = min(tpl_beat.day_offset, self.duration_days)
            beats.append(
                StoryArcBeat.create(
                    arc_id=arc.id,
                    sequence=tpl_beat.sequence,
                    scheduled_date=start_date + timedelta(days=offset),
                    title=tpl_beat.title,
                    summary=tpl_beat.summary,
                    tension=tpl_beat.tension,
                    scene_type=tpl_beat.scene_type,
                    location=tpl_beat.location,
                    scene_characters=tpl_beat.scene_characters,
                    dramatic_question=tpl_beat.dramatic_question,
                    required=tpl_beat.required,
                )
            )
        return arc.with_beats(beats)

    def with_beats(
        self, beats: Iterable[ArcTemplateBeat],
    ) -> "ArcTemplate":
        ordered = tuple(
            sorted(beats, key=lambda b: (b.day_offset, b.sequence))
        )
        return replace(self, beats=ordered)

    def with_language(self, language: str) -> "ArcTemplate":
        """Return a copy tagged with ``language`` (metadata only).

        Used by the translator to stamp the target language onto a
        localized copy so the picker badge reflects what the operator
        actually sees. Empty / blank falls back to the default tag.
        """
        return replace(
            self,
            language=(language or "").strip() or DEFAULT_ARC_TEMPLATE_LANGUAGE,
        )
