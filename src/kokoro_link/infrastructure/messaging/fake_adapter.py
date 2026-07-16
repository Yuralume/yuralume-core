"""In-memory channel adapter used by tests and BDD walkthroughs."""

from collections.abc import Sequence

from kokoro_link.contracts.messaging import ChannelAdapterPort, OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform


class FakeChannelAdapter(ChannelAdapterPort):
    def __init__(self, platform: Platform) -> None:
        self._platform = platform
        self.sent: list[OutboundMessage] = []
        self.batches: list[tuple[OutboundMessage, ...]] = []
        """Each ``send_many`` hand-over as one tuple, so tests can assert
        batch boundaries (LINE packing) while ``sent`` keeps the flat
        per-bubble view most tests rely on."""

    @property
    def platform(self) -> Platform:
        return self._platform

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    async def send_many(self, messages: Sequence[OutboundMessage]) -> None:
        batch = tuple(messages)
        self.batches.append(batch)
        for message in batch:
            await self.send(message)
