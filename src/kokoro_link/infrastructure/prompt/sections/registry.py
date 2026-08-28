"""Section registry, mutual-exclusion resolvers, and the assembler.

``build()`` is now two steps: snapshot the turn into a
:class:`~.context.PromptSectionContext`, then hand it to
:func:`assemble`. Everything that used to be inline control flow around
the big join lives here in one of three shapes:

``PromptSection``
    a named, ordered ``ctx -> list[str]`` renderer;
resolver
    the *only* place a section may be blanked because of another one —
    the digest takeover, the scene exclusivity, the experiment overlay;
``assemble``
    render everything, run the resolvers, concatenate in table order.

Resolvers run *after* rendering rather than gating it, because that is
what the original code did: the old ``build()`` computed every block
eagerly and then reassigned the suppressed ones to ``[]``. Keeping the
same shape keeps the output byte-identical and, more usefully, keeps a
resolver from having to know how to predict a renderer's emptiness.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.order import SECTION_ORDERS

SectionRenderer = Callable[[PromptSectionContext], list[str]]
"""A section's whole contract: read the frozen context, return lines."""

RenderedSections = dict[str, list[str]]
SectionResolver = Callable[[PromptSectionContext, RenderedSections], None]


@dataclass(frozen=True, slots=True)
class PromptSection:
    """One named block of the chat prompt."""

    name: str
    order: int
    render: SectionRenderer


def section(name: str, render: SectionRenderer) -> PromptSection:
    """Build a section, taking its order from the canonical table.

    Raises ``KeyError`` for a name the table does not know, so a typo is
    an import-time failure rather than a silently missing block.
    """
    return PromptSection(name=name, order=SECTION_ORDERS[name], render=render)


class PromptSectionRegistry:
    """The set of sections a prompt is assembled from.

    Registering a name that already exists **overrides** it — that is the
    extension seam: a caller can swap one block's rendering without
    forking the whole builder.
    """

    __slots__ = ("_sections",)

    def __init__(self, sections: Iterable[PromptSection] = ()) -> None:
        self._sections: dict[str, PromptSection] = {}
        for entry in sections:
            self.register(entry)

    def register(self, entry: PromptSection) -> None:
        self._sections[entry.name] = entry

    def __contains__(self, name: object) -> bool:
        return name in self._sections

    def __len__(self) -> int:
        return len(self._sections)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.ordered())

    def ordered(self) -> tuple[PromptSection, ...]:
        """Sections in render order. Ties break on name so the sort is
        total even if an out-of-tree section reuses an order value."""
        return tuple(
            sorted(self._sections.values(), key=lambda s: (s.order, s.name))
        )


# --------------------------------------------------------------------
# Resolvers — the only sanctioned way one section blanks another.
# --------------------------------------------------------------------

def resolve_scene_exclusivity(
    ctx: PromptSectionContext, rendered: RenderedSections
) -> None:
    """起幕 (SC1-C): a live framed scene *replaces* the scripted beat.

    Exactly one of ``story_scene`` / ``today_scene`` ever renders — they
    occupy the same slot and are the same kind of instruction, the live
    one only stronger.
    """
    if rendered.get("story_scene"):
        rendered["today_scene"] = []


DIGEST_SUPPRESSED_SECTIONS: Final[tuple[str, ...]] = (
    "emotion_events",
    "self_reflection",
    "story_events",
    "story_arc",
    "recent_feed",
)
"""What a material digest takes over.

The digest is a compressed restatement of the same five sources; leaving
the originals in alongside it doubles the material and invites the model
to treat the raw text as a style template.
"""


def resolve_material_digest_takeover(
    ctx: PromptSectionContext, rendered: RenderedSections
) -> None:
    if rendered.get("material_digest"):
        for name in DIGEST_SUPPRESSED_SECTIONS:
            rendered[name] = []


def resolve_experiment_overlay(
    ctx: PromptSectionContext, rendered: RenderedSections
) -> None:
    """HUMANIZATION_ROADMAP §4.6 sticky-bucket overlay.

    A variant id of ``off`` collapses the block it names. Only the two
    whole-section suppressions live here; ``subjective_time`` narrows the
    timing block's *content* instead and is read from
    ``RailsContext.include_catchup_hint`` by that section.
    """
    rails = ctx.rails
    if rails.experiment_overlay.get("self_reflection") == "off":
        rendered["self_reflection"] = []
    if (
        not rails.body_state_enabled
        or rails.experiment_overlay.get("body_state") == "off"
    ):
        rendered["body_state"] = []


DEFAULT_RESOLVERS: Final[tuple[SectionResolver, ...]] = (
    resolve_experiment_overlay,
    resolve_material_digest_takeover,
    resolve_scene_exclusivity,
)


# --------------------------------------------------------------------
# Registry assembly
# --------------------------------------------------------------------

def _all_sections() -> tuple[PromptSection, ...]:
    # Imported lazily: the section modules import ``PromptSection`` from
    # here, so a module-level import both ways would be a cycle.
    from kokoro_link.infrastructure.prompt.sections import (
        dialogue,
        health_care,
        honesty,
        identity,
        schedule,
        state,
        story,
        tools,
        vision,
    )

    return (
        *identity.SECTIONS,
        *state.SECTIONS,
        *schedule.SECTIONS,
        *story.SECTIONS,
        *tools.SECTIONS,
        *honesty.SECTIONS,
        *health_care.SECTIONS,
        *vision.SECTIONS,
        *dialogue.SECTIONS,
    )


_DEFAULT_REGISTRY: PromptSectionRegistry | None = None


def default_registry() -> PromptSectionRegistry:
    """The built-in section set, built once and shared.

    Callers that want to override a block should copy it
    (``PromptSectionRegistry(default_registry().ordered())``) rather than
    mutate the shared instance.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = PromptSectionRegistry(_all_sections())
    return _DEFAULT_REGISTRY


def assemble(
    ctx: PromptSectionContext,
    *,
    registry: PromptSectionRegistry | None = None,
    resolvers: Sequence[SectionResolver] = DEFAULT_RESOLVERS,
) -> str:
    """Render every section, apply the resolvers, join in table order."""
    active = registry if registry is not None else default_registry()
    sections = active.ordered()
    rendered: RenderedSections = {
        entry.name: entry.render(ctx) for entry in sections
    }
    for resolver in resolvers:
        resolver(ctx, rendered)
    lines: list[str] = []
    for entry in sections:
        lines.extend(rendered[entry.name])
    return "\n".join(lines)
