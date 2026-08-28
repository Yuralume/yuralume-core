"""Story-zone prompt renderers: story events, scripted beats, live
scenes and arc history."""

from dataclasses import fields
from datetime import date as date_type

from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    StoryArc,
    StoryArcBeat,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_scene_session import StorySceneSession
from kokoro_link.infrastructure.prompt.player_knowledge_lines import (
    BEAT_LIST_DATE_DISCIPLINE_LINE,
    render_arc_forward_feed_knowledge_line,
    render_arc_history_knowledge_line,
    render_arc_history_solo_heading,
    render_beat_schedule_stamp_lines,
    render_player_knowledge_lines,
)
from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    PromptSection,
    section,
)
from kokoro_link.infrastructure.prompt.sections.text import (
    _DIGEST_SOURCE_FRAME,
)

def _render_story_events_block(events: list[StoryEvent]) -> list[str]:
    """Inject today's story events as the character's own life-colour.

    Unlike world events, these are *first person* — written in the
    character's voice. The prompt frames them as inner reality so the
    model treats them as genuine experiences the character could bring
    up naturally.
    """
    if not events:
        return []
    lines: list[str] = [
        _DIGEST_SOURCE_FRAME,
        "今天你身上發生的小事（第一人稱、是你真的經歷的情緒片段，可自然融入對話）：",
    ]
    for event in events:
        text = event.narrative.strip()
        tone = (event.emotional_tone or "").strip()
        if tone:
            lines.append(f"- ({tone}) {text}")
        else:
            lines.append(f"- {text}")
    lines.append(
        "注意：以上只是情緒與話題素材，**不是你此刻身處的地點或正在做的活動**。"
        "若與上方「行程」段落的當前地點／活動衝突（例：故事說在學校，行程顯示在使用者家），"
        "一律以行程為準；故事內容可作為「剛才」「今天稍早」的回憶帶過，不要假裝自己正在那個場景裡。"
    )
    return lines


_TENSION_HINTS = {
    "setup": "故事才剛起頭",
    "rising": "事情正在往上堆",
    "climax": "重要的時刻要來了",
    "falling": "餘波還在慢慢散",
    "resolution": "告一段落的時候",
}


_SCENE_TYPE_LABELS = {
    "encounter": "日常／相遇",
    "revelation": "頓悟／揭露",
    "conflict": "衝突／拉扯",
    "resolution": "解決／釋懷",
    "interlude": "過場／喘息",
}


# --- Operator position framing (OP2-A) -------------------------------
# ``StoryArcBeat.operator_position`` (OP0) tells a scene consumer where
# the player stands in a beat; before this slot existed, every consumer
# had to assume one — a static "the player is right here" assertion or a
# forced "make the player interrupt this" instruction. Both retired in
# favour of a framing sentence derived from the enum, with ``None``
# (*unjudged*) degrading to a semantic-derivation instruction rather
# than a guess.
#
# There are **two** tables below, not one, because the same enum value
# means two different instructions depending on whether the curtain has
# gone up:
#
# * 尚未開幕 (``_render_today_directive_operator_position_lines``) — the
#   player is chatting, the beat is still waiting to be played. ``absent``
#   here genuinely means "this scene has no player in it".
# * 已開幕 (``_render_live_scene_operator_position_lines``) — the player
#   pressed 起幕 and the opener already narrated them walking in (see
#   ``operator_scene_position._OPENER_FRAMING``). ``absent`` here means
#   "this scene was *not originally* about the player, and he has walked
#   into it anyway". Rendering the pending wording in an open scene told
#   the model 「不要假裝玩家在場」 one turn after the opening narration put
#   him on stage — a flat contradiction, and the shipped
#   ``cafe_idol_audition`` template is absent end to end, so every beat of
#   it hit that contradiction.
#
# Only the enum and the note formatting are shared; the prose is
# deliberately per-situation (same reason the opener/closer keep their own
# in ``operator_scene_position.py``).
_TODAY_DIRECTIVE_OPERATOR_POSITION_FRAMES = {
    OPERATOR_POSITION_CENTRAL: (
        "玩家是這場戲的核心——沒有玩家的參與，這場戲演不下去。"
        "順著對話把玩家拉進戲的中心，讓戲圍繞玩家展開；"
        "玩家帶開話題時，找機會把他引回戲的核心，而不是繞著他演下去。"
    ),
    OPERATOR_POSITION_PRESENT: (
        "玩家在這場戲裡，但戲的重心不在玩家身上——"
        "讓玩家自然地在場、陪伴、旁觀或參與，戲仍照自己的節奏推進，"
        "不必刻意把每一步都繞回玩家。"
    ),
    OPERATOR_POSITION_ABSENT: (
        "這場戲裡沒有玩家的位置——這是你自己的戲，可以獨自演到底。"
        "可以事後講給玩家聽、或帶著這份心情跟玩家互動，"
        "但不要假裝玩家在場見證整個過程。"
    ),
}


