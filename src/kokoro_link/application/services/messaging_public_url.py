"""Public URL resolution for external messaging channels."""

from __future__ import annotations

from kokoro_link.contracts.repositories import PreferencesRepositoryPort

MESSAGING_PUBLIC_BASE_URL_KEY = "messaging.public_base_url"


def normalize_public_base_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


class MessagingPublicUrlResolver:
    """Resolve the public app origin used by external messaging channels.

    Admin channel settings take precedence over the environment fallback so
    operators can fix public delivery domains without restarting the app.
    """

    def __init__(
        self,
        *,
        preferences_repository: PreferencesRepositoryPort,
        app_public_base_url: str = "",
    ) -> None:
        self._preferences = preferences_repository
        self._app_public_base_url = normalize_public_base_url(app_public_base_url)

    async def resolve(self) -> str:
        stored = normalize_public_base_url(
            await self._preferences.get(MESSAGING_PUBLIC_BASE_URL_KEY),
        )
        if stored:
            return stored
        return self._app_public_base_url
