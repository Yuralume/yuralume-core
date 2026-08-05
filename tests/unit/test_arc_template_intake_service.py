"""ArcTemplateIntakeService — wizard backend (Phase 2.7 of SCENE_BEAT_PLAN).

Covers the LLM-driven suggestion methods + save path with stubbed
LLM responses. Robustness is the focus: garbage JSON, LLM timeouts,
fake-provider fallback all get exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kokoro_link.application.services.arc_template_intake_service import (
    ArcTemplateIntakeService,
    BeatContext,
    BeatDraft,
    MetaSuggestions,
    TemplateDraft,
    _build_beat_summary_prompt,
    _build_full_draft_prompt,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)


_TEST_USER_ID = "alice"


class _FakeModel:
    supports_vision = False

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_prompt: str | None = None

    async def generate(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        self.last_prompt = prompt
        return self._response

    def generate_stream(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


class _CrashingModel:
    supports_vision = False

    async def generate(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        raise RuntimeError("LLM provider exploded")

    def generate_stream(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


def _service(tmp_path: Path, model) -> ArcTemplateIntakeService:
    # tmp_path is no longer the storage backend — templates land in the
    # in-memory repo. The parameter stays for signature stability across
    # the existing tests, but it's not touched here.
    del tmp_path
    repo = InMemoryArcTemplateRepository()
    return ArcTemplateIntakeService(repository=repo, model=model)


# ---------- suggest_meta -------------------------------------------


@pytest.mark.asyncio
async def test_suggest_meta_parses_clean_json(tmp_path: Path) -> None:
    payload = json.dumps({
        "titles": ["三週的試鏡", "鋼琴的最後一週", "暗夜中的練習"],
        "themes": ["ambition", "discovery"],
        "tones": ["dramatic", "daily"],
        "world_frames": ["modern", "school"],
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))

    result = await svc.suggest_meta("一個內向的學生準備鋼琴比賽")

    assert isinstance(result, MetaSuggestions)
    assert result.titles[0] == "三週的試鏡"
    assert "ambition" in result.themes
    assert "dramatic" in result.tones
    assert "modern" in result.world_frames


@pytest.mark.asyncio
async def test_suggest_meta_strips_markdown_fences(tmp_path: Path) -> None:
    payload = (
        "```json\n"
        '{"titles": ["a", "b"], "themes": ["loss"], '
        '"tones": ["dark"], "world_frames": []}\n'
        "```"
    )
    svc = _service(tmp_path, _FakeModel(payload))
    result = await svc.suggest_meta("一段安靜的告別")
    assert result.titles == ["a", "b"]
    assert result.themes == ["loss"]


@pytest.mark.asyncio
async def test_suggest_meta_garbage_falls_back_to_pitch_echo(
    tmp_path: Path,
) -> None:
    """LLM returned non-JSON prose → fall back to the static helper.

    The fallback echoes the pitch as a candidate title (so the wizard
    isn't completely blank) and seeds 'custom' as a safe theme. The
    operator can still type their own answers."""
    svc = _service(tmp_path, _FakeModel("這個我幫不了忙"))
    result = await svc.suggest_meta("一段神秘的故事")
    assert result.titles == ["一段神秘的故事"]
    assert "custom" in result.themes


@pytest.mark.asyncio
async def test_suggest_meta_llm_crash_returns_fallback(tmp_path: Path) -> None:
    svc = _service(tmp_path, _CrashingModel())
    result = await svc.suggest_meta("黑暗奇幻征服戰")

    # Static fallback echoes the pitch as a candidate title so the
    # operator at least sees something to pick from.
    assert "黑暗奇幻征服戰" in result.titles[0]
    # Fallback themes always include 'custom' as a safe default.
    assert "custom" in result.themes


@pytest.mark.asyncio
async def test_suggest_meta_blank_pitch_short_circuits(tmp_path: Path) -> None:
    """Empty pitch shouldn't burn an LLM call."""
    model = _FakeModel('{"titles": []}')
    svc = _service(tmp_path, model)
    result = await svc.suggest_meta("   ")
    assert result.titles == []
    # Confirms we didn't even prompt the model.
    assert model.last_prompt is None


# ---------- condense_premise ---------------------------------------


@pytest.mark.asyncio
async def test_condense_premise_returns_text(tmp_path: Path) -> None:
    svc = _service(tmp_path, _FakeModel(
        "週一早上她看見公告欄上的試鏡海報，那天傍晚她偷偷把報名表塞進了信箱。"
        "接下來兩週，她要在鏡子前一次次撞上自己練得不夠的真相。"
    ))
    result = await svc.condense_premise(
        logline="她報名了一場試鏡",
        start_state="平靜的高中生活",
        end_state="知道自己想唱給誰聽",
        tone="dramatic",
    )
    assert "試鏡" in result
    assert len(result) <= 200


@pytest.mark.asyncio
async def test_condense_premise_caps_runaway_output(tmp_path: Path) -> None:
    long = "她" * 500
    svc = _service(tmp_path, _FakeModel(long))
    result = await svc.condense_premise(
        logline="任意 logline", start_state="", end_state="",
    )
    # Truncated + ellipsis suffix.
    assert len(result) <= 201  # 200 cap + ellipsis char
    assert result.endswith("…")


@pytest.mark.asyncio
async def test_condense_premise_blank_logline_returns_empty(
    tmp_path: Path,
) -> None:
    svc = _service(tmp_path, _FakeModel("ignored"))
    assert await svc.condense_premise(
        logline="", start_state="x", end_state="y",
    ) == ""


@pytest.mark.asyncio
async def test_condense_premise_llm_crash_falls_back(tmp_path: Path) -> None:
    svc = _service(tmp_path, _CrashingModel())
    result = await svc.condense_premise(
        logline="她報名了試鏡",
        start_state="平靜",
        end_state="覺醒",
    )
    # Fallback joins the three answers — wizard still moves forward.
    assert "她報名了試鏡" in result
    assert "覺醒" in result


# ---------- suggest_beat_options -----------------------------------


@pytest.mark.asyncio
async def test_suggest_beat_options_parses_full_set(tmp_path: Path) -> None:
    payload = json.dumps({
        "titles": ["公告張貼", "報名表", "鏡子前", "夜的練習"],
        "locations": ["學校公告欄", "自己的房間", "音樂教室", "咖啡廳"],
        "scene_characters": ["", "凜", "指導老師", "母親", "同學"],
        "dramatic_questions": [
            "她敢報名嗎？",
            "她能撐到比賽當天嗎？",
            "她知道自己唱給誰聽嗎？",
            "她願意承認嗎？",
        ],
        "scene_types": ["encounter", "conflict", "revelation"],
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))
    ctx = BeatContext(
        template_title="三週的試鏡",
        premise="她報名了一場試鏡。",
        theme="ambition",
        tone="dramatic",
        duration_days=14,
        world_frames=("modern", "school"),
        beat_position=0,
        total_beats=6,
        day_offset=0,
        tension="setup",
    )
    result = await svc.suggest_beat_options(ctx)
    assert result.titles[0] == "公告張貼"
    assert "學校公告欄" in result.locations
    # Empty-string slot kept (signals "獨白場可選").
    assert "" in result.scene_characters
    assert result.scene_types[0] == "encounter"


@pytest.mark.asyncio
async def test_suggest_beat_options_fallback_includes_tension(
    tmp_path: Path,
) -> None:
    svc = _service(tmp_path, _CrashingModel())
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="daily", duration_days=14, world_frames=(),
        beat_position=0, total_beats=5, day_offset=0,
        tension="rising",
    )
    result = await svc.suggest_beat_options(ctx)
    # Fallback can't propose names, but at least seeds scene_types
    # with the auto-derived tension so the UI has something to show.
    assert result.scene_types == ["rising"]
    assert result.titles == []


