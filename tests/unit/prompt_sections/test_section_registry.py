"""Structural pins for the prompt section registry.

The goldens prove the *output* is unchanged. These prove the
*structure* stayed sane afterwards — the failure mode a byte-oracle
cannot see is a renderer that quietly stops being wired to anything:
its lines vanish from the prompt only for the fixtures that happen to
exercise it, and every golden that leaves the block empty still passes.

So: names are unique, the order table and the registry agree, and every
``_render_*`` helper living in the sections package is reachable from a
registered section.
"""

from __future__ import annotations

import ast
import pathlib
import pkgutil

import pytest

from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
)
from kokoro_link.infrastructure.prompt import sections as sections_pkg
from kokoro_link.infrastructure.prompt.sections.context import RailsContext
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
from kokoro_link.infrastructure.prompt.sections.order import (
    SECTION_ORDER,
    SECTION_ORDERS,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    DIGEST_SUPPRESSED_SECTIONS,
    PromptSection,
    PromptSectionRegistry,
    assemble,
    default_registry,
    resolve_experiment_overlay,
    resolve_material_digest_takeover,
    resolve_scene_exclusivity,
)

SECTION_MODULES = (
    identity,
    state,
    schedule,
    story,
    tools,
    honesty,
    health_care,
    vision,
    dialogue,
)

PACKAGE_DIR = pathlib.Path(sections_pkg.__file__).parent


def _all_declared_sections() -> list[PromptSection]:
    return [entry for module in SECTION_MODULES for entry in module.SECTIONS]


# --- names and order -------------------------------------------------


def test_section_names_are_unique_across_modules() -> None:
    names = [entry.name for entry in _all_declared_sections()]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == []


def test_registry_matches_the_order_table_exactly() -> None:
    registry = default_registry()
    # Both directions: no section without a table entry (it would have
    # nowhere to sort to), no table entry without a section (a block
    # that silently stopped rendering).
    assert registry.names == SECTION_ORDER


def test_every_section_takes_its_order_from_the_table() -> None:
    for entry in _all_declared_sections():
        assert entry.order == SECTION_ORDERS[entry.name]


def test_order_values_are_strictly_increasing_in_table_order() -> None:
    values = [SECTION_ORDERS[name] for name in SECTION_ORDER]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_unknown_section_name_fails_at_construction() -> None:
    from kokoro_link.infrastructure.prompt.sections.registry import section

    with pytest.raises(KeyError):
        section("no_such_block", lambda ctx: [])


# --- nothing left unwired --------------------------------------------


def _module_sources() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for info in pkgutil.iter_modules([str(PACKAGE_DIR)]):
        path = PACKAGE_DIR / f"{info.name}.py"
        trees[info.name] = ast.parse(path.read_text(encoding="utf-8"))
    return trees


def _function_reference_graph() -> tuple[dict[str, set[str]], dict[str, str]]:
    """``{function name: names it mentions}`` plus ``{function: module}``.

    Top-level function names are unique across the package (pinned
    below), so the graph can ignore module scoping.
    """
    refs: dict[str, set[str]] = {}
    home: dict[str, str] = {}
    for module_name, tree in _module_sources().items():
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            home[node.name] = module_name
            refs[node.name] = {
                inner.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Name)
            }
    return refs, home


def test_top_level_function_names_are_unique_across_the_package() -> None:
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for module_name, tree in _module_sources().items():
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if node.name in seen:
                    clashes.append(
                        f"{node.name}: {seen[node.name]} vs {module_name}"
                    )
                seen[node.name] = module_name
    assert clashes == []


def test_every_render_helper_is_reachable_from_a_registered_section() -> None:
    refs, home = _function_reference_graph()
    reachable: set[str] = set()
    frontier = [entry.render.__name__ for entry in _all_declared_sections()]
    while frontier:
        name = frontier.pop()
        if name in reachable or name not in refs:
            continue
        reachable.add(name)
        frontier.extend(refs[name])

    orphans = sorted(
        f"{home[name]}.{name}"
        for name in refs
        if name.startswith("_render_") and name not in reachable
    )
    assert orphans == [], (
        "these renderers live in the sections package but no registered "
        "section reaches them — they render into nothing"
    )


def test_default_module_no_longer_defines_block_renderers() -> None:
    from kokoro_link.infrastructure.prompt import default as default_mod

    tree = ast.parse(
        pathlib.Path(default_mod.__file__).read_text(encoding="utf-8")
    )
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("_render_")
    ]
    assert defined == []


