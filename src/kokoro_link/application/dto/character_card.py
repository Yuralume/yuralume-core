"""Character card (``.lumecard``) manifest DTO.

``manifest.json`` carries only the **A-layer** portable settings —
the parts of a character that are authored at creation time and mean
the same thing on any deployment. Deployment-bound routing (B-layer:
``feature_models`` / image / video profiles, ``voice_profile``,
``loras``) and runtime accumulation (C-layer: ``state`` / memory /
persona / schedule / feed / ...) are intentionally absent. See
``docs/CHARACTER_CARD_PLAN.md`` §2.

The schema is versioned via :data:`CHARACTER_CARD_SCHEMA_VERSION` so a
future exporter can bump the shape while older importers reject what
they can't read.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from pydantic import BaseModel, Field

from kokoro_link.application.dto.character import (
    CharacterCompanionPayload,
    CharacterDispositionPayload,
    CharacterPersonalityTypePayload,
    CharacterStatePayload,
    CreateCharacterRequest,
)
from kokoro_link.domain.entities.arc_series import ArcSeries
from kokoro_link.domain.entities.character import Character, DEFAULT_ALLOWED_TOOLS
from kokoro_link.domain.value_objects.visual_subject import (
    DEFAULT_VISUAL_SUBJECT_TYPE,
    VisualSubjectType,
    normalise_visual_subject_type,
)
from kokoro_link.domain.value_objects.visual_generation_style import (
    normalise_character_visual_generation_style,
)

CHARACTER_CARD_SCHEMA_VERSION = 1


class CharacterCardMeta(BaseModel):
    """Card-level metadata (marketplace / provenance), not character
    behaviour. ``note`` is the author's free-text advice about B-layer
    setup the card can't carry (e.g. "built for an anime image profile")
    — purely informational, never auto-applied on import."""

    title: str = ""
    author: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    app_version: str = ""
    created_at: str = ""


class CharacterCardProfile(BaseModel):
    """The portable A-layer character settings.

    Mirrors the subset of :class:`CreateCharacterRequest` that is safe
    to carry across deployments. Notably excludes ``image_urls`` (carried
    as bundled assets, re-uploaded on import), ``arc_template_id``
    (carried as :attr:`arc_template_ref` pointing at a bundled template),
    and everything in the B / C layers.
    """

    name: str
    summary: str = ""
    personality: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    speaking_style: str = "natural"
    boundaries: list[str] = Field(default_factory=list)
    aspirations: list[str] = Field(default_factory=list)
    appearance: str = ""
    gender_identity: str = ""
    third_person_pronoun: str = ""
    visual_gender_presentation: str = ""
    visual_subject_type: VisualSubjectType = DEFAULT_VISUAL_SUBJECT_TYPE
    visual_generation_style: str = ""
    date_of_birth: date | None = None
    disposition: CharacterDispositionPayload = Field(
        default_factory=CharacterDispositionPayload,
    )
    personality_type: CharacterPersonalityTypePayload = Field(
        default_factory=CharacterPersonalityTypePayload,
    )
    world_frame: str = "modern"
    world_awareness_enabled: bool = False
    world_topics: list[str] = Field(default_factory=list)
    subscribed_categories: list[str] = Field(default_factory=list)
    excluded_topics: list[str] = Field(default_factory=list)
    proactive_enabled: bool = True
    proactive_daily_limit: int = Field(default=3, ge=0, le=50)
    proactive_cooldown_minutes: int = Field(default=30, ge=1, le=24 * 60)
    accepts_web_proactive: bool = True
    feed_daily_limit: int = Field(default=3, ge=0, le=50)
    allowed_tools: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS),
    )
    companions: list[CharacterCompanionPayload] = Field(default_factory=list)
    arc_template_ref: str | None = None
    """Id of a bundled arc template (one of
    :attr:`CharacterCardManifest.bundled_arc_templates`) this character
    should bind to on import, or ``None`` for no main arc."""
    arc_series_ref: str | None = None
    """Id of a bundled arc series (one of
    :attr:`CharacterCardManifest.bundled_arc_series`) this character
    should bind to on import, or ``None`` for no fixed continuation series."""

    @classmethod
    def from_domain(
        cls,
        character: Character,
        *,
        arc_template_ref: str | None,
        arc_series_ref: str | None = None,
    ) -> "CharacterCardProfile":
        """Project a domain ``Character`` into the portable profile.

        ``arc_template_ref`` is supplied by the export service: it is the
        character's ``arc_template_id`` only when that template was
        actually bundled into the card (a dangling ref would import to a
        character pointing at a template the importer doesn't have)."""
        return cls(
            name=character.name,
            summary=character.summary,
            personality=list(character.personality),
            interests=list(character.interests),
            speaking_style=character.speaking_style,
            boundaries=list(character.boundaries),
            aspirations=list(character.aspirations),
            appearance=character.appearance,
            gender_identity=character.gender_identity,
            third_person_pronoun=character.third_person_pronoun,
            visual_gender_presentation=character.visual_gender_presentation,
            visual_subject_type=normalise_visual_subject_type(
                character.visual_subject_type,
            ),
            visual_generation_style=normalise_character_visual_generation_style(
                character.visual_generation_style,
            ),
            date_of_birth=character.date_of_birth,
            disposition=CharacterDispositionPayload.from_domain(
                character.disposition,
            ),
            personality_type=CharacterPersonalityTypePayload.from_domain(
                character.personality_type,
            ),
            world_frame=character.world_frame or "modern",
            world_awareness_enabled=character.world_awareness_enabled,
            world_topics=list(character.world_topics),
            subscribed_categories=list(character.subscribed_categories),
            excluded_topics=list(character.excluded_topics),
            proactive_enabled=character.proactive_enabled,
            proactive_daily_limit=character.proactive_daily_limit,
            proactive_cooldown_minutes=character.proactive_cooldown_minutes,
            accepts_web_proactive=character.accepts_web_proactive,
            feed_daily_limit=character.feed_daily_limit,
            allowed_tools=list(character.allowed_tools),
            companions=[
                CharacterCompanionPayload.from_domain(c)
                for c in character.companions
            ],
            arc_template_ref=arc_template_ref,
            arc_series_ref=arc_series_ref,
        )

    def to_create_request(
        self,
        *,
        image_urls: list[str],
        arc_template_id: str | None,
        arc_series_id: str | None = None,
    ) -> CreateCharacterRequest:
        """Build a ``CreateCharacterRequest`` for the import path.

        ``image_urls`` are the freshly re-uploaded stage assets (in the
        importer's storage) and ``arc_template_id`` is the resolved
        local id of the bundled template (after any collision remap).
        Every B / C layer field is left at its default → the imported
        character starts from zero on the importer's deployment."""
        return CreateCharacterRequest(
            name=self.name,
            summary=self.summary,
            personality=list(self.personality),
            interests=list(self.interests),
            speaking_style=self.speaking_style,
            boundaries=list(self.boundaries),
            aspirations=list(self.aspirations),
            appearance=self.appearance,
            gender_identity=self.gender_identity,
            third_person_pronoun=self.third_person_pronoun,
            visual_gender_presentation=self.visual_gender_presentation,
            visual_subject_type=normalise_visual_subject_type(
                self.visual_subject_type,
            ),
            visual_generation_style=normalise_character_visual_generation_style(
                self.visual_generation_style,
            ),
            date_of_birth=self.date_of_birth,
            image_urls=list(image_urls),
            allowed_tools=list(self.allowed_tools),
            initial_state=CharacterStatePayload(),
            proactive_enabled=self.proactive_enabled,
            proactive_daily_limit=self.proactive_daily_limit,
            proactive_cooldown_minutes=self.proactive_cooldown_minutes,
            feed_daily_limit=self.feed_daily_limit,
            world_awareness_enabled=self.world_awareness_enabled,
            world_topics=list(self.world_topics),
            subscribed_categories=list(self.subscribed_categories),
            excluded_topics=list(self.excluded_topics),
            world_frame=self.world_frame,
            accepts_web_proactive=self.accepts_web_proactive,
            arc_template_id=arc_template_id,
            arc_series_id=arc_series_id,
            companions=list(self.companions),
            disposition=self.disposition,
            personality_type=self.personality_type,
        )


class CharacterCardArcSeriesBinding(BaseModel):
    """Portable authoring constraints for a bundled ``ArcSeries``."""

    world_frames: list[str] = Field(default_factory=list)
    required_traits: list[str] = Field(default_factory=list)


class CharacterCardArcSeriesMember(BaseModel):
    """One member template reference inside a bundled ``ArcSeries``."""

    template_ref: str
    position: int


class CharacterCardArcSeriesBundle(BaseModel):
    """Authoring-layer ``ArcSeries`` projection carried in ``manifest.json``.

    This intentionally excludes per-character progress and any runtime
    artifacts. Member refs point at bundled arc templates and are rewired
    on import after template id collision handling.
    """

    id: str
    title: str
    premise: str
    theme: str = "custom"
    tone: str = "dramatic"
    binding: CharacterCardArcSeriesBinding = Field(
        default_factory=CharacterCardArcSeriesBinding,
    )
    members: list[CharacterCardArcSeriesMember] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, series: ArcSeries) -> "CharacterCardArcSeriesBundle":
        return cls(
            id=series.id,
            title=series.title,
            premise=series.premise,
            theme=series.theme,
            tone=series.tone,
            binding=CharacterCardArcSeriesBinding(
                world_frames=list(series.binding.world_frames),
                required_traits=list(series.binding.required_traits),
            ),
            members=[
                CharacterCardArcSeriesMember(
                    template_ref=member.template_id,
                    position=member.position,
                )
                for member in series.members
            ],
        )


