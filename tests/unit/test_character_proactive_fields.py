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


def test_defaults_for_proactive_fields() -> None:
    character = _character()
    assert character.proactive_enabled is True
    assert character.proactive_daily_limit == 3
    assert character.proactive_cooldown_minutes == 30


def test_create_clamps_daily_limit_range() -> None:
    low = _character(proactive_daily_limit=-5)
    assert low.proactive_daily_limit == 0
    high = _character(proactive_daily_limit=9999)
    assert high.proactive_daily_limit == 50


def test_create_clamps_cooldown_range() -> None:
    low = _character(proactive_cooldown_minutes=0)
    assert low.proactive_cooldown_minutes == 1
    high = _character(proactive_cooldown_minutes=10 ** 6)
    assert high.proactive_cooldown_minutes == 24 * 60


def test_update_respects_proactive_fields() -> None:
    character = _character()
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
        proactive_enabled=False,
        proactive_daily_limit=5,
        proactive_cooldown_minutes=45,
    )
    assert updated.proactive_enabled is False
    assert updated.proactive_daily_limit == 5
    assert updated.proactive_cooldown_minutes == 45


def test_update_leaves_proactive_fields_alone_when_none() -> None:
    character = _character(
        proactive_enabled=False, proactive_daily_limit=2, proactive_cooldown_minutes=60,
    )
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, state=None,
    )
    assert updated.proactive_enabled is False
    assert updated.proactive_daily_limit == 2
    assert updated.proactive_cooldown_minutes == 60
