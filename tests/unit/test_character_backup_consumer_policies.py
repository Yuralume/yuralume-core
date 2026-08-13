"""消費者政策軸釘住測試（CD0 驗收紅線）.

``test_character_backup_registry.py`` pins *what a character's data is*.
This suite pins *what each consumer does with it*, with the same
mechanism CB0 used — introspect the real artefacts (ORM metadata, the
export service's own source) rather than trusting a plan document:

1. **Every consumer has a stance on every registered table.** Resolution
   is total or the suite is red; there is no silent fallback. Deleting
   any single declaration — a table override, a classification default,
   a reset flag entry — turns something below red.

2. **"Rows survive" always carries a reason.** ``RETAIN`` / ``SKIP`` /
   ``CLEAR_REFERENCE`` are the stances a future reader is most likely to
   mistake for an oversight, so an empty reason is a failure.

3. **Reset is description, not design.** The declared flag → table sets
   are checked against the schema facts they claim (FK ``ondelete``
   modes), so a migration that changes a cascade turns this red instead
   of silently widening what "clear the chat log" means.

4. **The media harvest surface cannot drift from the exporter.** The
   registry's media-URL field list is compared **both ways** against the
   AST of ``CharacterBackupExportService._observe_record``.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

# Import every module that defines ORM tables so ``Base.metadata`` is
# complete before introspection (same trunk as the CB0 pin test).
from kokoro_link.infrastructure.persistence import (  # noqa: F401
    branching_drama_models as _branching_drama_models,
    character_backup_models as _character_backup_models,
    fusion_story_models as _fusion_story_models,
    rss_models as _rss_models,
)
from kokoro_link.infrastructure.persistence.models import Base

import pytest

from kokoro_link.application.services import (
    character_backup_export_service as export_module,
)
from kokoro_link.application.dto.character_backup import (
    BACKUP_TABLE_OVERRIDES,
    BACKUP_TABLE_RULES_BY_NAME,
    CHARACTER_BACKUP_TABLE_RULES,
    CHARACTER_MEDIA_URL_FIELDS,
    CHARACTER_STORAGE_PREFIXES,
    DELETE_CLASSIFICATION_DEFAULTS,
    DELETE_TABLE_OVERRIDES,
    LUMECARD_SCOPE,
    OPERATOR_SCOPED_MEDIA_PREFIXES,
    RESET_FLAG_EFFECTS,
    RESET_UNTOUCHED_REASON,
    SURVIVING_POLICIES,
    TABLE_POLICY_CONSUMERS,
    BackupClassification,
    DataConsumer,
    DeletePolicy,
    MediaUrlShape,
    PolicySource,
    ResetFlag,
    ResetPolicy,
    UnresolvedPolicyError,
    character_storage_prefixes,
    lumecard_excluded_columns,
    media_url_fields_by_table,
    policy_for,
    reset_primary_table_for_flag,
    reset_tables_for_flag,
)


_SRC_ROOT = Path(export_module.__file__).resolve().parents[3]
"""``src/`` — ``.../src/kokoro_link/application/services/x.py`` → 3 up."""


# ======================================================================
# Not-vacuous guards
# ======================================================================


def test_consumer_axis_is_not_vacuous() -> None:
    """Guard the guard: the axis must cover the known landscape."""
    assert len(CHARACTER_BACKUP_TABLE_RULES) >= 40
    assert {consumer.value for consumer in DataConsumer} >= {
        "backup", "delete", "reset", "lumecard",
    }
    assert set(TABLE_POLICY_CONSUMERS) == {
        DataConsumer.BACKUP, DataConsumer.DELETE, DataConsumer.RESET,
    }
    # ``.lumecard`` is documentary on purpose — see LUMECARD_SCOPE.
    assert DataConsumer.LUMECARD not in TABLE_POLICY_CONSUMERS
    assert len(DELETE_TABLE_OVERRIDES) >= 20
    assert len(RESET_FLAG_EFFECTS) == 4


# ======================================================================
# 1. Totality: consumer × table
# ======================================================================


def test_every_consumer_resolves_every_registered_table() -> None:
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        for consumer in TABLE_POLICY_CONSUMERS:
            resolved = policy_for(consumer, rule.table)
            assert resolved.table == rule.table
            assert resolved.consumer is consumer
            assert resolved.reason.strip(), (consumer, rule.table)


def test_surviving_policies_always_carry_a_reason() -> None:
    seen: set[object] = set()
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        for consumer in TABLE_POLICY_CONSUMERS:
            resolved = policy_for(consumer, rule.table)
            if resolved.policy in SURVIVING_POLICIES:
                seen.add(resolved.policy)
                assert len(resolved.reason.strip()) >= 8, (
                    f"{consumer.value}/{rule.table}: a 'rows survive' "
                    "stance needs a real reason, not a placeholder"
                )
    # Guard the guard: the sweep must actually hit surviving stances.
    assert {DeletePolicy.RETAIN, DeletePolicy.SKIP, ResetPolicy.SKIP} <= seen


def test_unresolvable_lookups_fail_loud() -> None:
    with pytest.raises(UnresolvedPolicyError):
        policy_for(DataConsumer.DELETE, "no_such_table")
    with pytest.raises(UnresolvedPolicyError):
        # ``.lumecard`` has no table axis at all.
        policy_for(DataConsumer.LUMECARD, "characters")


def test_no_phantom_overrides() -> None:
    """An override for an unregistered table is dead weight that reads
    as coverage."""
    for consumer, overrides in (
        (DataConsumer.BACKUP, BACKUP_TABLE_OVERRIDES),
        (DataConsumer.DELETE, DELETE_TABLE_OVERRIDES),
    ):
        unknown = set(overrides) - set(BACKUP_TABLE_RULES_BY_NAME)
        assert not unknown, (consumer, sorted(unknown))
    reset_unknown = {
        effect.table
        for effects in RESET_FLAG_EFFECTS.values()
        for effect in effects
    } - set(BACKUP_TABLE_RULES_BY_NAME)
    assert not reset_unknown, sorted(reset_unknown)


# ======================================================================
# 2. BACKUP — identity mapping
# ======================================================================


def test_backup_policy_is_the_classification_itself() -> None:
    assert not BACKUP_TABLE_OVERRIDES, (
        "an override here would mean the table's classification lies "
        "about what backup does with it"
    )
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        resolved = policy_for(DataConsumer.BACKUP, rule.table)
        assert resolved.policy is rule.classification, rule.table
        assert resolved.source is PolicySource.CLASSIFICATION_DEFAULT


# ======================================================================
# 3. DELETE — owner 拍板 D1 / D2
# ======================================================================


def test_cross_or_deployment_tables_must_each_declare_their_own_stance(
) -> None:
    """The provable red line: this classification has **no** delete
    default, so removing any one of its overrides makes resolution
    raise instead of silently falling back."""
    assert (
        BackupClassification.EXCLUDE_CROSS_OR_DEPLOYMENT
        not in DELETE_CLASSIFICATION_DEFAULTS
    )
    b_layer = [
        rule.table
        for rule in CHARACTER_BACKUP_TABLE_RULES
        if rule.classification
        is BackupClassification.EXCLUDE_CROSS_OR_DEPLOYMENT
    ]
    assert len(b_layer) >= 13
    for table in b_layer:
        resolved = policy_for(DataConsumer.DELETE, table)
        assert resolved.source is PolicySource.TABLE_OVERRIDE, table


def test_delete_pinned_decisions() -> None:
    """Spot-pin the calls that were argued for explicitly."""
    def verb(table: str) -> DeletePolicy:
        policy = policy_for(DataConsumer.DELETE, table).policy
        assert isinstance(policy, DeletePolicy)
        return policy

    # D1: billing / audit evidence outlives the character.
    for table in ("generation_usage_events", "account_runtime_events"):
        assert verb(table) is DeletePolicy.RETAIN, table

    # D2: co-authored works are kept verbatim, ids never rewritten.
    for table in (
        "fusion_stories", "fusion_story_beats", "fusion_story_versions",
        "branching_dramas", "branching_drama_nodes",
        "branching_drama_sessions",
    ):
        assert verb(table) is DeletePolicy.SKIP, table

    # Cross-character four tables: either side matches.
    for table in (
        "character_relationships", "character_peer_profiles",
        "character_encounters", "character_encounter_intents",
    ):
        assert verb(table) is DeletePolicy.PURGE_ANY_SIDE, table

    # Templates / series are installation-level shared assets.
    for table in ("arc_templates", "arc_series", "arc_series_members"):
        assert verb(table) is DeletePolicy.SKIP, table

    # Messaging: the account goes, its bindings ride the FK cascade.
    assert verb("messaging_accounts") is DeletePolicy.PURGE
    assert verb("channel_bindings") is DeletePolicy.PURGE_VIA_PARENT

    assert verb("character_event_inbox") is DeletePolicy.PURGE
    assert verb("characters") is DeletePolicy.PURGE
    # Runtime default really is purge.
    assert verb("turn_records") is DeletePolicy.PURGE
    assert verb("background_jobs") is DeletePolicy.PURGE


def test_delete_purge_targets_actually_have_a_character_column() -> None:
    """A ``PURGE`` stance promises ``WHERE character_id = :id`` is a
    statement that can be written. Tables without such a column must say
    ``PURGE_VIA_PARENT`` / ``PURGE_ANY_SIDE`` / ``SKIP`` instead —
    otherwise the eventual consumer emits SQL that cannot compile."""
    checked = 0
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        resolved = policy_for(DataConsumer.DELETE, rule.table)
        if resolved.policy is not DeletePolicy.PURGE:
            continue
        if rule.table == "characters":
            continue  # the root row itself, keyed by ``id``
        columns = set(Base.metadata.tables[rule.table].columns.keys())
        assert "character_id" in columns, (
            f"{rule.table} is declared PURGE but has no character_id "
            f"column: {sorted(columns)}"
        )
        checked += 1
    assert checked >= 25


def test_delete_any_side_targets_have_two_character_columns() -> None:
    ref = re.compile(r"character.*_id$")
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        resolved = policy_for(DataConsumer.DELETE, rule.table)
        if resolved.policy is not DeletePolicy.PURGE_ANY_SIDE:
            continue
        columns = [
            column.name
            for column in Base.metadata.tables[rule.table].columns
            if ref.search(column.name)
        ]
        assert len(columns) == 2, (rule.table, columns)


def test_delete_via_parent_targets_have_a_cascading_parent_fk() -> None:
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        resolved = policy_for(DataConsumer.DELETE, rule.table)
        if resolved.policy is not DeletePolicy.PURGE_VIA_PARENT:
            continue
        table = Base.metadata.tables[rule.table]
        assert "character_id" not in table.columns, (
            f"{rule.table} has its own character_id — declare PURGE"
        )
        cascades = [
            fk.target_fullname
            for fk in table.foreign_keys
            if fk.ondelete == "CASCADE"
        ]
        assert cascades, (
            f"{rule.table} is declared PURGE_VIA_PARENT but has no "
            "ON DELETE CASCADE parent FK"
        )


# ======================================================================
# 4. RESET — 現行行為的如實記載
# ======================================================================


def test_reset_flag_table_sets_are_pinned() -> None:
    """The exact reach of each flag of ``reset_character_data``."""
    assert reset_tables_for_flag(ResetFlag.MEMORIES) == frozenset(
        {"memory_items"},
    )
    assert reset_tables_for_flag(ResetFlag.CONVERSATIONS) == frozenset({
        "conversations",
        "messages",
        "turn_journals",
        "pending_follow_ups",
        "story_scene_sessions",
        "channel_bindings",
    })
    assert reset_tables_for_flag(ResetFlag.STATE_HISTORY) == frozenset(
        {"state_snapshots"},
    )
    assert reset_tables_for_flag(ResetFlag.OPERATOR_PERSONA) == frozenset(
        {"operator_profile_fields"},
    )


def test_reset_primary_table_per_flag_is_pinned() -> None:
    """The table whose row count ``reset_character_data`` reports.

    ``reset_primary_table_for_flag`` is a *derivation*: it reads the
    **first** entry declared for the flag in ``RESET_FLAG_EFFECTS``. That
    makes declaration order load-bearing on an API contract — the reset
    endpoint returns this table's count per flag, so reordering a tuple
    (adding ``messages`` above ``conversations``, say) would silently
    change the number every caller reads, with no other symptom. These
    assertions are the pin that turns such a reorder red.
    """
    assert reset_primary_table_for_flag(ResetFlag.MEMORIES) == "memory_items"
    assert reset_primary_table_for_flag(
        ResetFlag.CONVERSATIONS,
    ) == "conversations"
    assert reset_primary_table_for_flag(
        ResetFlag.STATE_HISTORY,
    ) == "state_snapshots"
    assert reset_primary_table_for_flag(
        ResetFlag.OPERATOR_PERSONA,
    ) == "operator_profile_fields"
    # Every flag has one, and it is always inside that flag's own reach.
    for flag in ResetFlag:
        assert reset_primary_table_for_flag(flag) in reset_tables_for_flag(flag)


def test_reset_conversation_cascade_verbs_are_pinned() -> None:
    """The invisible half: what the FK cascade takes that the repository
    call does not mention."""
    def verb(table: str) -> ResetPolicy:
        resolved = policy_for(DataConsumer.RESET, table)
        assert resolved.reset_flag is ResetFlag.CONVERSATIONS, table
        assert isinstance(resolved.policy, ResetPolicy)
        return resolved.policy

    assert verb("conversations") is ResetPolicy.PURGE
    assert verb("messages") is ResetPolicy.PURGE
    for table in (
        "turn_journals", "pending_follow_ups", "story_scene_sessions",
    ):
        assert verb(table) is ResetPolicy.PURGE_VIA_PARENT, table
    assert verb("channel_bindings") is ResetPolicy.CLEAR_REFERENCE


def test_reset_touches_nothing_else() -> None:
    touched = {
        effect.table
        for effects in RESET_FLAG_EFFECTS.values()
        for effect in effects
    }
    untouched = 0
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        if rule.table in touched:
            continue
        resolved = policy_for(DataConsumer.RESET, rule.table)
        assert resolved.policy is ResetPolicy.SKIP, rule.table
        assert resolved.source is PolicySource.RESET_UNTOUCHED
        assert resolved.reason == RESET_UNTOUCHED_REASON
        assert resolved.reset_flag is None
        untouched += 1
    assert untouched >= 40
    # Reset must never reach the character row itself.
    assert "characters" not in touched


def test_reset_cascade_claims_match_the_schema() -> None:
    """The declared cascade verbs are checked against the real FK modes,
    so a migration that flips one turns this red instead of silently
    changing what a reset flag means."""
    expected_ondelete = {
        ResetPolicy.PURGE_VIA_PARENT: "CASCADE",
        ResetPolicy.CLEAR_REFERENCE: "SET NULL",
    }
    for effect in RESET_FLAG_EFFECTS[ResetFlag.CONVERSATIONS]:
        want = expected_ondelete.get(effect.policy)
        if want is None:
            continue
        table = Base.metadata.tables[effect.table]
        modes = {
            fk.ondelete
            for fk in table.foreign_keys
            if fk.target_fullname == "conversations.id"
        }
        assert modes == {want}, (effect.table, modes, want)

    # The explicit-delete claims: those two tables really are what the
    # repositories select on.
    for table, column in (
        ("memory_items", "character_id"),
        ("state_snapshots", "character_id"),
        ("operator_profile_fields", "character_id"),
        ("conversations", "character_id"),
    ):
        assert column in Base.metadata.tables[table].columns, table


def test_reset_does_not_widen_the_operator_persona_family() -> None:
    """``operator_persona=True`` clears exactly one table today. The
    sibling persona tables staying untouched is a fact, not a bug to fix
    inside a declaration ticket."""
    for table in (
        "operator_address_preferences",
        "operator_address_change_log",
        "persona_curiosity_attempts",
        "character_operator_relationship_seeds",
    ):
        assert (
            policy_for(DataConsumer.RESET, table).policy is ResetPolicy.SKIP
        ), table


# ======================================================================
# 5. LUMECARD — documentary, derived from the column-level mechanism
# ======================================================================


def test_lumecard_scope_is_derived_not_restated() -> None:
    excluded = lumecard_excluded_columns()
    characters_rule = BACKUP_TABLE_RULES_BY_NAME["characters"]
    assert excluded <= set(characters_rule.excluded_columns)
    assert {
        "voice_profile_json",
        "loras_json",
        "feature_models_json",
        "feature_image_profiles_json",
        "feature_video_profiles_json",
        "subscription_locked",
        "origin_official_card_id",
    } == set(excluded)
    for table in LUMECARD_SCOPE.carried_tables:
        assert table in BACKUP_TABLE_RULES_BY_NAME, table
    assert LUMECARD_SCOPE.column_scope_table == "characters"
    assert LUMECARD_SCOPE.reason.strip()


# ======================================================================
# 6. Storage prefixes
# ======================================================================


def test_storage_prefixes_are_pinned_and_formattable() -> None:
    templates = {entry.template for entry in CHARACTER_STORAGE_PREFIXES}
    assert templates == {
        "characters/{character_id}/",
        "feed/{character_id}/",
        "tts/{character_id}/",
    }
    assert character_storage_prefixes("c-1") == (
        "characters/c-1/", "feed/c-1/", "tts/c-1/",
    )
    for entry in CHARACTER_STORAGE_PREFIXES:
        assert entry.reason.strip(), entry.template
        assert entry.writers, entry.template


def test_storage_prefix_writers_still_write_that_prefix() -> None:
    """Every declared writer must actually build a key under the prefix
    it is cited for — a citation that stopped being true is worse than
    no citation."""
    for entry in CHARACTER_STORAGE_PREFIXES:
        head = entry.template.split("/", 1)[0]
        pattern = re.compile(rf'{re.escape(head)}/\{{[^}}]+\}}/')
        for writer in entry.writers:
            path = _SRC_ROOT / "kokoro_link" / writer.split("::", 1)[0]
            assert path.is_file(), writer
            source = path.read_text(encoding="utf-8")
            assert pattern.search(source), writer


def test_operator_scoped_prefix_note_still_holds() -> None:
    """Chat attachments live under the *operator's* tree, so no
    per-character prefix sweep reaches them. If that ever changes, the
    argument for keeping the media-field list complete changes with it."""
    assert OPERATOR_SCOPED_MEDIA_PREFIXES == (
        "users/{user_id}/chat-uploads/",
    )
    source = (
        _SRC_ROOT / "kokoro_link" / "api" / "routes" / "chat.py"
    ).read_text(encoding="utf-8")
    assert '"chat-uploads"' in source
    assert re.search(r'users/\{[^}]+\}/', source)
    # And it is *not* one of the character-owned prefixes.
    heads = {
        entry.template.split("/", 1)[0]
        for entry in CHARACTER_STORAGE_PREFIXES
    }
    assert "users" not in heads


# ======================================================================
# 7. Media URL fields ↔ the exporter's harvest surface
# ======================================================================


#: Record attributes ``_observe_record`` reads that are **not** media —
#: NSFW write-time mode facts. Listed here so the export→registry
#: direction below can subtract them; adding a new non-media fact to the
#: exporter is a deliberate act that has to extend this list with a
#: reason rather than silently loosening the pin.
_NON_MEDIA_OBSERVED_FIELDS: dict[tuple[str, str], str] = {
    ("messages", "content_mode"): "NSFW 折疊判斷（寫入時態事實）",
    ("messages", "safe_summary"): "NSFW 折疊：有安全摘要就替換而非丟棄",
    ("memory_items", "tags"): "NSFW 記憶標記",
    ("operator_profile_fields", "content_mode"): "NSFW 畫像欄標記",
    ("pending_follow_ups", "messages_json"): "NSFW 待辦訊息標記",
}


def _observed_record_fields() -> set[tuple[str, str]]:
    """``(table, record field)`` pairs ``_observe_record`` actually reads.

    Parsed from source rather than executed: the point is to catch a new
    ``stats.media_urls.append`` branch that nobody registered, and a
    branch only reachable with a fixture that does not exist yet would
    never be caught by running the thing.
    """
    module_ast = ast.parse(
        Path(export_module.__file__).read_text(encoding="utf-8"),
    )
    func: ast.FunctionDef | None = None
    for node in ast.walk(module_ast):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_observe_record"
        ):
            func = node
            break
    assert func is not None, "_observe_record disappeared from the exporter"

    found: set[tuple[str, str]] = set()

    def record_fields(body: list[ast.stmt]) -> set[str]:
        names: set[str] = set()
        for statement in body:
            for node in ast.walk(statement):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "record"
                ):
                    names.add(node.attr)
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "record"
                    and isinstance(node.args[1], ast.Constant)
                ):
                    names.add(str(node.args[1].value))
        return names

    def walk_chain(node: ast.If) -> None:
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "table"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
        ):
            table = str(test.comparators[0].value)
            # Only the branch body, not the elif chain hanging off it.
            for name in record_fields(node.body):
                found.add((table, name))
        for orelse in node.orelse:
            if isinstance(orelse, ast.If):
                walk_chain(orelse)

    for statement in func.body:
        if isinstance(statement, ast.If):
            walk_chain(statement)
    return found


def test_observed_field_scan_is_not_vacuous() -> None:
    observed = _observed_record_fields()
    assert len(observed) >= 8, observed
    for expected in (
        ("characters", "image_urls"),
        ("messages", "attachments_json"),
        ("feed_posts", "image_url"),
        ("character_album_items", "url"),
    ):
        assert expected in observed, expected


def test_media_url_fields_match_the_exporter_both_ways() -> None:
    """The registry's harvest list and the exporter's branches are the
    same set. A media column added to one and not the other is red —
    which is the whole point: delete reads this list to know what to
    reap, and a column export collects but delete never hears about is a
    file that outlives the character."""
    declared = {
        (field.table, field.column) for field in CHARACTER_MEDIA_URL_FIELDS
    }
    observed = _observed_record_fields() - set(_NON_MEDIA_OBSERVED_FIELDS)
    assert observed == declared, (
        "media harvest drift between "
        "registry.CHARACTER_MEDIA_URL_FIELDS and "
        "CharacterBackupExportService._observe_record — "
        f"only in exporter: {sorted(observed - declared)}; "
        f"only in registry: {sorted(declared - observed)}"
    )
    for reason in _NON_MEDIA_OBSERVED_FIELDS.values():
        assert reason.strip()


def test_media_url_fields_exist_on_their_tables_and_dtos() -> None:
    for field in CHARACTER_MEDIA_URL_FIELDS:
        rule = BACKUP_TABLE_RULES_BY_NAME[field.table]
        assert rule.classification is BackupClassification.CARRY, field
        columns = set(Base.metadata.tables[field.table].columns.keys())
        assert field.column in columns, field
        assert field.column not in rule.excluded_columns, field
        assert rule.dto is not None
        assert field.column in rule.dto.model_fields, field
        assert isinstance(field.shape, MediaUrlShape)
        assert field.reason.strip(), field


def test_media_url_fields_grouped_view_is_consistent() -> None:
    grouped = media_url_fields_by_table()
    assert set(grouped) == {
        field.table for field in CHARACTER_MEDIA_URL_FIELDS
    }
    assert sum(len(fields) for fields in grouped.values()) == len(
        CHARACTER_MEDIA_URL_FIELDS,
    )
    assert {field.column for field in grouped["feed_posts"]} == {
        "image_url", "video_url",
    }


def test_exporter_still_defines_observe_record_as_a_method() -> None:
    """Anchor for the AST scan above: if ``_observe_record`` is renamed
    or moved, the scan's assertion fires — but this makes the intent
    obvious in the failure output."""
    assert inspect.isfunction(
        export_module.CharacterBackupExportService._observe_record,
    )
