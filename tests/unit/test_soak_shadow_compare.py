"""Unit tests for the Phase 2 synthetic-soak comparison judge (pure core).

Exercises :func:`compare_shadow` over synthetic fixtures with no database: the
three exit gates (missed / duplicate / wrong-run), ±1 tolerance, boundary
grace, the min-buckets floor, and the stable JSON report shape.
"""

from __future__ import annotations

from scripts.soak_shadow_compare import (
    CHARACTER_TICK_KIND,
    SOCIAL_TICK_KIND,
    JobRow,
    JournalEntry,
    compare_shadow,
    parse_job_bucket,
)


_UNSET = object()


def _char_entry(bucket: int, character_id: str) -> JournalEntry:
    return JournalEntry(
        bucket=bucket, kind=CHARACTER_TICK_KIND, character_id=character_id,
    )


def _social_entry(bucket: int) -> JournalEntry:
    return JournalEntry(bucket=bucket, kind=SOCIAL_TICK_KIND, character_id=None)


def _char_job(
    bucket: int,
    character_id: str,
    *,
    status: str = "done",
    would_run: bool = True,
    skip_reason: str | None = None,
    outcome: object = _UNSET,
    key: str | None = None,
) -> JobRow:
    # A would_run=false skip MUST carry a documented skip_reason (H5); default a
    # valid one so fixtures stay well-formed unless they intentionally malform.
    if would_run is False and skip_reason is None:
        skip_reason = "frozen"
    resolved_outcome = (
        {"dry_run": True, "would_run": would_run, "skip_reason": skip_reason}
        if outcome is _UNSET else outcome
    )
    return JobRow(
        kind=CHARACTER_TICK_KIND,
        status=status,
        idempotency_key=key or f"{CHARACTER_TICK_KIND}:{character_id}:{bucket}",
        character_id=character_id,
        payload={"character_id": character_id, "bucket": bucket},
        outcome=resolved_outcome,
    )


def _social_job(bucket: int, *, status: str = "done") -> JobRow:
    return JobRow(
        kind=SOCIAL_TICK_KIND,
        status=status,
        idempotency_key=f"{SOCIAL_TICK_KIND}:{bucket}",
        character_id=None,
        payload={"bucket": bucket},
        outcome={"dry_run": True, "would_run": True, "skip_reason": None},
    )


def _full_window(chars: tuple[str, ...], buckets: range):
    journal = []
    jobs = []
    for b in buckets:
        journal.append(_social_entry(b))
        jobs.append(_social_job(b))
        for c in chars:
            journal.append(_char_entry(b, c))
            jobs.append(_char_job(b, c))
    return journal, jobs


def test_happy_path_all_zero() -> None:
    journal, jobs = _full_window(("A", "B"), range(0, 6))
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 0
    assert report.duplicate_count == 0
    assert report.wrong_run_count == 0
    assert report.buckets_evaluated == 4  # buckets 1..4 (0 and 5 grace-excluded)
    assert report.gates_clean is True
    assert report.passes(min_buckets=4) is True


def test_one_missed_char_tick() -> None:
    # Char B fills every bucket for healthy coverage. Char A is journaled at a
    # SINGLE bucket (3) and its lone job is dropped — with no neighbour job
    # within ±1, that is a genuine miss (a single dropped job amid consecutive
    # buckets is tolerated by design, so the miss must be isolated).
    journal, jobs = _full_window(("B",), range(0, 6))
    journal.append(_char_entry(3, "A"))
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 1
    assert report.missed[0]["character_id"] == "A"
    assert report.missed[0]["bucket"] == 3
    assert report.passes(min_buckets=1) is False


def test_boundary_bucket_excluded() -> None:
    # A journal entry with no matching job on the FIRST and LAST bucket must
    # not count as missed (boundary grace).
    journal, jobs = _full_window(("A",), range(1, 5))  # jobs only 1..4
    journal.append(_char_entry(0, "A"))  # boundary, no job
    journal.append(_char_entry(5, "A"))  # boundary, no job
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 0


