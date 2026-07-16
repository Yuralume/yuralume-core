"""Pytest entry for the LLM evals harness.

Drives every YAML under ``tests/evals/fixtures/`` through the runner +
judge. Marked ``@pytest.mark.evals`` and **skipped by default** — CI
opts in via ``pytest -m evals`` once the LM Studio (or equivalent)
endpoints are reachable via the documented env vars.

Skip-conditions and why they exist:

* No fixtures discovered → nothing to run (treat as skip, not fail —
  fresh checkouts don't yet have curated rubrics).
* ``KOKORO_EVALS_SYSTEM_ENDPOINT`` or ``KOKORO_EVALS_JUDGE_ENDPOINT``
  unset → local-dev convenience. Without these, evals can't actually
  exercise the LLMs they're testing; failing pytest in that case would
  punish anyone running `pytest -m evals` on their laptop without a
  running LM Studio.
"""

from __future__ import annotations

import logging

import pytest

from tests.evals.runner import (
    Fixture,
    FixtureResult,
    _build_chat_model,
    discover_fixtures,
    load_judge_endpoint,
    load_system_endpoint,
    run_fixture,
)

_LOGGER = logging.getLogger(__name__)


pytestmark = [pytest.mark.evals, pytest.mark.asyncio]


_FIXTURES = discover_fixtures()


def _fixture_id(f: Fixture) -> str:
    return f.id


@pytest.fixture(scope="session")
def _system_model():
    cfg = load_system_endpoint()
    if cfg is None:
        pytest.skip(
            "KOKORO_EVALS_SYSTEM_ENDPOINT not set — evals require a real "
            "system-under-test endpoint (e.g. LM Studio).",
            allow_module_level=False,
        )
    return _build_chat_model(cfg, provider_id="evals-system"), cfg.model or None


@pytest.fixture(scope="session")
def _judge_model():
    cfg = load_judge_endpoint()
    if cfg is None:
        pytest.skip(
            "KOKORO_EVALS_JUDGE_ENDPOINT not set — evals require a real "
            "judge endpoint (may point to the same server as system, "
            "different model id recommended).",
            allow_module_level=False,
        )
    return _build_chat_model(cfg, provider_id="evals-judge"), cfg.model or None


if not _FIXTURES:
    @pytest.mark.skip(reason="no fixtures under tests/evals/fixtures/")
    def test_no_fixtures():
        pass
else:
    @pytest.mark.parametrize("fixture", _FIXTURES, ids=_fixture_id)
    async def test_fixture_passes_judge(
        fixture: Fixture,
        _system_model,
        _judge_model,
    ) -> None:
        system_model, system_model_id = _system_model
        judge_model, judge_model_id = _judge_model
        result: FixtureResult = await run_fixture(
            fixture,
            system_model=system_model,
            judge_model=judge_model,
            system_model_id=system_model_id,
            judge_model_id=judge_model_id,
        )
        if not result.verdict.passed:
            reasons = "; ".join(result.verdict.reasons) or "no reasons given"
            pytest.fail(
                f"\n  fixture: {result.fixture_id}"
                f"\n  candidate: {result.candidate[:400]!r}"
                f"\n  score: {result.verdict.score:.2f}"
                f"\n  reasons: {reasons}",
            )
