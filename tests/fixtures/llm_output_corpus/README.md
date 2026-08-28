# `llm_output` differential corpus

Raw model-reply strings used to prove that the shared extraction layer
(`kokoro_link.llm_output`) never reads *less* than the hand-rolled
extractors it replaced.

## Why a corpus and not just more unit tests

The DH series replaces per-site extraction code with one shared
implementation. The risk of that shape of change is silent narrowing:
some input the old site handled — a fence, a preamble, a cut mid-string
— stops parsing, and nothing goes red because no test named that exact
input. So the regression gate is mechanical rather than enumerated: a
frozen copy of each old implementation lives in
`tests/unit/llm_output/_frozen_oracles.py`, and every string in this
corpus is fed to both. **Whatever the old code accepted, the new layer
must accept, with an equal value.** The reverse is allowed — the new
layer may read inputs the old one dropped, and it does.

## Files

### `literal_cases.json`

Verbatim strings, `{id, note, raw}`. Three sources:

1. Every input from the pre-existing parser tests —
   `test_tool_call_parser_object_literal.py`,
   `test_tool_call_parser_truncation.py`, `test_memory_extractor.py`.
   These are the shapes that were already known to matter.
2. Adversarial shapes those tests do not cover: CJK quotation marks,
   braces and brackets inside string values, escaped quotes, a cut
   landing right after a backslash, trailing commas, line comments,
   two payloads in a row, an array truncated between items,
   mis-nested regions, an extra closer.
3. The **`pathological.` family** (FX1/DH-1): deep unclosed nesting, the
   shape a model stuck in a repetition loop emits — a few thousand `[`
   and nothing else. `json.loads` recurses once per level and answers
   these with a `RecursionError`, which is *not* a `JSONDecodeError`,
   so it escaped every `except json.JSONDecodeError` in the codebase and
   killed the turn it was parsing for.

   A case may spell its string as `{"prefix": …, "raw": "[", "repeat":
   3000, "suffix": …}` instead of one literal — three thousand brackets
   pasted into a fixture hide the reason for the length.

   **These cases are excluded from the differential comparison** and
   only reach the "must not raise" tests, because the frozen oracles
   crash on them: "new reads everything old read" is vacuous where old
   raised instead of reading. See `corpus.PATHOLOGICAL_PREFIX`; the
   crash itself is asserted in `test_fx1_hardening.py` rather than left
   as an unexplained exclusion.

### `degradation_matrix.json`

The generator inputs for the synthetic matrix: canonical well-formed
`objects` / `arrays` (drawn from real prompt contracts — a tool call, a
post-turn five-in-one reply, a memory array, a schedule array),
`prefixes`, `suffixes`, and `truncate_fractions`.

`tests/unit/llm_output/corpus.py` expands them into two families:

- **contaminated but complete** — every `prefix × seed × suffix`. This
  is the "model wrapped the payload in something" axis.
- **truncated** — every `prefix × seed` cut at each fraction of the
  seed's length, with no suffix, because a reply that was cut off has
  nothing after it. This is the `max_tokens` axis, and the fractions
  land the cut in different structural positions: inside a key, inside
  a value, between items, after a comma, inside a nested array.

## Adding a case

Add the string to `literal_cases.json` with a `note` saying which real
failure mode it stands for. Nothing else needs changing: the
differential tests iterate the whole corpus, so a new case is
immediately load-bearing for every migrated site.

Do **not** record expected outputs here. The corpus is inputs only —
the expectation is always "whatever the frozen oracle did", which is
what makes it impossible to accidentally bless a regression by editing
a fixture.
