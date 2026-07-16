"""Phase 7 static guard: no new cloud-mode paid capability adapter may be wired
directly in Core.

The behavioural route-matrix tests prove today's wiring; this AST guard is the
forward-looking rail. It parses ``container.py`` and fails if any ``if
app_settings.cloud.active:`` branch constructs a capability adapter (chat model,
image/video provider, TTS adapter, embedder) that is not one of the approved
Cloud Gateway adapters — so a future ``OpenAIEmbedder`` or third-party SDK wired
straight into the cloud branch trips the test instead of silently bypassing the
Gateway (plan §7).
"""

from __future__ import annotations

import ast
import pathlib

_CONTAINER = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src" / "kokoro_link" / "bootstrap" / "container.py"
)

# Capability adapters allowed inside a cloud-mode branch: each forwards to the
# Cloud Gateway rather than calling a paid provider directly.
_ALLOWED_CLOUD_ADAPTERS = frozenset({
    "CloudActiveLLMProvider",
    "CloudActiveImageProvider",
    "CloudActiveVideoProvider",
    "CloudGatewayChatModel",
    "CloudGatewayImageProvider",
    "CloudGatewayVideoProvider",
    "CloudGatewayTTSAdapter",
})

# Class-name suffixes that mark a paid capability adapter constructor.
_SUSPECT_SUFFIXES = (
    "ChatModel",
    "ImageProvider",
    "VideoProvider",
    "TTSAdapter",
    "Embedder",
    "EmbeddingProvider",
)


def _is_cloud_active_test(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "active"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "cloud"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "app_settings"
    )


def _called_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_no_direct_paid_capability_adapter_in_cloud_branch() -> None:
    tree = ast.parse(_CONTAINER.read_text(encoding="utf-8"))
    offenders: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not _is_cloud_active_test(node.test):
            continue
        # Scan only the cloud-active branch body, never the self-host else/elif.
        for statement in node.body:
            for inner in ast.walk(statement):
                if not isinstance(inner, ast.Call):
                    continue
                name = _called_name(inner)
                if name.endswith(_SUSPECT_SUFFIXES) and name not in _ALLOWED_CLOUD_ADAPTERS:
                    offenders.add(name)

    assert not offenders, (
        "cloud-mode branches must route paid capabilities through the Cloud Gateway "
        f"adapters, not construct them directly: {sorted(offenders)}"
    )
