"""Application service for authored arc series."""

from __future__ import annotations

from dataclasses import replace

from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.arc_series import (
    ArcSeries,
    CharacterSeriesProgress,
    SERIES_STATUS_CONCLUDED,
)
from kokoro_link.domain.entities.arc_template import ArcTemplateBinding


class ArcSeriesNotFoundError(ValueError):
    """Series is missing or not visible to the caller."""


class ArcSeriesValidationError(ValueError):
    """Series payload is invalid for the caller."""


class ArcSeriesService:
    def __init__(
        self,
        *,
        series_repository: ArcSeriesRepositoryPort,
        template_repository: ArcTemplateRepositoryPort,
        character_repository: CharacterRepositoryPort,
    ) -> None:
        self._series_repository = series_repository
        self._template_repository = template_repository
        self._character_repository = character_repository

    async def list_for_user(self, user_id: str) -> list[ArcSeries]:
        return await self._series_repository.list_for_user(user_id)

    async def get_for_user(
        self, series_id: str, *, user_id: str,
    ) -> ArcSeries:
        series = await self._series_repository.get_for_user(
            series_id, user_id=user_id,
        )
        if series is None:
            raise ArcSeriesNotFoundError(f"Arc series {series_id!r} not found")
        return series

    async def create_for_user(
        self,
        *,
        user_id: str,
        title: str,
        premise: str,
        theme: str = "custom",
        tone: str = "dramatic",
        world_frames: list[str] | tuple[str, ...] = (),
        required_traits: list[str] | tuple[str, ...] = (),
        template_ids: list[str] | tuple[str, ...],
        id: str | None = None,
    ) -> ArcSeries:
        member_ids = await self._validate_member_templates(
            template_ids, user_id=user_id,
        )
        series = ArcSeries.create(
            id=id,
            title=title,
            premise=premise,
            theme=theme,
            tone=tone,
            binding=ArcTemplateBinding(
                world_frames=tuple(world_frames),
                required_traits=tuple(required_traits),
            ),
            template_ids=member_ids,
            user_id=user_id,
        )
        await self._series_repository.save_for_user(
            series, user_id=user_id, overwrite=False,
        )
        return await self.get_for_user(series.id, user_id=user_id)

    async def update_for_user(
        self,
        series_id: str,
        *,
        user_id: str,
        title: str,
        premise: str,
        theme: str = "custom",
        tone: str = "dramatic",
        world_frames: list[str] | tuple[str, ...] = (),
        required_traits: list[str] | tuple[str, ...] = (),
        template_ids: list[str] | tuple[str, ...],
    ) -> ArcSeries:
        existing = await self.get_for_user(series_id, user_id=user_id)
        if existing.user_id is None:
            raise ArcSeriesValidationError(
                f"Arc series {series_id!r} is a bundled pack and cannot be edited",
            )
        member_ids = await self._validate_member_templates(
            template_ids, user_id=user_id,
        )
        updated = existing.with_fields(
            title=title,
            premise=premise,
            theme=theme,
            tone=tone,
            binding=ArcTemplateBinding(
                world_frames=tuple(world_frames),
                required_traits=tuple(required_traits),
            ),
        ).with_members(member_ids)
        await self._series_repository.save_for_user(
            updated, user_id=user_id, overwrite=True,
        )
        return await self.get_for_user(series_id, user_id=user_id)

    async def reorder_for_user(
        self,
        series_id: str,
        *,
        user_id: str,
        template_ids: list[str] | tuple[str, ...],
    ) -> ArcSeries:
        existing = await self.get_for_user(series_id, user_id=user_id)
        if existing.user_id is None:
            raise ArcSeriesValidationError(
                f"Arc series {series_id!r} is a bundled pack and cannot be edited",
            )
        member_ids = await self._validate_member_templates(
            template_ids, user_id=user_id,
        )
        reordered = existing.with_members(member_ids)
        await self._series_repository.save_for_user(
            reordered, user_id=user_id, overwrite=True,
        )
        return await self.get_for_user(series_id, user_id=user_id)

    async def delete_for_user(self, series_id: str, *, user_id: str) -> None:
        characters = await self._character_repository.list_for_user(user_id)
        removed = await self._series_repository.delete_for_user(
            series_id, user_id=user_id,
        )
        if not removed:
            raise ArcSeriesNotFoundError(f"Arc series {series_id!r} not found")
        await self._series_repository.clear_progress_for_series(series_id)
        for character in characters:
            if character.arc_series_id == series_id:
                await self._character_repository.save(
                    replace(character, arc_series_id=None),
                )

    async def bind_to_character(
        self,
        *,
        character_id: str,
        series_id: str | None,
        user_id: str,
    ) -> ArcSeries | None:
        character = await self._character_repository.get(character_id)
        if character is None or character.user_id != user_id:
            raise ArcSeriesNotFoundError(f"Character {character_id!r} not found")
        series: ArcSeries | None = None
        if series_id is not None:
            series = await self.get_for_user(series_id, user_id=user_id)
            if not series.members:
                raise ArcSeriesValidationError(
                    f"Arc series {series_id!r} must have at least one member",
                )
            existing_progress = await self._series_repository.get_progress(
                character.id,
                series.id,
            )
            should_preserve_progress = (
                character.arc_series_id == series.id
                and existing_progress is not None
                and existing_progress.status != SERIES_STATUS_CONCLUDED
            )
            if not should_preserve_progress:
                await self._series_repository.clear_progress_for_character(
                    character.id,
                )
                progress = CharacterSeriesProgress.start(
                    character_id=character.id,
                    series_id=series.id,
                )
                await self._series_repository.save_progress(progress)
        else:
            await self._series_repository.clear_progress_for_character(
                character.id,
            )
        await self._character_repository.save(
            replace(character, arc_series_id=series.id if series else None),
        )
        return series

    async def progress_for_character(
        self,
        *,
        character_id: str,
        series_id: str,
        user_id: str,
    ) -> CharacterSeriesProgress | None:
        character = await self._character_repository.get(character_id)
        if character is None or character.user_id != user_id:
            raise ArcSeriesNotFoundError(f"Character {character_id!r} not found")
        await self.get_for_user(series_id, user_id=user_id)
        return await self._series_repository.get_progress(character_id, series_id)

    async def _validate_member_templates(
        self,
        template_ids: list[str] | tuple[str, ...],
        *,
        user_id: str,
    ) -> tuple[str, ...]:
        cleaned = tuple(
            template_id.strip()
            for template_id in template_ids
            if template_id.strip()
        )
        if len(cleaned) < 2:
            raise ArcSeriesValidationError(
                "Arc series must contain at least two templates",
            )
        if len(set(cleaned)) != len(cleaned):
            raise ArcSeriesValidationError(
                "Arc series cannot contain duplicate template ids",
            )
        for template_id in cleaned:
            template = await self._template_repository.get_for_user(
                template_id, user_id=user_id,
            )
            if template is None:
                raise ArcSeriesValidationError(
                    f"Arc template {template_id!r} is not visible to this user",
                )
        return cleaned


__all__ = [
    "ArcSeriesNotFoundError",
    "ArcSeriesService",
    "ArcSeriesValidationError",
]
