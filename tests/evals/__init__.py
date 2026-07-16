"""LLM evals harness.

Goal: build a regression net for "the character is acting like a
human" — the kinds of behavioural failures that pass unit tests (no
exception, JSON parses) but feel wrong to a user (says "我沒提過"
when they did, drifts personality across channels, double-replies in
busy windows).

Two LLM hops per fixture:

1. **System-under-test**: real backend prompt + real model produce a
   candidate response. Endpoint configured via
   ``KOKORO_EVALS_SYSTEM_ENDPOINT``.
2. **Judge**: a second model scores the candidate against the fixture
   rubric. Endpoint configured via ``KOKORO_EVALS_JUDGE_ENDPOINT`` —
   may point to the same physical server with a different ``model_id``
   to avoid self-evaluation bias.

Both env vars missing → evals auto-skip with a clear message so local
dev without LM Studio still gets through pytest.
"""
