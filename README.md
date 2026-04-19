# Lithos Lens

Visual browser for Lithos.

This repository is currently a Python 3.12 project skeleton: a minimal
hello-world entry point, a TOML-based configuration system, a multi-stage
Docker image, per-environment Docker stacks driven by `.env.<env>` files, a
Makefile, and a GitHub Actions CI pipeline — ready to be extended with real
functionality.

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
# → Hello from Lithos Lens (dev)

python -m lithos_lens
# → Hello from Lithos Lens (dev)
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
```

### Config fields

#### `[lithos-lens]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `environment` | string | No | `"dev"` | Human label surfaced in output and suitable for logging/telemetry. |
| `greeting` | string | No | `"Hello"` | Greeting prefix printed by `lithos-lens`. |

#### `[lithos-lens.storage]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `data_dir` | string (path) | No | `~/.lithos-lens/data` | Root directory for application data. |

#### `[lithos-lens.logging]`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `level` | enum | No | `"info"` | Log level. Valid values: `debug`, `info`, `warning`, `error`. |

### Environment variable overrides

Loaded via `python-dotenv` at startup. **Precedence: env var → config file → built-in default.**

| Env var | Overrides | Notes |
|---------|-----------|-------|
| `LITHOS_LENS_CONFIG` | — | Path to `lithos-lens.toml`. Default search order below. |
| `LITHOS_LENS_ENVIRONMENT` | `lithos-lens.environment` | Handy for CI/container deployments. |
| `LITHOS_LENS_DATA_DIR` | `lithos-lens.storage.data_dir` | Handy for CI/container deployments. |
| `LITHOS_LENS_LOG_LEVEL` | `lithos-lens.logging.level` | Must be one of the enum values above. |

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
- `LITHOS_LENS_LOG_LEVEL`, if set, must likewise be one of those values

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
