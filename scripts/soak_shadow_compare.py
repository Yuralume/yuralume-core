"""Phase 2 synthetic-soak exit judge (HOSTED_CORE_SCALING §13 Phase 2 / §14).

Compares the embedded scheduler's tick journal (``scheduler_tick_journal``)
against the distributed shadow queue's completed jobs (``background_jobs``) over
a soak window and decides the three machine-checkable exit criteria:

    漏job (missed)      = 0
    重複 logical job    = 0
    錯 tenant/freeze    = 0

The core is a PURE, importable comparison function
(:func:`compare_shadow`) over plain value objects so it is exhaustively
unit-testable with synthetic fixtures and never needs a database. A thin async
loader (:func:`load_soak_rows`) reads the two tables via the real SA models when
run as a CLI.

Comparison semantics (see the module-level constants and each detector):

* **±1 bucket tolerance** — the embedded tick and the shadow enqueue observe
  ``floor(unix / bucket_seconds)`` independently, so a matched pair may land one
  bucket apart across a clock edge. Every match therefore accepts ``{B-1, B,
  B+1}``.
* **Boundary grace** — the first and last bucket of the window are EXCLUDED
  from the gated detectors: at the edges one side may legitimately be truncated
  (the soak started/stopped mid-bucket), which is not a defect.

Exit code is ``0`` only when ``missed == duplicates == wrong_runs == 0`` AND at
least ``--min-buckets`` interior buckets actually CARRIED observed work
(``buckets_covered`` — a journal entry or a done job landed there), not merely
fell inside a wide ``--from``/``--to`` window. An empty or idle database over a
nominal-width window covers no buckets and must NOT pass (silence is not
success).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Local copies of the two well-known kinds. Deliberately NOT imported from the
# application service so the pure comparison core stays free of app wiring and
# importable in a bare test process.
CHARACTER_TICK_KIND = "character_tick"
SOCIAL_TICK_KIND = "social_tick"
_DONE = "done"
_DEAD = "dead"
_FAILED = "failed"

# Documented dry-run skip reasons the worker may emit (kept in lockstep with
# ``background_shadow_worker._SKIP_*``). A ``would_run=false`` done job is only a
# *correct* skip when its ``skip_reason`` is one of these; anything else is a
# malformed outcome (a new hard gate). ``identity_mismatch`` is on the allowlist
# so it parses as well-formed, but it is a DEFECT gated separately — never a
# correct skip.
_SKIP_FROZEN = "frozen"
_SKIP_MISSING = "missing_character"
_SKIP_SUBSCRIPTION_LOCKED = "subscription_locked"
_IDENTITY_MISMATCH_REASON = "identity_mismatch"
_SKIP_REASON_ALLOWLIST = frozenset({
    _SKIP_FROZEN,
    _SKIP_MISSING,
    _SKIP_SUBSCRIPTION_LOCKED,
    _IDENTITY_MISMATCH_REASON,
})

_DEFAULT_BUCKET_SECONDS = 300
# 24h / 300s = 288 buckets; minus the two boundary-grace buckets = 286. A soak
# that evaluated fewer than this did not cover a full day and must not pass.
_DEFAULT_MIN_BUCKETS = (24 * 3600) // _DEFAULT_BUCKET_SECONDS - 2


# --------------------------------------------------------------------------- #
# Value objects (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One embedded-tick journal row: ``(bucket, kind, character_id)``.

    ``character_id`` is ``None`` for ``social_tick`` (the cross-character global
    bucket). ``recorded_at`` is carried for completeness but is not used by the
    comparison (bucket alignment is what matters)."""

    bucket: int
    kind: str
    character_id: str | None = None
    recorded_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobRow:
    """One ``background_jobs`` row projected to what the comparison needs."""

    kind: str
    status: str
    idempotency_key: str
    character_id: str | None = None
    tenant_id: str | None = None
    operator_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    outcome: Mapping[str, Any] | None = None
    attempt_count: int = 0

    @property
    def is_done(self) -> bool:
        return self.status == _DONE

    @property
    def skip_reason(self) -> str | None:
        if not self.outcome:
            return None
        reason = self.outcome.get("skip_reason")
        return reason if isinstance(reason, str) and reason else None

    @property
    def would_run(self) -> bool:
        """Whether the dry-run worker judged this job as work it *would* run.

        Only meaningful for a well-formed outcome; a false/absent value means
        the shadow validation deliberately skipped."""
        if not self.outcome:
            return False
        return self.outcome.get("would_run") is True

    @property
    def outcome_valid(self) -> bool:
        """Whether a done job's outcome parses to the documented dry-run shape.

        ``{dry_run: true, would_run: bool}`` and, when ``would_run`` is false, a
        non-empty ``skip_reason`` from the documented allowlist. A malformed /
        missing outcome must NOT count as a silent success — it is gated by
        ``invalid_outcomes`` (H5)."""
        if not isinstance(self.outcome, Mapping):
            return False
        if self.outcome.get("dry_run") is not True:
            return False
        would_run = self.outcome.get("would_run")
        if not isinstance(would_run, bool):
            return False
        if would_run is False:
            reason = self.outcome.get("skip_reason")
            if not (
                isinstance(reason, str) and reason in _SKIP_REASON_ALLOWLIST
            ):
                return False
        return True

    @property
    def is_identity_mismatch(self) -> bool:
        """A well-formed skip whose reason is the H3 identity-mismatch marker —
        a defect gated on its own, not a correct skip."""
        return self.skip_reason == _IDENTITY_MISMATCH_REASON


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """Machine-checkable outcome of one soak-window comparison."""

    from_bucket: int
    to_bucket: int
    bucket_seconds: int
    buckets_evaluated: int
    journal_buckets_covered: int
    shadow_buckets_covered: int
    missed: tuple[dict[str, Any], ...]
    duplicates: tuple[dict[str, Any], ...]
    wrong_runs: tuple[dict[str, Any], ...]
    identity_mismatches: tuple[dict[str, Any], ...]
    invalid_outcomes: tuple[dict[str, Any], ...]
    informational: dict[str, Any]

    @property
    def missed_count(self) -> int:
        return len(self.missed)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates)

    @property
    def wrong_run_count(self) -> int:
        return len(self.wrong_runs)

    @property
    def identity_mismatch_count(self) -> int:
        return len(self.identity_mismatches)

    @property
    def invalid_outcome_count(self) -> int:
        return len(self.invalid_outcomes)

    @property
    def gates_clean(self) -> bool:
        """All FIVE hard exit gates are zero (does NOT include the min-buckets
        floor — that is applied by the caller so it can name the threshold):
        missed, duplicates, wrong-runs, identity-mismatches (H3) and
        invalid-outcomes (H5)."""
        return (
            self.missed_count == 0
            and self.duplicate_count == 0
            and self.wrong_run_count == 0
            and self.identity_mismatch_count == 0
            and self.invalid_outcome_count == 0
        )

    def passes(self, *, min_buckets: int) -> bool:
        """Pass = all gates clean AND BOTH the journal side and the shadow side
        independently cover at least ``min_buckets`` interior buckets that carry
        observed work (H5 split coverage). Requiring each side separately is
        what makes 'silence is not success' real on both halves: a window where
        only the shadow enqueued (no embedded journal) — or only the embedded
        ran (shadow off) — covers zero on one side and cannot pass."""
        return (
            self.gates_clean
            and self.journal_buckets_covered >= min_buckets
            and self.shadow_buckets_covered >= min_buckets
        )

    def to_dict(self) -> dict[str, Any]:
        """Stable JSON shape (keys are contract for the runbook / CI)."""
        return {
            "window": {
                "from_bucket": self.from_bucket,
                "to_bucket": self.to_bucket,
                "bucket_seconds": self.bucket_seconds,
                "buckets_evaluated": self.buckets_evaluated,
                "journal_buckets_covered": self.journal_buckets_covered,
                "shadow_buckets_covered": self.shadow_buckets_covered,
            },
            "exit": {
                "missed": self.missed_count,
                "duplicates": self.duplicate_count,
                "wrong_runs": self.wrong_run_count,
                "identity_mismatches": self.identity_mismatch_count,
                "invalid_outcomes": self.invalid_outcome_count,
                "gates_clean": self.gates_clean,
            },
            "missed": list(self.missed),
            "duplicates": list(self.duplicates),
            "wrong_runs": list(self.wrong_runs),
            "identity_mismatches": list(self.identity_mismatches),
            "invalid_outcomes": list(self.invalid_outcomes),
            "informational": self.informational,
        }


