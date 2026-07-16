"""In-memory channel adapter used by tests and BDD walkthroughs."""

from kokoro_link.contracts.messaging import ChannelAdapterPort, OutboundMessage
from kokoro_link.domain.value_objects.platform import Platform


class FakeChannelAdapter(ChannelAdapterPort):
    def __init__(self, platform: Platform) -> None:
        self._platform = platform
        self.sent: list[OutboundMessage] = []

    @property
    def platform(self) -> Platform:
        return self._platform

    async def send(self, message: OutboundMessage) -> None:
        self.sent.append(message)
