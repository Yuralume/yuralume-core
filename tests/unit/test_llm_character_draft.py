"""Parser tests for LLMCharacterDraftGenerator.

We don't hit any real HTTP endpoint — the generator is constructed then
its ``_call`` is patched to return canned strings. This keeps the test
focused on the parsing/sanitisation behaviour.
"""

import base64
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import date
from unittest.mock import AsyncMock

import httpx
import pytest

from kokoro_link.application.services.feature_keys import (
    FEATURE_CHARACTER_DRAFT,
    FEATURE_IMAGE_RECOGNITION,
)
from kokoro_link.contracts.character_draft import ImageInput
from kokoro_link.contracts.cloud_gateway import CloudGatewayIdentity
from kokoro_link.contracts.llm import ImageInputRejectedError
from kokoro_link.infrastructure.character_draft.llm_generator import (
    LLMCharacterDraftGenerator,
)
from kokoro_link.infrastructure.llm.cloud_gateway_model import (
    CloudGatewayChatModel,
)
from kokoro_link.contracts.character_draft import CompanionGenerationContext
from kokoro_link.infrastructure.character_draft.stub import (
    StubCharacterDraftGenerator,
    StubCompanionDraftGenerator,
)


def _generator() -> LLMCharacterDraftGenerator:
    return LLMCharacterDraftGenerator(
        base_url="http://unit-test.invalid/v1",
        api_key="test",
        model="test-model",
    )


class _MockAsyncClient(httpx.AsyncClient):
    """Same transport stub the Cloud gateway adapter's own tests use."""

    def __init__(self, handler, **kwargs) -> None:  # noqa: ANN001, ANN003
        super().__init__(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
        )


class _RecordingModel:
    provider_id = "unit"

    def __init__(
        self,
        *,
        supports_vision: bool,
        response: str,
    ) -> None:
        self.supports_vision = supports_vision
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.calls.append({
            "prompt": prompt,
            "image_urls": tuple(image_urls),
            "model": model,
        })
        return self.response

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        yield await self.generate(prompt, image_urls=image_urls, model=model)

    async def list_models(self) -> list[str]:
        return []


class _PublicUrlAwareModel(_RecordingModel):
    """A model that declares the Cloud-gateway carrier preference.

    ``_RecordingModel`` deliberately doesn't declare
    ``prefers_public_image_urls`` at all — that mirrors legacy adapters and
    keeps the ``getattr(..., False)`` default under test. This subclass is for
    the adapters that do declare it (Cloud gateway = True, self-host = False).
    """

    def __init__(
        self,
        *,
        supports_vision: bool,
        response: str,
        prefers_public_image_urls: bool,
    ) -> None:
        super().__init__(supports_vision=supports_vision, response=response)
        self.prefers_public_image_urls = prefers_public_image_urls


class _RoutingProvider:
    def __init__(
        self,
        *,
        draft_model: _RecordingModel,
        image_model: _RecordingModel | None = None,
    ) -> None:
        self.draft_model = draft_model
        self.image_model = image_model
        self.resolve_calls: list[str | None] = []
        self.operator_ids: list[object] = []

    async def resolve(
        self,
        feature_key: str | None = None,
        **kwargs: object,
    ) -> _RecordingModel:
        self.resolve_calls.append(feature_key)
        self.operator_ids.append(kwargs.get("operator_id"))
        if feature_key == FEATURE_IMAGE_RECOGNITION and self.image_model is not None:
            return self.image_model
        return self.draft_model

    async def resolve_model_id(
        self,
        feature_key: str | None = None,
        **kwargs: object,
    ) -> str | None:
        self.operator_ids.append(kwargs.get("operator_id"))
        if feature_key == FEATURE_IMAGE_RECOGNITION and self.image_model is not None:
            return "vision-model"
        if feature_key == FEATURE_CHARACTER_DRAFT:
            return "draft-model"
        return None

    async def is_fake(
        self,
        feature_key: str | None = None,  # noqa: ARG002
        **kwargs: object,
    ) -> bool:
        self.operator_ids.append(kwargs.get("operator_id"))
        return False


