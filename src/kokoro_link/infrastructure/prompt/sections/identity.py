"""Identity-zone prompt renderers: operator identity, birthday,
aftermath residue, knowledge boundary and register."""

from datetime import (
    date as date_type,
    datetime,
    timezone,
    tzinfo,
)

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_lines,
)
from kokoro_link.infrastructure.prompt.player_persona_note_lines import (
    render_player_persona_note_lines,
)
from kokoro_link.infrastructure.prompt.register_blocks import (
    render_turn_register_block,
)
from kokoro_link.infrastructure.prompt.role_boundary import (
    render_role_knowledge_boundary_lines,
)
from kokoro_link.infrastructure.prompt.sections.context import (
    PromptSectionContext,
)
from kokoro_link.infrastructure.prompt.sections.registry import (
    PromptSection,
    section,
)
from kokoro_link.infrastructure.prompt.sections.text import _clip


def _render_operator_language_block(
    operator: "OperatorProfile | None",
) -> list[str]:
    """Thin adapter around the shared helper — keeps the existing
    block-shaped composition in ``build()`` while letting other LLM
    jobs reuse the same wording via
    ``infrastructure.prompt.operator_language``."""
    if operator is None:
        return []
    return render_operator_language_lines(operator.primary_language)


def _render_operator_block(
    operator: "OperatorProfile | None",
    resolved: "ResolvedAddress | None" = None,
) -> list[str]:
    """Render the "對方身份（使用者）" block at the very top of the
    prompt — gives the model a name to attach to the role label
    "使用者" appearing in directives and history lines.

    Phase 1 of the world-system roadmap: when the operator hasn't
    saved a real name yet (``has_real_name() == False``), the block
    renders nothing so legacy "使用者" wording everywhere else still
    reads naturally. Once a real name is saved we surface name +
    aliases + pronouns so the model never has to guess at "他/她"
    pronouns and can pick up the operator by name in cross-character
    contexts later (when other characters appear in memories).

    Multi-character / world phase will extend this with a roster of
    fellow characters; today we only emit the operator's identity.

    When a ``resolved`` address is supplied (the bidirectional address
    resolver, run by the caller with seed + persona + profile in hand),
    its primary term becomes 「稱呼」 and the recognised alternates become
    「別稱」 — so a per-character seed name or learned name outranks the
    global display name, and old names still resolve to the same person.
    Falls back to the legacy display-name rendering when no resolver
    result is passed (keeps non-chat callers untouched)."""
    if resolved is not None and not resolved.is_fallback:
        lines = [
            "對方身份（即角色設定中所說的「使用者」）：",
            f"- 稱呼：{resolved.primary}",
        ]
        if resolved.aliases:
            lines.append(f"- 別稱：{', '.join(resolved.aliases)}")
        if operator is not None and operator.pronouns:
            lines.append(f"- 代名詞：{operator.pronouns}")
        lines.append(
            "在自然對話中可直接用以上稱呼/別稱稱呼對方；"
            "若對方先以特定暱稱自稱，優先使用對方剛剛用的版本。",
        )
        return lines
    if operator is None or not operator.has_real_name():
        return []
    lines = [
        "對方身份（即角色設定中所說的「使用者」）：",
        f"- 稱呼：{operator.display_name}",
    ]
    if operator.aliases:
        lines.append(f"- 別稱：{', '.join(operator.aliases)}")
    if operator.pronouns:
        lines.append(f"- 代名詞：{operator.pronouns}")
    lines.append(
        "在自然對話中可直接用以上稱呼/別稱稱呼對方；"
        "若對方先以特定暱稱自稱，優先使用對方剛剛用的版本。",
    )
    return lines


_BIRTHDAY_SOON_DAYS = 7
"""距離下一次生日 ≤ 這個數字（天）就會在提示裡加上「再 N 天就是生日」
的明示，鼓勵模型自然地帶到準備或期待。再多天的話就只保留靜態欄位
（年齡 / 星座），避免在生日還很遠時硬把話題拉回去。"""


