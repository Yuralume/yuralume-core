"""Unit tests for the shared memory-line rendering helpers.

These helpers were extracted from ``infrastructure/prompt/default.py``
so encounter/background surfaces share the exact chat rendering. The
assertions here lock the participant tag + relative-time anchor
behaviour that the chat prompt builder tests exercise indirectly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.prompt.memory_lines import (
    PLAYER_DISCLOSED_FRAME,
    PLAYER_UNAWARE_FRAME,
    format_memory_line,
    memory_time_tag,
)


def _memory(
    content: str = "小鈴說她在神社打工",
    *,
    created_at: datetime | None = None,
    participants: tuple[ParticipantRef, ...] = (),
) -> MemoryItem:
    return MemoryItem.create(
        character_id="char-a",
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=0.5,
        created_at=created_at or datetime.now(timezone.utc),
        participants=participants,
    )


def test_time_tag_renders_relative_anchor() -> None:
    now = datetime.now(timezone.utc)
    item = _memory(created_at=now - timedelta(days=2))
    tag = memory_time_tag(item, now)
    assert tag.startswith("（約") and tag.endswith("前）")


def test_time_tag_empty_without_reference_clock() -> None:
    item = _memory()
    assert memory_time_tag(item, None) == ""


def test_time_tag_empty_for_future_timestamp() -> None:
    now = datetime.now(timezone.utc)
    item = _memory(created_at=now + timedelta(minutes=30))
    assert memory_time_tag(item, now) == ""


def test_time_tag_assumes_utc_for_naive_created_at() -> None:
    now = datetime.now(timezone.utc)
    naive = (now - timedelta(hours=3)).replace(tzinfo=None)
    item = _memory(created_at=naive)
    assert memory_time_tag(item, now) != ""


def test_format_line_without_participants_has_no_tag() -> None:
    now = datetime.now(timezone.utc)
    item = _memory(created_at=now - timedelta(days=1))
    line = format_memory_line(item, now=now)
    assert line.startswith("- 小鈴說她在神社打工")
    assert "[與" not in line
    assert "（約" in line


def test_format_line_prefixes_participants() -> None:
    item = _memory(
        participants=(
            ParticipantRef(
                actor_kind="character",
                actor_id="char-b",
                display_name="芊璃",
                role="encounter_partner",
            ),
        ),
    )
    line = format_memory_line(item)
    assert line.startswith("- [與 芊璃 一起] ")


def test_format_line_caps_participant_names_at_three() -> None:
    participants = tuple(
        ParticipantRef(
            actor_kind="character",
            actor_id=f"char-{i}",
            display_name=f"角色{i}",
            role="peer",
        )
        for i in range(5)
    )
    line = format_memory_line(_memory(participants=participants))
    assert "等 5 人" in line
    assert "角色3" not in line


def test_default_builder_uses_shared_helpers() -> None:
    # The chat builder must alias the shared functions, not keep a
    # drifting private copy.
    from kokoro_link.infrastructure.prompt import default as default_mod

    assert default_mod._memory_time_tag is memory_time_tag


# --- KB7: the player-knowledge frame ---------------------------------


def _judged(value: str, **kwargs: object) -> MemoryItem:
    return MemoryItem.create(
        character_id="char-a",
        kind=MemoryKind.EPISODIC,
        content="一個人走進林道，回來時手上全是擦傷",
        salience=0.5,
        player_knowledge=value,
        **kwargs,  # type: ignore[arg-type]
    )


def test_private_memory_is_framed_as_something_the_player_never_saw() -> None:
    """The incident line: recall handed the model a solo rescue with no
    marking, and it came back as 「你是不是又去了山區」."""
    line = format_memory_line(_judged("private"))

    assert PLAYER_UNAWARE_FRAME in line
    assert line.endswith("一個人走進林道，回來時手上全是擦傷")


def test_disclosed_memory_is_marked_as_already_told() -> None:
    line = format_memory_line(_judged("disclosed"))

    assert PLAYER_DISCLOSED_FRAME in line
    assert PLAYER_UNAWARE_FRAME not in line


def test_shared_and_legacy_memories_render_byte_identically() -> None:
    """``shared`` and the unjudged ``""`` must stay pre-ledger output —
    this is what makes the frozen chat goldens (and the whole unjudged
    back catalogue) unaffected by KB5/KB6 landing."""
    now = datetime.now(timezone.utc)
    shared = _judged("shared", created_at=now - timedelta(days=1))
    legacy = _judged("", created_at=now - timedelta(days=1))

    rendered = format_memory_line(shared, now=now)

    assert rendered == format_memory_line(legacy, now=now)
    assert rendered.startswith("- 一個人走進林道")
    assert "【" not in rendered


def test_unknown_ledger_value_degrades_to_no_frame() -> None:
    """A value outside the domain normalises to ``""`` at the entity
    boundary, so a future write station that mislabels can only lose the
    frame, never invent one."""
    assert "【" not in format_memory_line(_judged("kind-of-private"))


def test_the_frame_is_off_for_surfaces_that_are_not_about_the_player() -> None:
    """Peer-knowledge consolidation reasons about what another character
    knows — an axis this ledger does not describe (plan §3.2)."""
    line = format_memory_line(_judged("private"), knowledge_frame=False)

    assert line == "- 一個人走進林道，回來時手上全是擦傷"


def test_kind_who_and_knowledge_render_in_one_stable_order() -> None:
    """The proactive decider needs its kind bucket back after converging
    on this renderer; it has to coexist with the other two prefixes."""
    item = MemoryItem.create(
        character_id="char-a",
        kind=MemoryKind.EPISODIC,
        content="在林道遇到巡山員",
        salience=0.5,
        player_knowledge="private",
        participants=(
            ParticipantRef(
                actor_kind="character",
                actor_id="char-b",
                display_name="巡山員",
                role="peer",
            ),
        ),
    )

    line = format_memory_line(item, include_kind=True)

    assert line == (
        f"- [episodic] [與 巡山員 一起] {PLAYER_UNAWARE_FRAME} 在林道遇到巡山員"
    )


def test_kind_tag_is_opt_in() -> None:
    assert "[episodic]" not in format_memory_line(_judged(""))