# --------------------------------------------------------------------------- #
# Bucket parsing
# --------------------------------------------------------------------------- #


def parse_job_bucket(row: JobRow) -> int | None:
    """Recover a job's tick bucket from its idempotency key, then payload.

    Keys are ``character_tick:{character_id}:{bucket}`` and
    ``social_tick:{bucket}``. The trailing integer segment is the bucket; the
    ``payload['bucket']`` is a fallback for rows whose key was constructed
    differently. Returns ``None`` when neither yields an integer."""
    key = row.idempotency_key or ""
    if key:
        tail = key.rsplit(":", 1)[-1]
        if tail.isdigit() or (tail.startswith("-") and tail[1:].isdigit()):
            try:
                return int(tail)
            except ValueError:
                pass
    bucket = row.payload.get("bucket") if row.payload else None
    if isinstance(bucket, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(bucket, int):
        return bucket
    return None


# --------------------------------------------------------------------------- #
# Pure comparison
# --------------------------------------------------------------------------- #


def compare_shadow(
    journal_entries: Sequence[JournalEntry],
    job_rows: Sequence[JobRow],
    *,
    bucket_seconds: int,
    window: tuple[int, int],
) -> ComparisonReport:
    """Compare embedded journal vs. shadow queue over ``window`` (inclusive).

    ``window`` is ``(from_bucket, to_bucket)`` inclusive; the gated detectors
    evaluate the OPEN interval ``(from_bucket, to_bucket)`` so the two boundary
    buckets are grace-excluded. Matching runs over the FULL range so a
    legitimate pair sitting on a boundary bucket can still satisfy an interior
    one under ±1 tolerance; only UNMATCHED items whose bucket falls inside the
    interval are counted against a gate.

    The heart is a single BIDIRECTIONAL greedy 1:1 matching per series (each
    character, plus social): each journal entry consumes at most one
    ``would_run=true`` done job within ±1; an unmatched journal entry is a
    ``missed`` (漏job) and an unmatched ``would_run=true`` done job is a
    ``wrong_run`` (錯freeze — the shadow ran work the embedded scheduler
    skipped). ``would_run=false`` correct skips never match and never count as
    extra; ``identity_mismatch`` (H3) and malformed outcomes (H5) are pulled out
    into their own gates rather than silently absorbed as correct skips."""
    from_bucket, to_bucket = window
    if to_bucket < from_bucket:
        raise ValueError("window to_bucket must be >= from_bucket")
    evaluated = set(range(from_bucket + 1, to_bucket))  # open interval
    buckets_evaluated = len(evaluated)

    # -- journal reference over the FULL window (boundary rows are valid refs) #
    journal_char: dict[str, list[int]] = {}
    journal_social: list[int] = []
    for entry in journal_entries:
        if entry.kind == CHARACTER_TICK_KIND and entry.character_id is not None:
            journal_char.setdefault(entry.character_id, []).append(entry.bucket)
        elif entry.kind == SOCIAL_TICK_KIND:
            journal_social.append(entry.bucket)

    # -- classify done jobs (full window) into match candidates + side gates -- #
    run_char: dict[str, list[tuple[int, JobRow]]] = {}
    missing_char: dict[str, list[tuple[int, JobRow]]] = {}
    run_social: list[tuple[int, JobRow]] = []
    invalid_outcomes: list[dict[str, Any]] = []
    identity_mismatches: list[dict[str, Any]] = []
    # key -> parsed buckets for each done row. Keep every parsed bucket so the
    # same-key gate can apply boundary grace per execution, even if malformed
    # data somehow gives one key rows with different payload buckets.
    key_done_buckets: dict[str, list[int | None]] = {}
    # (kind, character_id, bucket) -> set of distinct idempotency keys, done only
    tuple_keys: dict[tuple[str, str | None, int | None], set[str]] = {}

    dead_kinds: Counter[str] = Counter()
    failed_jobs = 0
    total_attempts = 0

    for row in job_rows:
        bucket = parse_job_bucket(row)
        total_attempts += max(row.attempt_count, 0)
        if row.status == _DEAD:
            dead_kinds[row.kind] += 1
        if row.status == _FAILED:
            failed_jobs += 1
        if not row.is_done:
            continue
        key_done_buckets.setdefault(row.idempotency_key, []).append(bucket)
        tuple_keys.setdefault(
            (row.kind, row.character_id, bucket), set(),
        ).add(row.idempotency_key)
        in_scope = bucket is None or bucket in evaluated
        # H5: a done job only PARTICIPATES in matching with a well-formed
        # outcome; a malformed / missing outcome is a hard-gated defect.
        if not row.outcome_valid:
            if in_scope:
                invalid_outcomes.append(_row_ref(row, bucket))
            continue
        # H3: identity_mismatch is a defect gated on its own, never a correct
        # skip and excluded from the wrong-run absorption below.
        if row.is_identity_mismatch:
            if bucket is not None and bucket in evaluated:
                identity_mismatches.append(_row_ref(row, bucket))
            continue
        # would_run=false correct skips are not generally evidence that the
        # journaled tick was mirrored. The missing-character path is different:
        # the coordinator enqueues it for every journal row, and the worker's
        # valid skip outcome is a neutral candidate for that same character.
        if not row.would_run:
            if (
                row.skip_reason == _SKIP_MISSING
                and row.kind == CHARACTER_TICK_KIND
                and row.character_id is not None
                and bucket is not None
            ):
                missing_char.setdefault(row.character_id, []).append((bucket, row))
            continue
        if bucket is None:  # a runnable done job with no recoverable bucket
            continue
        if row.kind == CHARACTER_TICK_KIND and row.character_id is not None:
            run_char.setdefault(row.character_id, []).append((bucket, row))
        elif row.kind == SOCIAL_TICK_KIND:
            run_social.append((bucket, row))

    missed, wrong_runs = _match_all(
        journal_char, journal_social, run_char, run_social, missing_char,
        evaluated,
    )
    duplicates = _detect_duplicates(key_done_buckets, tuple_keys, evaluated)

    # -- split coverage: each side must independently carry work (H5) --------- #
    journal_covered = {
        b
        for buckets in journal_char.values() for b in buckets if b in evaluated
    } | {b for b in journal_social if b in evaluated}
    shadow_covered = {
        parse_job_bucket(row)
        for row in job_rows if row.is_done
    }
    shadow_covered = {b for b in shadow_covered if b is not None and b in evaluated}

    journal_char_sets = {k: set(v) for k, v in journal_char.items()}
    informational = _informational(
        journal_char=journal_char_sets,
        journal_social=set(journal_social),
        done_char_buckets={
            cid: {b for b, _ in rows} for cid, rows in run_char.items()
        },
        done_social_buckets={b for b, _ in run_social},
        evaluated=evaluated,
        dead_kinds=dead_kinds,
        failed_jobs=failed_jobs,
        total_attempts=total_attempts,
        job_rows=job_rows,
    )

    return ComparisonReport(
        from_bucket=from_bucket,
        to_bucket=to_bucket,
        bucket_seconds=bucket_seconds,
        buckets_evaluated=buckets_evaluated,
        journal_buckets_covered=len(journal_covered),
        shadow_buckets_covered=len(shadow_covered),
        missed=tuple(missed),
        duplicates=tuple(duplicates),
        wrong_runs=tuple(wrong_runs),
        identity_mismatches=tuple(identity_mismatches),
        invalid_outcomes=tuple(invalid_outcomes),
        informational=informational,
    )


def _row_ref(row: JobRow, bucket: int | None) -> dict[str, Any]:
    """Compact offender reference shared by the identity / invalid gates."""
    return {
        "kind": row.kind,
        "character_id": row.character_id,
        "tenant_id": row.tenant_id,
        "operator_id": row.operator_id,
        "bucket": bucket,
        "idempotency_key": row.idempotency_key,
    }


def _greedy_unmatched(
    primary: list[tuple[int, Any]], secondary_buckets: list[int],
) -> list[tuple[int, Any]]:
    """Greedy 1:1 consume of each ``primary`` item (``(bucket, payload)``)
    against an available ``secondary`` bucket within ±1; return the primary
    items left unmatched.

    Processing primaries ascending and always taking the smallest feasible
    secondary ({b-1, b, b+1}) is the optimal assignment for this unit-interval
    structure, so a genuine ±1 clock-edge skew is tolerated and no false
    unmatched item is produced when a valid 1:1 assignment exists."""
    available: dict[int, int] = {}
    for bucket in secondary_buckets:
        available[bucket] = available.get(bucket, 0) + 1
    unmatched: list[tuple[int, Any]] = []
    for bucket, payload in sorted(primary, key=lambda item: item[0]):
        pick = next(
            (b for b in (bucket - 1, bucket, bucket + 1) if available.get(b)),
            None,
        )
        if pick is None:
            unmatched.append((bucket, payload))
        else:
            available[pick] -= 1
    return unmatched


def _match_series(
    journal_buckets: list[int],
    done_rows: list[tuple[int, JobRow]],
    evaluated: set[int],
) -> tuple[list[int], list[tuple[int, JobRow]]]:
    """Bidirectional 1:1 match of executable jobs in one series.

    Two consuming 1:1 passes with boundary grace applied per direction:

    * **missed** — INTERIOR journal buckets consumed against ALL
      ``would_run=true`` done buckets (a boundary done job may satisfy an
      interior journal entry). Unmatched interior journal → missed.
    * **extra** — INTERIOR done rows consumed against ALL journal buckets (a
      boundary journal entry may satisfy an interior done job). Unmatched
      interior done → wrong_run.

    Missing-character skips are deliberately handled after this executable
    pass by :func:`_match_neutral`; this ordering makes a real execution win
    over a neutral skip when both could satisfy one journal entry.
    """
    done_all_buckets = [bucket for bucket, _ in done_rows]
    interior_journal = [(b, b) for b in journal_buckets if b in evaluated]
    missed = [
        bucket for bucket, _ in _greedy_unmatched(
            interior_journal, done_all_buckets,
        )
    ]
    interior_done = [(b, row) for b, row in done_rows if b in evaluated]
    extra = _greedy_unmatched(interior_done, list(journal_buckets))
    return missed, extra


def _match_neutral(
    missed_journal: list[int],
    neutral_rows: list[tuple[int, JobRow]],
) -> list[int]:
    """Consume missed journal entries with neutral missing-character skips.

    A neutral skip can cover one same-character journal entry within ±1, but
    an unmatched skip is intentionally discarded: it is neither an execution
    nor a wrong-run candidate. The caller has already scoped ``missed_journal``
    to interior buckets, while neutral rows are allowed to include boundaries
    for the same grace behavior as executable jobs.
    """
    journal_items = [(bucket, bucket) for bucket in missed_journal]
    neutral_buckets = [bucket for bucket, _ in neutral_rows]
    return [
        bucket for bucket, _ in _greedy_unmatched(
            journal_items, neutral_buckets,
        )
    ]


def _match_all(
    journal_char: dict[str, list[int]],
    journal_social: list[int],
    run_char: dict[str, list[tuple[int, JobRow]]],
    run_social: list[tuple[int, JobRow]],
    missing_char: dict[str, list[tuple[int, JobRow]]],
    evaluated: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match executable work, then neutral missing-character skips.

    The executable pass is intentionally first and consuming. Only journal
    entries left missed by that pass are offered to neutral missing skips;
    therefore execution wins deterministically, while an unmatched neutral skip
    remains invisible only to wrong-run. Duplicate detection independently
    indexes every done row, including valid missing-character skips.
    """
    missed: list[dict[str, Any]] = []
    wrong_runs: list[dict[str, Any]] = []

    for character_id in sorted(
        set(journal_char) | set(run_char) | set(missing_char),
    ):
        um_journal, um_rows = _match_series(
            journal_char.get(character_id, []),
            run_char.get(character_id, []),
            evaluated,
        )
        um_journal = _match_neutral(
            um_journal, missing_char.get(character_id, []),
        )
        missed.extend(
            {
                "kind": CHARACTER_TICK_KIND,
                "character_id": character_id,
                "bucket": bucket,
            }
            for bucket in um_journal
        )
        wrong_runs.extend(
            {
                "kind": CHARACTER_TICK_KIND,
                "character_id": character_id,
                "bucket": bucket,
                "idempotency_key": row.idempotency_key,
            }
            for bucket, row in um_rows
        )

    um_journal, um_rows = _match_series(journal_social, run_social, evaluated)
    missed.extend(
        {"kind": SOCIAL_TICK_KIND, "character_id": None, "bucket": bucket}
        for bucket in um_journal
    )
    wrong_runs.extend(
        {
            "kind": SOCIAL_TICK_KIND,
            "character_id": None,
            "bucket": bucket,
            "idempotency_key": row.idempotency_key,
        }
        for bucket, row in um_rows
    )
    return missed, wrong_runs


def _detect_duplicates(
    key_done_buckets: dict[str, list[int | None]],
    tuple_keys: dict[tuple[str, str | None, int | None], set[str]],
    evaluated: set[int],
) -> list[dict[str, Any]]:
    """Two flavours of a logically-duplicated completion:

    (i)  the SAME idempotency key completed ``done`` more than once — count it
         only when at least two of that key's executions are in evaluated
         interior buckets. Boundary rows must not combine with one interior row
         to manufacture a duplicate, and this remains deterministic even if
         malformed rows for one key parse to different buckets;
    (ii) the same ``(kind, character_id, bucket)`` tuple completed under two
         DIFFERENT keys — belt-and-braces for a key-construction drift that the
         active-idempotency index cannot catch.
    """
    duplicates: list[dict[str, Any]] = []
    for key, buckets in sorted(key_done_buckets.items()):
        interior_buckets = [bucket for bucket in buckets if bucket in evaluated]
        if len(interior_buckets) >= 2:
            duplicates.append({
                "type": "same_key",
                "idempotency_key": key,
                "done_count": len(buckets),
            })
    for (kind, character_id, bucket), keys in sorted(
        tuple_keys.items(), key=lambda kv: (kv[0][0], kv[0][2] or 0, kv[0][1] or ""),
    ):
        if bucket is not None and bucket not in evaluated:
            continue
        if len(keys) > 1:
            duplicates.append({
                "type": "same_tuple",
                "kind": kind,
                "character_id": character_id,
                "bucket": bucket,
                "keys": sorted(keys),
            })
    return duplicates


def _informational(
    *,
    journal_char: dict[str, set[int]],
    journal_social: set[int],
    done_char_buckets: dict[str, set[int]],
    done_social_buckets: set[int],
    evaluated: set[int],
    dead_kinds: Counter[str],
    failed_jobs: int,
    total_attempts: int,
    job_rows: Sequence[JobRow],
) -> dict[str, Any]:
    """Non-gating diagnostics: liveness, coverage, and a clock-skew histogram
    of ``B_job - B_journal`` for matched character-tick pairs."""
    journal_char_entries = sum(
        1 for buckets in journal_char.values() for b in buckets if b in evaluated
    )
    done_char_jobs = sum(
        1 for buckets in done_char_buckets.values() for b in buckets if b in evaluated
    )
    journal_social_entries = sum(1 for b in journal_social if b in evaluated)
    done_social_jobs = sum(1 for b in done_social_buckets if b in evaluated)

    # Clock-skew: for each done character-tick job, find the nearest journal
    # bucket for the same character within ±1 and record the signed delta.
    skew: Counter[int] = Counter()
    for row in job_rows:
        if row.kind != CHARACTER_TICK_KIND or not row.is_done:
            continue
        if row.character_id is None:
            continue
        bucket = parse_job_bucket(row)
        if bucket is None:
            continue
        candidates = journal_char.get(row.character_id, set())
        for delta in (0, -1, 1):
            if (bucket + delta) in candidates:
                skew[-delta] += 1  # B_job - B_journal = -delta
                break

    return {
        "dead_jobs": int(sum(dead_kinds.values())),
        "dead_kinds": dict(sorted(dead_kinds.items())),
        "failed_jobs": failed_jobs,
        "total_attempts": total_attempts,
        "clock_skew_job_minus_journal": {
            str(k): v for k, v in sorted(skew.items())
        },
        "coverage": {
            "buckets_evaluated": len(evaluated),
            "journal_char_entries": journal_char_entries,
            "done_char_jobs": done_char_jobs,
            "journal_social_entries": journal_social_entries,
            "done_social_jobs": done_social_jobs,
            "distinct_characters_journaled": len(journal_char),
        },
    }


# --------------------------------------------------------------------------- #
# Async DB loader (thin)
# --------------------------------------------------------------------------- #


def timestamp_to_bucket(ts: datetime, bucket_seconds: int) -> int:
    return int(ts.timestamp()) // bucket_seconds


async def load_soak_rows(
    *,
    database_url: str,
    from_ts: datetime,
    to_ts: datetime,
    bucket_seconds: int,
) -> tuple[list[JournalEntry], list[JobRow], tuple[int, int]]:
    """Read journal + queue rows for the window and project to value objects.

    Returns ``(journal_entries, job_rows, (from_bucket, to_bucket))``. Jobs are
    pulled with a ±1-bucket grace on ``due_at`` so a matched pair one bucket
    across an edge is still available to the comparison."""
    from sqlalchemy import select

    from kokoro_link.contracts.clock import ensure_utc
    from kokoro_link.infrastructure.persistence.engine import (
        build_async_engine,
        build_session_factory,
    )
    from kokoro_link.infrastructure.persistence.models import (
        BackgroundJobRow,
        SchedulerTickJournalRow,
    )

    from_ts = ensure_utc(from_ts)
    to_ts = ensure_utc(to_ts)
    from_bucket = timestamp_to_bucket(from_ts, bucket_seconds)
    to_bucket = timestamp_to_bucket(to_ts, bucket_seconds)
    grace = bucket_seconds

    engine = build_async_engine(database_url)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            journal_rows = (await session.execute(
                select(SchedulerTickJournalRow).where(
                    SchedulerTickJournalRow.tick_bucket >= from_bucket,
                    SchedulerTickJournalRow.tick_bucket <= to_bucket,
                ),
            )).scalars().all()
            journal_entries = [
                JournalEntry(
                    bucket=row.tick_bucket,
                    kind=row.kind,
                    character_id=row.character_id,
                    recorded_at=ensure_utc(row.recorded_at),
                )
                for row in journal_rows
            ]

            from_due = datetime.fromtimestamp(
                (from_bucket * bucket_seconds) - grace, tz=timezone.utc,
            )
            to_due = datetime.fromtimestamp(
                (to_bucket * bucket_seconds) + grace, tz=timezone.utc,
            )
            job_db_rows = (await session.execute(
                select(BackgroundJobRow).where(
                    BackgroundJobRow.kind.in_(
                        (CHARACTER_TICK_KIND, SOCIAL_TICK_KIND),
                    ),
                    BackgroundJobRow.due_at >= from_due,
                    BackgroundJobRow.due_at <= to_due,
                ),
            )).scalars().all()
            job_rows = [
                JobRow(
                    kind=row.kind,
                    status=row.status,
                    idempotency_key=row.idempotency_key,
                    character_id=row.character_id,
                    tenant_id=row.tenant_id,
                    operator_id=row.operator_id,
                    payload=_load_json(row.payload_json),
                    outcome=_load_json(row.outcome_json) if row.outcome_json else None,
                    attempt_count=row.attempt_count,
                )
                for row in job_db_rows
            ]
    finally:
        await engine.dispose()

    return journal_entries, job_rows, (from_bucket, to_bucket)


def _load_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_timestamp(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def render_summary(report: ComparisonReport, *, min_buckets: int) -> str:
    passed = report.passes(min_buckets=min_buckets)
    info = report.informational
    cov = info["coverage"]
    lines = [
        "Phase 2 synthetic-soak shadow comparison",
        f"  window buckets : {report.from_bucket}..{report.to_bucket} "
        f"({report.bucket_seconds}s each)",
        f"  evaluated      : {report.buckets_evaluated} buckets "
        f"(window width)",
        f"  covered        : journal={report.journal_buckets_covered} "
        f"shadow={report.shadow_buckets_covered} buckets with work "
        f"(each min required {min_buckets})",
        f"  missed (漏job)         : {report.missed_count}",
        f"  duplicates (重複)      : {report.duplicate_count}",
        f"  wrong-run (錯freeze)   : {report.wrong_run_count}",
        f"  identity-mismatch (錯tenant): {report.identity_mismatch_count}",
        f"  invalid-outcome        : {report.invalid_outcome_count}",
        f"  dead jobs              : {info['dead_jobs']} {info['dead_kinds'] or ''}",
        f"  failed jobs            : {info['failed_jobs']}",
        f"  coverage               : char journal={cov['journal_char_entries']} "
        f"done={cov['done_char_jobs']} / social journal="
        f"{cov['journal_social_entries']} done={cov['done_social_jobs']}",
        f"  clock skew (job-journal): "
        f"{info['clock_skew_job_minus_journal'] or '{}'}",
        f"  RESULT                 : {'PASS' if passed else 'FAIL'}",
    ]
    if not passed and report.gates_clean:
        lines.append(
            "  (gates clean but too few buckets carried work on one/both sides "
            "— an empty/idle/short/one-sided window is not a pass)",
        )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    database_url = _resolve_database_url(args)
    if database_url is None:
        return 2
    journal_entries, job_rows, window = await load_soak_rows(
        database_url=database_url,
        from_ts=args.from_ts,
        to_ts=args.to_ts,
        bucket_seconds=args.bucket_seconds,
    )
    report = compare_shadow(
        journal_entries, job_rows,
        bucket_seconds=args.bucket_seconds,
        window=window,
    )
    payload = report.to_dict()
    payload["exit"]["min_buckets"] = args.min_buckets
    payload["exit"]["passed"] = report.passes(min_buckets=args.min_buckets)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
    print(serialized)
    print(render_summary(report, min_buckets=args.min_buckets), file=sys.stderr)
    return 0 if report.passes(min_buckets=args.min_buckets) else 1


def _resolve_database_url(args: argparse.Namespace) -> str | None:
    import os

    if args.database_url:
        return args.database_url
    if not args.i_know_this_is_a_soak_environment:
        print(
            "refusing to read DATABASE_URL from the ambient environment "
            "without an explicit opt-in; pass --database-url or "
            "--i-know-this-is-a-soak-environment to target a soak database.",
            file=sys.stderr,
        )
        return None
    env_url = os.getenv("DATABASE_URL", os.getenv("KOKORO_DATABASE_URL", ""))
    if not env_url:
        print("no DATABASE_URL / KOKORO_DATABASE_URL set.", file=sys.stderr)
        return None
    return env_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the embedded scheduler tick journal against the shadow "
            "queue over a soak window and judge the Phase 2 exit criteria."
        ),
    )
    parser.add_argument(
        "--from", dest="from_ts", type=_parse_timestamp, required=True,
        help="Window start (ISO 8601, e.g. 2026-07-20T00:00:00Z).",
    )
    parser.add_argument(
        "--to", dest="to_ts", type=_parse_timestamp, required=True,
        help="Window end (ISO 8601).",
    )
    parser.add_argument(
        "--bucket-seconds", type=int, default=_DEFAULT_BUCKET_SECONDS,
        help="Tick bucket width; must match the coordinator (default 300).",
    )
    parser.add_argument(
        "--min-buckets", type=int, default=_DEFAULT_MIN_BUCKETS,
        help=(
            "Minimum interior buckets that must CARRY observed work (journal "
            "or done job) to pass; an empty/idle window covers none and never "
            f"passes (default {_DEFAULT_MIN_BUCKETS}, ~24h minus grace)."
        ),
    )
    parser.add_argument(
        "--database-url", default="",
        help="Soak DATABASE_URL. Explicitly passing it is the prod-safety guard.",
    )
    parser.add_argument(
        "--i-know-this-is-a-soak-environment", action="store_true",
        help=(
            "Opt-in to read DATABASE_URL from the environment. Required when "
            "--database-url is omitted so a prod URL is never picked up by "
            "accident."
        ),
    )
    parser.add_argument(
        "--out", default="",
        help="Also write the JSON report to this path.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.bucket_seconds < 1:
        parser.error("--bucket-seconds must be >= 1")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
