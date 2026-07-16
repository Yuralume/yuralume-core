from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest, UpdateCharacterRequest
from kokoro_link.application.services.character_service import (
    CharacterService,
    CharacterValidationError,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEMO_ACCOUNT_RUNTIME_PROFILE,
)
from kokoro_link.domain.entities.arc_series import ArcSeries
from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_SCOPE_CHARACTER_BOUND,
    ArcTemplate,
    ArcTemplateBeat,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_series import (
    InMemoryArcSeriesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import InMemoryCharacterRepository
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)


class _StaticRuntimeProfileResolver:
    async def resolve_for_operator(self, operator_id: str):
        return DEMO_ACCOUNT_RUNTIME_PROFILE


class _MutableClock:
    def __init__(self, initial: datetime) -> None:
        self.current = initial

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _template(template_id: str, *, target_ids: list[str] | None = None) -> ArcTemplate:
    return ArcTemplate.create(
        id=template_id,
        title="專用劇情",
        premise="只適合特定角色的劇情。",
        beats=[
            ArcTemplateBeat.create(
                sequence=0,
                day_offset=0,
                title="開場",
                summary="她走進只屬於自己的故事。",
            ),
        ],
        applicability_scope=(
            ARC_TEMPLATE_SCOPE_CHARACTER_BOUND if target_ids is not None else "generic"
        ),
        target_character_ids=target_ids or [],
    )


@pytest.mark.asyncio
async def test_create_and_update_character() -> None:
    service = CharacterService(InMemoryCharacterRepository())

    created = await service.create_character(
        CreateCharacterRequest(
            name="Airi",
            summary="一個溫柔的角色",
            personality=["gentle"],
            interests=["music"],
            speaking_style="soft",
            boundaries=["no violence"],
        )
    )

    assert created.name == "Airi"
    assert created.state.energy == 100
    assert created.proactive_enabled is True

    updated = await service.update_character(
        created.id,
        UpdateCharacterRequest(
            interests=["music", "news"],
            speaking_style="playful",
        ),
    )

    assert updated is not None
    assert updated.interests == ["music", "news"]
    assert updated.speaking_style == "playful"


@pytest.mark.asyncio
async def test_demo_runtime_profile_rejects_second_character() -> None:
    service = CharacterService(
        InMemoryCharacterRepository(),
        account_runtime_profile_resolver=_StaticRuntimeProfileResolver(),
        account_runtime_usage_repository=InMemoryAccountRuntimeUsageRepository(),
    )

    await service.create_character(CreateCharacterRequest(name="Airi"), user_id="cloud:acct")

    with pytest.raises(CharacterValidationError, match="character limit"):
        await service.create_character(
            CreateCharacterRequest(name="Rin"),
            user_id="cloud:acct",
        )


@pytest.mark.asyncio
async def test_demo_runtime_profile_requires_usage_ledger_for_daily_limit() -> None:
    service = CharacterService(
        InMemoryCharacterRepository(),
        account_runtime_profile_resolver=_StaticRuntimeProfileResolver(),
    )

    with pytest.raises(CharacterValidationError, match="ledger is not configured"):
        await service.create_character(
            CreateCharacterRequest(name="Airi"),
            user_id="cloud:acct",
        )


@pytest.mark.asyncio
async def test_demo_daily_character_create_limit_survives_delete() -> None:
    character_repo = InMemoryCharacterRepository()
    clock = _MutableClock(datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc))
    service = CharacterService(
        character_repo,
        account_runtime_profile_resolver=_StaticRuntimeProfileResolver(),
        account_runtime_usage_repository=InMemoryAccountRuntimeUsageRepository(),
        clock=clock,
    )
    created = await service.create_character(
        CreateCharacterRequest(name="Airi"),
        user_id="cloud:acct",
    )
    assert await service.delete_character(created.id, user_id="cloud:acct") is True

    with pytest.raises(CharacterValidationError, match="daily character create limit"):
        await service.create_character(
            CreateCharacterRequest(name="Rin"),
            user_id="cloud:acct",
        )

    clock.advance(timedelta(days=1, seconds=1))
    recreated = await service.create_character(
        CreateCharacterRequest(name="Rin"),
        user_id="cloud:acct",
    )
    assert recreated.name == "Rin"


