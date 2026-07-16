"""Minimal prompt template loader.

Loads ``.txt`` templates from a configurable prompts directory and
applies straightforward ``${var}`` substitution + safe newline handling.

Why not Jinja2 / Mako?
----------------------
LLM-first means the *Python side* decides which sections to render
(based on whether memory / weather / arc / etc. data exists). Templates
should only own the **wording** of those sections, not the branching.
Keeping the engine to ``${var}`` substitution is enough for that role
and avoids pulling in a templating dependency.

Override at runtime
-------------------
The default directory is ``src/kokoro_link/data/prompts/`` (shipped with
the package). Set ``YURALUME_PROMPT_PACK_DIR`` to point at an external
prompt pack directory to override individual templates without
rebuilding — the loader falls back to the package directory for any
name the override directory does not provide. ``PROMPTS_DIR`` and
``KOKORO_PROMPTS_DIR`` remain legacy fallback env names.
"""

from __future__ import annotations

import os
import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Mapping

_PACKAGE_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "prompts"
_ENV_PROMPT_PACK_DIR = "YURALUME_PROMPT_PACK_DIR"
_ENV_OVERRIDE_DIR = "PROMPTS_DIR"
_LEGACY_ENV_OVERRIDE_DIR = "KOKORO_PROMPTS_DIR"


class PromptTemplateNotFoundError(LookupError):
    """Raised when a prompt template name does not resolve to a file."""


class PromptVariableMissingError(KeyError):
    """Raised when a template references ``${var}`` but caller did not
    supply it. We surface this loudly rather than silently leaving a
    literal ``${var}`` in the prompt — a missing variable is a bug."""


@dataclass(frozen=True)
class _Resolved:
    """Internal cache entry — raw text + parsed Template."""

    path: Path
    raw: str
    template: Template


@dataclass(frozen=True)
class _HashCacheEntry:
    """Digest cache keyed by snapshot and prompt file metadata."""

    fingerprint: tuple[object, ...]
    digest: str


@dataclass(frozen=True)
class PromptPackOverlayStatus:
    """Operational summary for the external prompt pack overlay."""

    configured: bool
    path: str
    exists: bool
    is_dir: bool
    overlay_template_count: int
    effective_template_count: int
    sample_templates: tuple[str, ...] = ()