class CharacterCardManifest(BaseModel):
    """Top-level ``manifest.json`` schema."""

    schema_version: int = CHARACTER_CARD_SCHEMA_VERSION
    card: CharacterCardMeta = Field(default_factory=CharacterCardMeta)
    character: CharacterCardProfile
    stage_images: list[str] = Field(default_factory=list)
    """In-zip member paths of the stage carousel images, in order
    (e.g. ``["assets/stage/0.png", "assets/stage/1.jpg"]``)."""
    bundled_arc_templates: list[str] = Field(default_factory=list)
    """Ids of the arc templates bundled under ``arc_templates/``."""
    bundled_arc_series: list[CharacterCardArcSeriesBundle] = Field(
        default_factory=list,
    )
    """Authored ArcSeries definitions bundled in ``manifest.json``."""


class CharacterCardPreviewCompanion(BaseModel):
    """Player-safe companion projection for card previews."""

    name: str = ""
    role: str = ""


class CharacterCardPreview(BaseModel):
    """Display projection shared by bundled packs and upload previews.

    This is still pure A-layer data. It intentionally carries no
    deployment routing, memory, persona, chat, schedule, emotion, or
    ownership state; confirming an import still runs the existing
    ``import_card`` path that creates a brand-new character.
    """

    pack_id: str | None = None
    title: str = ""
    author: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    name: str
    summary: str = ""
    personality: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    speaking_style: str = "natural"
    boundaries: list[str] = Field(default_factory=list)
    aspirations: list[str] = Field(default_factory=list)
    appearance: str = ""
    gender_identity: str = ""
    third_person_pronoun: str = ""
    visual_gender_presentation: str = ""
    visual_subject_type: VisualSubjectType = DEFAULT_VISUAL_SUBJECT_TYPE
    visual_generation_style: str = ""
    date_of_birth: date | None = None
    disposition: CharacterDispositionPayload = Field(
        default_factory=CharacterDispositionPayload,
    )
    personality_type: CharacterPersonalityTypePayload = Field(
        default_factory=CharacterPersonalityTypePayload,
    )
    world_frame: str = "modern"
    world_awareness_enabled: bool = False
    world_topics: list[str] = Field(default_factory=list)
    subscribed_categories: list[str] = Field(default_factory=list)
    excluded_topics: list[str] = Field(default_factory=list)
    proactive_enabled: bool = True
    proactive_daily_limit: int = 3
    proactive_cooldown_minutes: int = 30
    accepts_web_proactive: bool = True
    feed_daily_limit: int = 3
    companions: list[CharacterCardPreviewCompanion] = Field(default_factory=list)
    has_main_arc: bool = False
    bundled_arc_template_count: int = 0
    bundled_arc_titles: list[str] = Field(default_factory=list)
    has_arc_series: bool = False
    bundled_arc_series_count: int = 0
    bundled_arc_series_titles: list[str] = Field(default_factory=list)
    bundled_arc_series_member_count: int = 0
    stage_image_count: int = 0
    image_urls: list[str] = Field(default_factory=list)
    source_format: str = "lumecard"
    """Which upload format this preview came from — ``"lumecard"`` for the
    native path (and bundled packs) or ``"sillytavern"`` when it was
    converted from a SillyTavern card. The frontend uses this to show the
    "AI-normalised" notice only for converted cards."""
    dropped_fields: list[str] = Field(default_factory=list)
    """Stable markers for ST fields the import intentionally dropped
    (``character_book`` / ``greetings`` / ``extra_assets``); empty for the
    native ``.lumecard`` path. Surfaced in the preview drop-notice (D7)."""
    suggested_known_context: str = ""
    """Neutral rewrite of a SillyTavern ``scenario`` used to *pre-fill* the
    initial-relationship wizard (D5). Never auto-applied — the importer
    confirms it. Empty for the native ``.lumecard`` path."""


