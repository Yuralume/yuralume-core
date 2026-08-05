"""In-memory story scene session repository (self-host without a DB).

Unlike the in-memory story *arc* repository — which cannot detect the
single-active-arc race because that race is between processes — this one
**does** enforce "at most one open scene per character". The in-memory
store only ever backs a single process with a single event loop, and
these methods contain no awaits between the check and the write, so the
check is genuine mutual exclusion here rather than wishful thinking. The
port's guarantee therefore holds on both storage backends, and the
service can rely on ``SceneSessionConflict`` instead of carrying a
"maybe your repository enforces this" branch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.story_scene import (
    SceneSessionConflict,
    StorySceneSessionRepositoryPort,
)
from kokoro_link.domain.entities.story_scene_session import StorySceneSession


class InMemoryStorySceneSessionRepository(StorySceneSessionRepositoryPort):
    def __init__(self) -> None:
        self._sessions: dict[str, StorySceneSession] = {}

    async def add(self, scene: StorySceneSession) -> None:
        if scene.id in self._sessions:
            raise ValueError(f"scene session {scene.id} already exists")
        self._guard_single_open(scene)
        self._sessions[scene.id] = scene

    async def get(self, session_id: str) -> StorySceneSession | None:
        return self._sessions.get(session_id)

    async def get_open_for_character(
        self, character_id: str,
    ) -> StorySceneSession | None:
        live = [
            scene for scene in self._sessions.values()
            if scene.character_id == character_id and scene.is_open
        ]
        if not live:
            return None
        live.sort(key=lambda scene: scene.opened_at, reverse=True)
        return live[0]

    async def save(self, scene: StorySceneSession) -> None:
        self._guard_single_open(scene)
        self._sessions[scene.id] = scene

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def touch_activity(self, session_id: str, *, at: datetime) -> bool:
        scene = self._sessions.get(session_id)
        if scene is None or not scene.is_open:
            return False
        # ``touched`` owns the monotonic rule, so the two backends cannot
        # drift on what an out-of-order bump does.
        self._sessions[session_id] = scene.touched(at)
        return True

    async def close(
        self, session_id: str, *, reason: str, at: datetime,
    ) -> StorySceneSession | None:
        scene = self._sessions.get(session_id)
        if scene is None or not scene.is_open:
            return None
        # ``closed`` owns the transition (terminal, keeps the original
        # reason) so the two backends cannot drift on what closing means.
        closed = scene.closed(reason=reason, at=at)
        self._sessions[session_id] = closed
        return closed

    async def list_stale_open(
        self, *, idle_before: datetime, limit: int = 100,
    ) -> tuple[StorySceneSession, ...]:
        if limit <= 0:
            return ()
        cutoff = ensure_utc(idle_before)
        stale = [
            scene for scene in self._sessions.values()
            if scene.is_open and scene.last_activity_at < cutoff
        ]
        stale.sort(key=lambda scene: scene.last_activity_at)
        return tuple(stale[:limit])

    async def count_opened_since(
        self, character_ids: Sequence[str], *, since: datetime,
    ) -> int:
        ids = set(character_ids)
        if not ids:
            return 0
        cutoff = ensure_utc(since)
        return sum(
            1 for scene in self._sessions.values()
            if scene.character_id in ids and scene.opened_at >= cutoff
        )

    def _guard_single_open(self, scene: StorySceneSession) -> None:
        if not scene.is_open:
            return
        for existing in self._sessions.values():
            if (
                existing.id != scene.id
                and existing.character_id == scene.character_id
                and existing.is_open
            ):
                raise SceneSessionConflict(scene.character_id)