# ---------- generate_beat_summary ----------------------------------


@pytest.mark.asyncio
async def test_generate_beat_summary_returns_text(tmp_path: Path) -> None:
    """Model ignores the JSON envelope and returns plain prose (the
    pre-OP1-B contract) — the whole response still becomes the summary,
    and the position proposal is simply absent (stays unjudged) rather
    than the call failing."""
    svc = _service(tmp_path, _FakeModel(
        "鏡子裡只剩自己，呼吸卻還是不夠穩。"
        "老師站在門邊看了一會，沒說話就走開了。"
    ))
    beat = BeatDraft(
        sequence=1, day_offset=5, title="第一次撞牆",
        summary="", tension="rising", scene_type="conflict",
        location="音樂教室", scene_characters=("指導老師",),
        dramatic_question="她要承認嗎？", required=True,
    )
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="dramatic", duration_days=14, world_frames=(),
        beat_position=1, total_beats=6, day_offset=5,
        tension="rising",
    )
    result = await svc.generate_beat_summary(beat=beat, context=ctx)
    assert "鏡子" in result.summary
    assert len(result.summary) <= 250
    assert result.operator_position is None
    assert result.operator_note is None


@pytest.mark.asyncio
async def test_generate_beat_summary_parses_position_proposal(
    tmp_path: Path,
) -> None:
    """OP1-B: the model proposes a player-position pair alongside the
    summary; both land on the returned suggestion so the wizard can
    pre-fill the draft's two OP0 columns."""
    payload = json.dumps({
        "summary": (
            "鏡子裡只剩自己，呼吸卻還是不夠穩。"
            "她抬頭看見你就站在門邊，一句話也說不出口。"
        ),
        "operator_position": "central",
        "operator_note": "她要向你坦白練得不夠的事實",
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))
    beat = BeatDraft(
        sequence=1, day_offset=5, title="第一次撞牆",
        summary="", tension="rising", scene_type="conflict",
        location="音樂教室", scene_characters=("指導老師",),
        dramatic_question="她要向你承認嗎？", required=True,
    )
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="dramatic", duration_days=14, world_frames=(),
        beat_position=1, total_beats=6, day_offset=5,
        tension="rising",
    )
    result = await svc.generate_beat_summary(beat=beat, context=ctx)
    assert "鏡子" in result.summary
    assert result.operator_position == "central"
    assert result.operator_note == "她要向你坦白練得不夠的事實"


