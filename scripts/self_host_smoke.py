"""Self-host compose smoke verification.

Walks the DoD in ``docs/SELF_HOST_COMPOSE_READY_TASK.md``:

* container basics: app /health, storage /health, SPA fallback
* auth config probe (honours AUTH_ENABLED + needs_setup)
* runtime build info probe (/system/version)
* admin provider catalogue + runtime provider list
* media catalogues (image / video / TTS) are reachable
* (optional, with --openai-key) full BYOK round-trip — create LLM
  + embedding provider via Admin API, server-side test, confirm
  /system/providers picks them up without restart, then delete

Designed to be re-runnable: any provider it creates is labelled
``[smoke] …`` and deleted on exit (including on early failure), so a
second run starts from a clean state.

Exit code: 0 if every step the user opted into passed, 1 otherwise.

Typical usage::

    uv run python scripts/self_host_smoke.py
    uv run python scripts/self_host_smoke.py --openai-key sk-...
    uv run python scripts/self_host_smoke.py --base-url http://yuralume.local

Auth-enabled deployments: pass --email / --password and the script
will /auth/login (or /auth/setup on a fresh box) to acquire a bearer
token before hitting admin routes.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


# ----------------------------------------------------------------------
# Result tracking
# ----------------------------------------------------------------------


@dataclass(slots=True)
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass(slots=True)
class SmokeReport:
    steps: list[StepResult] = field(default_factory=list)

    def record(
        self,
        name: str,
        ok: bool,
        detail: str = "",
        skipped: bool = False,
    ) -> None:
        self.steps.append(
            StepResult(name=name, ok=ok, detail=detail, skipped=skipped),
        )
        if skipped:
            print(f"  -  {name} (skipped: {detail})")
        elif ok:
            tail = f"  -- {detail}" if detail else ""
            print(f"  ok {name}{tail}")
        else:
            print(f"  XX {name} -- {detail}")

    @property
    def passed(self) -> bool:
        return all(s.ok or s.skipped for s in self.steps)

    def summary(self) -> str:
        passed = sum(1 for s in self.steps if s.ok and not s.skipped)
        skipped = sum(1 for s in self.steps if s.skipped)
        failed = sum(1 for s in self.steps if not s.ok and not s.skipped)
        return (
            f"{passed} passed / {failed} failed / {skipped} skipped"
        )


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------


class Smoke:
    SMOKE_LABEL_PREFIX = "[smoke]"

    def __init__(
        self,
        *,
        base_url: str,
        storage_url: str,
        email: str | None,
        password: str | None,
        openai_key: str | None,
        openai_chat_model: str,
        openai_embedding_model: str,
        skip_cleanup: bool,
        timeout: float,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.storage = storage_url.rstrip("/")
        self.email = email
        self.password = password
        self.openai_key = openai_key
        self.openai_chat_model = openai_chat_model
        self.openai_embedding_model = openai_embedding_model
        self.skip_cleanup = skip_cleanup
        self.report = SmokeReport()
        self.token: str | None = None
        self.created_provider_ids: list[str] = []
        self._client = httpx.Client(timeout=timeout, follow_redirects=False)

    # -- session ------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _api(self, path: str) -> str:
        return f"{self.base}/api/v1{path}"

    # -- phase: container basics --------------------------------------

    def phase_health(self) -> None:
        print("\n[health]")
        try:
            r = self._client.get(f"{self.base}/health")
            ok = r.status_code == 200 and r.json().get("status") == "ok"
            self.report.record(
                "GET /health",
                ok,
                f"status={r.status_code} body={r.text[:120]}",
            )
        except httpx.HTTPError as exc:
            self.report.record("GET /health", False, repr(exc))
            return

        try:
            r = self._client.get(f"{self.storage}/health")
            ok = r.status_code == 200
            self.report.record(
                "storage /health",
                ok,
                f"status={r.status_code}",
            )
        except httpx.HTTPError as exc:
            self.report.record("storage /health", False, repr(exc))

    def phase_spa_fallback(self) -> None:
        print("\n[spa-fallback]")
        # Any SPA route should return the app shell (index.html), not 404.
        # We don't assert exact content — the server may serve a router
        # placeholder during dev — only that it isn't 404.
        for path in ("/admin", "/stage", "/fusion-story", "/branching-drama"):
            try:
                r = self._client.get(f"{self.base}{path}")
                ok = r.status_code == 200 and (
                    "<html" in r.text.lower() or "vite" in r.text.lower()
                )
                self.report.record(
                    f"GET {path}",
                    ok,
                    f"status={r.status_code}",
                )
            except httpx.HTTPError as exc:
                self.report.record(f"GET {path}", False, repr(exc))

    def phase_version(self) -> None:
        print("\n[version]")
        try:
            r = self._client.get(self._api("/system/version"))
        except httpx.HTTPError as exc:
            self.report.record("GET /system/version", False, repr(exc))
            return
        if r.status_code != 200:
            self.report.record(
                "GET /system/version",
                False,
                f"status={r.status_code} body={r.text[:200]}",
            )
            return
        body = r.json()
        build = body.get("build") if isinstance(body, dict) else {}
        ok = (
            isinstance(body, dict)
            and isinstance(body.get("version"), str)
            and isinstance(body.get("api_version"), str)
            and isinstance(build, dict)
        )
        self.report.record(
            "GET /system/version",
            ok,
            (
                f"version={body.get('version')} "
                f"image_tag={build.get('image_tag')}"
            ) if isinstance(body, dict) and isinstance(build, dict) else "",
        )

    # -- phase: auth --------------------------------------------------

    def phase_auth(self) -> None:
        print("\n[auth]")
        try:
            r = self._client.get(self._api("/auth/config"))
        except httpx.HTTPError as exc:
            self.report.record("GET /auth/config", False, repr(exc))
            return
        if r.status_code != 200:
            self.report.record(
                "GET /auth/config", False, f"status={r.status_code}",
            )
            return
        body = r.json()
        self.report.record(
            "GET /auth/config",
            True,
            f"auth_enabled={body.get('auth_enabled')} needs_setup={body.get('needs_setup')}",
        )

        if not body.get("auth_enabled"):
            return  # bearer token unnecessary; admin routes accept default user

        if not (self.email and self.password):
            self.report.record(
                "login",
                False,
                "AUTH_ENABLED=true but --email/--password not supplied",
            )
            return

        # Either /auth/setup (first run) or /auth/login (subsequent).
        if body.get("needs_setup"):
            ok, detail = self._try_setup()
            self.report.record("POST /auth/setup", ok, detail)
            if not ok:
                return
        else:
            ok, detail = self._try_login()
            self.report.record("POST /auth/login", ok, detail)

    def _try_setup(self) -> tuple[bool, str]:
        try:
            r = self._client.post(
                self._api("/auth/setup"),
                json={
                    "email": self.email,
                    "password": self.password,
                    "primary_language": "zh-TW",
                },
            )
        except httpx.HTTPError as exc:
            return False, repr(exc)
        if r.status_code in (200, 201):
            self.token = r.json().get("token")
            return bool(self.token), "token acquired"
        return False, f"status={r.status_code} body={r.text[:200]}"

    def _try_login(self) -> tuple[bool, str]:
        try:
            r = self._client.post(
                self._api("/auth/login"),
                json={"email": self.email, "password": self.password},
            )
        except httpx.HTTPError as exc:
            return False, repr(exc)
        if r.status_code == 200:
            self.token = r.json().get("token")
            return bool(self.token), "token acquired"
        return False, f"status={r.status_code} body={r.text[:200]}"

    # -- phase: catalogues --------------------------------------------

    def phase_admin_catalog(self) -> None:
        print("\n[admin-catalog]")
        try:
            r = self._client.get(
                self._api("/admin/providers/catalog"),
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            self.report.record(
                "GET /admin/providers/catalog", False, repr(exc),
            )
            return
        if r.status_code != 200:
            self.report.record(
                "GET /admin/providers/catalog",
                False,
                f"status={r.status_code} body={r.text[:200]}",
            )
            return
        entries = r.json()
        ok = isinstance(entries, list) and len(entries) >= 5
        self.report.record(
            "GET /admin/providers/catalog",
            ok,
            f"{len(entries)} catalog entries",
        )

        # Existing connections (may include a previous smoke run's leftovers).
        try:
            r = self._client.get(
                self._api("/admin/providers"),
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            self.report.record(
                "GET /admin/providers", False, repr(exc),
            )
            return
        if r.status_code != 200:
            self.report.record(
                "GET /admin/providers",
                False,
                f"status={r.status_code} body={r.text[:200]}",
            )
            return
        connections = r.json()
        self.report.record(
            "GET /admin/providers",
            True,
            f"{len(connections)} connection(s) currently saved",
        )

        # Pre-clean any leftover [smoke] rows from prior failed runs.
        leftover_ids = [
            c["id"]
            for c in connections
            if str(c.get("label", "")).startswith(self.SMOKE_LABEL_PREFIX)
        ]
        for cid in leftover_ids:
            self._delete_provider(cid, label="pre-clean leftover")

    def phase_runtime_providers(self) -> None:
        print("\n[runtime-routes]")
        for path, expect_list in (
            ("/system/providers", True),
            ("/system/image-profiles", True),
            ("/system/video-profiles", True),
            ("/tts/assets", False),  # response shape is object {assets: [...]}
        ):
            try:
                r = self._client.get(
                    self._api(path),
                    headers=self._auth_headers(),
                )
            except httpx.HTTPError as exc:
                self.report.record(f"GET {path}", False, repr(exc))
                continue
            if r.status_code != 200:
                self.report.record(
                    f"GET {path}",
                    False,
                    f"status={r.status_code} body={r.text[:160]}",
                )
                continue
            try:
                body = r.json()
            except ValueError:
                self.report.record(
                    f"GET {path}", False, "response is not JSON",
                )
                continue
            if expect_list:
                ok = isinstance(body, list)
                detail = f"{len(body)} item(s)" if ok else f"got {type(body).__name__}"
            else:
                ok = isinstance(body, dict)
                detail = f"keys={list(body.keys())[:4]}" if ok else f"got {type(body).__name__}"
            self.report.record(f"GET {path}", ok, detail)

    # -- phase: BYOK round-trip --------------------------------------

    def phase_byok(self) -> None:
        if not self.openai_key:
            print("\n[byok-openai] (skipped -- no --openai-key supplied)")
            self.report.record(
                "BYOK round-trip",
                True,
                "skipped (provide --openai-key to enable)",
                skipped=True,
            )
            return

        print("\n[byok-openai]")

        # 1. Draft-test via /admin/providers/test-draft. Confirms the
        # key + base_url + model + adapter selection actually round-
        # trip to OpenAI *before* we save anything to DB.
        draft = {
            "provider": "openai",
            "label": f"{self.SMOKE_LABEL_PREFIX} OpenAI draft test",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {
                "base_url": "https://api.openai.com/v1",
                "default_model": self.openai_chat_model,
            },
            "secret": {"api_key": self.openai_key},
        }
        try:
            r = self._client.post(
                self._api("/admin/providers/test-draft"),
                json=draft,
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            self.report.record(
                "POST /admin/providers/test-draft", False, repr(exc),
            )
            return
        if r.status_code != 200:
            self.report.record(
                "POST /admin/providers/test-draft",
                False,
                f"status={r.status_code} body={r.text[:200]}",
            )
            return
        body = r.json()
        if not body.get("ok"):
            self.report.record(
                "POST /admin/providers/test-draft",
                False,
                f"server returned ok=false: {body.get('last_validation_error')}",
            )
            return
        self.report.record(
            "POST /admin/providers/test-draft (LLM)",
            True,
            "OpenAI key validated",
        )

        # 2. Persist the LLM provider — runtime sync should pick it up
        # without any restart.
        llm_id = self._create_provider({**draft, "capabilities": ["llm"]})
        if not llm_id:
            return

        # 3. Hit /system/providers and confirm the new provider id is
        # registered. This is the BYOK "no restart needed" assertion.
        if not self._wait_for_runtime_provider(llm_id_or_name="openai"):
            self.report.record(
                "runtime sync picked up LLM",
                False,
                "/system/providers did not surface 'openai'",
            )
        else:
            self.report.record(
                "runtime sync picked up LLM",
                True,
                "'openai' present in /system/providers",
            )

        # 4. Embedding capability. Different connection so the model
        # field can differ; we exercise the same provider id.
        emb_draft = {
            "provider": "openai",
            "label": f"{self.SMOKE_LABEL_PREFIX} OpenAI embedding",
            "enabled": True,
            "capabilities": ["embedding"],
            "config": {
                "base_url": "https://api.openai.com/v1",
                "embedding_model": self.openai_embedding_model,
            },
            "secret": {"api_key": self.openai_key},
        }
        try:
            r = self._client.post(
                self._api("/admin/providers/test-draft"),
                json=emb_draft,
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            self.report.record(
                "POST /admin/providers/test-draft (embedding)",
                False,
                repr(exc),
            )
            return
        if r.status_code != 200 or not r.json().get("ok"):
            self.report.record(
                "POST /admin/providers/test-draft (embedding)",
                False,
                f"status={r.status_code} body={r.text[:200]}",
            )
            return
        self.report.record(
            "POST /admin/providers/test-draft (embedding)",
            True,
            "OpenAI embedding key validated",
        )
        self._create_provider(emb_draft)

    # -- helpers ------------------------------------------------------

    def _create_provider(self, payload: dict[str, Any]) -> str | None:
        try:
            r = self._client.post(
                self._api("/admin/providers"),
                json=payload,
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            self.report.record(
                f"POST /admin/providers ({payload.get('label')})",
                False,
                repr(exc),
            )
            return None
        if r.status_code not in (200, 201):
            self.report.record(
                f"POST /admin/providers ({payload.get('label')})",
                False,
                f"status={r.status_code} body={r.text[:200]}",
            )
            return None
        body = r.json()
        cid = body.get("id")
        if not cid:
            self.report.record(
                f"POST /admin/providers ({payload.get('label')})",
                False,
                "no id in response",
            )
            return None
        self.created_provider_ids.append(cid)
        self.report.record(
            f"POST /admin/providers ({payload.get('label')})",
            True,
            f"id={cid}",
        )
        return cid

    def _wait_for_runtime_provider(
        self, llm_id_or_name: str, attempts: int = 4, gap: float = 0.5,
    ) -> bool:
        """Poll /system/providers until the named provider shows up.

        runtime_sync.sync_provider_connections is awaited synchronously
        inside POST /admin/providers, so this normally hits on the first
        try; the retry is purely a guard against slow registry locks on
        very loaded boxes."""
        for _ in range(attempts):
            try:
                r = self._client.get(
                    self._api("/system/providers"),
                    headers=self._auth_headers(),
                )
            except httpx.HTTPError:
                time.sleep(gap)
                continue
            if r.status_code == 200 and isinstance(r.json(), list):
                if llm_id_or_name in r.json():
                    return True
            time.sleep(gap)
        return False

    def _delete_provider(self, cid: str, *, label: str = "") -> None:
        try:
            r = self._client.delete(
                self._api(f"/admin/providers/{cid}"),
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            self.report.record(
                f"DELETE /admin/providers/{cid}", False, repr(exc),
            )
            return
        ok = r.status_code in (200, 204, 404)
        self.report.record(
            f"DELETE /admin/providers/{cid}",
            ok,
            label or f"status={r.status_code}",
        )

    # -- cleanup ------------------------------------------------------

    def cleanup(self) -> None:
        if self.skip_cleanup or not self.created_provider_ids:
            return
        print("\n[cleanup]")
        for cid in list(self.created_provider_ids):
            self._delete_provider(cid)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Self-host Docker Compose smoke for Yuralume. Walks the "
            "DoD in docs/SELF_HOST_COMPOSE_READY_TASK.md."
        ),
    )
    p.add_argument(
        "--base-url",
        default="http://127.0.0.1:8012",
        help="Yuralume app base URL (default: container compose host port).",
    )
    p.add_argument(
        "--storage-url",
        default="http://127.0.0.1:9012",
        help="Object Storage public base URL (storage-local default).",
    )
    p.add_argument(
        "--email",
        default=None,
        help=(
            "Operator email -- required only when AUTH_ENABLED=true. "
            "On a fresh deployment this also drives /auth/setup."
        ),
    )
    p.add_argument(
        "--password",
        default=None,
        help="Operator password -- pairs with --email.",
    )
    p.add_argument(
        "--openai-key",
        default=None,
        help=(
            "OpenAI API key. When set, runs the BYOK round-trip "
            "(create LLM + embedding provider, server-side test, "
            "verify /system/providers picks them up)."
        ),
    )
    p.add_argument(
        "--openai-chat-model",
        default="gpt-4o-mini",
        help="OpenAI chat model used by the BYOK round-trip.",
    )
    p.add_argument(
        "--openai-embedding-model",
        default="text-embedding-3-small",
        help="OpenAI embedding model used by the BYOK round-trip.",
    )
    p.add_argument(
        "--skip-cleanup",
        action="store_true",
        help=(
            "Leave [smoke]-labelled provider rows in DB instead of "
            "deleting them on exit. Useful when chaining with manual "
            "follow-up smoke."
        ),
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout (seconds) for every request.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    smoke = Smoke(
        base_url=args.base_url,
        storage_url=args.storage_url,
        email=args.email,
        password=args.password,
        openai_key=args.openai_key,
        openai_chat_model=args.openai_chat_model,
        openai_embedding_model=args.openai_embedding_model,
        skip_cleanup=args.skip_cleanup,
        timeout=args.timeout,
    )
    try:
        smoke.phase_health()
        smoke.phase_spa_fallback()
        smoke.phase_version()
        smoke.phase_auth()
        smoke.phase_admin_catalog()
        smoke.phase_runtime_providers()
        smoke.phase_byok()
    finally:
        smoke.cleanup()
        smoke.close()

    print("\n" + "=" * 60)
    print(smoke.report.summary())
    if smoke.report.passed:
        print("RESULT: PASS")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
