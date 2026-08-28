"""Shared plumbing for reading structured values out of LLM replies.

A top-level, dependency-free package on purpose — the same standing as
``contracts``. It imports nothing from ``domain``, ``application`` or
``infrastructure`` (stdlib only), so any layer may depend on it without
creating a cycle, and so nothing in here can quietly grow a business
rule.

The split of responsibility is the point of the package:

- **here** — text handling. Where does the JSON start, where does it
  end, is it truncated, can it be repaired, and why did it fail.
- **at the call site** — meaning. Which fields are required, what the
  caps and clamps are, what an unparseable reply should degrade into.

Nothing here interprets a field. Nothing here decides a fallback. Both
of those live with the caller that owns the contract, which is why
adopting this layer never changes a site's behaviour beyond widening
what it can read.

:mod:`~kokoro_link.llm_output.tokens` sits here for the same reason: it
is pure text measurement with no schema and no policy, and both the
application and infrastructure layers need it. Read its docstring before
using a number it returns — the estimate is ±30% and is only ever valid
for comparing one piece of text against another.
"""

from __future__ import annotations

from kokoro_link.llm_output.diagnostics import (
    ParseOutcome,
    ParseReason,
    log_parse_outcome,
)
from kokoro_link.llm_output.extract import (
    ARRAY_REGION,
    MAX_NESTING_DEPTH,
    OBJECT_REGION,
    BalancedRegion,
    balanced_end,
    extract_array,
    extract_array_outcome,
    extract_object,
    extract_object_outcome,
    first_balanced_region,
    first_region_is_array,
    iter_embedded_json,
    strip_fences,
)
from kokoro_link.llm_output.tokens import (
    estimate_tokens,
    estimate_total_tokens,
)

__all__ = [
    "ARRAY_REGION",
    "MAX_NESTING_DEPTH",
    "OBJECT_REGION",
    "BalancedRegion",
    "ParseOutcome",
    "ParseReason",
    "balanced_end",
    "estimate_tokens",
    "estimate_total_tokens",
    "extract_array",
    "extract_array_outcome",
    "extract_object",
    "extract_object_outcome",
    "first_balanced_region",
    "first_region_is_array",
    "iter_embedded_json",
    "log_parse_outcome",
    "strip_fences",
]
