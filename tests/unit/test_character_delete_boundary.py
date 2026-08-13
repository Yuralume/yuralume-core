"""角色刪除邊界的零孤兒釘住測試（CD2 驗收紅線）.

The point of this suite is that **it contains no table list**. Every
assertion set is derived from the CD0 registry plus the ``DELETE``
consumer policy, exactly like the engine under test — so a table that
lands tomorrow is seeded, deleted and asserted here the moment CB0's pin
test forces it to pick a classification, without anyone editing this
file.

The rows are seeded by ORM metadata introspection too: for each
registered table we build one row filling every NOT NULL column with a
type-appropriate value, resolving NOT NULL foreign keys by recursively
seeding the parent. That is what lets the sweep be proved against the
real schema instead of against a hand-picked sample of it.

Four invariants are pinned:

1. **Zero orphans** — after ``delete_character`` no row matching the
   character survives in any ``PURGE`` / ``PURGE_ANY_SIDE`` table, and
   the ``PURGE_VIA_PARENT`` children are gone too. **Every case runs
   twice: with and without ``PRAGMA foreign_keys``.** Production SQLite
   (self-host) does not set the pragma — ``persistence/engine.py`` never
   has — so a suite that only ran with it on was pinning a cascade that
   real deployments do not perform, and the ``PURGE_VIA_PARENT`` children
   survived every self-host delete while this file stayed green. Zero
   orphans must hold in both rigs, which is only possible if the eraser
   states those deletes itself.
2. **D1 evidence survives** — ``RETAIN`` rows (billing / audit ledgers)
   are still there.
3. **D2 co-authored works are untouched** — the ``fusion_stories`` row
   still exists and its ``character_ids_json`` is byte-identical.
4. **Storage is swept but never fatal** — DB-tracked keys are deleted
   one by one (including the operator-scoped chat upload no prefix sweep
   can reach), the three owned prefixes are purged, and a storage
   adapter that fails outright — or that has no purge capability at all —
   leaves the database result unchanged.
5. **The sweep is contained** — the character's rows hold URLs pointing
   at a *peer character's* object and at *another operator's* chat
   upload (exactly what an attacker gets by pasting a victim's URL into
   their own character). Both survive the delete: media URL columns are
   operator-controlled, so the harvest must not be usable as a
   cross-tenant delete primitive.
6. **Containment is built from the key the writer actually wrote** — the
   chat-upload prefix folds the owner id through the same sanitiser
   ``api/routes/chat.py`` uses. A hosted operator id (``cloud:{account}``)
   loses its colon on the way into the key, so a boundary built from the
   raw DB id matches nothing and skips every hosted attachment silently.
   Pinned in the last section, against a fake database eraser rather than
   the seeded world — the seam under test is the prefix arithmetic, not
   the SQL.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import uuid
from datetime import date, datetime, timezone
from types import MappingProxyType

import pytest
import pytest_asyncio
from sqlalchemy import event, func, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

# Register every ORM table before ``create_all`` (registry pin-test trick).
from kokoro_link.infrastructure.persistence import (  # noqa: F401
    branching_drama_models as _branching_drama_models,
    character_backup_models as _character_backup_models,
    fusion_story_models as _fusion_story_models,
    rss_models as _rss_models,
)

from kokoro_link.application.dto.character_backup import (
    CHARACTER_BACKUP_TABLE_RULES,
    DataConsumer,
    DeletePolicy,
    character_storage_prefixes,
    policy_for,
)
from kokoro_link.application.services.character_data_erasure import (
    CHARACTER_ERASURE_REPOSITORY_SLOTS,
    CharacterDataErasureService,
    DatabaseErasureResult,
    RepositoryCharacterDatabaseEraser,
    UnknownErasureRepositorySlotError,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import Base
from kokoro_link.infrastructure.persistence.sa_character_backup_export_reader import (
    character_predicate,
)
from kokoro_link.infrastructure.persistence.sa_character_data_eraser import (
    SACharacterDataEraser,
    any_side_character_columns,
    any_side_character_predicate,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

OWNER = "owner-1"
VICTIM_OWNER = "owner-2"
TARGET_ID = "char-target"
PEER_ID = "char-peer"
THIRD_ID = "char-third"

CHAT_UPLOAD_KEY = f"users/{OWNER}/chat-uploads/attachment.png"
AVATAR_KEY = f"characters/{TARGET_ID}/avatar.png"
ALBUM_KEY = f"characters/{TARGET_ID}/album/shot.png"
FEED_KEY = f"feed/{TARGET_ID}/post.png"
TTS_KEY = f"tts/{TARGET_ID}/deadbeef.mp3"
PEER_KEEP_KEY = f"characters/{PEER_ID}/avatar.png"
VICTIM_UPLOAD_KEY = f"users/{VICTIM_OWNER}/chat-uploads/private.png"
"""Objects the deleted character's rows *point at* but does not own —
seeded into ``image_urls`` below to reproduce the harvest-as-weapon
path."""


def _url(object_key: str) -> str:
    return f"/uploads/{object_key}"


# ======================================================================
# Generic, metadata-driven row seeding
# ======================================================================


def _dummy_value(column):
    """A type-appropriate placeholder for one NOT NULL column."""
    try:
        py = column.type.python_type
    except (NotImplementedError, AttributeError):
        return ""
    if py is bool:
        return False
    if py is int:
        return 0
    if py is float:
        return 0.0
    if py is datetime:
        return datetime.now(timezone.utc)
    if py is date:
        return date.today()
    if py is bytes:
        return b""
    if py is dict:
        return {}
    if py is list:
        return []
    if py is str:
        if column.primary_key:
            return uuid.uuid4().hex
        if column.name.endswith("_json"):
            return "[]"
        return "seed"
    return None


_CHECK_IN_CLAUSE = re.compile(r"(\w+)\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_CHECK_EQ_CLAUSE = re.compile(r"(\w+)\s*=\s*'([^']*)'")


def _enumerated_columns(table) -> dict[str, str]:
    """Columns pinned by a CHECK to a literal set, → one legal value.

    Placeholder strings would otherwise trip the state-machine CHECKs the
    schema puts on ``channel`` / ``status`` style columns.
    """
    allowed: dict[str, str] = {}
    for constraint in table.constraints:
        sqltext = getattr(constraint, "sqltext", None)
        if sqltext is None:
            continue
        text_form = str(sqltext)
        for column_name, values in _CHECK_IN_CLAUSE.findall(text_form):
            literals = re.findall(r"'([^']*)'", values)
            if literals and column_name in table.c:
                allowed[column_name] = literals[0]
        for column_name, literal in _CHECK_EQ_CLAUSE.findall(text_form):
            if column_name in table.c:
                allowed.setdefault(column_name, literal)
    return allowed


class _Seeder:
    """Inserts one metadata-derived row per table, parents first."""

    def __init__(self, connection) -> None:
        self._connection = connection
        self.ids: dict[str, object] = {}

    async def ensure(self, table_name: str) -> object:
        if table_name in self.ids:
            return self.ids[table_name]
        return await self.seed(table_name)

    async def seed(self, table_name: str, **overrides) -> object:
        table = Base.metadata.tables[table_name]
        enumerated = _enumerated_columns(table)
        values: dict[str, object] = {}
        for column in table.columns:
            if column.name in overrides:
                continue
            if column.nullable:
                continue
            if column.default is not None or column.server_default is not None:
                continue
            parent = _foreign_parent(column)
            if parent is not None:
                values[column.name] = await self.ensure(parent)
                continue
            if column.name in enumerated:
                values[column.name] = enumerated[column.name]
                continue
            values[column.name] = _dummy_value(column)
        values.update(overrides)
        await self._connection.execute(insert(table).values(**values))
        primary = next(iter(table.primary_key.columns))
        row_id = values.get(primary.name)
        self.ids.setdefault(table_name, row_id)
        return row_id


def _foreign_parent(column) -> str | None:
    for foreign_key in column.foreign_keys:
        return foreign_key.column.table.name
    return None


_CHARACTER_COLUMN = re.compile(r"character.*_id$")


def _character_columns(table) -> tuple[str, ...]:
    """Scalar character-reference columns, by the registry's own shape."""
    return tuple(
        column.name
        for column in table.columns
        if _CHARACTER_COLUMN.search(column.name)
    )


