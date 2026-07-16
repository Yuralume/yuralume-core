from pathlib import Path

from kokoro_link.bootstrap.dev_launcher import (
    DevLaunchConfig,
    build_server_command,
    build_server_environment,
)


def test_build_server_command_uses_uvicorn_factory_app() -> None:
    config = DevLaunchConfig(project_root=Path("C:/workspace/Yuralume"), host="127.0.0.1", port=9000)

    command = build_server_command(config, Path("C:/workspace/Yuralume/.venv/Scripts/python.exe"))

    assert command == [
        "C:/workspace/Yuralume/.venv/Scripts/python.exe",
        "-m",
        "uvicorn",
        "kokoro_link.api.app:create_app",
        "--factory",
        "--reload",
        "--host",
        "127.0.0.1",
        "--port",
        "9000",
    ]


def test_build_server_environment_prefers_lmstudio_when_model_is_provided() -> None:
    config = DevLaunchConfig(
        project_root=Path("C:/workspace/Yuralume"),
        lmstudio_model="gemma-4-31b-it-uncensored",
    )

    environment = build_server_environment(config, {"PATH": "base"})

    assert environment["PATH"] == "base"
    assert environment["KOKORO_LMSTUDIO_MODEL"] == "gemma-4-31b-it-uncensored"
    assert environment["KOKORO_DEFAULT_PROVIDER_ID"] == "lmstudio"
