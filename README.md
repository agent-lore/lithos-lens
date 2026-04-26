# Lithos Lens

Visual browser for Lithos.

Lithos Lens is a local FastAPI web UI for observing Lithos coordination state
and, in later milestones, browsing Lithos knowledge. The current implementation
contains the common-core web scaffold: TOML configuration, structured logging,
Lithos health probing, startup agent registration, degraded-mode rendering,
vendored static assets, and a Tasks landing page shell.

## Getting Started

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Usage

Drop a `lithos-lens.toml` in the cwd (or point `LITHOS_LENS_CONFIG` at one —
see [lithos-lens.example.toml](lithos-lens.example.toml) for a complete
template) and then:

```bash
uv run lithos-lens
# serves Lithos Lens on http://0.0.0.0:8000

python -m lithos_lens
# same entry point
```

## Configuration

Lithos Lens reads a TOML file (`lithos-lens.toml`) at startup. The annotated
example lives in [lithos-lens.example.toml](lithos-lens.example.toml).

### Example

```toml
[lithos-lens]
environment = "dev"
greeting = "Hello"

[lithos-lens.storage]
data_dir = "~/.lithos-lens/data"

[lithos-lens.logging]
level = "info"

[lithos-lens.lithos]
url = "http://localhost:8765"
mcp_sse_path = "/sse"
sse_events_path = "/events"
agent_id = "lithos-lens"
```

### Config fields

#### `[lithos-lens]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `environment` | string | No | `"dev"` | Human label surfaced in output and suitable for logging/telemetry. |
| `greeting` | string | No | `"Hello"` | Legacy scaffold field retained for config compatibility. |

#### `[lithos-lens.storage]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `data_dir` | string (path) | No | `~/.lithos-lens/data` | Root directory for application data. |

#### `[lithos-lens.logging]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `level` | enum | No | `"info"` | Log level. Valid values: `debug`, `info`, `warning`, `error`. |

#### `[lithos-lens.lithos]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `url` | string | No | `http://localhost:8765` | Base URL for Lithos HTTP health and MCP/SSE endpoints. |
| `mcp_sse_path` | string | No | `/sse` | Lithos MCP-over-SSE endpoint path used for tool calls such as startup registration. |
| `sse_events_path` | string | No | `/events` | Lithos event stream path used by later Tasks SSE milestones. |
| `agent_id` | string | No | `lithos-lens` | Agent ID used when Lens registers with Lithos. |

#### `[lithos-lens.tasks]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `auto_refresh_interval_s` | integer | No | `30` | Polling fallback interval used when live events are unavailable. |
| `visible_cap` | integer | No | `50` | Maximum visible rows enriched with per-task claim status. |
| `default_time_range_days` | integer | No | `30` | Created-at window for completed/cancelled task context. |

#### `[lithos-lens.events]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | boolean | No | `true` | Enables the shared Lithos event subscriber skeleton. |
| `reconnect_backoff_ms` | integer array | No | `[500, 1000, 2000, 5000, 10000]` | Reconnect schedule for later SSE implementation. |

#### `[lithos-lens.llm]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | boolean | No | `false` | Enables optional LiteLLM-backed features. |
| `provider` | string | No | `""` | Human-readable LiteLLM provider prefix metadata. |
| `model` | string | No | `""` | LiteLLM model string. |
| `api_key` | string | No | `""` | Provider API key, when required. |
| `base_url` | string | No | `""` | Optional LiteLLM API base URL. |
| `extra_headers_json` | string | No | `""` | Optional provider-specific headers as a JSON object string. |
| `max_tokens` | integer | No | `2048` | Default LLM output budget. |

#### `[lithos-lens.telemetry]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | boolean | No | `false` | Enables optional request instrumentation hooks. |
| `console_fallback` | boolean | No | `false` | Reserved for later OTEL console export behavior. |
| `service_name` | string | No | `lithos-lens` | Service name used by telemetry. |
| `export_interval_ms` | integer | No | `30000` | Export interval for later OTEL metric support. |

### Environment variable overrides

Loaded via `python-dotenv` at startup. **Precedence: env var → config file → built-in default.**

