"""Site-settings hot reload across processes (G0.1).

The bug this covers: ``build_container`` overlaid the DB-persisted ``site.*``
groups onto :class:`AppSettings` exactly **once**, at boot, and handed frozen
value objects to the weather / calendar / GeoIP adapters and the RSS seed. In a
Hosted deploy (2×api + coordinator + worker) each process therefore held
whatever the table said at *its own* boot: an Admin change to the site weather
coordinates took effect on the replica that served the PUT and needed a rolling
restart to reach the rest, while a failed overlay read silently downgraded a
process to env defaults with nothing anywhere saying so.

Everything is driven with in-memory fakes — no database, no asyncpg — so the
decision logic is pinned independently of the transport.
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.application.services.app_runtime_settings_service import (
    AppRuntimeSettingsService,
)
from kokoro_link.application.services.rss_source_sync_service import (
    RssSourceSyncService,
)
from kokoro_link.application.services.runtime_config_refresher import (
    RuntimeConfigRefresher,
)
from kokoro_link.bootstrap.settings import (
    AppSettings,
    CalendarSettings,
    GeoIpSettings,
    WeatherSettings,
)
from kokoro_link.bootstrap.site_settings_holder import (
    SITE_SETTINGS_HOT_GROUPS,
    SiteSettingsHolder,
    SiteSettingsSnapshot,
)
from kokoro_link.bootstrap.site_settings_providers import (
    ReloadableCalendarProvider,
    ReloadableGeoLocationProvider,
    ReloadableWeatherProvider,
)
from kokoro_link.bootstrap.site_settings_refresh import (
    SITE_SETTINGS_HOT_KEYS,
    build_site_settings_refresher,
    build_site_settings_reloader,
)
from kokoro_link.contracts.runtime_config_events import (
    RUNTIME_CONFIG_TOPIC_PROVIDERS,
    RUNTIME_CONFIG_TOPIC_SITE_SETTINGS,
)
from kokoro_link.infrastructure.app_runtime_settings.schemas import key_for_group
from kokoro_link.infrastructure.geo.null_provider import NullGeoLocationProvider
from kokoro_link.infrastructure.repositories.in_memory_runtime_settings import (
    InMemoryRuntimeSettingsRepository,
)
from kokoro_link.infrastructure.weather.open_meteo_provider import (
    NullWeatherProvider,
    OpenMeteoWeatherProvider,
)


def _snapshot(**overrides) -> SiteSettingsSnapshot:
    base = AppSettings()
    return SiteSettingsSnapshot.from_app_settings(base, **overrides)


# --- holder -----------------------------------------------------------------


def test_holder_accessors_read_the_current_snapshot() -> None:
    holder = SiteSettingsHolder(_snapshot())
    assert holder.calendar_region == holder.calendar_region  # accessor, not value
    assert holder.calendar_region() == AppSettings().calendar.region
    assert holder.overlay_source == "db"


def test_holder_replace_is_all_or_nothing() -> None:
    """One rebind of an immutable snapshot — a reader never sees a partial mix."""
    holder = SiteSettingsHolder(_snapshot())
    before = holder.snapshot
    holder.replace(
        SiteSettingsSnapshot(
            weather=WeatherSettings(latitude=35.0, longitude=139.0),
            calendar=CalendarSettings(region="JP"),
            geoip=GeoIpSettings(enabled=False),
            world_events=before.world_events,
            source="db",
        ),
    )
    assert holder.weather().latitude == 35.0
    assert holder.calendar_region() == "JP"
    assert holder.geoip().enabled is False
    # The previously handed-out snapshot is untouched by the swap.
    assert before.calendar.region == "TW"
    assert before is not holder.snapshot


def test_hot_groups_and_keys_agree() -> None:
    assert SITE_SETTINGS_HOT_GROUPS == ("weather", "calendar", "geoip", "world_events")
    assert SITE_SETTINGS_HOT_KEYS == tuple(
        key_for_group(g) for g in SITE_SETTINGS_HOT_GROUPS
    )


# --- consumers follow the holder --------------------------------------------


@pytest.mark.asyncio
async def test_weather_provider_uses_the_new_coordinates_after_a_swap() -> None:
    holder = SiteSettingsHolder(_snapshot())
    provider = ReloadableWeatherProvider(
        holder=holder, default_primary_language="zh-TW",
    )
    # Env default has no coordinates → the adapter is built but resolves nothing.
    first = provider.inner
    assert isinstance(first, OpenMeteoWeatherProvider)
    assert await provider.describe() == ""

    snapshot = holder.snapshot
    holder.replace(
        SiteSettingsSnapshot(
            weather=WeatherSettings(
                latitude=35.68, longitude=139.69, location_label="東京",
            ),
            calendar=snapshot.calendar,
            geoip=snapshot.geoip,
            world_events=snapshot.world_events,
        ),
    )
    rebuilt = provider.inner
    assert rebuilt is not first
    assert isinstance(rebuilt, OpenMeteoWeatherProvider)
    assert rebuilt._latitude == 35.68
    assert rebuilt._longitude == 139.69


@pytest.mark.asyncio
async def test_weather_provider_does_not_rebuild_while_settings_are_unchanged() -> None:
    """Steady state must stay one equality check — this is the chat hot path."""
    holder = SiteSettingsHolder(_snapshot())
    provider = ReloadableWeatherProvider(
        holder=holder, default_primary_language="zh-TW",
    )
    first = provider.inner
    for _ in range(5):
        await provider.describe()
    assert provider.inner is first


@pytest.mark.asyncio
async def test_weather_provider_follows_the_enabled_toggle() -> None:
    holder = SiteSettingsHolder(_snapshot())
    provider = ReloadableWeatherProvider(
        holder=holder, default_primary_language="zh-TW",
    )
    assert isinstance(provider.inner, OpenMeteoWeatherProvider)
    snapshot = holder.snapshot
    holder.replace(
        SiteSettingsSnapshot(
            weather=WeatherSettings(enabled=False),
            calendar=snapshot.calendar,
            geoip=snapshot.geoip,
            world_events=snapshot.world_events,
        ),
    )
    assert isinstance(provider.inner, NullWeatherProvider)


def test_calendar_provider_follows_the_region() -> None:
    from datetime import date, timezone

    holder = SiteSettingsHolder(_snapshot())
    provider = ReloadableCalendarProvider(holder=holder, local_tz=timezone.utc)
    assert getattr(provider.inner, "region", None) == "TW"
    # 2026-02-28 is Taiwan's 和平紀念日 but an ordinary Saturday in Japan.
    tw_block = provider.describe(date(2026, 2, 28))

    snapshot = holder.snapshot
    holder.replace(
        SiteSettingsSnapshot(
            weather=snapshot.weather,
            calendar=CalendarSettings(region="JP"),
            geoip=snapshot.geoip,
            world_events=snapshot.world_events,
        ),
    )
    assert getattr(provider.inner, "region", None) == "JP"
    assert provider.describe(date(2026, 2, 28)) != tw_block


@pytest.mark.asyncio
async def test_geoip_provider_follows_the_enabled_toggle_and_releases_the_old() -> None:
    holder = SiteSettingsHolder(_snapshot())
    provider = ReloadableGeoLocationProvider(holder=holder)
    live = provider.inner
    assert live.__class__.__name__ == "IpApiGeoLocationProvider"

    snapshot = holder.snapshot
    holder.replace(
        SiteSettingsSnapshot(
            weather=snapshot.weather,
            calendar=snapshot.calendar,
            geoip=GeoIpSettings(enabled=False),
            world_events=snapshot.world_events,
        ),
    )
    assert isinstance(provider.inner, NullGeoLocationProvider)
    # The superseded adapter owned its own HTTP client; the swap released it
    # rather than stranding one connection pool per settings edit.
    assert live._released is True
    # Private IPs short-circuit before any HTTP, so this is a safe smoke call.
    assert await provider.locate("10.0.0.1") is None


class _GatedGeoProvider:
    """GeoIP adapter stand-in that parks inside ``locate`` until released.

    One gate per IP so a test can finish one in-flight lookup while another
    is still holding the adapter.
    """

    def __init__(self) -> None:
        self.released = False
        self.entered = asyncio.Event()
        self._gates: dict[str, asyncio.Event] = {}

    def gate(self, ip: str) -> asyncio.Event:
        return self._gates.setdefault(ip, asyncio.Event())

    def release(self) -> None:
        self.released = True

    async def locate(self, ip: str):  # noqa: ANN201 - stub
        gate = self.gate(ip)
        self.entered.set()
        await gate.wait()
        return None


def _stub_builder(monkeypatch, providers: list[_GatedGeoProvider]) -> None:
    from kokoro_link.bootstrap import site_settings_providers as module

    handed = iter(providers)
    monkeypatch.setattr(
        module, "build_geo_location_provider", lambda settings: next(handed),
    )


@pytest.mark.asyncio
async def test_a_superseded_geo_provider_survives_an_in_flight_lookup(
    monkeypatch,
) -> None:
    """Releasing on the spot closed the HTTP client out from under a lookup
    that was still awaiting it — a settings edit turned concurrent logins
    into ``ClientClosed`` errors."""
    first, second = _GatedGeoProvider(), _GatedGeoProvider()
    _stub_builder(monkeypatch, [first, second])
    holder = SiteSettingsHolder(_snapshot())
    provider = ReloadableGeoLocationProvider(holder=holder)

    lookup = asyncio.create_task(provider.locate("8.8.8.8"))
    await first.entered.wait()

    snapshot = holder.snapshot
    holder.replace(
        SiteSettingsSnapshot(
            weather=snapshot.weather,
            calendar=snapshot.calendar,
            geoip=GeoIpSettings(enabled=False),
            world_events=snapshot.world_events,
        ),
    )
    assert provider.inner is second
    assert first.released is False, "closed while a lookup still held it"

    first.gate("8.8.8.8").set()
    await lookup
    assert first.released is True


@pytest.mark.asyncio
async def test_the_release_waits_for_every_in_flight_lookup(
    monkeypatch,
) -> None:
    """Two overlapping lookups: the drain happens when the *last* one ends."""
    first, second = _GatedGeoProvider(), _GatedGeoProvider()
    _stub_builder(monkeypatch, [first, second])
    holder = SiteSettingsHolder(_snapshot())
    provider = ReloadableGeoLocationProvider(holder=holder)

    slow = asyncio.create_task(provider.locate("8.8.8.8"))
    await first.entered.wait()
    quick = asyncio.create_task(provider.locate("1.1.1.1"))
    await asyncio.sleep(0)

    snapshot = holder.snapshot
    holder.replace(
        SiteSettingsSnapshot(
            weather=snapshot.weather,
            calendar=snapshot.calendar,
            geoip=GeoIpSettings(enabled=False),
            world_events=snapshot.world_events,
        ),
    )
    assert provider.inner is second

    first.gate("1.1.1.1").set()
    await quick
    assert first.released is False, "closed while the slow lookup still held it"

    first.gate("8.8.8.8").set()
    await slow
    assert first.released is True


@pytest.mark.asyncio
async def test_rss_sync_resolves_the_deployment_region_when_it_runs() -> None:
    """The region accessor, not a wiring-time copy of it."""
    holder = SiteSettingsHolder(_snapshot())
    service = RssSourceSyncService(
        repository=object(),  # never touched: only _resolve_seed_enabled is exercised
        seed_path=__import__("pathlib").Path("does-not-exist.yaml"),
        deployment_region=holder.calendar_region,
    )
    emergency = {"category": "emergency", "region": "JP", "enabled": True}
    assert service._resolve_seed_enabled(emergency) is False  # site region is TW

    snapshot = holder.snapshot
    holder.replace(
        SiteSettingsSnapshot(
            weather=snapshot.weather,
            calendar=CalendarSettings(region="JP"),
            geoip=snapshot.geoip,
            world_events=snapshot.world_events,
        ),
    )
    assert service._resolve_seed_enabled(emergency) is True


def test_rss_sync_still_accepts_a_plain_region_string() -> None:
    """Self-host / existing call sites keep passing a code, not an accessor."""
    service = RssSourceSyncService(
        repository=object(),
        seed_path=__import__("pathlib").Path("does-not-exist.yaml"),
        deployment_region="jp",
    )
    assert service._resolve_seed_enabled(
        {"category": "emergency", "region": "JP", "enabled": True},
    ) is True


# --- write path notifies -----------------------------------------------------


class _Notifier:
    def __init__(self) -> None:
        self.groups: list[str] = []
        self.raises = False

    async def __call__(self, group: str) -> None:
        if self.raises:
            raise RuntimeError("notify transport down")
        self.groups.append(group)


class _RecordingRepository(InMemoryRuntimeSettingsRepository):
    """Records the order of durable writes so we can assert notify-after-write."""

    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self._log = log

    async def set(self, key: str, value: str) -> None:
        await super().set(key, value)
        self._log.append(f"write:{key}")


@pytest.mark.asyncio
async def test_set_notifies_after_the_durable_write() -> None:
    order: list[str] = []
    repo = _RecordingRepository(order)

    async def notify(group: str) -> None:
        order.append(f"notify:{group}")

    service = AppRuntimeSettingsService(repo, on_changed=notify)
    await service.set("weather", {"latitude": 35.0, "longitude": 139.0})
    assert order == ["write:site.weather", "notify:weather"]


@pytest.mark.asyncio
async def test_set_does_not_notify_when_validation_fails() -> None:
    notifier = _Notifier()
    service = AppRuntimeSettingsService(
        InMemoryRuntimeSettingsRepository(), on_changed=notifier,
    )
    with pytest.raises(Exception):
        await service.set("weather", {"latitude": 35.0})  # lon missing
    assert notifier.groups == []


@pytest.mark.asyncio
async def test_a_failed_notification_never_fails_the_save() -> None:
    """A dropped wake hint costs latency; the committed row is the durable truth."""
    notifier = _Notifier()
    notifier.raises = True
    repo = InMemoryRuntimeSettingsRepository()
    service = AppRuntimeSettingsService(repo, on_changed=notifier)
    saved = await service.set("weather", {"latitude": 1.0, "longitude": 2.0})
    assert saved.latitude == 1.0
    assert await repo.get(key_for_group("weather")) is not None


# --- reloader ----------------------------------------------------------------


def _reloader(holder: SiteSettingsHolder, repo, env: AppSettings):
    return build_site_settings_reloader(
        holder=holder, repository=repo, env_settings=env,
    )


@pytest.mark.asyncio
async def test_reload_picks_up_a_change_written_by_another_process() -> None:
    env = AppSettings()
    repo = InMemoryRuntimeSettingsRepository()
    holder = SiteSettingsHolder(SiteSettingsSnapshot.from_app_settings(env))
    reload = _reloader(holder, repo, env)

    # Another process (or another replica) writes the row.
    writer = AppRuntimeSettingsService(repo)
    await writer.set("weather", {"latitude": 35.68, "longitude": 139.69})
    await writer.set("calendar", {"region": "JP"})

    assert holder.weather().latitude is None  # still on its boot snapshot
    await reload()
    assert holder.weather().latitude == 35.68
    assert holder.calendar_region() == "JP"
    assert holder.overlay_source == "db"


@pytest.mark.asyncio
async def test_reload_falls_back_to_env_per_group_when_a_row_is_absent() -> None:
    """The reloader must resolve against ENV defaults, not its own last value."""
    env = AppSettings(weather=WeatherSettings(latitude=1.0, longitude=2.0))
    repo = InMemoryRuntimeSettingsRepository()
    holder = SiteSettingsHolder(SiteSettingsSnapshot.from_app_settings(env))
    reload = _reloader(holder, repo, env)
    await AppRuntimeSettingsService(repo).set(
        "weather", {"latitude": 35.0, "longitude": 139.0},
    )
    await reload()
    assert holder.weather().latitude == 35.0
    # Row disappears (operator wiped the KV) → back to the env value, not 35.0.
    repo._values.pop(key_for_group("weather"), None)  # type: ignore[attr-defined]
    await reload()
    assert holder.weather().latitude == 1.0


@pytest.mark.asyncio
async def test_a_failed_reload_keeps_the_last_good_snapshot() -> None:
    """Never silently downgrade a converged process to env defaults."""
    env = AppSettings()
    repo = InMemoryRuntimeSettingsRepository()
    holder = SiteSettingsHolder(SiteSettingsSnapshot.from_app_settings(env))
    reload = _reloader(holder, repo, env)
    await AppRuntimeSettingsService(repo).set(
        "weather", {"latitude": 35.0, "longitude": 139.0},
    )
    await reload()
    assert holder.weather().latitude == 35.0

    async def _boom(_key: str) -> str | None:
        raise RuntimeError("db down")

    repo.get = _boom  # type: ignore[method-assign]
    refresher = RuntimeConfigRefresher(
        refresh=reload, fingerprint=_Fingerprint("v2"), poll_interval=300.0,
    )
    assert await refresher.refresh_once(force=True) is False
    assert holder.weather().latitude == 35.0  # unchanged, not reset to env


# --- refresher wiring + topic routing ---------------------------------------


class _Fingerprint:
    def __init__(self, value: str | None = "v1") -> None:
        self.value = value

    async def __call__(self) -> str | None:
        return self.value


class _TopicSession:
    """A LISTEN session that hands out payloads (the shared-channel shape)."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def wait(self) -> str | None:
        return await self.queue.get()

    async def close(self) -> None:
        self.closed = True


