"""Prompt pack identity on the internal metrics endpoint (PD3).

``HOSTED_PROMPT_PACK_DISTRIBUTION_PLAN`` §1-2: a rolling restart over a changed
prompt pack leaves replicas split across two packs, and until now the only trace
was a ``prompt_pack_hash`` column nobody was watching. These tests pin what the
scrape has to promise for that split to become a deploy gate:

1. the hash is present on both shapes — an overlay pack and the baseline — and
   differs between them (a constant would gate nothing);
2. an overlay that was *configured but never arrived* is distinguishable from
   "no overlay was asked for", because a process silently running baseline is
   the failure §2-3 forbids;
3. a pack the loader cannot read degrades to a missing block, never a 500 — the
   scrape must not be the thing that breaks;
4. the hash never reaches the public ``/health`` payload (§3.4);
5. ``release_pack_hash`` reports what the pack's own ``manifest.json`` claims —
   and reports *nothing* rather than a guess when there is no manifest (the
   normal self-host shape) or it is malformed. The deploy smoke compares this
   label against the release pointer, so a wrong value fails a deploy for the
   wrong reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.observability import prompt_pack_metrics
from kokoro_link.infrastructure.observability.prompt_pack_metrics import (
    render_prompt_pack_metrics,
    reset_prompt_pack_metrics_cache,
)
from kokoro_link.infrastructure.prompts import reset_default_loader_for_tests
from kokoro_link.infrastructure.prompts.loader import PromptLoader

_METRICS_PATH = "/api/internal/v1/metrics"
_METRICS_ENV = "YURALUME_METRICS_INTERNAL_TOKEN"
_PACK_DIR_ENV = "YURALUME_PROMPT_PACK_DIR"
_TOKEN = "scrape-secret"

_INFO = "yuralume_core_prompt_pack_info"
_INFO_LINE = re.compile(
    rf'^{_INFO}{{pack_hash="([0-9a-f]{{64}})",overlay="(\w+)",'
    rf'release_pack_hash="([^"]*)"}} 1$',
    re.M,
)
_RELEASE_HASH = "b" * 64


class _Info(NamedTuple):
    pack_hash: str
    overlay: str
    release_pack_hash: str


@pytest.fixture(autouse=True)
def _fresh_pack_state():
    """The exporter and the loader both cache for the process lifetime, which is
    right in production and poison across tests."""
    reset_prompt_pack_metrics_cache()
    reset_default_loader_for_tests()
    yield
    reset_prompt_pack_metrics_cache()
    reset_default_loader_for_tests()


def _app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(_METRICS_ENV, _TOKEN)
    return create_app(
        AppSettings(database_url="", process=ProcessSettings(role="api")),
    )


def _scrape(app) -> str:
    with TestClient(app) as client:
        resp = client.get(
            _METRICS_PATH, headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 200
    return resp.text


def _info(body: str) -> _Info:
    match = _INFO_LINE.search(body)
    assert match is not None, f"no prompt pack info line in:\n{body}"
    return _Info(*match.groups())


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _pack(root: Path, *, manifest: str | None = None) -> Path:
    """Build the shape the release artifact really has: templates under
    ``tuned/`` with ``manifest.json`` beside that directory, which is why the
    overlay the fetcher hands Core is the ``tuned`` subdirectory."""
    overlay = root / "tuned"
    _write(overlay / "chat" / "instructions_footer.txt", "tuned footer")
    if manifest is not None:
        _write(root / "manifest.json", manifest)
    return overlay


def _manifest(pack_hash: str = _RELEASE_HASH) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "product_key": "tuned_prompts",
            "version": "2026.07.30",
            "pack_hash": pack_hash,
            "files": [],
        },
    )


def test_baseline_pack_is_reported_with_a_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No overlay configured: the shipped pack still has an identity, otherwise
    # self-host and a mis-deployed hosted replica would look alike.
    monkeypatch.delenv(_PACK_DIR_ENV, raising=False)

    body = _scrape(_app(monkeypatch))

    assert _info(body).overlay == "baseline"
    assert "yuralume_core_prompt_pack_overlay_templates 0" in body
    assert re.search(r"^yuralume_core_prompt_pack_templates ([1-9]\d*)$", body, re.M)
    # The pre-existing series are untouched.
    assert "yuralume_core_scheduler_ticks_total" in body


def test_overlay_pack_changes_the_hash_and_is_flagged_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_PACK_DIR_ENV, raising=False)
    baseline_hash = _info(_scrape(_app(monkeypatch))).pack_hash

    reset_prompt_pack_metrics_cache()
    reset_default_loader_for_tests()
    _write(tmp_path / "chat" / "instructions_footer.txt", "tuned footer")
    monkeypatch.setenv(_PACK_DIR_ENV, str(tmp_path))

    body = _scrape(_app(monkeypatch))

    info = _info(body)
    assert info.overlay == "active"
    assert "yuralume_core_prompt_pack_overlay_templates 1" in body
    # The whole point of the series: a changed pack is a changed hash. If these
    # matched, comparing replicas would prove nothing.
    assert info.pack_hash != baseline_hash


def test_configured_overlay_that_never_arrived_reads_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failed mount leaves the env var claiming an overlay while the process
    # runs baseline prompts. "Configured but contributing nothing" gets its own
    # label so that silent degradation is visible, not folded into "baseline".
    monkeypatch.setenv(_PACK_DIR_ENV, str(tmp_path / "never-mounted"))

    body = _scrape(_app(monkeypatch))

    assert _info(body).overlay == "missing"
    assert "yuralume_core_prompt_pack_overlay_templates 0" in body


def test_scrape_survives_a_pack_the_loader_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode() -> PromptLoader:
        raise OSError("prompt pack directory vanished")

    monkeypatch.setattr(prompt_pack_metrics, "get_default_loader", _explode)

    body = _scrape(_app(monkeypatch))

    # The block is absent — an honest omission — and every other series still
    # renders. A scrape that 500s takes the whole deployment's observability
    # with it.
    assert "yuralume_core_prompt_pack" not in body
    assert "yuralume_core_scheduler_ticks_total" in body


def test_public_health_never_exposes_the_pack_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §3.4 red line: a public build fingerprint only helps someone probing
    # which deployment is live.
    app = _app(monkeypatch)
    scraped_hash = _info(_scrape(app)).pack_hash

    with TestClient(app) as client:
        health = client.get("/health")

    assert health.status_code == 200
    assert scraped_hash not in health.text
    assert "prompt_pack" not in health.text


def test_renderer_declares_types_and_ends_with_a_newline(tmp_path: Path) -> None:
    _write(tmp_path / "chat" / "footer.txt", "hello")

    body = render_prompt_pack_metrics(loader=PromptLoader(package_dir=tmp_path))

    assert f"# TYPE {_INFO} gauge" in body
    assert "# TYPE yuralume_core_prompt_pack_templates gauge" in body
    assert "# TYPE yuralume_core_prompt_pack_overlay_templates gauge" in body
    assert body.endswith("\n")
    assert _info(body).overlay == "baseline"


def test_explicit_loader_neither_reads_nor_fills_the_process_cache(
    tmp_path: Path,
) -> None:
    # Tools and tests may render some other pack; that must not become the
    # answer the scrape gives for the rest of the process's life.
    _write(tmp_path / "chat" / "footer.txt", "hello")
    other = _info(
        render_prompt_pack_metrics(loader=PromptLoader(package_dir=tmp_path)),
    ).pack_hash

    live = _info(render_prompt_pack_metrics()).pack_hash

    assert live != other
    assert prompt_pack_metrics._CACHED_BODY is not None
    # Second call is served from the cache: the loader owns the pack for the
    # process lifetime, so re-walking every template per scrape buys nothing.
    assert render_prompt_pack_metrics() is prompt_pack_metrics._CACHED_BODY


# -- release identity (manifest.json) ---------------------------------------


def test_release_hash_comes_from_the_packs_own_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The deploy smoke compares THIS label against the pointer the fetcher
    # resolved, which is why it is read from the artifact rather than derived:
    # the release digest and the runtime digest are computed over different
    # snapshots and can never be equal.
    monkeypatch.setenv(_PACK_DIR_ENV, str(_pack(tmp_path, manifest=_manifest())))

    info = _info(_scrape(_app(monkeypatch)))

    assert info.release_pack_hash == _RELEASE_HASH
    assert info.overlay == "active"
    # Two identities, not one: the runtime hash stands on its own.
    assert info.pack_hash != _RELEASE_HASH


def test_release_hash_is_found_beside_a_flat_overlay_too(tmp_path: Path) -> None:
    # A pack laid out flat (manifest inside the overlay directory itself) is
    # read directly — the parent probe exists only for the artifact's `tuned/`
    # nesting, it is not the primary location.
    _write(tmp_path / "chat" / "footer.txt", "hello")
    _write(tmp_path / "manifest.json", _manifest())

    body = render_prompt_pack_metrics(
        loader=PromptLoader(package_dir=tmp_path, override_dir=tmp_path),
    )

    assert _info(body).release_pack_hash == _RELEASE_HASH


def test_overlay_without_a_manifest_reports_no_release_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The normal self-host shape: a hand-built overlay declares no release. An
    # empty label is the honest answer; anything invented here would be compared
    # against a pointer by the deploy smoke.
    monkeypatch.setenv(_PACK_DIR_ENV, str(_pack(tmp_path)))

    info = _info(_scrape(_app(monkeypatch)))

    assert info.release_pack_hash == ""
    assert info.overlay == "active"
    assert info.pack_hash  # the runtime identity is unaffected


def test_baseline_pack_reports_no_release_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_PACK_DIR_ENV, raising=False)

    assert _info(_scrape(_app(monkeypatch))).release_pack_hash == ""


@pytest.mark.parametrize(
    "manifest",
    [
        pytest.param("{not json at all", id="not-json"),
        pytest.param('["a", "list"]', id="not-an-object"),
        pytest.param('{"version": "2026.07.30"}', id="no-pack-hash"),
        pytest.param('{"pack_hash": 12345}', id="pack-hash-not-a-string"),
        pytest.param('{"pack_hash": "   "}', id="pack-hash-blank"),
    ],
)
def test_malformed_manifest_degrades_to_no_release_identity(
    manifest: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Never guess, never 500: a broken manifest costs the release label and
    # nothing else.
    monkeypatch.setenv(_PACK_DIR_ENV, str(_pack(tmp_path, manifest=manifest)))

    body = _scrape(_app(monkeypatch))

    info = _info(body)
    assert info.release_pack_hash == ""
    assert info.pack_hash
    assert "yuralume_core_scheduler_ticks_total" in body


def test_malformed_manifest_is_cached_but_an_unreadable_one_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A malformed manifest is a settled fact about the pack — caching it also
    # keeps the warning to one line per process. An I/O failure might not repeat,
    # so it must not blank the label for the rest of the process's life.
    monkeypatch.setenv(_PACK_DIR_ENV, str(_pack(tmp_path, manifest="{broken")))
    assert _info(render_prompt_pack_metrics()).release_pack_hash == ""
    assert prompt_pack_metrics._CACHED_BODY is not None

    reset_prompt_pack_metrics_cache()
    real_read_text = Path.read_text

    def _unreadable(self: Path, *args, **kwargs):
        if self.name == "manifest.json":
            raise OSError("transient I/O error")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _unreadable)

    assert _info(render_prompt_pack_metrics()).release_pack_hash == ""
    assert prompt_pack_metrics._CACHED_BODY is None

    monkeypatch.undo()
    monkeypatch.setenv(_PACK_DIR_ENV, str(_pack(tmp_path, manifest=_manifest())))
    reset_default_loader_for_tests()

    # The retry now sees the manifest, so the label recovers without a restart.
    assert _info(render_prompt_pack_metrics()).release_pack_hash == _RELEASE_HASH
