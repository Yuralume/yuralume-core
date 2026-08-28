"""QG0 counters and their Prometheus rendering.

Two properties carry all the weight here. **Labels, not fields**: the
surface dimension is open-ended, so a wave-3 ticket that adopts a new
surface must appear on the scrape without editing the renderer. And **the
scrape never breaks**: every reader is defensive, because these series ride
the same endpoint as the drain gauge that ``deploy.sh`` polls — a counter
bug that 500s the scrape would stall a rolling deploy.
"""

from __future__ import annotations

from kokoro_link.application.services.output_quality import (
    DEFAULT_FAILOPEN_ALARM_STREAK,
    OUTCOME_GATE_ERROR_FAILOPEN,
    OUTCOME_HARD_DEGRADED,
    OUTCOME_HARD_SKIPPED,
    OUTCOME_PASS,
    OutputQualityCounters,
)
from kokoro_link.infrastructure.observability.output_quality_metrics import (
    ALERT_SERIES,
    render_output_quality_metrics,
)

_OUTCOMES = "yuralume_output_quality_outcomes_total"
_OUTAGE = "yuralume_output_quality_failopen_outage_total"


def test_record_totals_by_surface_and_outcome() -> None:
    counters = OutputQualityCounters()

    counters.record("feed", OUTCOME_PASS)
    counters.record("feed", OUTCOME_PASS)
    counters.record("proactive", OUTCOME_HARD_SKIPPED)

    assert counters.total("feed", OUTCOME_PASS) == 2
    assert counters.total("proactive", OUTCOME_HARD_SKIPPED) == 1
    assert counters.total("proactive", OUTCOME_PASS) == 0


def test_callers_may_record_their_own_surface_specific_outcome() -> None:
    """Feed's text-only degrade is a disposal only feed can make; it still
    belongs on the same scrape as the ones the orchestrator makes."""
    counters = OutputQualityCounters()

    counters.record("feed", OUTCOME_HARD_DEGRADED)

    body = render_output_quality_metrics(counters)
    assert f'{_OUTCOMES}{{surface="feed",outcome="hard_degraded"}} 1' in body


def test_a_failopen_streak_raises_the_outage_counter_once_per_crossing() -> None:
    counters = OutputQualityCounters()

    for _ in range(DEFAULT_FAILOPEN_ALARM_STREAK - 1):
        counters.record("feed", OUTCOME_GATE_ERROR_FAILOPEN)
    assert counters.failopen_outages() == {}

    counters.record("feed", OUTCOME_GATE_ERROR_FAILOPEN)
    assert counters.failopen_outages() == {"feed": 1}

    # Still down: the crossing is not re-announced every tick.
    counters.record("feed", OUTCOME_GATE_ERROR_FAILOPEN)
    assert counters.failopen_outages() == {"feed": 1}


def test_any_other_outcome_resets_the_streak() -> None:
    """One timeout is weather. A streak broken by a real verdict must not
    accumulate towards an outage alarm hours later."""
    counters = OutputQualityCounters()

    counters.record("feed", OUTCOME_GATE_ERROR_FAILOPEN)
    counters.record("feed", OUTCOME_GATE_ERROR_FAILOPEN)
    counters.record("feed", OUTCOME_PASS)
    assert counters.failopen_streak("feed") == 0

    counters.record("feed", OUTCOME_GATE_ERROR_FAILOPEN)
    assert counters.failopen_outages() == {}


def test_streaks_are_tracked_per_surface() -> None:
    counters = OutputQualityCounters(failopen_alarm_streak=2)

    counters.record("feed", OUTCOME_GATE_ERROR_FAILOPEN)
    counters.record("proactive", OUTCOME_GATE_ERROR_FAILOPEN)
    assert counters.failopen_outages() == {}

    counters.record("feed", OUTCOME_GATE_ERROR_FAILOPEN)
    assert counters.failopen_outages() == {"feed": 1}


def test_render_emits_labelled_series() -> None:
    counters = OutputQualityCounters(failopen_alarm_streak=1)
    counters.record("feed", OUTCOME_PASS)
    counters.record("chat", OUTCOME_HARD_SKIPPED)
    counters.record("proactive", OUTCOME_GATE_ERROR_FAILOPEN)

    body = render_output_quality_metrics(counters)

    assert f"# TYPE {_OUTCOMES} counter" in body
    assert f'{_OUTCOMES}{{surface="feed",outcome="pass"}} 1' in body
    assert f'{_OUTCOMES}{{surface="chat",outcome="hard_skipped"}} 1' in body
    assert f'{_OUTAGE}{{surface="proactive"}} 1' in body
    assert body.endswith("\n")


def test_render_of_empty_counters_is_empty() -> None:
    """Nothing has been reviewed yet, so nothing is claimed — a zero-valued
    series here would be a lie about a surface that may not even exist on
    this deployment."""
    assert render_output_quality_metrics(OutputQualityCounters()) == ""
    assert render_output_quality_metrics(None) == ""


def test_render_ignores_objects_that_are_not_counters() -> None:
    assert render_output_quality_metrics(object()) == ""
    assert render_output_quality_metrics("not counters") == ""
    assert render_output_quality_metrics(42) == ""


def test_render_survives_a_reader_that_raises() -> None:
    class _Broken:
        def snapshot(self):
            raise RuntimeError("counters unavailable")

        def failopen_outages(self):
            return {"feed": 2}

    body = render_output_quality_metrics(_Broken())

    # The half that worked still renders; the half that raised is silent.
    assert _OUTCOMES not in body
    assert f'{_OUTAGE}{{surface="feed"}} 2' in body


def test_render_skips_malformed_rows_rather_than_raising() -> None:
    class _Junk:
        def snapshot(self):
            return {
                ("feed", "pass"): 1,
                "not-a-tuple": 3,
                ("feed", "pass", "extra"): 4,
                ("feed", "soft_recovered"): "not-an-int",
                ("feed", "hard_skipped"): True,  # bool is not a count
            }

        def failopen_outages(self):
            return {"feed": 1.5}

    body = render_output_quality_metrics(_Junk())

    samples = [line for line in body.splitlines() if not line.startswith("#")]
    assert samples == [f'{_OUTCOMES}{{surface="feed",outcome="pass"}} 1']
    assert _OUTAGE not in body


def test_label_values_are_escaped() -> None:
    counters = OutputQualityCounters()
    counters.record('we"ird\\surface', OUTCOME_PASS)

    body = render_output_quality_metrics(counters)

    assert 'surface="we\\"ird\\\\surface"' in body


def test_the_alert_series_are_named_as_data() -> None:
    """An alert rule generator reads this set; a human reads the module
    docstring. They must not be able to disagree."""
    assert _OUTAGE in ALERT_SERIES
    assert f'{_OUTCOMES}{{outcome="hard_skipped"}}' in ALERT_SERIES
