"""Actor — a uniform reference for anyone who participates in a scene
or memory.

This is the seam Phase 1 leaves for the eventual world system. Every
"who" mention in a memory, beat, or schedule activity ultimately
collapses to one of three kinds:

- ``operator``: the human controlling the app (single-operator today;
  multi-operator possible without schema change because we already
  carry an ``id``).
- ``character``: another in-app ``Character`` (cross-character scenes,
  shared episodes, NPC roster).
- ``npc``: someone the operator mentioned by name who isn't backed by
  a Character entity. ``id`` is ``None`` until/unless we promote them.

Memories and (later) schedule activities carry ``ParticipantRef``
tuples so the post-turn extractor can record "B took the operator out
to ramen" without leaving an ambiguous "他" inside the content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ActorKind = Literal["operator", "character", "npc"]
_VALID_KINDS: frozenset[str] = frozenset({"operator", "character", "npc"})


@dataclass(frozen=True, slots=True)
class Actor:
    """A normalised reference to a person in the world.

    ``id`` is the canonical identifier within ``kind``: an
    ``OperatorProfile.id`` for operators, a ``Character.id`` for
    characters, ``None`` for unresolved NPCs.

    ``display_name`` is what the model should be told to call this
    person. Always populated — even for operators with a blank profile
    we fall back to a sensible default so prompts never render an
    empty string.

    ``aliases`` lets the extractor recognise alternate names (the
    operator's nickname, a character's pet name) without re-running
    the resolver. Stored as a tuple to keep the dataclass hashable.
    """

    kind: ActorKind
    id: str | None
    display_name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        kind = (self.kind or "").strip()
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"Actor.kind must be one of {sorted(_VALID_KINDS)}, got {self.kind!r}",
            )
        object.__setattr__(self, "kind", kind)
        if self.id is not None:
            trimmed_id = self.id.strip()
            object.__setattr__(self, "id", trimmed_id or None)
        name = (self.display_name or "").strip()
        if not name:
            raise ValueError("Actor.display_name must be non-empty")
        object.__setattr__(self, "display_name", name)
        cleaned_aliases = tuple(
            alias.strip() for alias in self.aliases if alias and alias.strip()
        )
        object.__setattr__(self, "aliases", cleaned_aliases)


@dataclass(frozen=True, slots=True)
class ParticipantRef:
    """A pointer to an actor as recorded inside a memory or scene.

    Distinct from ``Actor`` because:

    - Storage rows want a thin shape (``kind / id / display_name``)
      that can be JSON-encoded without dragging the operator's full
      profile or alias list along.
    - ``role`` is scene-local (e.g. "speaker", "observer", "host")
      and only meaningful in context. Phase 2 leaves the field
      reserved; Phase 4+'s shared episode log will start populating
      it for narrative orchestration.

    ``actor_id`` is ``None`` for unresolved NPCs — the extractor
    captured a name but we couldn't link it to a ``Character`` row.
    Useful for fuzzy matching later if the operator promotes the NPC.
    """

    actor_kind: ActorKind
    actor_id: str | None
    display_name: str
    role: str | None = None

    def __post_init__(self) -> None:
        kind = (self.actor_kind or "").strip()
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"ParticipantRef.actor_kind must be one of {sorted(_VALID_KINDS)}, "
                f"got {self.actor_kind!r}",
            )
        object.__setattr__(self, "actor_kind", kind)
        if self.actor_id is not None:
            trimmed_id = self.actor_id.strip()
            object.__setattr__(self, "actor_id", trimmed_id or None)
        name = (self.display_name or "").strip()
        if not name:
            raise ValueError("ParticipantRef.display_name must be non-empty")
        object.__setattr__(self, "display_name", name)
        if self.role is not None:
            trimmed_role = self.role.strip()
            object.__setattr__(self, "role", trimmed_role or None)

    @classmethod
    def from_actor(cls, actor: Actor, *, role: str | None = None) -> "ParticipantRef":
        """Project an ``Actor`` (richer, has aliases) into the slim
        ``ParticipantRef`` shape used inside memories."""
        return cls(
            actor_kind=actor.kind,
            actor_id=actor.id,
            display_name=actor.display_name,
            role=role,
        )

    def to_dict(self) -> dict[str, str | None]:
        """JSON-friendly shape for ``participants_json`` storage."""
        return {
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "display_name": self.display_name,
            "role": self.role,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "ParticipantRef | None":
        """Inverse of :meth:`to_dict`. Returns ``None`` for malformed
        payloads — callers building a tuple from storage should drop
        bad rows rather than crash the read path."""
        kind = str(payload.get("actor_kind") or "").strip()
        if kind not in _VALID_KINDS:
            return None
        name = str(payload.get("display_name") or "").strip()
        if not name:
            return None
        actor_id_raw = payload.get("actor_id")
        actor_id: str | None
        if actor_id_raw is None:
            actor_id = None
        else:
            actor_id_str = str(actor_id_raw).strip()
            actor_id = actor_id_str or None
        role_raw = payload.get("role")
        role: str | None
        if role_raw is None:
            role = None
        else:
            role_str = str(role_raw).strip()
            role = role_str or None
        try:
            return cls(
                actor_kind=kind,  # type: ignore[arg-type]
                actor_id=actor_id,
                display_name=name,
                role=role,
            )
        except ValueError:
            return None
