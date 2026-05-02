---
title: Lithos Lens — Requirements Document
version: 0.7.0
date: 2026-04-29
status: draft
tags: [lithos-lens, requirements, design, architecture]
---

# Lithos Lens — Requirements Document

> [!abstract] Project Summary
> **Lithos Lens** is a local web UI for observing the Lithos coordination layer and for browsing and curating a Lithos knowledge base. It hosts two first-class views inside a single FastAPI app:
> - **Tasks View** — read-only dashboard over Lithos tasks, claims, and findings, with flexible filters and live updates from the Lithos SSE event stream. **This is the first view delivered.**
> - **Knowledge Browser** — feed view, interactive graph (Cytoscape.js over LCMA typed edges), cognitive search backed by `lithos_retrieve`, feedback controls (👍 / 👎), note comparison, and curated reading paths.
>
> Lens is a pure Lithos MCP client by default — zero runtime dependency on the Influx ingestion container, all data sourced from Lithos. From v0.5 onwards Lens may optionally call an LLM directly **when explicitly enabled** (`LENS_LLM_ENABLED=true`) to provide "most significant findings" curation in the Tasks view, answer synthesis, note comparison, and explanation-depth control; with the flag off Lens remains a pure MCP client.

> [!note] v0.5 changelog
> v0.5 incorporated ideas from a review of the Paperlens prototype (`/paperlens`): answer synthesis with citations, multi-note comparison, an expertise-level slider, curated reading paths, graph centrality overlay, and bidirectional node↔panel selection. Quiz/flashcard generation and embedding storage from Paperlens are explicitly out of scope.

> [!note] v0.6 changelog
> v0.6 promotes the document into four parts (Common Core, Tasks View, Knowledge Browser, Reference) and adds the **Tasks View** as a peer of the Knowledge Browser. The implementation order is **Tasks View first, Knowledge Browser second** — both ride on the same FastAPI app, MCP client, and shared SSE event subscription, so the common core (§1–§4) is built once and both views slot in. The Tasks View MVP is intentionally constrained to the current Lithos read surface (`lithos_task_list`, `lithos_task_status`, `lithos_finding_list`, `lithos_agent_list`, `lithos_stats`, and `/events`). Any richer task filtering, direct task lookup, claim metadata, or backend findings query pagination is deferred unless Lithos exposes it later.

> [!note] v0.7 changelog
> v0.7 splits the Tasks surface into **two routes** — an Operator View at `/tasks` (primary: "are my agents alive and making progress?") and a Planning View at `/tasks/plan` (secondary: "what should happen next?"). The Operator View structure is reshaped around four open-task sections: **Needs attention** (severity-ordered: expired claim → stale open → unclaimed-old) → **In progress** → **Queued** → **Unknown claim state** tail, with **collapsed Completed / Cancelled** below. Project tagging conventions become normative (`project:<slug>`), driving a first-class project filter, per-row project chip, and the Planning View's project breakdown / throughput sections. New milestone **M1.5** ships the Planning View after the Operator View stabilises. New row affordances: latest-finding inline, agent chips with role markers (created / claimed / latest), human-agent visual distinction, OR-across-roles filter. New global affordances: collapsible "Recent findings" drawer fed by a server-side rolling buffer, title-badge notifications (always on), debounced server-side metric recompute. Desktop notifications + LLM "most significant findings" remain in M3.

---

## Table of Contents