async def _drive(refresher: RuntimeConfigRefresher, calls, until: int) -> None:
    for _ in range(200):
        await asyncio.sleep(0.005)
        if calls.count >= until:
            return


class _Counter:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


@pytest.mark.asyncio
async def test_a_foreign_topic_does_not_force_a_refresh() -> None:
    """Routing, not tagging: a site-settings write must not force a provider
    re-sync on every process (that writes a runtime-status row per provider)."""
    calls = _Counter()
    session = _TopicSession()

    async def factory():
        return session

    refresher = RuntimeConfigRefresher(
        refresh=calls,
        fingerprint=_Fingerprint("v1"),
        listen_factory=factory,
        poll_interval=300.0,
        topics=frozenset({RUNTIME_CONFIG_TOPIC_PROVIDERS}),
    )
    await refresher.start()
    try:
        assert calls.count == 1  # boot sync
        session.queue.put_nowait(RUNTIME_CONFIG_TOPIC_SITE_SETTINGS)
        await asyncio.sleep(0.05)
        assert calls.count == 1  # ignored
        session.queue.put_nowait(RUNTIME_CONFIG_TOPIC_PROVIDERS)
        await _drive(refresher, calls, 2)
        assert calls.count == 2
    finally:
        await refresher.stop()


@pytest.mark.asyncio
async def test_an_unknown_payload_still_wakes_us() -> None:
    """Fail-open: a missed wake is a correctness bug, a spurious one is free."""
    calls = _Counter()
    session = _TopicSession()

    async def factory():
        return session

    refresher = RuntimeConfigRefresher(
        refresh=calls,
        fingerprint=_Fingerprint("v1"),
        listen_factory=factory,
        poll_interval=300.0,
        topics=frozenset({RUNTIME_CONFIG_TOPIC_SITE_SETTINGS}),
    )
    await refresher.start()
    try:
        session.queue.put_nowait(None)  # transport did not surface a payload
        await _drive(refresher, calls, 2)
        assert calls.count == 2
    finally:
        await refresher.stop()