def _render_birthday_block(
    *,
    character: Character,
    today: "date_type | None",
) -> list[str]:
    """Inject the character's age / zodiac / birthday cadence.

    Always-on (static) lines: 出生日期、年齡、星座 — gives the model
    constant background so age-appropriate phrasing emerges naturally
    without us prescribing it.

    Conditional cadence lines: when today *is* the birthday, or the
    next birthday is within the soon-window, an extra directive
    surfaces so the model can lead with the celebration (or quietly
    anticipate it) instead of having to discover the date itself.

    Returns ``[]`` when the operator hasn't set ``date_of_birth`` so
    legacy characters and unfinished imports stay completely
    unaffected.
    """
    if character.date_of_birth is None:
        return []
    if today is None:
        # No reference date means we can't compute age / cadence safely;
        # surface only the raw DOB so the model still has the static
        # context (rare path — caller almost always passes today_local).
        dob = character.date_of_birth
        return [
            "個人資料（生日相關，請自然帶入對話，不要照稿念）：",
            f"- 生日：{dob.month} 月 {dob.day} 日",
        ]
    ctx = character.birthday_context(today)
    if ctx is None:
        return []
    lines = [
        "個人資料（生日相關，請自然帶入對話，不要照稿念）：",
        f"- 生日：{ctx.dob.month} 月 {ctx.dob.day} 日",
        f"- 目前年齡：{ctx.age} 歲（依現實日期推算，會隨時間自然成長）",
        f"- 星座：{ctx.zodiac}",
    ]
    if ctx.is_today:
        lines.append(
            "- 【今天就是你的生日】可以自然地讓對話帶到這件事，"
            "看是想要對方記得、撒嬌、要禮物、低調帶過、還是裝作沒事，"
            "都依角色性格決定；不要刻意提醒對方「今天是我生日」三遍，"
            "也不要在對方主動祝賀前完全裝作不知道。",
        )
    elif 0 < ctx.days_until_next <= _BIRTHDAY_SOON_DAYS:
        lines.append(
            f"- 距離下一次生日還有 {ctx.days_until_next} 天，"
            "可在自然處流露期待、抱怨、計畫、或想要的禮物提示，"
            "但不要每一輪都繞回生日這個話題。",
        )
    lines.append(
        "以上資訊只是讓你知道自己的年齡、生日與星座；星座僅作為閒聊話題，"
        "不是命運導向，請不要把它當成宿命論依據。",
    )
    return lines


_AFTERMATH_TAG = "aftermath"
"""Tag set by :class:`ScheduleMemorializer` on episodic memories whose
LLM-judged residue is worth promoting. The prompt builder uses it (and
nothing else) to decide which memories belong in the 情緒尾韻 block."""


_RESIDUE_FRESH_WINDOW_HOURS = 24
"""How long an aftermath stays in the prime-position block. Past this
window the memory still lives in the regular memory recall path but
stops crowding out the start of the prompt — models psychological
decay: yesterday's annoyance shouldn't pollute today's mood unless the
user brings it up."""


def _render_residue_block(
    *,
    memories: list[MemoryItem],
    now: datetime | None,
) -> list[str]:
    """Promote fresh aftermath memories to a dedicated 情緒尾韻 block.

    Filter rules: memory must carry the ``aftermath`` tag (set by the
    schedule memorialiser when the LLM judged a notable residue) and
    must have been created within the last 24h. Sort newest first so
    the most recent feeling dominates the model's framing of the
    current turn.

    Empty list when no fresh residues — keeps uneventful days lean.
    """
    if not memories:
        return []
    fresh: list[MemoryItem] = []
    for memory in memories:
        if _AFTERMATH_TAG not in memory.tags:
            continue
        if now is None:
            # Caller didn't pass a clock — skip the freshness filter
            # and let every aftermath through. Production callers
            # always pass ``now``; this guards tests / replay paths.
            fresh.append(memory)
            continue
        created = memory.created_at
        if created.tzinfo is None:
            # In-memory test fixtures often produce naive UTC datetimes;
            # treat them as UTC so the freshness window stays correct.
            created = created.replace(tzinfo=timezone.utc)
        elapsed_hours = (now - created).total_seconds() / 3600.0
        if 0.0 <= elapsed_hours <= _RESIDUE_FRESH_WINDOW_HOURS:
            fresh.append(memory)
    if not fresh:
        return []
    # Newest first — most recent feeling dominates the current turn.
    fresh.sort(key=lambda m: m.created_at, reverse=True)
    lines = [
        "最近活動的情緒尾韻（新→舊；這些是你剛經歷的活動還沒散去的感覺，"
        "可以自然地讓對方感覺到，例如語氣帶煩、語氣偏快、想抱怨、心情很好"
        "等等；若話題相關時可以主動帶出來抱怨／分享，但不要照念，"
        "也不要硬背每一條都講一遍）：",
    ]
    for memory in fresh:
        lines.append(f"- {memory.content}")
    return lines


def _render_knowledge_boundary_block() -> list[str]:
    """Authorise the character to admit ignorance / lapses of memory.

    Without this block the LLM defaults to "answer everything
    confidently" — which is fine for a Q&A bot but breaks the illusion
    of a person. People don't know things outside their interests / age
    bracket / life experience, and they don't perfectly recall every
    past conversation. We hand the model the semantic axes (persona /
    age / interests / summary / 過去事件 memories) and let *it* judge
    whether the current question is in-scope, rather than enumerating
    "topics the character should reject" — that path violates the
    project's top directive (no keyword whitelists / hardcoded
    branching).

    Placed right after the birthday block so persona + age + scope are
    read as one coherent unit before the model meets the numeric state.
    """
    return render_role_knowledge_boundary_lines()