### Part A — Common Core
- [[#1. Goals & Non-Goals]]
- [[#2. Architecture Overview]]
- [[#3. Infrastructure & Deployment]]
- [[#4. Configuration]]

### Part B — Tasks View
- [[#5. Tasks View — Operator View]]
- [[#5A. Tasks View — Planning View]]
- [[#5B. Project Tracking Conventions]]

### Part C — Knowledge Browser
- [[#6. Feed View]]
- [[#7. Graph View]]
- [[#8. Cognitive Search]]
- [[#9. Feedback Mechanism]]
- [[#10. Note Comparison]]
- [[#11. Reading Paths]]
- [[#12. Conflict Resolution UI]]

### Part D — Reference
- [[#13. Settings View]]
- [[#14. Resilience & Error Handling]]
- [[#15. Observability]]
- [[#16. API Reference]]
- [[#17. Implementation Plan]]

---

# Part A — Common Core

The common-core sections describe behaviour, infrastructure, and configuration shared by every view (Tasks View, Knowledge Browser, and any future view). View-specific behaviour lives in Parts B and C.

---

## 1. Goals & Non-Goals

### Goals

#### Common
- Provide a low-latency local browser UI over the Lithos coordination layer and a Lithos knowledge base
- Two first-class views — **Tasks View** (delivered first) and **Knowledge Browser** — sharing one FastAPI app, one MCP client, and one `base.html` shell with a top-nav view switcher
- Operate purely as a Lithos MCP client when `LENS_LLM_ENABLED=false` — no dependency on Influx runtime
- Subscribe to the Lithos SSE event stream once (a shared `app/events.py` utility) and let any view consume the events it cares about
- Optional LLM-backed features ("most significant findings" curation, answer synthesis, comparison themes, complexity slider) behind a single config flag, gracefully degrading when disabled
- Minimal stack: FastAPI + HTMX + Cytoscape.js; no heavy JS framework, no build step

#### Tasks View
- Two co-equal routes sharing the same data, MCP client, and SSE subscription:
  - **Operator View (`/tasks`)** — "are my agents alive and making progress?" Live, read-only operator dashboard. Primary surface.
  - **Planning View (`/tasks/plan`)** — "what should happen next?" Throughput / starvation / bottleneck signals plus a top-level Human-actionable section. Secondary surface, ships in M1.5.
- Both routes reachable from the shared top-nav alongside future Knowledge Browser routes; navigation between them preserves no view-specific state (filters reset on switch — they answer different questions).
- **Operator View shape** — open work split into severity-ordered sections: **Needs attention** (expired-claim → stale-open → unclaimed-old) → **In progress** (has active claims) → **Queued** (no active claims) → **Unknown claim state** tail (rows past `tasks.visible_cap`). Collapsed Completed / Cancelled groups below.
- Project tagging is first-class: every row carries a project chip; project is a top-level filter alongside status, tag, agent, and created-at range.
- Findings surface in two places: a one-line "latest finding" on each open row, and a collapsible global "Recent findings" drawer fed by a server-side rolling buffer.
- Detail surface is a right-side panel by default (`/tasks?selected=<task_id>`) with an "Expand" button to the full-page route (`/tasks/{task_id}`).
- Agent chips on rows collapse roles (`created` / `claimed` / `latest`) into a single chip per agent; human-agents (configured set) render with a person-icon prefix and distinct background. Clicking an agent chip filters across all roles (OR semantics).
- Auto-update via the shared SSE event subscription, with a configurable polling fallback. Server-side metric recomputation is debounced; pushed to all open tabs via HTMX OOB swaps.
- Title-badge notifications (`(N) Lithos Lens`) always on for unseen Needs-attention items. Desktop notifications opt-in (M3, lands alongside the LLM milestone).
- Findings link out to the Knowledge Browser via explicit `finding.knowledge_id` (no inference, no heuristics).

#### Knowledge Browser
- Feed view: time-ordered cards filterable by profile, date, tag, confidence / parsed profile score, source
- Interactive graph view with Cytoscape.js, rendering LCMA typed edges
- Cognitive search bar using `lithos_retrieve` (seven-scout PTS retrieval with reranking)
- Feedback controls — mark items as relevant / not relevant; write back to Lithos
- Conflict resolution UI for LCMA `contradicts` edges
- Multi-note **comparison** view (metadata + content; LLM-driven theme and concept analysis when LLM is enabled)
- Curated **reading paths** through a node subset — algorithmic by default, LLM-curated when LLM is enabled
- Graph **centrality overlay** to highlight bridge nodes between clusters

### Non-Goals

- Editing note content inline (that's Obsidian's job — or direct MCP tools)
- Running its own ingestion — Lens never writes source research notes from scratch. It may write narrow Lens-authored operational/curation notes only where explicitly specified (for example saved reading paths); feedback is written as metadata/tag updates on existing notes.
- Hosting an external collaboration surface — single-user, local-only
- Authoring feedback for knowledge items that Influx did not ingest — v1 assumes feedback is Influx-centric; a later version can generalise
- Deep editing of LCMA edges (users can resolve conflicts; creating/deleting arbitrary edges is out of scope for v1)
- Quiz / flashcard generation
- Hosting embeddings or running its own vector index — UMAP-style semantic projections are deferred and depend on Lithos exposing embeddings via a future MCP tool
- **Task creation, mutation, or claim management** — the Tasks view is strictly read-only; any agent that wants to create, claim, or update tasks does so via the Lithos MCP API directly, not through Lens
- Running its own task scheduler, cron, or worker — Lens observes; it does not orchestrate

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                         DOCKER NETWORK                            │
│                                                                   │
│  ┌──────────────┐     ┌──────────────┐     ┌─────────────────┐   │
│  │    LITHOS    │◀────│    INFLUX    │     │  LITHOS-LENS    │   │
│  │              │     │  (ingestion) │     │   (web UI)      │   │
│  │  knowledge   │     │              │     │                 │   │
│  │  store +     │     │  scheduled   │     │  stateless      │   │
│  │  tasks       │     │  batch job   │     │  HTTP server    │   │
│  │  MCP API +   │     │              │     │                 │   │
│  │  SSE events  │     │              │     │                 │   │
│  └──────────────┘     └──────────────┘     └────────┬────────┘   │
│          ▲       ▲                                   │            │
│          │       └─── SSE event stream ──────────────┤            │
│          └─────────── MCP API ─────────────────────  │            │
│                                                       ▼            │
│                                                ┌──────────────┐    │
│                                                │   BROWSER    │    │
│                                                │  (human UI)  │    │
│                                                └──────────────┘    │
└──────────────────────────────────────────────────────────────────┘
                                                       │
                            (LLM API, optional) ───────┤
                                                       ▼
                                                (Anthropic / OpenAI / Ollama)
```

> [!important] Lens is Influx-independent
> **Lithos Lens has zero runtime dependency on the Influx ingestion container.** It is a pure Lithos MCP client. All knowledge and coordination data — paper notes, feedback, graph edges, tasks, claims, and findings — comes from Lithos. The UI and ingestion pipeline can be restarted, updated, or fail independently. Lens may mount the `influx-archive` volume read-only so it can serve archived PDFs/HTMLs directly, but that is a file-system dependency on a shared volume, not a runtime dependency on the Influx process.

> [!note] Optional LLM client
> When `LENS_LLM_ENABLED=true`, Lens additionally talks to an LLM provider (Anthropic / OpenAI / Ollama) for "most significant findings" curation in the Tasks view, synthesis, comparison, and complexity-tuned output in the Knowledge Browser. With the flag off, all LLM-dependent UI surfaces are hidden and Lens remains a pure MCP client. When Lithos exposes a synthesis tool (`lithos_synthesize` or equivalent) in a later MVP, Lens prefers the MCP path and treats the local LLM as a fallback.

> [!note] Single SSE subscription
> Lens opens **one** SSE connection to Lithos at app start (`app/events.py`). Each view registers a callback for the event types it cares about; the connection is shared. The Tasks view subscribes to all task / claim / finding events; the Knowledge Browser may later subscribe to edge / note events for live graph updates.

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deployment | Single Docker container hosting both views | Independent restartability vs Lithos / Influx; one app, one process, one SSE connection |
| App structure | Single FastAPI app with view-specific routers (`tasks/*`, `knowledge/*`) sharing a `base.html` shell | Lets views share session state, the MCP client, and the event stream without coordination overhead |
| Lithos communication | Lithos MCP API (SSE transport for tools) + Lithos SSE event stream (for live updates) | One MCP transport for request/response, one SSE stream for push events; both supplied by Lithos |
| Graph rendering | Cytoscape.js | Best for knowledge graphs; handles typed LCMA edges; scales to ~10K nodes |
| Frontend | FastAPI + HTMX + Cytoscape.js | No build step; minimal stack; dynamic HTML without a JS framework; HTMX SSE extension drives live tile updates |
| Styling/assets | Vendored, pinned static assets (`static/`) with app CSS | Preserves local-first/offline behavior and avoids CDN supply-chain/runtime dependencies while keeping no build step |
| Config format | TOML (read-only, shared with Influx) | Consistent with Influx and Lithos conventions |
| OTEL | Opt-in, additive, optional packages | Consistent with Lithos conventions |
| Environments | `.env.dev` / `.env.prod` | Consistent with Lithos conventions |
| LLM access | Optional, env-gated LiteLLM client (`LENS_LLM_*`) | Lithos MVP 1 does not provide synthesis, comparison, or curation tools; Lens needs LLM access for "most significant findings", Q&A synthesis, comparison themes, and complexity-adjusted output. LiteLLM keeps Lens provider-agnostic across OpenAI, Anthropic, OpenRouter, Ollama, and local/custom providers. When Lithos ships MVP-3 synthesis, Lens prefers the MCP path. |
| Centrality computation | Client-side in Cytoscape | Lithos exposes edges via `lithos_edge_list` but no centrality scores; computing in the browser avoids a new MCP tool and operates on the already-loaded graph |
| SSE event handling | Single shared subscription in `app/events.py`, fan-out via per-view in-memory pub/sub | Avoids N independent SSE connections from the same app; lets the Tasks view receive every task/claim/finding event without coupling to the Knowledge Browser |

### Shared Application Surface

The following modules are shared by every view and constitute the "common core" that should be implemented before any view-specific work:

| Module | Purpose |
|--------|---------|
| `app/main.py` | FastAPI app, view-switcher top-nav, mounts all routers |
| `app/config.py` | TOML + env loader, exposes a typed config object |
| `app/lithos_client.py` | Lithos MCP client (SSE transport for tool calls) |
| `app/events.py` | Single SSE subscription to Lithos's event stream; in-process pub/sub for views |
| `app/llm_client.py` *(optional)* | LiteLLM-backed provider-agnostic LLM wrapper; only loaded when `LENS_LLM_ENABLED=true` |
| `app/telemetry.py` | OTEL tracer/meter setup; `@traced` decorator |
| `app/templates/base.html` | Layout shell with top-nav, view switcher, theme, banners |

---

## 3. Infrastructure & Deployment

### Container

| Container | Base image | Purpose |
|-----------|-----------|--------|
| `lithos-lens` | `python:3.12-slim` | Web UI hosting both Tasks View and Knowledge Browser |

### Shared Volumes

| Volume | Lens mount | Purpose |
|--------|------------|---------|
| `influx-archive` | `/archive` (ro) | Serve archived PDFs/HTMLs inline (Knowledge Browser) |
| `influx-config` | `/etc/influx` (ro) | Read the shared Influx TOML config for the settings view |

### Environment Files

**`.env.dev`:**
```env
LENS_ENVIRONMENT=dev
LENS_HOST_PORT=7843
LENS_CONTAINER_NAME=lithos-lens
LENS_OTEL_ENABLED=false
OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318

# Lithos transport
LITHOS_URL=http://host.docker.internal:8765
LITHOS_SSE_EVENTS_PATH=/events            # default; SSE event stream endpoint
LENS_AGENT_ID=lithos-lens

# Tasks view — operator view
LENS_TASKS_AUTO_REFRESH_INTERVAL_S=30     # manual fallback when SSE disconnects
LENS_TASKS_VISIBLE_CAP=50                 # cap on rows that fetch claims inline
LENS_TASKS_DEFAULT_TIME_RANGE_DAYS=30     # created-at date window for task list defaults
LENS_TASKS_STALE_OPEN_AGE_DAYS=7          # Needs-attention threshold: stale open
LENS_TASKS_UNCLAIMED_WARNING_MINUTES=60   # Needs-attention threshold: unclaimed-old
LENS_TASKS_METRICS_DEBOUNCE_MS=2000       # server-side metric recompute debounce
LENS_TASKS_PROJECT_TAG_KEY=project        # reserved tag-key for project chips & filter
LENS_TASKS_RECENT_FINDINGS_DRAWER_SIZE=50

# Tasks view — planning view (M1.5)
LENS_TASKS_BOTTLENECK_MIN_INFLIGHT=3
LENS_TASKS_BOTTLENECK_CONCENTRATION=0.7
LENS_TASKS_STALLED_NO_FINDINGS_HOURS=24
LENS_TASKS_THROUGHPUT_WINDOW_DAYS=30
LENS_TASKS_HUMAN_ACTIONABLE_TAG=human
# LENS_TASKS_HUMAN_AGENTS=dave,human       # comma-separated agent IDs that represent humans

# Optional LLM client — disabled by default
LENS_LLM_ENABLED=false
# LENS_LLM_PROVIDER=anthropic             # LiteLLM provider prefix: anthropic | openai | openrouter | ollama | ...
# LENS_LLM_MODEL=anthropic/claude-haiku-4-5-20251001
# LENS_LLM_API_KEY=                       # provider-dependent; not needed for local ollama
# LENS_LLM_BASE_URL=                      # optional LiteLLM api_base for OpenRouter/local gateways
# LENS_LLM_EXTRA_HEADERS_JSON=            # optional provider-specific headers, e.g. OpenRouter metadata
# LENS_LLM_MAX_TOKENS=2048
```

**`.env.prod`:**
```env
LENS_ENVIRONMENT=production
LENS_HOST_PORT=7843
LENS_CONTAINER_NAME=lithos-lens
LENS_OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318

LITHOS_URL=http://lithos:8765
LITHOS_SSE_EVENTS_PATH=/events
LENS_AGENT_ID=lithos-lens

LENS_TASKS_AUTO_REFRESH_INTERVAL_S=30
LENS_TASKS_VISIBLE_CAP=50
LENS_TASKS_DEFAULT_TIME_RANGE_DAYS=30
LENS_TASKS_STALE_OPEN_AGE_DAYS=7
LENS_TASKS_UNCLAIMED_WARNING_MINUTES=60
LENS_TASKS_METRICS_DEBOUNCE_MS=2000
LENS_TASKS_PROJECT_TAG_KEY=project
LENS_TASKS_RECENT_FINDINGS_DRAWER_SIZE=50
LENS_TASKS_BOTTLENECK_MIN_INFLIGHT=3
LENS_TASKS_BOTTLENECK_CONCENTRATION=0.7
LENS_TASKS_STALLED_NO_FINDINGS_HOURS=24
LENS_TASKS_THROUGHPUT_WINDOW_DAYS=30
LENS_TASKS_HUMAN_ACTIONABLE_TAG=human
# LENS_TASKS_HUMAN_AGENTS=dave,human

LENS_LLM_ENABLED=false
# LENS_LLM_PROVIDER=anthropic
# LENS_LLM_MODEL=anthropic/claude-haiku-4-5-20251001
# LENS_LLM_API_KEY=
# LENS_LLM_BASE_URL=
# LENS_LLM_EXTRA_HEADERS_JSON=
# LENS_LLM_MAX_TOKENS=2048
```

### `docker-compose.yml`

```yaml
# lithos-lens — tasks view + knowledge browser UI
services:
  lithos-lens:
    image: ${LENS_IMAGE:-lithos-lens:local}
    pull_policy: never
    build:
      context: .
      dockerfile: Dockerfile
    container_name: ${LENS_CONTAINER_NAME:-lithos-lens}
    user: "${LENS_UID:-1000}:${LENS_GID:-1000}"
    restart: unless-stopped
    ports:
      - "${LENS_HOST_PORT:-7843}:8000"
    volumes:
      - ${INFLUX_ARCHIVE_PATH:-./archive}:/archive:ro
      - ${INFLUX_CONFIG_PATH:-./config}:/etc/influx:ro
    environment:
      - LENS_ENVIRONMENT=${LENS_ENVIRONMENT:-dev}
      - LITHOS_URL=${LITHOS_URL:-http://host.docker.internal:8765}
      - LITHOS_SSE_EVENTS_PATH=${LITHOS_SSE_EVENTS_PATH:-/events}
      - LENS_AGENT_ID=${LENS_AGENT_ID:-lithos-lens}
      - LENS_OTEL_ENABLED=${LENS_OTEL_ENABLED:-false}
      - OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT:-http://host.docker.internal:4318}
      - LENS_LOG_LEVEL=${LENS_LOG_LEVEL:-INFO}
      - LENS_TASKS_AUTO_REFRESH_INTERVAL_S=${LENS_TASKS_AUTO_REFRESH_INTERVAL_S:-30}
      - LENS_TASKS_VISIBLE_CAP=${LENS_TASKS_VISIBLE_CAP:-50}
      - LENS_TASKS_DEFAULT_TIME_RANGE_DAYS=${LENS_TASKS_DEFAULT_TIME_RANGE_DAYS:-30}
      - LENS_LLM_ENABLED=${LENS_LLM_ENABLED:-false}
      - LENS_LLM_PROVIDER=${LENS_LLM_PROVIDER:-}
      - LENS_LLM_MODEL=${LENS_LLM_MODEL:-}
      - LENS_LLM_API_KEY=${LENS_LLM_API_KEY:-}
      - LENS_LLM_BASE_URL=${LENS_LLM_BASE_URL:-}
      - LENS_LLM_EXTRA_HEADERS_JSON=${LENS_LLM_EXTRA_HEADERS_JSON:-}
      - LENS_LLM_MAX_TOKENS=${LENS_LLM_MAX_TOKENS:-2048}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

### `run.sh`

Identical pattern to Influx with project name `lithos-lens-<env>`. Supports `up | down | restart | logs | status`.

---

## 4. Configuration

Lens has minimal configuration of its own. The main runtime config comes from environment variables (Lithos URL, ports, telemetry, optional LLM, tasks-view tuning). The **Influx TOML config is mounted read-only** so the settings view can display profiles, thresholds, models, and feed lists.

```toml
# /etc/lithos-lens/config.toml  (optional — most settings come from env)

[ui]
default_view = "tasks"          # tasks | knowledge — Tasks ships first so it is the default landing view
feed_page_size = 50
graph_max_nodes = 500           # safety cap for graph render
graph_centrality_overlay = false  # off by default; toggle in graph filter panel
reading_path_default = "salience" # salience | chronological | edge-traversal | llm
compare_max_notes = 4           # cap on the multi-select for comparison

[search]
default_limit = 20
namespace_filter = []           # optional; empty = all namespaces

[tasks]
auto_refresh_interval_s = 120     # manual refresh fallback when SSE disconnects
visible_cap = 50                  # rows for which lithos_task_status is fetched inline
default_time_range_days = 30      # created-at date window for completed/cancelled groups; open tasks are always shown by default
default_status_groups = ["open", "completed", "cancelled"]  # display order
metrics_debounce_ms = 2000        # server-side debounce window for metric recompute on SSE bursts

# Project tagging
project_tag_key = "project"       # tag-key reserved for projects; e.g. project:ganglion

# Needs-attention thresholds
stale_open_age_days = 7           # open tasks older than this surface in Needs attention
unclaimed_warning_minutes = 60    # unclaimed open tasks older than this surface in Needs attention
# expired-claim has no knob (rule: now > expires_at)

# Planning view (M1.5) thresholds
bottleneck_min_inflight = 3       # below this in-flight depth, bottleneck rule does not fire
bottleneck_concentration = 0.7    # one-agent share threshold for bottleneck flag
stalled_no_findings_hours = 24    # in-progress task with no finding.posted in this window flags as stalled
throughput_window_days = 30       # rolling window for Planning View throughput overview

# Recent findings drawer + buffer
recent_findings_drawer_size = 50  # rolling buffer size used by drawer + stalled detection
recent_findings_warmup_window_h = 48  # boot-time warm-up window for finding.posted backfill (≥ stalled threshold)

# Human-actionable (Planning View)
human_actionable_tag = "human"    # tag identifying tasks needing a human; configurable
human_agents = []                 # agent IDs that represent humans, e.g. ["dave", "human"]; renders person-icon chip

[tasks.notifications]
title_badge = true                # update <title> with "(N) Lithos Lens" for unseen Needs-attention items
desktop_optin = true              # show "Enable notifications" affordance; M3 wiring

[events]
sse_path = "/events"            # Lithos SSE event stream path (overridden by env)
reconnect_backoff_ms = [500, 1000, 2000, 5000, 10000]  # exponential backoff schedule

[llm]
enabled = false                 # overridden by LENS_LLM_ENABLED
provider = "anthropic"          # LiteLLM provider prefix: anthropic | openai | openrouter | ollama | ...
model = "anthropic/claude-haiku-4-5-20251001"
default_complexity = 3          # 1=beginner … 5=expert; per-session override allowed
max_tokens = 2048
base_url = ""                   # optional LiteLLM api_base
extra_headers_json = ""         # optional JSON object for provider-specific headers
synthesis_prefer_mcp = true     # use lithos_synthesize when available, else local LLM
findings_curation_enabled = true  # enables "most significant findings" view in tasks

[telemetry]
enabled = false                 # overridden by LENS_OTEL_ENABLED
console_fallback = false
service_name = "lithos-lens"
export_interval_ms = 30000
```

### 4.1 Common Core Startup Contract

At process startup Lens performs the following steps in order:

1. Load TOML config and environment overrides into a typed config object.
2. Configure structured stdout logging.
3. Configure OTEL only if `LENS_OTEL_ENABLED=true`; missing optional OTEL packages must not prevent boot when telemetry is disabled.
4. Create the Lithos MCP client.
5. Attempt startup auto-registration:

```python
lithos_agent_register(
    id=config.lens_agent_id,
    name="Lithos Lens",
    type="web-ui",
)
```

6. Start the shared Lithos `/events` subscriber if event streaming is enabled.
7. Start cached health probes for Lithos, events, and LLM.
8. Mount routers and serve HTTP.

Boot must succeed even when Lithos is unreachable. In that case Lens starts in degraded mode, `/health` reports `lithos="unreachable"`, and UI routes render degraded panels rather than crashing.

### 4.2 LiteLLM Configuration Contract

When `llm.enabled = false`, Lens must not import or initialize LiteLLM eagerly.

When `llm.enabled = true`, Lens validates configuration shape at startup but does not require a paid completion call to pass readiness. Per-feature LLM failures are surfaced as non-blocking UI errors.

Required / optional values:

| Config | Env | Required when enabled | Notes |
|--------|-----|-----------------------|-------|
| `llm.model` | `LENS_LLM_MODEL` | Yes | LiteLLM model string, e.g. `openai/gpt-4.1-mini`, `anthropic/claude-...`, `openrouter/...`, `ollama/...` |
| `llm.provider` | `LENS_LLM_PROVIDER` | No | Human-readable/provider prefix metadata; model string is authoritative |
| `llm.api_key` | `LENS_LLM_API_KEY` | Provider-dependent | Not required for local Ollama |
| `llm.base_url` | `LENS_LLM_BASE_URL` | No | LiteLLM `api_base`, useful for OpenRouter gateways, local Ollama, or self-hosted LiteLLM proxy |
| `llm.extra_headers_json` | `LENS_LLM_EXTRA_HEADERS_JSON` | No | JSON object for provider-specific headers |
| `llm.max_tokens` | `LENS_LLM_MAX_TOKENS` | No | Default 2048 |

### 4.3 Static Asset Policy

Production Lens serves frontend dependencies from pinned vendored files under `static/vendor/`; it does not depend on public CDNs at runtime.

Required policy:
- Vendor HTMX, the HTMX SSE extension, Cytoscape.js, and any precompiled CSS bundle into `static/vendor/`
- Record asset names, versions, source URLs, and checksums in `docs/vendor-assets.md`
- Keep `lens.css` app-owned and small
- Do not use Tailwind CDN in production; if utility CSS is desired, check in a precompiled CSS file
- Development may temporarily use CDN assets during prototyping, but committed default templates should reference vendored assets

---

# Part B — Tasks View

The Tasks View is the first user-facing view delivered. It is a read-only surface over the Lithos coordination layer (tasks, claims, findings) split across **two co-equal routes** that answer different questions:

- **Operator View (§5)** at `/tasks` — "are my agents alive and making progress?" Live dashboard structured around what's in play *now*.
- **Planning View (§5A)** at `/tasks/plan` — "what should happen next?" Project breakdown, throughput, human-actionable queue.

Both routes share the FastAPI app, MCP client, and SSE event subscription. Project tagging conventions (§5B) are normative for both. Switching between them via the top-nav resets view-specific filter state — the views answer different questions and shouldn't co-mingle filters.

The Tasks surface consumes only the current Lithos MCP tools and the Lithos SSE event stream. Requirements that would need new Lithos read APIs are explicitly deferred.

---

## 5. Tasks View — Operator View

### 5.1 Purpose & Scope

The Operator View is the primary Tasks surface and the default landing route. Its single job is to answer **"are my agents alive and making progress?"** — a glance-able operational dashboard.

It surfaces, in priority order:
- **Things that need attention right now** — expired claims, stale open tasks, unclaimed-old tasks
- **What's actively in flight** and which agent is doing what
- **What's queued and ready to be picked up**
- **What just happened across all tasks** — a rolling stream of recent findings
- Recent completions and cancellations as confirmation, not as primary content

> [!warning] Strictly read-only
> The view does **not** create, mutate, or claim tasks. Any agent that needs to manage tasks does so via the Lithos MCP API directly. This boundary is structural, not just stylistic — Lens does not import any task-mutation tool.

> [!note] Two-route shape
> "What should happen next?" — project starvation, bottleneck detection, throughput, human-actionable backlog — lives in the Planning View (§5A). Keeping the Operator View focused on "in play now" is deliberate: the two questions justify two layouts.

The view consumes:
- `lithos_task_list(filter…)` — primary list query
- `lithos_task_status(task_id)` — fetched per visible row up to `tasks.visible_cap` to render inline claim badges and drive In-progress/Queued split
- `lithos_finding_list(task_id, since=...)` — fetched on demand when the detail panel opens; also used during boot-time warm-up of the recent-findings rolling buffer
- `lithos_read(id)` — used to resolve `finding.knowledge_id` UUIDs to note titles for the link label
- `lithos_stats()` — supplements the agent-count signal
- `lithos_agent_list(...)` — sources the "creating agent" filter dropdown
- `lithos_tags(prefix="project:")` — sources the project filter dropdown and the universe of known projects (Planning View shares this fetch)
- Lithos SSE event stream — drives live updates (see §5.6) and the server-side metric recompute (see §5.6.4)

Current Lithos constraints the UI must respect:
- `lithos_task_list` supports `agent`, `status`, `tags`, and `since`, where `since` filters `created_at >= since`
- `lithos_task_list` does not support `limit`, `offset`, `until`, `completed_since`, `has_claims`, or direct claim embedding
- `lithos_task_status` returns active claims with `agent`, `aspect`, and `expires_at`; it does not expose `claimed_at`
- `lithos_finding_list` supports `task_id` and `since`; it does not support `limit`, `offset`, or direct lookup by `finding_id`
- There is no `lithos_task_get`; full detail panels are composed from the selected list row plus `lithos_task_status`
- Expected v1 scale is at most a few hundred total tasks and tens of open tasks, so Lens can scan task lists for direct links and enrich visible/open rows client-side without requiring new Lithos APIs

### 5.2 Operator View Structure

The Operator View renders, top-to-bottom, with the following sections. Each is rendered server-side at page load and updated in place via HTMX OOB swaps fed by the SSE pipeline (§5.6).

```
┌─────────────────────────────────────────────────────────────┐
│  Top-nav: [Tasks] [Tasks · Plan] [Knowledge ▾]   (N) Lens   │
│  Filter bar: project | status | tag | agent | created-at    │
│  Notification affordance | manual refresh | live badge      │
├─────────────────────────────────────────────────────────────┤
│  ⚠ Needs attention  (severity-ordered, oldest-first within) │
│     — expired claim → stale open → unclaimed-old             │
│     (collapses to "All systems healthy — 0 issues" stripe   │
│     when empty; toggle to hide section entirely)            │
├─────────────────────────────────────────────────────────────┤
│  ▶ In progress  (open, has active claims)                    │
├─────────────────────────────────────────────────────────────┤
│  ▶ Queued       (open, no active claims)                     │
├─────────────────────────────────────────────────────────────┤
│  ▶ Unknown claim state  (rows past visible_cap; tail)        │
├─────────────────────────────────────────────────────────────┤
│  ▶ Completed (12 in last 30 days)        [collapsed]         │
├─────────────────────────────────────────────────────────────┤
│  ▶ Cancelled (3 in last 30 days)         [collapsed]         │
└─────────────────────────────────────────────────────────────┘
                                                  ┌───────────┐
                                                  │ Recent    │
                                                  │ findings  │
                                                  │ drawer    │
                                                  │ (toggle)  │
                                                  └───────────┘
```

#### 5.2.1 Needs attention

A **severity-ordered single list** of open rows that have triggered any of the following rules. Within each severity tier, rows sort by `created_at` ascending — oldest persistent problem first.

| Severity | Rule | Configurable? |
|----------|------|---------------|
| **Expired claim** | Row has at least one active claim with `now > expires_at` | No knob (rule is intrinsic) |
| **Stale open** | Open task with `now - created_at > tasks.stale_open_age_days` (default 7d) | `[tasks].stale_open_age_days` |
| **Unclaimed old** | Open task with zero active claims and `now - created_at > tasks.unclaimed_warning_minutes` (default 60m) | `[tasks].unclaimed_warning_minutes` |

Rules:
- Each row in this section carries one or more **reason chips** showing which rule(s) fired (e.g. `expired-claim`, `stale-open`).
- A row that triggers any rule appears **only** in Needs attention — it is **de-duplicated** out of In progress / Queued.
- Rows past `tasks.visible_cap` are excluded from Needs attention because their claim state is unknown; Lens must not silently classify them.
- When the section is empty, render a thin `All systems healthy — 0 issues` stripe (kept visible for reassurance; do not hide entirely by default).
- A toggle in the section header lets the operator hide the section for routine review; persisted via cookie + URL param.

#### 5.2.2 In progress / Queued / Unknown claim state

- **In progress** — open rows that have at least one active claim, sorted by `created_at` desc.
- **Queued** — open rows with zero active claims, sorted by `created_at` desc.
- **Unknown claim state** — open rows past `tasks.visible_cap`. These rows render without claim chips. A footer banner reads: `Showing claim detail for the first <visible_cap> of <N> open rows — narrow your filters or click a row to load claims for the rest.`

The classic "claimed-state filter" (`any` / `known_claimed` / `known_unclaimed`) is **dropped** — these sections express the same intent structurally and avoid the silent-classification footgun.

#### 5.2.3 Completed / Cancelled (collapsed by default)

Both groups render as collapsible section headers:

```
▶ Completed (12 in last 30 days)
▶ Cancelled (3 in last 30 days)
```

Click expands. Expansion state persisted via cookie + URL param. When expanded, rows inside are flat, `created_at` desc, scoped to the last `tasks.default_time_range_days` days. SSE `task.completed` / `task.cancelled` events animate visible rows transitioning into these sections (and update header counts even when collapsed).

#### 5.2.4 Recent findings drawer

A collapsible side drawer (off by default) renders the last `tasks.recent_findings_drawer_size` `finding.posted` events across all tasks, newest first. Each row shows:

```
<agent> · <task title> · <relative time>
<summary, single-line, truncated>
```

Click → opens the parent task's detail panel (`/tasks?selected=<task_id>`).

The drawer is fed by a **server-side rolling buffer** (§5.6.4). It survives tab refresh and stays consistent across multiple open tabs.

#### 5.2.5 Notifications

- **Title-badge notifications** (always on by default; `[tasks].notifications.title_badge`): the page `<title>` updates from `Lithos Lens` to `(N) Lithos Lens` whenever there are unseen Needs-attention items. Tab focus clears the badge.
- **Desktop notifications** (opt-in; M3): an "Enable notifications" affordance appears in the header. Once granted, Lens fires a desktop notification only when a row *enters* Needs-attention (transition events, not steady-state); body format is `<task title> — <reason>`. Clicking the notification opens `/tasks?selected=<task_id>`.
- Notification grant state lives in `localStorage` (per-browser-install). All other persisted preferences live in cookies + URL.

### 5.3 Row Anatomy and Filters

#### 5.3.1 Row anatomy

Every list row renders a compact, scannable line with the following elements (specific layout left to the implementer; the data-shape contract is fixed):

| Element | Notes |
|---------|-------|
| **Project chip** | `project:<slug>` value rendered as a dedicated, visually distinct chip in the leftmost slot. Background colour = stable hash of slug. Rows without a `project:*` tag render `(no project)`. Tasks carrying multiple `project:*` tags render each chip and emit a soft warning to telemetry — Lens does not silently pick one. |
| **Title** | Truncated to one line; full title in tooltip |
| **Status badge** | `open` / `completed` / `cancelled` |
| **Reason chips** *(Needs attention only)* | One chip per rule fired: `expired-claim` / `stale-open` / `unclaimed-old`. Stalled (Planning View signal) renders as a row decoration where applicable. |
| **Latest finding line** *(open rows)* | One line: `<agent> — <summary>` plus relative timestamp. Sourced from the server-side rolling buffer (§5.6.4); falls back to the most recent entry in the per-task findings list when the buffer has no entry for that task. Updates on `finding.posted` SSE. |
| **Agent chips (collapsed by role)** | Single chip per agent appearing on the row, with role markers `created` / `claimed` / `latest`. When the same agent fills multiple roles, role markers collapse into one chip (e.g. `agent-zero · created · claimed · latest`). Agents listed in `[tasks].human_agents` render with a person-icon prefix and a distinct chip background. |
| **Active claims** *(open, in-progress, within visible cap)* | Compact list of `aspect → agent`, fetched via `lithos_task_status`. Each `aspect` rendered as a small chip nested under the claiming agent. |
| **Tags** | Chips for non-`project:*` tags; `key:value` shorthand renders as `key: value`. Reserved keys (`project:`, `[tasks].human_actionable_tag`) are surfaced via dedicated chips elsewhere on the row, not in the generic strip. |
| **Created at** | Relative time; absolute on hover |

Outcome, completion timestamp, cancellation timestamp, and duration are rendered opportunistically when Lithos exposes them; they are not required for MVP rendering.

#### 5.3.2 Filters

Filters appear in a sticky filter bar above the section list. All filters compose. Filter state reflects in the URL for shareability.

| Filter | Behaviour |
|--------|-----------|
| **Project** | First-class top-level dropdown sourced from `lithos_tags(prefix="project:")`. Scopes all sections (Needs attention, In progress, Queued, Unknown tail, Completed, Cancelled). Multi-select supported. URL: `?project=ganglion`. |
| **Status** | Multi-select section-group selector: `open` shows the four open-related sections; `completed` / `cancelled` show those flat groups expanded inline. URL: `?status=open,completed`. |
| **Tag** | Free-text input with `key:value` parsing. Excludes the reserved `project:*` and `[tasks].human_actionable_tag` keys (those have their own affordances). URL: `?tag=cli&tag=urgent`. |
| **Agent (with role)** | Dropdown sourced from `lithos_agent_list`; free-text fallback. **OR-across-roles by default**: a row matches if the agent appears as creator OR claimer OR latest-finding-poster. Toggle in the filter bar to narrow to a single role. URL: `?agent=agent-zero&agent_role=any` (default `any`; alternatives `creator`, `claimer`, `poster`). |
| **Created-at range** | Lower-bound date range using `lithos_task_list(since=...)`; defaults to "last `tasks.default_time_range_days` days" for completed/cancelled groups. Open sections ignore this range by default. |
| **Hide Needs attention** | Toggle to hide the Needs-attention section entirely. Persists via cookie + URL param. Default off. |

Filters preserve section structure even when scoped — a section header with no matching rows renders with a `no rows match current filters` placeholder rather than disappearing. This avoids the filter-hides-the-warning footgun.

Completed-at and cancelled-at filters are not in MVP — current Lithos does not expose them through `lithos_task_list`.

#### 5.3.3 Visible cap and degradation

The inline claim indicator, In progress / Queued split, and stalled-detection (Planning View) require one `lithos_task_status` call per open row. Lens batches these in parallel and caps the work at `tasks.visible_cap` (default 50). Beyond the cap:

- Rows past the cap render without the inline claim indicator in the **Unknown claim state** section.
- The Needs-attention section never includes Unknown-state rows (Lens won't silently classify them).
- Clicking a row past the cap fetches its `lithos_task_status` lazily (used to populate the side panel).

A future Lithos enhancement to embed active claims in `lithos_task_list` would eliminate this cap; Lens must not depend on that enhancement.

### 5.4 Task Detail: Side Panel + Full-Page Route

Clicking a row opens a **right-side panel** by default. The panel and the full-page route render the same content fragments (`detail.html`, `findings.html`) — single template path, two surfaces.

| Surface | URL | Use |
|---------|-----|-----|
| Side panel | `/tasks?selected=<task_id>` | Default. Triggered by clicking a row. Preserves list-section state and filter URL. |
| Full-page | `/tasks/{task_id}` | Triggered by the **Expand** button on the panel header. Shareable URL. Long-findings deep-dive. |

Closing the panel clears the `selected` URL param; the list state and filters are preserved.

#### 5.4.1 Panel content

| Section | Content |
|---------|---------|
| Header | Title, status, creating agent, `created_at`, project chip, **Expand** button |
| **Why this task is here** *(Needs attention only)* | Reason chips with one-line explanation (e.g. `Stale open — 9 days since created`, `Expired claim — agent-zero · ble-recover · expired 2h ago`) |
| Tags | Full tag list, one chip per tag (excludes `project:*` and `[tasks].human_actionable_tag`, which appear as dedicated chips) |
| Description | Markdown-rendered |
| Metadata | `metadata` dict rendered as a key-value table |
| Active claims | List of `aspect / claiming agent / expires_at / time remaining`; refreshed on SSE claim events |
| Findings | Full timeline (§5.5) |

If future Lithos versions expose `completed_at`, `outcome`, cancellation reason, or `claimed_at` through read tools, Lens may render those fields opportunistically. They are not MVP requirements.

#### 5.4.2 Direct task lookup

Direct `/tasks/{task_id}` links are first-class. Lens resolves them by scanning `lithos_task_list` across open, completed, and cancelled statuses (unbounded by date because v1 scale is expected to be at most a few hundred total tasks), then fetching `lithos_task_status(task_id)`. See §5.9.2 for the resolution algorithm.

### 5.5 Findings Timeline

`lithos_finding_list(task_id)` is called once when the detail panel opens, and again on every relevant SSE event for the open task.

#### 5.5.1 Default rendering

Findings render chronologically (oldest → newest). Each entry shows:
- Posting agent
- Timestamp (relative + absolute on hover)
- Summary text
- **Knowledge link** *(only when `finding.knowledge_id` is non-null)* — a clickable label that opens the corresponding note in the Knowledge Browser at `/note/{knowledge_id}`. The label is the note title, resolved by a single `lithos_read(id=knowledge_id)` per finding. If the read fails, the label falls back to "View document" with a non-blocking warning toast. *Until the Knowledge Browser ships, the link target falls back to a minimal `/note/{knowledge_id}` route that renders the note's title, content and tags as plain Markdown — enough to dereference the finding without the full browser experience.*

#### 5.5.2 Operational Rendering

Current Lithos returns all findings for a task, optionally filtered by `since`. Lens renders the full findings timeline for the selected task and does not provide paging controls in the MVP; the Tasks view is an operational dashboard, not a long-history browsing surface. For very long timelines, Lens may collapse older findings behind a "Show older findings" disclosure without introducing paginated navigation.

#### 5.5.3 Most-significant findings *(LLM, optional)*

When `llm.enabled = true` **and** `llm.findings_curation_enabled = true`, the timeline header shows a toggle: **All findings** / **Most significant**. The "Most significant" mode passes the full findings list (summaries + agents + timestamps) to the configured LiteLLM provider with a prompt that returns the K findings with the largest signal (typically completion announcements, decisions, surprises, contradictions), each with a one-line rationale. The complexity slider (§8.5) modulates verbosity. With LLM disabled the toggle is hidden.

### 5.6 SSE Auto-Update

#### 5.6.1 Connection model

A single SSE connection is held by the shared `app/events.py` utility. The Tasks-view router subscribes to **all** task-related event types (`task.created`, `task.claimed`, `task.released`, `task.completed`, `task.cancelled`, `finding.posted`, plus any future task-related events). Filtering happens client-side in Lens: events that don't match the current filter state are ignored for list updates but always counted into the summary panel.

#### 5.6.2 UI behaviour on event

| Event | UI effect |
|-------|-----------|
| `task.created` matching filters | Optimistically insert row at top of Queued (no claim payload yet); reconcile on next debounced refresh |
| `task.claimed` | Optimistically add/update the row's claim chip and move row from Queued to In progress (or refresh detail panel's Active claims if open). May promote/demote into/out of Needs attention if the resulting row triggers an `expired-claim` rule on next recompute |
| `task.released` | Optimistically remove that aspect from the row's claim chip; move row from In progress to Queued if the last claim was released |
| `task.completed` | Optimistically remove row from open sections and update Completed section header count (animate into the collapsed group) |
| `task.cancelled` | Optimistically remove row from open sections and update Cancelled section header count (animate into the collapsed group) |
| `finding.posted` for the row | Update the row's latest-finding line (`<agent> — <summary>`) and add a `latest` role marker to the agent chip; insert into the recent-findings rolling buffer; if detail panel is open for that task, re-render the timeline fragment |
| `finding.posted` not for any visible row | Insert into the recent-findings rolling buffer; surfaces in the drawer; no per-row badge |

Pushed updates are HTMX OOB swaps for the affected fragments — no full-page reload. Adds OTEL spans `lens.tasks.event` (per event handled) and `lens.tasks.refresh` (per manual / fallback refresh).

#### 5.6.3 Reconnection

On SSE disconnect Lens uses the exponential backoff schedule in `events.reconnect_backoff_ms`. While disconnected:
- Data **freezes at last-known state**. The dashboard contents do not change between fallback refreshes.
- A `Live updates paused — reconnecting` badge is visible in the header.
- The polling fallback runs every `tasks.auto_refresh_interval_s` seconds. Each successful fallback refresh fires a transient 1-second toast: `Refreshed via fallback`.

On successful reconnect the badge clears and the tasks list is fully reloaded once. No per-element staleness markers in MVP.

#### 5.6.4 Server-side metric recompute and rolling buffers

Several derived signals depend on aggregating across the task list (Needs-attention membership; In progress / Queued / Unknown counts; project starvation / bottleneck / stalled flags; throughput counts). SSE events do not carry "queue depth changed by 1," so Lens recomputes server-side.

**Strategy:** SSE events mark metrics dirty; a debounce window (`tasks.metrics_debounce_ms`, default 2000ms) batches bursts; one recompute fires per window. Recompute lives in `app/events.py` alongside the rolling-buffer maintenance — single source of truth. Recomputed fragments push to all open browser tabs via HTMX OOB swap through `/tasks/events`.

Manual refresh, page load, and SSE reconnect bypass the debounce and force an immediate recompute.

**Recent-findings rolling buffer.** A server-side ring buffer of the last `tasks.recent_findings_drawer_size` (default 50) `finding.posted` events powers both the "Recent findings" drawer and the per-row latest-finding line. Each entry stores: `finding_id`, `task_id`, `task_title` (resolved at insert time via list cache), `agent`, `summary`, `timestamp`, `knowledge_id` if present.

**Boot-time warm-up.** On Lens startup, for each currently-open task within `tasks.visible_cap`, fetch `lithos_finding_list(task_id, since=now - tasks.recent_findings_warmup_window_h)` once to seed the buffer. Warm-up window default 48h (≥ stalled threshold of 24h, with margin).

**Stalled detection (Planning View signal).** Open in-progress task with no `finding.posted` in the last `tasks.stalled_no_findings_hours` (default 24h). Computed only for tasks within `visible_cap` using the rolling buffer. Stalled rows get a row decoration on the Operator View but are **not** promoted into Needs attention.

**OTEL spans:** `lens.tasks.metrics_recompute` (attribute `trigger=sse|manual|reconnect|warmup`), `lens.tasks.findings_recent` (warm-up + drawer endpoint).

### 5.7 Cross-View Linking

#### 5.7.1 Tasks → Knowledge (MVP)

Findings with a non-null `knowledge_id` link directly to `/note/{knowledge_id}`. The browser opens with the note pre-selected. Until the Knowledge Browser ships, `/note/{knowledge_id}` renders a minimal Markdown view (title, tags, content) so finding links remain useful from day one. Once the Knowledge Browser is delivered, the same URL renders the full feed-detail panel — finding links require no change.

This is a straight UUID passthrough — no inference, no text matching, no schema change.

#### 5.7.2 Knowledge → Tasks (deferred)

Notes whose Lithos metadata records a producing task (for example `metadata.source` containing a task ID from the `source_task` write parameter) should display a "Produced by task X" badge in the feed view (§6.3) and graph view, linking back to `/tasks/{task_id}`. This is deferred because producers must consistently pass `source_task` when writing task-derived notes.

### 5.8 API

| Endpoint | Purpose |
|----------|---------|
| `GET /tasks` | Operator View: server-rendered dashboard with filter bar + section list |
| `GET /tasks?selected=<task_id>` | Same page with the side panel pre-opened on a task |
| `GET /tasks/{task_id}` | Full-page detail route (also serves the panel HTMX fragment when requested) |
| `GET /tasks/{task_id}/findings` | Findings timeline HTMX fragment |
| `GET /tasks/findings/recent` | Recent-findings drawer HTMX fragment, sourced from the server-side rolling buffer |
| `GET /tasks/plan` | Planning View (M1.5; see §5A) |
| `GET /tasks/events` | Server-Sent-Events endpoint Lens exposes to its own browser tabs — re-broadcasts Lithos events plus pushes recomputed metric fragments |
| `POST /api/tasks/findings/curate` | LLM-curated "most significant findings" endpoint (M3, when `llm.enabled`) |

No `POST` / `PUT` / `DELETE` endpoints touch task state — the read-only contract is enforced at the router level.

### 5.9 MVP Implementation Contract

This section is normative for Milestones 0–2. It exists to keep the Tasks View implementable against current Lithos without adding read APIs.

#### 5.9.1 Initial dashboard query flow

On `GET /tasks`, Lens performs these Lithos calls:

1. `lithos_task_list(status="open")` with no `since`
2. `lithos_task_list(status="completed", since=<created_range_start>)`
3. `lithos_task_list(status="cancelled", since=<created_range_start>)`
4. `lithos_stats()`
5. `lithos_agent_list()`
6. `lithos_task_status(task_id)` for open rows up to `tasks.visible_cap`

The completed/cancelled created range defaults to `now - tasks.default_time_range_days`. Open tasks ignore this range by default.

#### 5.9.2 Direct task lookup flow

`GET /tasks/{task_id}` is first-class even though current Lithos has no `lithos_task_get`.

Resolution algorithm:

1. Call `lithos_task_list(status="open")`.
2. Call `lithos_task_list(status="completed")`.
3. Call `lithos_task_list(status="cancelled")`.
4. Merge rows by `id`.
5. Find the requested `task_id`.
6. If found, call `lithos_task_status(task_id)` and `lithos_finding_list(task_id)`.
7. If not found, render a non-500 "Task not found in current Lithos task lists" panel with a link back to `/tasks`.

These three unbounded list calls are acceptable for v1 because the expected total task count is at most a few hundred.

#### 5.9.3 Detail panel query flow

When the user opens a row already present in the task list, Lens uses the row payload as the task record and calls:

1. `lithos_task_status(task_id)` for active claims
2. `lithos_finding_list(task_id)` for findings
3. `lithos_read(id=knowledge_id)` once per finding that has a non-null `knowledge_id`, with per-panel caching

If any of these calls fail, the panel renders the sections that succeeded and shows a retry affordance for the failed section.

#### 5.9.4 Section membership rules (replaces the legacy claimed-state filter)

The legacy `any` / `known_claimed` / `known_unclaimed` filter is dropped. Section membership is structural:

| Open row state | Section |
|----------------|---------|
| Within `visible_cap`, ≥1 claim, triggers no Needs-attention rule | In progress |
| Within `visible_cap`, 0 claims, triggers no Needs-attention rule | Queued |
| Within `visible_cap`, triggers any Needs-attention rule | Needs attention (single tier; row removed from In progress / Queued) |
| Beyond `visible_cap` | Unknown claim state tail |

Lens must not silently classify rows beyond the cap. The Unknown-state tail and its accuracy banner replace the legacy "filter covers the first 50" banner.

#### 5.9.4a Empty states

Four empty states must be tested explicitly:

| State | Behaviour |
|-------|-----------|
| **No tasks at all in Lithos** | Dedicated "No tasks yet" panel with a single help line — `Tasks are created via lithos_task_create from any agent. Lens is read-only.` Plus a link to the project-tracking conventions doc. No empty section headers. |
| **Tasks exist but none open** | Render the Operator View normally. The four open sections show `All clear — no open tasks`. Completed/Cancelled headers (collapsed) still visible below. |
| **All open healthy (no flagged signals)** | Needs-attention collapses to a thin `All systems healthy — 0 issues` stripe (kept visible for reassurance). In progress / Queued render normally. |
| **Lithos unreachable** | Degraded banner from §14. Page renders the banner with empty state below it. No stale-cache fallback in MVP. |

#### 5.9.5 Sparse SSE payload rules

Lithos task events are intentionally sparse. Lens optimistically updates visible UI from the payload where possible, then schedules a debounced reconciliation refresh.

Rules:

| Event | Immediate action | Reconciliation |
|-------|------------------|----------------|
| `task.created` | Insert skeleton open row with `task_id` and `title`; missing fields render as loading placeholders | Debounced list refresh |
| `task.claimed` | Add/update claim chip from `task_id`, `agent`, `aspect` | Refresh row status if row is visible or detail is open |
| `task.released` | Remove matching claim chip by `task_id` + `aspect` | Refresh row status if row is visible or detail is open |
| `task.completed` | Remove visible row from open sections; update Completed section header count | Debounced metric recompute + section refresh |
| `task.cancelled` | Remove visible row from open sections; update Cancelled section header count | Debounced metric recompute + section refresh |
| `finding.posted` | Insert into server-side rolling buffer; update row latest-finding line if visible; add `latest` role marker on the row's agent chip | If detail is open, refetch full `lithos_finding_list(task_id)` |

The reconciliation refresh should be debounced across bursts of events so a batch of task events does not trigger one full refresh per event.

#### 5.9.6 Browser event stream contract

Lens holds one server-side subscription to Lithos `/events`. Browser tabs do not connect directly to Lithos; they connect to `GET /tasks/events`.

`/tasks/events` emits normalized Lens events with this minimum shape:

```json
{
  "id": "<lithos-event-id>",
  "type": "task.claimed",
  "task_id": "<task-id>",
  "payload": {},
  "requires_refresh": true
}
```

Rules:
- Preserve the Lithos event `id` for browser-side dedupe.
- Include `requires_refresh=true` when the Lithos payload is too sparse for a complete UI update.
- Browser-side handlers should tolerate duplicate events and out-of-order reconciliation responses.

#### 5.9.7 Common route failure behavior

Task routes must not return HTTP 500 for expected Lithos availability or lookup failures.

| Situation | Route behavior |
|-----------|----------------|
| Lithos unreachable | Render degraded banner/panel with retry; preserve last successful render where available |
| Task ID not found | Render not-found panel with link to `/tasks` |
| `lithos_task_status` fails | Render task row/detail without claim section and show retry |
| `lithos_finding_list` fails | Render task metadata and show findings retry |
| `lithos_read` for finding link fails | Render "View document" fallback label and a warning toast |

---

## 5A. Tasks View — Planning View

The Planning View answers **"what should happen next?"** across the agent fleet. It ships in **M1.5**, after the Operator View stabilises. It lives at `/tasks/plan` and is reachable from the top-nav alongside the Operator View and the Knowledge Browser routes.

### 5A.1 Purpose & Scope

The Planning View is read-only and consumes the same Lithos read surface and SSE event stream as the Operator View. Its three stacked sections answer three sub-questions, top to bottom:

1. **What manual work do I need to pick up?** — Human-actionable section.
2. **Where is system throughput stuck?** — Project breakdown with starvation, bottleneck, stalled flags.
3. **What's the overall shape of work across projects?** — Throughput overview with completion / cancellation counts per project over a rolling window.

### 5A.2 Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Top-nav: [Tasks] [Tasks · Plan] [Knowledge ▾]              │
│  Filter bar: project | created-at range | hide dormant      │
├─────────────────────────────────────────────────────────────┤
│  👤 Human-actionable                                         │
│     open tasks tagged `[tasks].human_actionable_tag`,        │
│     grouped by project, oldest first; includes tasks         │
│     already claimed by a human-agent so you can resume       │
├─────────────────────────────────────────────────────────────┤
│  📊 Project breakdown                                        │
│     per project: queue depth, in-flight depth,               │
│     starvation / bottleneck / stalled flags                  │
├─────────────────────────────────────────────────────────────┤
│  📈 Throughput overview                                      │
│     per project: completed count, cancelled count,           │
│     completion ratio, over `tasks.throughput_window_days`    │
│     ordered by completed-count desc, dormant projects shown  │
└─────────────────────────────────────────────────────────────┘
```

### 5A.3 Human-actionable section

- **Membership**: open tasks carrying tag `[tasks].human_actionable_tag` (default `human`). Includes tasks already claimed by an agent listed in `[tasks].human_agents` (so you can resume your own work).
- **Grouping**: by project (`project:<slug>` chip), oldest first within each project. `(no project)` group renders last.
- **Row anatomy**: same as Operator View (§5.3.1) — title, project chip, status, tags, agent chips, latest finding line. Reason chips do not apply here (this section is its own selection rule).
- **Empty state**: `Nothing for you to do right now ✓`.

### 5A.4 Project breakdown

For every project from `lithos_tags(prefix="project:")`, render one row showing:

| Field | Value |
|-------|-------|
| Project chip | `project:<slug>` |
| Queue depth | Count of open rows with zero claims (within `visible_cap`) |
| In-flight depth | Count of open rows with ≥1 claim (within `visible_cap`) |
| Flag chips | Any of: `starvation`, `bottleneck`, `stalled` |

**Rules:**

| Flag | Rule |
|------|------|
| **Starvation** | Queue depth ≥ 1 AND in-flight depth = 0 |
| **Bottleneck** | In-flight depth ≥ `tasks.bottleneck_min_inflight` (default 3) AND one agent holds ≥ `tasks.bottleneck_concentration` (default 0.7) of those claims |
| **Stalled** | At least one in-progress task in the project has had no `finding.posted` in the last `tasks.stalled_no_findings_hours` (default 24h), per the rolling buffer |

Hover on a flag → tooltip with the rule details (which agent dominates the bottleneck; which task is stalled; etc.).

### 5A.5 Throughput overview

For every project (from `lithos_tags(prefix="project:")`), render one row covering the last `tasks.throughput_window_days` (default 30):

| Field | Value |
|-------|-------|
| Project chip | `project:<slug>` |
| Completed count | Tasks with `status=completed` and `created_at >= now - throughput_window_days` |
| Cancelled count | Tasks with `status=cancelled` and `created_at >= now - throughput_window_days` |
| Completion ratio | `completed / (completed + cancelled)` (or `—` when both zero) |

**Ordering:** completed count desc, then completion ratio desc, then alphabetical.

**Dormant projects** (zero completed, zero cancelled in window) are shown by default with explicit `0 / 0`. A `Hide dormant` toggle (cookie + URL) suppresses them.

> [!note] No sparklines in MVP
> A per-project sparkline of daily completions is deferred. MVP is counts only.

### 5A.6 Project discovery and caching

Both the project filter dropdown and the Project breakdown / Throughput sections read from `lithos_tags(prefix="project:")` on dashboard load, cached per request and shared with the Operator View. Cache invalidation: full page load, manual refresh, and `task.created` SSE events that carry a never-seen `project:*` tag.

### 5A.7 API

| Endpoint | Purpose |
|----------|---------|
| `GET /tasks/plan` | Server-rendered Planning View |
| `GET /tasks/plan/projects` | Project breakdown HTMX fragment (refreshable independently) |
| `GET /tasks/plan/throughput` | Throughput overview HTMX fragment |

### 5A.8 OTEL spans

| Span | Description |
|------|-------------|
| `lens.tasks.plan` | Planning View page render |
| `lens.tasks.plan.projects` | Project breakdown computation |
| `lens.tasks.plan.throughput` | Throughput overview computation |

---

## 5B. Project Tracking Conventions

These conventions are **normative for Lens** and assumed across the Tasks views and the future Knowledge Browser. Lens itself is read-only and does not enforce these on writes — they are conventions agents must follow when creating tasks and writing project-related notes.

### 5B.1 Project documents

All project-related knowledge documents are stored under `projects/<project-slug>/`. Every such document must be tagged with `project:<project-slug>` plus any relevant category tags. Documents describing the overall context or purpose of a project also receive the tag `project-context`.

```
lithos_write(
  title="Ganglion — Project Context",
  path="projects/ganglion",
  tags=["project:ganglion", "project-context"],
  ...
)
```

### 5B.2 Project tasks

Tasks are created via `lithos_task_create` and **must always be tagged** with `project:<project-slug>` at creation time.

```
lithos_task_create(
  title="Implement BLE reconnect logic",
  agent="agent-zero",
  tags=["project:ganglion", ...]
)
```

### 5B.3 Project slug naming

Slugs are derived from the project title (not from a directory or system name):

- Lowercase
- Spaces replaced with hyphens
- Special characters removed or replaced

| Title | Slug |
|-------|------|
| `Ralph++` | `ralph-plus-plus` |
| `Kindred Code` | `kindred-code` |
| `Restore SGI Indy` | `restore-sgi-indy` |

### 5B.4 Known active projects

| Project | Tag |
|---------|-----|
| Lithos Core | `project:lithos-core` |
| Lithos Ecosystem | `project:lithos-ecosystem` |
| Best Developer Year 2026 | `project:best-developer-year-2026` |
| Commit To Change | `project:commit-to-change` |
| Effective Agents | `project:effective-agents` |
| Financial Planning | `project:financial-planning` |
| Ganglion | `project:ganglion` |
| Influx | `project:influx` |
| Kindred Code | `project:kindred-code` |
| Ralph++ | `project:ralph-plus-plus` |
| Restore SGI Indy | `project:restore-sgi-indy` |
| vajra | `project:vajra` |
| NAO bridge | `project:nao-bridge` |
| NAO cross compilation toolchain | `project:nao-cross-compilation-toolchain` |
| Enterprise USB keyboard | `project:enterprise-usb-keyboard` |
| Cardinal | `project:cardinal` |

### 5B.5 Ideas

Speculative or early-stage items not yet linked to a project live under `ideas/` with the tag `idea`. Confidence `0.5–0.7`. If softly related to a project, they may carry the `project:<slug>` tag alongside `idea`. When promoted to a project, the document is moved to `projects/<slug>/`.

### 5B.6 Lens reading patterns

| Goal | Method |
|------|--------|
| All tasks for a project | `lithos_task_list(tags=["project:<slug>"])` |
| All docs for a project | `lithos_list(path_prefix="projects/<slug>/")` |
| Search within a project | `lithos_search(query="...", path_prefix="projects/<slug>/")` |
| Project context docs | `lithos_list(path_prefix="projects/", tags=["project-context"])` |
| All ideas | `lithos_list(tags=["idea"])` |

### 5B.7 Lens behaviour with multi-`project:*` tasks

A task carrying multiple `project:*` tags is unusual but supported:

- Operator View renders one project chip per tag and emits a soft warning to telemetry — Lens does not silently pick one.
- Planning View shows the task once per group it claims to be in (visible duplication is preferred over hidden behaviour).

### 5B.8 Configurability

The `project:` key is a default. `[tasks].project_tag_key` (default `"project"`) makes it configurable per deployment.

---

# Part C — Knowledge Browser

The Knowledge Browser is the second view. It provides feed, graph, search, feedback, comparison, reading paths, and conflict resolution over Lithos notes. Every section here assumes the Common Core (§1–§4) and (where indicated) the Tasks View are in place.

---

## 6. Feed View

### 6.1 Purpose

Time-ordered list of recently ingested items. The default landing view of the Knowledge Browser.

### 6.2 Data Source

```python
items = lithos_list(
    path_prefix="papers/",
    tags=([f"profile:{profile}"] if profile else []) + (selected_tags or []) or None,
    since=selected_since or None,
    limit=config.ui.feed_page_size,
    offset=page_offset,
)
```

### 6.3 UI Elements

| Element | Behaviour |
|---------|-----------|
| Filter bar | Profile dropdown, date range, tag chips, confidence slider |
| Paper card | Title, source URL/domain, confidence, tags, collapsed excerpt |
| Expandable excerpt | HTMX swap loads note content from `lithos_read(id=...)`; for Influx notes Lens may parse the `## Abstract` section when present |
| Archive link | For Influx notes, Lens may parse the `**Local file:** /archive/...` body line and link via `/archive/...`; no frontmatter `local_file` field is required |
| Source link | Opens `source_url` externally |
| 👍 / 👎 buttons | Write back via `lithos_write` (see §9) |
| Multi-select toggle | Enables comparison mode (see §10); shows checkbox on each card and a floating "Compare N notes" action |
| "Related" teaser | Calls `lithos_related(id=..., include=["edges", "links"], depth=1)` |
| "Open in graph" link | Jumps to graph view centred on this note's id |
| "Generate path" action | Opens reading-path picker (see §11) seeded from the current filter set or the selected card |
| "Produced by" badge *(deferred)* | When a note's Lithos metadata points at a producing task, link back to §5 — see §5.7 |

Current Influx note constraints:
- Profile membership is represented by `profile:<name>` tags, not by `papers/{profile}` paths
- Note-wide relevance is represented by Lithos `confidence` (max profile score / 10), while per-profile scores live in the note body under `## Profile Relevance`
- Authors, published date, abstract, and local archive path are body conventions, not guaranteed Lithos frontmatter fields
- The v1 Lens feed must render gracefully when those Influx body conventions are absent

### 6.4 Pagination

HTMX infinite scroll with `offset` passed as a query param. The `total` field from `lithos_list` is used to display "Showing N of M".

---

## 7. Graph View

### 7.1 Purpose

Interactive visualisation of the knowledge base as a typed, weighted graph. Helps surface clusters, contradictions, and lineage at a glance.

### 7.2 Data Sources

```python
# All edges in the Influx namespace (capped by config.ui.graph_max_nodes)
edges = lithos_edge_list(namespace="influx")

# Node detail panel
related = lithos_related(
    id=note_id,
    include=["edges", "links", "provenance"],
    depth=2,
)

# Node salience / usage stats
stats = lithos_node_stats(node_id=note_id)
```

### 7.3 Rendering

- **Nodes**: papers sized by `stats.salience` (or Lithos `confidence` fallback), coloured by profile tag (e.g. `profile:ai-robotics` → one hue, `profile:hema` → another)
- **Edges**: Cytoscape.js with distinct colours per type:

| Edge type | Colour | Source |
|-----------|--------|--------|
| `related_to` | 🔵 Blue | Semantic similarity (Influx) |
| `builds_on` | 🟢 Green | LLM extraction (Influx Tier 3) |
| `contradicts` | 🔴 Red | LCMA contradiction detection |
| `uses_method` | 🟡 Yellow | LCMA concept formation |
| `analogous_to` | 🟣 Purple | LCMA analogy scout |
| `derived_from` | ◻ Grey (dotted) | Frontmatter `derived_from_ids` projected into `edges.db` by Lithos reconcile |

### 7.4 Interactions

- Click a node → side panel with detail, `lithos_related`, `lithos_node_stats`
- Filter panel: profile, date, tag, confidence / parsed profile score, edge type, namespace
- "Centre on this" action rebuilds the graph around a single node at depth 2
- Ctrl/cmd-click adds nodes to the comparison multi-select (see §10)
- "Path from here" action seeds a reading path (see §11) from the selected node
- If the raw edge count exceeds `ui.graph_max_nodes`, the view degrades to a paged sample with a warning banner

### 7.5 Performance Notes

- Cytoscape.js comfortably handles ~10K nodes on a modern browser
- For larger graphs, apply `namespace_filter` or `path_prefix` before fetch
- Edge fetching happens once per view load; subsequent filter changes operate on the client-side dataset

### 7.6 Centrality Overlay

When `ui.graph_centrality_overlay = true` (or the user toggles "Highlight bridge nodes" in the filter panel), Lens computes betweenness centrality client-side over the currently-loaded edge set using Cytoscape's built-in `cy.elements().bc()`. The top-K nodes (default 5%, configurable via the toggle) render with a halo / outline ring. Centrality is recomputed whenever the visible subgraph changes (e.g. when an edge-type filter is toggled). No new MCP calls are required. Adds OTEL span `lens.graph.centrality`.

### 7.7 Bidirectional Node ↔ Panel Selection

The existing flow is one-way: click node → side panel updates. v0.5 adds the reverse: clicking a related-paper row in the side panel highlights and centres the corresponding node in the graph (without rebuilding the layout). This makes the panel the primary navigation surface as well as a read-only detail view. No new data fetch — pure UI wiring on the already-loaded graph.

---

## 8. Cognitive Search

### 8.1 Search Bar

Every Knowledge Browser view has a search bar. Backed by `lithos_retrieve` (LCMA MVP1) rather than plain `lithos_search`, because retrieval runs seven scouts with reranking and gives an audit receipt:

```python
results = lithos_retrieve(
    query=search_query,
    limit=config.search.default_limit,
    agent_id="lithos-lens",
    tags=[f"profile:{active_profile}"] if active_profile else None,
)
```

### 8.2 Results Rendering

Each result is rendered as a compact card:

- Title + score
- `snippet` (from Lithos)
- `reasons` (LCMA-specific audit — which scouts fired)
- `scouts` list (chips)
- Click-through to feed-view detail or graph-view node

### 8.3 Search Scope

A toggle exposes `namespace_filter` so the user can scope search to a single profile's namespace or leave it broad.

### 8.4 Answer Synthesis (LLM, optional)

When `llm.enabled = true`, the search bar shows a "Synthesise answer" toggle alongside the result list. When toggled on, Lens:

1. Calls `lithos_retrieve` as normal.
2. Passes the top-N snippets (each with `id`, `path`, `snippet`, `reasons`) to the configured LLM along with the query.
3. The system prompt **requires** that every claim in the synthesised answer carry an inline citation referencing the snippet `id` (e.g. `[1]`, `[2]`).
4. Renders the synthesised answer as a single block above the result list, with click-through citations linking to the corresponding feed-detail or graph-node view. The underlying retrieve result list stays visible below for transparency.

If `llm.synthesis_prefer_mcp = true` and Lithos exposes a synthesis MCP tool (e.g. `lithos_synthesize`), Lens calls that tool first and only falls back to the local LLM if it returns `not_supported` or errors.

**Failure modes**: LLM error → hide the synthesised block, keep the result list, show a non-blocking warning badge ("Synthesis unavailable — showing raw results"). Adds OTEL span `lens.llm.synthesize`.

### 8.4.1 LiteLLM Provider Contract

Lens uses LiteLLM for direct LLM calls when `llm.enabled = true`. The app passes the configured model string through to LiteLLM, so provider selection is configuration, not code branching. Supported deployments include hosted providers such as OpenAI and Anthropic, routing providers such as OpenRouter, and local providers such as Ollama. Provider-specific base URLs, API keys, and extra headers are configuration values (`LENS_LLM_BASE_URL`, `LENS_LLM_API_KEY`, `LENS_LLM_EXTRA_HEADERS_JSON`). Lens does not special-case "local" vs "remote" in feature logic; operators choose the provider appropriate for their privacy and cost constraints.

### 8.5 Complexity Slider (LLM, optional)

A session-scoped slider (1 = beginner … 5 = expert), default = `llm.default_complexity`, is exposed in the search bar and in any LLM-augmented panel (synthesis result, comparison themes tab, LLM-curated paths, "most significant findings" in the tasks view). The selected value is persisted via cookie or query param and injected into every LLM prompt as a system instruction modulating verbosity and technicality. The slider has no effect when `llm.enabled = false` and is hidden entirely in that mode.

Rationale: Paperlens demonstrated this is a high-value, low-cost UX lever — the same retrieved evidence yields visibly different explanations for different audiences without changing the underlying data.

---

## 9. Feedback Mechanism

### 9.1 Overview

Lens is the only place in the system where humans generate feedback. For Influx-authored notes, feedback is profile-scoped and stored as tags on the existing Lithos note: `influx:rejected:<profile>` for rejection and, if positive reinforcement is enabled, `influx:accepted:<profile>` for acceptance. Unscoped `influx:rejected` is not used because Influx consumes profile-specific rejection tags.

### 9.2 Writing Feedback — Critical Contract

> [!warning] `lithos_write` requires `title` and `content` on every call
> Even when only updating tags on an existing note, `lithos_write` requires `title`, `content`, and `agent` at the MCP boundary. Lens must first `lithos_read(id=...)` the note and pass the existing title+content back through. Omitted optional fields preserve existing values, but `title`/`content`/`agent` are never optional.

```python
existing = lithos_read(id=note_id)
existing_tags = existing["metadata"].get("tags", [])
profile_tag = f"profile:{profile}"
rejected_tag = f"influx:rejected:{profile}"
accepted_tag = f"influx:accepted:{profile}"
new_tags = [t for t in existing_tags if t not in {rejected_tag, accepted_tag}]
if accepted:
    new_tags.append(accepted_tag)
else:
    new_tags = [t for t in new_tags if t != profile_tag]
    new_tags.append(rejected_tag)

lithos_write(
    id=note_id,
    title=existing["title"],
    content=existing["content"],
    agent="lithos-lens",
    tags=new_tags,
    confidence=existing["metadata"].get("confidence", 1.0),
    expected_version=existing["metadata"].get("version"),  # optimistic lock
)
```

Handle `{status: "error", code: "version_conflict"}` by re-reading and retrying.

### 9.3 Feedback Entry Points

| Entry point | Mechanism |
|-------------|-----------|
| Feed view | 👍 / 👎 buttons on each paper card |
| Graph view | 👎 button in node detail panel |
| Notification link | `[not relevant]` link from Agent Zero digest → `POST /api/feedback` |

### 9.4 Feedback API

```
POST /api/feedback
{
  "note_id": "<uuid>",
  "profile": "<profile-name>",
  "verdict": "accepted" | "rejected",
  "note": "<optional user comment>"
}
```

Response: `{"status": "ok"}` on success, `{"status": "error", "message": "..."}` on failure. Errors are shown in the UI — never silently swallowed.

### 9.5 Downstream Effect on Influx

Influx picks up feedback at the start of each run by calling `lithos_list(tags=[f"influx:rejected:{profile_name}"])` — see Influx requirements §12. Lens does not have to notify Influx directly. Profile-scoped Lens views SHOULD exclude notes carrying `influx:rejected:<active-profile>` by default.

---

## 10. Note Comparison

### 10.1 Purpose

Place two or more notes side-by-side to surface what they share and where they diverge. Particularly useful for evaluating papers that look similar in the feed view but differ in method, scope, or claim, and for inspecting both endpoints of a `contradicts` edge before resolving it.

### 10.2 Entry Points

| Entry point | Mechanism |
|-------------|-----------|
| Feed view | Multi-select toggle reveals checkboxes on cards; floating "Compare N notes" action opens the comparison view |
| Graph view | Ctrl/cmd-click on nodes to add to the selection; "Compare selected" button in the side panel |
| Search results | "Compare with…" action on any result card |
| Conflict resolution UI | "Compare endpoints" button on a `contradicts` edge auto-loads its `from_id` and `to_id` |

The maximum number of notes that can be compared is configurable via `ui.compare_max_notes` (default 4).

### 10.3 Layout

The comparison view is a horizontally scrollable side-by-side panel with three tabs:

- **Metadata** — titles, source URL/domain, updated date, profile tags, confidence, ids, and any parsed Influx body conventions (authors, published date, profile-specific score) when present. Shared values (e.g. overlapping tags) are highlighted. No LLM required.
- **Content** — collapsed/expandable abstracts and bodies fetched via `lithos_read`; tunable character limit per pane to avoid runaway panels. No LLM required.
- **Themes & Concepts** *(only when `llm.enabled = true`)* — Lens passes the selected notes' titles + abstracts (or full content under a per-call token budget) to the LLM with a structured prompt that returns:
  - A bullet list of dominant themes per note
  - A shared-concepts table (concept → notes mentioning it → terminology variant in each note)
  - A unique-concepts list per note
  - One paragraph summarising similarities, differences, and complementarity
  When `llm.enabled = false` this tab is hidden.

### 10.4 API

```
POST /api/compare
{
  "note_ids": ["<uuid>", "<uuid>", ...],
  "include_themes": true        # ignored when llm.enabled = false
}
```

Response: `{"status": "ok", "metadata": [...], "content": [...], "themes": {...} | null}`. Comparison is read-only — it does not write notes back to Lithos.

Adds OTEL span `lens.compare`.

---

## 11. Reading Paths

### 11.1 Purpose

Surface an ordered traversal through a subset of notes — a "what should I read next, and in what order?" view. Distinct from search (which ranks for relevance) and from the graph (which shows topology without ordering). Inspired by Paperlens's curated learning paths but generalised to any Lithos namespace.

### 11.2 Entry Points

| Entry point | Mechanism |
|-------------|-----------|
| Feed view | "Generate path" button in the filter bar uses the active filter set as the candidate pool |
| Graph view | "Path from here" action on a selected node uses that node as the seed and walks outward |
| Settings view | "Generate path for profile" link uses the profile namespace as the pool |

### 11.3 Modes

| Mode | Description | LLM required |
|------|-------------|--------------|
| `salience` | Order by `lithos_node_stats.salience` desc | No |
| `chronological` | Order by ingestion date asc (matches a "build-up" reading style) | No |
| `edge-traversal` | BFS / topological walk over `builds_on` and `derived_from` edges starting from the seed node | No |
| `llm` | Pass node titles + summaries to the LLM with a "produce a pedagogical reading order with one-line justifications" prompt | Yes |

Default mode is `ui.reading_path_default` (default `salience`). The picker UI lets the user override per-request. The `llm` mode is hidden when `llm.enabled = false`.

### 11.4 Output

Output is a single ordered list rendered as a printable, shareable page at `GET /path/{slug}`. Each step shows position, title, one-line justification (manual for non-LLM modes, LLM-generated otherwise), and a deep link to the note's feed-detail view.

The user can save a path. Saved paths persist as a Lens-authored Lithos note via `lithos_write` with `path="lens/paths"`, `note_type="summary"`, and tags such as `lens:path` and `lens:path-mode:<mode>`. Because current `lithos_write` does not accept arbitrary frontmatter fields, the seed, mode, filter set, and ordered ids are stored in a small structured block in the note body, followed by the human-readable reading path.

### 11.5 API

```
POST /api/path
{
  "seed_id": "<uuid> | null",
  "filter": { "profile": "...", "tags": [...], "since": "..." },
  "mode": "salience | chronological | edge-traversal | llm",
  "limit": 20,
  "save_as": "<slug> | null"
}
```

Response: `{"status": "ok", "slug": "...", "steps": [{"id": "...", "title": "...", "rationale": "..."}, ...]}`.

Adds OTEL span `lens.path.generate`.

---

## 12. Conflict Resolution UI

When `contradicts` edges exist, Lens exposes a resolution panel on the relevant nodes.

```python
lithos_conflict_resolve(
    edge_id=edge_id,
    resolution="superseded",   # accepted_dual | superseded | refuted | merged
    resolver="user",
    winner_id=winning_note_id,  # required when resolution == "superseded"
)
```

UI affordances:

- Resolution dropdown with the four valid values
- Winner picker (required for `superseded`), choosing between the edge's `from_id` and `to_id`
- Optional free-form reason field is a UI-only annotation in the MVP and is not persisted, because current `lithos_conflict_resolve` does not accept a reason parameter. Persisted resolution notes are deferred.
- "Compare endpoints" button (see §10.2) for side-by-side inspection before resolving

On success, the edge is re-fetched and redrawn with a resolution badge. Unresolved contradictions are surfaced in a "Needs attention" banner on the feed view.

---

# Part D — Reference

Cross-cutting concerns, reference tables, and the implementation plan.

---

## 13. Settings View

Read-only. Displays:

- Current Influx profiles (names, descriptions, thresholds) — parsed from `/etc/influx/config.toml`
- Current feed list per profile
- Current model assignments
- Current telemetry flags
- Current LLM flags (`llm.enabled`, provider, model, complexity default, findings curation flag) — values only, no API key disclosure
- Current Tasks-view tuning (auto-refresh interval, visible cap, default time range)
- Lithos SSE connection status (live / reconnecting / disabled), last successful event time
- Recent Influx run/backfill tasks from Lithos (`lithos_task_list(tags=["influx:run"])` and `lithos_task_list(tags=["influx:backfill"])`), limited to fields Lithos currently exposes

Editing happens by changing the TOML file or env vars outside the container. Lens does not write to config.

---

## 14. Resilience & Error Handling

| Failure | Behaviour |
|---------|-----------|
| Lithos unreachable | Show banner "Lithos is offline"; disable all writes; retry transparently on next request |
| `lithos_write` returns `version_conflict` | Re-read note, merge feedback tags, retry once |
| `lithos_write` returns `slug_collision` | Not expected on update — surface as error |
| `lithos_retrieve` errors | Fall back to `lithos_search` with a warning badge |
| `lithos_node_stats` returns `doc_not_found` | Render with default salience values |
| Archive file missing on disk | Link still renders; 404 response from `/archive/...` shows a placeholder icon |
| Feedback write fails | Toast error; do not silently drop; retry button offered |
| Graph edge count exceeds `ui.graph_max_nodes` | Paged sample + warning banner |
| `llm.enabled = false` | Synthesis toggle, comparison "Themes" tab, complexity slider, `llm` reading-path mode, and "Most significant findings" toggle are hidden; remaining UI fully functional |
| LLM provider error (transient) | Per-feature failure: synthesis hides and result list still renders; comparison falls back to metadata + content tabs only; reading path falls back to `salience` mode; tasks findings fall back to "All findings"; non-blocking toast with retry |
| LLM provider misconfigured at startup | Log error, set effective `llm.enabled = false`, surface warning in settings view |
| Centrality computation fails | Disable overlay; toast warning; rest of graph unaffected |
| `lithos_task_list` errors | Tasks view shows error banner with retry; preserves last successful render so the page is not blank |
| `lithos_task_status` errors for a row | Row renders without inline claim indicator; tooltip explains; rest of list unaffected |
| `lithos_finding_list` errors | Detail panel shows "Could not load findings — retry"; metadata sections still render |
| `lithos_read` for a `finding.knowledge_id` errors | Finding link label falls back to "View document"; warning toast once per panel open |
| SSE event stream disconnects | Show "Live updates paused — reconnecting" badge; manual `tasks.auto_refresh_interval_s` polling takes over; on reconnect, full reload of visible list |
| SSE event stream not supported by Lithos build | Disable SSE silently after initial connect failure; rely on polling; surface state in settings view |

---

## 15. Observability

### OTEL — Opt-In, Additive

Same pattern as Lithos and Influx:

- `LENS_OTEL_ENABLED=true` enables it
- Optional packages installed via `uv sync --extra otel`
- `LENS_OTEL_CONSOLE_FALLBACK=true` prints spans to stdout

**Key spans:**

| Span | Description |
|------|-------------|
| `lens.request` | Each HTTP request |
| `lens.tasks.list` | Tasks list fetch (`lithos_task_list` + per-row `lithos_task_status` fan-out) |
| `lens.tasks.detail` | Task detail panel fetch (`lithos_task_status` + current `lithos_finding_list` result) |
| `lens.tasks.findings` | Findings page fetch |
| `lens.tasks.event` | Single SSE event handled (one span per event, attribute `event.type`) |
| `lens.tasks.refresh` | Manual / polling-fallback refresh |
| `lens.tasks.curate` | LLM "most significant findings" call |
| `lens.events.connect` | SSE connection lifecycle (connect / reconnect / disconnect) |
| `lens.feed.list` | Feed-view data fetch |
| `lens.graph.edges` | Graph-view edge fetch |
| `lens.graph.centrality` | Centrality overlay computation |
| `lens.retrieve` | Cognitive search call |
| `lens.llm.synthesize` | Answer synthesis (MCP or local LLM) |
| `lens.compare` | Multi-note comparison |
| `lens.path.generate` | Reading-path generation |
| `lens.feedback.write` | Feedback write to Lithos |
| `lens.archive.serve` | Archive file serve |

### Logging

- stdout only; structured JSON via `python-json-logger`
- `LENS_LOG_LEVEL` controls verbosity

### Health Endpoint

```
GET /health → {
  "status": "ok",
  "lithos": "ok" | "degraded" | "unreachable",
  "events": "live" | "reconnecting" | "disabled",
  "llm": "disabled" | "ok" | "error"
}
```

The `lithos` status is derived from a single `lithos_stats()` call on start-up plus a cached result refreshed every 30 seconds. The `events` status reports the SSE subscription state. The `llm` status reports the configured provider's reachability when `llm.enabled = true`, else `"disabled"`.

---

## 16. API Reference

### 16.1 Lithos MCP API — Lens Usage

| Tool | Required args | Purpose |
|------|---------------|---------|
| `lithos_task_list(status?, tags?, agent?, since?)` | none | Tasks view primary list query; `since` is a created-at lower bound |
| `lithos_task_status(task_id)` | `task_id` | Active claims for a task; called per visible row up to `tasks.visible_cap`, and on demand for the detail panel |
| `lithos_finding_list(task_id, since?)` | `task_id` | Findings timeline in the task detail panel; Lens renders the current task timeline without paging controls |
| `lithos_stats()` | none | Health endpoint status probe; tasks-summary tile counts |
| `lithos_agent_register(id, name?, type?)` | `id` | Startup auto-registration for Lens |
| `lithos_agent_list(type?, active_since?)` | none | Drives the Tasks-view "creating agent" filter dropdown |
| `lithos_list(path_prefix?, tags?, since?, limit?, offset?)` | none | Feed view paper listing |
| `lithos_read(id)` | `id` | Paper detail; feedback writes; resolving `finding.knowledge_id` to a title |
| `lithos_retrieve(query, limit?, agent_id?, tags?)` | `query` | Cognitive search bar |
| `lithos_search(query, mode?, tags?, ...)` | `query` | Fallback search when `retrieve` errors |
| `lithos_edge_list(namespace?, type?)` | none | Graph edge data; client-side centrality |
| `lithos_related(id, include?, depth?, namespace?)` | `id` | Node detail panel; seed for `edge-traversal` reading paths |
| `lithos_node_stats(node_id)` | `node_id` | Node salience and retrieval stats; `salience` reading-path mode |
| `lithos_conflict_resolve(edge_id, resolution, resolver, winner_id?)` | first three | Contradiction resolution UI |
| `lithos_write(title, content, agent, id?, tags?, confidence?, expected_version?, path?, note_type?)` | `title`, `content`, `agent` | Feedback writes; persisted reading paths under `path="lens/paths"` |
| `lithos_tags(prefix?)` | none | Tag cloud / filter panel |
| `lithos_synthesize(query, snippet_ids, agent_id?)` *(future, MVP 3+)* | `query`, `snippet_ids` | Preferred over local LLM for answer synthesis when present; Lens falls back to local LLM otherwise |

#### 16.1.1 SSE event stream

Lens consumes the Lithos SSE event stream at `${LITHOS_URL}${LITHOS_SSE_EVENTS_PATH}`. Event types Lens handles today:

| Event | Consumer |
|-------|----------|
| `task.created` / `task.claimed` / `task.released` / `task.completed` / `task.cancelled` | Tasks view list and summary |
| `finding.posted` | Tasks view findings timeline and "+N findings" badge |
| `note.created` / `edge.upserted` *(future)* | Knowledge Browser live graph updates |

### 16.2 Lens Internal HTTP Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Default view (`ui.default_view`, defaults to `tasks` until the Knowledge Browser ships) |
| `GET /tasks` | Tasks dashboard |
| `GET /tasks/{task_id}` | Task detail HTMX fragment |
| `GET /tasks/{task_id}/findings` | Findings page HTMX fragment |
| `GET /tasks/events` | Server-Sent-Events stream re-broadcast to the browser tab |
| `POST /api/tasks/findings/curate` | LLM "most significant findings" (only when `llm.enabled`) |
| `GET /note/{id}` | Note detail panel (minimal during Tasks-only milestones; full feed-detail once the Knowledge Browser ships) |
| `GET /knowledge` | Knowledge Browser feed view |
| `GET /graph` | Graph view |
| `GET /search?q=...` | Search results |
| `POST /api/feedback` | Feedback write endpoint |
| `POST /api/conflict/resolve` | Conflict resolution submission |
| `POST /api/synthesize` | Answer synthesis (only when `llm.enabled` or `lithos_synthesize` is available) |
| `POST /api/compare` | Multi-note comparison |
| `POST /api/path` | Reading-path generation |
| `GET /path/{slug}` | Render a saved reading path |
| `GET /settings` | Read-only settings view |
| `GET /archive/{path}` | Stream archived files from the mounted volume |
| `GET /health` | Health probe |

---

## 17. Implementation Plan

> [!note] Tasks View ships first
> Milestones 0–3 deliver the common core, the Tasks View MVP, observability, and Tasks SSE auto-update. Only after that does the Knowledge Browser begin (Milestones 4–9). The post-Tasks priority is the **knowledge cockpit** — feed, search, graph, comparison, reading paths, and curation — rather than a deeper operator triage product. Milestone 10 (semantic projection) is deferred and depends on Lithos exposing embeddings.

### Milestone 0 — Common Core (v0.1)
*Goal: shared scaffolding both views build on*

- [ ] Project scaffold: `pyproject.toml`, `Dockerfile`, FastAPI app skeleton
- [ ] `app/main.py` with view-switcher top-nav and `base.html`
- [ ] `app/config.py` — TOML + env loader, typed config object
- [ ] `app/lithos_client.py` — Lithos MCP client (SSE transport for tools)
- [ ] Startup auto-registration via `lithos_agent_register(id=LENS_AGENT_ID, name="Lithos Lens", type="web-ui")`
- [ ] `app/events.py` — single SSE subscription to Lithos's event stream + in-process pub/sub (skeleton; consumers wired in M2)
- [ ] `app/telemetry.py` skeleton with OTEL setup and `@traced` decorator; `lens.request` span on every route
- [ ] `/health` endpoint reporting `lithos`, `events`, `llm` statuses
- [ ] Read-only Settings view skeleton
- [ ] `docker-compose.yml` with `influx-archive` and `influx-config` mounts
- [ ] `run.sh`
- [ ] Structured JSON logging

**M0 acceptance:**
- App boots with Lithos offline and `/health` reports `lithos="unreachable"` without process failure
- `/` and `/tasks` render a degraded banner instead of HTTP 500 when Lithos is offline
- Startup attempts `lithos_agent_register` when Lithos is reachable
- Static templates reference vendored local assets, not public CDN URLs
- `docs/vendor-assets.md` records vendored asset versions and checksums
- With `LENS_LLM_ENABLED=false`, missing LiteLLM dependencies do not prevent boot

### Milestone 1 — Operator View MVP (v0.2)
*Goal: live, read-only Operator View over Lithos tasks with the section structure, project tagging, recent-findings drawer, and title-badge notifications.*

- [ ] `GET /tasks` route + `app/routers/tasks.py`
- [ ] Operator View section structure: **Needs attention** (severity-ordered: expired-claim → stale-open → unclaimed-old) → **In progress** → **Queued** → **Unknown claim state** tail → collapsed **Completed** → collapsed **Cancelled**
- [ ] Reason chips on Needs-attention rows; row de-duplication so flagged rows appear only in Needs attention
- [ ] Project chip per row (configurable `[tasks].project_tag_key`); rows without a project tag render `(no project)`
- [ ] Latest finding inline on each open row (`<agent> — <summary>` + relative time), updates on `finding.posted`
- [ ] Agent chips with role markers (`created` / `claimed` / `latest`) collapsed to one chip per agent
- [ ] Human-agent visual distinction (person-icon prefix, distinct chip background) for agents listed in `[tasks].human_agents`
- [ ] Filters: project (first-class), status (multi-select group selector), tag (`key:value` parsing), agent with OR-across-roles + role-narrow toggle (`creator` / `claimer` / `poster` / `any`), created-at range (open sections ignore by default), Hide-Needs-attention toggle
- [ ] URL-reflected filter and section-collapse state; `?selected=<task_id>` opens side panel
- [ ] Right-side panel + **Expand** button → `/tasks/{task_id}` full-page route; both surfaces reuse `detail.html` / `findings.html`
- [ ] Detail panel "Why this task is here" block on Needs-attention rows
- [ ] Per-row `lithos_task_status` fan-out up to `tasks.visible_cap`; Unknown-state tail with accuracy banner past the cap
- [ ] Findings timeline rendered without paging controls; per-finding `lithos_read` for non-null `knowledge_id` to render title-labelled links; fallback label on read failure
- [ ] Click-through from finding link to a minimal `/note/{knowledge_id}` route (full feed-detail arrives in M5)
- [ ] **SSE pipeline**: shared `app/events.py` subscription; tasks router subscribes to task / claim / finding event types; HTMX OOB swaps for optimistic row insert, optimistic row move-between-sections, claim indicator update, latest-finding update, section-count update
- [ ] **`GET /tasks/events`** browser re-broadcast endpoint
- [ ] **Server-side metric recompute** debounced at `tasks.metrics_debounce_ms` (default 2000ms); manual refresh / page load / SSE reconnect bypass debounce
- [ ] **Server-side recent-findings rolling buffer** of size `tasks.recent_findings_drawer_size` (default 50), with boot-time warm-up over `tasks.recent_findings_warmup_window_h` (default 48h)
- [ ] **`GET /tasks/findings/recent`** drawer endpoint + collapsible drawer UI (off by default)
- [ ] Reconnect with exponential backoff; `Live updates paused — reconnecting` badge during disconnect; polling fallback at `tasks.auto_refresh_interval_s`; transient `Refreshed via fallback` toast on each fallback success
- [ ] **Title-badge notifications** (`(N) Lithos Lens`) for unseen Needs-attention items; cleared on tab focus; `[tasks].notifications.title_badge` toggle
- [ ] Empty states: no tasks at all / tasks but none open / all open healthy / Lithos unreachable — all four explicitly tested
- [ ] OTEL spans: `lens.tasks.list`, `lens.tasks.detail`, `lens.tasks.findings`, `lens.tasks.findings_recent`, `lens.tasks.refresh`, `lens.tasks.event`, `lens.tasks.metrics_recompute`, `lens.events.connect`
- [ ] Set `ui.default_view = "tasks"` so `/` lands here

**M1 acceptance:**
- Section structure renders correctly: a row with an expired claim appears only in Needs attention with an `expired-claim` reason chip; same row does not appear in In progress; section header counts agree with rendered rows
- A row with no claims appears only in Queued; once claimed via SSE, it animates into In progress without page reload
- Project chip renders on every row; the project filter (top-level) scopes all sections; multi-`project:*` tasks render multiple chips and emit a telemetry warning
- Latest-finding line updates within ~1s of a `finding.posted` SSE event; row's agent chip gains a `latest` role marker
- Side-panel + Expand-to-`/tasks/{task_id}` both render the same content; URL `/tasks?selected=<id>` deep-links to the panel
- Direct `/tasks/{task_id}` works for open, completed, and cancelled tasks by scanning current Lithos task lists; unknown ID renders a not-found panel (not HTTP 500)
- Recent findings drawer opens in <200ms from the rolling buffer with no blocking MCP calls; surviving across tab refresh
- Title badge updates to `(N) Lithos Lens` when N rows enter Needs attention; clears on tab focus
- `lithos_tags(prefix="project:")` is fetched once per page load and shared across project filter / Operator-view rendering
- All four empty states render the specified content; Lithos-unreachable banner appears without the page erroring
- SSE disconnect shows the paused badge; polling fallback fires the `Refreshed via fallback` toast on each successful refresh
- `LENS_LLM_ENABLED=false` does not break boot or hide any operator-view affordance (LLM features are not in M1)

### Milestone 1.5 — Planning View (v0.3)
*Goal: ship `/tasks/plan` with Human-actionable, Project breakdown, and Throughput overview sections — answers "what should happen next?"*

- [ ] `GET /tasks/plan` route + `app/routers/tasks_plan.py`
- [ ] **Human-actionable section**: open tasks tagged `[tasks].human_actionable_tag` (default `human`), grouped by project, oldest first; includes tasks claimed by an agent listed in `[tasks].human_agents`
- [ ] **Project breakdown section**: per-project queue depth, in-flight depth, flag chips (`starvation`, `bottleneck`, `stalled`); flag tooltips show rule details
- [ ] **Throughput overview section**: per-project completed count, cancelled count, completion ratio over `tasks.throughput_window_days` (default 30d); ordered by completed desc / ratio desc / alphabetical; dormant projects shown by default with `0 / 0`
- [ ] `Hide dormant` toggle (cookie + URL)
- [ ] Filter bar: project (multi-select), created-at range (within window), Hide-dormant
- [ ] **Stalled detection** integrated into the rolling buffer: in-progress task with no `finding.posted` in `tasks.stalled_no_findings_hours` (default 24h); also drives row decoration on the Operator View
- [ ] **Bottleneck detection**: in-flight depth ≥ `tasks.bottleneck_min_inflight` (default 3) AND one agent holds ≥ `tasks.bottleneck_concentration` (default 0.7) of those claims
- [ ] **Starvation detection**: queue depth ≥ 1 AND in-flight depth = 0
- [ ] HTMX fragment endpoints `GET /tasks/plan/projects` and `GET /tasks/plan/throughput`
- [ ] Top-nav switcher links Operator View ↔ Planning View ↔ (future) Knowledge Browser; switching resets view-specific filter state
- [ ] OTEL spans: `lens.tasks.plan`, `lens.tasks.plan.projects`, `lens.tasks.plan.throughput`

**M1.5 acceptance:**
- A project with one queued task and no in-flight tasks displays a `starvation` flag
- A project with 5 in-flight tasks where 4 are claimed by `agent-zero` displays a `bottleneck` flag with hover tooltip naming `agent-zero`
- A project with one in-progress task that has had no `finding.posted` in 25h displays a `stalled` flag
- Throughput overview correctly sums completed and cancelled tasks within `tasks.throughput_window_days`
- Dormant projects (zero activity in window) appear by default with `0 / 0` and are hidden when `Hide dormant` is enabled
- Human-actionable section renders open tasks tagged `human`, grouped by project; navigation back to Operator View preserves no Planning-View filter state

### Milestone 2 — (renumbered into M1) — REMOVED
*M1 now bundles SSE auto-update; the previous M1/M2 split is collapsed into a single shippable Operator View MVP. The legacy "M2 acceptance" criteria are folded into M1 acceptance above.*

### Milestone 3 — Optional LLM client + Tasks curation + Desktop notifications (v0.4)
*Goal: enable the optional LLM path; ship the Tasks "most significant findings" curation; ship opt-in desktop notifications wiring on top of the Operator View.*

- [ ] `app/llm_client.py` — LiteLLM-backed provider-agnostic wrapper
- [ ] Optional install via `uv sync --extra llm`
- [ ] `LENS_LLM_*` env wiring; gated UI hidden when disabled
- [ ] Complexity slider, session-scoped, injected into all LLM prompts (initially used by Tasks curation; reusable for later milestones)
- [ ] "Most significant findings" toggle behind `llm.enabled && llm.findings_curation_enabled`
- [ ] `POST /api/tasks/findings/curate` endpoint
- [ ] LLM status surfaced in `/health` and settings view
- [ ] OTEL span `lens.tasks.curate`
- [ ] **Desktop notifications (opt-in)**: "Enable notifications" affordance in Operator View header; Notification API permission flow; grant state in `localStorage`; notifications fire only on Needs-attention transitions (row entering the section), body `<task title> — <reason>`, click → `/tasks?selected=<task_id>`; `[tasks].notifications.desktop_optin` toggle

### Milestone 4 — Feed View + Feedback (v0.5)
*Goal: human-readable feed with feedback mechanism — first Knowledge Browser milestone*

- [ ] Feed view: paper list from `lithos_list`, filterable by profile/date/tag/confidence
- [ ] Expandable abstract via HTMX + `lithos_read`
- [ ] Archive file streaming via `/archive/...`
- [ ] 👍 / 👎 feedback buttons → read-then-write pattern with `lithos_write`
- [ ] `POST /api/feedback` with version-conflict retry
- [ ] Promote `/note/{id}` from the M1 minimal renderer to the full feed-detail panel
- [ ] Settings view content (Influx config display)

### Milestone 5 — Cognitive Search (v0.6)
- [ ] Search bar wired to `lithos_retrieve`
- [ ] Fallback to `lithos_search` on retrieval errors
- [ ] Namespace/profile scope toggle
- [ ] Result cards with scout chips and reasons
- [ ] Tag cloud from `lithos_tags`

### Milestone 6 — Graph View (v0.7)
- [ ] Cytoscape.js graph
- [ ] Nodes sized by `lithos_node_stats.salience`, coloured by profile
- [ ] Edges from `lithos_edge_list` — typed and colour-coded
- [ ] Click node → side panel with `lithos_related`
- [ ] Filter panel (profile, date, tag, confidence / parsed profile score, edge type)
- [ ] Safety cap via `ui.graph_max_nodes`

### Milestone 7 — Conflict Resolution (v0.8)
- [ ] "Needs attention" banner for unresolved `contradicts` edges
- [ ] Node-detail resolution panel calling `lithos_conflict_resolve`
- [ ] Winner picker for `superseded` resolution
- [ ] Resolution badges in graph

### Milestone 8 — Reading Paths & Centrality (v0.9)
- [ ] Bidirectional node ↔ side-panel selection
- [ ] Centrality overlay toggle in graph filter panel
- [ ] `POST /api/path` with `salience`, `chronological`, `edge-traversal` modes
- [ ] Reading-path picker UI in feed and graph views
- [ ] Persisted path notes under `path="lens/paths"` with structured path metadata in the note body
- [ ] `GET /path/{slug}`

### Milestone 9 — LLM Knowledge-Browser features (v1.0)
*Goal: extend the M3 LLM client to the Knowledge Browser*

- [ ] Answer synthesis: routed through `lithos_synthesize` when present, else local LLM
- [ ] Multi-note comparison "Themes & Concepts" tab
- [ ] LLM-curated reading-path mode

### Milestone 10 — Semantic Projection (deferred)
> Blocked on Lithos exposing a `lithos_embeddings` MCP tool.

- [ ] `lithos_embeddings` MCP tool available in Lithos
- [ ] 2D UMAP / t-SNE projection of nodes
- [ ] Toggle between force-directed and semantic-projection layouts
- [ ] Cluster overlay derived from projection density

### Milestone 11 — Knowledge → Tasks back-link (deferred)
*Depends on producers passing `source_task` so Lithos stores the producing task ID in note metadata.*

- [ ] "Produced by task X" badge in feed and graph node detail
- [ ] Click-through to `/tasks/{task_id}`

---

## Appendix A — Directory Structure

```
lithos-lens/
├── Dockerfile
├── docker-compose.yml
├── .env.dev
├── .env.prod
├── pyproject.toml
├── README.md
├── run.sh
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── lithos_client.py
│   ├── events.py                 # shared SSE subscription + pub/sub
│   ├── llm_client.py             # optional, gated by LENS_LLM_ENABLED
│   ├── telemetry.py
│   ├── routers/
│   │   ├── tasks.py              # tasks dashboard, list, detail, findings
│   │   ├── tasks_events.py       # SSE re-broadcast endpoint
│   │   ├── feed.py
│   │   ├── graph.py
│   │   ├── search.py
│   │   ├── feedback.py
│   │   ├── conflict.py
│   │   ├── compare.py
│   │   ├── path.py
│   │   └── settings.py
│   └── templates/
│       ├── base.html
│       ├── tasks/
│       │   ├── dashboard.html
│       │   ├── list.html         # HTMX fragment
│       │   ├── row.html          # HTMX fragment
│       │   ├── summary.html      # HTMX fragment
│       │   ├── detail.html
│       │   └── findings.html
│       ├── feed.html
│       ├── graph.html
│       ├── search.html
│       ├── compare.html
│       ├── path.html
│       └── settings.html
└── static/
    ├── cytoscape.min.js
    ├── htmx.min.js
    ├── htmx-sse.js
    ├── vendor.css
    └── lens.css
```

---

## Appendix B — Key Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework |
| `uvicorn` | ASGI server |
| `httpx` | Lithos MCP transport (SSE for tools); also LLM HTTP client |
| `httpx-sse` (or equivalent) | SSE consumption for Lithos's event stream |
| `jinja2` | HTML templating |
| `pydantic` | Request/response validation |
| `python-json-logger` | Structured JSON logging |
| `opentelemetry-*` | OTEL (optional extra: `uv sync --extra otel`) |
| `litellm` | Provider-agnostic LLM calls (optional extra: `uv sync --extra llm`) |
| Cytoscape.js (vendored static asset) | Graph visualisation; client-side centrality via `cy.elements().bc()` |
| HTMX + SSE extension (vendored static assets) | Dynamic HTML; SSE extension drives live tile and row updates |
| App CSS / optional precompiled utility CSS (vendored static asset) | Styling without a frontend build step or CDN dependency |

> [!note] LLM provider neutrality
> The LLM client is wrapped in `app/llm_client.py` so the rest of the app calls a small `synthesize()` / `compare_themes()` / `order_path()` / `curate_findings()` interface. LiteLLM handles provider-specific API differences; swapping providers is an env/config change, not a code change.

> [!note] Frontend asset recommendation
> For a local-first operational tool, production builds should serve pinned, vendored JS/CSS from `static/` rather than CDNs. This keeps the app usable offline, avoids runtime dependency on third-party CDNs, and gives reproducible upgrades. Tailwind's CDN mode is useful for prototypes but is not recommended as the default production path; use explicit app CSS or a checked-in precompiled CSS bundle instead.
