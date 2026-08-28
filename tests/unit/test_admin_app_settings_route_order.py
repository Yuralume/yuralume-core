"""The static ``/admin/app-settings/*`` routes must out-rank ``{group}``.

``admin_app_settings.py`` owns a greedy ``/admin/app-settings/{group}`` pair,
while ``observability.py`` owns three *static* siblings underneath the same
prefix (quiet hours read/write plus two flag snapshots). Starlette resolves in
registration order, so whichever router is mounted first wins the path — and
when the greedy one won, every static sibling answered
``404 unknown group: quiet-hours`` instead of running. Nothing else went red:
the routes still existed, still passed their own unit tests, and only the
legacy admin panel calling them ever found out.

So the invariant is pinned here on resolution itself rather than on mount
order, which is the thing a future reader would have to remember.
"""

from __future__ import annotations

import pytest
from starlette.routing import Match

from kokoro_link.api.app import create_app
from kokoro_link.bootstrap.settings import AppSettings

_GREEDY = "/api/v1/admin/app-settings/{group}"


def _resolved_path(app, method: str, path: str) -> str:
    """Path template of the first route Starlette would hand the request to."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
    }
    for route in app.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route.path
    raise AssertionError(f"no route matched {method} {path}")


@pytest.fixture(scope="module")
def app():
    return create_app(AppSettings())


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/admin/app-settings/quiet-hours"),
        ("PUT", "/api/v1/admin/app-settings/quiet-hours"),
        ("GET", "/api/v1/admin/app-settings/humanization-flags"),
        ("GET", "/api/v1/admin/app-settings/persona-curiosity-flags"),
    ],
)
def test_static_app_settings_routes_are_not_shadowed_by_group(
    app, method: str, path: str,
) -> None:
    resolved = _resolved_path(app, method, path)
    assert resolved != _GREEDY, (
        f"{method} {path} resolves to the greedy group route, which answers "
        "404 for it; mount admin_app_settings_router after "
        "observability_router"
    )
    assert resolved == path


def test_group_route_still_serves_real_groups(app) -> None:
    """The reordering must not cost the greedy route its own traffic."""
    assert _resolved_path(
        app, "PUT", "/api/v1/admin/app-settings/weather",
    ) == _GREEDY
