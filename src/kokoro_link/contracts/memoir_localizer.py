"""Port for localising player-visible memoir text."""

from __future__ import annotations

from abc import ABC, abstractmethod

from kokoro_link.domain.entities.memoir import MemoirView


class MemoirLocalizerPort(ABC):
    @abstractmethod
    async def localize_view(
        self,
        view: MemoirView,
        *,
        target_language: str,
    ) -> MemoirView:
        """Render player-visible memoir prose in ``target_language``.

        Implementations must preserve the memoir structure: ids, entry
        kinds, dates, scores, pin state, and non-prose metadata are not
        translated. Returning ``view`` means localisation was skipped or
        failed.
        """
