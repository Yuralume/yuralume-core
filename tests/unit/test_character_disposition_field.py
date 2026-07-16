"""Wire-through tests for ``Character.disposition``.

Covers the entity-level surface: default value, ``create()`` kwarg,
``update()`` leave-alone-when-None semantics. Persistence round-trip is
covered separately by ``tests/integration/test_sa_repositories.py``.
"""

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.disposition import CharacterDisposition


def _character(**overrides: object) -> Character:
    kwargs: dict[str, object] = {
        "name": "Mio",
        "summary": "",
        "personality": [],
        "interests": [],
        "speaking_style": "",
        "boundaries": [],
        "state": CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    }
    kwargs.update(overrides)
    return Character.create(**kwargs)  # type: ignore[arg-type]


def test_default_disposition_is_all_medium() -> None:
    character = _character()
    assert character.disposition.is_default is True


def test_create_accepts_disposition() -> None:
    disposition = CharacterDisposition(
        self_centeredness="high", sharing_drive="high",
    )
    character = _character(disposition=disposition)
    assert character.disposition.self_centeredness == "high"
    assert character.disposition.sharing_drive == "high"
    assert character.disposition.candor == "medium"


def test_update_leaves_disposition_alone_when_none() -> None:
    original = CharacterDisposition(candor="low")
    character = _character(disposition=original)
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
        disposition=None,
    )
    assert updated.disposition is original or updated.disposition == original


def test_update_overrides_disposition_when_provided() -> None:
    character = _character()
    new_disposition = CharacterDisposition(associativeness="high")
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
        disposition=new_disposition,
    )
    assert updated.disposition.associativeness == "high"
    assert updated.disposition.self_centeredness == "medium"
