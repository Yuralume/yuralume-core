import asyncio
from collections.abc import AsyncIterator, Sequence

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.infrastructure.prompt.default import LATEST_USER_MESSAGE_MARKER


class FakeChatModel(ChatModelPort):
    supports_vision: bool = False

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        latest_line = prompt.split(LATEST_USER_MESSAGE_MARKER, maxsplit=1)[-1].strip()
        # The prompt ends with the instruction line; strip everything after
        # the first newline so the echoed content is just the user message.
        latest_line = latest_line.split("\n", maxsplit=1)[0].strip()
        if not latest_line:
            latest_line = "我在這裡。"
        return f"我有收到你剛剛說的內容：{latest_line}。我會接著這個方向跟你聊。"

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        full_text = await self.generate(prompt, image_urls=image_urls)
        for char in full_text:
            yield char
            await asyncio.sleep(0.02)

    async def list_models(self) -> list[str]:
        # Fake provider is single-model — surface one entry so the UI
        # dropdown has something selectable rather than rendering empty.
        return [self.provider_id]