class PromptLoader:
    """Reads prompt templates by logical name (e.g. ``"shared/role_boundary"``).

    Names use forward-slash path components and **never** include the
    ``.txt`` suffix — the loader adds it. This keeps call sites readable
    and decouples them from on-disk extension choices.
    """

    def __init__(
        self,
        *,
        package_dir: Path | None = None,
        override_dir: Path | None = None,
    ) -> None:
        self._package_dir = package_dir or _PACKAGE_PROMPTS_DIR
        self._override_dir = override_dir
        self._cache: dict[str, _Resolved] = {}
        self._hash_cache: dict[str, _HashCacheEntry] = {}
        self._lock = threading.Lock()

    # -- public API -------------------------------------------------------

    def render(self, name: str, /, **variables: Any) -> str:
        """Return the fully rendered prompt as one string.

        Variables are substituted via ``string.Template`` semantics
        (``${name}`` / ``$name``). Missing variables raise
        :class:`PromptVariableMissingError`.

        A *single* trailing newline (POSIX file convention) is stripped
        so callers can splice the rendered prompt next to other content
        without producing a phantom blank line. Use :meth:`raw` if you
        need the unmodified file contents.
        """
        resolved = self._resolve(name)
        if not variables:
            rendered = self._render_no_vars(resolved)
        else:
            try:
                rendered = resolved.template.substitute(variables)
            except KeyError as exc:
                raise PromptVariableMissingError(
                    f"template {name!r} requires variable {exc.args[0]!r}",
                ) from exc
        if rendered.endswith("\n"):
            rendered = rendered[:-1]
        return rendered

    def render_lines(self, name: str, /, **variables: Any) -> list[str]:
        """Return rendered prompt split into individual lines.

        Convenience for callers that build prompts as ``list[str]`` and
        then ``"\\n".join(...)`` at the end — the historical shape across
        this codebase. Uses ``splitlines()`` so a trailing newline in the
        template file does **not** produce a phantom empty element.
        """
        return self.render(name, **variables).splitlines()

    def raw(self, name: str) -> str:
        """Return template text *unrendered*. Useful for snapshots and
        callers that want to inspect placeholders before rendering."""
        return self._resolve(name).raw

    def prompt_pack_hash(self, snapshot: Mapping[str, Any] | None = None) -> str:
        """Return a stable hash for the effective prompt pack.

        The digest covers every ``.txt`` template visible to this loader
        after overlay resolution. ``snapshot`` lets callers include
        runtime switches that change prompt assembly without changing
        template files, such as humanization feature flags. Template
        bytes are hashed after CRLF->LF normalisation so identical prompt
        packs produce the same digest on Windows and Linux checkouts.
        """
        snapshot_json = json.dumps(
            snapshot or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        names = self._template_names()
        resolved_files = tuple((name, self._locate(name)) for name in names)
        fingerprint = (
            snapshot_json,
            tuple(
                (
                    name,
                    str(path),
                    path.stat().st_mtime_ns,
                    path.stat().st_size,
                )
                for name, path in resolved_files
            ),
        )
        cache_key = hashlib.sha256(repr(fingerprint).encode("utf-8")).hexdigest()
        with self._lock:
            cached = self._hash_cache.get(cache_key)
            if cached is not None and cached.fingerprint == fingerprint:
                return cached.digest

        digest = hashlib.sha256()
        digest.update(b"yuralume.prompt-pack.v1\0")
        digest.update(snapshot_json.encode("utf-8"))
        digest.update(b"\0")
        for name, resolved in resolved_files:
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(resolved.read_bytes().replace(b"\r\n", b"\n"))
            digest.update(b"\0")
        value = digest.hexdigest()
        with self._lock:
            self._hash_cache[cache_key] = _HashCacheEntry(
                fingerprint=fingerprint,
                digest=value,
            )
        return value

    def overlay_status(self) -> PromptPackOverlayStatus:
        """Return a startup-safe summary of the configured overlay.

        The status scans the override directory itself, so startup logs can
        distinguish a real mounted pack from a merely configured env var.
        It never reads or logs prompt contents.
        """
        effective_count = len(self._template_names())
        if self._override_dir is None:
            return PromptPackOverlayStatus(
                configured=False,
                path="",
                exists=False,
                is_dir=False,
                overlay_template_count=0,
                effective_template_count=effective_count,
            )

        exists = self._override_dir.exists()
        is_dir = self._override_dir.is_dir()
        names: list[str] = []
        if is_dir:
            names = self._template_names_for_root(self._override_dir)
        return PromptPackOverlayStatus(
            configured=True,
            path=str(self._override_dir),
            exists=exists,
            is_dir=is_dir,
            overlay_template_count=len(names),
            effective_template_count=effective_count,
            sample_templates=tuple(names[:5]),
        )

    def exists(self, name: str) -> bool:
        try:
            self._resolve(name)
        except PromptTemplateNotFoundError:
            return False
        return True

    def clear_cache(self) -> None:
        """Drop the in-memory cache. Tests use this to pick up files
        written after the loader was constructed; production code should
        not need it."""
        with self._lock:
            self._cache.clear()
            self._hash_cache.clear()

    # -- internals --------------------------------------------------------

    def _resolve(self, name: str) -> _Resolved:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._cache.get(name)
            if cached is not None:
                return cached
            path = self._locate(name)
            raw = path.read_text(encoding="utf-8")
            resolved = _Resolved(path=path, raw=raw, template=Template(raw))
            self._cache[name] = resolved
            return resolved

    def _locate(self, name: str) -> Path:
        if not name or name.startswith("/") or ".." in name.split("/"):
            raise PromptTemplateNotFoundError(
                f"invalid prompt template name: {name!r}",
            )
        rel = Path(*name.split("/")).with_suffix(".txt")
        candidates: list[Path] = []
        if self._override_dir is not None:
            candidates.append(self._override_dir / rel)
        candidates.append(self._package_dir / rel)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        searched = ", ".join(str(c) for c in candidates)
        raise PromptTemplateNotFoundError(
            f"prompt template {name!r} not found (searched: {searched})",
        )

    def _template_names(self) -> list[str]:
        names: set[str] = set()
        for root in (self._package_dir, self._override_dir):
            if root is None or not root.is_dir():
                continue
            names.update(self._template_names_for_root(root))
        return sorted(names)

    def _template_names_for_root(self, root: Path) -> list[str]:
        names: set[str] = set()
        for path in root.rglob("*.txt"):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            names.add(rel.with_suffix("").as_posix())
        return sorted(names)

    def _render_no_vars(self, resolved: _Resolved) -> str:
        # ``Template.substitute({})`` still complains if any ``${var}``
        # exists. ``safe_substitute`` would silently leave placeholders.
        # We want loud failure, so attempt substitution with empty mapping
        # only when we know there are no placeholders.
        if "$" not in resolved.raw:
            return resolved.raw
        # Force the same code path as render() so the error message is
        # consistent.
        try:
            return resolved.template.substitute({})
        except KeyError as exc:
            raise PromptVariableMissingError(
                f"template {resolved.path} requires variable {exc.args[0]!r}",
            ) from exc


_default_loader: PromptLoader | None = None
_default_loader_lock = threading.Lock()


def get_default_loader() -> PromptLoader:
    """Return the process-wide default :class:`PromptLoader`.

    Honours ``YURALUME_PROMPT_PACK_DIR`` for the overlay directory. The
    instance is cached for the lifetime of the process; callers that
    need different roots (tests, multi-tenant) should construct their
    own ``PromptLoader`` directly.
    """
    global _default_loader
    if _default_loader is not None:
        return _default_loader
    with _default_loader_lock:
        if _default_loader is None:
            override_raw = (
                os.environ.get(_ENV_PROMPT_PACK_DIR, "")
                or os.environ.get(_ENV_OVERRIDE_DIR, "")
                or os.environ.get(_LEGACY_ENV_OVERRIDE_DIR, "")
            ).strip()
            override = Path(override_raw).expanduser() if override_raw else None
            _default_loader = PromptLoader(override_dir=override)
    return _default_loader


def reset_default_loader_for_tests() -> None:
    """Drop the cached default loader. Test-only helper."""
    global _default_loader
    with _default_loader_lock:
        _default_loader = None


__all__ = [
    "PromptLoader",
    "PromptPackOverlayStatus",
    "PromptTemplateNotFoundError",
    "PromptVariableMissingError",
    "get_default_loader",
    "reset_default_loader_for_tests",
]


_ = Mapping  # re-exported for type hints in callers, keep import live