_TODAY_DIRECTIVE_OPERATOR_POSITION_UNJUDGED = (
    "玩家在這場戲的位置尚未判定，請依下面的出場人物與戲劇問題自行判斷："
    "若明顯不含玩家，用旁觀或自然引入的角度帶出這場戲；"
    "若描述中出現可能暗示玩家的詞（例如「伴侶」「對方」等），"
    "由你自行判斷是否即指玩家、以及玩家該站在戲裡的什麼位置。"
)


# The 已開幕 half. Every branch takes 「玩家此刻人就在這場戲裡」 as a given —
# the opening narration is a message in the very conversation being
# rendered below, so it is not a judgement call the model gets to redo.
# What the enum still decides is whose story this is.
_LIVE_SCENE_OPERATOR_POSITION_FRAMES = {
    OPERATOR_POSITION_CENTRAL: (
        "玩家是這場戲的核心——這場戲本來就是關於玩家的，沒有他演不下去。"
        "把每一步都接在玩家剛剛的反應上，讓戲繞著他往前推；"
        "玩家帶開話題時，找機會把他引回戲的核心。"
    ),
    OPERATOR_POSITION_PRESENT: (
        "玩家在這場戲裡，但戲的重心不在玩家身上——他此刻在場（陪伴、旁觀，"
        "或剛好被捲入），戲仍照你自己的處境往前走，不必把每一步都繞回玩家。"
    ),
    OPERATOR_POSITION_ABSENT: (
        "這場戲原本不是關於玩家的——它本來是你自己的一段戲，玩家不在計畫內。"
        "但戲已經開演，玩家已經走進來了，此刻人就在現場：把他當成半路加入的人"
        "來演（你可以意外、可以需要重新調整、可以還沒準備好被他看見），"
        "但不要把整場戲改寫成關於玩家的。"
        "**不要**寫得像玩家不在場、也不要把這段戲說成事後才要講給他聽。"
    ),
}


_LIVE_SCENE_OPERATOR_POSITION_UNJUDGED = (
    "玩家在這場戲的位置沒有人標記過，但有一件事是確定的：戲已經開演，"
    "玩家此刻人就在這場戲裡。請依上面的場景資訊與下方對話自行判斷，"
    "他比較像是這場戲的核心、在場的陪伴，還是原本不在計畫內、這次剛好走了進來——"
    "並據此決定這一回合把鏡頭擺在誰身上。"
)


def _render_today_directive_operator_position_lines(
    position: str | None, note: str | None,
) -> list[str]:
    """Player framing for a beat that is **still waiting to be played**."""
    return _render_operator_position_lines(
        _TODAY_DIRECTIVE_OPERATOR_POSITION_FRAMES,
        _TODAY_DIRECTIVE_OPERATOR_POSITION_UNJUDGED,
        position,
        note,
    )


def _render_live_scene_operator_position_lines(
    position: str | None, note: str | None,
) -> list[str]:
    """Player framing for a beat whose scene is **already open**."""
    return _render_operator_position_lines(
        _LIVE_SCENE_OPERATOR_POSITION_FRAMES,
        _LIVE_SCENE_OPERATOR_POSITION_UNJUDGED,
        position,
        note,
    )


def _render_operator_position_lines(
    frames: dict[str, str],
    unjudged: str,
    position: str | None,
    note: str | None,
) -> list[str]:
    """Look up one framing sentence and append the free-text note.

    Never a keyword table branching on scene content — the three known
    positions map to a fixed framing sentence about *how to write the
    player*, and the one open-ended judgement call (does this beat's
    prose actually mean the player?) is left to the model via the
    caller's ``unjudged`` guidance when the beat carries no verdict yet.
    """
    lines = [frames.get(position or "", unjudged)]
    if note:
        lines.append(f"- 玩家在這場戲的位置備註：{note}")
    return lines


