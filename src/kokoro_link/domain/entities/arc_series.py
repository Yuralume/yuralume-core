"""Authored multi-template story series.

An ``ArcSeries`` is an authoring/runtime bridge: authors choose an
ordered list of existing ``ArcTemplate`` ids, while runtime still
materialises one normal ``StoryArc`` at a time. The series owns order
and completion semantics; individual templates remain reusable,
standalone blueprints.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Iterable
from uuid import uuid4

from kokoro_link.domain.entities.arc_template import ArcTemplateBinding

SERIES_STATUS_ACTIVE = "active"
SERIES_STATUS_CONCLUDED = "concluded"

_VALID_PROGRESS_STATUSES = frozenset(
    {SERIES_STATUS_ACTIVE, SERIES_STATUS_CONCLUDED},
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ArcSeriesMember:
    """One ordered template entry in a series."""

    template_id: str
    position: int

    def __post_init__(self) -> None:
        cleaned = _normalise_id(self.template_id)
        if cleaned is None:
            raise ValueError("ArcSeriesMember.template_id must be non-empty")
        if self.position < 0:
            raise ValueError("ArcSeriesMember.position must be >= 0")
        object.__setattr__(self, "template_id", cleaned)


@dataclass(frozen=True, slots=True)
class ArcSeries:
    id: str
    title: str
    premise: str
    theme: str = "custom"
    tone: str = "dramatic"
    binding: ArcTemplateBinding = field(default_factory=ArcTemplateBinding)
    members: tuple[ArcSeriesMember, ...] = ()
    user_id: str | None = None
    pack_id: str | None = None
    external_id: str | None = None
    enabled: bool = True
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        cleaned_id = _normalise_id(self.id)
        if cleaned_id is None:
            raise ValueError("ArcSeries.id must be non-empty")
        if not self.title.strip():
            raise ValueError("ArcSeries.title must be non-empty")
        if not self.premise.strip():
            raise ValueError("ArcSeries.premise must be non-empty")
        if not self.theme.strip():
            raise ValueError("ArcSeries.theme must be non-empty")
        if not self.tone.strip():
            raise ValueError("ArcSeries.tone must be non-empty")
        ordered = _normalise_members(self.members)
        if not ordered:
            raise ValueError("ArcSeries.members must contain at least one template")
        object.__setattr__(self, "id", cleaned_id)
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "premise", self.premise.strip())
        object.__setattr__(self, "theme", self.theme.strip())
        object.__setattr__(self, "tone", self.tone.strip())
        object.__setattr__(self, "members", ordered)
        object.__setattr__(self, "user_id", _normalise_optional_id(self.user_id))
        object.__setattr__(self, "pack_id", _normalise_optional_id(self.pack_id))
        object.__setattr__(
            self,
            "external_id",
            _normalise_optional_id(self.external_id),
        )

    @classmethod
    def create(
        cls,
        *,
        title: str,
        premise: str,
        id: str | None = None,
        theme: str = "custom",
        tone: str = "dramatic",
        binding: ArcTemplateBinding | None = None,
        template_ids: Iterable[str] = (),
        user_id: str | None = None,
        pack_id: str | None = None,
        external_id: str | None = None,
        enabled: bool = True,
    ) -> "ArcSeries":
        return cls(
            id=_normalise_id(id) or uuid4().hex,
            title=title,
            premise=premise,
            theme=theme,
            tone=tone,
            binding=binding or ArcTemplateBinding(),
            members=tuple(
                ArcSeriesMember(template_id=tid, position=index)
                for index, tid in enumerate(template_ids)
            ),
            user_id=user_id,
            pack_id=pack_id,
            external_id=external_id,
            enabled=enabled,
        )

    @property
    def member_template_ids(self) -> tuple[str, ...]:
        return tuple(member.template_id for member in self.members)

    @property
    def is_pack(self) -> bool:
        return self.user_id is None

    def with_members(self, template_ids: Iterable[str]) -> "ArcSeries":
        return replace(
            self,
            members=tuple(
                ArcSeriesMember(template_id=tid, position=index)
                for index, tid in enumerate(template_ids)
            ),
            updated_at=_utcnow(),
        )

    def with_fields(
        self,
        *,
        title: str | None = None,
        premise: str | None = None,
        theme: str | None = None,
        tone: str | None = None,
        binding: ArcTemplateBinding | None = None,
        enabled: bool | None = None,
    ) -> "ArcSeries":
        return replace(
            self,
            title=self.title if title is None else title,
            premise=self.premise if premise is None else premise,
            theme=self.theme if theme is None else theme,
            tone=self.tone if tone is None else tone,
            binding=self.binding if binding is None else binding,
            enabled=self.enabled if enabled is None else enabled,
            updated_at=_utcnow(),
        )


@dataclass(frozen=True, slots=True)
class CharacterSeriesProgress:
    character_id: str
    series_id: str
    current_index: int = 0
    status: str = SERIES_STATUS_ACTIVE
    last_arc_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        character_id = _normalise_id(self.character_id)
        series_id = _normalise_id(self.series_id)
        if character_id is None:
            raise ValueError("CharacterSeriesProgress.character_id must be non-empty")
        if series_id is None:
            raise ValueError("CharacterSeriesProgress.series_id must be non-empty")
        if self.current_index < 0:
            raise ValueError("CharacterSeriesProgress.current_index must be >= 0")
        if self.status not in _VALID_PROGRESS_STATUSES:
            raise ValueError(
                "CharacterSeriesProgress.status must be one of "
                f"{sorted(_VALID_PROGRESS_STATUSES)}",
            )
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "series_id", series_id)
        object.__setattr__(
            self,
            "last_arc_id",
            _normalise_optional_id(self.last_arc_id),
        )

    @classmethod
    def start(cls, *, character_id: str, series_id: str) -> "CharacterSeriesProgress":
        return cls(character_id=character_id, series_id=series_id)

    def with_started_member(
        self, *, index: int, arc_id: str,
    ) -> "CharacterSeriesProgress":
        return replace(
            self,
            current_index=max(0, index),
            status=SERIES_STATUS_ACTIVE,
            last_arc_id=arc_id,
            updated_at=_utcnow(),
        )

    def next_member(self) -> "CharacterSeriesProgress":
        return replace(
            self,
            current_index=self.current_index + 1,
            status=SERIES_STATUS_ACTIVE,
            updated_at=_utcnow(),
        )

    def concluded(self) -> "CharacterSeriesProgress":
        return replace(
            self,
            status=SERIES_STATUS_CONCLUDED,
            updated_at=_utcnow(),
        )


def _normalise_members(
    members: Iterable[ArcSeriesMember],
) -> tuple[ArcSeriesMember, ...]:
    seen: set[str] = set()
    ordered: list[ArcSeriesMember] = []
    for raw in sorted(members, key=lambda member: member.position):
        template_id = _normalise_id(raw.template_id)
        if template_id is None or template_id in seen:
            continue
        seen.add(template_id)
        ordered.append(
            ArcSeriesMember(template_id=template_id, position=len(ordered)),
        )
    return tuple(ordered)


def _normalise_id(value: object) -> str | None:
    if value is None or not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_optional_id(value: object) -> str | None:
    return _normalise_id(value)


__all__ = [
    "ArcSeries",
    "ArcSeriesMember",
    "CharacterSeriesProgress",
    "SERIES_STATUS_ACTIVE",
    "SERIES_STATUS_CONCLUDED",
]