@pytest.mark.asyncio
async def test_generate_beat_summary_off_vocabulary_position_degrades_to_unjudged(
    tmp_path: Path,
) -> None:
    """A hallucinated position must not fail the whole summary call —
    it's a review-step suggestion the operator can still edit."""
    payload = json.dumps({
        "summary": "一段普通的摘要文字，長度足夠通過檢查。",
        "operator_position": "protagonist",
        "operator_note": "",
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))
    beat = BeatDraft(sequence=0, day_offset=0, title="t")
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="daily", duration_days=14, world_frames=(),
        beat_position=0, total_beats=4, day_offset=0,
        tension="setup",
    )
    result = await svc.generate_beat_summary(beat=beat, context=ctx)
    assert result.summary == "一段普通的摘要文字，長度足夠通過檢查。"
    assert result.operator_position is None


@pytest.mark.asyncio
async def test_generate_beat_summary_fallback_assembles_skeleton(
    tmp_path: Path,
) -> None:
    svc = _service(tmp_path, _CrashingModel())
    beat = BeatDraft(
        sequence=0, day_offset=0, title="公告",
        summary="", tension="setup", scene_type="encounter",
        location="公告欄", scene_characters=("凜",),
        dramatic_question="她敢報名嗎？", required=True,
    )
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="daily", duration_days=14, world_frames=(),
        beat_position=0, total_beats=4, day_offset=0,
        tension="setup",
    )
    result = await svc.generate_beat_summary(beat=beat, context=ctx)
    # Fallback stitches the structured fields into a usable sentence
    # so the operator can edit rather than start blank.
    assert "公告欄" in result.summary
    assert "凜" in result.summary
    assert "她敢報名嗎？" in result.summary
    # No semantic judgement available in the static fallback — position
    # stays unjudged rather than a heuristic guessing it (LLM-first).
    assert result.operator_position is None
    assert result.operator_note is None


def test_beat_summary_prompt_retires_blanket_exclusion_rule() -> None:
    """OP1-B. Characterized before flipping: the beat-summary prompt
    used to carry a flat 「不要把使用者寫進場景。」 rule that a beat with
    a player-centred dramatic_question would directly contradict.
    Retired in favour of guidance that asks the model to judge the
    player's position honestly (absent/present/central) and propose
    it in the JSON envelope."""
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="daily", duration_days=14, world_frames=(),
        beat_position=0, total_beats=4, day_offset=0,
        tension="setup",
    )
    prompt = _build_beat_summary_prompt(
        beat=BeatDraft(sequence=0, day_offset=0, title="t"),
        context=ctx,
    )
    assert "不要把使用者寫進場景" not in prompt
    assert "operator_position" in prompt
    assert "central" in prompt