def _today_scene_beat(
    arc: "StoryArc | None", today: date_type | None,
) -> "StoryArcBeat | None":
    """Find the beat the model should play today, if any.

    Direction B keeps due beats pending until the interaction actually
    plays them, so this directive should target pending/active beats
    only. Realized beats flow through StoryEvent / memory and must not
    be forced into the next reply again.
    """
    if arc is None or today is None:
        return None
    candidates = [
        b for b in arc.beats
        if b.scheduled_date <= today
        and b.status in {"pending", "active"}
    ]
    if not candidates:
        return None
    # Earliest overdue/today beat wins; stable when the planner emits
    # "morning + afternoon" beats on the same day.
    candidates.sort(key=lambda b: (b.scheduled_date, b.sequence))
    return candidates[0]


def _scene_has_structure(beat: "StoryArcBeat") -> bool:
    """Cheap mirror of ``SceneContext.is_meaningful`` — used here
    instead of constructing a SceneContext just to ask the question."""
    return bool(
        beat.location
        or beat.scene_characters
        or beat.dramatic_question
    )


def _render_today_scene_directive_block(
    *,
    arc: "StoryArc | None",
    today: date_type | None,
) -> list[str]:
    """Strong directive segment for today's scripted scene beat.

    Distinct from ``_render_story_arc_block`` (informational forward
    feed) and ``_render_story_events_block`` (the narrative material
    of today's event): this block tells the model **what scene to
    play right now** — location, who else is there, what tension
    drives the moment. Emits nothing when there's no beat for today
    or when the beat carries no scene structure (older arcs, gacha-
    only days), so a character without scripted scenes sees no extra
    noise in the prompt.
    """
    beat = _today_scene_beat(arc, today)
    if beat is None or not _scene_has_structure(beat):
        return []
    label = _SCENE_TYPE_LABELS.get(beat.scene_type, beat.scene_type)
    header = (
        "【今日場景指引（必演）】" if beat.required
        else "【今日場景指引（可選；可在自然處帶過）】"
    )
    lines: list[str] = [
        header,
        "今天的對話應自然進入下方這場戲（不要逐句念骨架，用角色當下感受演出）：",
        f"- 場景類型：{label}",
    ]
    if beat.location:
        lines.append(f"- 場景地點：{beat.location}")
    if beat.scene_characters:
        lines.append(
            f"- 出場人物（除你之外）：{'、'.join(beat.scene_characters)}"
        )
    if beat.dramatic_question:
        lines.append(f"- 戲劇問題：{beat.dramatic_question}")
    overdue_days = max(0, (today - beat.scheduled_date).days) if today else 0
    if overdue_days:
        lines.append(f"- 已經延後：{overdue_days} 天")
    if beat.play_attempt_count:
        lines.append(f"- 已嘗試帶出：{beat.play_attempt_count} 次")
    if beat.last_play_push_intensity:
        lines.append(f"- 上次推進力道：{beat.last_play_push_intensity}")
    if beat.last_play_attempt_result:
        lines.append(f"- 上次結果：{beat.last_play_attempt_result}")
    # Title gives the LLM a one-phrase anchor; useful when the realized
    # event narrative is in a different angle than the beat title.
    lines.append(f"- 場景標題：{beat.title}")
    # KB4: ``delay_beat`` moves the schedule and leaves the prose alone,
    # so the summary above may still be dated for the day this beat was
    # *originally* planned for. Stamp the authoritative day next to the
    # material instead of rewriting model-written prose.
    lines.extend(
        render_beat_schedule_stamp_lines(
            beat.scheduled_date,
            relative_label=_format_date_delta(today, beat.scheduled_date),
        ),
    )
    # OP2-A: the old instruction here forced "at least one of
    # atmosphere/NPC-presence/tension must surface", which only ever
    # gave the player one identity in this block — someone who might
    # digress and need steering back. Retired in favour of framing
    # derived from where the player actually stands in this beat.
    lines.append("在合適時機讓這場戲自然發生（不要逐句念骨架）：")
    lines.extend(
        _render_today_directive_operator_position_lines(
            beat.operator_position, beat.operator_note,
        ),
    )
    # KB3: the block above is a director's note the player has never
    # read. Without this rail the model plays the scene as if the two of
    # them had agreed on it beforehand — the 2026-08-25 incident.
    lines.extend(render_player_knowledge_lines())
    return lines


