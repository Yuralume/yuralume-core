"""Prometheus rendering for this process's prompt pack identity (PD3).

``HOSTED_PROMPT_PACK_DISTRIBUTION_PLAN`` §1-2 names the gap this closes: a
rolling restart over a changed prompt pack leaves some replicas on the old pack
and some on the new one, and while every ``TurnRecord`` carries its
``prompt_pack_hash`` as evidence, **nothing announces the split**. Mixed
versions were an archaeology exercise after the fact. Exposing the same hash on
the scrape turns it into a deployment gate (§3.4: deploy.sh compares the hash
across replicas) and an alertable series.

Two identities, deliberately (§3.4)
----------------------------------
``pack_hash`` is the **runtime** identity: the digest taken with the runtime
snapshot the prompt builder uses (``prompt_pack_hash_snapshot`` over humanization
/ prompt-quality settings), so this series and the recorded turns are the *same*
value. A metric that disagreed with the evidence it is meant to alert on would be
worse than no metric. It answers "are all replicas running the same thing" and
carries eval attribution.

``release_pack_hash`` is the **release** identity, read straight out of the
``manifest.json`` the packer put in the artifact. It answers a different
question — "is the pack this process is reading the one this deploy resolved from
the pointer" — and it exists because the two digests can never be compared: the
release side hashes with the lock file's canonical snapshot (humanization only)
while the runtime side hashes with the live settings (humanization + prompt
quality), so they are different digests by construction, not by drift. Reading
the manifest sidesteps hash conventions entirely and additionally proves the
process is reading the directory the fetcher published.

Absent is a valid answer for the release identity: self-host's hand-built overlay
has no manifest, and a malformed one is reported as unknown rather than guessed
at. A wrong release hash would fail a deploy for the wrong reason.

Deliberately mirrors ``execution_mode_metrics`` / ``action_billing_metrics``: no
``prometheus_client``, text exposition format 0.0.4, and settings arrive as
arguments so this module never learns how they are reached from the container.

**Internal only.** The hash never joins the public ``/health`` payload — a
public version fingerprint is a gift to anyone probing which build is live
(§3.4). The internal metrics route is token-gated, so self-host gets the same
series as hosted with no conditional.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kokoro_link.infrastructure.prompt.default import prompt_pack_hash_snapshot
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.infrastructure.prompts.loader import (
    PromptLoader,
    PromptPackOverlayStatus,
)

_LOGGER = logging.getLogger(__name__)

_INFO = "yuralume_core_prompt_pack_info"
_TEMPLATES = "yuralume_core_prompt_pack_templates"
_OVERLAY_TEMPLATES = "yuralume_core_prompt_pack_overlay_templates"

_MANIFEST_NAME = "manifest.json"

#: The release artifact nests its templates under ``tuned/`` next to the
#: manifest, and the hosted fetcher therefore points the overlay at that
#: subdirectory (``pack_fetch.PackCache.overlay_dir``) — so the manifest sits one
#: level ABOVE the directory this process reads. The parent is probed only when
#: the overlay directory carries that exact name, which is as narrow as the
#: layout that motivates it: an arbitrary overlay must never adopt whatever
#: ``manifest.json`` happens to sit beside it.
_ARTIFACT_OVERLAY_SUBDIR = "tuned"

#: Overlay states. ``missing`` is the one worth paging on: the pack directory
#: was configured but contributes nothing, i.e. the process silently fell back
#: to the baseline pack — exactly the silent degradation §2-3 forbids.
_OVERLAY_BASELINE = "baseline"
_OVERLAY_ACTIVE = "active"
_OVERLAY_MISSING = "missing"

#: Rendered once per process. The loader caches the pack for the lifetime of the
#: process (hot reload is deliberately not supported — §3.3), so re-walking every
#: template file on each scrape would buy nothing. A benign race just renders
#: twice; the value is identical either way, so no lock is warranted.
_CACHED_BODY: str | None = None


def render_prompt_pack_metrics(
    *,
    humanization: object | None = None,
    prompt_quality: object | None = None,
    loader: PromptLoader | None = None,
) -> str:
    """Render the prompt-pack identity series in Prometheus text format 0.0.4.

    ``loader`` defaults to the process-wide :func:`get_default_loader`, whose
    result is cached for the process. Passing an explicit loader (tests, tools)
    bypasses the cache and never populates it.

    Exceptions are **not** swallowed here: the caller owns that, matching the
    sibling exporters, so a pack that cannot be read is logged once per scrape
    with a stack trace instead of vanishing into an empty string. Nothing is
    cached in that case either, so a transient failure does not blank the series
    for the rest of the process's life. The same applies to a manifest that could
    not be *read* (as opposed to one that is simply absent or malformed, which is
    a settled fact about the pack): the render is returned but not remembered, so
    the next scrape looks again.
    """
    global _CACHED_BODY
    if loader is not None:
        return _render(loader, humanization, prompt_quality).body
    cached = _CACHED_BODY
    if cached is not None:
        return cached
    render = _render(get_default_loader(), humanization, prompt_quality)
    if render.settled:
        _CACHED_BODY = render.body
    return render.body


def reset_prompt_pack_metrics_cache() -> None:
    """Drop the cached render. Test-only helper (mirrors the loader's)."""
    global _CACHED_BODY
    _CACHED_BODY = None


@dataclass(frozen=True)
class _Render:
    """A rendered block plus whether it is safe to remember for the process.

    ``settled`` is false only for a manifest that could not be read — an I/O
    error that may well not repeat. Everything else (no overlay, no manifest, a
    malformed manifest) is a fact about the pack that cannot change without a
    restart, so it is cached like the rest of the block.
    """

    body: str
    settled: bool = True


@dataclass(frozen=True)
class _ReleaseIdentity:
    """The pack's self-declared release hash, ``""`` when it does not say."""

    pack_hash: str = ""
    settled: bool = True


def _render(
    loader: PromptLoader,
    humanization: object | None,
    prompt_quality: object | None,
) -> _Render:
    snapshot = prompt_pack_hash_snapshot(humanization, prompt_quality)
    pack_hash = loader.prompt_pack_hash(snapshot)
    status = loader.overlay_status()
    release = _read_release_pack_hash(status)

    lines: list[str] = []
    lines.append(
        # ASCII only, like every sibling exporter: HELP text crosses the wire
        # into whatever scraper is configured, and none of this needs prose.
        f"# HELP {_INFO} Prompt pack identity of this process (constant 1). "
        "pack_hash is the runtime identity recorded on every TurnRecord; "
        "replicas disagreeing on it means a mixed-version deployment. "
        "release_pack_hash is what the pack's own manifest.json claims, for "
        "comparison against the release pointer; empty when the overlay ships "
        "no manifest (self-host) or its manifest is unreadable.",
    )
    lines.append(f"# TYPE {_INFO} gauge")
    labels = _render_labels(
        {
            "pack_hash": pack_hash,
            "overlay": _overlay_state(status),
            # Always emitted, empty when unknown: Prometheus treats an empty
            # label value and an absent label as the same thing, so this keeps
            # one stable series shape across hosted and self-host instead of two.
            "release_pack_hash": release.pack_hash,
        },
    )
    lines.append(f"{_INFO}{labels} 1")

    lines.append(
        f"# HELP {_TEMPLATES} Templates visible to the loader after overlay "
        "resolution.",
    )
    lines.append(f"# TYPE {_TEMPLATES} gauge")
    lines.append(f"{_TEMPLATES} {status.effective_template_count}")

    lines.append(
        f"# HELP {_OVERLAY_TEMPLATES} Templates contributed by the configured "
        "overlay directory (0 = running the baseline pack).",
    )
    lines.append(f"# TYPE {_OVERLAY_TEMPLATES} gauge")
    lines.append(f"{_OVERLAY_TEMPLATES} {status.overlay_template_count}")

    return _Render("\n".join(lines) + "\n", settled=release.settled)


def _read_release_pack_hash(status: PromptPackOverlayStatus) -> _ReleaseIdentity:
    """Read ``pack_hash`` out of the overlay pack's ``manifest.json``.

    Never guesses and never raises: no overlay, no manifest, a manifest that is
    not JSON, or one without a usable string ``pack_hash`` all yield the unknown
    identity. Only an actual read failure is reported as unsettled so the next
    scrape retries — a malformed manifest will still be malformed after a
    thousand scrapes, and logging it a thousand times helps nobody.
    """
    if not status.configured or not status.is_dir or not status.path:
        return _ReleaseIdentity()
    overlay = Path(status.path)
    candidates = [overlay / _MANIFEST_NAME]
    if overlay.name == _ARTIFACT_OVERLAY_SUBDIR:
        candidates.append(overlay.parent / _MANIFEST_NAME)

    for candidate in candidates:
        try:
            raw = candidate.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            continue  # a hand-built overlay simply has no manifest
        except UnicodeDecodeError:  # binary junk is malformed, not unreadable
            _LOGGER.warning("prompt pack manifest is not UTF-8: %s", candidate)
            return _ReleaseIdentity()
        except OSError:
            _LOGGER.warning(
                "prompt pack manifest unreadable: %s", candidate, exc_info=True,
            )
            return _ReleaseIdentity(settled=False)
        return _ReleaseIdentity(_parse_manifest_pack_hash(raw, candidate))
    return _ReleaseIdentity()


def _parse_manifest_pack_hash(raw: str, source: Path) -> str:
    try:
        manifest = json.loads(raw)
    except ValueError:
        _LOGGER.warning("prompt pack manifest is not valid JSON: %s", source)
        return ""
    if not isinstance(manifest, dict):
        _LOGGER.warning("prompt pack manifest is not an object: %s", source)
        return ""
    pack_hash = manifest.get("pack_hash")
    if not isinstance(pack_hash, str) or not pack_hash.strip():
        _LOGGER.warning(
            "prompt pack manifest has no usable pack_hash: %s", source,
        )
        return ""
    return pack_hash.strip()


def _overlay_state(status: PromptPackOverlayStatus) -> str:
    """Collapse the overlay status into one label value.

    ``configured`` alone is not evidence the overlay is live: a bad mount or an
    empty directory leaves the process running the baseline pack while its env
    claims otherwise. That case gets its own value rather than being folded into
    ``baseline``, because "nobody asked for an overlay" and "an overlay was
    asked for and did not arrive" are opposite operational verdicts.
    """
    if not status.configured:
        return _OVERLAY_BASELINE
    if status.is_dir and status.overlay_template_count > 0:
        return _OVERLAY_ACTIVE
    return _OVERLAY_MISSING


def _render_labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    body = ",".join(
        f'{name}="{_escape_label_value(value)}"'
        for name, value in labels.items()
    )
    return "{" + body + "}"


def _escape_label_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )
