"""Presence-frame block rendering after the stage-access judge retired.

Stage turns are no longer gated by an out-of-narrative verdict (SA series,
2026-07-30). ``web_stage`` means "the player declared this turn is a
same-place interaction"; plausibility is handled *inside* the narrative by
the character anchoring its own schedule. These tests are the seam for
that wording plus the operator ``current_status`` context injection (D4).
"""

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.presence_frame import AccessContext, PresenceFrame
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder

_LEGACY_STAGE_CONTEXTS = (
    AccessContext.REMOTE_STAGE,
    AccessContext.PUBLIC_ENCOUNTER,
    AccessContext.INVITED_VISIT,
    AccessContext.SCHEDULED_MEETUP,
    AccessContext.ESTABLISHED_ROUTINE,
    AccessContext.NOT_PLAUSIBLE,
)

_NOW = datetime(2026, 7, 30, 21, 40, tzinfo=timezone.utc)


def _make_character() -> Character:
    return Character.create(
        name="Airi",
        summary="溫柔的角色",
        personality=["gentle"],
        interests=["music"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _build_prompt(
    frame: PresenceFrame,
    *,
    operator: OperatorProfile | None = None,
    now: datetime | None = None,
    include_operator_status: bool | None = None,
    character: Character | None = None,
    conversation: Conversation | None = None,
) -> str:
    # ``Character.create`` / ``Conversation.start`` mint fresh ids, which the
    # prompt renders — tests that diff two prompts must reuse both.
    character = character or _make_character()
    conversation = conversation or Conversation.start(character_id=character.id)
    # ``include_operator_status`` is omitted (not passed as ``True``) when the
    # caller doesn't care, so these tests also pin the *default* of the new
    # keyword rather than always overriding it.
    status_kwargs = (
        {} if include_operator_status is None
        else {"include_operator_status": include_operator_status}
    )
    return DefaultPromptContextBuilder().build(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="嗨",
        presence_frame=frame,
        operator=operator,
        now=now,
        **status_kwargs,
    )


def _operator_with_status(
    status: str | None,
    *,
    set_at: datetime | None = None,
) -> OperatorProfile:
    return OperatorProfile(
        id="user-1",
        display_name="Alex",
        current_status=status,
        current_status_set_at=set_at,
    )


def _assert_stage_narrative_guidance(prompt: str) -> None:
    """The four §2 points that replaced the judge's blocking copy."""
    # 1) player-declared co-presence is not to be questioned
    assert "玩家宣告此刻與你同場" in prompt
    assert "不需要質疑他是怎麼出現的" in prompt
    # 2) answer from your own schedule / situation
    assert "依你目前的行程與處境自然回應" in prompt
    assert "明說現在不方便" in prompt
    assert "演出日常共處" in prompt
    assert "不要寫成早就約好" in prompt
    # 3) boundaries are performed, not enforced by refusing to interact
    assert "用演出設界，不是拒絕互動" in prompt
    # 4) no fabricating the player's actions / whereabouts
    assert "不要虛構玩家的行動與位置細節" in prompt


def _assert_no_judge_blocking_copy(prompt: str) -> None:
    assert "使用者目前不合理出現在你的當前場景" not in prompt
    assert "請不要描寫使用者已經在你身邊" not in prompt
    assert "應先透過文字訊息約定或邀請" not in prompt
    assert "同場理由：" not in prompt


def _assert_not_texting_style(prompt: str) -> None:
    assert "手機即時通訊" not in prompt
    assert "每則訊息之間空一行" not in prompt
    assert "通常一到三則" not in prompt
    assert "不要使用 `*...*`" not in prompt
    assert "動作、表情、或當下狀態的描寫請用" in prompt


def test_stage_frame_renders_player_declared_narrative_guidance() -> None:
    prompt = _build_prompt(PresenceFrame.web_stage())

    assert "互動語境" in prompt
    assert "站內同場互動" in prompt
    _assert_stage_narrative_guidance(prompt)
    _assert_no_judge_blocking_copy(prompt)


def test_stage_frame_default_is_not_treated_as_phone_texting() -> None:
    prompt = _build_prompt(PresenceFrame.web_stage())

    _assert_not_texting_style(prompt)


def test_explicit_player_declared_context_matches_default_stage_block() -> None:
    prompt = _build_prompt(
        PresenceFrame.web_stage(access_context=AccessContext.PLAYER_DECLARED),
    )

    _assert_stage_narrative_guidance(prompt)
    _assert_not_texting_style(prompt)


@pytest.mark.parametrize("legacy", _LEGACY_STAGE_CONTEXTS)
def test_legacy_stage_contexts_render_the_single_stage_block(
    legacy: AccessContext,
) -> None:
    """A deployed self-hosted frontend still sends the old verdict values;
    they collapse into one narrative block instead of two behaviours."""
    prompt = _build_prompt(PresenceFrame.web_stage(access_context=legacy))

    _assert_stage_narrative_guidance(prompt)
    _assert_no_judge_blocking_copy(prompt)
    _assert_not_texting_style(prompt)


def test_web_dm_frame_still_renders_texting_style() -> None:
    prompt = _build_prompt(PresenceFrame.web_dm())

    assert "站內私訊" in prompt
    assert "文字訊息對話" in prompt
    assert "不是面對面場景" in prompt
    assert "手機即時通訊" in prompt
    assert "口語、簡短" in prompt
    assert "不要使用 `*...*`" in prompt
    assert "每則訊息之間空一行" in prompt
    assert "多數情況一到兩則" in prompt
    assert "通常一到三則" in prompt
    assert "連發四五則以上是少數" in prompt
    assert "動作、表情、或當下狀態的描寫請用" not in prompt
    assert "手機文字訊息" in prompt
    # The stage narrative block must not leak into the phone branch.
    assert "玩家宣告此刻與你同場" not in prompt


def test_messaging_frame_still_renders_texting_style() -> None:
    prompt = _build_prompt(PresenceFrame.messaging(platform=Platform.TELEGRAM))

    assert "Telegram" in prompt
    assert "文字訊息對話" in prompt
    assert "不是面對面場景" in prompt
    assert "避免描寫你直接看見對方" in prompt
    assert "本回合只有文字內容" in prompt
    assert "手機即時通訊" in prompt
    assert "洗版" in prompt
    assert "動作、表情、或當下狀態的描寫請用" not in prompt
    assert "玩家宣告此刻與你同場" not in prompt


def test_presence_frame_with_attachments_renders_visibility_boundary() -> None:
    prompt = _build_prompt(
        PresenceFrame.messaging(platform=Platform.LINE, has_attachments=True),
    )

    assert "LINE" in prompt
    assert "本回合可能含附件" in prompt
    assert "看不到的細節要保留不確定性" in prompt


def test_stage_frame_still_renders_legacy_stage_access_note() -> None:
    note = "使用者意外出現在現場；你事前不知情，請依性格自然演出驚訝。"

    prompt = _build_prompt(PresenceFrame.web_stage(stage_access_note=note))

    assert f"可抵達性補充：{note}" in prompt


def test_stage_frame_without_note_renders_no_note_line() -> None:
    prompt = _build_prompt(PresenceFrame.web_stage())

    assert "可抵達性補充" not in prompt


def test_operator_current_status_is_injected_into_stage_context() -> None:
    prompt = _build_prompt(
        PresenceFrame.web_stage(),
        operator=_operator_with_status(
            "加班中，大概九點才會到家",
            set_at=_NOW - timedelta(hours=2),
        ),
        now=_NOW,
    )

    assert "目前狀態" in prompt
    assert "加班中，大概九點才會到家" in prompt


def test_operator_current_status_is_injected_into_phone_context() -> None:
    prompt = _build_prompt(
        PresenceFrame.web_dm(),
        operator=_operator_with_status(
            "在醫院陪家人",
            set_at=_NOW - timedelta(minutes=20),
        ),
        now=_NOW,
    )

    assert "目前狀態" in prompt
    assert "在醫院陪家人" in prompt
    # Phone branch keeps its own style guidance alongside the status.
    assert "手機即時通訊" in prompt


def test_no_current_status_leaves_no_trace() -> None:
    prompt = _build_prompt(
        PresenceFrame.web_stage(),
        operator=_operator_with_status(None),
        now=_NOW,
    )

    assert "目前狀態" not in prompt
    assert "自述的近況" not in prompt


def test_missing_operator_leaves_no_status_trace() -> None:
    prompt = _build_prompt(PresenceFrame.web_stage(), now=_NOW)

    assert "目前狀態" not in prompt


def test_current_status_carries_semantic_freshness_not_a_hard_ttl() -> None:
    set_at = _NOW - timedelta(hours=3)
    prompt = _build_prompt(
        PresenceFrame.web_stage(),
        operator=_operator_with_status("在咖啡廳趕稿", set_at=set_at),
        now=_NOW,
    )

    # Both timestamps are handed to the model so it can judge freshness.
    assert "設定於 2026-07-30 18:40" in prompt
    assert "現在是 2026-07-30 21:40" in prompt
    # …and the instruction says the judgement is the model's, semantic,
    # with no fixed expiry window.
    assert "沒有固定有效期限" in prompt
    assert "自行判斷" in prompt


def test_stale_current_status_is_still_injected_verbatim() -> None:
    """No program-side expiry: a very old status is still handed over with
    its timestamps rather than silently dropped (LLM-first, non-TTL)."""
    prompt = _build_prompt(
        PresenceFrame.web_stage(),
        operator=_operator_with_status(
            "出差到下週",
            set_at=_NOW - timedelta(days=9),
        ),
        now=_NOW,
    )

    assert "出差到下週" in prompt
    assert "設定於 2026-07-21 21:40" in prompt


def test_status_timestamps_are_distinguishable_a_year_apart() -> None:
    """A month/day-only stamp renders "07/30 21:40" for both a status set
    minutes ago and one set a year ago, so the model reads a year-old
    situation as current. The year has to be on the wire."""
    prompt = _build_prompt(
        PresenceFrame.web_stage(),
        operator=_operator_with_status(
            "在花蓮出差",
            set_at=_NOW.replace(year=_NOW.year - 1),
        ),
        now=_NOW,
    )

    assert "設定於 2025-07-30 21:40" in prompt
    assert "現在是 2026-07-30 21:40" in prompt


def test_current_status_without_set_at_still_renders() -> None:
    prompt = _build_prompt(
        PresenceFrame.web_stage(),
        operator=_operator_with_status("剛下班"),
        now=_NOW,
    )

    assert "剛下班" in prompt
    assert "未記錄設定時間" in prompt


def test_status_injection_can_be_withheld_from_a_multi_sender_channel() -> None:
    """The status belongs to the *account owner*. On an external channel that
    may carry several humans (open / multi-entry allowlist) the turn is sent
    with persona learning off; injecting the owner's status there attributes
    it to whoever is actually typing."""
    prompt = _build_prompt(
        PresenceFrame.messaging(platform=Platform.LINE),
        operator=_operator_with_status(
            "在醫院陪家人",
            set_at=_NOW - timedelta(minutes=20),
        ),
        now=_NOW,
        include_operator_status=False,
    )

    assert "在醫院陪家人" not in prompt
    assert "目前狀態" not in prompt
    assert "自述的近況" not in prompt
    assert "設定於" not in prompt


def test_withholding_status_changes_nothing_else_in_the_prompt() -> None:
    """Only the status injection is gated — the operator identity block and
    the language block are pre-existing behaviour on multi-sender channels
    and must stay exactly as they were."""
    frame = PresenceFrame.messaging(platform=Platform.LINE)
    character = _make_character()
    conversation = Conversation.start(character_id=character.id)
    withheld = _build_prompt(
        frame,
        operator=_operator_with_status(
            "在醫院陪家人",
            set_at=_NOW - timedelta(minutes=20),
        ),
        now=_NOW,
        include_operator_status=False,
        character=character,
        conversation=conversation,
    )
    never_set = _build_prompt(
        frame,
        operator=_operator_with_status(None),
        now=_NOW,
        character=character,
        conversation=conversation,
    )

    assert withheld == never_set
    # …and the identity the block is built from is still there.
    assert "Alex" in withheld
