"""Configuration and environment loading.

Lithos Lens is configured by a TOML file (``lithos-lens.toml``). This module
defines the in-memory representation of that file, validates it on load, and
applies a small set of environment-variable overrides so that env beats
file beats built-in default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import load_dotenv

from lithos_lens.errors import ConfigError
from lithos_lens.tasks import TASK_STATUSES, TaskStatusName

__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_ENVIRONMENT",
    "DEFAULT_GREETING",
    "DEFAULT_LOG_LEVEL",
    "EventsConfig",
    "ConfigError",
    "HealthConfig",
    "LithosLensConfig",
    "LithosConfig",
    "LLMConfig",
    "LogLevel",
    "LoggingConfig",
    "StorageConfig",
    "TelemetryConfig",
    "TasksConfig",
    "UIConfig",
    "find_config_path",
    "load_config",
    "parse_log_level",
]

# ── Literal types + validators ─────────────────────────────────────────

LogLevel = Literal["debug", "info", "warning", "error"]

_VALID_LOG_LEVEL: set[str] = {"debug", "info", "warning", "error"}


# ── Defaults ───────────────────────────────────────────────────────────

DEFAULT_DATA_DIR = Path.home() / ".lithos-lens" / "data"
DEFAULT_ENVIRONMENT = "dev"
DEFAULT_GREETING = "Hello"
DEFAULT_LOG_LEVEL: LogLevel = "info"
DEFAULT_LITHOS_URL = "http://localhost:8765"
DEFAULT_LITHOS_MCP_SSE_PATH = "/sse"
DEFAULT_LITHOS_SSE_EVENTS_PATH = "/events"
DEFAULT_LENS_AGENT_ID = "lithos-lens"
DEFAULT_TASKS_AUTO_REFRESH_INTERVAL_S = 30
DEFAULT_TASKS_VISIBLE_CAP = 50
DEFAULT_TASKS_DEFAULT_TIME_RANGE_DAYS = 30
DEFAULT_LLM_MAX_TOKENS = 2048
DEFAULT_HEALTH_REFRESH_INTERVAL_S = 30


def parse_log_level(value: str) -> LogLevel:
    """Validate and narrow a string to a ``LogLevel`` literal."""
    if value not in _VALID_LOG_LEVEL:
        raise ConfigError(
            f"Invalid log level {value!r}. Valid values: {sorted(_VALID_LOG_LEVEL)}"
        )
    return cast(LogLevel, value)


# ── Dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path = DEFAULT_DATA_DIR


@dataclass(frozen=True)
class LoggingConfig:
    level: LogLevel = DEFAULT_LOG_LEVEL


@dataclass(frozen=True)
class LithosConfig:
    url: str = DEFAULT_LITHOS_URL
    mcp_sse_path: str = DEFAULT_LITHOS_MCP_SSE_PATH
    sse_events_path: str = DEFAULT_LITHOS_SSE_EVENTS_PATH
    agent_id: str = DEFAULT_LENS_AGENT_ID


@dataclass(frozen=True)
class TasksConfig:
    auto_refresh_interval_s: int = DEFAULT_TASKS_AUTO_REFRESH_INTERVAL_S
    visible_cap: int = DEFAULT_TASKS_VISIBLE_CAP
    default_time_range_days: int = DEFAULT_TASKS_DEFAULT_TIME_RANGE_DAYS
    default_status_groups: tuple[TaskStatusName, ...] = TASK_STATUSES


@dataclass(frozen=True)
class EventsConfig:
    enabled: bool = True
    reconnect_backoff_ms: tuple[int, ...] = (500, 1000, 2000, 5000, 10000)


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool = False
    provider: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    extra_headers_json: str = ""
    max_tokens: int = DEFAULT_LLM_MAX_TOKENS


@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = False
    console_fallback: bool = False
    service_name: str = "lithos-lens"
    export_interval_ms: int = 30000


@dataclass(frozen=True)
class UIConfig:
    default_view: str = "tasks"


@dataclass(frozen=True)
class HealthConfig:
    refresh_interval_s: int = DEFAULT_HEALTH_REFRESH_INTERVAL_S


@dataclass(frozen=True)
class LithosLensConfig:
    environment: str
    greeting: str
    storage: StorageConfig
    logging: LoggingConfig
    lithos: LithosConfig
    tasks: TasksConfig
    events: EventsConfig
    llm: LLMConfig
    telemetry: TelemetryConfig
    ui: UIConfig
    health: HealthConfig


# ── Discovery and loading ──────────────────────────────────────────────


def _default_config_candidates() -> list[Path]:
    """Return the filesystem candidates checked when LITHOS_LENS_CONFIG is unset.

    Exposed as a helper so tests can monkeypatch the search locations
    without having to override HOME and /etc.
    """
    return [
        Path.cwd() / "lithos-lens.toml",
        Path.home() / ".lithos-lens" / "lithos-lens.toml",
        Path("/etc/lithos-lens/lithos-lens.toml"),
    ]


def find_config_path() -> Path:
    """Return the first existing ``lithos-lens.toml`` in the discovery order.

    Order: ``LITHOS_LENS_CONFIG`` env var, then ``./lithos-lens.toml``, then
    ``~/.lithos-lens/lithos-lens.toml``, then
    ``/etc/lithos-lens/lithos-lens.toml``.
    Raises ``ConfigError`` if none are found.
    """
    load_dotenv()
    explicit = os.environ.get("LITHOS_LENS_CONFIG", "")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise ConfigError(
                f"LITHOS_LENS_CONFIG points at {p}, but no file exists there"
            )
        return p

    candidates = _default_config_candidates()
    for p in candidates:
        if p.exists():
            return p

    joined = "\n  ".join(str(p) for p in candidates)
    raise ConfigError(
        "No lithos-lens.toml found. Set LITHOS_LENS_CONFIG or create one of:\n  "
        + joined
    )


def load_config(path: Path | None = None) -> LithosLensConfig:
    """Load, validate, and return a ``LithosLensConfig``.

    When ``path`` is ``None`` the config file is located via
    :func:`find_config_path`. Env-var overrides (``LITHOS_LENS_ENVIRONMENT``,
    ``LITHOS_LENS_DATA_DIR``, ``LITHOS_LENS_LOG_LEVEL``) are applied after file
    parsing so that env beats file beats built-in default.
    """
    load_dotenv()
    config_path = path if path is not None else find_config_path()

    try:
        with config_path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: invalid TOML: {exc}") from exc

    lithos_lens_section = raw.get("lithos-lens", {})
    if not isinstance(lithos_lens_section, dict):
        raise ConfigError(f"{config_path}: 'lithos-lens' must be a table")

    environment = _optional_str(
        lithos_lens_section,
        "environment",
        DEFAULT_ENVIRONMENT,
        config_path,
        "lithos-lens",
    )
    greeting = _optional_str(
        lithos_lens_section,
        "greeting",
        DEFAULT_GREETING,
        config_path,
        "lithos-lens",
    )
    storage = _parse_storage(lithos_lens_section.get("storage", {}), config_path)
    logging_cfg = _parse_logging(lithos_lens_section.get("logging", {}), config_path)
    lithos = _parse_lithos(lithos_lens_section.get("lithos", {}), config_path)
    tasks = _parse_tasks(lithos_lens_section.get("tasks", {}), config_path)
    events = _parse_events(lithos_lens_section.get("events", {}), config_path)
    llm = _parse_llm(lithos_lens_section.get("llm", {}), config_path)
    telemetry = _parse_telemetry(lithos_lens_section.get("telemetry", {}), config_path)
    ui = _parse_ui(lithos_lens_section.get("ui", {}), config_path)
    health = _parse_health(lithos_lens_section.get("health", {}), config_path)

    cfg = LithosLensConfig(
        environment=environment,
        greeting=greeting,
        storage=storage,
        logging=logging_cfg,
        lithos=lithos,
        tasks=tasks,
        events=events,
        llm=llm,
        telemetry=telemetry,
        ui=ui,
        health=health,
    )
    return _apply_env_overrides(cfg)


# ── Internal parsing helpers ───────────────────────────────────────────


def _parse_storage(data: Any, config_path: Path) -> StorageConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.storage] must be a table")
    data_dir = _optional_path(
        data, "data_dir", DEFAULT_DATA_DIR, config_path, "lithos-lens.storage"
    )
    return StorageConfig(data_dir=data_dir)


def _parse_logging(data: Any, config_path: Path) -> LoggingConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.logging] must be a table")
    level_raw = data.get("level", DEFAULT_LOG_LEVEL)
    if not isinstance(level_raw, str):
        raise ConfigError(
            f"{config_path}: [lithos-lens.logging].level must be a string"
        )
    try:
        level = parse_log_level(level_raw)
    except ConfigError as exc:
        raise ConfigError(f"{config_path}: [lithos-lens.logging]: {exc}") from exc
    return LoggingConfig(level=level)


def _parse_lithos(data: Any, config_path: Path) -> LithosConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.lithos] must be a table")
    return LithosConfig(
        url=_optional_str(
            data, "url", DEFAULT_LITHOS_URL, config_path, "lithos-lens.lithos"
        ),
        mcp_sse_path=_optional_str(
            data,
            "mcp_sse_path",
            DEFAULT_LITHOS_MCP_SSE_PATH,
            config_path,
            "lithos-lens.lithos",
        ),
        sse_events_path=_optional_str(
            data,
            "sse_events_path",
            DEFAULT_LITHOS_SSE_EVENTS_PATH,
            config_path,
            "lithos-lens.lithos",
        ),
        agent_id=_optional_str(
            data, "agent_id", DEFAULT_LENS_AGENT_ID, config_path, "lithos-lens.lithos"
        ),
    )


def _parse_tasks(data: Any, config_path: Path) -> TasksConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.tasks] must be a table")
    return TasksConfig(
        auto_refresh_interval_s=_optional_int(
            data,
            "auto_refresh_interval_s",
            DEFAULT_TASKS_AUTO_REFRESH_INTERVAL_S,
            config_path,
            "lithos-lens.tasks",
            minimum=1,
        ),
        visible_cap=_optional_int(
            data,
            "visible_cap",
            DEFAULT_TASKS_VISIBLE_CAP,
            config_path,
            "lithos-lens.tasks",
            minimum=1,
        ),
        default_time_range_days=_optional_int(
            data,
            "default_time_range_days",
            DEFAULT_TASKS_DEFAULT_TIME_RANGE_DAYS,
            config_path,
            "lithos-lens.tasks",
            minimum=1,
        ),
        default_status_groups=_optional_status_groups(
            data,
            "default_status_groups",
            TASK_STATUSES,
            config_path,
            "lithos-lens.tasks",
        ),
    )


def _parse_events(data: Any, config_path: Path) -> EventsConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.events] must be a table")
    backoff = data.get("reconnect_backoff_ms", EventsConfig().reconnect_backoff_ms)
    if not isinstance(backoff, (list, tuple)) or not all(
        isinstance(v, int) for v in backoff
    ):
        raise ConfigError(
            f"{config_path}: [lithos-lens.events].reconnect_backoff_ms "
            "must be a list of integers"
        )
    return EventsConfig(
        enabled=_optional_bool(
            data, "enabled", True, config_path, "lithos-lens.events"
        ),
        reconnect_backoff_ms=tuple(backoff),
    )


def _parse_llm(data: Any, config_path: Path) -> LLMConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.llm] must be a table")
    return LLMConfig(
        enabled=_optional_bool(data, "enabled", False, config_path, "lithos-lens.llm"),
        provider=_optional_str(data, "provider", "", config_path, "lithos-lens.llm"),
        model=_optional_str(data, "model", "", config_path, "lithos-lens.llm"),
        api_key=_optional_str(data, "api_key", "", config_path, "lithos-lens.llm"),
        base_url=_optional_str(data, "base_url", "", config_path, "lithos-lens.llm"),
        extra_headers_json=_optional_str(
            data, "extra_headers_json", "", config_path, "lithos-lens.llm"
        ),
        max_tokens=_optional_int(
            data,
            "max_tokens",
            DEFAULT_LLM_MAX_TOKENS,
            config_path,
            "lithos-lens.llm",
            minimum=1,
        ),
    )


def _parse_telemetry(data: Any, config_path: Path) -> TelemetryConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.telemetry] must be a table")
    return TelemetryConfig(
        enabled=_optional_bool(
            data, "enabled", False, config_path, "lithos-lens.telemetry"
        ),
        console_fallback=_optional_bool(
            data, "console_fallback", False, config_path, "lithos-lens.telemetry"
        ),
        service_name=_optional_str(
            data, "service_name", "lithos-lens", config_path, "lithos-lens.telemetry"
        ),
        export_interval_ms=_optional_int(
            data,
            "export_interval_ms",
            30000,
            config_path,
            "lithos-lens.telemetry",
            minimum=1,
        ),
    )


def _parse_ui(data: Any, config_path: Path) -> UIConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.ui] must be a table")
    return UIConfig(
        default_view=_optional_str(
            data, "default_view", "tasks", config_path, "lithos-lens.ui"
        )
    )


def _parse_health(data: Any, config_path: Path) -> HealthConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.health] must be a table")
    return HealthConfig(
        refresh_interval_s=_optional_int(
            data,
            "refresh_interval_s",
            DEFAULT_HEALTH_REFRESH_INTERVAL_S,
            config_path,
            "lithos-lens.health",
            minimum=1,
        )
    )


def _optional_str(
    data: dict[str, Any],
    key: str,
    default: str,
    config_path: Path,
    section: str,
) -> str:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a string")
    return value


def _optional_path(
    data: dict[str, Any],
    key: str,
    default: Path,
    config_path: Path,
    section: str,
) -> Path:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a string path")
    return Path(value).expanduser()


def _optional_int(
    data: dict[str, Any],
    key: str,
    default: int,
    config_path: Path,
    section: str,
    *,
    minimum: int | None = None,
) -> int:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, int):
        raise ConfigError(f"{config_path}: [{section}].{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{config_path}: [{section}].{key} must be >= {minimum}")
    return value


def _optional_bool(
    data: dict[str, Any],
    key: str,
    default: bool,
    config_path: Path,
    section: str,
) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a boolean")
    return value


def _optional_status_groups(
    data: dict[str, Any],
    key: str,
    default: tuple[TaskStatusName, ...],
    config_path: Path,
    section: str,
) -> tuple[TaskStatusName, ...]:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{config_path}: [{section}].{key} must be a list of strings")
    groups: list[TaskStatusName] = []
    for item in value:
        if item not in TASK_STATUSES:
            raise ConfigError(
                f"{config_path}: [{section}].{key} contains invalid status {item!r}"
            )
        if item not in groups:
            groups.append(cast(TaskStatusName, item))
    if not groups:
        raise ConfigError(f"{config_path}: [{section}].{key} must not be empty")
    return tuple(groups)


def _apply_env_overrides(cfg: LithosLensConfig) -> LithosLensConfig:
    env_override = os.environ.get("LITHOS_LENS_ENVIRONMENT", "")
    data_dir_override = os.environ.get("LITHOS_LENS_DATA_DIR", "")
    log_level_override = os.environ.get("LITHOS_LENS_LOG_LEVEL", "")
    lithos_url_override = os.environ.get("LITHOS_LENS_LITHOS_URL", "")
    lithos_mcp_sse_path_override = os.environ.get("LITHOS_LENS_MCP_SSE_PATH", "")
    lithos_events_path_override = os.environ.get("LITHOS_LENS_SSE_EVENTS_PATH", "")
    agent_id_override = os.environ.get("LITHOS_LENS_AGENT_ID", "")
    tasks_visible_cap_override = os.environ.get("LITHOS_LENS_TASKS_VISIBLE_CAP", "")
    llm_enabled_override = os.environ.get("LITHOS_LENS_LLM_ENABLED", "")
    llm_model_override = os.environ.get("LITHOS_LENS_LLM_MODEL", "")
    llm_provider_override = os.environ.get("LITHOS_LENS_LLM_PROVIDER", "")
    llm_api_key_override = os.environ.get("LITHOS_LENS_LLM_API_KEY", "")
    llm_base_url_override = os.environ.get("LITHOS_LENS_LLM_BASE_URL", "")
    llm_extra_headers_override = os.environ.get(
        "LITHOS_LENS_LLM_EXTRA_HEADERS_JSON", ""
    )
    llm_max_tokens_override = os.environ.get("LITHOS_LENS_LLM_MAX_TOKENS", "")
    telemetry_enabled_override = os.environ.get("LITHOS_LENS_OTEL_ENABLED", "")

    if not any(
        [
            env_override,
            data_dir_override,
            log_level_override,
            lithos_url_override,
            lithos_mcp_sse_path_override,
            lithos_events_path_override,
            agent_id_override,
            tasks_visible_cap_override,
            llm_enabled_override,
            llm_model_override,
            llm_provider_override,
            llm_api_key_override,
            llm_base_url_override,
            llm_extra_headers_override,
            llm_max_tokens_override,
            telemetry_enabled_override,
        ]
    ):
        return cfg

    new_cfg = cfg
    if env_override:
        new_cfg = replace(new_cfg, environment=env_override)
    if data_dir_override:
        new_storage = replace(
            new_cfg.storage, data_dir=Path(data_dir_override).expanduser()
        )
        new_cfg = replace(new_cfg, storage=new_storage)
    if log_level_override:
        new_logging = replace(
            new_cfg.logging, level=parse_log_level(log_level_override)
        )
        new_cfg = replace(new_cfg, logging=new_logging)
    if (
        lithos_url_override
        or lithos_mcp_sse_path_override
        or lithos_events_path_override
        or agent_id_override
    ):
        new_lithos = replace(
            new_cfg.lithos,
            url=lithos_url_override or new_cfg.lithos.url,
            mcp_sse_path=lithos_mcp_sse_path_override or new_cfg.lithos.mcp_sse_path,
            sse_events_path=lithos_events_path_override
            or new_cfg.lithos.sse_events_path,
            agent_id=agent_id_override or new_cfg.lithos.agent_id,
        )
        new_cfg = replace(new_cfg, lithos=new_lithos)
    if tasks_visible_cap_override:
        new_tasks = replace(
            new_cfg.tasks,
            visible_cap=_parse_env_int(
                "LITHOS_LENS_TASKS_VISIBLE_CAP", tasks_visible_cap_override
            ),
        )
        new_cfg = replace(new_cfg, tasks=new_tasks)
    if any(
        [
            llm_enabled_override,
            llm_model_override,
            llm_provider_override,
            llm_api_key_override,
            llm_base_url_override,
            llm_extra_headers_override,
            llm_max_tokens_override,
        ]
    ):
        new_llm = replace(
            new_cfg.llm,
            enabled=_parse_env_bool("LITHOS_LENS_LLM_ENABLED", llm_enabled_override)
            if llm_enabled_override
            else new_cfg.llm.enabled,
            provider=llm_provider_override or new_cfg.llm.provider,
            model=llm_model_override or new_cfg.llm.model,
            api_key=llm_api_key_override or new_cfg.llm.api_key,
            base_url=llm_base_url_override or new_cfg.llm.base_url,
            extra_headers_json=llm_extra_headers_override
            or new_cfg.llm.extra_headers_json,
            max_tokens=_parse_env_int(
                "LITHOS_LENS_LLM_MAX_TOKENS", llm_max_tokens_override
            )
            if llm_max_tokens_override
            else new_cfg.llm.max_tokens,
        )
        new_cfg = replace(new_cfg, llm=new_llm)
    if telemetry_enabled_override:
        new_telemetry = replace(
            new_cfg.telemetry,
            enabled=_parse_env_bool(
                "LITHOS_LENS_OTEL_ENABLED", telemetry_enabled_override
            ),
        )
        new_cfg = replace(new_cfg, telemetry=new_telemetry)
    return new_cfg


def _parse_env_int(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise ConfigError(f"{name} must be >= 1")
    return parsed


def _parse_env_bool(name: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean")
