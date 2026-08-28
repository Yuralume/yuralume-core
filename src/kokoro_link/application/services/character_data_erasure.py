"""角色資料邊界的**刪除引擎**（CD2，ROADMAP #35）.

``delete_character`` used to be a hand-maintained list of twelve
repository calls living inside ``character_service.py`` — a list that had
to be remembered, and therefore drifted: sixteen character-referencing
tables had no foreign key to ``characters`` at all, so nothing in the
database swept them either. This module is that sweep, driven by the CD0
registry instead of by memory:

* **what a character's data is** — ``registry.py`` (tables, storage
  prefixes, media URL columns),
* **what delete does with each table** — ``consumer_policies.py``
  (:class:`~kokoro_link.application.dto.character_backup.DeletePolicy`),
* **how to execute it** — here plus the SA adapter.

There is deliberately **no table list in this file**. Adding a table to
the registry is the only action needed to bring it into the delete
boundary; forgetting to classify it turns CB0's pin test red before it
can reach production.

Order discipline (plan §5), and why each step must precede the next:

1. **harvest object keys** — media URLs live *in the rows*
   (``characters.image_urls``, ``messages.attachments_json``, …). Once
   the rows are gone the reverse lookup returns nothing, so the harvest
   has to run while the data still exists. This is also the only path
   that reaches operator-scoped chat uploads under
   ``users/{user_id}/chat-uploads/``, which no per-character prefix
   sweep can ever match. Because those URLs are stored verbatim from
   operator input, the harvest **contains** what it yields: a key must
   fall under one of the character's own prefixes or under the owning
   operator's chat-upload subtree, or it is logged and skipped — a row
   pointing at somebody else's object must not turn this sweep into a
   cross-tenant delete primitive.
2. **purge the database** — one transaction, registry-driven.
3. **delete the harvested keys** — one at a time (the variant-aware
   adapter fans each key out to its renditions).
4. **prefix backstop** — ``characters/{id}/``, ``feed/{id}/``,
   ``tts/{id}/``. Content-addressed TTS keys are never in any DB row, so
   step 3 structurally cannot reach them.

Steps 1, 3 and 4 are **fail-soft**: storage is a second system, and a
character delete that half-succeeded in the database must not be rolled
back because an object store hiccuped. Leftover bytes are recoverable
(the prefix backstop runs again on demand, and orphaned objects are
unreferenced); a half-deleted database is not. Every failure lands in
:class:`StorageErasureReport` and in the log rather than in an exception.

Deletion stays a **hard** delete: the policy vocabulary decides *which*
rows go, never *whether* they really go.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from kokoro_link.application.dto.character_backup import (
    MediaUrlField,
    MediaUrlShape,
    character_storage_prefixes,
    operator_scoped_media_prefixes,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.infrastructure.storage.keys import safe_key_segment

_LOGGER = logging.getLogger(__name__)

_CHAT_UPLOAD_OWNER_FALLBACK = "unknown"
"""The exact fallback the chat-upload **write** path passes to
:func:`~kokoro_link.infrastructure.storage.keys.safe_key_segment`
(``api/routes/chat.py::_safe_user_dir``).

The containment prefix has to be built from the same sanitised segment
the writer put in the key, or it matches nothing: a hosted operator id
is ``"cloud:{account_id}"``, whose colon the allow-list drops, so the
stored key is ``users/cloudXXXX/chat-uploads/…`` while the raw DB owner
id would build ``users/cloud:XXXX/chat-uploads/`` — a prefix no key can
ever start with, which silently turns the containment filter into a
blanket skip of every hosted chat attachment.

