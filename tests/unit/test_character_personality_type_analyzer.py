import json

import pytest

from kokoro_link.contracts.character_personality_type import (
    CharacterPersonalityTypeAnalysisInput,
)
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.infrastructure.character_personality_type.llm_analyzer import (
    LLMCharacterPersonalityTypeAnalyzer,
)


class _FakeModel:
    supports_vision = False

    def __init__(self, response: str) -> None:
        self.response = response
        self.last_prompt = ""

    async def generate(self, prompt: str, **kwargs):  # noqa: ANN003
        self.last_prompt = prompt
        return self.response

    def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        async def _empty():
            if False:
                yield ""
        return _empty()


@pytest.mark.asyncio
async def test_analyzer_parses_suggestion() -> None:
    model = _FakeModel(json.dumps({
        "suggested_code": "ISTJ",
        "confidence": 0.82,
        "source": "llm_inferred",
        "is_consistent": True,
        "conflict_level": "none",
        "rationale": "重視計畫與責任感。",
        "conflict_notes": ["具體人設優先。"],
        "user_questions": [],
    }, ensure_ascii=False))
    analyzer = LLMCharacterPersonalityTypeAnalyzer(model=model)

    result = await analyzer.analyze(
        CharacterPersonalityTypeAnalysisInput(
            name="澄香",
            summary="嚴謹、按部就班的圖書管理員。",
            personality=("細心", "可靠"),
            speaking_style="短句，會先整理重點。",
        )
    )

    assert result.suggested_type.code == "ISTJ"
    assert result.suggested_type.source == "llm_inferred"
    assert result.suggested_type.confidence == 0.82
    assert result.conflict_level == "none"
    assert "使用者手選類型： （未手選）" not in model.last_prompt
    assert "不要擅自覆蓋" in model.last_prompt


@pytest.mark.asyncio
async def test_user_selected_type_is_checked_not_overwritten() -> None:
    model = _FakeModel(json.dumps({
        "suggested_code": "ENTP",
        "confidence": 0.65,
        "source": "user_explicit",
        "is_consistent": False,
        "conflict_level": "blocking",
        "rationale": "使用者手選類型與按部就班的人設有反差。",
        "conflict_notes": ["需要補充她的彈性從哪裡來。"],
        "user_questions": ["你想保留這個反差，還是改成更重視計畫的類型？"],
    }, ensure_ascii=False))
    analyzer = LLMCharacterPersonalityTypeAnalyzer(model=model)

    result = await analyzer.analyze(
        CharacterPersonalityTypeAnalysisInput(
            summary="嚴謹、按部就班、重視計畫。",
            user_selected_type=CharacterPersonalityType(code="ENTP"),
        )
    )

    assert result.is_blocking is True
    assert result.suggested_type.code == "ENTP"
    assert result.user_questions == (
        "你想保留這個反差，還是改成更重視計畫的類型？",
    )
    assert "使用者手選類型：ENTP" in model.last_prompt


@pytest.mark.asyncio
async def test_bad_json_falls_back_to_selected_type() -> None:
    analyzer = LLMCharacterPersonalityTypeAnalyzer(model=_FakeModel("not json"))
    result = await analyzer.analyze(
        CharacterPersonalityTypeAnalysisInput(
            user_selected_type=CharacterPersonalityType(code="INFP"),
        )
    )

    assert result.suggested_type.code == "INFP"
    assert result.conflict_level == "none"


@pytest.mark.asyncio
async def test_unknown_code_fails_soft_to_fallback() -> None:
    analyzer = LLMCharacterPersonalityTypeAnalyzer(
        model=_FakeModel(json.dumps({
            "suggested_code": "XXXX",
            "confidence": 0.9,
            "source": "llm_inferred",
            "is_consistent": True,
            "conflict_level": "none",
            "rationale": "bad",
        }))
    )
    result = await analyzer.analyze(
        CharacterPersonalityTypeAnalysisInput(
            current_type=CharacterPersonalityType(code="ISFP"),
        )
    )

    assert result.suggested_type.code == "ISFP"
