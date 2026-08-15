"""Ports for the player-pulled story scene (「起幕」, SC1).

Five seams live here:

* :class:`StorySceneSessionRepositoryPort` — the DB-backed scene state.
* :class:`StorySceneMaterialProviderPort` — one layer of the §3.1
  waterfall. SC1-A ships only the first (pending arc beat); SC1-B adds
  the forced-new-season and ad-hoc-side-story layers by appending
  providers to the service's ordered list, with no edit to the opening
  path itself.
* :class:`StorySceneOpenerPort` — the single LLM call that turns the
  chosen material into narration + the character's first spoken line +
  the visible scene frame.
* :class:`StorySceneChipsWriterPort` — the suggested in-scene actions
  offered after the opening and after every in-scene turn.
* :class:`StorySceneCloserPort` — the wrap-up (SC1-D): the verdict on
  whether the scene is finished, the closing narration, and the summary
  of what was actually performed that lands as canon.

Kept out of ``contracts/story_arc.py`` on purpose: two of the three
waterfall layers do not involve an arc at all, so the scene runtime must
not be typed as an arc feature.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_scene_session import StorySceneSession


SCENE_NARRATION_SPEAKER = "旁白"
"""Label for a narration line in a rendered scene transcript.

Narration is labelled rather than attributed to the character, because
an unlabelled line reads as the character still talking — and a chips
writer that believes the character spoke last writes *the character's*
next line, which is the player-visible bug this label exists to stop.
"""

SCENE_PLAYER_SPEAKER = "玩家"
"""Label for the player's own line."""


def render_scene_line(speaker: str, text: str) -> str:
    """One transcript line, in the format every scene writer reads.

    A function rather than an f-string at each call site so the separator
    travels with the labels above: the opening path has no player line
    yet, so a drifted format there is invisible in tests that only look
    at in-scene turns.

    Internal newlines are flattened because every scene writer's prompt
    promises one physical line per entry (「每行冒號前是說話的人」).
    The opening narration is multi-paragraph prose; without this, its
    second paragraph would enter the transcript as an unlabelled line
    and be read as whoever spoke last — the exact mis-attribution the
    speaker labels exist to stop.
    """
    return f"{speaker}：{' '.join(text.split())}"


class SceneSessionConflict(Exception):
    """This character already owns an ``open`` scene session.

    Raised by ``add`` when the write would give a character two open
    scenes — i.e. when the partial unique index rejects it. Distinct from
    the service-level pre-check on purpose: the pre-check is the friendly
    answer for the common case ("你已經在一場戲裡了"), this is the
    schema-level backstop for two replicas racing the same tap, which no
    read-then-write can close.
    """

    def __init__(self, character_id: str) -> None:
        super().__init__(
            f"character {character_id} already has an open story scene",
        )
        self.character_id = character_id


