from kokoro_link.contracts.llm import ChatModelPort, ChatModelRegistryPort


class InMemoryChatModelRegistry(ChatModelRegistryPort):
    def __init__(self, default_provider_id: str) -> None:
        self._default_provider_id = default_provider_id
        self._providers: dict[str, ChatModelPort] = {}

    def register(self, provider: ChatModelPort) -> None:
        self._providers[provider.provider_id] = provider

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    def list_ids(self) -> list[str]:
        return list(self._providers.keys())

    def resolve(self, provider_id: str) -> ChatModelPort:
        effective_provider_id = provider_id or self._default_provider_id
        provider = self._providers.get(effective_provider_id)
        if provider is None:
            raise ValueError(f"Unknown provider: {effective_provider_id}")
        return provider
