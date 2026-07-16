"""Story-event seed.

A seed is a terse, one-line prompt that can be expanded by an LLM into
a full in-character "life event" for a given day. Seeds exist to solve
three linked problems:

1. Non-modern personas can't use the RSS world-event pool — RSS assumes
   modern-Earth framing.
2. Schedules regenerate with similar categories day over day; conversation
   loses novelty.
3. Without *internal* events happening to the character, they have
   nothing new to bring up and become a passive "slow response bot".

Seeds live in the DB so the UI can manage them, but the canonical source
for shipped packs is YAML files bundled with the repo (imported via
``cli.import_story_seeds`` — idempotent upsert on ``external_id``).

A seed with ``character_id=None`` is global (shared across all
characters); a seed with a specific ``character_id`` is available only
to that character.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class StorySeed:
    id: str
    seed_text: str
    """One-line Chinese prompt (e.g. ``做了個奇怪的夢，醒來記不太清楚``)."""
    tags: tuple[str, ...] = field(default_factory=tuple)
    """Free-form descriptors used for future filtering / UI search.
    Common tags: ``dream``, ``introspective``, ``outdoor``, ``gentle``,
    ``rainy-day``. Nothing enforces vocabulary."""
    world_frames: tuple[str, ...] = ("any",)
    """Which ``character.world_frame`` values this seed fits. ``any``
    means it's compatible with all frames (dreams, emotions, internal
    thoughts — universal across settings). Non-universal seeds list
    specific frames (e.g. ``["modern"]`` for a seed about checking
    social media)."""
    weight: float = 1.0
    """Relative draw weight. Higher = more likely to be picked. Used
    by the gacha service when several seeds pass the cooldown/frame
    filters."""
    cooldown_days: int = 7
    """Minimum days between the same seed firing for the same character.
    Prevents a small pack from feeling repetitive."""
    enabled: bool = True
    """Soft-disable flag so operators can mute a seed without deleting
    its history in ``story_events``."""
    character_id: str | None = None
    """When set, this seed is visible only to that character. ``None``
    = global / system seed."""
    language: str = "zh-TW"
    """Language tag of ``seed_text`` (provenance only).

    Bundled packs ship ``zh-TW``. When ``cli.import_story_seeds`` is run
    with ``--translate``, the importer localizes ``seed_text`` into the
    operator's primary language and stamps that tag here so the seed
    management UI can badge the source. Never used to filter seeds — an
    en/ja operator's pool must not empty out (the runtime expander
    already localizes generated output via the operator language hint)."""
    external_id: str | None = None
    """Idempotent import key (e.g. ``core:dream:001``). Set for seeds
    loaded from bundled YAML packs; left ``None`` for UI-created seeds
    so they don't collide with future pack updates."""
    pack_id: str | None = None
    """Origin pack identifier (e.g. ``core_universal``). Lets operators
    disable / remove a whole pack."""
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def create(
        cls,
        *,
        seed_text: str,
        tags: list[str] | tuple[str, ...] | None = None,
        world_frames: list[str] | tuple[str, ...] | None = None,
        weight: float = 1.0,
        cooldown_days: int = 7,
        enabled: bool = True,
        character_id: str | None = None,
        language: str = "zh-TW",
        external_id: str | None = None,
        pack_id: str | None = None,
    ) -> "StorySeed":
        trimmed = seed_text.strip()
        if not trimmed:
            raise ValueError("StorySeed.seed_text must be non-empty")
        return cls(
            id=str(uuid4()),
            seed_text=trimmed,
            tags=tuple(tags or ()),
            world_frames=tuple(world_frames or ("any",)),
            weight=max(0.0, float(weight)),
            cooldown_days=max(0, int(cooldown_days)),
            enabled=enabled,
            character_id=character_id,
            language=(language or "").strip() or "zh-TW",
            external_id=external_id,
            pack_id=pack_id,
        )

    def with_localized_text(self, seed_text: str, *, language: str) -> "StorySeed":
        """Return a copy with translated ``seed_text`` + language tag.

        Used by the import-time translator; blank text falls back to the
        original so a translation miss never blanks a seed."""
        cleaned = (seed_text or "").strip()
        if not cleaned:
            return self
        return replace(
            self,
            seed_text=cleaned,
            language=(language or "").strip() or self.language,
            updated_at=_utcnow(),
        )

    def with_updates(
        self,
        *,
        seed_text: str | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        world_frames: list[str] | tuple[str, ...] | None = None,
        weight: float | None = None,
        cooldown_days: int | None = None,
        enabled: bool | None = None,
    ) -> "StorySeed":
        return replace(
            self,
            seed_text=self.seed_text if seed_text is None else seed_text.strip(),
            tags=self.tags if tags is None else tuple(tags),
            world_frames=(
                self.world_frames if world_frames is None else tuple(world_frames)
            ),
            weight=(
                self.weight if weight is None else max(0.0, float(weight))
            ),
            cooldown_days=(
                self.cooldown_days if cooldown_days is None
                else max(0, int(cooldown_days))
            ),
            enabled=self.enabled if enabled is None else enabled,
            updated_at=_utcnow(),
        )

    def fits_frame(self, frame: str) -> bool:
        """True when this seed can be drawn for a character whose
        ``world_frame`` is ``frame``. Seeds tagged ``any`` fit everything.
        """
        if "any" in self.world_frames:
            return True
        return frame in self.world_frames
