"""Configuration loading: discovery, TOML parsing, and env overrides.

Lithos Lens is configured by a TOML file (``lithos-lens.toml``). This module
finds it, validates it on load, and applies a small set of
environment-variable overrides so that env beats file beats built-in default.
The typed shape it produces — the dataclasses, their defaults, and their
ceilings — lives in :mod:`lithos_lens.config_schema` and is re-exported here,
so ``from lithos_lens.config import LithosLensConfig`` (and every other name)
keeps working.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

from lithos_lens.config_fields import (
    optional_bool,
    optional_int,
    optional_path,
    optional_status_groups,
    optional_str,
)
from lithos_lens.config_schema import (
    DEFAULT_DATA_DIR,
    DEFAULT_ENVIRONMENT,
    DEFAULT_GREETING,
    DEFAULT_HEALTH_REFRESH_INTERVAL_S,
    DEFAULT_KNOWLEDGE_RECENT_LIMIT,
    DEFAULT_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP,
    DEFAULT_KNOWLEDGE_SEARCH_LIMIT,
    DEFAULT_LENS_AGENT_ID,
    DEFAULT_LITHOS_MCP_SSE_PATH,
    DEFAULT_LITHOS_SSE_EVENTS_PATH,
    DEFAULT_LITHOS_URL,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_TASKS_AUTO_REFRESH_INTERVAL_S,
    DEFAULT_TASKS_CLAIM_EXPIRING_SOON_MINUTES,
    DEFAULT_TASKS_DEFAULT_TIME_RANGE_DAYS,
    DEFAULT_TASKS_FRONTIER_LIMIT,
    DEFAULT_TASKS_GATE_WAITING_ATTENTION_HOURS,
    DEFAULT_TASKS_STALE_OPEN_AGE_DAYS,
    DEFAULT_TASKS_UNCLAIMED_READY_AGE_MINUTES,
    DEFAULT_TASKS_VISIBLE_CAP,
    MAX_KNOWLEDGE_LANDING_LIMIT,
    MAX_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP,
    MAX_TASKS_INT_KNOBS,
    EventsConfig,
    HealthConfig,
    KnowledgeConfig,
    LithosConfig,
    LithosLensConfig,
    LLMConfig,
    LoggingConfig,
    LogLevel,
    StorageConfig,
    TasksConfig,
    TelemetryConfig,
    UIConfig,
    parse_log_level,
)
from lithos_lens.errors import ConfigError
from lithos_lens.tasks import (
    DEFAULT_PROJECT_CONVENTION,
    DEFAULT_PROJECT_TAG_KEY,
    PROJECT_CONVENTIONS,
    TASK_STATUSES,
    ProjectConvention,
)

logger = logging.getLogger(__name__)

# One-time deprecation latch for [lithos-lens.tasks].visible_cap.
_VISIBLE_CAP_WARNED = False

# Re-export surface: `lithos_lens.config` stays the one import site for both
# the loader and the schema it produces (see config_schema).
__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_ENVIRONMENT",
    "DEFAULT_GREETING",
    "DEFAULT_HEALTH_REFRESH_INTERVAL_S",
    "DEFAULT_KNOWLEDGE_RECENT_LIMIT",
    "DEFAULT_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP",
    "DEFAULT_KNOWLEDGE_SEARCH_LIMIT",
    "DEFAULT_LENS_AGENT_ID",
    "DEFAULT_LITHOS_MCP_SSE_PATH",
    "DEFAULT_LITHOS_SSE_EVENTS_PATH",
    "DEFAULT_LITHOS_URL",
    "DEFAULT_LLM_MAX_TOKENS",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_TASKS_AUTO_REFRESH_INTERVAL_S",
    "DEFAULT_TASKS_CLAIM_EXPIRING_SOON_MINUTES",
    "DEFAULT_TASKS_DEFAULT_TIME_RANGE_DAYS",
    "DEFAULT_TASKS_FRONTIER_LIMIT",
    "DEFAULT_TASKS_GATE_WAITING_ATTENTION_HOURS",
    "DEFAULT_TASKS_STALE_OPEN_AGE_DAYS",
    "DEFAULT_TASKS_UNCLAIMED_READY_AGE_MINUTES",
    "DEFAULT_TASKS_VISIBLE_CAP",
    "MAX_KNOWLEDGE_LANDING_LIMIT",
    "MAX_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP",
    "MAX_TASKS_INT_KNOBS",
    "EventsConfig",
    "ConfigError",
    "HealthConfig",
    "KnowledgeConfig",
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

    environment = optional_str(
        lithos_lens_section,
        "environment",
        DEFAULT_ENVIRONMENT,
        config_path,
        "lithos-lens",
    )
    greeting = optional_str(
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
    knowledge = _parse_knowledge(lithos_lens_section.get("knowledge", {}), config_path)

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
        knowledge=knowledge,
    )
    return _apply_env_overrides(cfg)


# ── Internal parsing helpers ───────────────────────────────────────────


def _parse_storage(data: Any, config_path: Path) -> StorageConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.storage] must be a table")
    data_dir = optional_path(
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
        url=optional_str(
            data, "url", DEFAULT_LITHOS_URL, config_path, "lithos-lens.lithos"
        ),
        mcp_sse_path=optional_str(
            data,
            "mcp_sse_path",
            DEFAULT_LITHOS_MCP_SSE_PATH,
            config_path,
            "lithos-lens.lithos",
        ),
        sse_events_path=optional_str(
            data,
            "sse_events_path",
            DEFAULT_LITHOS_SSE_EVENTS_PATH,
            config_path,
            "lithos-lens.lithos",
        ),
        agent_id=optional_str(
            data, "agent_id", DEFAULT_LENS_AGENT_ID, config_path, "lithos-lens.lithos"
        ),
    )


def _parse_tasks(data: Any, config_path: Path) -> TasksConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.tasks] must be a table")
    global _VISIBLE_CAP_WARNED
    if "visible_cap" in data and not _VISIBLE_CAP_WARNED:
        _VISIBLE_CAP_WARNED = True
        logger.warning(
            "[lithos-lens.tasks].visible_cap is deprecated and unused since the "
            "graph-native dashboard (T1) — the live scale dial is "
            "frontier_limit (LITHOS_LENS_TASKS_FRONTIER_LIMIT). Remove "
            "visible_cap from %s.",
            config_path,
        )

    # Every [tasks] knob below is a positive integer parsed the same way, so
    # one local binding keeps the eight of them readable. Those whose value
    # ends up in a ``timedelta`` also carry a ceiling (see MAX_TASKS_INT_KNOBS,
    # which is the list — do not infer it from the key names). The rest are
    # unbounded: auto_refresh_interval_s (a browser poll interval),
    # frontier_limit (a row cap pushed upstream) and the deprecated
    # visible_cap.
    def positive_int(key: str, default: int) -> int:
        return optional_int(
            data,
            key,
            default,
            config_path,
            "lithos-lens.tasks",
            minimum=1,
            maximum=MAX_TASKS_INT_KNOBS.get(key),
        )

    return TasksConfig(
        auto_refresh_interval_s=positive_int(
            "auto_refresh_interval_s", DEFAULT_TASKS_AUTO_REFRESH_INTERVAL_S
        ),
        visible_cap=positive_int("visible_cap", DEFAULT_TASKS_VISIBLE_CAP),
        frontier_limit=positive_int("frontier_limit", DEFAULT_TASKS_FRONTIER_LIMIT),
        default_time_range_days=positive_int(
            "default_time_range_days", DEFAULT_TASKS_DEFAULT_TIME_RANGE_DAYS
        ),
        gate_waiting_attention_hours=positive_int(
            "gate_waiting_attention_hours", DEFAULT_TASKS_GATE_WAITING_ATTENTION_HOURS
        ),
        claim_expiring_soon_minutes=positive_int(
            "claim_expiring_soon_minutes", DEFAULT_TASKS_CLAIM_EXPIRING_SOON_MINUTES
        ),
        stale_open_age_days=positive_int(
            "stale_open_age_days", DEFAULT_TASKS_STALE_OPEN_AGE_DAYS
        ),
        unclaimed_ready_age_minutes=positive_int(
            "unclaimed_ready_age_minutes", DEFAULT_TASKS_UNCLAIMED_READY_AGE_MINUTES
        ),
        default_status_groups=optional_status_groups(
            data,
            "default_status_groups",
            TASK_STATUSES,
            config_path,
            "lithos-lens.tasks",
        ),
        project_convention=_project_convention(data, config_path),
        project_tag_key=_project_tag_key(data, config_path),
    )


def _project_convention(data: dict[str, Any], config_path: Path) -> ProjectConvention:
    value = optional_str(
        data,
        "project_convention",
        DEFAULT_PROJECT_CONVENTION,
        config_path,
        "lithos-lens.tasks",
    )
    if value not in PROJECT_CONVENTIONS:
        raise ConfigError(
            f"{config_path}: [lithos-lens.tasks].project_convention must be one "
            f"of {sorted(PROJECT_CONVENTIONS)}"
        )
    return cast(ProjectConvention, value)


def _project_tag_key(data: dict[str, Any], config_path: Path) -> str:
    value = optional_str(
        data,
        "project_tag_key",
        DEFAULT_PROJECT_TAG_KEY,
        config_path,
        "lithos-lens.tasks",
    ).strip()
    # The key is used as a "<key>:" tag prefix, so an empty or already-suffixed
    # key would silently match every tag (or nothing at all).
    if not value or ":" in value:
        raise ConfigError(
            f"{config_path}: [lithos-lens.tasks].project_tag_key must be a "
            "non-empty tag key without ':'"
        )
    return value


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
        enabled=optional_bool(data, "enabled", True, config_path, "lithos-lens.events"),
        reconnect_backoff_ms=tuple(backoff),
    )


def _parse_llm(data: Any, config_path: Path) -> LLMConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.llm] must be a table")
    return LLMConfig(
        enabled=optional_bool(data, "enabled", False, config_path, "lithos-lens.llm"),
        provider=optional_str(data, "provider", "", config_path, "lithos-lens.llm"),
        model=optional_str(data, "model", "", config_path, "lithos-lens.llm"),
        api_key=optional_str(data, "api_key", "", config_path, "lithos-lens.llm"),
        base_url=optional_str(data, "base_url", "", config_path, "lithos-lens.llm"),
        extra_headers_json=optional_str(
            data, "extra_headers_json", "", config_path, "lithos-lens.llm"
        ),
        max_tokens=optional_int(
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
        enabled=optional_bool(
            data, "enabled", True, config_path, "lithos-lens.telemetry"
        ),
        endpoint=optional_str(
            data, "endpoint", "", config_path, "lithos-lens.telemetry"
        ),
        console_fallback=optional_bool(
            data, "console_fallback", False, config_path, "lithos-lens.telemetry"
        ),
        service_name=optional_str(
            data, "service_name", "lithos-lens", config_path, "lithos-lens.telemetry"
        ),
        export_interval_ms=optional_int(
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
        default_view=optional_str(
            data, "default_view", "tasks", config_path, "lithos-lens.ui"
        )
    )


def _parse_health(data: Any, config_path: Path) -> HealthConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.health] must be a table")
    return HealthConfig(
        refresh_interval_s=optional_int(
            data,
            "refresh_interval_s",
            DEFAULT_HEALTH_REFRESH_INTERVAL_S,
            config_path,
            "lithos-lens.health",
            minimum=1,
        )
    )


def _parse_knowledge(data: Any, config_path: Path) -> KnowledgeConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: [lithos-lens.knowledge] must be a table")
    return KnowledgeConfig(
        related_title_fanout_cap=optional_int(
            data,
            "related_title_fanout_cap",
            DEFAULT_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP,
            config_path,
            "lithos-lens.knowledge",
            minimum=1,
            # Bound the per-request title fan-out so a misconfigured cap can't
            # amplify one /note/{id} request into an unbounded concurrent
            # lithos_read burst against the shared MCP session.
            maximum=MAX_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP,
        ),
        search_limit=optional_int(
            data,
            "search_limit",
            DEFAULT_KNOWLEDGE_SEARCH_LIMIT,
            config_path,
            "lithos-lens.knowledge",
            minimum=1,
            maximum=MAX_KNOWLEDGE_LANDING_LIMIT,
        ),
        recent_limit=optional_int(
            data,
            "recent_limit",
            DEFAULT_KNOWLEDGE_RECENT_LIMIT,
            config_path,
            "lithos-lens.knowledge",
            minimum=1,
            maximum=MAX_KNOWLEDGE_LANDING_LIMIT,
        ),
    )


def _apply_env_overrides(cfg: LithosLensConfig) -> LithosLensConfig:
    env_override = os.environ.get("LITHOS_LENS_ENVIRONMENT", "")
    data_dir_override = os.environ.get("LITHOS_LENS_DATA_DIR", "")
    log_level_override = os.environ.get("LITHOS_LENS_LOG_LEVEL", "")
    lithos_url_override = os.environ.get("LITHOS_LENS_LITHOS_URL", "")
    lithos_mcp_sse_path_override = os.environ.get("LITHOS_LENS_MCP_SSE_PATH", "")
    lithos_events_path_override = os.environ.get("LITHOS_LENS_SSE_EVENTS_PATH", "")
    agent_id_override = os.environ.get("LITHOS_LENS_AGENT_ID", "")
    tasks_visible_cap_override = os.environ.get("LITHOS_LENS_TASKS_VISIBLE_CAP", "")
    tasks_frontier_limit_override = os.environ.get(
        "LITHOS_LENS_TASKS_FRONTIER_LIMIT", ""
    )
    gate_wait_env = os.environ.get("LITHOS_LENS_TASKS_GATE_WAITING_ATTENTION_HOURS", "")
    claim_expiry_env = os.environ.get(
        "LITHOS_LENS_TASKS_CLAIM_EXPIRING_SOON_MINUTES", ""
    )
    stale_open_env = os.environ.get("LITHOS_LENS_TASKS_STALE_OPEN_AGE_DAYS", "")
    unclaimed_env = os.environ.get("LITHOS_LENS_TASKS_UNCLAIMED_READY_AGE_MINUTES", "")
    knowledge_fanout_cap_override = os.environ.get(
        "LITHOS_LENS_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP", ""
    )
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
    telemetry_endpoint_override = os.environ.get("LITHOS_LENS_OTEL_ENDPOINT", "")

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
    # Every [tasks] env override is an independent positive integer, collected
    # in one pass and applied in a single replace(). The names follow the
    # shipped convention (LITHOS_LENS_TASKS_<FIELD>), and the literal
    # os.environ.get reads above are what the docs<->code env guardrail matches
    # on — it reads them by AST, so each one has to appear verbatim.
    tasks_env_overrides = {
        field: _parse_env_int(
            f"LITHOS_LENS_TASKS_{field.upper()}",
            raw,
            maximum=MAX_TASKS_INT_KNOBS.get(field),
        )
        for field, raw in (
            ("visible_cap", tasks_visible_cap_override),
            ("frontier_limit", tasks_frontier_limit_override),
            ("gate_waiting_attention_hours", gate_wait_env),
            ("claim_expiring_soon_minutes", claim_expiry_env),
            ("stale_open_age_days", stale_open_env),
            ("unclaimed_ready_age_minutes", unclaimed_env),
        )
        if raw
    }
    if tasks_env_overrides:
        new_cfg = replace(new_cfg, tasks=replace(new_cfg.tasks, **tasks_env_overrides))
    if knowledge_fanout_cap_override:
        new_knowledge = replace(
            new_cfg.knowledge,
            related_title_fanout_cap=_parse_env_int(
                "LITHOS_LENS_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP",
                knowledge_fanout_cap_override,
                # Same bounds as the [lithos-lens.knowledge] TOML key: a
                # misconfigured env can't amplify the per-request read fan-out.
                maximum=MAX_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP,
            ),
        )
        new_cfg = replace(new_cfg, knowledge=new_knowledge)
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
    if telemetry_enabled_override or telemetry_endpoint_override:
        new_telemetry = new_cfg.telemetry
        if telemetry_enabled_override:
            new_telemetry = replace(
                new_telemetry,
                enabled=_parse_env_bool(
                    "LITHOS_LENS_OTEL_ENABLED", telemetry_enabled_override
                ),
            )
        if telemetry_endpoint_override:
            new_telemetry = replace(new_telemetry, endpoint=telemetry_endpoint_override)
        new_cfg = replace(new_cfg, telemetry=new_telemetry)
    return new_cfg


def _parse_env_int(name: str, value: str, *, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise ConfigError(f"{name} must be >= 1")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{name} must be <= {maximum}")
    return parsed


def _parse_env_bool(name: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean")
