from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.feature_keys import FEATURE_SCENE_ACCESS
from kokoro_link.contracts.scene_access import SceneAccessContext
from kokoro_link.domain.value_objects.presence_frame import AccessContext
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.scene_access.llm_judge import (
    LLMSceneAccessJudge,
    SceneAccessJudgeError,
)
from kokoro_link.infrastructure.usage.llm_metering import MeteredActiveLLMProvider
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder


class _Model:
    provider_id = "test"
    supports_vision = False

    def __init__(self, text: str) -> None:
        self.text = text

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN001
        assert "Scene Access gate" in prompt
        return self.text

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN001
        yield self.text

    async def list_models(self) -> list[str]:
        return []


class _CapturingModel(_Model):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.prompt = ""

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN001
        self.prompt = prompt
        return self.text


class _Provider:
    def __init__(self, model: _Model) -> None:
        self.model = model

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character=None,  # noqa: ANN001
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> _Model:
        self.last_resolve = (feature_key, character, content_tolerance)
        return self.model

    async def resolve_model_id(
        self,
        feature_key: str | None = None,
        *,
        character=None,  # noqa: ANN001
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> str | None:
        return "scene-model"

    async def is_fake(
        self,
        feature_key: str | None = None,
        *,
        character=None,  # noqa: ANN001
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> bool:
        return False


@pytest.mark.asyncio
async def test_llm_scene_access_judge_parses_json_verdict() -> None:
    judge = LLMSceneAccessJudge(
        model=_Model(
            """
            {
              "decision": "allow",
              "recommended_action": "use_stage",
              "access_context": "scheduled_meetup",
              "reason_for_user": "你們已經約好見面。",
              "prompt_fact": "本輪可以承接事先約定的見面場景。",
              "suggested_opener": null
            }
            """,
        ),
    )

    verdict = await judge.judge(
        SceneAccessContext(
            character_id="c1",
            operator_id="default",
            character_name="Mio",
        ),
    )

    assert verdict.access_context is AccessContext.SCHEDULED_MEETUP
    assert verdict.reason_for_user == "你們已經約好見面。"


@pytest.mark.asyncio
async def test_llm_scene_access_judge_records_metered_operator_id() -> None:
    repo = InMemoryGenerationUsageRepository()
    recorder = BackgroundUsageEventRecorder(repo)
    provider = MeteredActiveLLMProvider(
        inner=_Provider(
            _Model(
                """
                {
                  "decision": "allow",
                  "recommended_action": "use_stage",
                  "access_context": "scheduled_meetup",
                  "reason_for_user": "你們已經約好見面。",
                  "prompt_fact": "本輪可以承接事先約定的見面場景。",
                  "suggested_opener": null
                }
                """,
            ),
        ),
        recorder=lambda: recorder,
    )
    judge = LLMSceneAccessJudge(
        provider=provider,
        feature_key=FEATURE_SCENE_ACCESS,
    )

    await judge.judge(
        SceneAccessContext(
            character_id="c1",
            operator_id="operator-7",
            character_name="Mio",
        ),
    )
    await recorder.flush()

    rows = await repo.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.feature_key == FEATURE_SCENE_ACCESS
    assert row.character_id == "c1"
    assert row.operator_id == "operator-7"


@pytest.mark.asyncio
async def test_llm_scene_access_judge_rejects_malformed_json() -> None:
    judge = LLMSceneAccessJudge(model=_Model("not json"))

    with pytest.raises(SceneAccessJudgeError):
        await judge.judge(
            SceneAccessContext(
                character_id="c1",
                operator_id="default",
                character_name="Mio",
            ),
        )


@pytest.mark.asyncio
async def test_llm_scene_access_judge_parses_json_after_non_json_brace() -> None:
    """A stray non-JSON brace (an aside) before the real object must not make
    the gate give up — it should scan on and parse the genuine verdict."""
    judge = LLMSceneAccessJudge(
        model=_Model(
            "先思考：{角色現在很忙}，但仍可見面。\n\n"
            "```json\n"
            "{\n"
            '  "decision": "allow",\n'
            '  "recommended_action": "use_stage",\n'
            '  "access_context": "scheduled_meetup",\n'
            '  "reason_for_user": "你們已經約好見面。",\n'
            '  "prompt_fact": "本輪可以承接事先約定的見面場景。",\n'
            '  "suggested_opener": null\n'
            "}\n"
            "```",
        ),
    )

    verdict = await judge.judge(
        SceneAccessContext(
            character_id="c1",
            operator_id="default",
            character_name="Mio",
        ),
    )

    assert verdict.access_context is AccessContext.SCHEDULED_MEETUP
    assert verdict.reason_for_user == "你們已經約好見面。"


@pytest.mark.asyncio
async def test_llm_scene_access_judge_renders_schedule_gap_context() -> None:
    model = _CapturingModel(
        """
        {
          "decision": "warn",
          "recommended_action": "use_phone",
          "access_context": "text_message_only",
          "reason_for_user": "目前在行程空檔。",
          "prompt_fact": "本輪先用文字訊息，不要假設使用者已實際到場。",
          "suggested_opener": "你現在方便嗎？"
        }
        """,
    )
    judge = LLMSceneAccessJudge(model=model)

    await judge.judge(
        SceneAccessContext(
            character_id="c1",
            operator_id="default",
            character_name="Mio",
            schedule_context_summary=(
                "目前不在任何已規劃活動段；上一段剛結束：午餐；"
                "下一段預定：練琴。"
            ),
        ),
    )

    assert "行程上下文補充" in model.prompt
    assert "目前不在任何已規劃活動段" in model.prompt
    assert "不要把「沒有活動」當成公共可抵達" in model.prompt
    assert "remote_stage" not in model.prompt


@pytest.mark.asyncio
async def test_llm_scene_access_judge_renders_operator_language_hint() -> None:
    model = _CapturingModel(
        """
        {
          "decision": "block",
          "recommended_action": "use_phone",
          "access_context": "text_message_only",
          "reason_for_user": "It would feel abrupt to enter their private space right now.",
          "prompt_fact": "Use text messages; do not assume the user is physically present.",
          "suggested_opener": "Are you free to talk for a moment?"
        }
        """,
    )
    judge = LLMSceneAccessJudge(model=model)

    await judge.judge(
        SceneAccessContext(
            character_id="c1",
            operator_id="default",
            character_name="Mio",
            operator_primary_language="en-US",
        ),
    )

    assert "玩家可見自然語言輸出語言（BCP 47 標籤）：en-US" in model.prompt
    assert "reason_for_user" in model.prompt
    assert "suggested_opener" in model.prompt


@pytest.mark.asyncio
async def test_llm_scene_access_judge_renders_user_status_and_recent_dialogue() -> None:
    model = _CapturingModel(
        """
        {
          "decision": "allow",
          "recommended_action": "use_stage",
          "access_context": "public_encounter",
          "reason_for_user": "使用者今天也在同一個公開場域。",
          "prompt_fact": "使用者意外出現在現場；角色事前不知情，請自然演出驚訝。",
          "suggested_opener": null
        }
        """,
    )
    judge = LLMSceneAccessJudge(model=model)

    await judge.judge(
        SceneAccessContext(
            character_id="c1",
            operator_id="default",
            character_name="Mio",
            recent_dialogue=(
                "user: 我今天被安排去你們學校演講。",
                "assistant: 聽起來會很忙。",
            ),
            operator_current_status="正在校門口等演講",
            operator_current_status_set_at=datetime(
                2026, 5, 29, 10, 30, tzinfo=timezone.utc,
            ),
        ),
    )

    assert "使用者近期對話線索" in model.prompt
    assert "user: 我今天被安排去你們學校演講。" in model.prompt
    assert "使用者目前狀態" in model.prompt
    assert "正在校門口等演講" in model.prompt
    assert "2026-05-29T10:30+00:00" in model.prompt
    assert "計畫外或一次性理由" in model.prompt


@pytest.mark.asyncio
async def test_llm_scene_access_judge_renders_initial_relationship_block() -> None:
    model = _CapturingModel(
        """
        {
          "decision": "allow",
          "recommended_action": "use_stage",
          "access_context": "established_routine",
          "reason_for_user": "這是共同住所裡的日常共處。",
          "prompt_fact": "使用者與角色有明確同住設定；一般居家活動可作為日常共處。",
          "suggested_opener": null
        }
        """,
    )
    judge = LLMSceneAccessJudge(model=model)

    await judge.judge(
        SceneAccessContext(
            character_id="c1",
            operator_id="default",
            character_name="Mio",
            familiarity_band="stranger",
            initial_relationship_lines=(
                "使用者創角時確認的起始關係設定：",
                "- 關係：貼身小精靈",
                "- 居住安排：住在使用者家裡",
            ),
        ),
    )

    assert "起始關係設定" in model.prompt
    assert "居住安排：住在使用者家裡" in model.prompt
    assert "established_routine" in model.prompt
    assert "不要只因統計低就否定使用者明確設定的關係" in model.prompt


@pytest.mark.asyncio
async def test_llm_scene_access_judge_renders_sleep_intimacy_guidance() -> None:
    """睡眠不再被一律當成脆弱時段擋下：judge prompt 必須帶到「依關係親密度
    判斷」，親密同住伴侶共眠可同場，室友／家人／寵物等仍保守。"""
    model = _CapturingModel(
        """
        {
          "decision": "allow",
          "recommended_action": "use_stage",
          "access_context": "established_routine",
          "reason_for_user": "你們是同住伴侶，現在是共同臥室的休息時段。",
          "prompt_fact": "親密同住伴侶共眠屬共同生活日常，可同場，但不是共同回憶。",
          "suggested_opener": null
        }
        """,
    )
    judge = LLMSceneAccessJudge(model=model)

    await judge.judge(
        SceneAccessContext(
            character_id="c1",
            operator_id="default",
            character_name="Mio",
            initial_relationship_lines=(
                "使用者創角時確認的起始關係設定：",
                "- 關係：伴侶",
                "- 居住安排：住在一起、同床共枕",
            ),
        ),
    )

    assert "睡眠不要一律當成脆弱時段擋下" in model.prompt
    assert "同床共枕屬日常" in model.prompt
    assert (
        "室友、家人、寵物、剛認識或獨居關係，睡眠仍是私密脆弱時段" in model.prompt
    )
