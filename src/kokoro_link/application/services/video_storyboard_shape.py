"""The **neutral** structured storyboard Core emits (plan D12).

Why this module exists
----------------------

V0 of ``video_storyboard`` asked the model for one blob of free English
cinematography prose. That threw away half of what the hosted I2V engine
can do — MiniMax H3 generates **audio alongside the picture**, and prose
that never mentions a soundscape ships a clip whose sound was invented
from nothing.

The obvious fix would be to teach the baseline prompt H3's own mandatory
format (an anchor line, three fixed field names, ``[Shot N]`` prefixes,
``MM:SS.mmm`` cut times). Owner ruling D12 (2026-08-07) says no: that
knowledge belongs to the **pipeline adapter**, not to Core. Core produces
a neutral structure — what is seen, how the camera moves, what must not
drift from the first frame, what is heard, what (if anything) is scored —
and each pipeline renders it into whatever its engine wants. Swapping H3
for Wan or a cloud vendor then costs one adapter, not a prompt-pack
release per engine.

The authoritative consumer-side dataclasses live in the media worker's
``pipelines/h3_prompt.py``. This module is the producer half of that
contract, and deliberately restates only the parts of it a producer must
honour:

* the **first** shot never carries a cut time (the guide forbids one);
* every later shot carries a **strictly increasing** one, below the clip
  length — a violation raises in the worker, i.e. loses the whole job;
* ``visual`` / soundscape / music are English; dialogue ``text`` is the
  character's own language, verbatim.

Because those are hard failure modes downstream, this module is where
they are *enforced*, not merely requested: :func:`parse_storyboard`
drops any shot the worker would refuse rather than forwarding it.

Nothing here talks to a model, a clock or a network — it is pure parsing
and shaping, which is what lets the tests pin the contract literally.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from kokoro_link.llm_output import extract_object_outcome, first_region_is_array

DEFAULT_CLIP_SECONDS = 5
"""The clip length every hosted I2V pass renders today. Threaded through
rather than hard-coded in the prompt so a future provider with a
different window does not need a prompt-pack release."""

MAX_STORYBOARD_CHARS = 3000
"""Ceiling on the **serialised** storyboard string handed to the video
pipeline.

Raised from V0's 1500 because the payload now carries materially more:
per shot a ``visual`` body plus a camera object, consistency anchors and
an optional dialogue array, and then two new top-level blocks
(``overall_soundscape`` up to four sentences, ``non_diegetic_music`` up
to three) — on top of ~15-20% JSON key/quote overhead that the old prose
blob did not pay at all. Budgeted: 2 shots x ~700 visual + ~150 camera +
~250 anchors/style + ~450 soundscape + ~350 music + ~400 structural
≈ 2800.

Bounded on three sides, all of which 3000 clears:

* the worker's own ceilings — 1200 chars per shot, 4000 for the whole
  rendered description (``h3_prompt._MAX_SHOT_CHARS`` /
  ``_MAX_DESCRIPTION_CHARS``); staying under those means our output is
  never silently clipped mid-sentence downstream;
* the Gateway's LLM **output** budget — ``yuralume.llm.default-max-tokens``
  = 4096 (``services/gateway`` ``application.yml``; per-request override
  allowed up to 16384). 3000 chars of English-dominant JSON is roughly
  1000-1300 tokens, so the generation finishes well inside the default
  and never needs the override;
* the Gateway's **request** body cap — ``max-request-body-bytes`` =
  262144 (256 KiB). The storyboard instruction is ~4 KB, so the request
  side is dominated by the frame, not by this constant.