@pytest.mark.asyncio
async def test_update_character_validates_arc_series_visibility_when_wired() -> None:
    character_repo = InMemoryCharacterRepository()
    series_repo = InMemoryArcSeriesRepository()
    service = CharacterService(
        character_repo,
        arc_series_repository=series_repo,
    )
    created = await service.create_character(
        CreateCharacterRequest(name="Airi", summary="一個溫柔的角色"),
        user_id="user-a",
    )
    await series_repo.save_for_user(
        ArcSeries.create(
            id="series-a",
            title="第一季",
            premise="兩本劇本依序展開。",
            template_ids=["book-one", "book-two"],
            user_id="user-a",
        ),
        user_id="user-a",
    )

    updated = await service.update_character(
        created.id,
        UpdateCharacterRequest(arc_series_id="series-a"),
        user_id="user-a",
    )

    assert updated is not None
    assert updated.arc_series_id == "series-a"

    with pytest.raises(CharacterValidationError, match="not visible"):
        await service.update_character(
            created.id,
            UpdateCharacterRequest(arc_series_id="missing"),
            user_id="user-a",
        )


@pytest.mark.asyncio
async def test_update_character_rejects_cross_user_arc_series() -> None:
    character_repo = InMemoryCharacterRepository()
    series_repo = InMemoryArcSeriesRepository()
    service = CharacterService(
        character_repo,
        arc_series_repository=series_repo,
    )
    created = await service.create_character(
        CreateCharacterRequest(name="Airi", summary="一個溫柔的角色"),
        user_id="user-a",
    )
    await series_repo.save_for_user(
        ArcSeries.create(
            id="series-b",
            title="別人的系列",
            premise="兩本劇本依序展開。",
            template_ids=["book-one", "book-two"],
            user_id="user-b",
        ),
        user_id="user-b",
    )

    with pytest.raises(CharacterValidationError, match="not visible"):
        await service.update_character(
            created.id,
            UpdateCharacterRequest(arc_series_id="series-b"),
            user_id="user-a",
        )


@pytest.mark.asyncio
async def test_update_character_validates_arc_template_applicability_when_wired() -> None:
    character_repo = InMemoryCharacterRepository()
    template_repo = InMemoryArcTemplateRepository()
    service = CharacterService(
        character_repo,
        arc_template_repository=template_repo,
    )
    char_a = await service.create_character(
        CreateCharacterRequest(name="Airi", summary="一個溫柔的角色"),
        user_id="user-a",
    )
    char_b = await service.create_character(
        CreateCharacterRequest(name="Rin", summary="另一個角色"),
        user_id="user-a",
    )
    await template_repo.save_for_user(
        _template("airi_only", target_ids=[char_a.id]),
        user_id="user-a",
    )

    updated = await service.update_character(
        char_a.id,
        UpdateCharacterRequest(arc_template_id="airi_only"),
        user_id="user-a",
    )

    assert updated is not None
    assert updated.arc_template_id == "airi_only"
    with pytest.raises(CharacterValidationError, match="not applicable"):
        await service.update_character(
            char_b.id,
            UpdateCharacterRequest(arc_template_id="airi_only"),
            user_id="user-a",
        )


@pytest.mark.asyncio
async def test_update_character_rejects_cross_user_arc_template() -> None:
    character_repo = InMemoryCharacterRepository()
    template_repo = InMemoryArcTemplateRepository()
    service = CharacterService(
        character_repo,
        arc_template_repository=template_repo,
    )
    created = await service.create_character(
        CreateCharacterRequest(name="Airi", summary="一個溫柔的角色"),
        user_id="user-a",
    )
    await template_repo.save_for_user(
        _template("other_user_template"),
        user_id="user-b",
    )

    with pytest.raises(CharacterValidationError, match="not visible"):
        await service.update_character(
            created.id,
            UpdateCharacterRequest(arc_template_id="other_user_template"),
            user_id="user-a",
        )
