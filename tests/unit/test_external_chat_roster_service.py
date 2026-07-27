"""Unit coverage for :class:`ExternalChatRosterService` (LH2).

Covers the design invariants: paired-identity fail-closed resolution,
subscription-denial exclusion (tenant lock + legacy ``subscription_lapse``
freeze), idle/manual freeze retained with ``background_frozen``, avatar
object-key normalisation, a presentation version that is stable under
unrelated edits but changes on name/avatar, and a stable sort by
``character_id``.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

from kokoro_link.application.services.external_chat_roster_service import (
    ExternalChatRosterService,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_IDLE,
    FREEZE_REASON_MANUAL,
    FREEZE_REASON_SUBSCRIPTION_LAPSE,
    Character,
)
from kokoro_link.domain.entities.cloud_subscription import CloudSubscriptionState
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState

_TENANT = "tenant-A"
_ACCOUNT = "acct-1"
_OPERATOR_ID = "cloud:acct-1"


class _FakeOperatorRepo:
    def __init__(self, operators: list[OperatorProfile]) -> None:
        self._by_id = {op.id: op for op in operators}

    async def get(self, operator_id: str) -> OperatorProfile | None:
        return self._by_id.get(operator_id)

    async def get_by_cloud_account_id(
        self, cloud_account_id: str,
    ) -> OperatorProfile | None:
        # Mirror the real repo: match on cloud_account_id only; the service
        # is responsible for the auth_provider / tenant pairing checks.
        target = cloud_account_id.strip()
        for op in self._by_id.values():
            if (op.cloud_account_id or "") == target:
                return op
        return None


class _FakeSubscriptionRepo:
    def __init__(self, locked_tenants: tuple[str, ...] = ()) -> None:
        self._locked = set(locked_tenants)

    async def get(self, tenant_id: str) -> CloudSubscriptionState | None:
        if tenant_id in self._locked:
            return CloudSubscriptionState(
                tenant_id=tenant_id,
                locked=True,
                updated_at=datetime.now(timezone.utc),
            )
        return None


class _FakeCharacterRepo:
    def __init__(self, by_user: dict[str, list[Character]]) -> None:
        self._by_user = by_user

    async def list_for_user(self, user_id: str) -> list[Character]:
        return list(self._by_user.get(user_id, []))


class _FakeObjectStorage:
    """Reverse ``/uploads/{key}`` and ``/v1/public/{key}`` refs; anything
    else is unresolvable (returns ``None``), matching the real adapters."""

    def object_key_from_url(self, url: str) -> str | None:
        for prefix in ("/uploads/", "/v1/public/"):
            if url.startswith(prefix):
                key = url[len(prefix):]
                return key or None
        return None


def _operator() -> OperatorProfile:
    return OperatorProfile(
        id=_OPERATOR_ID,
        display_name="Player",
        cloud_account_id=_ACCOUNT,
        cloud_tenant_id=_TENANT,
        auth_provider="cloud",
    )


def _character(
    *,
    character_id: str = "c1",
    name: str = "Yui",
    image_urls: tuple[str, ...] = (),
    frozen: bool = False,
    frozen_reason: str | None = None,
    summary: str = "",
) -> Character:
    state = CharacterState(
        emotion="calm", affection=50, fatigue=20, trust=50, energy=60,
    )
    return Character(
        id=character_id,
        name=name,
        summary=summary,
        user_id=_OPERATOR_ID,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=state,
        image_urls=image_urls,
        frozen=frozen,
        frozen_reason=frozen_reason,
    )


def _service(
    *,
    characters: list[Character] | None = None,
    operators: list[OperatorProfile] | None = None,
    locked_tenants: tuple[str, ...] = (),
) -> ExternalChatRosterService:
    ops = operators if operators is not None else [_operator()]
    operator_repo = _FakeOperatorRepo(ops)
    subscription_repo = _FakeSubscriptionRepo(locked_tenants)
    guard = SubscriptionAccessGuard(
        subscription_repository=subscription_repo,
        operator_profile_repository=operator_repo,
    )
    by_user: dict[str, list[Character]] = {}
    for ch in characters or []:
        by_user.setdefault(ch.user_id, []).append(ch)
    return ExternalChatRosterService(
        character_repository=_FakeCharacterRepo(by_user),
        operator_profile_repository=operator_repo,
        subscription_access_guard=guard,
        object_storage=_FakeObjectStorage(),
    )


def _resolve(service, *, tenant_id=_TENANT, account_id=_ACCOUNT):
    return asyncio.run(
        service.resolve_roster(tenant_id=tenant_id, account_id=account_id),
    )


# --- pairing ---------------------------------------------------------------


def test_unknown_account_returns_none() -> None:
    service = _service(characters=[_character()])
    assert _resolve(service, account_id="ghost") is None


def test_tenant_mismatch_returns_none() -> None:
    service = _service(characters=[_character()])
    assert _resolve(service, tenant_id="tenant-OTHER") is None


def test_non_cloud_operator_returns_none() -> None:
    local_op = OperatorProfile(
        id=_OPERATOR_ID,
        display_name="Player",
        cloud_account_id=_ACCOUNT,
        cloud_tenant_id=_TENANT,
        auth_provider="local",
    )
    service = _service(operators=[local_op], characters=[_character()])
    assert _resolve(service) is None


def test_blank_inputs_return_none() -> None:
    service = _service(characters=[_character()])
    assert _resolve(service, tenant_id="   ") is None
    assert _resolve(service, account_id="") is None


# --- subscription denial ---------------------------------------------------


def test_locked_tenant_yields_empty_roster_not_none() -> None:
    service = _service(
        characters=[_character()], locked_tenants=(_TENANT,),
    )
    view = _resolve(service)
    assert view is not None
    assert view.characters == ()


def test_subscription_lapse_freeze_is_excluded() -> None:
    kept = _character(character_id="c-ok")
    lapsed = _character(
        character_id="c-lapsed",
        frozen=True,
        frozen_reason=FREEZE_REASON_SUBSCRIPTION_LAPSE,
    )
    view = _resolve(_service(characters=[kept, lapsed]))
    assert [c.character_id for c in view.characters] == ["c-ok"]


# --- freeze presentation ---------------------------------------------------


def test_idle_and_manual_freeze_retained_with_flag() -> None:
    idle = _character(
        character_id="c-idle", frozen=True, frozen_reason=FREEZE_REASON_IDLE,
    )
    manual = _character(
        character_id="c-manual", frozen=True, frozen_reason=FREEZE_REASON_MANUAL,
    )
    view = _resolve(_service(characters=[idle, manual]))
    flags = {c.character_id: c.background_frozen for c in view.characters}
    assert flags == {"c-idle": True, "c-manual": True}


def test_normal_character_flag_false() -> None:
    view = _resolve(_service(characters=[_character()]))
    assert view.characters[0].background_frozen is False


# --- avatar normalisation --------------------------------------------------


def test_avatar_uploads_url_normalised_to_object_key() -> None:
    ch = _character(image_urls=("/uploads/users/u1/portrait.png", "/uploads/alt.png"))
    view = _resolve(_service(characters=[ch]))
    assert view.characters[0].avatar_object_ref == "users/u1/portrait.png"


def test_avatar_unresolvable_or_absent_is_none() -> None:
    remote = _character(character_id="c-remote", image_urls=("https://cdn/x.png",))
    none_img = _character(character_id="c-none", image_urls=())
    view = _resolve(_service(characters=[remote, none_img]))
    refs = {c.character_id: c.avatar_object_ref for c in view.characters}
    assert refs == {"c-remote": None, "c-none": None}


# --- presentation version --------------------------------------------------


def test_presentation_version_matches_spec() -> None:
    ch = _character(name="Yui", image_urls=("/uploads/a.png",))
    view = _resolve(_service(characters=[ch]))
    expected = hashlib.sha256("Yui\na.png".encode("utf-8")).hexdigest()[:16]
    assert view.characters[0].presentation_version == expected
    assert len(view.characters[0].presentation_version) == 16


def test_presentation_version_stable_under_unrelated_edit() -> None:
    base = _character(name="Yui", image_urls=("/uploads/a.png",), summary="one")
    edited = _character(name="Yui", image_urls=("/uploads/a.png",), summary="two")
    v1 = _resolve(_service(characters=[base])).characters[0].presentation_version
    v2 = _resolve(_service(characters=[edited])).characters[0].presentation_version
    assert v1 == v2


def test_presentation_version_changes_on_name_or_avatar() -> None:
    base = _character(name="Yui", image_urls=("/uploads/a.png",))
    renamed = _character(name="Aoi", image_urls=("/uploads/a.png",))
    reimaged = _character(name="Yui", image_urls=("/uploads/b.png",))
    v_base = _resolve(_service(characters=[base])).characters[0].presentation_version
    v_name = _resolve(_service(characters=[renamed])).characters[0].presentation_version
    v_img = _resolve(_service(characters=[reimaged])).characters[0].presentation_version
    assert v_name != v_base
    assert v_img != v_base


# --- ordering --------------------------------------------------------------


def test_characters_sorted_by_character_id() -> None:
    chars = [
        _character(character_id="c-zeta"),
        _character(character_id="c-alpha"),
        _character(character_id="c-mid"),
    ]
    view = _resolve(_service(characters=chars))
    assert [c.character_id for c in view.characters] == [
        "c-alpha", "c-mid", "c-zeta",
    ]
