"""Renders the most recent per-pair address change as a relationship-event
line for the chat prompt.

A rename never rewrites history — old memories keep the old name. Instead
the single most recent change per direction surfaces here (plan §4 cap of
one event per direction) so the character can naturally acknowledge the new
term and link older references to the same person.

Pure functions: the caller (``ChatService``) fetches the latest events from
``AddressChangeLogRepositoryPort`` and passes them in, so this module does
no I/O and stays trivially testable.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo

from kokoro_link.domain.value_objects.address_change_event import (
    DIRECTION_CHARACTER,
    DIRECTION_PLAYER,
    AddressChangeEvent,
)
from kokoro_link.domain.value_objects.timezone import to_timezone


_HEADER = "稱呼變更（關係事件，請自然 acknowledge，不要照稿念）："


def render_address_change_lines(
    *,
    player_event: "AddressChangeEvent | None" = None,
    character_event: "AddressChangeEvent | None" = None,
    local_tz: tzinfo = timezone.utc,
) -> list[str]:
    """Render at most one bullet per direction for the latest rename.

    ``player_event`` is direction A (how the character addresses the
    player); ``character_event`` is direction B (how the player addresses
    the character). Either may be ``None`` (no rename recorded). Returns
    ``[]`` when neither direction has a usable event so the prompt stays
    quiet for the common no-rename case.
    """
    bullets: list[str] = []
    if player_event is not None and player_event.direction == DIRECTION_PLAYER:
        bullets.append(_player_line(player_event, local_tz))
    if (
        character_event is not None
        and character_event.direction == DIRECTION_CHARACTER
    ):
        bullets.append(_character_line(character_event, local_tz))
    if not bullets:
        return []
    return [_HEADER, *bullets]


def _player_line(event: AddressChangeEvent, local_tz: tzinfo) -> str:
    when = _format_date(event.effective_at, local_tz)
    lead = (
        f"- 使用者自 {when} 起希望你改稱呼他為「{event.new_value}」"
        if when
        else f"- 使用者現在希望你改稱呼他為「{event.new_value}」"
    )
    return lead + _old_suffix(event.old_value)


def _character_line(event: AddressChangeEvent, local_tz: tzinfo) -> str:
    when = _format_date(event.effective_at, local_tz)
    lead = (
        f"- 使用者自 {when} 起改用「{event.new_value}」稱呼你"
        if when
        else f"- 使用者現在改用「{event.new_value}」稱呼你"
    )
    return lead + _old_suffix(event.old_value)


def _old_suffix(old_value: str) -> str:
    if not old_value:
        return "。"
    return (
        f"，先前是「{old_value}」；歷史對話或記憶中出現舊稱呼時，"
        "指的是同一個人。"
    )


def _format_date(dt: datetime | None, local_tz: tzinfo) -> str:
    if dt is None:
        return ""
    local = to_timezone(dt, local_tz)
    return f"{local.month}/{local.day}"