def _render_phrase_habit_block(lines: list[str]) -> list[str]:
    habits = [_clip(item, 120) for item in lines if item and item.strip()]
    if not habits:
        return []
    rendered = [
        "角色口吻習慣（來自近期回覆觀察；作為語氣參考，不是固定口頭禪）：",
    ]
    for habit in habits[:3]:
        rendered.append(f"- 可自然延續：{habit}")
    rendered.append(
        "- 不要每句都套用，也不要直接解釋這些觀察；若與角色設定或當下情緒衝突，以當下語境為準。"
    )
    return rendered


# Extracted to ``register_blocks.py`` so encounter/background surfaces share
# the exact same rails; kept as module aliases for existing callers.
_render_turn_register_block = render_turn_register_block


_REGISTER_PACE_PHRASES: dict[str, str] = {
    "more_active": "對方明確希望你「主動一點 / 多話一點」",
    "balanced": "對方對對話節奏沒有特別偏好",
    "more_quiet": "對方明確希望你「安靜一點 / 多留白」",
}


_REGISTER_FORMALITY_PHRASES: dict[str, str] = {
    "low": "對方說話很放鬆，不太用敬語（暱稱、口語、表情符號常見）",
    "medium": "對方說話的敬語層級中等（禮貌但不過度正式）",
    "high": "對方明顯偏正式（用敬語、不省略主詞、語句完整）",
}


_REGISTER_LENGTH_PHRASES: dict[str, str] = {
    "short": "對方偏好短句、快節奏（一兩句就換話題）",
    "medium": "對方偏好中等長度（句子完整但不冗長）",
    "long": "對方偏好長段、慢慢說明（願意讀完一段話）",
}


def _render_register_block(
    *,
    character: Character,
    address_preference,
    resolved_character_address: "ResolvedAddress | None" = None,
) -> list[str]:
    """HUMANIZATION_ROADMAP §4.2 — operator register / pace fact-layer block.

    Owner decision (2026-05-21): the **observed** ``OperatorAddressPreference``
    (§4.2) takes priority over the **explicit** ``operator_pace_preference``
    knob (§3.6). When both exist the observation leads and the explicit
    setting is demoted to a "secondary cue" bullet — the LLM still sees
    both, just ordered freshest-first.

    Returns an empty list when neither signal is set so the prompt stays
    quiet in the cold-start case.
    """
    observed: list[str] = []
    # Resolved character-direction address (seed > observed salutation)
    # owns the 「對方稱呼你」 slot when it carries a real signal — so an
    # explicit seed surfaces even before any observation. The
    # character-name fallback is intentionally not surfaced so the
    # cold-start prompt stays quiet about an unobserved salutation.
    resolved_salutation = ""
    if (
        resolved_character_address is not None
        and resolved_character_address.provenance.value
        in {"explicit_seed", "observed_preference"}
    ):
        resolved_salutation = resolved_character_address.primary
    has_pref = address_preference is not None and not address_preference.is_empty
    salutation = resolved_salutation or (
        address_preference.salutation if has_pref else ""
    )
    if salutation:
        observed.append(f"- 對方稱呼你：{salutation}")
    if has_pref:
        formality_phrase = _REGISTER_FORMALITY_PHRASES.get(
            address_preference.formality_level,
        )
        if formality_phrase:
            observed.append(f"- {formality_phrase}")
        length_phrase = _REGISTER_LENGTH_PHRASES.get(
            address_preference.response_length_pref,
        )
        if length_phrase:
            observed.append(f"- {length_phrase}")
    pace_phrase = _REGISTER_PACE_PHRASES.get(
        (character.operator_pace_preference or "").strip(),
    )
    if not observed and not pace_phrase:
        return []
    lines = ["對方說話風格與期望節奏（事實層，自然反映於你的回覆）："]
    lines.extend(observed)
    if pace_phrase:
        prefix = "- 〔顯式設定〕" if observed else "- "
        lines.append(f"{prefix}{pace_phrase}")
    return lines


# --------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------

def _operator_language(ctx: PromptSectionContext) -> list[str]:
    return _render_operator_language_block(ctx.identity.operator)


def _operator_identity(ctx: PromptSectionContext) -> list[str]:
    return _render_operator_block(
        ctx.identity.operator, ctx.identity.resolved_player_address,
    )


def _address_change(ctx: PromptSectionContext) -> list[str]:
    """Latest per-pair rename, rendered upstream by ``ChatService`` from
    the address-change log (one event per direction). Sits right under the
    operator identity so the character acknowledges the new term while
    linking older references to the same person."""
    return list(ctx.identity.address_change_lines)


