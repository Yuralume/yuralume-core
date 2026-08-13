"""角色資料邊界 registry 釘住測試（CB0 驗收紅線）.

Two invariants, enforced by ORM metadata introspection so nothing
depends on humans remembering the plan document:

1. **Every character-referencing table is registered.** Any table with a
   ``character_id`` column — or any character-reference column such as
   ``peer_character_id`` / ``from_character_id`` / ``character_a_id`` /
   ``*_character_ids_json`` — that is missing from
   ``CHARACTER_BACKUP_TABLE_RULES`` turns this suite red. A new feature
   table must take a stance (carry / exclude, with a reason) before it
   can land.

2. **Carried DTOs cannot drift from the schema.** For every CARRY rule,
   ``dto fields == table columns − excluded columns`` exactly — adding a
   column to a carried table without extending its DTO (or explicitly
   excluding the column with a reason) is red too.
"""

from __future__ import annotations

import re

# Import every module that defines ORM tables so ``Base.metadata`` is
# complete before introspection. ``models`` is the trunk; the split
# model files register on import.
from kokoro_link.infrastructure.persistence import (  # noqa: F401
    branching_drama_models as _branching_drama_models,
    character_backup_models as _character_backup_models,
    fusion_story_models as _fusion_story_models,
    rss_models as _rss_models,
)
from kokoro_link.infrastructure.persistence.models import Base

from kokoro_link.application.dto.character_backup import (
    BACKUP_TABLE_RULES_BY_NAME,
    CHARACTER_BACKUP_TABLE_RULES,
    BackupClassification,
    carried_table_rules,
)


_CHARACTER_REF_COLUMN = re.compile(
    r"character.*(_id|_ids_json)$",
)
"""Matches every character-reference column shape in the schema today:
``character_id``, ``peer_character_id``, ``from_character_id`` /
``to_character_id``, ``character_a_id`` / ``character_b_id``,
``character_ids_json`` / ``focus_character_ids_json`` /
``appearing_character_ids_json`` / ``target_character_ids_json``."""


def _character_ref_tables() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for table in Base.metadata.tables.values():
        refs = [
            column.name
            for column in table.columns
            if _CHARACTER_REF_COLUMN.search(column.name)
        ]
        if refs:
            found[table.name] = refs
    return found


def test_metadata_scan_is_not_vacuous() -> None:
    """Guard the guard: the scan must see the known landscape."""
    tables = _character_ref_tables()
    # A sample across the four model modules.
    for expected in (
        "conversations",            # models.py, character_id
        "character_peer_profiles",  # peer_character_id
        "character_relationships",  # from/to_character_id
        "character_encounters",     # character_a_id / character_b_id
        "fusion_stories",           # character_ids_json
        "branching_drama_nodes",    # appearing_character_ids_json
        "arc_templates",            # target_character_ids_json
        "character_event_inbox",    # rss_models.py
        "character_backup_jobs",    # this series' own job table
    ):
        assert expected in tables, expected
    assert len(tables) >= 40


def test_every_character_referencing_table_is_registered() -> None:
    unregistered = {
        name: columns
        for name, columns in _character_ref_tables().items()
        if name not in BACKUP_TABLE_RULES_BY_NAME
    }
    assert not unregistered, (
        "Tables reference characters but are missing from the character "
        "backup boundary registry "
        "(kokoro_link/application/dto/character_backup/registry.py). "
        "Classify each per CHARACTER_FULL_BACKUP_PLAN §3 — carry with a "
        f"DTO, or exclude with a reason: {unregistered!r}"
    )


def test_registered_tables_exist_in_metadata() -> None:
    """No phantom entries — a renamed / dropped table must leave the
    registry too."""
    missing = [
        rule.table
        for rule in CHARACTER_BACKUP_TABLE_RULES
        if rule.table not in Base.metadata.tables
    ]
    assert not missing, missing