def build_card_preview(
    manifest: CharacterCardManifest,
    *,
    image_url_fn: Callable[[int, str], str | None],
    pack_id: str | None = None,
    bundled_arc_titles: list[str] | None = None,
    prefer_profile_text: bool = False,
) -> CharacterCardPreview:
    """Project a validated manifest into the shared preview DTO.

    ``image_url_fn`` lets each caller decide how an image is addressed:
    bundled packs use a streaming endpoint, upload previews use an
    inline data URL. Returning ``None`` skips a missing / unusable image
    without dropping the rest of the card.
    """
    image_urls: list[str] = []
    for index, member_path in enumerate(manifest.stage_images):
        url = image_url_fn(index, member_path)
        if url:
            image_urls.append(url)

    arc_titles = list(bundled_arc_titles or manifest.bundled_arc_templates)
    profile = manifest.character
    return CharacterCardPreview(
        pack_id=pack_id,
        title=profile.name if prefer_profile_text else manifest.card.title or profile.name,
        author=manifest.card.author,
        description=profile.summary if prefer_profile_text else manifest.card.description,
        tags=list(manifest.card.tags),
        note=manifest.card.note,
        name=profile.name,
        summary=profile.summary,
        personality=list(profile.personality),
        interests=list(profile.interests),
        speaking_style=profile.speaking_style,
        boundaries=list(profile.boundaries),
        aspirations=list(profile.aspirations),
        appearance=profile.appearance,
        gender_identity=profile.gender_identity,
        third_person_pronoun=profile.third_person_pronoun,
        visual_gender_presentation=profile.visual_gender_presentation,
        visual_subject_type=normalise_visual_subject_type(
            profile.visual_subject_type,
        ),
        visual_generation_style=normalise_character_visual_generation_style(
            profile.visual_generation_style,
        ),
        date_of_birth=profile.date_of_birth,
        disposition=profile.disposition,
        personality_type=profile.personality_type,
        world_frame=profile.world_frame,
        world_awareness_enabled=profile.world_awareness_enabled,
        world_topics=list(profile.world_topics),
        subscribed_categories=list(profile.subscribed_categories),
        excluded_topics=list(profile.excluded_topics),
        proactive_enabled=profile.proactive_enabled,
        proactive_daily_limit=profile.proactive_daily_limit,
        proactive_cooldown_minutes=profile.proactive_cooldown_minutes,
        accepts_web_proactive=profile.accepts_web_proactive,
        feed_daily_limit=profile.feed_daily_limit,
        companions=[
            CharacterCardPreviewCompanion(name=c.name, role=c.role)
            for c in profile.companions
        ],
        has_main_arc=bool(profile.arc_template_ref),
        bundled_arc_template_count=len(manifest.bundled_arc_templates),
        bundled_arc_titles=arc_titles,
        has_arc_series=bool(profile.arc_series_ref),
        bundled_arc_series_count=len(manifest.bundled_arc_series),
        bundled_arc_series_titles=[
            series.title or series.id
            for series in manifest.bundled_arc_series
        ],
        bundled_arc_series_member_count=sum(
            len(series.members)
            for series in manifest.bundled_arc_series
        ),
        stage_image_count=len(image_urls),
        image_urls=image_urls,
    )