def _render_story_scene_block(
    scene: "StorySceneSession | None",
) -> list[str]:
    """The frame around a turn played inside a live 起幕 scene.

    Why this *replaces* ``_render_today_scene_directive_block`` instead of
    sitting beside it: when the player pulled the scene from today's due
    beat, both blocks describe the same beat, and they give opposite
    instructions — 「在合適時機讓這場戲自然發生」 (find an opening) versus
    「你已經在這場戲裡」 (you are mid-performance). A model handed both
    tends to re-open a scene it is already playing. The frame wins because
    it is the one describing what is actually happening.

    The scene's established facts are not repeated here: the opening
    narration is a message in the very conversation being rendered below,
    so restating it would double-feed the model its own text.
    """
    if scene is None or not scene.is_open:
        return []
    lines: list[str] = [
        "【劇情場景進行中】",
        "你正在跟玩家演一段「劇情場景」——這不是普通聊天，是一場已經拉開序幕的戲。"
        "下方對話中的旁白已經交代了這場戲的既定事實。",
    ]
    if scene.title:
        lines.append(f"- 場景標題：{scene.title}")
    if scene.location:
        lines.append(f"- 場景地點：{scene.location}")
    if scene.mood:
        lines.append(f"- 氛圍：{scene.mood}")
    if scene.scene_type:
        lines.append(
            "- 場景類型："
            f"{_SCENE_TYPE_LABELS.get(scene.scene_type, scene.scene_type)}",
        )
    if scene.dramatic_question:
        lines.append(f"- 戲劇問題：{scene.dramatic_question}")
    # OP2-A: "玩家人就在戲裡" used to be an unconditional assertion here
    # regardless of what kind of beat pulled this scene open. Replaced
    # with position-derived framing — read off the **session**, which
    # snapshotted both fields at open time (OP2-D) exactly like
    # ``scene_type`` / ``dramatic_question``. Re-resolving the beat
    # instead would let an edit, a retire, or a realize mid-performance
    # silently downgrade a judged scene to "unjudged" halfway through
    # the very scene it opened, and the closer (SC1-D) already reads the
    # session — two readers of the same fact must not disagree.
    lines.extend(
        _render_live_scene_operator_position_lines(
            scene.operator_position, scene.operator_note,
        ),
    )
    lines.append(
        "本回合就在這場戲裡演下去：延續現場的地點與氛圍、承接玩家剛剛的行動，"
        "讓戲劇問題往前推一點——但不要急著替這場戲收尾或下結論。"
        "玩家岔題時可以自然地把戲拉回來；玩家想離開這場戲時不要硬留。"
        "不要替玩家決定他做了什麼、說了什麼；也不要寫系統說明或選項清單。",
    )
    return lines


def _render_story_arc_block(
    *,
    arc: StoryArc | None,
    upcoming: list[StoryArcBeat],
    today: date_type | None,
) -> list[str]:
    """Forward-feed arc context: current premise + next 1–2 pending beats.

    Gives the model **anticipation** — it can drop hints like "再兩天
    就要試鏡了" naturally in conversation without the operator having
    to inject it manually. Realized beats are rendered separately by
    ``_render_arc_history_block``; today's beat (if any) is handled by
    ``_render_today_scene_directive_block`` so this block stays purely
    informational.
    """
    if arc is None:
        return []
    lines: list[str] = [
        _DIGEST_SOURCE_FRAME,
        "你正在經歷的一段故事（主軸；對話可以自然呼應、但不要背臺詞）：",
        f"- 主題：{arc.title}",
        f"- 前情：{arc.premise}",
    ]
    if upcoming:
        lines.append("接下來即將發生的節奏：")
        for beat in upcoming:
            hint = _TENSION_HINTS.get(beat.tension, beat.tension)
            delta_label = _format_date_delta(today, beat.scheduled_date)
            # Summary is paragraph-length; trim for prompt economy.
            snippet = beat.summary.strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "…"
            lines.append(
                f"- {delta_label}：{beat.title}（{hint}）— {snippet}"
            )
        # KB4: the per-beat labels above are derived from the *schedule*;
        # the summaries were written earlier and may name a different day.
        # One rider covers the whole list.
        lines.append(BEAT_LIST_DATE_DISCIPLINE_LINE)
    # KB3: premise and upcoming beats are the character's own story, not
    # a shared plan — the player has usually never heard any of it.
    lines.append(render_arc_forward_feed_knowledge_line())
    return lines