# ---------- generate_beat_summary: anchoring an existing decision ---
# (Codex review, M1 — "regenerate summary" used to unconditionally
# overwrite operator_position/operator_note, so an LLM crash, an
# off-vocabulary hallucination, or a model simply judging differently
# on rerun could silently wipe an operator's `central` call back to
# unjudged. `generate_beat_summary` now anchors every return path
# against the beat's own already-decided fields.)


def test_beat_summary_prompt_states_decided_position_as_fixed_fact() -> None:
    """Once decided, the prompt stops asking the model to (re-)judge
    the position from scratch — it states the value as a fixed fact
    the summary should be written around."""
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="dramatic", duration_days=14, world_frames=(),
        beat_position=0, total_beats=4, day_offset=0,
        tension="climax",
    )
    prompt = _build_beat_summary_prompt(
        beat=BeatDraft(
            sequence=0, day_offset=0, title="t",
            operator_position="central",
            operator_note="她要向你坦白",
        ),
        context=ctx,
    )
    assert "central" in prompt
    assert "她要向你坦白" in prompt
    assert "既定事實" in prompt
    # The open three-way judgement instruction is for the still-unjudged
    # case only.
    assert "使用者的位置請依劇情誠實判斷" not in prompt


@pytest.mark.asyncio
async def test_generate_beat_summary_preserves_decided_position_against_llm_override(
    tmp_path: Path,
) -> None:
    """The LLM answers with a *different* valid position on this
    regenerate call — the operator's own prior `central` call still
    wins; the model cannot silently overrule it."""
    payload = json.dumps({
        "summary": "一段新的摘要文字，長度足夠通過檢查用於測試案例。",
        "operator_position": "absent",
        "operator_note": "他完全不在場",
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))
    beat = BeatDraft(
        sequence=0, day_offset=0, title="攤牌",
        operator_position="central",
        operator_note="她要向你坦白練得不夠的事實",
    )
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="dramatic", duration_days=14, world_frames=(),
        beat_position=0, total_beats=4, day_offset=0,
        tension="climax",
    )
    result = await svc.generate_beat_summary(beat=beat, context=ctx)
    assert "新的摘要" in result.summary
    assert result.operator_position == "central"
    assert result.operator_note == "她要向你坦白練得不夠的事實"


@pytest.mark.asyncio
async def test_generate_beat_summary_llm_crash_preserves_decided_position(
    tmp_path: Path,
) -> None:
    """M1 regression: before this fix, an LLM crash during 'regenerate
    summary' fell back to the static skeleton stitcher, which always
    returned operator_position=None — silently clearing an operator's
    `central` call. The static fallback still has no semantic
    judgement to offer (LLM-first: it must not guess a *fresh* value),
    but it must not erase a judgement that already exists either."""
    svc = _service(tmp_path, _CrashingModel())
    beat = BeatDraft(
        sequence=0, day_offset=0, title="攤牌",
        location="頂樓", scene_characters=("同學",),
        dramatic_question="她敢說嗎？",
        operator_position="central",
        operator_note="她要向你坦白",
    )
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="dramatic", duration_days=14, world_frames=(),
        beat_position=0, total_beats=4, day_offset=0,
        tension="climax",
    )
    result = await svc.generate_beat_summary(beat=beat, context=ctx)
    assert result.operator_position == "central"
    assert result.operator_note == "她要向你坦白"


@pytest.mark.asyncio
async def test_generate_beat_summary_preserves_note_but_still_accepts_new_position(
    tmp_path: Path,
) -> None:
    """Gating is per field, independently: the operator only wrote a
    note by hand (position is still unjudged) — regenerating still
    lets the model propose a position, but must not blow away the
    manually-written note."""
    payload = json.dumps({
        "summary": "一段摘要文字，長度足夠通過檢查。",
        "operator_position": "present",
        "operator_note": "模型自己想的 note",
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))
    beat = BeatDraft(
        sequence=0, day_offset=0, title="t",
        operator_note="操作者手寫的 note，不該被蓋掉",
    )
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="daily", duration_days=14, world_frames=(),
        beat_position=0, total_beats=4, day_offset=0,
        tension="setup",
    )
    result = await svc.generate_beat_summary(beat=beat, context=ctx)
    assert result.operator_position == "present"
    assert result.operator_note == "操作者手寫的 note，不該被蓋掉"