Enforcement is by **dropping trailing shots**, never by cutting the
string: a truncated JSON document still starts with ``{``, so the worker
would take it for prose and render the raw envelope into the clip. See
:meth:`NeutralStoryboard.to_json`.
"""

# Per-field clamps. These keep a single runaway field from eating the
# whole budget and forcing a shot to be dropped for no good reason.
MAX_SHOTS = 4
MAX_VISUAL_CHARS = 900
MAX_ANCHORS = 6
MAX_ANCHOR_CHARS = 80
MAX_STYLE_CHARS = 80
MAX_CAMERA_TARGET_CHARS = 120
MAX_DIALOGUE_LINES = 4
MAX_DIALOGUE_CHARS = 240
MAX_SPEAKER_DESCRIPTION_CHARS = 160
MAX_SOUNDSCAPE_CHARS = 500
MAX_MUSIC_CHARS = 400

CAMERA_MOTION_TYPES = (
    "Zoom In",
    "Zoom Out",
    "Push In",
    "Pull Out",
    "Pan Left",
    "Pan Right",
    "Truck Left",
    "Truck Right",
    "Tilt Up",
    "Tilt Down",
    "Pedestal Up",
    "Pedestal Down",
    "Arc Shot",
    "Tracking Shot",
    "Static Shot",
    "Shake Slightly",
    "Shake Strongly",
    "POV",
    "Roll Clockwise",
    "Roll Counterclockwise",
)
"""The engine-neutral motion vocabulary the prompt offers the model.

It happens to match H3's list because that is the richest of the ones we
have seen, but nothing downstream *requires* a value from it — the worker
renders an unknown move as ``performs a <x> move`` rather than dropping
it. Canonicalising here only stops ``truck_right`` / ``TRUCK RIGHT`` /
``Truck-Right`` from reaching the adapter as three different moves."""

CAMERA_AMPLITUDES = ("small", "large")
"""Medium is expressed by *omitting* the field, per the guide."""

CAMERA_SPEEDS = ("slow", "fast")
"""Normal is expressed by *omitting* the field, per the guide."""

_CANONICAL_MOTION = {
    " ".join(value.lower().split()): value for value in CAMERA_MOTION_TYPES
}


# --------------------------------------------------------------- shapes


@dataclass(frozen=True, slots=True)
class CameraMotion:
    """One camera move in three dimensions: what, how far, how fast."""

    motion_type: str
    amplitude: str = ""
    speed: str = ""
    target: str = ""

    def to_payload(self) -> dict[str, str]:
        payload = {"motion_type": self.motion_type}
        if self.amplitude:
            payload["amplitude"] = self.amplitude
        if self.speed:
            payload["speed"] = self.speed
        if self.target:
            payload["target"] = self.target
        return payload


@dataclass(frozen=True, slots=True)
class DialogueLine:
    """One spoken line.

    ``text`` is the character's own language, verbatim — translating it
    would change which language the engine *speaks*, which is the one
    thing a caption could never fix afterwards."""

    text: str
    speaker_id: str = "S1"
    language: str = ""
    speaker_description: str = ""
    delivery: str = "says"
    voiceover: bool = False

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "speaker_id": self.speaker_id,
            "text": self.text,
        }
        if self.language:
            payload["language"] = self.language
        if self.speaker_description:
            payload["speaker_description"] = self.speaker_description
        if self.delivery and self.delivery != "says":
            payload["delivery"] = self.delivery
        if self.voiceover:
            payload["voiceover"] = True
        return payload


@dataclass(frozen=True, slots=True)
class StoryboardShot:
    """One shot. ``start_at_seconds`` is ``None`` on the first shot only."""

    visual: str
    start_at_seconds: float | None = None
    style: str = ""
    consistency_anchors: tuple[str, ...] = ()
    camera: CameraMotion | None = None
    dialogue: tuple[DialogueLine, ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"visual": self.visual}
        if self.start_at_seconds is not None:
            payload["start_at_seconds"] = self.start_at_seconds
        if self.style:
            payload["style"] = self.style
        if self.consistency_anchors:
            payload["consistency_anchors"] = list(self.consistency_anchors)
        if self.camera is not None:
            payload["camera"] = self.camera.to_payload()
        if self.dialogue:
            payload["dialogue"] = [line.to_payload() for line in self.dialogue]
        return payload


@dataclass(frozen=True, slots=True)
class NeutralStoryboard:
    """The whole neutral storyboard — shots plus the two audio blocks."""

    shots: tuple[StoryboardShot, ...] = ()
    soundscape: str = ""
    music: str = ""

    @property
    def is_usable(self) -> bool:
        return bool(self.shots)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "shots": [shot.to_payload() for shot in self.shots],
        }
        if self.soundscape:
            payload["overall_soundscape"] = self.soundscape
        if self.music:
            payload["non_diegetic_music"] = self.music
        return payload

    def to_json(self, *, max_chars: int | None = MAX_STORYBOARD_CHARS) -> str:
        """Serialise, fitting ``max_chars`` **without ever cutting the
        string**.

        A truncated JSON document is worse than a shorter storyboard: the
        worker's ``coerce_storyboard`` only takes the structured path when
        the string parses, so a half-written object degrades to "prose"
        and renders ``{"shots": [{"visual": ...`` into the video prompt
        itself. So overflow is paid for by dropping trailing shots, and
        only in the pathological case (one shot still too big) by
        clamping that shot's ``visual`` — both of which keep the document
        valid.
        """
        payload = self.to_payload()
        text = _dumps(payload)
        if max_chars is None or len(text) <= max_chars:
            return text

        shots = list(payload.get("shots") or [])
        while len(shots) > 1 and len(text) > max_chars:
            shots.pop()
            payload["shots"] = shots
            text = _dumps(payload)
        if len(text) > max_chars and shots:
            visual = str(shots[0].get("visual", ""))
            keep = max(80, len(visual) - (len(text) - max_chars))
            shots[0] = {**shots[0], "visual": _clamp(visual, keep)}
            payload["shots"] = shots
            text = _dumps(payload)
        return text


# -------------------------------------------------------------- parsing


_FENCE_OPEN_RE = re.compile(r"^```(?:json)?\s*")
_FENCE_CLOSE_RE = re.compile(r"```\s*$")
_SHOTS_ARRAY_RE = re.compile(r'"shots"\s*:\s*\[')

SCHEMA_LEAK_MARKERS = (
    '"shots"',
    '"visual"',
    '"overall_soundscape"',
    '"non_diegetic_music"',
    '"storyboard_prompt"',
)
"""If a would-be prose fallback still carries one of these keys it is a
serialised storyboard envelope that failed to parse. Forwarding it would
render JSON keys into the clip, so it is discarded instead."""

LEGACY_PROSE_FIELD = "storyboard_prompt"
"""V0's single-field schema. Still honoured on the way *in* so a
deployment running an un-refreshed external prompt pack degrades to good
prose instead of to nothing."""


def strip_code_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = _FENCE_CLOSE_RE.sub("", _FENCE_OPEN_RE.sub("", text)).strip()
    return text


def _crude_object_span_decodes(text: str) -> bool:
    """Old behaviour, preserved exactly: does the first-``{`` to
    last-``}`` slice parse as JSON at all. Behaviourally identical to
    the old greedy ``\\{.*\\}`` (``DOTALL``) regex it replaces here — both
    are "grab from the first opener to the last closer" — just spelled
    with string methods instead of a pattern. Used only as a gate; see
    ``decode_json_object``.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        json.loads(text[start: end + 1])
    except (json.JSONDecodeError, RecursionError):
        return False
    return True


def decode_json_object(text: str) -> Mapping[str, object] | None:
    """Whole document first, then the first ``{...}`` block inside prose.

    DH2-services: the second step used to be a **greedy** regex
    (``\\{.*\\}``, ``DOTALL``) that ran from the first ``{`` to the
    *last* ``}`` in the whole string. That crude span is now only used
    to *gate* the swap to the shared scanner: when it already decodes
    (the common single-object-reply case, or a lone object nested one
    level inside otherwise-scalar wrapping), old already succeeded with
    exactly that value and the balanced scanner recovers it identically.
    When it does *not* decode, ``text`` is checked for opening with a
    balanced **array** — an array of several shot-shaped objects, most
    likely — and if so, extraction is refused rather than reaching past
    that structure for a plausible-looking fragment (a caller expecting
    the *whole* storyboard must not be handed just its first shot
    dressed up as the top-level document). FX1/DH-2: that check is
    structural now. It used to require the whole reply to be complete,
    well-formed JSON, which meant a storyboard array followed by one
    line of commentary switched the guard off and produced exactly the
    first-shot-as-document confusion it exists to prevent. Only when
    neither crude check finds a verdict
    (most likely: the object never closed at all — truncated) does the
    shared scanner get a wider try. Repair is deliberately **off**
    throughout — this module's own :func:`_salvage_payload` already owns
    truncation recovery for the ``shots`` schema specifically (it can
    build a partial storyboard out of the shots that arrived before a
    cut; the generic repair in ``llm_output`` knows nothing about that
    schema and could reach a parseable-but-wrong dict for a truncated
    reply, pre-empting the schema-aware salvage path that
    ``parse_storyboard`` falls to below).
    """
    if not text:
        return None
    if not _crude_object_span_decodes(text) and first_region_is_array(text):
        return None
    return extract_object_outcome(text, repair_truncated=False).value


def parse_storyboard(
    raw: str, *, clip_seconds: int = DEFAULT_CLIP_SECONDS,
) -> NeutralStoryboard | None:
    """Model answer → a storyboard the worker is guaranteed to accept.

    ``None`` means "there is nothing structured here" — the caller then
    decides between prose and the blind fallback. Tolerant by design, in
    the order deviations actually happen:

    1. clean JSON, fenced or not;
    2. a JSON object with chatter around it;
    3. a **truncated** object — the AE-series 4096-ceiling lesson. Every
       complete shot object written before the cut is still recoverable,
       and a storyboard of the shots that survived is a real storyboard.
    """
    text = strip_code_fence(raw)
    if not text:
        return None
    payload = decode_json_object(text)
    if payload is None or "shots" not in payload:
        salvaged = _salvage_payload(text)
        if salvaged is None:
            return None
        payload = salvaged
    return _normalise(payload, clip_seconds=clip_seconds)


def looks_like_schema_leak(text: str) -> bool:
    stripped = (text or "").lstrip()
    return stripped.startswith("{") and any(
        marker in stripped for marker in SCHEMA_LEAK_MARKERS
    )


# ------------------------------------------------------------- salvage


def _salvage_payload(text: str) -> dict[str, object] | None:
    shots = _salvage_shots(text)
    if not shots:
        return None
    payload: dict[str, object] = {"shots": shots}
    soundscape = _salvage_string(text, "overall_soundscape") or _salvage_string(
        text, "soundscape",
    )
    if soundscape:
        payload["overall_soundscape"] = soundscape
    music = _salvage_string(text, "non_diegetic_music") or _salvage_string(
        text, "music",
    )
    if music:
        payload["non_diegetic_music"] = music
    return payload


def _salvage_shots(text: str) -> list[Mapping[str, object]]:
    """Pull every *complete* object out of a possibly-unclosed ``shots``
    array, using the JSON decoder itself rather than brace counting."""
    match = _SHOTS_ARRAY_RE.search(text)
    if match is None:
        return []
    decoder = json.JSONDecoder()
    index = match.end()
    shots: list[Mapping[str, object]] = []
    length = len(text)
    while index < length and len(shots) < MAX_SHOTS:
        while index < length and text[index] in " \t\r\n,":
            index += 1
        if index >= length or text[index] != "{":
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            break
        if isinstance(value, Mapping):
            shots.append(value)
    return shots


def _salvage_string(text: str, key: str) -> str:
    pattern = re.compile(
        rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        return ""
    try:
        value = json.loads(f'"{match.group(1)}"')
    except json.JSONDecodeError:
        return ""
    return str(value).strip()


# ----------------------------------------------------------- shaping


def _normalise(
    payload: Mapping[str, object], *, clip_seconds: int,
) -> NeutralStoryboard | None:
    raw_shots = payload.get("shots")
    if not isinstance(raw_shots, Sequence) or isinstance(
        raw_shots, (str, bytes),
    ):
        return None

    window = float(clip_seconds if clip_seconds > 0 else DEFAULT_CLIP_SECONDS)
    shots: list[StoryboardShot] = []
    previous = 0.0
    for entry in raw_shots:
        if len(shots) >= MAX_SHOTS:
            break
        if not isinstance(entry, Mapping):
            continue
        shot = _shot(
            entry,
            first=not shots,
            previous=previous,
            window=window,
        )
        if shot is None:
            # A shot whose cut time is missing, backwards or past the end
            # of the clip is one the worker would raise on (or silently
            # drop). Losing it here costs a second of screen time; sending
            # it costs the whole job.
            continue
        shots.append(shot)
        if shot.start_at_seconds is not None:
            previous = shot.start_at_seconds

    if not shots:
        return None
    return NeutralStoryboard(
        shots=tuple(shots),
        soundscape=_clamp(
            _text(payload, "overall_soundscape", "soundscape"),
            MAX_SOUNDSCAPE_CHARS,
        ),
        music=_clamp(
            _text(payload, "non_diegetic_music", "music"), MAX_MUSIC_CHARS,
        ),
    )


def _shot(
    payload: Mapping[str, object],
    *,
    first: bool,
    previous: float,
    window: float,
) -> StoryboardShot | None:
    visual = _clamp(
        _text(payload, "visual", "description", "action"), MAX_VISUAL_CHARS,
    )
    dialogue = _dialogue(payload.get("dialogue"))
    if not visual and not dialogue:
        return None

    start: float | None = None
    if not first:
        start = _seconds(
            payload.get("start_at_seconds", payload.get("start_seconds")),
        )
        if start is None or start <= previous or start >= window:
            return None

    # ``style`` and ``consistency_anchors`` only describe the frame the
    # clip starts on, so carrying them on later shots is pure payload.
    style = _clamp(_text(payload, "style"), MAX_STYLE_CHARS) if first else ""
    anchors = _anchors(payload.get("consistency_anchors")) if first else ()
    return StoryboardShot(
        visual=visual,
        start_at_seconds=start,
        style=style,
        consistency_anchors=anchors,
        camera=_camera(payload.get("camera")),
        dialogue=dialogue,
    )


def _camera(value: object) -> CameraMotion | None:
    if not isinstance(value, Mapping):
        return None
    motion = _text(value, "motion_type", "motion", "type")
    if not motion:
        return None
    canonical = _CANONICAL_MOTION.get(_key(motion), motion.strip()[:60])
    amplitude = _key(_text(value, "amplitude"))
    speed = _key(_text(value, "speed"))
    return CameraMotion(
        motion_type=canonical,
        amplitude=amplitude if amplitude in CAMERA_AMPLITUDES else "",
        speed=speed if speed in CAMERA_SPEEDS else "",
        target=_clamp(
            _text(value, "target", "subject"), MAX_CAMERA_TARGET_CHARS,
        ),
    )


def _dialogue(value: object) -> tuple[DialogueLine, ...]:
    lines: list[DialogueLine] = []
    for entry in _sequence(value):
        if len(lines) >= MAX_DIALOGUE_LINES:
            break
        if not isinstance(entry, Mapping):
            continue
        text = _clamp(
            _text(entry, "text", "line", "content"), MAX_DIALOGUE_CHARS,
        )
        if not text:
            continue
        lines.append(
            DialogueLine(
                text=text,
                speaker_id=_text(entry, "speaker_id", "speaker")[:12] or "S1",
                language=_text(entry, "language", "lang")[:32],
                speaker_description=_clamp(
                    _text(entry, "speaker_description", "speaker_identity"),
                    MAX_SPEAKER_DESCRIPTION_CHARS,
                ),
                delivery=_text(entry, "delivery")[:32] or "says",
                voiceover=bool(entry.get("voiceover", False)),
            ),
        )
    return tuple(lines)


def _anchors(value: object) -> tuple[str, ...]:
    anchors: list[str] = []
    for entry in _sequence(value):
        if len(anchors) >= MAX_ANCHORS:
            break
        if not isinstance(entry, str):
            continue
        anchor = _clamp(entry.strip(), MAX_ANCHOR_CHARS)
        if anchor:
            anchors.append(anchor)
    return tuple(anchors)


# ------------------------------------------------------------ plumbing


def _dumps(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()


def _text(payload: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _key(value: str) -> str:
    return " ".join(value.replace("_", " ").replace("-", " ").lower().split())


def _seconds(value: object) -> float | None:
    """Accepts a number or the numeric string models keep emitting.

    ``bool`` is rejected explicitly — it is an ``int`` subclass in Python,
    and ``start_at_seconds: true`` becoming ``1.0`` would invent a cut."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().rstrip("sS").strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, 2)


def _clamp(text: str, limit: int) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    head = body[:limit].rsplit(" ", 1)[0].rstrip(",;:- ")
    return head or body[:limit]


__all__ = [
    "CAMERA_AMPLITUDES",
    "CAMERA_MOTION_TYPES",
    "CAMERA_SPEEDS",
    "DEFAULT_CLIP_SECONDS",
    "LEGACY_PROSE_FIELD",
    "MAX_STORYBOARD_CHARS",
    "SCHEMA_LEAK_MARKERS",
    "CameraMotion",
    "DialogueLine",
    "NeutralStoryboard",
    "StoryboardShot",
    "decode_json_object",
    "looks_like_schema_leak",
    "parse_storyboard",
    "strip_code_fence",
]