def test_plus_minus_one_tolerance_accepted() -> None:
    journal = [_char_entry(2, "A")]
    # Job lands one bucket ahead (B+1) — a clock-edge skew, still a match.
    jobs = [_char_job(3, "A", key=f"{CHARACTER_TICK_KIND}:A:3")]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 0
    # And the skew histogram records B_job - B_journal = +1.
    assert report.informational["clock_skew_job_minus_journal"] == {"1": 1}


def test_done_duplicate_same_key() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [
        _char_job(2, "A", key="character_tick:A:2"),
        _char_job(2, "A", key="character_tick:A:2"),  # second done, same key
    ]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.duplicate_count == 1
    assert report.duplicates[0]["type"] == "same_key"
    assert report.duplicates[0]["done_count"] == 2


def test_done_duplicate_same_key_excludes_boundary_only_rows() -> None:
    jobs = [
        _char_job(0, "A", key="shared-key"),
        _char_job(5, "A", key="shared-key"),
    ]
    report = compare_shadow([], jobs, bucket_seconds=300, window=(0, 5))
    assert report.duplicate_count == 0


def test_done_duplicate_same_key_boundary_plus_one_interior_is_excluded() -> None:
    jobs = [
        _char_job(0, "A", key="shared-key"),
        _char_job(2, "A", key="shared-key"),
    ]
    report = compare_shadow([], jobs, bucket_seconds=300, window=(0, 5))
    assert report.duplicate_count == 0


def test_done_duplicate_same_key_counts_two_interior_rows() -> None:
    # A key without a numeric suffix exercises the deterministic rule for the
    # malformed case where one key's rows recover buckets from payload instead.
    jobs = [
        _char_job(2, "A", key="shared-key"),
        _char_job(3, "A", key="shared-key"),
    ]
    report = compare_shadow([], jobs, bucket_seconds=300, window=(0, 5))
    assert report.duplicate_count == 1
    assert report.duplicates[0]["type"] == "same_key"
    assert report.duplicates[0]["done_count"] == 2


def test_done_duplicate_same_tuple_different_keys() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [
        _char_job(2, "A", key="character_tick:A:2"),
        _char_job(2, "A", key="character_tick:A:2:retry"),  # different key
    ]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    types = {d["type"] for d in report.duplicates}
    assert "same_tuple" in types
    tuple_dupe = next(d for d in report.duplicates if d["type"] == "same_tuple")
    assert tuple_dupe["character_id"] == "A"
    assert tuple_dupe["bucket"] == 2


def test_wrong_run_when_no_journal_entry() -> None:
    # Job completed would_run=true for A@2 but the embedded scheduler never
    # journaled A there → the shadow would have run work embedded skipped.
    journal: list[JournalEntry] = [_char_entry(2, "B")]  # only B, not A
    jobs = [_char_job(2, "A", would_run=True)]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.wrong_run_count == 1
    assert report.wrong_runs[0]["character_id"] == "A"
    assert report.wrong_runs[0]["bucket"] == 2


def test_would_run_false_not_counted_as_wrong_run() -> None:
    # A frozen/subscription-locked skip: would_run=false is correct behaviour.
    journal: list[JournalEntry] = []  # no journal entry for A at all
    jobs = [_char_job(2, "A", would_run=False)]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.wrong_run_count == 0


def test_missing_character_skip_consumes_same_journal_bucket() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [_char_job(2, "A", would_run=False, skip_reason="missing_character")]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 0
    assert report.duplicate_count == 0
    assert report.wrong_run_count == 0
    assert report.invalid_outcome_count == 0


def test_two_missing_character_skips_with_same_key_are_duplicate() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [
        _char_job(
            2, "A", would_run=False, skip_reason="missing_character",
            key="missing:A:2",
        ),
        _char_job(
            2, "A", would_run=False, skip_reason="missing_character",
            key="missing:A:2",
        ),
    ]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.duplicate_count == 1
    assert report.duplicates[0]["type"] == "same_key"
    assert report.duplicates[0]["done_count"] == 2
    assert report.wrong_run_count == 0