class StorySceneSessionRepositoryPort(ABC):
    """Persistence for :class:`StorySceneSession`.

    At most ONE ``open`` session per character. Implementations must make
    that true at the storage layer (a partial unique index for SQL, the
    equivalent check under the single-writer in-memory store) and raise
    :class:`SceneSessionConflict` when a write would break it — the
    caller cannot be trusted to have checked first, because between its
    check and its write another replica may have won.
    """

    @abstractmethod
    async def add(self, session: StorySceneSession) -> None:
        """Insert a new session.

        Raises :class:`SceneSessionConflict` when ``session`` is open and
        the character already has an open one."""

    @abstractmethod
    async def get(self, session_id: str) -> StorySceneSession | None: ...

    @abstractmethod
    async def get_open_for_character(
        self, character_id: str,
    ) -> StorySceneSession | None:
        """The character's live scene, or ``None`` when it is not in one."""

    @abstractmethod
    async def save(self, session: StorySceneSession) -> None:
        """Upsert an existing session (activity bump, close transition).

        Raises :class:`SceneSessionConflict` when re-opening a session
        would produce a second open row for the character."""

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Remove a session row. Idempotent.

        Exists for the opening path's rollback: a scene whose narration
        could not be written to the thread must not leave an ``open`` row
        behind, or the player is locked out of 起幕 until the SC1-E
        timeout closer runs."""

    @abstractmethod
    async def touch_activity(self, session_id: str, *, at: datetime) -> bool:
        """Record that the scene was played at ``at``; is it still live?

        The idle clock SC1-E's timeout closer reads (SC1-C bumps it on
        every in-scene turn). A targeted write rather than
        ``save(session.touched(at))`` for two reasons: the turn holds a
        snapshot read at its *start*, so writing the whole row back would
        undo a close that landed while the model was generating, and the
        bump must be monotonic — a slower replica finishing second must
        not drag the clock backwards and make a live scene look stale.

        Returns ``True`` when a session with this id is still ``open``
        (whether or not the stamp actually moved), ``False`` when it has
        closed or vanished — which is the caller's cue that the chips it
        was about to generate now belong to a scene nobody is in."""

    @abstractmethod
    async def close(
        self, session_id: str, *, reason: str, at: datetime,
    ) -> StorySceneSession | None:
        """Close an ``open`` session; did THIS call close it?

        Returns the freshly closed session when the write won, ``None``
        when the row was already closed or has vanished. Compare-and-set
        rather than ``save(session.closed(...))`` for the same reason
        :meth:`touch_activity` is a targeted write: three callers can
        reach a close at once (the in-turn verdict, the player's 「結束
        場景」 button, SC1-E's idle sweep), they run on different
        replicas, and only one of them may write the closing narration
        and land the scene as canon. A whole-row write would also stamp
        back a snapshot read before the scene's last turn.

        The ``None`` answer is not an error — it is the losing racer
        being told "someone else already wrapped this up", which is why
        every caller treats it as an idempotent success rather than a
        failure to report."""

    @abstractmethod
    async def list_stale_open(
        self, *, idle_before: datetime, limit: int = 100,
    ) -> tuple[StorySceneSession, ...]:
        """``open`` sessions whose last activity predates ``idle_before``.

        The idle sweep's whole-table view (SC1-E). Scanned by ``(status,
        last_activity_at)`` — the index SC1-A created for exactly this
        query — and never by character, because the rows this answer
        exists for are the ones **no character chain covers**: a session
        whose character row was deleted out from under it. SQLite does not
        enforce the ``ON DELETE CASCADE`` this table declares (the pragma
        is off by default), so on a self-host install those rows are real,
        durable, and permanently ``open`` unless something sweeps by time
        alone.

        Oldest first, so a backlog drains in the order it accumulated and
        ``limit`` cannot starve the scene that has been idle longest.
        """

    @abstractmethod
    async def count_opened_since(
        self, character_ids: Sequence[str], *, since: datetime,
    ) -> int:
        """How many sessions with ``character_id`` in ``character_ids`` were
        opened at or after ``since``, regardless of current status.

        SC3-B's per-tier daily ceiling on 起幕 openings: once a scene is opened the player has spent the
        slot, whether they play it to a close or abandon it — a closed row
        must count exactly like an open one, or the ceiling could be ground
        down by opening and immediately walking away. ``character_ids``
        carries every character the operator owns (the ceiling is
        per-player, not per-character) rather than a single id, so the
        caller sums across a whole account in one query instead of one
        per character."""


@dataclass(frozen=True, slots=True)
class StorySceneMaterial:
    """What the opening is being written *about*, layer-agnostically.

    Every waterfall layer produces this same shape, which is why SC1-B
    can add layers 2 and 3 without touching the opener, the message
    write, or the session row. Layer 1 fills it from an arc beat; layer 2
    from the first beat of a freshly forced season; layer 3 from
    relationship + memory + a story seed, with ``arc_id`` / ``beat_id``
    left ``None``.
    """

    layer: str
    title: str
    summary: str = ""
    arc_id: str | None = None
    beat_id: str | None = None
    arc_title: str = ""
    arc_premise: str = ""
    arc_tone: str = ""
    tension: str = ""
    scene_type: str | None = None
    location: str | None = None
    dramatic_question: str | None = None
    scene_characters: tuple[str, ...] = ()
    operator_position: str | None = None
    """Where the player stands in this scene (OP0's ``operator_position``
    on the beat), or ``None``. Layers 1/2 copy it straight from the beat;
    layer 3 (the ad-hoc side story) has no beat to have judged one and
    leaves this ``None`` on purpose — the player is already the addressee
    of the side story's relationship/memory/dialogue material, so forcing
    a value here would be inventing a judgment nobody made."""
    operator_note: str | None = None
    """Optional prose about *how* the player figures in this scene,
    copied from the same beat field."""


class StorySceneMaterialProviderPort(ABC):
    """One layer of the §3.1 waterfall.

    ``resolve`` returns the material this layer can offer *right now*, or
    ``None`` to fall through to the next layer. Raising is also treated
    as falling through (a broken layer must not swallow the layers below
    it), so implementations may be as adventurous as they need to be.
    """

    #: Value written to ``StorySceneSession.source_layer``.
    layer: str

    @abstractmethod
    async def resolve(
        self, character: Character, *, today: date,
    ) -> StorySceneMaterial | None: ...


@dataclass(frozen=True, slots=True)
class StorySceneOpeningContext:
    character: Character
    material: StorySceneMaterial
    today: date
    operator_primary_language: str = "zh-TW"


@dataclass(frozen=True, slots=True)
class StorySceneOpeningDraft:
    """The opening the player sees: a frame, narration, and a first line.

    ``narration`` and ``character_line`` become two messages in the
    thread; ``title`` / ``location`` / ``mood`` become the scene frame
    rendered around them and are persisted on the session so a reload
    (and SC1-C's per-turn context) still knows where the scene is set.
    """

    narration: str
    character_line: str
    title: str = ""
    location: str | None = None
    mood: str | None = None
    suggested_actions: tuple[str, ...] = field(default=())
    """Opening-move hints an opener may volunteer. Normally empty.

    Chips are produced by :class:`StorySceneChipsWriterPort` in a
    separate lightweight call (§4.2 ②) — the service asks that writer,
    not this field, so an opener that leaves it empty (all of them today)
    costs the player nothing."""


class StorySceneOpenerPort(ABC):
    """Writes the opening of a scene from its chosen material.

    Returns ``None`` when it cannot produce a real opening. There is no
    synthesized-placeholder fallback here, unlike the autonomous beat
    scene writer: the opening is what the hosted player is charged a
    fixed price for (red line 2), so a scaffolded
    stand-in would be billing them for filler. No opening → the whole
    action fails and nothing is written.
    """

    @abstractmethod
    async def write_opening(
        self, context: StorySceneOpeningContext,
    ) -> StorySceneOpeningDraft | None: ...


@dataclass(frozen=True, slots=True)
class StorySceneChipsContext:
    """What the chips writer reads to propose the player's next moves.

    ``session`` carries the whole scene frame (title / place / mood /
    scene type / dramatic question), so a chip can be about *this* scene
    rather than generic small talk; ``recent_lines`` are the last few
    turns rendered as prose, which is what makes the third chip a reply
    to what just happened instead of a restatement of the premise.
    """

    character: Character
    session: StorySceneSession
    recent_lines: tuple[str, ...] = ()
    operator_primary_language: str = "zh-TW"
    max_chips: int = 3


class StorySceneChipsWriterPort(ABC):
    """Suggests 2–3 things the player could do next, in this scene.

    Chips are an *aid*, never a menu: the player is always free to type
    anything, and the scene must play identically when this returns
    nothing. Every caller therefore treats an empty tuple as the normal
    degraded answer rather than an error — which is also why this port
    returns ``()`` instead of raising, and why implementations must not
    let a model failure escape.

    LLM-first red line: the chips are written
    by a model reading the scene. No keyword table, no phrasebook, no
    "if scene_type == conflict" branch anywhere behind this seam.
    """

    @abstractmethod
    async def suggest_actions(
        self, context: StorySceneChipsContext,
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class StorySceneClosingContext:
    """Everything the wrap-up writer reads (SC1-D).

    ``mode`` is the *close reason* the scene would be closed with, not a
    separate vocabulary: ``resolved`` asks the writer to judge whether
    the scene is finished and to wrap up only if it is, while ``manual``
    and ``timeout`` are already-decided closes that differ only in the
    frame they are written from (「你們就此打住」 vs 「你離開之後」).
    One vocabulary means the value that reaches the writer is the same
    value that reaches the session row.

    ``transcript`` is the scene as it was actually played, speaker-
    labelled and oldest-first, and ``player_lines`` is the subset the
    player themselves produced. The second is not a convenience: it is
    the evidence the red line is checked against (§3.4 #1) — a wrap-up
    may only describe player actions it can quote from these lines.
    """

    character: Character
    session: StorySceneSession
    mode: str
    transcript: tuple[str, ...] = ()
    player_lines: tuple[str, ...] = ()
    today: date | None = None
    operator_primary_language: str = "zh-TW"


@dataclass(frozen=True, slots=True)
class StorySceneClosingDraft:
    """A verdict, and — only when the scene is over — how it ended.

    ``resolved=False`` is the cheap, common answer on the in-turn path:
    the scene keeps playing and nothing else in this object is read, so
    an unfinished scene costs one short model reply and no writes.
    """

    resolved: bool
    closing_narration: str = ""
    """Third-person wrap-up appended to the thread as ``scene_narration``.

    May be empty even when ``resolved`` — see
    :class:`StorySceneCloserPort` on why a dropped narration is a valid
    outcome and a fabricated one never is."""
    canon_summary: str = ""
    """What actually got performed, as the sentence that lands in canon
    (a realized beat's narrative, or the side story's episodic memory)."""
    emotional_tone: str | None = None


class StorySceneCloserPort(ABC):
    """Judges whether a scene is over and writes how it ended (SC1-D).

    Returns ``None`` when it cannot produce a usable answer. The callers
    read that differently by mode, and the difference is the whole design:

    * ``resolved`` (the in-turn verdict) — ``None`` means *not resolved*.
      A wrap-up writer that failed must never end a scene the player is
      still playing, so the failure direction is "the scene continues".
    * ``manual`` / ``timeout`` (already decided) — ``None`` cannot stop
      the close. The session is closed regardless and the scene lands
      with whatever canon the caller can honestly assemble. The wrap-up's
      obligation is that the player is never stuck, not that prose always
      exists (the one price bought the opening).

    **Red line (§3.4 #1): the wrap-up must never invent player actions.**
    Implementations own both halves of enforcing that — instructing the
    model, *and* refusing a draft that claims the player did something no
    transcript line shows them doing. A refused draft is ``None``, which
    is exactly why ``None`` had to be a first-class answer rather than an
    error: rejecting a fabrication has to be as ordinary as an upstream
    timeout.
    """

    @abstractmethod
    async def write_closing(
        self, context: StorySceneClosingContext,
    ) -> StorySceneClosingDraft | None: ...