def _player_persona_note(ctx: PromptSectionContext) -> list[str]:
    """Adjacent to the inferred portrait but never merged into it: one
    block is what the player said is true, the other is what the character
    worked out. Collapsing them would invite the model to treat a
    declaration as a guess it may doubt."""
    return render_player_persona_note_lines(ctx.identity.player_persona_note)


def _operator_persona(ctx: PromptSectionContext) -> list[str]:
    """Built upstream by ``OperatorPersonaService.render_for_prompt`` so
    this module doesn't have to know about ProfileField shapes / layer
    rules — keeps the prompt builder a pure formatter and the service free
    to evolve its rendering policy."""
    return list(ctx.identity.operator_persona_lines)


def _peer_roster(ctx: PromptSectionContext) -> list[str]:
    return list(ctx.identity.peer_roster_lines)


def _character_profile(ctx: PromptSectionContext) -> list[str]:
    character = ctx.identity.character
    return [
        "角色設定：",
        f"- 名稱：{character.name}",
        f"- 簡介：{character.summary}",
        *render_character_identity_lines(character),
        f"- 外觀：{character.appearance or '（未設定）'}",
        f"- 性格：{', '.join(character.personality) or '無'}",
        f"- 興趣：{', '.join(character.interests) or '無'}",
        f"- 說話風格：{character.speaking_style}",
        f"- 禁忌：{', '.join(character.boundaries) or '無'}",
    ]


def _disposition(ctx: PromptSectionContext) -> list[str]:
    """內在動機傾向（四維 qualitative band）—— 全 medium 時 to_prompt_lines
    回空 list，所以不需要額外的 if-else 跳過。LLM-first 紅線：禁止在這層或
    下游 heuristic 讀 disposition 的個別欄位做分支條件。"""
    return ctx.identity.character.disposition.to_prompt_lines()


def _personality_type(ctx: PromptSectionContext) -> list[str]:
    return ctx.identity.character.personality_type.to_prompt_lines()


def _body_state(ctx: PromptSectionContext) -> list[str]:
    """HUMANIZATION_ROADMAP §4.1 —— 具身訊號（hunger / thirst / sleep_debt /
    seasonal_allergy）。全 low 時 to_prompt_lines 自己回空；非 low 維度自然
    體現於語氣，**禁止**程式分支讀取（owner decision 2026-05-21）。

    §4.6 的 ``body_state`` 變體與 humanization 開關會整段抹掉它——那條在
    ``registry.resolve_experiment_overlay``，不在這裡。"""
    return ctx.identity.character.body_state.to_prompt_lines()


def _register(ctx: PromptSectionContext) -> list[str]:
    """HUMANIZATION_ROADMAP §4.2 — operator register / address preference.

    Owner decision (2026-05-21): observation takes priority over the §3.6
    explicit pace knob; falls back to pace_preference when the observation
    buffer is empty."""
    return _render_register_block(
        character=ctx.identity.character,
        address_preference=(
            ctx.identity.address_preference
            if ctx.rails.address_preference_enabled
            else None
        ),
        resolved_character_address=ctx.identity.resolved_character_address,
    )


def _turn_register(ctx: PromptSectionContext) -> list[str]:
    return _render_turn_register_block(ctx.dialogue.turn_register_profile)


def _phrase_habit(ctx: PromptSectionContext) -> list[str]:
    return _render_phrase_habit_block(list(ctx.dialogue.phrase_habit_lines))


def _birthday(ctx: PromptSectionContext) -> list[str]:
    return _render_birthday_block(
        character=ctx.identity.character, today=ctx.time.today_local,
    )


def _knowledge_boundary(ctx: PromptSectionContext) -> list[str]:
    return _render_knowledge_boundary_block()


def _residue(ctx: PromptSectionContext) -> list[str]:
    return _render_residue_block(
        memories=list(ctx.state.memories), now=ctx.time.now,
    )


def _initial_relationship(ctx: PromptSectionContext) -> list[str]:
    return list(ctx.identity.initial_relationship_lines)


SECTIONS: tuple[PromptSection, ...] = (
    section("operator_language", _operator_language),
    section("operator_identity", _operator_identity),
    section("address_change", _address_change),
    section("player_persona_note", _player_persona_note),
    section("operator_persona", _operator_persona),
    section("peer_roster", _peer_roster),
    section("character_profile", _character_profile),
    section("disposition", _disposition),
    section("personality_type", _personality_type),
    section("body_state", _body_state),
    section("register", _register),
    section("turn_register", _turn_register),
    section("phrase_habit", _phrase_habit),
    section("birthday", _birthday),
    section("knowledge_boundary", _knowledge_boundary),
    section("residue", _residue),
    section("initial_relationship", _initial_relationship),
)