| Env var | Overrides | Notes |
|---------|-----------|-------|
| `LITHOS_LENS_CONFIG` | — | Path to `lithos-lens.toml`. Default search order below. |
| `LITHOS_LENS_ENVIRONMENT` | `lithos-lens.environment` | Handy for CI/container deployments. |
| `LITHOS_LENS_DATA_DIR` | `lithos-lens.storage.data_dir` | Handy for CI/container deployments. |
| `LITHOS_LENS_LOG_LEVEL` | `lithos-lens.logging.level` | Must be one of the enum values above. |
| `LITHOS_LENS_LITHOS_URL` | `lithos-lens.lithos.url` | Base Lithos URL. |
| `LITHOS_LENS_MCP_SSE_PATH` | `lithos-lens.lithos.mcp_sse_path` | MCP-over-SSE path. |
| `LITHOS_LENS_SSE_EVENTS_PATH` | `lithos-lens.lithos.sse_events_path` | Event stream path. |
| `LITHOS_LENS_AGENT_ID` | `lithos-lens.lithos.agent_id` | Startup registration agent ID. |
| `LITHOS_LENS_TASKS_VISIBLE_CAP` | `lithos-lens.tasks.visible_cap` | Must be a positive integer. |
| `LITHOS_LENS_LLM_ENABLED` | `lithos-lens.llm.enabled` | Boolean. |
| `LITHOS_LENS_LLM_MODEL` | `lithos-lens.llm.model` | LiteLLM model string. |
| `LITHOS_LENS_LLM_PROVIDER` | `lithos-lens.llm.provider` | Optional provider label. |
| `LITHOS_LENS_LLM_API_KEY` | `lithos-lens.llm.api_key` | Provider-dependent. |
| `LITHOS_LENS_LLM_BASE_URL` | `lithos-lens.llm.base_url` | Optional API base URL. |
| `LITHOS_LENS_LLM_EXTRA_HEADERS_JSON` | `lithos-lens.llm.extra_headers_json` | Optional JSON object string. |
| `LITHOS_LENS_LLM_MAX_TOKENS` | `lithos-lens.llm.max_tokens` | Must be a positive integer. |
| `LITHOS_LENS_OTEL_ENABLED` | `lithos-lens.telemetry.enabled` | Boolean. |

### Config file discovery order

When `LITHOS_LENS_CONFIG` is not set, Lithos Lens looks for `lithos-lens.toml`
in this order:

1. `./lithos-lens.toml` (current working directory)
2. `~/.lithos-lens/lithos-lens.toml` (user home)
3. `/etc/lithos-lens/lithos-lens.toml` (system-wide)

First file found wins. Error if none found.

### Validation rules

- `environment` and `greeting` must be strings
- `data_dir` must be a string path (`~` is expanded)
- `logging.level` must be one of `debug`, `info`, `warning`, `error`
- Boolean fields accept TOML booleans in config and common boolean strings in env overrides
- Integer fields such as `tasks.visible_cap` and `llm.max_tokens` must be positive
- `LITHOS_LENS_LOG_LEVEL`, if set, must be one of the log-level values above

## Docker

### Build the image

```bash
make docker-build
```

### Multi-environment stacks

Lithos Lens ships with per-environment Docker stacks driven by `.env.<env>`
files. Two environments are supported out of the box: `dev` and `prod`. Each
stack runs with its own Docker Compose project name, container name, and
data path, so they can coexist on the same host.

Set up an environment file:

```bash
cp docker/.env.example docker/.env.dev
# edit docker/.env.dev as needed
```

Manage the stack with `docker/run.sh`:

```bash
./docker/run.sh dev up        # build and start (detached)
./docker/run.sh dev logs      # follow logs
./docker/run.sh dev status    # show running containers
./docker/run.sh dev restart   # down + up
./docker/run.sh dev down      # stop and remove

./docker/run.sh prod up       # same, for prod
```

Or via Make:

```bash
make docker-up-dev
make docker-down-dev
make docker-up-prod
make docker-down-prod
```

`LITHOS_LENS_ENVIRONMENT` is passed into the container so application code
can read it (e.g. for logging or telemetry labels).

`.env.example` is committed; `.env.dev` and `.env.prod` are gitignored.

## Development

Format, lint, type-check, and test:

```bash
make fmt        # auto-format
make lint       # lint + format check
make typecheck  # pyright
make test       # pytest
make check      # all of the above
```

## Implementation Tracking

Progress is tracked in [docs/IMPLEMENTATION_CHECKLIST.md](docs/IMPLEMENTATION_CHECKLIST.md).
Milestone 0 common-core items are implemented; Tasks MVP work begins at
Milestone 1.