@pytest.mark.asyncio
async def test_generate_beat_summary_clips_overlong_operator_note(
    tmp_path: Path,
) -> None:
    """Bonus (Codex review, same family as M1/M2): every other optional
    string this file's LLM-response parsers accept is length-bounded;
    operator_note wasn't. Clamped to the same 120-char bound the
    planner ingest path already enforces
    (OP1-A, ``llm_arc_planner._MAX_OPERATOR_NOTE_CHARS``)."""
    long_note = "她" * 400
    payload = json.dumps({
        "summary": "一段摘要文字，長度足夠通過檢查用於測試。",
        "operator_position": "present",
        "operator_note": long_note,
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))
    beat = BeatDraft(sequence=0, day_offset=0, title="t")
    ctx = BeatContext(
        template_title="t", premise="p", theme="ambition",
        tone="daily", duration_days=14, world_frames=(),
        beat_position=0, total_beats=4, day_offset=0,
        tension="setup",
    )
    result = await svc.generate_beat_summary(beat=beat, context=ctx)
    assert result.operator_note is not None
    assert len(result.operator_note) <= 120


# ---------- generate_full_draft ------------------------------------


@pytest.mark.asyncio
async def test_generate_full_draft_returns_complete_template(
    tmp_path: Path,
) -> None:
    payload = json.dumps({
        "id": "test_full_draft",
        "title": "三週的試鏡",
        "premise": "她報名了一場試鏡。" * 3,
        "theme": "ambition",
        "tone": "dramatic",
        "duration_days": 14,
        "world_frames": ["modern", "school"],
        "required_traits": [],
        "beats": [
            {
                "sequence": 0, "day_offset": 0,
                "title": "公告", "summary": "公告欄前停了三秒。" * 5,
                "tension": "setup", "scene_type": "encounter",
                "location": "學校公告欄",
                "scene_characters": [],
                "dramatic_question": "她敢嗎？",
                "required": True,
            },
            {
                "sequence": 1, "day_offset": 5,
                "title": "撞牆", "summary": "鏡子前的呼吸。" * 5,
                "tension": "rising", "scene_type": "conflict",
                "location": "音樂教室",
                "scene_characters": ["指導老師"],
                "dramatic_question": "她要承認嗎？",
                "required": True,
                "operator_position": "present",
                "operator_note": "你在一旁看著她練習",
            },
            {
                "sequence": 2, "day_offset": 14,
                "title": "結果", "summary": "電車上的震動。" * 5,
                "tension": "resolution", "scene_type": "resolution",
                "location": "電車上",
                "scene_characters": [],
                "dramatic_question": "她要怎麼面對？",
                "required": True,
            },
        ],
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))
    draft = await svc.generate_full_draft(
        pitch="一個學生準備鋼琴試鏡的故事",
    )
    assert draft is not None
    assert draft.id == "test_full_draft"
    assert draft.tone == "dramatic"
    assert len(draft.beats) == 3
    assert draft.beats[1].location == "音樂教室"
    assert draft.beats[1].scene_characters == ("指導老師",)
    # OP0-B: the full-draft LLM parse must carry the player-position
    # pair the same as every other beat field. A beat that omits it
    # (beat 0 here) still degrades to unjudged rather than failing —
    # some model responses will skip it even though the prompt asks
    # (M2, below, is what makes the prompt ask in the first place).
    assert draft.beats[1].operator_position == "present"
    assert draft.beats[1].operator_note == "你在一旁看著她練習"
    assert draft.beats[0].operator_position is None
    assert draft.beats[0].operator_note is None


@pytest.mark.asyncio
async def test_generate_full_draft_garbage_returns_none(
    tmp_path: Path,
) -> None:
    svc = _service(tmp_path, _FakeModel("不知道"))
    assert await svc.generate_full_draft(pitch="任意") is None


@pytest.mark.asyncio
async def test_generate_full_draft_off_vocabulary_position_degrades_to_unjudged(
    tmp_path: Path,
) -> None:
    """A hallucinated position must not fail the whole draft — it's a
    review-step suggestion the operator can still edit, not a save."""
    payload = json.dumps({
        "id": "test_bad_position",
        "title": "標題",
        "premise": "一段測試 premise。" * 5,
        "theme": "ambition",
        "beats": [
            {
                "sequence": 0, "day_offset": 0,
                "title": "起點", "summary": "場景摘要。" * 5,
                "operator_position": "protagonist",
            },
        ],
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))
    draft = await svc.generate_full_draft(pitch="任意")
    assert draft is not None
    assert draft.beats[0].operator_position is None


