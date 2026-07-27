"""Regenerate the Core feature-key manifest contract artifact.

Writes the canonical ``contracts/feature-key-manifest.json`` at the *Cloud repo
root* (the parent of the Core project), the bundled copy under the Cloud User
service resources so the Java control-plane can load it from the classpath, and
the admin-console mirror the vitest byte-compare guards.

Run after adding/removing a routable feature key:

    cd Yuralume-Core
    .venv/Scripts/python.exe scripts/export_feature_key_manifest.py

The drift guard tests (Core ``tests/unit/test_feature_key_manifest.py`` and User
service ``FeatureKeyManifestTest``) fail until both files are regenerated.
"""

from __future__ import annotations

import sys
from pathlib import Path

_CORE_ROOT = Path(__file__).resolve().parents[1]
_SRC = _CORE_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from kokoro_link.application.services.feature_key_manifest import (  # noqa: E402
    build_feature_key_manifest,
)

# Core can be embedded directly under the Cloud repository or checked out as a
# sibling in a shared workspace (the hosted deployment layout). Prefer the
# sibling Cloud repository only when its contract consumers actually exist;
# otherwise retain the historical parent layout.
_WORKSPACE_ROOT = _CORE_ROOT.parent
_SIBLING_CLOUD_ROOT = _WORKSPACE_ROOT / "yuralume-cloud"
_REPO_ROOT = (
    _SIBLING_CLOUD_ROOT
    if (
        (_SIBLING_CLOUD_ROOT / "services" / "user").is_dir()
        and (_SIBLING_CLOUD_ROOT / "admin-console").is_dir()
    )
    else _WORKSPACE_ROOT
)
CANONICAL_PATH = _REPO_ROOT / "contracts" / "feature-key-manifest.json"
USER_SERVICE_COPY = (
    _REPO_ROOT
    / "services"
    / "user"
    / "src"
    / "main"
    / "resources"
    / "contracts"
    / "feature-key-manifest.json"
)
ADMIN_CONSOLE_COPY = (
    _REPO_ROOT
    / "admin-console"
    / "src"
    / "contracts"
    / "feature-key-manifest.json"
)


def main() -> int:
    manifest = build_feature_key_manifest()
    payload = manifest.to_json()
    for target in (CANONICAL_PATH, USER_SERVICE_COPY, ADMIN_CONSOLE_COPY):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"wrote {target} ({manifest.content_hash})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