def test_two_missing_character_skips_with_different_keys_same_tuple_are_duplicate() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [
        _char_job(
            2, "A", would_run=False, skip_reason="missing_character",
            key="missing:A:2:first",
        ),
        _char_job(
            2, "A", would_run=False, skip_reason="missing_character",
            key="missing:A:2:second",
        ),
    ]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.duplicate_count == 1
    assert report.duplicates[0]["type"] == "same_tuple"
    assert report.duplicates[0]["keys"] == [
        "missing:A:2:first", "missing:A:2:second",
    ]
    assert report.wrong_run_count == 0


def test_missing_character_same_key_obeys_boundary_grace() -> None:
    jobs = [
        _char_job(
            0, "A", would_run=False, skip_reason="missing_character",
            key="shared-missing-key",
        ),
        _char_job(
            2, "A", would_run=False, skip_reason="missing_character",
            key="shared-missing-key",
        ),
    ]
    report = compare_shadow([], jobs, bucket_seconds=300, window=(0, 5))
    assert report.duplicate_count == 0
    assert report.wrong_run_count == 0


def test_missing_character_skip_consumes_plus_minus_one_bucket() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [_char_job(3, "A", would_run=False, skip_reason="missing_character")]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 0
    assert report.wrong_run_count == 0


def test_missing_character_skip_without_bucket_cannot_match() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [JobRow(
        kind=CHARACTER_TICK_KIND,
        status="done",
        idempotency_key="",
        character_id="A",
        outcome={
            "dry_run": True,
            "would_run": False,
            "skip_reason": "missing_character",
        },
    )]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 1
    assert report.wrong_run_count == 0
    assert report.invalid_outcome_count == 0


def test_frozen_and_subscription_locked_skips_do_not_consume_journal() -> None:
    journal = [_char_entry(2, "A"), _char_entry(3, "A")]
    jobs = [
        _char_job(2, "A", would_run=False, skip_reason="frozen"),
        _char_job(3, "A", would_run=False, skip_reason="subscription_locked"),
    ]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 2
    assert report.wrong_run_count == 0


def test_missing_character_skip_does_not_absorb_identity_mismatch_gate() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [_char_job(2, "A", would_run=False, skip_reason="identity_mismatch")]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.identity_mismatch_count == 1
    assert report.missed_count == 1
    assert report.gates_clean is False


def test_execution_and_missing_character_skip_same_tuple_are_duplicate() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [
        _char_job(2, "A", would_run=True, key="run:A:2"),
        _char_job(
            2, "A", would_run=False, skip_reason="missing_character",
            key="skip:A:2",
        ),
    ]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 0
    assert report.wrong_run_count == 0
    assert report.duplicate_count == 1
    assert report.duplicates[0]["type"] == "same_tuple"
    assert report.invalid_outcome_count == 0


def test_social_tick_missed() -> None:
    journal = [_social_entry(2)]
    jobs: list[JobRow] = []  # no social job at all
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 1
    assert report.missed[0]["kind"] == SOCIAL_TICK_KIND
    assert report.missed[0]["character_id"] is None


def test_empty_window_fails_via_min_buckets() -> None:
    # A window with no interior buckets evaluates nothing; gates are trivially
    # clean but the run must NOT pass (silence is not success).
    report = compare_shadow([], [], bucket_seconds=300, window=(0, 1))
    assert report.buckets_evaluated == 0
    assert report.journal_buckets_covered == 0
    assert report.shadow_buckets_covered == 0
    assert report.gates_clean is True
    assert report.passes(min_buckets=1) is False
    assert report.passes(min_buckets=0) is True  # explicit zero-floor opt-in


def test_empty_data_over_wide_window_does_not_pass() -> None:
    # The regression the min-buckets floor must catch: a NOMINAL-width window
    # (here 300 interior buckets) over an EMPTY database. Gates are trivially
    # clean and buckets_evaluated is huge, but nothing was actually observed —
    # coverage gating must keep this from certifying green.
    report = compare_shadow([], [], bucket_seconds=300, window=(0, 301))
    assert report.buckets_evaluated == 300  # window width alone
    assert report.journal_buckets_covered == 0  # but no bucket carried work
    assert report.shadow_buckets_covered == 0
    assert report.gates_clean is True
    assert report.passes(min_buckets=286) is False


