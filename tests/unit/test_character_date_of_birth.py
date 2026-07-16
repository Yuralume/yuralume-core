"""Domain-level tests for ``Character.date_of_birth``.

Covers entity defaults, ``create`` propagation, ``update``'s tri-state
(omit / clear / set), and the ``birthday_context`` convenience method.
"""

from datetime import date

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState


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


def test_default_date_of_birth_is_none() -> None:
    character = _character()
    assert character.date_of_birth is None


def test_create_with_date_of_birth() -> None:
    dob = date(2000, 6, 15)
    character = _character(date_of_birth=dob)
    assert character.date_of_birth == dob


def test_update_omit_keeps_existing_birthday() -> None:
    dob = date(2000, 6, 15)
    character = _character(date_of_birth=dob)
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
    )
    assert updated.date_of_birth == dob


def test_update_explicit_none_clears_birthday() -> None:
    character = _character(date_of_birth=date(2000, 6, 15))
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
        date_of_birth=None,
    )
    assert updated.date_of_birth is None


def test_update_can_set_new_birthday() -> None:
    character = _character()
    new_dob = date(1998, 3, 21)
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
        date_of_birth=new_dob,
    )
    assert updated.date_of_birth == new_dob


def test_birthday_context_none_when_unset() -> None:
    character = _character()
    assert character.birthday_context(date(2026, 1, 1)) is None


def test_birthday_context_derives_from_dob() -> None:
    character = _character(date_of_birth=date(2000, 6, 15))
    ctx = character.birthday_context(date(2026, 6, 15))
    assert ctx is not None
    assert ctx.age == 26
    assert ctx.zodiac == "雙子座"
    assert ctx.is_today is True
