from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DevLaunchConfig:
    project_root: Path
    host: str = "127.0.0.1"
    port: int = 8000
    lmstudio_model: str | None = None

    @property
    def app_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"


def build_server_command(config: DevLaunchConfig, python_executable: Path) -> list[str]:
    return [
        str(python_executable).replace("\\", "/"),
        "-m",
        "uvicorn",
        "kokoro_link.api.app:create_app",
        "--factory",
        "--reload",
        "--host",
        config.host,
        "--port",
        str(config.port),
    ]


def build_server_environment(
    config: DevLaunchConfig,
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(base_environment or {})
    if config.lmstudio_model:
        environment["KOKORO_LMSTUDIO_MODEL"] = config.lmstudio_model
        environment.setdefault("KOKORO_DEFAULT_PROVIDER_ID", "lmstudio")
    return environment