def test_isolated_single_drop_amid_consecutive_buckets_is_missed() -> None:
    # Char A ticks every bucket 1..4 and completes a job in each EXCEPT bucket 3
    # (a single intermittent loss, with healthy neighbours at 2 and 4). The
    # aggregate-membership tolerance used to mask this; consuming 1:1 matching
    # must surface it as exactly one miss for A.
    journal = [_char_entry(b, "A") for b in range(1, 5)]
    jobs = [_char_job(b, "A") for b in range(1, 5) if b != 3]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    a_missed = [m for m in report.missed if m["character_id"] == "A"]
    assert len(a_missed) == 1
    assert report.passes(min_buckets=1) is False


def test_dead_and_failed_are_informational_only() -> None:
    journal = [_char_entry(2, "A")]
    jobs = [
        _char_job(2, "A"),
        _char_job(2, "A", status="dead", key="character_tick:A:2:dead"),
        _char_job(2, "A", status="failed", key="character_tick:A:2:failed"),
    ]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    # dead/failed rows are not "done", so they gate nothing.
    assert report.missed_count == 0
    assert report.wrong_run_count == 0
    assert report.informational["dead_jobs"] == 1
    assert report.informational["dead_kinds"] == {CHARACTER_TICK_KIND: 1}
    assert report.informational["failed_jobs"] == 1


def test_report_json_shape_stable() -> None:
    journal, jobs = _full_window(("A",), range(0, 6))
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    payload = report.to_dict()
    assert set(payload) == {
        "window", "exit", "missed", "duplicates", "wrong_runs",
        "identity_mismatches", "invalid_outcomes", "informational",
    }
    assert set(payload["window"]) == {
        "from_bucket", "to_bucket", "bucket_seconds", "buckets_evaluated",
        "journal_buckets_covered", "shadow_buckets_covered",
    }
    assert set(payload["exit"]) == {
        "missed", "duplicates", "wrong_runs", "identity_mismatches",
        "invalid_outcomes", "gates_clean",
    }
    assert set(payload["informational"]) == {
        "dead_jobs", "dead_kinds", "failed_jobs", "total_attempts",
        "clock_skew_job_minus_journal", "coverage",
    }
    assert set(payload["informational"]["coverage"]) == {
        "buckets_evaluated", "journal_char_entries", "done_char_jobs",
        "journal_social_entries", "done_social_jobs",
        "distinct_characters_journaled",
    }


def test_extra_done_job_is_wrong_run_not_absorbed() -> None:
    # H4 Codex repro: journal A@2, A@3; done A@2, A@3, A@4. The bidirectional
    # 1:1 match consumes A@2↔A@2 and A@3↔A@3, leaving the EXTRA A@4 unmatched —
    # a wrong_run. The old directional pass let A@4 be absorbed by A@3's journal
    # entry under ±1 tolerance (no reverse check), hiding the extra.
    journal = [_char_entry(2, "A"), _char_entry(3, "A")]
    jobs = [_char_job(2, "A"), _char_job(3, "A"), _char_job(4, "A")]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 0
    assert report.wrong_run_count == 1
    assert report.wrong_runs[0]["character_id"] == "A"
    assert report.wrong_runs[0]["bucket"] == 4
    assert report.passes(min_buckets=1) is False


def test_social_extra_is_wrong_run() -> None:
    # H4: the social series now has the reverse check too. Journal social@2;
    # done social@2 + an extra social@3 with no journal partner → wrong_run.
    journal = [_social_entry(2)]
    jobs = [_social_job(2), _social_job(3)]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.missed_count == 0
    assert report.wrong_run_count == 1
    assert report.wrong_runs[0]["kind"] == SOCIAL_TICK_KIND
    assert report.wrong_runs[0]["bucket"] == 3


