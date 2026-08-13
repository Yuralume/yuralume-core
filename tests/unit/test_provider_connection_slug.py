"""Several connections of the SAME provider preset, told apart by slug.

Registry-keyed capabilities (llm / image / video) identify a runtime
adapter by ``provider_id``. That id used to be the catalog preset id
verbatim, so a second row of the same preset silently overwrote the
first one at sync time: both rows saved fine, both showed green in the
admin UI, and only the most recently created one actually served
traffic. Operators hit this with relay vendors that split models across
several API keys — one ``custom_openai_compatible`` row is not enough.

The optional ``connection_slug`` config field gives each row its own
runtime id. A blank slug keeps the historical id (``custom_openai_compatible``)
so stored preferences / character overrides / NSFW targets keep
resolving, and a collision is now refused at save time instead of
silently swallowed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from tests.unit.test_provider_settings import _configure_env


def _row(provider: str, config: dict, *, capabilities=("llm",)):
    from kokoro_link.contracts.provider_settings import ProviderConnection

    return ProviderConnection(
        id="row-id",
        provider=provider,
        label="label",
        enabled=True,
        capabilities=tuple(capabilities),
        config=config,
    )


# ---------------------------------------------------------------------------
# slug normalisation + runtime id derivation
# ---------------------------------------------------------------------------


def test_normalize_connection_slug_rules() -> None:
    from kokoro_link.infrastructure.provider_settings.runtime_ids import (
        normalize_connection_slug,
    )

    assert normalize_connection_slug("relay-b") == "relay-b"
    assert normalize_connection_slug("  Relay A  ") == "relay-a"
    assert normalize_connection_slug("Relay__B") == "relay-b"
    assert normalize_connection_slug("a//b") == "a-b"
    assert normalize_connection_slug("--a--") == "a"
    assert normalize_connection_slug("") == ""
    # Non-ASCII collapses to empty rather than producing a unicode runtime
    # id; the service turns that into an explicit save-time error so the
    # operator never gets a silently slug-less row.
    assert normalize_connection_slug("中轉甲") == ""
    assert len(normalize_connection_slug("x" * 80)) == 32


def test_runtime_provider_id_blank_slug_keeps_preset_id() -> None:
    from kokoro_link.infrastructure.provider_settings.runtime_ids import (
        runtime_provider_id,
    )

    assert runtime_provider_id(_row("custom_openai_compatible", {})) == (
        "custom_openai_compatible"
    )
    assert runtime_provider_id(
        _row("custom_openai_compatible", {"connection_slug": "   "}),
    ) == "custom_openai_compatible"


def test_runtime_provider_id_with_slug_is_scoped_to_the_row() -> None:
    from kokoro_link.infrastructure.provider_settings.runtime_ids import (
        runtime_provider_id,
    )

    assert runtime_provider_id(
        _row("custom_openai_compatible", {"connection_slug": "Relay B"}),
    ) == "custom_openai_compatible__relay-b"


def test_connection_slug_offered_on_registry_keyed_capabilities_only() -> None:
    """The field belongs to capabilities whose registry keys by id
    (llm / image / video). tts / embedding / search mount exactly one
    backend (most-recently-updated wins), so a slug there would imply a
    parallelism the runtime does not have."""
    from kokoro_link.infrastructure.provider_settings.catalog import catalog_by_id
    from kokoro_link.infrastructure.provider_settings.runtime_ids import (
        CONNECTION_SLUG_FIELD_KEY,
        IDENTITY_SCOPED_CAPABILITIES,
    )

    catalog = catalog_by_id()
    for entry in catalog.values():
        keys = {field.key for field in entry.config_fields}
        offered = CONNECTION_SLUG_FIELD_KEY in keys
        if entry.id == "yuralume_cloud":
            # One hosted deployment = one connection; a second row would
            # be a second billing identity, not a second adapter.
            assert not offered
            continue
        expected = bool(set(entry.capabilities) & IDENTITY_SCOPED_CAPABILITIES)
        assert offered is expected, entry.id


# ---------------------------------------------------------------------------
# end-to-end: two rows of one preset both reach the runtime
# ---------------------------------------------------------------------------


def _create_llm(client: TestClient, *, label: str, model: str, slug: str | None):
    config = {
        "base_url": "https://relay.example.test/v1",
        "default_model": model,
    }
    if slug is not None:
        config["connection_slug"] = slug
    return client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "custom_openai_compatible",
            "label": label,
            "enabled": True,
            "capabilities": ["llm"],
            "config": config,
            "secret": {"api_key": f"sk-{label}"},
        },
    )


def test_two_custom_openai_compatible_rows_both_register(monkeypatch) -> None:
    _configure_env(monkeypatch)
    app = create_app()
    client = TestClient(app)

    first = _create_llm(client, label="Relay A", model="model-a", slug=None)
    assert first.status_code == 201
    second = _create_llm(client, label="Relay B", model="model-b", slug="relay-b")
    assert second.status_code == 201

    providers = client.get("/api/v1/system/providers").json()
    assert "custom_openai_compatible" in providers
    assert "custom_openai_compatible__relay-b" in providers

    registry = app.state.container.model_registry
    payload_a = registry.resolve("custom_openai_compatible")._build_payload("hi")  # noqa: SLF001
    payload_b = registry.resolve(
        "custom_openai_compatible__relay-b",
    )._build_payload("hi")  # noqa: SLF001
    assert payload_a["model"] == "model-a"
    assert payload_b["model"] == "model-b"


def test_second_row_without_slug_is_refused_at_save_time(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    assert _create_llm(
        client, label="Relay A", model="model-a", slug=None,
    ).status_code == 201
    clash = _create_llm(client, label="Relay B", model="model-b", slug=None)
    assert clash.status_code == 400
    assert "connection_slug" in clash.json()["detail"]


def test_duplicate_slug_is_refused(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    assert _create_llm(
        client, label="Relay A", model="model-a", slug="relay",
    ).status_code == 201
    clash = _create_llm(client, label="Relay B", model="model-b", slug="Relay")
    assert clash.status_code == 400


def test_non_ascii_slug_is_refused_rather_than_silently_dropped(
    monkeypatch,
) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    refused = _create_llm(client, label="Relay", model="m", slug="中轉甲")
    assert refused.status_code == 400
    assert "connection_slug" in refused.json()["detail"]


def test_editing_a_row_does_not_collide_with_itself(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    created = _create_llm(client, label="Relay A", model="model-a", slug="relay-a")
    assert created.status_code == 201
    updated = client.patch(
        f"/api/v1/admin/providers/{created.json()['id']}",
        json={
            "config": {
                "base_url": "https://relay.example.test/v1",
                "default_model": "model-a2",
                "connection_slug": "relay-a",
            },
        },
    )
    assert updated.status_code == 200


def test_rows_of_different_presets_never_collide(monkeypatch) -> None:
    """The uniqueness check is scoped to one preset — an ``openai`` row and
    a ``custom_openai_compatible`` row have different ids by construction."""
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    assert _create_llm(
        client, label="Relay A", model="model-a", slug=None,
    ).status_code == 201
    other = client.post(
        "/api/v1/admin/providers",
        json={
            "provider": "openai",
            "label": "OpenAI",
            "enabled": True,
            "capabilities": ["llm"],
            "config": {"default_model": "gpt-4o-mini"},
            "secret": {"api_key": "sk-openai"},
        },
    )
    assert other.status_code == 201


def test_capabilities_that_mount_one_backend_still_allow_standby_rows(
    monkeypatch,
) -> None:
    """tts/embedding/search keep their active+standby semantics: two rows of
    the same preset are legitimate there, so the uniqueness check must not
    reach them."""
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    for label in ("TTS A", "TTS B"):
        created = client.post(
            "/api/v1/admin/providers",
            json={
                "provider": "openai",
                "label": label,
                "enabled": True,
                "capabilities": ["tts"],
                "config": {"tts_model": "gpt-4o-mini-tts"},
                "secret": {"api_key": "sk-tts"},
            },
        )
        assert created.status_code == 201, label


def test_admin_response_exposes_the_runtime_id(monkeypatch) -> None:
    """The id a row occupies at runtime is what the model selector lists,
    so the admin row has to show it — otherwise the operator has to guess
    which entry in the picker belongs to which connection."""
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    plain = _create_llm(client, label="Relay A", model="model-a", slug=None)
    assert plain.json()["runtime_provider_id"] == "custom_openai_compatible"
    slugged = _create_llm(client, label="Relay B", model="model-b", slug="Relay B")
    assert slugged.json()["runtime_provider_id"] == (
        "custom_openai_compatible__relay-b"
    )

    listed = client.get("/api/v1/admin/providers").json()
    assert {row["runtime_provider_id"] for row in listed} == {
        "custom_openai_compatible",
        "custom_openai_compatible__relay-b",
    }


def test_two_image_rows_of_one_preset_both_mount(monkeypatch) -> None:
    _configure_env(monkeypatch)
    client = TestClient(create_app())

    for label, slug, model in (
        ("Grok A", None, "grok-imagine-image-quality"),
        ("Grok B", "grok-b", "grok-2-image-1212"),
    ):
        config = {"default_model": model}
        if slug is not None:
            config["connection_slug"] = slug
        created = client.post(
            "/api/v1/admin/providers",
            json={
                "provider": "xai",
                "label": label,
                "enabled": True,
                "capabilities": ["image"],
                "config": config,
                "secret": {"api_key": "xai-secret"},
            },
        )
        assert created.status_code == 201, label

    profile_ids = [p["id"] for p in client.get("/api/v1/system/image-profiles").json()]
    assert profile_ids == ["xai", "xai__grok-b"]