The fallback itself is the one segment that must **not** be used as a
prefix: it is a shared bucket every unsanitisable id lands in, so
``users/unknown/chat-uploads/`` can hold several operators' files. When
an owner id sanitises down to it, the chat-upload half of the boundary
is dropped instead — leftover bytes are recoverable, another operator's
deleted uploads are not."""


# ======================================================================
# Reports
# ======================================================================


@dataclass(frozen=True, slots=True)
class DatabaseErasureResult:
    """Outcome of the database half of an erasure.

    ``character_existed`` is what the API contract turns into 204 vs 404,
    so it must mean "a ``characters`` row was actually removed" — not
    "the sweep ran".
    """

    character_existed: bool
    rows_deleted: Mapping[str, int]
    """Rows removed per unit of work. The SA eraser keys this by table
    name; the repository eraser keys it by repository class, because that
    is the granularity it actually has."""


@dataclass
class StorageErasureReport:
    """Best-effort tally of the object-storage half."""

    objects_deleted: int = 0
    objects_failed: int = 0
    prefixes_purged: int = 0
    objects_purged_by_prefix: int = 0
    prefix_purge_unsupported: bool = False
    """The adapter has no :class:`SupportsPrefixPurge` capability — the
    content-addressed remainder is left behind (pre-CD1 behaviour, not a
    regression). See the fallback note on that protocol."""
    failures: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.failures and not self.prefix_purge_unsupported


@dataclass(frozen=True, slots=True)
class CharacterErasureReport:
    """Everything one ``delete_character`` did, for the caller's log."""

    character_existed: bool
    rows_deleted: Mapping[str, int]
    storage: StorageErasureReport


# ======================================================================
# Ports
# ======================================================================


class SupportsCharacterScopedDelete(Protocol):
    """The one method every character-scoped repository already has."""

    async def delete_for_character(self, character_id: str) -> int:
        ...


class SupportsCharacterRowDelete(Protocol):
    """The character repository, narrowed to what erasure needs."""

    async def get(self, character_id: str) -> Any:
        ...

    async def delete(self, character_id: str) -> bool:
        ...


class CharacterDatabaseEraserPort(Protocol):
    """The database half — one implementation per persistence mode.

    Kept as a port rather than an ``if session_factory is not None``
    inside the service because the two modes differ in kind, not in
    degree: the SA one derives its work from ORM metadata, the in-memory
    one can only call the repositories it was handed.
    """

    async def collect_media_object_urls(
        self, character_id: str,
    ) -> tuple[str, ...]:
        """Media URLs still reachable from this character's rows."""

    async def owner_user_id(self, character_id: str) -> str | None:
        """The operator this character belongs to, or ``None``.

        Needed only to bound the harvest: chat attachments live under the
        *operator's* subtree, so without an owner there is no way to tell
        this operator's uploads from anyone else's.
        """

    async def erase(self, character_id: str) -> DatabaseErasureResult:
        """Delete every row the DELETE policy purges, character last."""


# ======================================================================
# Repository-driven eraser (in-memory mode)
# ======================================================================


CHARACTER_ERASURE_REPOSITORY_SLOTS: tuple[str, ...] = (
    "state_history",
    "goal",
    "schedule",
    "memory",
    "memoir_pin",
    "proactive_attempt",
    "tool_invocation",
    "event_mention",
    "turn_journal",
    "album",
    "feed_post",
    "story_event",
    "story_arc",
    "operator_persona",
    "relationship_seed",
    "emotion_event",
    "behavioral_pattern",
    "self_reflection",
    "disposition_drift_history",
    "messaging_account",
    "character_relationship",
    "character_peer_profile",
    "character_encounter",
    "character_encounter_intent",
    "dialogue_checkpoint",
    "pending_follow_up",
    "conversation",
)
"""**The** declaration of the in-memory delete boundary: which repository
roles participate, and in which order they run.

Two call sites build a :class:`RepositoryCharacterDatabaseEraser` — the
container (authoritative: it claims every role it knows how to build)
and ``CharacterService``'s no-injection fallback (a legitimate subset,
see :attr:`RepositoryCharacterDatabaseEraser.declared_slot_names`).
Before this declaration existed each of them carried its own ordered
repository list, so a repository added to one silently never reached the
other: a second list that could only drift. Now both hand in a *mapping*
keyed by these names, and the order below — not the caller — decides
execution order. Registering a repository is one edit here plus one at
whichever call site owns it; a name neither list knows is a
construction-time error, not a silent gap.

**Order is child-before-parent**, mirroring the registry order
:class:`~kokoro_link.infrastructure.persistence.sa_character_data_eraser.SACharacterDataEraser`
reverses. It matters for the *counts*, not for orphans: every repository
here selects by ``character_id`` and states its own delete, so none of
them depends on another having run. But on a deployment where the FK
*is* enforced, purging a parent takes its children with it, and a child
repository that ran afterwards would report zero rows it did in fact
own. ``pending_follow_up`` before ``conversation`` is the case that bit.

``story_seed`` is absent on purpose: its in-memory twin has no
``delete_for_character`` (the SA path reaches private seeds through the
registry instead).

``dialogue_checkpoint`` arrives as ``None`` on every deployment with
``FEATURE_DIALOGUE_CHECKPOINT`` off — which is what the ``None``
tolerance is for. Claiming the slot regardless is the point: the wiring
decision is "this boundary includes checkpoints", and it must not depend
on whether a flag happened to be on when somebody read the list."""