# ---------- generate_full_draft: fast-path asks for player position -
# (Codex review, M2 — the one-shot "全部交給 AI" prompt never asked
# the model for operator_position/operator_note at all, so every beat
# it produced landed unjudged even though the parse side (OP0-B) has
# supported both columns since day one.)


def test_full_draft_prompt_asks_for_player_position_schema() -> None:
    prompt = _build_full_draft_prompt(
        pitch="p", hint="", operator_primary_language="zh-TW",
    )
    assert "operator_position" in prompt
    assert "operator_note" in prompt
    assert "absent" in prompt and "present" in prompt and "central" in prompt


def test_full_draft_prompt_gives_mixed_guidance_not_a_fixed_ratio() -> None:
    """LLM-first (紅線 1): the guidance must read as a semantic
    instruction the model judges per beat, not a hardcoded ratio or a
    keyword rule."""
    prompt = _build_full_draft_prompt(
        pitch="p", hint="", operator_primary_language="zh-TW",
    )
    assert "不要按固定比例分配" in prompt
    assert "climax" in prompt or "resolution" in prompt  # tension cue present


@pytest.mark.asyncio
async def test_generate_full_draft_fast_path_round_trips_player_position(
    tmp_path: Path,
) -> None:
    """End-to-end fast-path test: a model that answers the (now-asked)
    player-position pair for every beat round-trips cleanly through
    ``generate_full_draft`` — and the prompt it was sent actually asked
    for it."""
    payload = json.dumps({
        "id": "full_draft_with_positions",
        "title": "測試 arc",
        "premise": "一段測試 premise。" * 3,
        "theme": "ambition",
        "beats": [
            {
                "sequence": 0, "day_offset": 0,
                "title": "日常", "summary": "一段摘要文字。" * 5,
                "operator_position": "absent", "operator_note": "",
            },
            {
                "sequence": 1, "day_offset": 7,
                "title": "攤牌", "summary": "一段摘要文字。" * 5,
                "operator_position": "central",
                "operator_note": "她要在這裡把話說開",
            },
        ],
    }, ensure_ascii=False)
    model = _FakeModel(payload)
    svc = _service(tmp_path, model)

    draft = await svc.generate_full_draft(pitch="任意 pitch")

    assert model.last_prompt is not None
    assert "operator_position" in model.last_prompt
    assert "central" in model.last_prompt
    assert draft is not None
    assert draft.beats[0].operator_position == "absent"
    assert draft.beats[1].operator_position == "central"
    assert draft.beats[1].operator_note == "她要在這裡把話說開"


@pytest.mark.asyncio
async def test_generate_full_draft_clips_overlong_operator_note(
    tmp_path: Path,
) -> None:
    """Bonus (Codex review, same family as M1/M2): ``operator_note``
    wasn't length-clamped in the full-draft parser while every other
    optional string was. Clamped to the same 120-char bound the
    planner ingest path already enforces
    (OP1-A, ``llm_arc_planner._MAX_OPERATOR_NOTE_CHARS``)."""
    long_note = "她" * 400
    payload = json.dumps({
        "id": "clip_test",
        "title": "標題",
        "premise": "一段測試 premise。" * 5,
        "theme": "ambition",
        "beats": [
            {
                "sequence": 0, "day_offset": 0,
                "title": "起點", "summary": "場景摘要。" * 5,
                "operator_position": "present",
                "operator_note": long_note,
            },
        ],
    }, ensure_ascii=False)
    svc = _service(tmp_path, _FakeModel(payload))
    draft = await svc.generate_full_draft(pitch="任意")
    assert draft is not None
    assert draft.beats[0].operator_note is not None
    assert len(draft.beats[0].operator_note) <= 120


# ---------- save_template ------------------------------------------


@pytest.mark.asyncio
async def test_save_template_writes_via_repository(tmp_path: Path) -> None:
    svc = _service(tmp_path, _FakeModel("ignored"))
    draft = TemplateDraft(
        id="saved_via_intake",
        title="儲存測試",
        premise="一段測試 premise，要夠長才能驗證通過。",
        theme="ambition",
        tone="dramatic",
        duration_days=14,
        world_frames=("modern",),
        beats=(
            BeatDraft(
                sequence=0, day_offset=0, title="t1",
                summary="場景 1 的摘要。",
            ),
            BeatDraft(
                sequence=1, day_offset=7, title="t2",
                summary="場景 2 的摘要。",
            ),
        ),
    )
    saved_id = await svc.save_template(draft, user_id=_TEST_USER_ID)
    assert saved_id == "saved_via_intake"
    # Filesystem is no longer the storage backend — saved drafts live
    # in the repo (in-memory in this test) and are visible to their
    # owner only.
    template = await svc._repository.get_for_user(
        "saved_via_intake", user_id=_TEST_USER_ID,
    )
    assert template is not None
    assert template.title == "儲存測試"


