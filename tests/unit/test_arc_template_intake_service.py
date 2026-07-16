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
    assert "鏡子" in result
    assert len(result) <= 250


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
    assert "公告欄" in result
    assert "凜" in result
    assert "她敢報名嗎？" in result


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


@pytest.mark.asyncio
async def test_generate_full_draft_garbage_returns_none(
    tmp_path: Path,
) -> None:
    svc = _service(tmp_path, _FakeModel("不知道"))
    assert await svc.generate_full_draft(pitch="任意") is None


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