@pytest.mark.asyncio
async def test_fingerprint_poll_converges_when_the_notification_is_lost() -> None:
    """The safety net: NOTIFY is only ever a latency optimisation."""
    env = AppSettings()
    repo = InMemoryRuntimeSettingsRepository()
    holder = SiteSettingsHolder(SiteSettingsSnapshot.from_app_settings(env))
    fingerprint = _Fingerprint("v1")
    refresher = RuntimeConfigRefresher(
        refresh=_reloader(holder, repo, env),
        fingerprint=fingerprint,
        poll_interval=300.0,
    )
    await refresher.start()  # boot sync + baseline at v1
    try:
        # Another replica writes and its NOTIFY is dropped on the floor.
        await AppRuntimeSettingsService(repo).set("calendar", {"region": "US"})
        assert holder.calendar_region() == "TW"
        fingerprint.value = "v2"  # the poll notices the table moved
        assert await refresher.refresh_once(force=False) is True
        assert holder.calendar_region() == "US"
    finally:
        await refresher.stop()


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEngine:
    def __init__(self, name: str) -> None:
        self.dialect = _FakeDialect(name)


async def _noop() -> None:
    return None


def test_wiring_builds_on_postgres_and_routes_only_its_own_topic() -> None:
    refresher = build_site_settings_refresher(
        engine=_FakeEngine("postgresql"),
        database_url="postgresql+asyncpg://u:p@h/db",
        reload=_noop,
    )
    assert isinstance(refresher, RuntimeConfigRefresher)
    assert refresher._wants(RUNTIME_CONFIG_TOPIC_SITE_SETTINGS) is True
    assert refresher._wants(RUNTIME_CONFIG_TOPIC_PROVIDERS) is False