def _draft_response(name: str = "識圖角色") -> str:
    return (
        '{"name": "' + name + '", "summary": "根據提示產生的角色", '
        '"personality": ["細心"], "interests": ["觀察"], '
        '"speaking_style": "語氣穩定", "boundaries": [], '
        '"aspirations": [], "appearance": "藍色短髮，白色外套"}'
    )


class TestLLMGeneratorParsing:
    @pytest.mark.asyncio
    async def test_parses_well_formed_json(self) -> None:
        gen = _generator()
        gen._call = AsyncMock(return_value=(
            '{"name": "結衣", "summary": "文靜的圖書管理員", '
            '"name_candidates": [{"name": "結衣", "rationale": "柔和且有書卷感"}], '
            '"personality": ["細心", "溫柔"], "interests": ["詩集"], '
            '"speaking_style": "語調柔和", "boundaries": ["不談政治"], '
            '"aspirations": ["找到安靜的居所"], "appearance": "及肩黑髮，白色針織衫", '
            '"visual_subject_type": "human", '
            '"date_of_birth": "1998-04-12", "world_frame": "modern", '
            '"personality_type": {"system": "mbti_16", "code": "ISFJ", '
            '"source": "llm_inferred", "confidence": 0.74, '
            '"rationale": "安靜照顧型", "consistency_notes": ["具體人設優先"]}}'
        ))
        draft = await gen.generate(prompt="圖書管理員", image=None)
        assert draft.name == "結衣"
        assert draft.name_candidates[0].name == "結衣"
        assert draft.name_candidates[0].rationale == "柔和且有書卷感"
        assert draft.summary.startswith("文靜的")
        assert draft.personality == ["細心", "溫柔"]
        assert draft.speaking_style == "語調柔和"
        assert draft.appearance.startswith("及肩")
        assert draft.visual_subject_type == "human"
        assert draft.date_of_birth == date(1998, 4, 12)
        assert draft.world_frame == "modern"
        assert draft.personality_type.code == "ISFJ"
        assert draft.personality_type.source == "llm_inferred"

    @pytest.mark.asyncio
    async def test_tolerates_preamble_and_code_fence(self) -> None:
        gen = _generator()
        gen._call = AsyncMock(return_value=(
            "好的這是結果：\n```json\n"
            '{"name": "A", "summary": "s", "personality": [], "interests": [], '
            '"speaking_style": "", "boundaries": [], "aspirations": [], "appearance": ""}\n'
            "```"
        ))
        draft = await gen.generate(prompt="x", image=None)
        assert draft.name == "A"
        assert draft.summary == "s"

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty_draft(self) -> None:
        gen = _generator()
        gen._call = AsyncMock(return_value="not even close to json")
        draft = await gen.generate(prompt="x", image=None)
        assert draft.name == ""
        assert draft.summary == ""
        assert draft.visual_subject_type == "auto"
        assert draft.date_of_birth is None
        assert draft.world_frame == "modern"

    @pytest.mark.asyncio
    async def test_sanitizes_draft_birthday_and_world_frame(self) -> None:
        gen = _generator()
        gen._call = AsyncMock(return_value=(
            '{"name": "流星", "summary": "", "personality": [], '
            '"interests": [], "speaking_style": "", "boundaries": [], '
            '"aspirations": [], "appearance": "", '
            '"date_of_birth": "not-a-date", "world_frame": "cyberpunk"}'
        ))
        draft = await gen.generate(prompt="x", image=None)
        assert draft.date_of_birth is None
        assert draft.world_frame == "custom"

    @pytest.mark.asyncio
    async def test_bad_personality_type_fails_soft_to_unset(self) -> None:
        gen = _generator()
        gen._call = AsyncMock(return_value=(
            '{"name": "流星", "summary": "", "personality": [], '
            '"interests": [], "speaking_style": "", "boundaries": [], '
            '"aspirations": [], "appearance": "", '
            '"personality_type": {"code": "XXXX", "source": "llm_inferred"}}'
        ))
        draft = await gen.generate(prompt="x", image=None)
        assert draft.personality_type.is_unset is True

    @pytest.mark.asyncio
    async def test_normalises_visual_subject_type(self) -> None:
        gen = _generator()
        gen._call = AsyncMock(return_value=(
            '{"name": "Mochi", "summary": "", "personality": [], '
            '"interests": [], "speaking_style": "", "boundaries": [], '
            '"aspirations": [], "appearance": "橘貓", '
            '"visual_subject_type": "animal"}'
        ))

        draft = await gen.generate(prompt="橘貓", image=None)

        assert draft.visual_subject_type == "animal"

    @pytest.mark.asyncio
    async def test_name_candidates_are_capped_and_deduplicated(self) -> None:
        gen = _generator()
        gen._call = AsyncMock(return_value=(
            '{"name": "A", "name_candidates": ['
            '{"name": "A", "rationale": "r1"}, {"name": "A", "rationale": "dup"}, '
            '{"name": "B", "rationale": "r2"}, {"name": "C", "rationale": "r3"}, '
            '{"name": "D", "rationale": "r4"}, {"name": "E", "rationale": "r5"}, '
            '{"name": "F", "rationale": "r6"}], '
            '"summary": "", "personality": [], "interests": [], '
            '"speaking_style": "", "boundaries": [], "aspirations": [], "appearance": ""}'
        ))
        draft = await gen.generate(prompt="x", image=None)
        assert [candidate.name for candidate in draft.name_candidates] == [
            "A", "B", "C", "D", "E",
        ]

    @pytest.mark.asyncio
    async def test_list_items_capped_and_trimmed(self) -> None:
        gen = _generator()
        gen._call = AsyncMock(return_value=(
            '{"name": "x", "summary": "", '
            '"personality": ["a", "b", "c", "d", "e", "f", "g"], '
            '"interests": [], "speaking_style": "", "boundaries": [], '
            '"aspirations": [], "appearance": ""}'
        ))
        draft = await gen.generate(prompt="x", image=None)
        assert len(draft.personality) == 6  # capped to 6

    @pytest.mark.asyncio
    async def test_image_failure_falls_back_to_text_only(self) -> None:
        gen = _generator()
        call_count = {"n": 0}

        async def fake_call(
            instruction: str,
            *,
            image: ImageInput | None,
            operator_id: str | None = None,
        ) -> str:
            _ = operator_id
            call_count["n"] += 1
            if image is not None:
                raise RuntimeError("model does not support vision")
            return (
                '{"name": "FallbackOK", "summary": "", "personality": [], '
                '"interests": [], "speaking_style": "", "boundaries": [], '
                '"aspirations": [], "appearance": ""}'
            )

        gen._call = fake_call  # type: ignore[assignment]
        draft = await gen.generate(
            prompt="hello",
            image=ImageInput(data=b"fake", mime_type="image/png"),
        )
        assert draft.name == "FallbackOK"
        assert call_count["n"] == 2  # first with image (failed), then without

    @pytest.mark.asyncio
    async def test_image_returns_bad_json_then_text_only_retry(self) -> None:
        gen = _generator()
        results = iter([
            "garbage from vision path",  # image call — unparseable
            '{"name": "Ok"}',  # text-only retry
        ])

        async def fake_call(
            instruction: str,
            *,
            image: ImageInput | None,
            operator_id: str | None = None,
        ) -> str:
            _ = instruction, image, operator_id
            return next(results)

        gen._call = fake_call  # type: ignore[assignment]
        draft = await gen.generate(
            prompt="x",
            image=ImageInput(data=b"fake", mime_type="image/jpeg"),
        )
        assert draft.name == "Ok"

    @pytest.mark.asyncio
    async def test_vision_draft_model_receives_image_directly(self) -> None:
        draft_model = _RecordingModel(
            supports_vision=True,
            response=_draft_response("直讀圖片"),
        )
        image_model = _RecordingModel(
            supports_vision=True,
            response="不應被呼叫",
        )
        provider = _RoutingProvider(
            draft_model=draft_model,
            image_model=image_model,
        )
        gen = LLMCharacterDraftGenerator(
            provider=provider,
            feature_key=FEATURE_CHARACTER_DRAFT,
        )

        draft = await gen.generate(
            prompt="做成校園角色",
            image=ImageInput(data=b"fake", mime_type="image/png"),
        )

        assert draft.name == "直讀圖片"
        assert image_model.calls == []
        assert len(draft_model.calls) == 1
        assert draft_model.calls[0]["model"] == "draft-model"
        assert draft_model.calls[0]["image_urls"]
        assert "圖片識別摘要" not in str(draft_model.calls[0]["prompt"])

    @pytest.mark.asyncio
    async def test_draft_generator_forwards_operator_id_to_active_provider(self) -> None:
        draft_model = _RecordingModel(
            supports_vision=True,
            response=_draft_response("帳號層草稿"),
        )
        provider = _RoutingProvider(draft_model=draft_model)
        gen = LLMCharacterDraftGenerator(
            provider=provider,
            feature_key=FEATURE_CHARACTER_DRAFT,
        )

        draft = await gen.generate(
            prompt="做一個 demo 角色",
            image=None,
            operator_id="cloud:acct-demo",
        )

        assert draft.name == "帳號層草稿"
        assert provider.operator_ids
        assert set(provider.operator_ids) == {"cloud:acct-demo"}

    @pytest.mark.asyncio
    async def test_text_only_draft_model_uses_image_recognition_route(self) -> None:
        draft_model = _RecordingModel(
            supports_vision=False,
            response=_draft_response("摘要轉寫"),
        )
        image_model = _RecordingModel(
            supports_vision=True,
            response="藍色短髮、白色外套、校園制服風格，表情冷靜。",
        )
        provider = _RoutingProvider(
            draft_model=draft_model,
            image_model=image_model,
        )
        gen = LLMCharacterDraftGenerator(
            provider=provider,
            feature_key=FEATURE_CHARACTER_DRAFT,
        )

        draft = await gen.generate(
            prompt="做成校園角色",
            image=ImageInput(data=b"fake", mime_type="image/png"),
        )

        assert draft.name == "摘要轉寫"
        assert len(image_model.calls) == 1
        assert image_model.calls[0]["model"] == "vision-model"
        assert str(image_model.calls[0]["image_urls"][0]).startswith(
            "data:image/png;base64,",
        )
        assert len(draft_model.calls) == 1
        assert draft_model.calls[0]["model"] == "draft-model"
        assert draft_model.calls[0]["image_urls"] == ()
        final_prompt = str(draft_model.calls[0]["prompt"])
        assert "圖片識別摘要" in final_prompt
        assert "藍色短髮、白色外套" in final_prompt
        assert FEATURE_IMAGE_RECOGNITION in provider.resolve_calls

    @pytest.mark.asyncio
    async def test_text_only_draft_model_does_not_fake_image_when_recognizer_is_text_only(
        self,
    ) -> None:
        draft_model = _RecordingModel(
            supports_vision=False,
            response=_draft_response("文字保守"),
        )
        image_model = _RecordingModel(
            supports_vision=False,
            response="不應被呼叫",
        )
        provider = _RoutingProvider(
            draft_model=draft_model,
            image_model=image_model,
        )
        gen = LLMCharacterDraftGenerator(
            provider=provider,
            feature_key=FEATURE_CHARACTER_DRAFT,
        )

        draft = await gen.generate(
            prompt="幫我做角色",
            image=ImageInput(data=b"fake", mime_type="image/jpeg"),
        )

        assert draft.name == "文字保守"
        assert image_model.calls == []
        assert len(draft_model.calls) == 1
        assert draft_model.calls[0]["image_urls"] == ()
        final_prompt = str(draft_model.calls[0]["prompt"])
        assert "圖片狀態" in final_prompt
        assert "不要假裝看過圖片" in final_prompt