@pytest.mark.asyncio
async def test_save_template_round_trips_the_player_position_pair(
    tmp_path: Path,
) -> None:
    svc = _service(tmp_path, _FakeModel("ignored"))
    draft = TemplateDraft(
        id="saved_with_position",
        title="玩家位置測試",
        premise="一段測試 premise，要夠長才能驗證通過。",
        theme="ambition",
        beats=(
            BeatDraft(
                sequence=0, day_offset=0, title="t1",
                summary="場景 1 的摘要。",
                operator_position="central",
                operator_note="她要向你坦白",
            ),
        ),
    )
    await svc.save_template(draft, user_id=_TEST_USER_ID)

    template = await svc._repository.get_for_user(
        "saved_with_position", user_id=_TEST_USER_ID,
    )
    assert template is not None
    assert template.beats[0].operator_position == "central"
    assert template.beats[0].operator_note == "她要向你坦白"


@pytest.mark.asyncio
async def test_save_template_raises_on_empty_beats(tmp_path: Path) -> None:
    svc = _service(tmp_path, _FakeModel("ignored"))
    draft = TemplateDraft(
        id="empty",
        title="空 beats",
        premise="一段測試 premise。",
        theme="ambition",
    )
    with pytest.raises(ValueError, match="beats"):
        await svc.save_template(draft, user_id=_TEST_USER_ID)


def _minimal_draft(template_id: str, *, language: str = "") -> TemplateDraft:
    return TemplateDraft(
        id=template_id,
        title="語言標記測試",
        premise="一段測試 premise，要夠長才能驗證通過。",
        theme="ambition",
        language=language,
        beats=(
            BeatDraft(
                sequence=0, day_offset=0, title="t1",
                summary="場景 1 的摘要。",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_save_template_stamps_operator_language_when_draft_omits_it(
    tmp_path: Path,
) -> None:
    """en-US operator saves a wizard draft without picking a language ->
    the saved row should carry the operator's language, not the domain
    default (zh-TW). This is the Phase A0 bug: save/PATCH never passed
    language through, so every self-authored template silently landed
    as zh-TW regardless of who wrote it."""
    svc = _service(tmp_path, _FakeModel("ignored"))
    draft = _minimal_draft("lang_fallback")
    await svc.save_template(
        draft, user_id=_TEST_USER_ID, operator_language="en-US",
    )
    template = await svc._repository.get_for_user(
        "lang_fallback", user_id=_TEST_USER_ID,
    )
    assert template is not None
    assert template.language == "en-US"


@pytest.mark.asyncio
async def test_save_template_prefers_explicit_draft_language(
    tmp_path: Path,
) -> None:
    """If the draft itself carries a language (e.g. imported content),
    that takes precedence over the operator's stored primary language."""
    svc = _service(tmp_path, _FakeModel("ignored"))
    draft = _minimal_draft("lang_explicit", language="ja-JP")
    await svc.save_template(
        draft, user_id=_TEST_USER_ID, operator_language="en-US",
    )
    template = await svc._repository.get_for_user(
        "lang_explicit", user_id=_TEST_USER_ID,
    )
    assert template is not None
    assert template.language == "ja-JP"


@pytest.mark.asyncio
async def test_save_template_defaults_to_zh_tw_when_nothing_supplied(
    tmp_path: Path,
) -> None:
    """No draft language, no operator_language passed -> domain default
    (zh-TW) still applies. Keeps backward compatibility for any caller
    that hasn't been updated to pass operator_language yet."""
    svc = _service(tmp_path, _FakeModel("ignored"))
    draft = _minimal_draft("lang_default")
    await svc.save_template(draft, user_id=_TEST_USER_ID)
    template = await svc._repository.get_for_user(
        "lang_default", user_id=_TEST_USER_ID,
    )
    assert template is not None
    assert template.language == "zh-TW"