def test_invalid_outcome_none_is_gated() -> None:
    # H5 Codex repro: a done job with outcome=None cannot count as a silent
    # success — it is a hard-gated invalid_outcome and never participates in
    # matching (so it neither covers a journal entry nor counts as extra).
    journal = [_char_entry(2, "A")]
    jobs = [_char_job(2, "A", outcome=None)]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.invalid_outcome_count == 1
    assert report.invalid_outcomes[0]["character_id"] == "A"
    assert report.invalid_outcomes[0]["bucket"] == 2
    # The journal entry has no VALID job to match → also a missed (both fire).
    assert report.missed_count == 1
    assert report.gates_clean is False
    assert report.passes(min_buckets=1) is False


def test_would_run_false_bad_skip_reason_is_invalid() -> None:
    # A would_run=false skip with an off-allowlist reason is malformed (H5).
    journal: list[JournalEntry] = []
    jobs = [_char_job(2, "A", would_run=False, skip_reason="because_i_said_so")]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.invalid_outcome_count == 1
    assert report.gates_clean is False


def test_identity_mismatch_is_gated_and_not_a_correct_skip() -> None:
    # H3(c): a done identity_mismatch skip (would_run=false) is a DEFECT, gated
    # as its own count — not silently absorbed as a correct skip.
    journal = [_char_entry(2, "A")]
    jobs = [_char_job(2, "A", would_run=False, skip_reason="identity_mismatch")]
    report = compare_shadow(journal, jobs, bucket_seconds=300, window=(0, 5))
    assert report.identity_mismatch_count == 1
    assert report.identity_mismatches[0]["character_id"] == "A"
    assert report.invalid_outcome_count == 0  # identity_mismatch parses fine
    assert report.gates_clean is False
    assert report.passes(min_buckets=1) is False


def test_shadow_only_window_fails_journal_coverage() -> None:
    # H5(b) split coverage isolated: a window covered on the SHADOW side with
    # only CORRECT skips (would_run=false, valid reason → no wrong_run, no
    # invalid) but NO journal entries. Gates are genuinely clean, yet the run
    # must FAIL because the journal side covered zero buckets.
    jobs = [_char_job(b, "A", would_run=False, skip_reason="frozen")
            for b in range(0, 6)]
    report = compare_shadow([], jobs, bucket_seconds=300, window=(0, 5))
    assert report.shadow_buckets_covered == 4   # buckets 1..4 carried done jobs
    assert report.journal_buckets_covered == 0  # nothing on the journal side
    assert report.gates_clean is True           # skips are correct, gates clean
    assert report.passes(min_buckets=4) is False


def test_shadow_only_wouldrun_true_is_wrong_run() -> None:
    # And the loud variant: shadow ran would_run=true work with no journal at
    # all → the reverse 1:1 check flags every interior job as a wrong_run.
    jobs = [_social_job(b) for b in range(0, 6)]
    report = compare_shadow([], jobs, bucket_seconds=300, window=(0, 5))
    assert report.wrong_run_count == 4  # buckets 1..4
    assert report.passes(min_buckets=1) is False


def test_journal_only_window_fails_shadow_coverage() -> None:
    # The mirror image: embedded journaled but the shadow never ran (shadow off)
    # → shadow coverage 0 → FAIL. Missed fires too, but the coverage split alone
    # already denies the pass.
    journal = [_social_entry(b) for b in range(0, 6)]
    report = compare_shadow(journal, [], bucket_seconds=300, window=(0, 5))
    assert report.journal_buckets_covered == 4
    assert report.shadow_buckets_covered == 0
    assert report.passes(min_buckets=4) is False


def test_parse_job_bucket_from_key_and_payload() -> None:
    assert parse_job_bucket(
        JobRow(kind=CHARACTER_TICK_KIND, status="done",
               idempotency_key="character_tick:abc:42", character_id="abc"),
    ) == 42
    assert parse_job_bucket(
        JobRow(kind=SOCIAL_TICK_KIND, status="done",
               idempotency_key="social_tick:7"),
    ) == 7
    # Fallback to payload when the key has no trailing integer.
    assert parse_job_bucket(
        JobRow(kind=CHARACTER_TICK_KIND, status="done",
               idempotency_key="weird-key", payload={"bucket": 9}),
    ) == 9
    # Unparseable → None.
    assert parse_job_bucket(
        JobRow(kind=CHARACTER_TICK_KIND, status="done", idempotency_key=""),
    ) is None