_ARC_HISTORY_SHARED_HEADING = (
    "這段故事至今你們已經一起經歷過（確實發生過，可自然延續，不要當成未來預告）："
)


def _arc_history_entry(beat: StoryArcBeat) -> str:
    hint = _TENSION_HINTS.get(beat.tension, beat.tension)
    snippet = beat.summary.strip()
    if len(snippet) > 120:
        snippet = snippet[:117] + "…"
    return f"- 《{beat.title}》{snippet}（{hint}）"


def _render_arc_history_block(arc: StoryArc | None) -> list[str]:
    """Realized beats, filed by whether the player was actually there.

    KB7 (``PLAYER_KNOWLEDGE_BOUNDARY_PLAN``): one heading used to claim
    「你們已經一起經歷過」 over the whole list, which is simply false for an
    ``absent`` beat — the character's own solo chapter, handed to her as
    shared history. That is the same confusion that produced 「你是不是又去
    了山區」. ``operator_position`` is already on the beat, so the split is
    material-driven rather than a rider apologising after the fact.

    Unjudged (``None``) beats stay under the shared heading with the KB3
    rider correcting it, rather than being filed as solo: claiming he was
    absent from a scene he may have played makes the character
    re-introduce his own memories to him, which is the error direction
    the plan calls visible-to-the-player (D7).
    """
    if arc is None:
        return []
    beats = arc.realized_history_beats(limit=5)
    if not beats:
        return []
    solo = [b for b in beats if b.operator_position == OPERATOR_POSITION_ABSENT]
    together = [b for b in beats if b.operator_position != OPERATOR_POSITION_ABSENT]
    lines: list[str] = []
    if together:
        lines.append(_ARC_HISTORY_SHARED_HEADING)
        lines.extend(_arc_history_entry(beat) for beat in together)
    if solo:
        lines.append(render_arc_history_solo_heading())
        lines.extend(_arc_history_entry(beat) for beat in solo)
    if any(beat.operator_position is None for beat in together):
        # The rider walks the shared heading back, and only an unjudged
        # beat still needs that: the solo group says what it means, and a
        # list of nothing but central/present beats really was shared —
        # doubting it would have her re-introduce his own memories to him.
        lines.append(render_arc_history_knowledge_line())
    return lines


def _format_date_delta(today: date_type | None, target: date_type) -> str:
    if today is None:
        return target.isoformat()
    delta = (target - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "後天"
    if delta > 0:
        return f"再 {delta} 天"
    if delta == -1:
        return "昨天"
    return f"{-delta} 天前"


# --------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------

def _story_scene(ctx: PromptSectionContext) -> list[str]:
    """起幕 (SC1-C): the player is inside a framed scene right now. Its
    frame *replaces* the scripted-beat directive rather than joining it —
    see ``registry.resolve_scene_exclusivity``."""
    return _render_story_scene_block(ctx.story.story_scene)


def _today_scene(ctx: PromptSectionContext) -> list[str]:
    """Today's scripted beat — directive segment, distinct from the
    narrative material in the ``story_events`` section. Empty for
    characters without active arcs or beats lacking scene structure
    (legacy arcs, gacha-only days)."""
    return _render_today_scene_directive_block(
        arc=ctx.story.story_arc, today=ctx.time.today_local,
    )


def _story_events(ctx: PromptSectionContext) -> list[str]:
    return _render_story_events_block(list(ctx.story.story_events))


def _story_arc(ctx: PromptSectionContext) -> list[str]:
    return _render_story_arc_block(
        arc=ctx.story.story_arc,
        upcoming=list(ctx.story.upcoming_arc_beats),
        today=ctx.time.today_local,
    )


def _arc_history(ctx: PromptSectionContext) -> list[str]:
    return _render_arc_history_block(ctx.story.story_arc)


SECTIONS: tuple[PromptSection, ...] = (
    section("story_scene", _story_scene),
    section("today_scene", _today_scene),
    section("story_events", _story_events),
    section("story_arc", _story_arc),
    section("arc_history", _arc_history),
)
