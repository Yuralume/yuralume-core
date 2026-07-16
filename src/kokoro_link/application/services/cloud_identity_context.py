"""Ambient (request / task-scoped) cloud actor identity.

A cross-cutting concern. Many *leaf* LLM services — persona and behaviour
extractors, the TTS / memoir / character-card translators, the personality
analyzer, the companion generator — need the *current* cloud account so the
gateway provider can attach per-account headers and resolve routing. They
have no business taking an ``operator_id`` / ``Character`` parameter purely
to forward it; threading cloud identity through every unrelated port would
couple those layers to the cloud transport and scatter one global fact
across the whole codebase.

Instead the few orchestration boundaries that genuinely know the actor bind
it once, here:

* the authenticated HTTP request (``api.dependencies.get_current_user``)
* the background persona-dream consolidation tick (per character / operator)

``CloudActiveLLMProvider`` reads this as a **fallback** — only when a call
site passes neither ``character`` nor ``operator_id`` explicitly. Explicit
arguments always win, so per-character routing context is still threaded
where it matters (chat, feed, proactive, ...); the ambient value is purely
the identity safety net for the identity-only leaf calls.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character


@dataclass(frozen=True, slots=True)
class AmbientCloudActor:
    """Whoever the current request / task is acting for."""

    operator_id: str | None = None
    character: "Character | None" = None

    def is_empty(self) -> bool:
        return self.character is None and not (self.operator_id or "").strip()


_AMBIENT_CLOUD_ACTOR: ContextVar[AmbientCloudActor | None] = ContextVar(
    "ambient_cloud_actor", default=None,
)


def current_cloud_actor() -> AmbientCloudActor | None:
    """The actor bound for the current request / task, or ``None``.

    Collapses an empty binding to ``None`` so callers can treat "nothing
    useful is bound" uniformly.
    """
    actor = _AMBIENT_CLOUD_ACTOR.get()
    if actor is None or actor.is_empty():
        return None
    return actor


def bind_cloud_actor(
    *, operator_id: str | None = None, character: "Character | None" = None,
) -> Token:
    """Bind the ambient actor for the *current task*, without auto-reset.

    Use at a per-task boundary whose lifecycle already isolates context —
    e.g. an ``async`` FastAPI dependency: each request runs in its own task
    with a freshly copied context, so the binding cannot leak across
    requests and there is nothing to reset. Returns the ``Token`` in case a
    caller does want to reset it explicitly.
    """
    return _AMBIENT_CLOUD_ACTOR.set(
        AmbientCloudActor(operator_id=operator_id, character=character),
    )


@contextmanager
def cloud_actor_scope(
    *, operator_id: str | None = None, character: "Character | None" = None,
) -> Iterator[None]:
    """Bind the ambient actor for the duration of a ``with`` block.

    Use for bounded background work (e.g. one persona-dream consolidation)
    where there is no per-task lifecycle to lean on; the binding is reset on
    exit so sequential pairs processed in a loop never bleed into each other.
    """
    token = bind_cloud_actor(operator_id=operator_id, character=character)
    try:
        yield
    finally:
        _AMBIENT_CLOUD_ACTOR.reset(token)
