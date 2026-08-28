"""Dialogue-surface prompt renderers: presence frame, transcript,
self-observation rails, feed/proactive recall and the turn tail."""

from datetime import datetime, timezone, tzinfo
from typing import Mapping

from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.prompt_material_digest import PromptMaterialDigest
from kokoro_link.domain.entities.conversation import (
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.presence_frame import (
    AccessContext,
    ChatSurface,
    PresenceFrame,
    VisibilityMode,
)
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.prompt.register_blocks import (
    render_diversity_evidence_block,
)
from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    PromptSection,
    section,
)
from kokoro_link.infrastructure.prompt.sections.text import (
    LATEST_USER_MESSAGE_MARKER,
    _DIGEST_SOURCE_FRAME,
    _clip,
)
from kokoro_link.infrastructure.prompt.sections.vision import (
    _format_marker_prefix,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_elapsed_ago_label,
    format_gap_duration_label,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_ROLE_LABELS: dict[str, str] = {"user": "使用者", "assistant": "角色"}


def _format_history_line(message: Message, marker_numbers: list[int]) -> str:
    """Render one ``role：text`` line, prefixing any ``[圖 N]`` markers
    that belong to this turn so the model can tell which of the
    images it received goes with which historical message."""
    role_label = _ROLE_LABELS.get(message.role.value, message.role.value)
    prefix = _format_marker_prefix(marker_numbers)
    return f"{role_label}：{prefix}{message.content}"


_HISTORY_GAP_MARKER_THRESHOLD_MINUTES = 6 * 60.0
"""Insert a time-gap separator between two consecutive history turns (or
between the last turn and the current message) when their authored-time
gap exceeds this. 6h matches the subjective-time catch-up boundary in
``timing_utils`` — below it the turns read as one continuous sitting,
above it the model should see that time passed. Without this the literal
last line ("我要去買飲料" sent yesterday afternoon) reads as a live
message when the user returns the next morning, because the flat
transcript carries no per-turn time."""


def _message_created_at(message: Message) -> datetime | None:
    created = getattr(message, "created_at", None)
    if created is None:
        return None
    if created.tzinfo is None:
        return created.replace(tzinfo=timezone.utc)
    return created


def _format_history_gap_marker(gap_minutes: float, *, trailing: bool) -> str:
    label = format_gap_duration_label(gap_minutes)
    if trailing:
        return f"——（距離上面這幾句已經隔了{label}，以下才是這次的新訊息）——"
    return f"——（中間隔了{label}）——"


def _render_history_lines(
    messages: list[Message],
    markers: "Mapping[int, list[int]]",
    *,
    now: datetime | None = None,
    gap_threshold_minutes: float = _HISTORY_GAP_MARKER_THRESHOLD_MINUTES,
) -> list[str]:
    """Render the "近期對話" transcript, interleaving time-gap separators.

    A separator is inserted whenever the gap between two consecutive
    turns crosses ``gap_threshold_minutes`` so multi-sitting / multi-day
    windows don't read as one continuous thread. After the loop, when
    ``now`` is known and the last turn predates it by the same
    threshold, a trailing seam marker is appended so the stale last line
    isn't read as a just-now message (the current turn is rendered below
    the transcript as ``最新使用者訊息``). Negative gaps (clock skew /
    default timestamps from replay) never fire."""
    lines: list[str] = []
    previous_at: datetime | None = None
    for idx, message in enumerate(messages):
        current_at = _message_created_at(message)
        if previous_at is not None and current_at is not None:
            gap = (current_at - previous_at).total_seconds() / 60.0
            if gap >= gap_threshold_minutes:
                lines.append(_format_history_gap_marker(gap, trailing=False))
        lines.append(_format_history_line(message, markers.get(idx, [])))
        if current_at is not None:
            previous_at = current_at
    if now is not None and previous_at is not None:
        gap = (now - previous_at).total_seconds() / 60.0
        if gap >= gap_threshold_minutes:
            lines.append(_format_history_gap_marker(gap, trailing=True))
    return lines


def _render_persona_curiosity_block(
    plan: PersonaCuriosityPlan | None,
) -> list[str]:
    if plan is None or not plan.should_ask:
        return []
    lines = [
        "自然認識對方的提示：",
        "- 如果本輪回覆自然適合，可以把下列探索意圖融入角色自己的語氣；不要把這段當成固定問句。",
        "- 探索不必用問句收尾；也可以先分享你自己的相關經驗或反應，讓對方自然接話。",
        "- 一則回覆最多一個自然問題；若使用者正在求助、情緒高壓或有明確任務，先回應當下，不急著探索。",
        "- 不要提到使用者畫像、資料蒐集、補欄位或問卷；不要列問題清單。",
    ]
    if plan.target_layer is not None:
        lines.append(f"- 目標層級：Layer {plan.target_layer}")
    if plan.target_topic:
        lines.append(f"- 探索主題：{_clip(plan.target_topic, 100)}")
    if plan.tone_strategy:
        lines.append(f"- 語氣策略：{_clip(plan.tone_strategy, 140)}")
    if plan.question_intent:
        lines.append(f"- 探索意圖：{_clip(plan.question_intent, 260)}")
    if plan.safety_reason:
        lines.append(f"- 安全理由：{_clip(plan.safety_reason, 260)}")
    avoid = [_clip(item, 140) for item in plan.avoid if item and item.strip()]
    if avoid:
        lines.append("- 避免：")
        lines.extend(f"  - {item}" for item in avoid[:6])
    return lines


def _render_material_digest_block(
    digest: PromptMaterialDigest | None,
) -> list[str]:
    if digest is None or not digest.bullets:
        return []
    lines: list[str] = [
        "近期素材事實摘要（已去除原文文體；只作事實參照）：",
        "最高原則：若摘要提到使用者曾揭露的脆弱面，必須以保護姿態對待，禁止情勒、禁止當笑點、禁止當籌碼。",
        "行程對齊：以下是今天稍早或近期回憶素材，不是你此刻所在地點或正在做的事；若與「行程」段衝突，一律以行程為準。",
        _DIGEST_SOURCE_FRAME,
    ]
    for bullet in digest.bullets[:12]:
        text = bullet.strip()
        if text:
            lines.append(f"- {_clip(text, 220)}")
    return lines


def _render_retry_directive_block(retry_directive: str | None) -> list[str]:
    feedback = (retry_directive or "").strip()
    if not feedback:
        return []
    return [
        "上一輪嘗試的問題：",
        _clip(feedback, 320),
        "本輪務必帶出至少一件具體的新事、細節、想法或反應；避開上述問題，不要只是把近況素材換句話重講。",
    ]


_STAGE_PRESENCE_GUIDANCE = (
    "- 這是玩家選擇的站內同場互動：玩家宣告此刻與你同場，你不需要質疑他是怎麼出現的，"
    "也不必要求他先改用訊息或先約好才能繼續。",
    "- 但同場不代表你的行程消失：請依你目前的行程與處境自然回應——在外面或忙碌時，"
    "可以演出驚訝、抽空陪他一下，或明說現在不方便；在家或休息時，可以演出日常共處；"
    "他的出現在你的計畫之外時，依你的性格演出意外感，不要寫成早就約好。",
    "- 洗澡、情緒崩潰、深度獨處這類私密或脆弱的時段，你可以自然設界——迴避、請他稍等、"
    "隔著門說話都行；用演出設界，不是拒絕互動。",
    "- 不要虛構玩家的行動與位置細節：他做了什麼、站在哪裡、身上有什麼，"
    "只能依他的訊息與你已知的資訊，其餘保留不確定。",
)
"""Stage co-presence guidance.

Replaces the retired judge's out-of-narrative blocking copy. The four
points carry the judge prompt's intent — co-presence is not teleportation,
privacy still holds, the player's actions are not ours to invent — but
hand the judgement to the main model *inside* the scene instead of
refusing the turn before it starts.
"""


_STAGE_NUDGE_GUIDANCE = (
    "本輪情境（玩家請你先開口）：",
    "- 本輪玩家沒有對你說話——他們（若有補充）宣告了場景中的事實或自己的動作，"
    "或僅是示意你注意到他們在場。",
    "- 請你主動開口：接住當下語境、你自己的行程與狀態，自然地說出這一刻的第一句話；"
    "不要等他先講，也不要把這輪寫成在回覆一句他沒說出口的話。",
    "- 玩家宣告過的行動可以承認、可以回應（那是他自己說的），"
    "但不得替玩家虛構新的行動或台詞。",
)
"""Stage nudge (SN1) — the player pulled the turn instead of speaking.

A conditional block in the same spirit as :data:`_STAGE_PRESENCE_GUIDANCE`:
it hands the model the *situation* and one red line, and leaves the reading
of the supplement (a declared fact? an action? nothing at all?) to the
model, which is the only thing that can tell them apart.
"""


_STAGE_NUDGE_NEUTRAL_GUIDANCE = (
    "本輪情境（玩家請你先開口）：",
    "- 本輪玩家沒有對你說話，只是示意你先起頭；若有補充，那是他對此刻情況的說明。",
    "- 請你主動開口，接住當下語境與你自己的行程與狀態。",
    "- 玩家宣告過的行動可以承認、可以回應，但不得替玩家虛構新的行動或台詞。",
)
"""The same block for a turn that is not same-place acting.

The nudge flag is not gated on the surface (no verdict gate, per the SA
retirement): a DM or messaging frame that sends it still gets a turn, only
framed neutrally — "they asked you to start" rather than "they are here
with you", which would put the character in a room the frame says it is
not in.
"""


def _render_latest_user_message_line(
    *,
    stage_nudge: bool,
    prefix: str,
    message: str,
) -> list[str]:
    """The 「最新使用者訊息：」 line — omitted when there is no message.

    Only a *silent* 示意 can omit it, and only because the nudge block
    rendered immediately above already said 「本輪玩家沒有對你說話」. The
    two together used to read as a contradiction: a labelled slot for the
    player's line, empty, right under a sentence saying no line exists —
    which invites the model to answer the silence (「你怎麼不說話」) on the
    feature's most common press.

    Every other turn keeps the line byte for byte, including an ordinary
    turn whose text cleaned down to nothing (a bare ``/pic``) — that
    player did send something, and the empty slot is the honest rendering
    of it. The ``prefix`` guard is belt-and-braces: image markers without
    text cannot reach a silent nudge (the request contract rejects that
    combination), and if one ever did, dropping the line would hide the
    ``[圖 N]`` tags the attached images are numbered by.
    """
    if stage_nudge and not prefix and not message.strip():
        return []
    return [f"{LATEST_USER_MESSAGE_MARKER}{prefix}{message}"]


def _render_stage_nudge_block(
    stage_nudge: bool,
    presence_frame: "PresenceFrame | None",
) -> list[str]:
    """The conditional 示意 block, or nothing at all on an ordinary turn."""
    if not stage_nudge:
        return []
    frame = presence_frame or PresenceFrame.web_stage()
    if _presence_frame_uses_texting_style(frame):
        return list(_STAGE_NUDGE_NEUTRAL_GUIDANCE)
    return list(_STAGE_NUDGE_GUIDANCE)


def _render_presence_frame_block(
    presence_frame: "PresenceFrame | None",
    operator_language: str | None = None,
) -> list[str]:
    frame = presence_frame or PresenceFrame.web_stage()
    uses_texting_style = _presence_frame_uses_texting_style(frame)
    # Derive the channel display name from the channel enum honouring the
    # operator's content language (plan #1 / D4) instead of echoing the
    # client-sent natural-language label. Falls back to the frame's own
    # display_name for any channel not in the localized catalogue.
    channel_key = f"presence.channel.{frame.channel.value}"
    try:
        channel_label = localized_fallback_text(channel_key, operator_language)
    except KeyError:
        channel_label = frame.display_name
    lines = [
        "互動語境：",
        f"- 當前介面：{channel_label}（{frame.surface.value} / {frame.channel.value}）。",
    ]
    if uses_texting_style:
        lines.append(
            "- 這是文字訊息對話：你收到的是對方傳來的訊息，不是面對面場景。",
        )
        lines.append(
            "- 這可能是因為當下不適合直接同場；回覆時避免描寫你直接看見對方、"
            "觸碰對方或和對方面對面做動作。",
        )
    else:
        lines.extend(_STAGE_PRESENCE_GUIDANCE)

    if uses_texting_style:
        lines.extend(_render_texting_style_lines())

    if frame.stage_access_note:
        lines.append(f"- 可抵達性補充：{frame.stage_access_note}")

    if frame.visibility is VisibilityMode.TEXT_AND_ATTACHMENTS:
        lines.append(
            "- 本回合可能含附件；只能依系統實際提供給你的文字與圖片內容回應，"
            "看不到的細節要保留不確定性。",
        )
    elif frame.visibility is VisibilityMode.TEXT_ONLY:
        lines.append(
            "- 本回合只有文字內容；只能依文字與已知記憶理解對方狀態。",
        )
    else:
        lines.append(
            "- 本回合仍以文字為主要輸入；同場感只表示互動框架，不能憑空補完現實細節。",
        )
    return lines


def _presence_frame_uses_texting_style(frame: PresenceFrame) -> bool:
    """Whether this turn is phone-texting rather than same-place acting.

    Stage turns never are: legacy verdict values already folded onto
    ``PLAYER_DECLARED`` in ``PresenceFrame.__post_init__``, so the only way
    a ``web_stage`` frame reads as texting is an explicit
    ``text_message_only`` declaration (a pre-retirement blocked frame
    replayed from stored metadata).
    """
    return (
        frame.surface is not ChatSurface.WEB_STAGE
        or frame.access_context is AccessContext.TEXT_MESSAGE_ONLY
    )


def _render_response_format_instruction(frame: PresenceFrame) -> str:
    if _presence_frame_uses_texting_style(frame):
        return (
            "回覆格式慣例：這一輪是手機文字訊息。只輸出對方會在訊息裡看到的文字；"
            "不要寫動作、表情、場景旁白或任何 `*...*` 內容。"
            "如果想帶到你正在做的事，改成口語自然說明，例如「我剛剛在整理相簿」；"
            "不要寫成 `*把手機相簿往下滑*` 這類動作敘事。"
            "自然適合時可用空白行拆成幾則短訊息；不要用 markdown、列表或標題。"
        )
    return (
        "回覆格式慣例：口語台詞直接寫，不要加引號；動作、表情、或當下狀態的描寫請用"
        "**星號 `*...*` 包住**（例：`*倒了杯茶*`、`*偷瞄了一眼*`、`*歪頭*`、"
        "`*沉默片刻*`），讓前端可以把動作/狀態和口語區分渲染。星號 `*...*` 內的"
        "動作、表情與狀態描寫也屬於玩家可見自然語言，必須和台詞一樣使用上方指定的"
        "主要語言；不要因為下方格式範例是中文就把動作描寫寫成中文。若主要語言是 "
        "en-US，動作描寫也應自然寫成英文（例如 `*sets the phone down*`），而不是 "
        "`*放下手機*`。不要改用括號、方括號、破折號或其他符號——全場統一用 "
        "`*...*`，且動作描寫要簡短具體，不要一整段散文包在星號裡。"
    )


def _render_texting_style_lines() -> list[str]:
    return [
        "- 手機即時通訊文體：像在 LINE / IG DM 跟朋友傳訊息；用口語、簡短、自然的句子，"
        "不要一次回一大坨。",
        "- 不要寫動作、表情或場景旁白；不要使用 `*...*` 包動作。只傳你真的會打給對方看的字。",
        "- 訊息密度要依你的內在表達傾向、性格與當下精神狀態決定：多數情況一到兩則就好，"
        "簡短一句也完全可以只傳一則；分享慾低、內向或疲累時通常一兩則就停。",
        "- 通常一到三則；連發四五則以上是少數真的很興奮、分享慾很高、或有很多事想講的時刻。"
        "連發太多會讓對方一直被洗版，請像真人一樣考慮對方來不來得及讀。",
        "- 每則訊息之間空一行。不要為了拆而拆；自然短句優先。",
    ]


def _render_older_dialogue_summary_block(summary: str | None) -> list[str]:
    text = (summary or "").strip()
    if not text:
        return []
    return [
        "較早對話摘要（較舊輪次，系統壓縮）：",
        f"- {text}",
    ]


def _render_recent_proactive_block(
    *,
    attempts: tuple[ProactiveAttempt, ...],
    now: datetime | None,
    idle_minutes: float | None,
) -> list[str]:
    """Surface the character's own recent proactive pushes in the chat prompt.

    Same anti-repetition lever the proactive decider uses — without it
    the chat-side LLM has no idea the character just pinged the user
    on Telegram about the same topic, so a reply that retreads "你今天
    試鏡準備得怎樣了？" right after a proactive that asked the same
    question feels jarring and breaks the illusion of one continuous
    voice across surfaces. We tag whether the user has replied yet so
    unanswered pushes carry extra "back off" weight.
    """
    if not attempts:
        return []
    lines = [
        "你最近主動傳給對方的訊息（新→舊；這些已經送出，"
        "本輪不要再用同樣的題材／問題重問一次，可以換角度或先聽對方說）：",
    ]
    for att in attempts:
        when_text = ""
        reply_tag = ""
        if now is not None:
            elapsed_min = (now - att.decided_at).total_seconds() / 60.0
            when_text = format_elapsed_ago_label(elapsed_min)
            if idle_minutes is not None:
                if idle_minutes < elapsed_min:
                    reply_tag = "（對方已回）"
                else:
                    reply_tag = "（對方還沒回）"
        text = (att.message or "").strip() or "(無內容)"
        prefix = f"- {when_text}{reply_tag}：" if when_text else "- "
        lines.append(f"{prefix}{text}")
    lines.append(
        "若最新一則對方還沒回，更要小心：本輪盡量不要再追問同一件事，"
        "讓對方有空間先回應，或自然轉到別的話題／回應對方剛剛的訊息。"
    )
    return lines


_SELF_LINES_BUDGET = 3
"""How many recent assistant turns to re-surface as an explicit anti-
repetition rail. Three is enough to catch immediate echo without
crowding the prompt."""


_SELF_LINE_SNIPPET = 180
"""Per-line excerpt budget. Long assistant replies get truncated to a
recognisable tail so the framing stays compact; the full text is still
present in the regular history block below."""


def _render_recent_self_lines_block(
    *,
    recent_messages: list[Message],
) -> list[str]:
    """Re-surface the character's own recent in-conversation replies
    with explicit anti-repetition framing.

    The assistant lines are already in the regular history block, but
    folded in with user turns the model treats them as ambient context
    rather than its own commitments. Repeated phrasing / questions /
    openings within a single conversation are the most common quality
    drop reported by operators — pulling the last few assistant turns
    out into a dedicated rail with "don't reuse these phrasings"
    framing is a cheap, semantic anti-repetition lever (no extra LLM
    call). Same pattern as ``_render_recent_proactive_block`` does for
    cross-surface pushes.

    Returns ``[]`` when there isn't at least one assistant turn yet —
    the rail would just be noise on the very first turn.
    """
    assistant_turns = [
        m for m in recent_messages
        if m.role is MessageRole.ASSISTANT
        and m.kind is MessageKind.CHAT
        and m.content.strip()
    ]
    if not assistant_turns:
        return []
    selected = assistant_turns[-_SELF_LINES_BUDGET:]
    lines: list[str] = [
        "你本對話最近自己說過的話（新→舊；**這些是你自己已經講過的**，"
        "本輪不要再用同樣的措辭、同樣的開場、同樣的提問或同樣的比喻；"
        "若話題沒有變化，可以換切入點或先聽對方說）：",
    ]
    # newest first so the first bullet is the line the model most
    # likely to mechanically echo if not warned.
    for msg in reversed(selected):
        text = msg.content.strip()
        if len(text) > _SELF_LINE_SNIPPET:
            text = text[:_SELF_LINE_SNIPPET] + "…"
        lines.append(f"- {text}")
    return lines


def _render_self_repetition_hint_block(
    *,
    hint: str | None,
) -> list[str]:
    """Surface the periodic self-repetition extractor's verdict.

    Complements ``_render_recent_self_lines_block`` — the lines block
    is a *literal* "you just said these, don't echo them" rail, this
    block is a *semantic* "the pattern across the last 10 turns is X"
    rail. They're cheap together: the lines rail catches immediate
    echo, the hint rail catches slower-forming habits the model
    wouldn't notice from the literal text alone. Empty hint → no
    rail emitted.
    """
    if not hint or not hint.strip():
        return []
    return [
        "你近期回覆中已被偵測到的重複傾向（**本輪請主動避開這些模式**）：",
        hint.strip(),
    ]


_render_diversity_evidence_block = render_diversity_evidence_block


def _render_persona_self_check_block() -> list[str]:
    return [
        "畫像使用自檢：送出前請檢查本輪是否只是把「關於對方」段落換句話背出來；"
        "若是，請改成回應對方當下訊息，或完全不提畫像資訊。",
    ]


_FEED_PROMPT_SNIPPET_CHARS = 90
"""Cap per-post body in the prompt rail. Long posts crowd out other
context; the snippet is a recall hook, not a faithful reproduction —
the LLM only needs enough to recognise the topic if the user mentions it."""


def _render_recent_feed_block(
    *,
    posts: tuple[FeedPost, ...],
    now: datetime | None,
) -> list[str]:
    """Surface the character's own recent feed-wall posts in chat.

    Without this rail the chat-side LLM has no idea the character just
    posted "今天的咖啡好香" on the feed wall, so when the user opens with
    "你那篇咖啡的動態怎麼了" the character looks blank or — worse —
    invents a different post. We list the most recent posts (newest
    first) with elapsed time + a trimmed snippet so the model can
    recognise references and respond in continuity.
    """
    if not posts:
        return []
    lines = [
        _DIGEST_SOURCE_FRAME,
        "你最近在動態牆上發過的貼文（新→舊；使用者瀏覽時可能會聊到，"
        "本輪請記得自己發過這些內容，不要表現得像沒發過）：",
    ]
    for post in posts:
        when_text = ""
        if now is not None:
            elapsed_min = (now - post.created_at).total_seconds() / 60.0
            when_text = format_elapsed_ago_label(elapsed_min)
        snippet = (post.content_text or "").strip() or "(無內容)"
        if len(snippet) > _FEED_PROMPT_SNIPPET_CHARS:
            snippet = snippet[:_FEED_PROMPT_SNIPPET_CHARS].rstrip() + "…"
        image_tag = "（含圖）" if post.image_url else ""
        prefix = f"- {when_text}{image_tag}：" if when_text else f"- {image_tag}"
        lines.append(f"{prefix}{snippet}")
    return lines


# --------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------

def _presence_frame(ctx: PromptSectionContext) -> list[str]:
    return _render_presence_frame_block(
        ctx.dialogue.presence_frame,
        operator_language=getattr(ctx.identity.operator, "primary_language", None),
    )


def _conversation_id(ctx: PromptSectionContext) -> list[str]:
    return [f"對話 ID：{ctx.dialogue.conversation.id}"]


def _older_dialogue(ctx: PromptSectionContext) -> list[str]:
    return _render_older_dialogue_summary_block(
        ctx.dialogue.older_dialogue_summary,
    )


def _recent_proactive(ctx: PromptSectionContext) -> list[str]:
    return _render_recent_proactive_block(
        attempts=ctx.dialogue.recent_proactive_messages,
        now=ctx.time.now,
        idle_minutes=ctx.time.idle_minutes,
    )


def _recent_feed(ctx: PromptSectionContext) -> list[str]:
    return _render_recent_feed_block(
        posts=ctx.dialogue.recent_feed_posts, now=ctx.time.now,
    )


def _recent_self_lines(ctx: PromptSectionContext) -> list[str]:
    return _render_recent_self_lines_block(
        recent_messages=list(ctx.dialogue.recent_messages),
    )


def _self_repetition(ctx: PromptSectionContext) -> list[str]:
    return _render_self_repetition_hint_block(
        hint=ctx.dialogue.self_repetition_hint,
    )


def _diversity_evidence(ctx: PromptSectionContext) -> list[str]:
    return _render_diversity_evidence_block(
        ctx.dialogue.reply_diversity_evidence,
    )


def _persona_self_check(ctx: PromptSectionContext) -> list[str]:
    """Only worth emitting when there is a portrait to over-recite; reads
    the persona section's *input* for the same reason
    ``relationship_anchor`` does."""
    if not ctx.identity.operator_persona_lines:
        return []
    return _render_persona_self_check_block()


def _persona_curiosity(ctx: PromptSectionContext) -> list[str]:
    return _render_persona_curiosity_block(ctx.dialogue.persona_curiosity_plan)


def _material_digest(ctx: PromptSectionContext) -> list[str]:
    return _render_material_digest_block(ctx.dialogue.material_digest)


def _recent_dialogue(ctx: PromptSectionContext) -> list[str]:
    return [
        "近期對話：",
        *_render_history_lines(
            list(ctx.dialogue.recent_messages),
            ctx.vision.markers,
            now=ctx.time.now,
        ),
    ]


def _stage_nudge(ctx: PromptSectionContext) -> list[str]:
    """SN1: rendered next to the latest-user-message line rather than up
    with the presence frame — it is a statement about *this* turn ("nobody
    spoke to you"), and it has to land right where the model would
    otherwise be looking for the line that isn't there."""
    return _render_stage_nudge_block(
        ctx.dialogue.stage_nudge, ctx.dialogue.presence_frame,
    )


def _latest_user_message(ctx: PromptSectionContext) -> list[str]:
    # Index ``len(recent_messages)`` is the special "this turn's user
    # message" slot in the vision inventory — its markers splice in front
    # of the message text so the placeholder ordering in the prompt
    # matches the order of ``image_urls`` sent to the model.
    latest_markers = ctx.vision.markers.get(
        len(ctx.dialogue.recent_messages), [],
    )
    prefix = (
        _format_marker_prefix(latest_markers) if latest_markers else ""
    )
    return _render_latest_user_message_line(
        stage_nudge=ctx.dialogue.stage_nudge,
        prefix=prefix,
        message=ctx.dialogue.latest_user_message,
    )


def _retry_directive(ctx: PromptSectionContext) -> list[str]:
    return _render_retry_directive_block(ctx.dialogue.retry_directive)


def _instructions_footer(ctx: PromptSectionContext) -> list[str]:
    return [
        get_default_loader().render(
            "chat/instructions_footer",
            response_format_instruction=_render_response_format_instruction(
                ctx.dialogue.presence_frame,
            ),
        )
    ]


SECTIONS: tuple[PromptSection, ...] = (
    section("presence_frame", _presence_frame),
    section("material_digest", _material_digest),
    section("conversation_id", _conversation_id),
    section("older_dialogue", _older_dialogue),
    section("recent_proactive", _recent_proactive),
    section("recent_feed", _recent_feed),
    section("recent_self_lines", _recent_self_lines),
    section("self_repetition", _self_repetition),
    section("diversity_evidence", _diversity_evidence),
    section("persona_self_check", _persona_self_check),
    section("persona_curiosity", _persona_curiosity),
    section("recent_dialogue", _recent_dialogue),
    section("stage_nudge", _stage_nudge),
    section("latest_user_message", _latest_user_message),
    section("retry_directive", _retry_directive),
    section("instructions_footer", _instructions_footer),
)