class UnknownErasureRepositorySlotError(LookupError):
    """A repository was wired under a name
    :data:`CHARACTER_ERASURE_REPOSITORY_SLOTS` does not declare.

    Raised rather than ignored: a typo'd or unregistered slot means that
    repository's rows survive every in-memory delete, which is precisely
    the silent gap the single declaration exists to prevent.
    """


class RepositoryCharacterDatabaseEraser:
    """Erasure through the repository ports — the DB-less container mode.

    **Known to be narrower than the registry boundary**, on purpose: an
    in-memory rig has no schema to introspect, so the only tables it can
    reach are the ones some repository was written for. Tables with no
    repository (the runtime/audit ledgers ``turn_records``,
    ``proactive_attempts``' siblings, ``background_visible_slots``,
    ``scheduler_tick_journal``, ``experiment_assignments``,
    ``external_chat_turn_receipts``, ``external_proactive_events``,
    ``character_image_candidate_batches``, ``character_backup_jobs``,
    ``character_event_inbox``, and the ``deferred_intents`` /
    ``operator_address_*`` / ``persona_curiosity_attempts`` /
    ``character_series_progress`` / ``story_scene_sessions`` family)
    keep their rows here. That is acceptable because this mode is a
    development / test fallback whose process-local stores die with the
    process; the zero-orphan guarantee is pinned against the SA path,
    which is what every real deployment runs.

    ``repositories`` is keyed by the slot names of
    :data:`CHARACTER_ERASURE_REPOSITORY_SLOTS`; unwired roles are simply
    absent (or ``None``). **Callers do not choose the order** — the
    declaration does. That is the whole point of the mapping: an ordered
    list handed in by the caller is a boundary statement, and there were
    two of them.
    """

    def __init__(
        self,
        *,
        character_repository: SupportsCharacterRowDelete,
        repositories: Mapping[str, SupportsCharacterScopedDelete | None],
    ) -> None:
        unknown = sorted(
            set(repositories) - set(CHARACTER_ERASURE_REPOSITORY_SLOTS),
        )
        if unknown:
            raise UnknownErasureRepositorySlotError(
                "unknown character-erasure repository slot(s): "
                f"{', '.join(unknown)} — register them in "
                "CHARACTER_ERASURE_REPOSITORY_SLOTS",
            )
        self._character_repository = character_repository
        self._declared_slot_names = tuple(
            slot
            for slot in CHARACTER_ERASURE_REPOSITORY_SLOTS
            if slot in repositories
        )
        wired = tuple(
            (slot, repositories[slot])
            for slot in self._declared_slot_names
            if repositories[slot] is not None
        )
        self._slot_names = tuple(slot for slot, _ in wired)
        self._child_repositories = tuple(repository for _, repository in wired)

    @property
    def declared_slot_names(self) -> tuple[str, ...]:
        """Every slot the caller took responsibility for, in execution
        order — including the ones this deployment left unbuilt.

        This, not :attr:`slot_names`, is the caller's boundary statement:
        whether ``operator_persona`` arrives as ``None`` depends on a
        feature flag, whereas *claiming the slot at all* is the wiring
        decision the pin test compares between call sites.
        """
        return self._declared_slot_names

    @property
    def slot_names(self) -> tuple[str, ...]:
        """The subset of :attr:`declared_slot_names` this deployment
        actually built, in execution order — what ``erase`` will sweep."""
        return self._slot_names

    async def collect_media_object_urls(
        self, character_id: str,
    ) -> tuple[str, ...]:
        """The character's own image URLs — all this mode can see.

        Feed media, album originals and chat attachments need a row-level
        reverse lookup this mode has no query layer for; they are left to
        the prefix backstop (which covers ``feed/``, not chat uploads).
        """
        character = await self._character_repository.get(character_id)
        if character is None:
            return ()
        return tuple(
            url for url in getattr(character, "image_urls", ()) or () if url
        )

    async def owner_user_id(self, character_id: str) -> str | None:
        character = await self._character_repository.get(character_id)
        if character is None:
            return None
        return getattr(character, "user_id", None)

    async def erase(self, character_id: str) -> DatabaseErasureResult:
        rows_deleted: dict[str, int] = {}
        for repository in self._child_repositories:
            deleted = await repository.delete_for_character(character_id)
            count = int(deleted or 0)
            if count:
                name = type(repository).__name__
                rows_deleted[name] = rows_deleted.get(name, 0) + count
        existed = bool(await self._character_repository.delete(character_id))
        return DatabaseErasureResult(
            character_existed=existed,
            rows_deleted=MappingProxyType(rows_deleted),
        )


