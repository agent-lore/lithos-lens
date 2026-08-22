"""Typed configuration schema: the in-memory shape of ``lithos-lens.toml``.

The frozen dataclasses one per ``[lithos-lens.*]`` table, their built-in
defaults, and the hard ceilings a value may not exceed. Loading lives next
door in :mod:`lithos_lens.config` (discovery, TOML parsing, env overrides),
which re-exports every name here — import from ``lithos_lens.config`` as
before.

Keeping the schema separate from the loader is what stops this pair from
growing into one god module: config.py sat at the guardrail's 800-line
stop-loss, and every new operator knob adds to both halves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from lithos_lens.errors import ConfigError
from lithos_lens.tasks import TASK_STATUSES, TaskStatusName

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
DEFAULT_TASKS_AUTO_REFRESH_INTERVAL_S = 120
DEFAULT_TASKS_VISIBLE_CAP = 50
DEFAULT_TASKS_FRONTIER_LIMIT = 500
DEFAULT_TASKS_DEFAULT_TIME_RANGE_DAYS = 30
# Needs-attention rule thresholds (REQUIREMENTS §5.2.2 rules 3-6). Rules 1
# (unsatisfiable blocker) and 2 (cycle) are intrinsic and have no knob.
DEFAULT_TASKS_GATE_WAITING_ATTENTION_HOURS = 24
DEFAULT_TASKS_CLAIM_EXPIRING_SOON_MINUTES = 10
DEFAULT_TASKS_STALE_OPEN_AGE_DAYS = 7
DEFAULT_TASKS_UNCLAIMED_READY_AGE_MINUTES = 60
# Ceilings for every [lithos-lens.tasks] knob that reaches ``timedelta()`` —
# the four Needs-attention thresholds and the resolved-window size — in their
# own units (a year / a week / ten years / a week / ten years). A value beyond
# these is a misconfiguration (the rule it governs could never fire), and an
# UNBOUNDED one is worse than useless: a large enough int makes ``timedelta()``
# raise OverflowError at render time and 500s every /tasks request. Bounding it
# here fails the load instead, with a ConfigError naming the key (the same
# treatment MAX_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP gives the read fan-out).
#
# Membership is the timedelta reachability test, not a category: add a key here
# the moment its value starts feeding a duration.
MAX_TASKS_INT_KNOBS: dict[str, int] = {
    "gate_waiting_attention_hours": 8760,
    "claim_expiring_soon_minutes": 10080,
    "stale_open_age_days": 3650,
    "unclaimed_ready_age_minutes": 10080,
    # -> tasks.default_since(days) -> timedelta(days=...), twice per /tasks.
    "default_time_range_days": 3650,
}
DEFAULT_LLM_MAX_TOKENS = 2048
DEFAULT_HEALTH_REFRESH_INTERVAL_S = 30
DEFAULT_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP = 20
MAX_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP = 100
DEFAULT_KNOWLEDGE_SEARCH_LIMIT = 20
DEFAULT_KNOWLEDGE_RECENT_LIMIT = 20
# Ceiling on the /knowledge result/recent limits: a misconfigured limit must not
# let one landing-page request materialize an unbounded lithos_search /
# lithos_list result set (the same bound the related-panel fan-out cap enforces).
MAX_KNOWLEDGE_LANDING_LIMIT = 200


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
    # Deprecated with the graph-native dashboard (T1): the per-row claim fan-out
    # it capped is gone (claims arrive inline). Parsed for backward-compat but
    # unused; ``frontier_limit`` is the live scale dial.
    visible_cap: int = DEFAULT_TASKS_VISIBLE_CAP
    # Cap sent to lithos_task_ready / lithos_task_blocked. Sized to clear the
    # production frontier with headroom; truncation is survivable (a
    # Not-classified tail) but should be rare.
    frontier_limit: int = DEFAULT_TASKS_FRONTIER_LIMIT
    default_time_range_days: int = DEFAULT_TASKS_DEFAULT_TIME_RANGE_DAYS
    # Needs-attention thresholds; ``frontier.AttentionPolicy`` consumes them.
    gate_waiting_attention_hours: int = DEFAULT_TASKS_GATE_WAITING_ATTENTION_HOURS
    claim_expiring_soon_minutes: int = DEFAULT_TASKS_CLAIM_EXPIRING_SOON_MINUTES
    stale_open_age_days: int = DEFAULT_TASKS_STALE_OPEN_AGE_DAYS
    unclaimed_ready_age_minutes: int = DEFAULT_TASKS_UNCLAIMED_READY_AGE_MINUTES
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
class KnowledgeConfig:
    # The related-panel render bound (RELATED_RENDER_CAP) is an internal
    # constant in lithos_lens.knowledge, not public config: the PRD only
    # specifies related_title_fanout_cap.
    related_title_fanout_cap: int = DEFAULT_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP
    # /knowledge landing dials: hybrid-search result count and the
    # recently-updated browse list length (K1-S6).
    search_limit: int = DEFAULT_KNOWLEDGE_SEARCH_LIMIT
    recent_limit: int = DEFAULT_KNOWLEDGE_RECENT_LIMIT


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
    knowledge: KnowledgeConfig