def test_registry_entries_carry_reasons_and_consistent_shape() -> None:
    seen: set[str] = set()
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        assert rule.table not in seen, f"duplicate rule: {rule.table}"
        seen.add(rule.table)
        assert rule.reason.strip(), f"{rule.table} has no reason"
        if rule.classification is BackupClassification.CARRY:
            assert rule.dto is not None, f"{rule.table} CARRY needs a DTO"
        else:
            assert rule.dto is None, (
                f"{rule.table} is not CARRY but declares a DTO"
            )
            assert not rule.excluded_columns, (
                f"{rule.table} excluded_columns only makes sense on CARRY"
            )
        for column, exclusion in rule.excluded_columns.items():
            assert exclusion.reason.strip(), (
                f"{rule.table}.{column} exclusion has no reason"
            )


def test_carry_dto_fields_match_table_columns_exactly() -> None:
    for rule in carried_table_rules():
        table = Base.metadata.tables[rule.table]
        columns = set(table.columns.keys())
        excluded = set(rule.excluded_columns)
        assert excluded <= columns, (
            f"{rule.table}: excluded columns not on the table: "
            f"{sorted(excluded - columns)}"
        )
        assert rule.dto is not None
        dto_fields = set(rule.dto.model_fields)
        expected = columns - excluded
        assert dto_fields == expected, (
            f"{rule.table}: DTO/schema drift — "
            f"missing from DTO: {sorted(expected - dto_fields)}; "
            f"extra in DTO: {sorted(dto_fields - expected)}"
        )


def test_plan_pinned_classifications() -> None:
    """Spot-pin the §3 decisions that were argued for explicitly."""
    by_name = BACKUP_TABLE_RULES_BY_NAME

    # §3.1 row filter: only character-private seeds travel.
    assert by_name["story_seeds"].row_filter == "character_id IS NOT NULL"

    # §3.2 column-level recompute: the two vectors never travel.
    memory_excluded = by_name["memory_items"].excluded_columns
    assert set(memory_excluded) == {"embedding", "tags_embedding"}
    for exclusion in memory_excluded.values():
        assert exclusion.classification is BackupClassification.RECOMPUTE

    # §3.4 cross-character four tables.
    for table in (
        "character_relationships",
        "character_peer_profiles",
        "character_encounters",
        "character_encounter_intents",
    ):
        assert (
            by_name[table].classification
            is BackupClassification.EXCLUDE_CROSS_OR_DEPLOYMENT
        ), table

    # §3.3 the loud ones.
    for table in ("turn_records", "generation_usage_events",
                  "background_jobs", "character_backup_jobs"):
        assert (
            by_name[table].classification
            is BackupClassification.EXCLUDE_RUNTIME
        ), table

    # Arc templates / series ride the .lumecard YAML mechanism.
    for table in ("arc_templates", "arc_series", "arc_series_members"):
        assert (
            by_name[table].classification
            is BackupClassification.CARRY_YAML
        ), table

    # B-layer never leaves the deployment (same line as .lumecard).
    character_excluded = by_name["characters"].excluded_columns
    for column in (
        "voice_profile_json",
        "loras_json",
        "feature_models_json",
        "feature_image_profiles_json",
        "feature_video_profiles_json",
    ):
        assert column in character_excluded, column


def test_carry_order_is_parent_before_child() -> None:
    """The registry's CARRY order doubles as the restore landing order."""
    order = [rule.table for rule in carried_table_rules()]
    position = {name: index for index, name in enumerate(order)}

    assert position["characters"] == 0
    for parent, child in (
        ("conversations", "messages"),
        ("conversations", "turn_journals"),
        ("conversations", "pending_follow_ups"),
        ("conversations", "story_scene_sessions"),
        ("daily_schedules", "schedule_activities"),
        ("story_arcs", "story_arc_beats"),
        ("story_seeds", "story_events"),
        ("story_arc_beats", "story_events"),
        ("feed_posts", "feed_reactions"),
        ("feed_posts", "feed_comments"),
    ):
        assert position[parent] < position[child], (parent, child)

    # Declared parent links agree with the ordering.
    for rule in carried_table_rules():
        if rule.parent_table is not None:
            assert position[rule.parent_table] < position[rule.table], (
                rule.table
            )