_PUBLIC_IMAGE_URL = "https://example.com/v1/public/draft-uploads/u1/x.png"


class TestDraftImageCarrier:
    """VP4: hosted Cloud routes drafts through a gateway with a bounded
    serialized request budget, so an inline base64 image always blows the
    envelope. When the resolved model prefers public URLs *and* the upload
    side managed to persist the bytes, we send the URL instead."""

    @pytest.mark.asyncio
    async def test_vision_draft_model_receives_public_url_when_preferred(
        self,
    ) -> None:
        draft_model = _PublicUrlAwareModel(
            supports_vision=True,
            response=_draft_response("公開網址直讀"),
            prefers_public_image_urls=True,
        )
        provider = _RoutingProvider(draft_model=draft_model)
        gen = LLMCharacterDraftGenerator(
            provider=provider,
            feature_key=FEATURE_CHARACTER_DRAFT,
        )

        draft = await gen.generate(
            prompt="做成校園角色",
            image=ImageInput(
                data=b"fake",
                mime_type="image/png",
                public_url=_PUBLIC_IMAGE_URL,
            ),
        )

        assert draft.name == "公開網址直讀"
        assert len(draft_model.calls) == 1
        assert draft_model.calls[0]["image_urls"] == (_PUBLIC_IMAGE_URL,)
        assert not str(draft_model.calls[0]["image_urls"][0]).startswith("data:")
        # Resolve-then-generate must keep forwarding the resolved model id.
        assert draft_model.calls[0]["model"] == "draft-model"

    @pytest.mark.asyncio
    async def test_image_recognition_route_receives_public_url_when_preferred(
        self,
    ) -> None:
        draft_model = _RecordingModel(
            supports_vision=False,
            response=_draft_response("摘要轉寫"),
        )
        image_model = _PublicUrlAwareModel(
            supports_vision=True,
            response="藍色短髮、白色外套。",
            prefers_public_image_urls=True,
        )
        provider = _RoutingProvider(
            draft_model=draft_model,
            image_model=image_model,
        )
        gen = LLMCharacterDraftGenerator(
            provider=provider,
            feature_key=FEATURE_CHARACTER_DRAFT,
        )

        draft = await gen.generate(
            prompt="做成校園角色",
            image=ImageInput(
                data=b"fake",
                mime_type="image/png",
                public_url=_PUBLIC_IMAGE_URL,
            ),
        )

        assert draft.name == "摘要轉寫"
        assert len(image_model.calls) == 1
        assert image_model.calls[0]["image_urls"] == (_PUBLIC_IMAGE_URL,)
        assert not str(image_model.calls[0]["image_urls"][0]).startswith("data:")
        assert image_model.calls[0]["model"] == "vision-model"
        assert FEATURE_IMAGE_RECOGNITION in provider.resolve_calls

    @pytest.mark.asyncio
    async def test_self_host_model_still_receives_inline_data_url(self) -> None:
        """Reverse pin: a stored public URL must not leak into the request
        for adapters that talk to the provider directly — self-host has no
        gateway budget problem and the URL may not even be reachable."""
        draft_model = _PublicUrlAwareModel(
            supports_vision=True,
            response=_draft_response("內聯自架"),
            prefers_public_image_urls=False,
        )
        provider = _RoutingProvider(draft_model=draft_model)
        gen = LLMCharacterDraftGenerator(
            provider=provider,
            feature_key=FEATURE_CHARACTER_DRAFT,
        )

        draft = await gen.generate(
            prompt="做成校園角色",
            image=ImageInput(
                data=b"fake",
                mime_type="image/png",
                public_url=_PUBLIC_IMAGE_URL,
            ),
        )

        assert draft.name == "內聯自架"
        assert len(draft_model.calls) == 1
        assert str(draft_model.calls[0]["image_urls"][0]).startswith(
            "data:image/png;base64,",
        )

    @pytest.mark.asyncio
    async def test_missing_public_url_falls_back_to_inline_data_url(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Degradation pin: the upload side is fail-soft, so ``public_url``
        can be None even on hosted. Better an oversized inline request that
        may still fit than an image silently dropped.

        It must not be *silent*, though. This is the only place where the
        requirement ("this model asked for a URL") and the fact ("there is
        none") are both in hand, so it is the only place that can say so — the
        staging side, three layers up, cannot know whether anyone needed it.
        """
        draft_model = _PublicUrlAwareModel(
            supports_vision=True,
            response=_draft_response("上傳失敗降級"),
            prefers_public_image_urls=True,
        )
        provider = _RoutingProvider(draft_model=draft_model)
        gen = LLMCharacterDraftGenerator(
            provider=provider,
            feature_key=FEATURE_CHARACTER_DRAFT,
        )

        with caplog.at_level(logging.WARNING):
            draft = await gen.generate(
                prompt="做成校園角色",
                image=ImageInput(data=b"fake", mime_type="image/jpeg"),
            )

        assert draft.name == "上傳失敗降級"
        assert len(draft_model.calls) == 1
        assert str(draft_model.calls[0]["image_urls"][0]).startswith(
            "data:image/jpeg;base64,",
        )
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("prefers public image URLs" in text for text in warnings), warnings

    @pytest.mark.asyncio
    async def test_self_host_carrier_fallback_stays_silent(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The warning fires where the requirement exists, nowhere else.

        Self-host has no gateway budget and no object storage to stage into;
        inline is its normal, correct path. Warning there would train operators
        to ignore the message that matters.
        """
        draft_model = _PublicUrlAwareModel(
            supports_vision=True,
            response=_draft_response("自架安靜"),
            prefers_public_image_urls=False,
        )
        provider = _RoutingProvider(draft_model=draft_model)
        gen = LLMCharacterDraftGenerator(
            provider=provider,
            feature_key=FEATURE_CHARACTER_DRAFT,
        )

        with caplog.at_level(logging.WARNING):
            await gen.generate(
                prompt="做成校園角色",
                image=ImageInput(data=b"fake", mime_type="image/jpeg"),
            )

        noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert noisy == [], [r.getMessage() for r in noisy]


class TestDraftImageCarrierThroughTheRealAdapter:
    """L4: the carrier decision must survive the *real* adapter, not a stub.

    Every test above resolves to a hand-written double that declares
    ``prefers_public_image_urls`` itself. That pins the generator's branch and
    nothing else — the assumption actually holding VP4 up is that the flag is
    still readable on whatever object ``ModelResolver`` hands back, and that
    the URL the flag selects is what lands on the wire. A wrapper inserted into
    that chain later (metadata capture, a routing decorator) would not fail a
    single stub-based test; it would just quietly send base64 again, and the
    only symptom would be hosted drafts degrading to text-only.

    So this one drives the genuine ``CloudGatewayChatModel`` with a 3 MB image
    over a mocked transport, and asserts the thing the stubs cannot: the
    request that leaves is small.
    """

    @pytest.mark.asyncio
    async def test_a_3mb_image_travels_as_a_url_within_the_gateway_budget(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[bytes] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": _draft_response("真轉接器")}},
                    ],
                },
            )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: _MockAsyncClient(handler, **kwargs),
        )
        adapter = CloudGatewayChatModel(
            base_url="https://gateway.example",
            deployment_token="ykl_deploy",
            default_model="preset-chat",
            feature_key=FEATURE_CHARACTER_DRAFT,
            identity=CloudGatewayIdentity(
                operator_id="cloud:acct_1",
                account_id="acct_1",
                tenant_id="tenant_1",
                character_ref="chr_abc",
            ),
        )
        assert adapter.prefers_public_image_urls is True
        gen = LLMCharacterDraftGenerator(model_port=adapter)

        # 3 MB — inlined as base64 this is ~4 MB, an order of magnitude over
        # the adapter's 224 KB wire budget, so the pre-VP4 carrier would be
        # rejected before the request was ever sent.
        draft = await gen.generate(
            prompt="做成校園角色",
            image=ImageInput(
                data=b"\x89PNG" + b"\x00" * (3 * 1024 * 1024),
                mime_type="image/png",
                public_url=_PUBLIC_IMAGE_URL,
            ),
        )

        assert draft.name == "真轉接器"
        # Exactly one request. The generator swallows an image-path failure and
        # silently retries text-only, so a count of two would be VP4 not
        # working while every other assertion here still passed.
        assert len(seen) == 1
        content = seen[0]
        assert _PUBLIC_IMAGE_URL.encode() in content
        assert b"base64," not in content
        assert len(content) < 224 * 1024

    @pytest.mark.asyncio
    async def test_the_same_image_without_a_url_is_rejected_before_the_wire(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The reverse pin that gives the one above its meaning.

        Without ``public_url`` the identical draft cannot be sent at all — so
        the test above is measuring staging's effect, not an image the budget
        would have accepted either way. (``generate`` still returns a draft:
        the generator catches the rejection and retries text-only, which is the
        degradation VP4 exists to avoid.)
        """
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": _draft_response("純文字降級")}},
                    ],
                },
            )

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: _MockAsyncClient(handler, **kwargs),
        )
        adapter = CloudGatewayChatModel(
            base_url="https://gateway.example",
            deployment_token="ykl_deploy",
            default_model="preset-chat",
            feature_key=FEATURE_CHARACTER_DRAFT,
            identity=CloudGatewayIdentity(
                operator_id="cloud:acct_1",
                account_id="acct_1",
                tenant_id="tenant_1",
                character_ref="chr_abc",
            ),
        )

        with pytest.raises(ImageInputRejectedError):
            await adapter.generate(
                "做成校園角色",
                image_urls=(
                    "data:image/png;base64,"
                    + base64.b64encode(b"\x00" * (3 * 1024 * 1024)).decode(
                        "ascii",
                    ),
                ),
            )

        assert calls == 0


class TestStubGenerator:
    @pytest.mark.asyncio
    async def test_returns_placeholder_draft(self) -> None:
        gen = StubCharacterDraftGenerator()
        draft = await gen.generate(prompt="安靜的女孩", image=None)
        assert draft.name
        assert draft.summary
        assert draft.date_of_birth is not None
        assert draft.world_frame == "modern"

    @pytest.mark.asyncio
    async def test_placeholder_draft_localized_for_en_operator(self) -> None:
        """No LLM configured → the AI-draft button prefills the form with
        stub values. Those must respect the operator language, not leak
        zh-TW to an en-US operator (empty prompt → default summary)."""
        gen = StubCharacterDraftGenerator()
        draft = await gen.generate(
            prompt=None, image=None, operator_primary_language="en-US",
        )
        # Placeholder field values must be English, not Chinese.
        assert draft.name and "新角色" not in draft.name
        assert draft.summary and "尚未設定" not in draft.summary
        assert all("溫柔" not in p for p in draft.personality)
        assert draft.speaking_style and "自然親切" not in draft.speaking_style

    @pytest.mark.asyncio
    async def test_placeholder_draft_zh_unchanged(self) -> None:
        gen = StubCharacterDraftGenerator()
        draft = await gen.generate(
            prompt=None, image=None, operator_primary_language="zh-TW",
        )
        assert draft.name == "新角色"
        assert draft.summary == "尚未設定的角色。"

    @pytest.mark.asyncio
    async def test_prompt_hint_still_used_as_summary_regardless_of_locale(
        self,
    ) -> None:
        gen = StubCharacterDraftGenerator()
        draft = await gen.generate(
            prompt="a quiet girl", image=None,
            operator_primary_language="en-US",
        )
        assert draft.summary == "a quiet girl"


class TestStubCompanionGenerator:
    @pytest.mark.asyncio
    async def test_companion_stub_localized_for_en_operator(self) -> None:
        gen = StubCompanionDraftGenerator()
        drafts = await gen.generate(
            context=CompanionGenerationContext(
                character_name="Mio",
                operator_primary_language="en-US",
            ),
        )
        assert len(drafts) == 1
        companion = drafts[0]
        # No zh-TW field values leak to an en operator.
        assert "室友" not in companion.name
        assert "室友" not in companion.role
        assert companion.brief_profile and "同居人" not in companion.brief_profile
        assert all("隨和" not in p for p in companion.personality_sketch)

    @pytest.mark.asyncio
    async def test_companion_stub_zh_unchanged(self) -> None:
        gen = StubCompanionDraftGenerator()
        drafts = await gen.generate(
            context=CompanionGenerationContext(
                character_name="米歐",
                operator_primary_language="zh-TW",
            ),
        )
        assert drafts[0].name == "室友"
        assert drafts[0].personality_sketch == ["隨和"]