# --- fingerprint --------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_moves_on_a_value_change_but_not_on_a_no_op_save() -> None:
    """Hashes VALUES, not ``updated_at``.

    The admin form PUTs the whole blob on every save, so a timestamp-based
    fingerprint (what ``provider_connections`` uses) would turn every no-op save
    into a fleet-wide reload. Four tiny JSON blobs cost no more to read than
    their timestamps.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from kokoro_link.infrastructure.persistence.runtime_config_signal import (
        build_site_settings_fingerprint_reader,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE app_runtime_settings "
                    "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)",
                ),
            )
            await conn.execute(
                text(
                    "INSERT INTO app_runtime_settings VALUES "
                    "(:k, :v, :t)",
                ),
                {"k": "site.weather", "v": '{"latitude": 25.0}', "t": "t0"},
            )
        read = build_site_settings_fingerprint_reader(
            engine, keys=["site.weather", "site.calendar"],
        )
        first = await read()
        assert first is not None

        # Re-save with the same value, newer timestamp → same fingerprint.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE app_runtime_settings SET updated_at = :t "
                    "WHERE key = :k",
                ),
                {"t": "t1", "k": "site.weather"},
            )
        assert await read() == first

        # A real value change moves it.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE app_runtime_settings SET value = :v WHERE key = :k"),
                {"v": '{"latitude": 35.0}', "k": "site.weather"},
            )
        assert await read() != first

        # A group outside the watched key set is invisible.
        moved = await read()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO app_runtime_settings VALUES (:k, :v, :t)"),
                {"k": "site.nsfw", "v": '{"ttl_seconds": 60}', "t": "t2"},
            )
        assert await read() == moved
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fingerprint_read_failure_reports_unknown_not_unchanged() -> None:
    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("db down")

    from kokoro_link.infrastructure.persistence.runtime_config_signal import (
        build_site_settings_fingerprint_reader,
    )

    read = build_site_settings_fingerprint_reader(
        _BrokenEngine(), keys=["site.weather"],
    )
    assert await read() is None


# --- boot overlay provenance -------------------------------------------------


def test_boot_overlay_reports_env_fallback_when_there_is_no_database() -> None:
    from kokoro_link.bootstrap.app_runtime_settings_seed import (
        resolve_site_settings_overlay,
    )

    result = resolve_site_settings_overlay(AppSettings())
    assert result.source == "env_fallback"
    assert result.settings is AppSettings() or result.settings == AppSettings()


def test_boot_overlay_reports_env_fallback_when_the_read_fails(caplog) -> None:
    """Fail-soft, but no longer silent: this used to be indistinguishable from
    "the operator never changed anything"."""
    import logging

    from kokoro_link.bootstrap.app_runtime_settings_seed import (
        resolve_site_settings_overlay,
    )

    settings = AppSettings(database_url="definitely-not-a-database-url")
    with caplog.at_level(logging.WARNING):
        result = resolve_site_settings_overlay(settings)
    assert result.source == "env_fallback"
    assert result.settings == settings  # env values preserved, app still boots
    assert any(
        "site settings DB overlay FAILED" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_boot_overlay_sync_runner_uses_a_thread_inside_running_loop() -> None:
    """FastAPI lifespan builds the sync container while its loop is running."""
    import threading

    from kokoro_link.bootstrap.app_runtime_settings_seed import (
        _run_coroutine_factory_blocking,
    )

    caller_thread = threading.get_ident()

    async def read() -> tuple[int, bool]:
        await asyncio.sleep(0)
        return threading.get_ident(), asyncio.get_running_loop().is_running()

    worker_thread, loop_was_running = _run_coroutine_factory_blocking(read)

    assert worker_thread != caller_thread
    assert loop_was_running is True


# --- self-host parity (single process, no PostgreSQL) ------------------------


def _selfhost_env(monkeypatch) -> None:
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    monkeypatch.setenv("CONFIG_ENCRYPTION_KEY", "unit-test-site-settings-key")


def test_selfhost_put_converges_this_process_without_any_transport(
    monkeypatch,
) -> None:
    """One process IS everyone: the admin write path reloads it inline.

    No PostgreSQL means no LISTEN and no refresher (the NOTIFY is an inert
    no-op), so the local half of ``_converge`` is the entire mechanism — and it
    is what makes self-host strictly better off than the restart-to-apply
    behaviour it replaces, not merely unchanged.
    """
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _selfhost_env(monkeypatch)
    app = create_app()
    client = TestClient(app)
    holder = app.state.container.site_settings_holder
    assert holder is not None
    assert holder.calendar_region() == "TW"

    assert client.put(
        "/api/v1/admin/app-settings/calendar", json={"region": "JP"},
    ).status_code == 200
    assert holder.calendar_region() == "JP"

    assert client.put(
        "/api/v1/admin/app-settings/weather",
        json={"enabled": True, "latitude": 35.68, "longitude": 139.69},
    ).status_code == 200
    assert holder.weather().latitude == 35.68


def test_selfhost_put_of_a_cold_group_does_not_touch_the_holder(monkeypatch) -> None:
    """Scope guard: only the four real-world groups are hot (owner decision)."""
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _selfhost_env(monkeypatch)
    app = create_app()
    client = TestClient(app)
    holder = app.state.container.site_settings_holder
    before = holder.snapshot

    assert client.put(
        "/api/v1/admin/app-settings/nsfw", json={"ttl_seconds": 600},
    ).status_code == 200
    assert holder.snapshot is before


def test_health_surfaces_the_overlay_provenance(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from kokoro_link.api.app import create_app

    _selfhost_env(monkeypatch)
    client = TestClient(create_app())
    body = client.get("/health").json()
    assert body["status"] == "ok"
    # No database in this shape → the settings came from env, and the probe
    # says so instead of leaving the operator to guess.
    assert body["site_settings_overlay"] == "env_fallback"


def test_wiring_skips_sqlite_no_database_and_a_container_without_a_holder() -> None:
    """Self-host red line: one process converges itself on the write path."""
    assert build_site_settings_refresher(
        engine=_FakeEngine("sqlite"),
        database_url="sqlite+aiosqlite:///./x.db",
        reload=_noop,
    ) is None
    assert build_site_settings_refresher(
        engine=None, database_url="", reload=_noop,
    ) is None
    assert build_site_settings_refresher(
        engine=_FakeEngine("postgresql"), database_url="x", reload=None,
    ) is None