# --- registry behaviour ----------------------------------------------


def test_registering_the_same_name_overrides_the_earlier_section() -> None:
    first = PromptSection("timing", SECTION_ORDERS["timing"], lambda ctx: ["a"])
    second = PromptSection(
        "timing", SECTION_ORDERS["timing"], lambda ctx: ["b"]
    )
    registry = PromptSectionRegistry([first, second])

    assert len(registry) == 1
    assert registry.ordered()[0].render(None) == ["b"]


def test_assemble_joins_in_table_order_not_registration_order() -> None:
    registry = PromptSectionRegistry(
        [
            PromptSection("memory", SECTION_ORDERS["memory"], lambda c: ["m"]),
            PromptSection("timing", SECTION_ORDERS["timing"], lambda c: ["t"]),
        ]
    )

    assert assemble(None, registry=registry, resolvers=()) == "t\nm"


def test_default_registry_is_shared_and_stable() -> None:
    assert default_registry() is default_registry()


# --- resolvers -------------------------------------------------------


def test_a_live_scene_blanks_the_scripted_beat() -> None:
    rendered = {"story_scene": ["scene"], "today_scene": ["beat"]}
    resolve_scene_exclusivity(None, rendered)

    assert rendered["today_scene"] == []
    assert rendered["story_scene"] == ["scene"]


def test_an_empty_scene_leaves_the_scripted_beat_alone() -> None:
    rendered = {"story_scene": [], "today_scene": ["beat"]}
    resolve_scene_exclusivity(None, rendered)

    assert rendered["today_scene"] == ["beat"]


def test_a_material_digest_takes_over_its_five_sources() -> None:
    rendered = {"material_digest": ["digest"]} | {
        name: ["kept"] for name in DIGEST_SUPPRESSED_SECTIONS
    }
    resolve_material_digest_takeover(None, rendered)

    assert rendered["material_digest"] == ["digest"]
    assert all(rendered[name] == [] for name in DIGEST_SUPPRESSED_SECTIONS)


def test_no_digest_means_the_five_sources_stay() -> None:
    rendered = {"material_digest": []} | {
        name: ["kept"] for name in DIGEST_SUPPRESSED_SECTIONS
    }
    resolve_material_digest_takeover(None, rendered)

    assert all(rendered[name] == ["kept"] for name in DIGEST_SUPPRESSED_SECTIONS)


def _rails(**overrides: object) -> RailsContext:
    base: dict[str, object] = {
        "experiment_overlay": {},
        "content_tolerance": CONTENT_TOLERANCE_FRONTIER,
        "body_state_enabled": True,
        "subjective_time_enabled": True,
        "address_preference_enabled": True,
    }
    return RailsContext(**(base | overrides))  # type: ignore[arg-type]


class _RailsOnlyContext:
    """Enough of a context for the overlay resolver, and nothing more."""

    def __init__(self, rails: RailsContext) -> None:
        self.rails = rails


@pytest.mark.parametrize(
    ("rails", "expected_reflection", "expected_body"),
    [
        (_rails(), ["kept"], ["kept"]),
        (
            _rails(experiment_overlay={"self_reflection": "off"}),
            [],
            ["kept"],
        ),
        (_rails(experiment_overlay={"body_state": "off"}), ["kept"], []),
        (_rails(body_state_enabled=False), ["kept"], []),
    ],
)
def test_experiment_overlay_only_blanks_what_it_names(
    rails: RailsContext,
    expected_reflection: list[str],
    expected_body: list[str],
) -> None:
    rendered = {"self_reflection": ["kept"], "body_state": ["kept"]}
    resolve_experiment_overlay(_RailsOnlyContext(rails), rendered)

    assert rendered["self_reflection"] == expected_reflection
    assert rendered["body_state"] == expected_body


@pytest.mark.parametrize(
    ("rails", "expected"),
    [
        (_rails(), True),
        (_rails(subjective_time_enabled=False), False),
        (_rails(experiment_overlay={"subjective_time": "off"}), False),
    ],
)
def test_subjective_time_narrows_the_timing_block_instead_of_blanking_it(
    rails: RailsContext, expected: bool
) -> None:
    # Not a resolver: the variant changes one block's *content*, so it is
    # a context read the timing section makes for itself.
    assert rails.include_catchup_hint is expected