# ======================================================================
# Fixture
# ======================================================================


@pytest_asyncio.fixture(
    params=[False, True], ids=["no-fk-pragma", "fk-pragma"],
)
async def world(request):  # noqa: ANN201
    """The same world twice, with FK enforcement off and on.

    ``no-fk-pragma`` is the *production* self-host shape:
    ``persistence/engine.py`` never issues ``PRAGMA foreign_keys=ON``, so
    a SQLite deployment performs no cascade at all. Anything this suite
    proves must therefore be proved without it; the ``fk-pragma`` run is
    the PostgreSQL-like rig kept alongside so the eraser's own statements
    are shown to be idempotent with the cascade rather than duplicated by
    it.
    """
    fk_enforced = request.param
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    if fk_enforced:
        @event.listens_for(engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # noqa: ANN202
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        seeded = await _seed_world(conn)
    session_factory = build_session_factory(engine)
    storage = InMemoryObjectStorage()
    for key in (
        AVATAR_KEY, ALBUM_KEY, FEED_KEY, TTS_KEY, CHAT_UPLOAD_KEY,
        PEER_KEEP_KEY, VICTIM_UPLOAD_KEY,
    ):
        await storage.put_bytes(
            object_key=key, content=b"bytes", content_type="image/png",
        )
    try:
        yield _World(engine, session_factory, storage, seeded, fk_enforced)
    finally:
        await engine.dispose()


class _World:
    def __init__(
        self, engine, session_factory, storage, seeded, fk_enforced,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.storage = storage
        self.seeded = seeded
        self.fk_enforced = fk_enforced

    def service(self, storage=None) -> CharacterService:
        """``CharacterService`` on the registry-driven erasure engine."""
        erasure = CharacterDataErasureService(
            database_eraser=SACharacterDataEraser(self.session_factory),
            object_storage=self.storage if storage is None else storage,
        )
        return CharacterService(
            SACharacterRepository(self.session_factory),
            character_data_erasure=erasure,
        )

    async def count(self, stmt) -> int:
        async with self.session_factory() as session:
            return int((await session.execute(stmt)).scalar_one() or 0)


async def _seed_world(conn) -> dict[str, object]:
    """One fully-populated character, plus a peer and a bystander."""
    seeder = _Seeder(conn)
    # ``characters.user_id`` is a real FK to the operator profile.
    await seeder.seed("operator_profiles", id=OWNER)
    seeder.ids["operator_profiles"] = OWNER
    for character_id in (TARGET_ID, PEER_ID, THIRD_ID):
        await seeder.seed(
            "characters",
            id=character_id,
            user_id=OWNER,
            name=f"name-{character_id}",
            image_urls="[]",
        )
        seeder.ids["characters"] = character_id
    # Every FK to ``characters`` resolves to the target from here on.
    seeder.ids["characters"] = TARGET_ID

    for rule in CHARACTER_BACKUP_TABLE_RULES:
        if rule.table == "characters":
            continue
        policy = policy_for(DataConsumer.DELETE, rule.table).policy
        table = Base.metadata.tables[rule.table]
        if policy is DeletePolicy.PURGE_ANY_SIDE:
            columns = [c.name for c in any_side_character_columns(table)]
            first, second = columns[0], columns[1]
            # Both sides, plus a row between two other characters that
            # must survive: a two-sided predicate that deletes too much
            # is as wrong as one that deletes too little.
            await seeder.seed(rule.table, **{first: TARGET_ID, second: PEER_ID})
            await seeder.seed(rule.table, **{first: PEER_ID, second: TARGET_ID})
            await seeder.seed(rule.table, **{first: PEER_ID, second: THIRD_ID})
            continue
        if policy in (DeletePolicy.PURGE, DeletePolicy.PURGE_VIA_PARENT):
            overrides = {
                name: TARGET_ID for name in _character_columns(table)
            }
            await seeder.seed(rule.table, **overrides)
            continue
        if policy is DeletePolicy.RETAIN:
            overrides = {
                name: TARGET_ID for name in _character_columns(table)
            }
            await seeder.seed(rule.table, **overrides)
            continue

    # D2: a co-authored work naming the target, and the global story seed
    # pool the row filter must leave alone.
    fusion_ids = json.dumps([TARGET_ID, PEER_ID])
    fusion_id = await seeder.seed(
        "fusion_stories", character_ids_json=fusion_ids,
    )
    global_seed_id = await seeder.seed("story_seeds", character_id=None)

    # Media the harvest has to find, in the columns the registry names.
    # ``image_urls`` is stored verbatim from operator input, so it also
    # carries the two hostile references the containment filter must
    # refuse to act on.
    await conn.execute(
        Base.metadata.tables["characters"].update().where(
            Base.metadata.tables["characters"].c.id == TARGET_ID,
        ).values(image_urls=json.dumps([
            _url(AVATAR_KEY), _url(PEER_KEEP_KEY), _url(VICTIM_UPLOAD_KEY),
        ])),
    )
    await conn.execute(
        Base.metadata.tables["messages"].update().values(
            attachments_json=json.dumps([{"url": _url(CHAT_UPLOAD_KEY)}]),
        ),
    )
    await conn.execute(
        Base.metadata.tables["feed_posts"].update().values(
            image_url=_url(FEED_KEY),
        ),
    )
    await conn.execute(
        Base.metadata.tables["character_album_items"].update().values(
            url=_url(ALBUM_KEY),
        ),
    )
    return {
        "fusion_id": fusion_id,
        "fusion_ids_json": fusion_ids,
        "global_seed_id": global_seed_id,
        "row_ids": dict(seeder.ids),
    }


# ======================================================================
# Guard the guard
# ======================================================================


@pytest.mark.asyncio
async def test_seeding_is_not_vacuous(world) -> None:
    """Every purged table really did get a row to delete."""
    empty: list[str] = []
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        policy = policy_for(DataConsumer.DELETE, rule.table).policy
        if policy not in (
            DeletePolicy.PURGE,
            DeletePolicy.PURGE_ANY_SIDE,
            DeletePolicy.PURGE_VIA_PARENT,
            DeletePolicy.RETAIN,
        ):
            continue
        table = Base.metadata.tables[rule.table]
        count = await world.count(select(func.count()).select_from(table))
        if count == 0:
            empty.append(rule.table)
    assert not empty, f"seeding missed: {empty}"


@pytest.mark.asyncio
async def test_both_rigs_really_differ_on_fk_enforcement(world) -> None:
    """Guard the parametrisation itself.

    The whole value of the ``no-fk-pragma`` run is that SQLite genuinely
    performs no cascade in it — if the pragma leaked in, the zero-orphan
    pin would silently go back to testing the database instead of the
    eraser.
    """
    async with world.session_factory() as session:
        pragma = await session.execute(text("PRAGMA foreign_keys"))
        assert pragma.scalar_one() == (1 if world.fk_enforced else 0)
        seeded = await session.execute(
            select(func.count()).select_from(Base.metadata.tables["messages"]),
        )
        assert seeded.scalar_one() >= 1


# ======================================================================
# 1. Zero orphans
# ======================================================================


@pytest.mark.asyncio
async def test_delete_leaves_zero_rows_in_every_purged_table(world) -> None:
    service = world.service()

    assert await service.delete_character(TARGET_ID) is True

    survivors: dict[str, int] = {}
    for rule in CHARACTER_BACKUP_TABLE_RULES:
        policy = policy_for(DataConsumer.DELETE, rule.table).policy
        table = Base.metadata.tables[rule.table]
        if policy is DeletePolicy.PURGE:
            predicate = character_predicate(table, rule, TARGET_ID)
        elif policy is DeletePolicy.PURGE_ANY_SIDE:
            predicate = any_side_character_predicate(table, TARGET_ID)
        elif policy is DeletePolicy.PURGE_VIA_PARENT:
            # No character column to filter on — the whole point of the
            # policy. Every row seeded here belonged to the character's
            # parent row, so the table must be empty. This is the
            # assertion that fails on the ``no-fk-pragma`` rig unless the
            # eraser issues the delete itself.
            predicate = None
        else:
            continue
        stmt = select(func.count()).select_from(table)
        if predicate is not None:
            stmt = stmt.where(predicate)
        remaining = await world.count(stmt)
        if remaining:
            survivors[rule.table] = remaining
    assert not survivors, f"orphan rows survived delete_character: {survivors}"


@pytest.mark.asyncio
async def test_other_characters_rows_survive(world) -> None:
    """The two-sided predicate must not reach rows between two peers."""
    service = world.service()
    await service.delete_character(TARGET_ID)

    for rule in CHARACTER_BACKUP_TABLE_RULES:
        policy = policy_for(DataConsumer.DELETE, rule.table).policy
        if policy is not DeletePolicy.PURGE_ANY_SIDE:
            continue
        table = Base.metadata.tables[rule.table]
        remaining = await world.count(
            select(func.count()).select_from(table).where(
                any_side_character_predicate(table, PEER_ID),
            ),
        )
        assert remaining == 1, rule.table

    characters = Base.metadata.tables["characters"]
    assert await world.count(
        select(func.count()).select_from(characters).where(
            characters.c.id.in_([PEER_ID, THIRD_ID]),
        ),
    ) == 2


@pytest.mark.asyncio
async def test_global_story_seed_pool_survives_the_row_filter(world) -> None:
    seeds = Base.metadata.tables["story_seeds"]
    await world.service().delete_character(TARGET_ID)
    assert await world.count(
        select(func.count()).select_from(seeds).where(
            seeds.c.id == world.seeded["global_seed_id"],
        ),
    ) == 1


# ======================================================================
# 2. D1 — billing evidence is never destroyed
# ======================================================================


@pytest.mark.asyncio
async def test_retained_evidence_survives_the_delete(world) -> None:
    retained = [
        rule.table
        for rule in CHARACTER_BACKUP_TABLE_RULES
        if policy_for(DataConsumer.DELETE, rule.table).policy
        is DeletePolicy.RETAIN
    ]
    assert retained, "the RETAIN policy has no tables — D1 lost its teeth"

    await world.service().delete_character(TARGET_ID)

    for table_name in retained:
        table = Base.metadata.tables[table_name]
        remaining = await world.count(
            select(func.count()).select_from(table),
        )
        assert remaining == 1, f"{table_name} lost its D1 evidence row"


# ======================================================================
# 3. D2 — co-authored works untouched
# ======================================================================


@pytest.mark.asyncio
async def test_coauthored_work_is_neither_deleted_nor_rewritten(
    world,
) -> None:
    fusion = Base.metadata.tables["fusion_stories"]
    await world.service().delete_character(TARGET_ID)

    async with world.session_factory() as session:
        row = (await session.execute(
            select(fusion.c.character_ids_json).where(
                fusion.c.id == world.seeded["fusion_id"],
            ),
        )).first()
    assert row is not None, "D2: the co-authored work was deleted"
    assert row[0] == world.seeded["fusion_ids_json"], (
        "D2: character_ids_json was rewritten"
    )


# ======================================================================
# 4. Storage
# ======================================================================


@pytest.mark.asyncio
async def test_tracked_keys_and_owned_prefixes_are_swept(world) -> None:
    await world.service().delete_character(TARGET_ID)

    for key in (AVATAR_KEY, ALBUM_KEY, FEED_KEY, TTS_KEY, CHAT_UPLOAD_KEY):
        assert await world.storage.stat(object_key=key) is None, key
    assert await world.storage.stat(object_key=PEER_KEEP_KEY) is not None


@pytest.mark.asyncio
async def test_harvest_cannot_delete_objects_outside_the_boundary(
    world,
) -> None:
    """The cross-tenant delete primitive, closed.

    ``characters.image_urls`` and ``messages.attachments_json`` take
    whatever the operator sends, unvalidated. If the delete sweep trusted
    them, pasting a victim's URL into your own character and deleting it
    would delete the victim's file. Both hostile references are harvested
    (the reverse lookup does find them) and both objects survive.
    """
    eraser = SACharacterDataEraser(world.session_factory)
    harvested = await eraser.collect_media_object_urls(TARGET_ID)
    assert _url(PEER_KEEP_KEY) in harvested
    assert _url(VICTIM_UPLOAD_KEY) in harvested

    await world.service().delete_character(TARGET_ID)

    assert await world.storage.stat(object_key=PEER_KEEP_KEY) is not None
    assert await world.storage.stat(object_key=VICTIM_UPLOAD_KEY) is not None
    # The character's own keys — including its own operator's chat
    # upload — still go.
    assert await world.storage.stat(object_key=AVATAR_KEY) is None
    assert await world.storage.stat(object_key=CHAT_UPLOAD_KEY) is None


@pytest.mark.asyncio
async def test_operator_scoped_upload_needs_the_db_reverse_lookup(
    world,
) -> None:
    """The chat attachment lives outside every character prefix, so only
    the harvest can reach it — pinned by showing no owned prefix matches
    it."""
    assert not any(
        CHAT_UPLOAD_KEY.startswith(prefix)
        for prefix in character_storage_prefixes(TARGET_ID)
    )
    eraser = SACharacterDataEraser(world.session_factory)
    urls = await eraser.collect_media_object_urls(TARGET_ID)
    assert _url(CHAT_UPLOAD_KEY) in urls
    assert _url(AVATAR_KEY) in urls
    assert _url(FEED_KEY) in urls
    assert _url(ALBUM_KEY) in urls


@pytest.mark.asyncio
async def test_storage_failure_never_blocks_the_delete(world) -> None:
    service = world.service(storage=_ExplodingStorage())

    assert await service.delete_character(TARGET_ID) is True

    characters = Base.metadata.tables["characters"]
    assert await world.count(
        select(func.count()).select_from(characters).where(
            characters.c.id == TARGET_ID,
        ),
    ) == 0
    conversations = Base.metadata.tables["conversations"]
    assert await world.count(
        select(func.count()).select_from(conversations).where(
            conversations.c.character_id == TARGET_ID,
        ),
    ) == 0


@pytest.mark.asyncio
async def test_adapter_without_purge_capability_still_deletes_keys(
    world,
) -> None:
    legacy = _NoPurgeStorage(world.storage)
    erasure = CharacterDataErasureService(
        database_eraser=SACharacterDataEraser(world.session_factory),
        object_storage=legacy,
    )

    report = await erasure.erase(TARGET_ID)

    assert report.character_existed is True
    assert report.storage.prefix_purge_unsupported is True
    assert report.storage.objects_failed == 0
    # DB-tracked keys still gone; the content-addressed TTS remainder is
    # what the missing capability costs (pre-CD1 behaviour, not a crash).
    for key in (AVATAR_KEY, ALBUM_KEY, FEED_KEY, CHAT_UPLOAD_KEY):
        assert await world.storage.stat(object_key=key) is None, key
    assert await world.storage.stat(object_key=TTS_KEY) is not None


@pytest.mark.asyncio
async def test_report_counts_what_it_swept(world) -> None:
    erasure = CharacterDataErasureService(
        database_eraser=SACharacterDataEraser(world.session_factory),
        object_storage=world.storage,
    )

    report = await erasure.erase(TARGET_ID)

    assert report.rows_deleted["characters"] == 1
    assert "generation_usage_events" not in report.rows_deleted
    assert "fusion_stories" not in report.rows_deleted
    assert report.storage.objects_deleted == 4
    assert report.storage.prefixes_purged == 3
    assert report.storage.clean is True


# ======================================================================
# Storage doubles
# ======================================================================


class _ExplodingStorage:
    """Every storage call fails — the fail-soft boundary under test."""

    def object_key_from_url(self, url: str) -> str | None:
        raise RuntimeError("storage down")

    async def delete(self, *, object_key: str) -> None:
        raise RuntimeError("storage down")

    async def purge_prefix(self, *, prefix: str) -> int:
        raise RuntimeError("storage down")


class _NoPurgeStorage:
    """A pre-CD1 adapter: an ``ObjectStoragePort`` and nothing more.

    Deliberately does **not** forward unknown attributes, so
    ``getattr(port, "purge_prefix")`` finds nothing — the capability
    detection this exercises must not be an ``isinstance`` check.
    """

    def __init__(self, inner: InMemoryObjectStorage) -> None:
        self._inner = inner

    def object_key_from_url(self, url: str) -> str | None:
        return self._inner.object_key_from_url(url)

    async def delete(self, *, object_key: str) -> None:
        await self._inner.delete(object_key=object_key)


# ======================================================================
# 5. The chat-upload prefix is built from the *written* key segment
# ======================================================================
#
# ``api/routes/chat.py`` folds the operator id through
# ``safe_key_segment`` before it becomes part of the object key, so the
# id on the row and the segment in the key are not the same string for
# any id containing characters outside the allow-list — which is every
# hosted operator id, since those are ``cloud:{account_id}``. The
# containment boundary has to apply the same fold or it compares against
# a prefix nothing can start with, and the "outside the boundary" branch
# then swallows every hosted attachment.
#
# These cases drive the service through a fake database eraser: the seam
# under test is prefix arithmetic, and the SQL rig above already proves
# the harvest itself.


FAKE_CHARACTER_ID = "char-prefix"


class _FakeDatabaseEraser:
    """A ``CharacterDatabaseEraserPort`` with hand-picked owner + URLs."""

    def __init__(self, *, owner_id: str | None, urls: tuple[str, ...]) -> None:
        self._owner_id = owner_id
        self._urls = urls

    async def collect_media_object_urls(
        self, character_id: str,
    ) -> tuple[str, ...]:
        return self._urls

    async def owner_user_id(self, character_id: str) -> str | None:
        return self._owner_id

    async def erase(self, character_id: str) -> DatabaseErasureResult:
        return DatabaseErasureResult(
            character_existed=True, rows_deleted=MappingProxyType({}),
        )


async def _erase_with_owner(
    owner_id: str | None, keys: tuple[str, ...],
) -> tuple[InMemoryObjectStorage, object]:
    storage = InMemoryObjectStorage()
    for key in keys:
        await storage.put_bytes(
            object_key=key, content=b"bytes", content_type="image/png",
        )
    service = CharacterDataErasureService(
        database_eraser=_FakeDatabaseEraser(
            owner_id=owner_id, urls=tuple(_url(key) for key in keys),
        ),
        object_storage=storage,
    )
    return storage, await service.erase(FAKE_CHARACTER_ID)


@pytest.mark.asyncio
async def test_hosted_owner_id_matches_the_sanitised_upload_prefix() -> None:
    """``cloud:abc`` on the row, ``users/cloudabc/…`` in the store.

    The colon never survives into the key, so a boundary built from the
    raw id (``users/cloud:abc/chat-uploads/``) matches nothing and the
    attachment is skipped as "outside the boundary" — a silent leak on
    every hosted character delete.
    """
    hosted_upload = "users/cloudabc/chat-uploads/attachment.png"
    own_avatar = f"characters/{FAKE_CHARACTER_ID}/avatar.png"

    storage, report = await _erase_with_owner(
        "cloud:abc", (hosted_upload, own_avatar),
    )

    assert await storage.stat(object_key=hosted_upload) is None
    assert await storage.stat(object_key=own_avatar) is None
    assert report.storage.objects_deleted == 2


@pytest.mark.asyncio
async def test_sanitising_never_widens_to_another_operators_uploads() -> None:
    """Containment still holds once the fold is applied.

    Both hostile shapes are refused: a different operator's subtree, and
    a neighbouring segment the owner's own segment is a string prefix of
    (``cloudabc`` vs ``cloudabcd`` — the reason the boundary prefix must
    keep its trailing slash).
    """
    victim_upload = "users/cloudother/chat-uploads/private.png"
    near_miss = "users/cloudabcd/chat-uploads/private.png"

    storage, report = await _erase_with_owner(
        "cloud:abc", (victim_upload, near_miss),
    )

    assert await storage.stat(object_key=victim_upload) is not None
    assert await storage.stat(object_key=near_miss) is not None
    assert report.storage.objects_deleted == 0


@pytest.mark.asyncio
async def test_owner_id_that_sanitises_to_the_shared_bucket_is_skipped(
    caplog,
) -> None:
    """``users/unknown/chat-uploads/`` is several operators' bucket.

    Any id the allow-list eats entirely lands there, so claiming it as
    "this operator's subtree" would let one delete take another
    operator's uploads. The chat-upload half is dropped instead, loudly.
    """
    shared_bucket_upload = "users/unknown/chat-uploads/attachment.png"
    own_avatar = f"characters/{FAKE_CHARACTER_ID}/avatar.png"

    with caplog.at_level(
        logging.WARNING,
        logger="kokoro_link.application.services.character_data_erasure",
    ):
        storage, report = await _erase_with_owner(
            "《》", (shared_bucket_upload, own_avatar),
        )

    assert await storage.stat(object_key=shared_bucket_upload) is not None
    assert await storage.stat(object_key=own_avatar) is None
    assert report.storage.objects_deleted == 1
    assert any(
        "shared" in record.getMessage() and "chat-upload" in record.getMessage()
        for record in caplog.records
    ), caplog.text


# ======================================================================
# In-memory slot wiring — one declaration, two call sites
# ======================================================================
#
# The SQL eraser derives its boundary from the registry, so it cannot
# drift. The in-memory eraser has nothing to derive from: it can only
# sweep repositories somebody handed it. Two places hand them in — the
# container (authoritative) and ``CharacterService``'s no-injection
# fallback — and each used to carry its own ordered list, so a repository
# added to one silently never reached the other.
#
# ``CHARACTER_ERASURE_REPOSITORY_SLOTS`` is now the single declaration of
# which roles exist and in which order they run; both call sites hand in
# a mapping keyed by it. These cases pin that arrangement: an unknown
# name cannot be wired at all, the container wires every declared slot it
# built, and the fallback is a subset of the container rather than a
# second boundary statement.


class _AnyRepository:
    """Satisfies every repository protocol the erasure fallback touches."""

    async def delete_for_character(self, character_id: str) -> int:
        return 0

    async def get(self, character_id: str):
        return None

    async def delete(self, character_id: str) -> bool:
        return False


def _maximally_wired_service() -> CharacterService:
    """A service with *every* optional repository present.

    Derived from the constructor signature rather than a list, so a
    repository added to the fallback is covered here the day it lands —
    a hand-kept list in the test would be the third copy of the very
    thing this suite exists to stop.
    """
    parameters = inspect.signature(CharacterService.__init__).parameters
    return CharacterService(
        _AnyRepository(),
        **{
            name: _AnyRepository()
            for name, parameter in parameters.items()
            if name.endswith("_repository")
            and parameter.kind is inspect.Parameter.KEYWORD_ONLY
        },
    )


def test_declared_slots_are_unique_and_child_before_parent() -> None:
    """The declaration is the ordering contract, so it has to be sane."""
    slots = CHARACTER_ERASURE_REPOSITORY_SLOTS

    assert len(slots) == len(set(slots)), "duplicate slot name"
    assert slots.index("pending_follow_up") < slots.index("conversation"), (
        "child before parent: where the FK is enforced, purging the "
        "conversations first takes the follow-ups with them and the "
        "follow-up repository then reports zero rows it did own"
    )


def test_unregistered_slot_name_is_rejected_loudly() -> None:
    """A slot nobody declared would be a repository that never runs."""
    with pytest.raises(UnknownErasureRepositorySlotError) as excinfo:
        RepositoryCharacterDatabaseEraser(
            character_repository=_AnyRepository(),
            repositories={"memory": _AnyRepository(), "typo": _AnyRepository()},
        )

    assert "typo" in str(excinfo.value)


def test_container_wires_every_declared_slot() -> None:
    """The container's wiring is the authoritative one.

    Adding a slot to the declaration without giving the container a
    repository for it turns this red — which is the point: the
    declaration is not a wish list, it is what the in-memory delete
    actually sweeps.
    """
    container = build_container(AppSettings(database_url=""))
    eraser = container.character_service._data_erasure.database_eraser

    assert isinstance(eraser, RepositoryCharacterDatabaseEraser)
    assert eraser.declared_slot_names == CHARACTER_ERASURE_REPOSITORY_SLOTS
    assert set(eraser.slot_names) <= set(eraser.declared_slot_names), (
        "a deployment can leave a declared slot unbuilt (feature flags), "
        "never wire one it never declared"
    )


def test_service_fallback_wiring_is_a_subset_of_the_container_wiring() -> None:
    """The fallback may reach less; it may never reach *elsewhere*.

    ``CharacterService`` only receives some of the repositories the
    container builds, so a narrower sweep is legitimate. A slot the
    fallback wires but the container does not is not — that would be a
    second, divergent boundary, which is exactly the drift the single
    declaration removed.
    """
    container = build_container(AppSettings(database_url=""))
    container_slots = set(
        container.character_service
        ._data_erasure.database_eraser.declared_slot_names,
    )
    fallback = _maximally_wired_service()._data_erasure.database_eraser
    fallback_slots = set(fallback.declared_slot_names)

    assert fallback_slots, "the fallback wired nothing at all"
    assert fallback_slots <= container_slots, (
        "slots the fallback wires but the container does not: "
        f"{sorted(fallback_slots - container_slots)}"
    )
    assert fallback.declared_slot_names == tuple(
        slot
        for slot in CHARACTER_ERASURE_REPOSITORY_SLOTS
        if slot in fallback_slots
    ), "execution order must come from the declaration, not the call site"