# ======================================================================
# Orchestrator — the single call face
# ======================================================================


class CharacterDataErasureService:
    """Runs the four erasure steps in the one order that is correct."""

    def __init__(
        self,
        *,
        database_eraser: CharacterDatabaseEraserPort,
        object_storage: ObjectStoragePort | None = None,
    ) -> None:
        self._database_eraser = database_eraser
        self._object_storage = object_storage

    @property
    def database_eraser(self) -> CharacterDatabaseEraserPort:
        """The wired database half — read-only, so the wiring pin test can
        compare what each call site built without reaching into privates."""
        return self._database_eraser

    async def erase(self, character_id: str) -> CharacterErasureReport:
        object_keys = await self._harvest_object_keys(character_id)
        database = await self._database_eraser.erase(character_id)
        storage = await self._erase_storage(character_id, object_keys)
        if not storage.clean:
            _LOGGER.warning(
                "character erasure %s: storage cleanup incomplete "
                "(deleted=%d failed=%d prefixes=%d unsupported=%s)",
                character_id,
                storage.objects_deleted,
                storage.objects_failed,
                storage.prefixes_purged,
                storage.prefix_purge_unsupported,
            )
        return CharacterErasureReport(
            character_existed=database.character_existed,
            rows_deleted=database.rows_deleted,
            storage=storage,
        )

    # -- step 1 ---------------------------------------------------------

    async def _harvest_object_keys(self, character_id: str) -> tuple[str, ...]:
        """Rows → URLs → object keys, before the rows are gone.

        Fail-soft in full: its only consumer is the storage cleanup, so a
        harvest that blows up must cost stored bytes, never the delete.

        **Every harvested key is checked against this character's own
        storage boundary before it is allowed near a delete.** The URLs
        come out of columns the write path stores verbatim —
        ``characters.image_urls`` and ``messages.attachments_json`` are
        whatever the operator sent — so an operator who pastes another
        character's (or another operator's) URL into their own character
        and then deletes it would otherwise have the delete sweep destroy
        someone else's objects for them. The write path not validating
        ownership is a separate debt; what this filter removes is the
        ability to *weaponise* it. Out-of-boundary keys are logged and
        skipped, never deleted.
        """
        if self._object_storage is None:
            return ()
        try:
            urls = await self._database_eraser.collect_media_object_urls(
                character_id,
            )
        except Exception:
            _LOGGER.exception(
                "character erasure %s: media URL harvest failed — stored "
                "objects may be left behind", character_id,
            )
            return ()
        owned_prefixes = await self._owned_key_prefixes(character_id)
        keys: list[str] = []
        seen: set[str] = set()
        for url in urls:
            try:
                object_key = self._object_storage.object_key_from_url(url)
            except Exception:
                _LOGGER.exception(
                    "character erasure %s: cannot derive object key from %s",
                    character_id, url,
                )
                continue
            if not object_key or object_key in seen:
                continue
            seen.add(object_key)
            if not object_key.startswith(owned_prefixes):
                _LOGGER.warning(
                    "character erasure %s: row references object %s outside "
                    "the character's storage boundary — not deleting",
                    character_id, object_key,
                )
                continue
            keys.append(object_key)
        return tuple(keys)

    async def _owned_key_prefixes(self, character_id: str) -> tuple[str, ...]:
        """The only prefixes this delete is allowed to remove keys from.

        The character's own three subtrees, plus the owning operator's
        chat-upload subtree — the one place a character's rows legitimately
        point outside their own prefix. An unknown owner narrows the
        boundary (chat uploads are left behind) instead of widening it:
        leftover bytes are recoverable, another operator's deleted uploads
        are not.

        The owner id is folded through the **write path's own sanitiser**
        before it becomes a prefix — see
        :data:`_CHAT_UPLOAD_OWNER_FALLBACK` for why a raw DB id builds a
        prefix that matches nothing, and why landing on the fallback
        segment drops the chat-upload half rather than widening it.
        """
        prefixes = character_storage_prefixes(character_id)
        try:
            owner_id = await self._database_eraser.owner_user_id(character_id)
        except Exception:
            _LOGGER.exception(
                "character erasure %s: owner lookup failed — operator-scoped "
                "chat uploads will be left behind", character_id,
            )
            return prefixes
        if not owner_id:
            return prefixes
        owner_segment = safe_key_segment(
            owner_id, fallback=_CHAT_UPLOAD_OWNER_FALLBACK,
        )
        if owner_segment == _CHAT_UPLOAD_OWNER_FALLBACK:
            _LOGGER.warning(
                "character erasure %s: owner id sanitises to the shared %r "
                "chat-upload bucket — skipping operator-scoped chat uploads "
                "rather than risk deleting another operator's files",
                character_id, _CHAT_UPLOAD_OWNER_FALLBACK,
            )
            return prefixes
        return prefixes + operator_scoped_media_prefixes(owner_segment)

    # -- steps 3 + 4 ----------------------------------------------------

    async def _erase_storage(
        self, character_id: str, object_keys: Sequence[str],
    ) -> StorageErasureReport:
        report = StorageErasureReport()
        storage = self._object_storage
        if storage is None:
            return report
        for object_key in object_keys:
            try:
                await storage.delete(object_key=object_key)
            except Exception as exc:
                report.objects_failed += 1
                report.failures.append(f"delete {object_key}: {exc}")
                _LOGGER.warning(
                    "character erasure %s: object delete failed (%s): %s",
                    character_id, object_key, exc,
                )
            else:
                report.objects_deleted += 1
        await self._purge_prefixes(character_id, report)
        return report

    async def _purge_prefixes(
        self, character_id: str, report: StorageErasureReport,
    ) -> None:
        """Prefix backstop, detected by capability — never ``isinstance``.

        A decorator that forwards unknown attributes (the variant-aware
        adapter) passes this check; a plain test double or a pre-CD1
        adapter does not, and then the untracked remainder is simply left
        behind with a log line.
        """
        purge = getattr(self._object_storage, "purge_prefix", None)
        if not callable(purge):
            report.prefix_purge_unsupported = True
            _LOGGER.info(
                "character erasure %s: object storage has no purge_prefix "
                "capability — skipping the prefix backstop; DB-tracked "
                "keys were still deleted individually", character_id,
            )
            return
        for prefix in character_storage_prefixes(character_id):
            try:
                removed = await purge(prefix=prefix)
            except Exception as exc:
                report.failures.append(f"purge {prefix}: {exc}")
                _LOGGER.warning(
                    "character erasure %s: prefix purge failed (%s): %s",
                    character_id, prefix, exc,
                )
                continue
            report.prefixes_purged += 1
            report.objects_purged_by_prefix += int(removed or 0)


# ======================================================================
# Media URL shape decoding — shared by every eraser implementation
# ======================================================================


def media_urls_from_value(
    field_spec: MediaUrlField, value: object,
) -> tuple[str, ...]:
    """URLs held by one column value, per its registered shape.

    Malformed JSON yields nothing rather than raising: a row whose
    attachment blob was hand-edited must not be able to stop a delete.
    """
    if value is None:
        return ()
    if field_spec.shape is MediaUrlShape.SCALAR:
        return (value,) if isinstance(value, str) and value else ()
    decoded = _decode_json_list(value)
    if field_spec.shape is MediaUrlShape.STRING_LIST_JSON:
        return tuple(item for item in decoded if isinstance(item, str) and item)
    urls: list[str] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            urls.append(url)
    return tuple(urls)


def _decode_json_list(value: object) -> list:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        _LOGGER.warning("character erasure: unreadable media JSON, skipping")
        return []
    return decoded if isinstance(decoded, list) else []
