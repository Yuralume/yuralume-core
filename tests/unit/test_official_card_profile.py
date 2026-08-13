"""The prose/structure seam of an official card (EC4-C).

Cloud publishes an exclusive card as two documents: the translatable field
set (prose, per locale, human-approved) and the card's own ``manifest.json``
(everything that must never be translated). ``CATALOG_PROSE_FIELDS`` is the
line between them, and it is the kind of line that goes wrong silently — a
structural key overwritten by a default, or an approved Japanese name thrown
away in favour of the zh-Hant manifest sitting beside it. So it is pinned
here rather than trusted to stay in step by inspection.
"""

from __future__ import annotations

from kokoro_link.application.dto.character_card import CharacterCardProfile
from kokoro_link.application.services.official_card_profile import (
    CATALOG_PROSE_FIELDS,
    card_profile_from_catalog,
    card_profile_with_document_structure,
)

_PROSE = {
    "name": "美緒",
    "summary": "咖啡廳打工女大生",
    "personality": ["開朗"],
    "interests": ["拉花"],
    "speaking_style": "輕快",
    "boundaries": ["不談前男友"],
    "aspirations": ["開自己的店"],
    "appearance": "短髮",
    "gender_identity": "女性",
    "third_person_pronoun": "她",
    "visual_gender_presentation": "feminine young woman",
    "world_topics": ["咖啡"],
    "excluded_topics": ["政治"],
    "personality_type": {"code": "ENFP", "rationale": "外向直覺"},
    "companions": [{"name": "小春", "role": "同事"}],
}

_DOCUMENT = {
    "schema_version": 1,
    "card": {"title": "", "author": "Yuralume"},
    "character": {
        "name": "美緒",
        "summary": "咖啡廳打工女大生",
        "world_frame": "school",
        "allowed_tools": ["generate_image"],
        "date_of_birth": "2004-03-09",
        "disposition": {"self_centeredness": "high"},
        "visual_subject_type": "anthropomorphic",
        "proactive_daily_limit": 7,
    },
    "stage_images": ["assets/stage/0.png"],
}


def test_the_prose_field_list_is_exactly_what_the_catalog_projection_fills(
) -> None:
    """The list and the projection must not drift apart.

    Every entry of ``_PROSE`` is deliberately away from the DTO's default,
    so a field the projection stopped setting — or started setting without
    being declared prose — shows up here as a set difference rather than as
    a mis-installed character six months later.
    """
    projected = card_profile_from_catalog(_PROSE, fallback_name="fallback")
    baseline = CharacterCardProfile(name="")

    filled = {
        name for name in CharacterCardProfile.model_fields
        if getattr(projected, name) != getattr(baseline, name)
    }

    assert filled == set(CATALOG_PROSE_FIELDS)


def test_structure_comes_from_the_manifest_and_words_from_the_catalog() -> None:
    prose = card_profile_from_catalog(_PROSE, fallback_name="fallback")
    prose = prose.model_copy(update={"name": "ミオ", "summary": "ja summary"})

    merged = card_profile_with_document_structure(prose, _DOCUMENT)

    assert merged.name == "ミオ"
    assert merged.summary == "ja summary"
    assert [c.name for c in merged.companions] == ["小春"]
    assert merged.world_frame == "school"
    assert merged.allowed_tools == ["generate_image"]
    assert merged.visual_subject_type == "anthropomorphic"
    assert merged.proactive_daily_limit == 7
    assert merged.disposition.self_centeredness == "high"
    assert merged.date_of_birth is not None
    assert merged.date_of_birth.isoformat() == "2004-03-09"


def test_arc_refs_are_dropped_because_the_templates_never_travelled() -> None:
    # The document is the manifest alone — a `.lumecard`'s arc templates are
    # separate zip members that stay on the Cloud side.
    document = {
        **_DOCUMENT,
        "character": {
            **_DOCUMENT["character"],
            "arc_template_ref": "mio-arc",
            "arc_series_ref": "mio-series",
        },
    }

    merged = card_profile_with_document_structure(
        card_profile_from_catalog(_PROSE, fallback_name="fallback"), document,
    )

    assert merged.arc_template_ref is None
    assert merged.arc_series_ref is None


def test_an_unusable_document_leaves_the_prose_profile_alone() -> None:
    prose = card_profile_from_catalog(_PROSE, fallback_name="fallback")

    for document in (
        None,
        {},
        "not-a-mapping",
        {"schema_version": 99, "character": {"name": "美緒"}},
        {"schema_version": 1, "character": "not-an-object"},
        {"schema_version": 1},
    ):
        assert card_profile_with_document_structure(prose, document) == prose
