"""The one implementation of "model reply text → JSON value".

Every LLM call in this codebase asks for JSON over a plain-text
channel (``ChatModelPort`` is ``str -> str`` on purpose — see the
provider-independence red line), so every consumer has to dig a value
out of prose. Before this module there were dozens of hand-rolled
diggers, and the hard parts — a string/escape-aware balance scanner and
truncation repair — existed in exactly one of them. Everywhere else, a
reply cut off by ``max_tokens`` was simply lost.

What this layer does, and equally what it refuses to do:

- **Anchors on the first opener and takes that one region only.** It
  does not hunt for "the region that happens to parse". A model that
  writes ``{微笑}好的：{"tool": …}`` gets ``None`` here, not the second
  region — because the callers that *want* the scanning behaviour
  (shape sniffing) ask for it explicitly via :func:`iter_embedded_json`,
  while the callers that parse a contract must not silently start
  accepting a payload buried behind roleplay markers.
- **Never raises, and never runs away, for any input.** Total
  functions; a malformed reply is a ``None``, never an exception in the
  middle of a turn. "Any input" includes the pathological ones a looping
  model emits, which is why there are two survival bounds here rather
  than none: :data:`MAX_NESTING_DEPTH` (the reason ``json.loads`` is
  guarded against ``RecursionError`` and not only ``JSONDecodeError``)
  and :data:`_SCAN_WORK_FACTOR` (the reason a reply full of unclosed
  openers cannot pin the worker reading it).
- **Answers "which family is this?" separately from "parse it".**
  :func:`first_balanced_region` reports the shape of a reply without
  requiring it to decode, so a call site that must refuse an
  array-shaped answer (:func:`first_region_is_array`) keeps refusing it
  when the model appends a sentence after the payload — and when the
  array itself was cut off before its ``]``, which is how an
  over-long array reply usually arrives. A guard spelled as "the whole
  reply parses and isn't a dict" silently evaporates on exactly those
  inputs.
- **Knows nothing about any schema.** Field names, caps, clamps and
  enum allowlists stay at the call site. This layer's whole contract is
  "text in, ``dict``/``list`` out".

Fence stripping is exposed (:func:`strip_fences`) but deliberately *not*
applied inside the extractors: brace scanning is already fence-agnostic
(backticks live outside the braces), so stripping first would only add
a way for the two to disagree about indices.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, Final, NamedTuple

from kokoro_link.llm_output.diagnostics import ParseOutcome, ParseReason


_FENCE_RE: Final = re.compile(r"^```[a-zA-Z0-9_+-]*\s*|\s*```$")
"""Leading ```` ```lang ```` marker and the trailing fence. Anchored to
the whole string (no ``MULTILINE``): a fence in the *middle* of a reply
belongs to a code block the model wrote on purpose, and eating it would
corrupt the payload around it."""

_CLOSERS: Final[dict[str, str]] = {"{": "}", "[": "]"}

OBJECT_REGION: Final = "object"
ARRAY_REGION: Final = "array"

_REGION_KINDS: Final[dict[str, str]] = {"{": OBJECT_REGION, "[": ARRAY_REGION}

MAX_NESTING_DEPTH: Final = 200
"""How deep a region may nest before the scanners call it unreadable.

Not a taste judgement — a survival bound. ``json.loads`` recurses once
per nesting level and raises :class:`RecursionError` (*not*
``JSONDecodeError``) past roughly a thousand, and a model stuck in a
repetition loop emits exactly that shape: three thousand ``[`` and
nothing else. Handing such a region to the decoder blew a
``RecursionError`` straight through the extractor and killed the turn.

The bound sits far below the interpreter's recursion limit and far above
any payload this codebase asks a model for — the deepest real contract
(the post-turn five-in-one object) nests four levels. A region past the
bound is reported as unbalanced, which every caller already handles.
"""

_DECODE_FAILURES: Final = (json.JSONDecodeError, RecursionError)
"""What ``json.loads`` may throw at us on model-authored text.

``RecursionError`` belongs here even though :data:`MAX_NESTING_DEPTH`
should keep it from ever firing: this layer's contract is *never
raises, for any input*, and a defence that depends on a second defence
being correct is not a defence. Deliberately narrow — a ``TypeError``
here would mean we passed something that is not a string, which is a
bug in this module and must stay loud.
"""

_SCAN_WORK_FACTOR: Final = 4
_SCAN_WORK_FLOOR: Final = 4096
"""Total characters the *wide* scanners may examine, as
``factor × len(text) + floor``.

The second survival bound in this module, and the same kind of bound as
:data:`MAX_NESTING_DEPTH`: not a taste judgement, a ceiling on what one
model reply can cost the worker that reads it. ``first_balanced_region``
and ``iter_embedded_json`` retry from every opener, and a reply that
sprays unclosed openers through a long body used to make each retry
re-scan the whole tail — quadratic, and a multi-megabyte reply of that
shape pinned a worker for a minute per call. :class:`_RegionIndex`
removes the re-scanning for every opener a scan actually walks past
(see its docstring); the budget covers what memoisation provably cannot:
an opener the previous scan only saw *inside a string*, and the openers
still stacked when a scan aborts on :data:`MAX_NESTING_DEPTH`. Both
need a fresh scan with a different starting state, and both can be
chained by a reply built to chain them.

Sized so that honest replies never come near it: with the memo, a reply
with no unclosed opener costs one pass, and the worst *shape* a real
model emits (one runaway bracket, then the payload) costs two. Past the
budget the scanners report "no region", which is the same degradation
every caller already handles for an unreadable reply.
"""


class BalancedRegion(NamedTuple):
    """Where a top-level ``{…}`` / ``[…]`` region sits, and which it is.

    ``end`` is inclusive, so the region is ``text[start : end + 1]``.
    ``kind`` is :data:`OBJECT_REGION` or :data:`ARRAY_REGION`.
    """

    kind: str
    start: int
    end: int


def strip_fences(raw: str) -> str:
    """Strip surrounding whitespace and a wrapping markdown code fence.

    Returns the text with outer whitespace already removed, so callers
    that need a fully-trimmed result should still ``.strip()`` — the two
    existing shape helpers differ on exactly that point and the
    difference is load-bearing for their scan bound.
    """
    if not raw:
        return ""
    return _FENCE_RE.sub("", raw.strip())


def balanced_end(text: str, start: int) -> int | None:
    """Index of the bracket closing the one at ``start``, or ``None``.

    ``None`` means the region never closes (truncated output, a stray
    brace), that it closes in the wrong order (``{"a": [ } ]``), **or**
    that it nests deeper than :data:`MAX_NESTING_DEPTH`. All three are
    "this is not a region I can hand to ``json.loads``", and collapsing
    them is safe: a mis-ordered region could never have decoded anyway,
    and an over-deep one would have crashed the decoder rather than
    decoded.

    Tracks string state and backslash escapes, so a bracket inside a
    string value — ``{"content": "quote with ]"}`` — does not move the
    depth counter. That single detail is why this scanner exists instead
    of ``text.rfind("}")``.
    """
    stack: list[str] = []
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSERS:
            stack.append(_CLOSERS[char])
            if len(stack) > MAX_NESTING_DEPTH:
                return None
        elif char in ("}", "]"):
            if not stack or stack[-1] != char:
                return None
            stack.pop()
            if not stack:
                return index
    return None


def _scan_region(text: str, start: int, ends: dict[int, int | None]) -> int:
    """One :func:`balanced_end` scan that records *everything* it settles.

    Byte-for-byte the same state machine as :func:`balanced_end`, with
    one addition: every opener the scan pushes is remembered, so when it
    finishes, the answer is written into ``ends`` not only for ``start``
    but for each of those openers too.

    That is sound, not an approximation. A scan started at opener ``j``
    would begin with ``in_string=False``; this scan was also outside a
    string when it pushed ``j`` (that is the only state in which it
    treats a character as an opener), and the state machine is
    deterministic, so from ``j`` onwards the two scans read identical
    state. Their stacks differ only by the frames below ``j``, which
    ``j``'s own scan never had — so they pop in step, they see the same
    top-of-stack at a mis-ordered closer, and ``j``'s depth is never the
    larger one. Whatever this scan concluded about ``j``, a scan from
    ``j`` concludes as well.

    Two things it may *not* conclude, and deliberately leaves unset:
    openers seen while inside a string (a scan starting there reads a
    different string parity, so nothing carries over), and the openers
    still stacked when the scan aborts on :data:`MAX_NESTING_DEPTH`
    (their own scans are shallower and would not have aborted there).
    Those cost a fresh scan — which is what :data:`_SCAN_WORK_FACTOR`
    is budgeting for.

    Returns the number of characters examined, for that budget.
    """
    stack: list[str] = []
    opens: list[int] = []
    in_string = False
    escape = False
    length = len(text)
    index = start
    while index < length:
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSERS:
            stack.append(_CLOSERS[char])
            opens.append(index)
            if len(stack) > MAX_NESTING_DEPTH:
                ends[start] = None
                return index - start + 1
        elif char in ("}", "]"):
            if not stack or stack[-1] != char:
                # Mis-ordered: no suffix can rescue any region still open.
                for opener_index in opens:
                    ends[opener_index] = None
                return index - start + 1
            stack.pop()
            ends[opens.pop()] = index
            if not stack:
                return index - start + 1
        index += 1
    # Ran off the end: everything still open never closes.
    for opener_index in opens:
        ends[opener_index] = None
    return length - start


class _RegionIndex:
    """Memo of *where does the region opening at index i close?*

    One instance per wide scan (:func:`first_balanced_region`,
    :func:`iter_embedded_json`) — never shared across calls, so there is
    no cache to invalidate and no state to leak between replies.

    Answers are identical to calling :func:`balanced_end` per opener;
    the only difference is that a scan which walks past other openers
    settles them on the way instead of leaving them to re-scan the same
    tail. Once the scanning budget (:data:`_SCAN_WORK_FACTOR`) is spent
    it stops scanning and reports "never closes" for anything it has not
    already settled.
    """

    __slots__ = ("_budget", "_ends", "_text")

    def __init__(self, text: str) -> None:
        self._text = text
        self._ends: dict[int, int | None] = {}
        self._budget = _SCAN_WORK_FACTOR * len(text) + _SCAN_WORK_FLOOR

    def end_of(self, start: int) -> int | None:
        try:
            return self._ends[start]
        except KeyError:
            pass
        if self._budget <= 0:
            return None
        self._budget -= _scan_region(self._text, start, self._ends)
        return self._ends.get(start)


def iter_embedded_json(text: str) -> Iterator[Any]:
    """Yield every top-level ``{…}`` / ``[…]`` region that parses, in order.

    Skips the regions that don't parse rather than stopping at them —
    that is what keeps a roleplay marker from hiding a real payload
    behind it (``{微笑}好，我查一下：{"name": …}`` has to reach the second
    region).

    This is the *wide* scan. Use it for deciding what a reply looks
    like; use :func:`extract_object` for reading a payload the prompt
    asked for.
    """
    index = 0
    length = len(text)
    regions = _RegionIndex(text)
    while index < length:
        if text[index] not in _CLOSERS:
            index += 1
            continue
        end = regions.end_of(index)
        if end is None:
            index += 1
            continue
        try:
            yield json.loads(text[index : end + 1])
        except _DECODE_FAILURES:
            pass
        index = end + 1


def first_balanced_region(text: str) -> BalancedRegion | None:
    """The first top-level region that *closes*, whatever it contains.

    Balance only — the region does not have to decode. That is the whole
    point: a call site asking "did this model reply with an array or with
    an object?" must still get an answer when the reply carries trailing
    commentary (``[…]\\n以上，有需要再跟我說！``), which is precisely the
    case where a whole-string ``json.loads`` says nothing and a guard
    built on one silently disappears.

    Skips openers that never close, the same way
    :func:`iter_embedded_json` does, so a truncated bracket in a
    roleplay marker cannot hide the real region behind it.

    Fence markers are neither brackets nor quotes, so this is
    fence-agnostic by construction — stripping fences first would change
    only the offsets, never the ``kind``.
    """
    index = 0
    length = len(text)
    regions = _RegionIndex(text)
    while index < length:
        char = text[index]
        if char not in _CLOSERS:
            index += 1
            continue
        end = regions.end_of(index)
        if end is None:
            index += 1
            continue
        return BalancedRegion(_REGION_KINDS[char], index, end)
    return None


def _first_opener(text: str) -> int | None:
    """Index of the first ``{`` / ``[`` in ``text``, closed or not."""
    for index, char in enumerate(text):
        if char in _CLOSERS:
            return index
    return None


def first_region_is_array(raw: str) -> bool:
    """Is this reply array-shaped, structurally speaking?

    The guard every object-reading call site needs. When a model answers
    an "emit one object" prompt with a *list* of them, the object
    extractors will happily reach inside and hand back the first element
    — a well-shaped, plausible, wrong payload that sails through an
    ``isinstance(value, dict)`` check at the call site. Asking the
    scanner which family the reply opens with is the only test that
    keeps working when the reply is not parseable as a whole.

    **A truncated array is still an array.** The two failure modes this
    guard exists for arrive together far more often than either arrives
    alone: a model that ignores "emit one object" and answers with a
    list is also a model emitting more tokens than the prompt budgeted
    for, so the reply is cut before the closing ``]``. Judged only by
    "which region *closes* first", such a reply looks like an object —
    the outer ``[`` never balances and gets skipped, and the first
    element ``{…}`` becomes the answer. That is precisely the case the
    guard was written to stop, arriving through the guard's own front
    door: the call site is then handed peer A's profile as peer B's.

    So an opener that never closes still decides the family. The old
    "first region that closes" answer is kept as well and still wins
    when it says *array* — this only ever adds refusals, never removes
    one — and a first opener that closed *as an object* still means
    "not an array", trailing commentary and all.
    """
    text = raw or ""
    region = first_balanced_region(text)
    if region is not None and region.kind == ARRAY_REGION:
        return True
    opener = _first_opener(text)
    if opener is None:
        return False
    if region is not None and region.start == opener:
        # The reply's first opener closed, and it is an object.
        return False
    return text[opener] == "["


def extract_object(raw: str, *, repair_truncated: bool = True) -> dict[str, Any] | None:
    """First top-level JSON object in ``raw``, or ``None``."""
    return extract_object_outcome(raw, repair_truncated=repair_truncated).value


def extract_array(raw: str, *, repair_truncated: bool = True) -> list[Any] | None:
    """First top-level JSON array in ``raw``, or ``None``."""
    return extract_array_outcome(raw, repair_truncated=repair_truncated).value


def extract_object_outcome(
    raw: str, *, repair_truncated: bool = True,
) -> ParseOutcome:
    """:func:`extract_object` with the failure reason attached."""
    return _extract(raw, opener="{", expected=dict, repair_truncated=repair_truncated)


def extract_array_outcome(
    raw: str, *, repair_truncated: bool = True,
) -> ParseOutcome:
    """:func:`extract_array` with the failure reason attached."""
    return _extract(raw, opener="[", expected=list, repair_truncated=repair_truncated)


def _extract(
    raw: str,
    *,
    opener: str,
    expected: type,
    repair_truncated: bool,
) -> ParseOutcome:
    if not raw:
        return ParseOutcome(None, ParseReason.NO_JSON, 0)
    length = len(raw)
    start = raw.find(opener)
    if start == -1:
        return ParseOutcome(None, ParseReason.NO_JSON, length)

    end = balanced_end(raw, start)
    if end is None:
        # Truncated (or mis-nested). Repair is the only path left.
        repaired = _repair(raw, start) if repair_truncated else None
        if isinstance(repaired, expected):
            return ParseOutcome(repaired, ParseReason.REPAIRED, length)
        return ParseOutcome(None, ParseReason.UNBALANCED, length)

    try:
        parsed = json.loads(raw[start : end + 1])
    except _DECODE_FAILURES:
        parsed = None
    if isinstance(parsed, expected):
        return ParseOutcome(parsed, ParseReason.OK, length)

    # A region that balances but does not decode is a *syntax* problem
    # (single quotes, a trailing comma, a CJK quotation mark), not a
    # truncation — repair almost never rescues it. We still try, because
    # the balanced region can be followed by a second, truncated one and
    # the cost of asking is one scan.
    if repair_truncated:
        repaired = _repair(raw, start)
        if isinstance(repaired, expected):
            return ParseOutcome(repaired, ParseReason.REPAIRED, length)
    return ParseOutcome(None, ParseReason.DECODE_ERROR, length)


def _repair(text: str, start: int) -> Any | None:
    """Best-effort recovery of a region that was cut off mid-flight.

    The scenario is one upstream truncation, not a general tolerant
    parser: the model emitted valid JSON and the response stopped
    before the closers arrived (``max_tokens``, a dropped stream). We
    close a dangling string, drop a comma whose value never came, then
    append the still-open closers in reverse order and retry.

    Returns ``None`` when the region is already structurally complete —
    then the failure was a syntax error and inventing closers would only
    turn brace soup into a dict — when the region is mis-nested, where no
    suffix could rescue it, and when it nests past
    :data:`MAX_NESTING_DEPTH`, where "append the missing closers" means
    appending thousands of them and handing the decoder a stack overflow.
    """
    stack: list[str] = []
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSERS:
            stack.append(_CLOSERS[char])
            if len(stack) > MAX_NESTING_DEPTH:
                return None
        elif char in ("}", "]"):
            if not stack or stack[-1] != char:
                return None
            stack.pop()
    if not stack and not in_string:
        return None

    candidate = text[start:]
    if in_string:
        # Close the dangling string. When the cut landed right after a
        # backslash, that backslash would escape our own closing quote,
        # so it needs a partner first.
        if candidate.endswith("\\"):
            candidate += "\\"
        candidate += '"'
    candidate = candidate.rstrip()
    if candidate.endswith(","):
        candidate = candidate[:-1]
    candidate += "".join(reversed(stack))
    try:
        return json.loads(candidate)
    except _DECODE_FAILURES:
        return None
