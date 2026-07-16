from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigestContext,
)
from kokoro_link.infrastructure.prompt.llm_material_digester import (
    LLMPromptMaterialDigester,
    _build_prompt,
)
from kokoro_link.infrastructure.prompt.null_material_digester import (
    NullPromptMaterialDigester,
)


class _Model:
    provider_id = "unit-provider"
    supports_vision = False

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.prompts: list[str] = []
        self.models: list[str | None] = []

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        del image_urls
        self.prompts.append(prompt)
        self.models.append(model)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        del prompt, image_urls, model
        if False:
            yield ""

    async def list_models(self) -> list[str]:
        return []


def _context() -> PromptMaterialDigestContext:
    return PromptMaterialDigestContext(
        character_id="c1",
        operator_id="u1",
        emotion_events=("剛剛 | 對話 | 被記得 | 引：你還記得我昨天說的事",),
        self_reflections=("這週我一直把那句話放在心上，像霧停在玻璃上。",),
        story_events=("我在走廊遇見一封沒署名的信。",),
        story_arc=("明天：拆開信封 - 思考那封信來自誰。",),
        recent_feed_posts=("15 分鐘前：今天的咖啡好香。",),
        source_language="zh-TW",
        content_tolerance="frontier",
    )


def test_material_digest_prompt_contains_all_source_categories() -> None:
    prompt = _build_prompt(_context())

    assert "最近情緒事件" in prompt
    assert "內在反思" in prompt
    assert "今天/近期故事事件" in prompt
    assert "故事主軸與接下來節奏" in prompt
    assert "最近動態牆貼文" in prompt
    assert "不要模仿原素材的措辭、句式或意象" in prompt
    assert "zh-TW" in prompt
    assert "frontier" in prompt


@pytest.mark.asyncio
async def test_llm_material_digester_parses_json_bullets_with_metadata() -> None:
    model = _Model(
        'preface {"bullets":["使用者昨天的話被角色記住。","明天的故事方向是拆開信封。"]}',
    )
    digester = LLMPromptMaterialDigester(model=model)

    digest = await digester.digest(_context())

    assert digest is not None
    assert digest.bullets == (
        "使用者昨天的話被角色記住。",
        "明天的故事方向是拆開信封。",
    )
    assert "像霧停在玻璃上" not in digest.bullets
    assert digest.digest_metadata["applied"] is True
    assert digest.digest_metadata["bullet_count"] == 2
    assert digest.digest_metadata["provider_id"] == "unit-provider"
    assert digest.digest_metadata["model_id"] == "unit-provider"
    assert digest.digest_metadata["content_tolerance"] == "frontier"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"bullets":[]}',
        '{"bullets":[123,null]}',
    ],
)
async def test_llm_material_digester_returns_none_for_unusable_output(
    response: str,
) -> None:
    digester = LLMPromptMaterialDigester(model=_Model(response))

    assert await digester.digest(_context()) is None


@pytest.mark.asyncio
async def test_llm_material_digester_returns_none_on_provider_error() -> None:
    digester = LLMPromptMaterialDigester(model=_Model(RuntimeError("boom")))

    assert await digester.digest(_context()) is None


@pytest.mark.asyncio
async def test_null_material_digester_returns_none() -> None:
    assert await NullPromptMaterialDigester().digest(_context()) is None
