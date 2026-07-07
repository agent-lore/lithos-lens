---
title: Lithos Lens — Requirements Document
version: 0.8.0
date: 2026-07-07
status: draft
tags: [lithos-lens, requirements, design, architecture]
---

# Lithos Lens — Requirements Document

> [!abstract] Project Summary
> **Lithos Lens** is a local web UI for observing and steering the Lithos coordination layer and for browsing and curating a Lithos knowledge base. It hosts two first-class views inside a single FastAPI app:
> - **Tasks View** — a **graph-native** dashboard over the Lithos task graph (typed edges, epics, gates, computed ready/blocked frontiers), with a small set of **curated write actions** (approve gates, reopen, cancel, create, add dependencies) gated behind an explicit config flag.
> - **Knowledge Browser** — real note rendering with wiki-links, search, related/back-link panels, an interactive typed-edge graph (Cytoscape.js), cognitive search backed by `lithos_retrieve`, feedback via frontmatter patches, and conflict resolution.
>
> Lens is a pure Lithos MCP client by default — zero runtime dependency on the Influx ingestion container; all data is sourced from Lithos. Lens may optionally call an LLM directly **when explicitly enabled** (`LITHOS_LENS_LLM_ENABLED=true`) for findings curation and synthesis; with the flag off Lens remains a pure MCP client.
>
> This document holds **durable product requirements only**. Milestone sequencing, status, and the upstream-Lithos dependency ledger live in [`docs/ROADMAP.md`](./ROADMAP.md); shipped behaviour is described by [`docs/SPECIFICATION.md`](./SPECIFICATION.md); execution detail for in-flight milestones lives in the just-in-time PRDs under [`docs/prd/`](./prd/).

> [!note] v0.8 changelog
> v0.8 rewrites the Tasks surface around **Lithos 0.4.0's task graph**: typed task edges (`blocks`, `parent_child`, `discovered_from`, `waits_on_gate`), task types (`task`/`epic`/`gate` — gates: human/timer/ci/pr/external_task), computed ready/blocked frontiers with classified blockers, spawn/reopen lifecycle, `lithos_task_get`, `resolved_since` filtering, and the `task.updated`/`task.reopened` events. The Operator View is restructured (Epic strip → Needs attention → Gates → In progress → Ready → Blocked), the attention model is rebuilt graph-aware (the old `expired-claim` rule was unobservable and is removed), the Planning View is rebased on the frontier, and a new **Curated Write Actions** contract (§5C) replaces every "strictly read-only" statement. Part C is reordered around a real Note View (server-rendered markdown, wiki-links, related panel) followed by search, graph, and later surfaces. The legacy `claimed_state` filter and `visible_cap` claim fan-out are deprecated (§4.4). The old §17 implementation plan is deleted — sequencing now lives exclusively in `docs/ROADMAP.md`. Scale posture is updated to observed production reality: **hundreds of open tasks, thousands of notes**.

> [!note] Earlier changelogs (condensed)
> v0.5 incorporated Paperlens-review ideas (synthesis, comparison, reading paths, centrality overlay). v0.6 promoted the document into four parts and added the Tasks View as a peer of the Knowledge Browser. v0.7 split the Tasks surface into Operator (`/tasks`) and Planning (`/tasks/plan`) routes, made project tagging normative, and added the recent-findings drawer, agent role chips, and title-badge notifications — all of which survive in this version.

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
- [[#5C. Curated Write Actions]]

### Part C — Knowledge Browser
- [[#6. Note View]]
- [[#7. Knowledge Search]]
- [[#8. Knowledge Graph View]]
- [[#9. Feed View]]
- [[#10. Feedback Mechanism]]
- [[#11. Conflict Resolution UI]]
- [[#12. Deferred Surfaces — Comparison & Reading Paths]]

### Part D — Reference
- [[#13. Settings View]]
- [[#14. Resilience & Error Handling]]
- [[#15. Observability]]
- [[#16. API Reference]]

---

# Part A — Common Core

The common-core sections describe behaviour, infrastructure, and configuration shared by every view (Tasks View, Knowledge Browser, and any future view). View-specific behaviour lives in Parts B and C.

---

## 1. Goals & Non-Goals

### Goals

#### Common
- Provide a low-latency local browser UI over the Lithos coordination layer and a Lithos knowledge base
- Two first-class roles — a **task dashboard** (observe the task graph, act on it through a curated write set) and a **knowledge browser** — sharing one FastAPI app, one MCP client, and one `base.html` shell with a top-nav view switcher
- Operate purely as a Lithos MCP client when `LITHOS_LENS_LLM_ENABLED=false` — no dependency on the Influx runtime
- Subscribe to the Lithos SSE event stream once (a shared events module) and let any view consume the events it cares about
- **Curated writes, not CRUD**: Lens exposes a small, deliberate set of operator actions (§5C) behind `[writes] enabled` (default off). With writes disabled, Lens is strictly read-only and registers no mutating routes.
- Scale posture: the production deployment Lens observes today runs **~330 open tasks (311 `task`, 21 `epic`) across ~20 projects and ~2,900 knowledge notes**. Lens MUST be designed for *hundreds of open tasks and thousands of notes* — not tens — and SHOULD remain usable into the low thousands of open tasks before requiring upstream bulk-fetch support (see the ROADMAP dependency ledger).
- Optional LLM-backed features ("most significant findings" curation, answer synthesis, complexity slider) behind a single config flag, gracefully degrading when disabled (sequencing: ROADMAP X1)
- Minimal stack: FastAPI + HTMX + Cytoscape.js + markdown-it-py; no heavy JS framework, no build step. Every graph surface has a **no-JS text baseline**; Cytoscape is progressive enhancement.

#### Tasks View
- Two co-equal routes sharing the same data, MCP client, and SSE subscription:
  - **Operator View (`/tasks`)** — "what can actually happen next, and what needs me?" Live dashboard structured by the computed ready/blocked frontier. Primary surface.
  - **Planning View (`/tasks/plan`)** — "what should happen next?" Human-actionable queue (gates first), starvation/bottleneck/stalled signals, throughput.
- Section structure is derived from **Lithos-computed graph state** (`lithos_task_ready` / `lithos_task_blocked`), never from Lens-side inference. Lens MUST NOT re-implement the readiness predicate.
- Gates — the part of the graph that is explicitly the operator's job — get a first-class section, with human gates surfaced above all other gate types.
- Epics roll up (progress chips) instead of polluting open-task counts.
- A dependency graph page (`/tasks/graph`) and a task-detail mini-graph make `blocks` chains, gates, and hierarchy visually legible.
- Curated write actions (§5C): approve/complete human gates, reopen, cancel with consequence preview, create task/epic/gate, add dependency edges — attributed to a named human operator identity distinct from the Lens service agent.
- Auto-update via the shared SSE event subscription, with `Last-Event-ID` replay on reconnect and a polling fallback.
- Findings link to the Knowledge Browser via explicit `finding.knowledge_id` (no inference, no heuristics).

#### Knowledge Browser
- A real **Note View**: server-rendered markdown (safe by default), clickable wiki-links via a Lens-side resolver, metadata chips, related/back-links panel, "produced by task" chip
- Search at `/knowledge`: `lithos_search` first, evolving to `lithos_retrieve` as the default engine with graceful fallback
- Interactive graph view with Cytoscape.js rendering typed LCMA edges, wiki-links, and provenance — ego-graph (focus) mode first
- Feedback controls that patch note frontmatter via `lithos_note_update` (never round-tripping the note body)
- Conflict resolution UI for LCMA `contradicts` edges
- Feed, note comparison, and reading paths are preserved as requirements but deferred (see ROADMAP)

### Non-Goals

- Editing note content inline (that's Obsidian's job — or direct MCP tools)
- Running its own ingestion — Lens never writes source research notes from scratch. It may write narrow Lens-authored operational data only where explicitly specified; feedback is written as frontmatter patches on existing notes.
- Hosting an external collaboration surface — single-operator, trusted local network only (see §5C.1 for the explicit security boundary)
- **Arbitrary task CRUD or claim management** — Lens's writes are limited to the curated action set in §5C. Lens never claims, renews, or releases task claims on behalf of agents, and never edits task titles/descriptions/tags. Agents doing work talk to the Lithos MCP API directly, not through Lens.
- Running its own task scheduler, cron, or worker — Lens observes and nudges; it does not orchestrate
- Quiz / flashcard generation
- Hosting embeddings or running its own vector index — semantic projections are deferred and depend on Lithos exposing embeddings via a future MCP tool

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                         DOCKER NETWORK                            │
│                                                                   │
│  ┌──────────────┐     ┌──────────────┐     ┌─────────────────┐   │
│  │    LITHOS    │◀────│    INFLUX    │     │  LITHOS-LENS    │   │
│  │              │     │  (optional   │     │   (web UI)      │   │
│  │  knowledge   │     │  ingestion)  │     │                 │   │
│  │  store +     │     │              │     │  stateless      │   │
│  │  task graph  │     │  scheduled   │     │  HTTP server    │   │
│  │  MCP API +   │     │  batch job   │     │                 │   │
│  │  SSE events  │     │              │     │                 │   │
│  └──────────────┘     └──────────────┘     └────────┬────────┘   │
│          ▲       ▲                                   │            │
│          │       └─── SSE event stream ──────────────┤            │
│          └─────────── MCP API (reads + writes) ───── │            │
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
> **Lithos Lens has zero runtime dependency on the Influx ingestion container.** It is a pure Lithos MCP client. All knowledge and coordination data — notes, feedback, graph edges, tasks, claims, and findings — comes from Lithos. Lens MAY optionally mount the `influx-archive` volume read-only to serve archived PDFs/HTMLs and the `influx-config` volume to display Influx settings; **both mounts are optional** and every Lens feature except archive serving and Influx-config display works without them.

> [!note] Optional LLM client
> When `LITHOS_LENS_LLM_ENABLED=true`, Lens additionally talks to an LLM provider (Anthropic / OpenAI / Ollama via LiteLLM) for "most significant findings" curation in the Tasks view and synthesis in the Knowledge Browser. With the flag off, all LLM-dependent UI surfaces are hidden and Lens remains a pure MCP client. When Lithos exposes a synthesis tool (`lithos_synthesize` or equivalent), Lens prefers the MCP path and treats the local LLM as a fallback. Sequencing: ROADMAP X1.

> [!note] Single SSE subscription
> Lens opens **one** SSE connection to Lithos at app start. Each view registers for the event types it cares about; the connection is shared. Browser tabs never connect to Lithos directly — they connect to Lens's own `/tasks/events` re-broadcast endpoint. See §5.8.

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deployment | Single Docker container hosting both views | Independent restartability vs Lithos / Influx; one app, one process, one SSE connection |
| App structure | Single FastAPI app with view-specific routers sharing a `base.html` shell | Lets views share session state, the MCP client, and the event stream without coordination overhead |
| Lithos communication | Lithos MCP API (single shared MCP-over-SSE session for tool calls) + Lithos `GET /events` SSE stream (push) | One MCP transport for request/response, one SSE stream for push events; both supplied by Lithos |
| Task/knowledge partitioning | Section membership and readiness come from Lithos (`task_ready`/`task_blocked`); Lens joins id-sets, never re-derives | Timer gates and NULL-safe gate handling are evaluated inside Lithos at query time; re-deriving readiness from edges in Lens is a correctness trap |
| Graph rendering | Cytoscape.js as progressive enhancement over a text baseline | Handles typed edges and DAG layouts; the no-JS baseline keeps every graph surface usable without scripting |
| Markdown rendering | Server-side `markdown-it-py`, safe by default (§6.2) | No client-side rendering of untrusted note content; no sanitizer dependency needed when raw HTML is never emitted |
| Writes | Curated action set, route-gated by `[writes] enabled`, form-encoded POSTs, refresh-after-write | Small blast radius, no optimistic state, degrades to read-only cleanly |
| Frontend | FastAPI + HTMX + Cytoscape.js | No build step; minimal stack; HTMX SSE extension drives live updates |
| Styling/assets | Vendored, pinned static assets (`static/`) with app CSS | Local-first/offline behaviour; no CDN supply-chain or runtime dependency |
| Config format | TOML + env overrides | Consistent with Lithos conventions |
| OTEL | Opt-in, additive, optional packages | Consistent with Lithos conventions |
| LLM access | Optional, env-gated LiteLLM client (`LITHOS_LENS_LLM_*`) | Provider-agnostic across OpenAI, Anthropic, OpenRouter, Ollama; prefers MCP synthesis when Lithos ships it |
| Centrality computation | Client-side in Cytoscape | Lithos exposes edges but no centrality scores; computing in the browser operates on the already-loaded graph |
| SSE event handling | Single shared subscription, fan-out via in-process pub/sub | Avoids N independent SSE connections; scope-aware normalization (§5.8.2) |

### Shared Application Surface

The following concerns are shared by every view and constitute the "common core": the FastAPI app and top-nav shell, the typed TOML+env config loader, the Lithos MCP client (one shared session), the shared events subscription and in-process hub, the optional LiteLLM wrapper, OTEL setup, and the base template. The authoritative module layout is documented in [`docs/SPECIFICATION.md`](./SPECIFICATION.md) and enforced by `docs/architecture.toml` guardrail tests; this document does not duplicate it.

---

## 3. Infrastructure & Deployment

### Container

| Container | Base image | Purpose |
|-----------|-----------|--------|
| `lithos-lens` | `python:3.12-slim` | Web UI hosting both Tasks View and Knowledge Browser |

### Volumes

| Volume | Lens mount | Purpose |
|--------|------------|---------|
| config/data | `/data` | Holds `lithos-lens.toml` (`LITHOS_LENS_CONFIG=/data/lithos-lens.toml`) and the Lens data dir (`LITHOS_LENS_DATA_DIR=/data`). This is the mount the shipped `docker/docker-compose.yml` uses. |
| `influx-archive` *(optional, future)* | `/archive` (ro) | Serve archived PDFs/HTMLs inline (Knowledge Browser). When absent, archive links are hidden. |
| `influx-config` *(optional, future)* | `/etc/influx` (ro) | Display the Influx TOML config in the settings view. When absent, that settings section is hidden. |

### Environment Files

Every TOML knob in §4 has a `LITHOS_LENS_*` environment override following the shipped naming convention (`[tasks].frontier_limit` → `LITHOS_LENS_TASKS_FRONTIER_LIMIT`; verify names against `config.py`, which is authoritative). Overrides for knobs whose milestone has not shipped yet (all `[tasks]` graph knobs, `[graph]`, `[writes]`, `[knowledge]`) land with that milestone's code — the convention below is the contract, not a promise that every name is wired today. The env files list the operationally interesting subset:

**`.env.dev`:**
```env
LITHOS_LENS_ENVIRONMENT=dev
LITHOS_LENS_HOST_PORT=7843
LITHOS_LENS_CONTAINER_NAME=lithos-lens
LITHOS_LENS_OTEL_ENABLED=false
OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318

# Lithos transport
LITHOS_LENS_LITHOS_URL=http://host.docker.internal:8765
LITHOS_LENS_SSE_EVENTS_PATH=/events            # default; SSE event stream endpoint
LITHOS_LENS_AGENT_ID=lithos-lens

# Tasks view — graph-native dashboard
LITHOS_LENS_TASKS_AUTO_REFRESH_INTERVAL_S=30     # polling fallback when SSE disconnects
LITHOS_LENS_TASKS_FRONTIER_LIMIT=500             # limit for task_ready / task_blocked
LITHOS_LENS_TASKS_DEFAULT_TIME_RANGE_DAYS=30     # resolved_since window for Completed/Cancelled
LITHOS_LENS_TASKS_GATE_WAITING_ATTENTION_HOURS=24
LITHOS_LENS_TASKS_CLAIM_EXPIRING_SOON_MINUTES=10
LITHOS_LENS_TASKS_UNCLAIMED_READY_AGE_MINUTES=60
LITHOS_LENS_TASKS_STALE_OPEN_AGE_DAYS=7
LITHOS_LENS_TASKS_PROJECT_CONVENTION=both        # metadata | tag | both
LITHOS_LENS_TASKS_METRICS_DEBOUNCE_MS=2000
LITHOS_LENS_TASKS_RECENT_FINDINGS_DRAWER_SIZE=50
# LITHOS_LENS_TASKS_HUMAN_AGENTS=dave,human      # comma-separated agent IDs that represent humans

# Task graph pages
LITHOS_LENS_GRAPH_CACHE_TTL_S=30
LITHOS_LENS_GRAPH_MAX_TASKS=300
LITHOS_LENS_GRAPH_FETCH_CONCURRENCY=16

# Curated writes — disabled by default; POST routes are not registered when false
LITHOS_LENS_WRITES_ENABLED=false
# LITHOS_LENS_WRITES_DEFAULT_OPERATOR=dave
LITHOS_LENS_WRITES_CONFIRM_CANCEL=true

# Knowledge browser
LITHOS_LENS_KNOWLEDGE_SEARCH_LIMIT=20
LITHOS_LENS_KNOWLEDGE_RECENT_LIMIT=20
LITHOS_LENS_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP=30

# Optional LLM client — disabled by default
LITHOS_LENS_LLM_ENABLED=false
# LITHOS_LENS_LLM_PROVIDER=anthropic             # LiteLLM provider prefix
# LITHOS_LENS_LLM_MODEL=anthropic/claude-haiku-4-5-20251001
# LITHOS_LENS_LLM_API_KEY=
# LITHOS_LENS_LLM_BASE_URL=
# LITHOS_LENS_LLM_EXTRA_HEADERS_JSON=
# LITHOS_LENS_LLM_MAX_TOKENS=2048
```

**`.env.prod`:** same keys with production values (`LITHOS_LENS_LITHOS_URL=http://lithos:8765`, `LITHOS_LENS_OTEL_ENABLED=true`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`). Deployments that enable writes set `LITHOS_LENS_WRITES_ENABLED=true` explicitly and deliberately.

### `docker-compose.yml`

The authoritative Compose file is the checked-in `docker/docker-compose.yml`
(built from `docker/Dockerfile`); this section states the requirements it must
satisfy rather than duplicating it. The shipped shape:

```yaml
# docker/docker-compose.yml — the checked-in file is authoritative
services:
  lithos-lens:
    image: ${LITHOS_LENS_IMAGE:-lithos-lens:dev}
    pull_policy: never
    build:
      context: ..
      dockerfile: docker/Dockerfile
    container_name: ${LITHOS_LENS_CONTAINER_NAME:-lithos-lens}
    user: "${LITHOS_LENS_UID:-1000}:${LITHOS_LENS_GID:-1000}"
    ports:
      - "${LITHOS_LENS_HOST_PORT:-8000}:8000"
    extra_hosts:
      - "host.docker.internal:host-gateway"   # reach a host-run Lithos in dev
    volumes:
      - ${LITHOS_LENS_DATA_PATH:-./data}:/data
      # optional, future — Knowledge Browser archive/config serving:
      # - ${INFLUX_ARCHIVE_PATH:-./archive}:/archive:ro
      # - ${INFLUX_CONFIG_PATH:-./config}:/etc/influx:ro
    environment:
      - LITHOS_LENS_CONFIG=/data/lithos-lens.toml
      - LITHOS_LENS_DATA_DIR=/data
      - LITHOS_LENS_ENVIRONMENT=${LITHOS_LENS_ENVIRONMENT:-dev}
```

Requirements: a single service on `python:3.12-slim`; a `/health` container
health check; reachability of Lithos over the network (dev via
`host.docker.internal`); config supplied through `LITHOS_LENS_*` env plus the
`/data`-mounted TOML; optional read-only archive/config mounts for the
Knowledge Browser (future). Per-environment values come from
`docker/.env.<env>` (see `docker/.env.example`), applied by `run.sh`.

### `run.sh`

`docker/run.sh <env> <cmd>` wraps Compose with project name `lithos-lens-<env>`
and applies `docker/.env.<env>`. Supports `up | down | restart | logs | status`.

---

## 4. Configuration

Lens has minimal configuration of its own. The main runtime config comes from environment variables layered over a TOML file (see `lithos-lens.example.toml` for the shipped shape; tables are namespaced `[lithos-lens.*]`). This section is the **config reference**; prose elsewhere refers to knobs in `[section].key` shorthand.

```toml
[lithos-lens.ui]
default_view = "tasks"            # tasks | knowledge — landing view

[lithos-lens.lithos]
url = "http://localhost:8765"
mcp_sse_path = "/sse"
sse_events_path = "/events"
agent_id = "lithos-lens"          # the Lens SERVICE agent; operator identity is separate (§5C.5)

[lithos-lens.tasks]
auto_refresh_interval_s = 30      # polling fallback when SSE disconnects
default_time_range_days = 30      # resolved_since window for Completed/Cancelled sections
frontier_limit = 500              # limit passed to task_ready / task_blocked
gate_waiting_attention_hours = 24 # Needs-attention rule 3: human gate waiting
claim_expiring_soon_minutes = 10  # Needs-attention rule 4: claim expiring soon
unclaimed_ready_age_minutes = 60  # Needs-attention rule 6: ready-but-unclaimed
stale_open_age_days = 7           # Needs-attention rule 5: stale open
project_convention = "both"       # metadata | tag | both — see §5B.1
project_tag_key = "project"       # tag-key reserved for the tag convention
metrics_debounce_ms = 2000        # server-side debounce for metric recompute on SSE bursts
recent_findings_drawer_size = 50  # rolling buffer size for drawer + latest-finding line
recent_findings_warmup_window_h = 48  # boot-time finding.posted backfill window
stalled_no_findings_hours = 24    # Planning View stalled rule
bottleneck_min_inflight = 3       # Planning View agent-overload rule
bottleneck_concentration = 0.7    # one-agent claim share threshold
throughput_window_days = 30       # Planning View throughput resolved_since window
human_actionable_tag = "human"    # tag identifying tasks needing a human
human_agents = []                 # agent IDs that represent humans, e.g. ["dave"]

[lithos-lens.tasks.notifications]
title_badge = true                # "(N) Lithos Lens" for unseen Needs-attention items
desktop_optin = true              # show "Enable notifications" affordance (wiring: ROADMAP X1)

[lithos-lens.graph]                # task dependency graph pages (§5.7)
cache_ttl_s = 30                  # in-process snapshot cache TTL; event-invalidated
max_tasks = 300                   # refuse to render larger scopes; ask to narrow
fetch_concurrency = 16            # semaphore for the per-task edge_list fan-out

[lithos-lens.writes]               # curated write actions (§5C)
enabled = false                   # POST routes are NOT REGISTERED when false
default_operator = ""             # operator identity fallback (§5C.5)
confirm_cancel = true             # consequence-aware cancel confirmation

[lithos-lens.knowledge]            # knowledge browser (Part C)
search_limit = 20                 # lithos_search / lithos_retrieve result limit
recent_limit = 20                 # recently-updated list size on /knowledge
related_title_fanout_cap = 20     # cap on cached lithos_read title lookups per related panel
graph_focus_max_nodes = 250       # knowledge graph cap, focus (ego) mode
graph_global_max_nodes = 500      # knowledge graph cap, global mode

[lithos-lens.events]
enabled = true
reconnect_backoff_ms = [500, 1000, 2000, 5000, 10000]

[lithos-lens.llm]
enabled = false                   # overridden by LITHOS_LENS_LLM_ENABLED
provider = "anthropic"            # LiteLLM provider prefix
model = "anthropic/claude-haiku-4-5-20251001"
default_complexity = 3            # 1=beginner … 5=expert; per-session override allowed
max_tokens = 2048
base_url = ""
extra_headers_json = ""
synthesis_prefer_mcp = true       # use lithos_synthesize when available, else local LLM
findings_curation_enabled = true  # enables "most significant findings" (ROADMAP X1)

[lithos-lens.telemetry]
enabled = false                   # overridden by LITHOS_LENS_OTEL_ENABLED
console_fallback = false
service_name = "lithos-lens"
export_interval_ms = 30000
```

### 4.1 Common Core Startup Contract

At process startup Lens performs the following steps in order:

1. Load TOML config and environment overrides into a typed config object.
2. Configure structured stdout logging.
3. Configure OTEL only if `LITHOS_LENS_OTEL_ENABLED=true`; missing optional OTEL packages must not prevent boot when telemetry is disabled.
4. Create the Lithos MCP client (one shared MCP-over-SSE session reused across all tool calls).
5. Attempt startup auto-registration of the **service agent**:

```python
lithos_agent_register(
    id=config.lithos.agent_id,     # "lithos-lens"
    name="Lithos Lens",
    type="web-ui",
)
```

6. Start the shared Lithos `/events` subscriber if event streaming is enabled, passing a server-side `types=` filter for the consumed event set (§16.1.1).
7. Start cached health probes for Lithos, events, and LLM.
8. Perform **graph feature detection**: if `lithos_task_ready` is missing from the connected Lithos (tool-not-found), set `graph_available=false` for the process — graph-native sections degrade to the flat list per §14.
9. Mount routers and serve HTTP. Write routes (§5C) are mounted **only** when `[writes] enabled = true`.

Boot must succeed even when Lithos is unreachable. In that case Lens starts in degraded mode, `/health` reports `lithos="unreachable"`, and UI routes render degraded panels rather than crashing.

### 4.2 LiteLLM Configuration Contract

When `llm.enabled = false`, Lens must not import or initialize LiteLLM eagerly.

When `llm.enabled = true`, Lens validates configuration shape at startup but does not require a paid completion call to pass readiness. Per-feature LLM failures are surfaced as non-blocking UI errors.

| Config | Env | Required when enabled | Notes |
|--------|-----|-----------------------|-------|
| `llm.model` | `LITHOS_LENS_LLM_MODEL` | Yes | LiteLLM model string, e.g. `openai/gpt-4.1-mini`, `anthropic/claude-...`, `ollama/...` |
| `llm.provider` | `LITHOS_LENS_LLM_PROVIDER` | No | Metadata; the model string is authoritative |
| `llm.api_key` | `LITHOS_LENS_LLM_API_KEY` | Provider-dependent | Not required for local Ollama |
| `llm.base_url` | `LITHOS_LENS_LLM_BASE_URL` | No | LiteLLM `api_base` for OpenRouter/local gateways |
| `llm.extra_headers_json` | `LITHOS_LENS_LLM_EXTRA_HEADERS_JSON` | No | JSON object for provider-specific headers |
| `llm.max_tokens` | `LITHOS_LENS_LLM_MAX_TOKENS` | No | Default 2048 |

### 4.3 Static Asset Policy

Production Lens serves frontend dependencies from pinned vendored files under `static/vendor/`; it does not depend on public CDNs at runtime.

Required policy:
- Vendor HTMX, the HTMX SSE extension, Cytoscape.js, and any precompiled CSS bundle into `static/vendor/`
- Record asset names, versions, source URLs, and checksums in `docs/vendor-assets.md`
- Keep `lens.css` app-owned and small
- Do not use Tailwind CDN in production; if utility CSS is desired, check in a precompiled CSS file
- Development may temporarily use CDN assets during prototyping, but committed default templates should reference vendored assets

### 4.4 Deprecated Configuration

The following pre-graph knobs are **deprecated**. For one release Lens MUST parse and ignore them (with a deprecation log line at startup); after that they are removed from the config schema:

| Deprecated | Replacement |
|------------|-------------|
| `[tasks].visible_cap` | Nothing — the per-row claim fan-out it capped is gone. Claims arrive inline via `with_claims=true`; there is no "Unknown claim state" tail. |
| `?claimed_state=` URL parameter (and its `[tasks].default_status_groups` interaction) | Nothing — section membership is structural (§5.3). Legacy URLs containing `claimed_state` are silently ignored so old bookmarks degrade gracefully. |

---

# Part B — Tasks View

The Tasks View is a **graph-native** surface over the Lithos coordination layer — tasks, typed task edges, epics, gates, claims, and findings — split across two co-equal routes that answer different questions:

- **Operator View (§5)** at `/tasks` — "what can actually happen next, and what needs me?" Live dashboard structured by the computed ready/blocked frontier. Primary surface.
- **Planning View (§5A)** at `/tasks/plan` — "what should happen next?" Human-actionable queue, project health, throughput.

Project tracking conventions (§5B) are normative for both. The curated write actions (§5C) attach to both surfaces when enabled. Switching routes via the top-nav resets view-specific filter state — the views answer different questions and shouldn't co-mingle filters.

**Graph features require Lithos ≥ 0.4** (the task-graph release). Against an older Lithos, Lens MUST degrade to a flat open/completed/cancelled list with a "graph features need Lithos ≥ 0.4" notice (§14) rather than erroring.

---

## 5. Tasks View — Operator View

### 5.1 Purpose & Scope

The Operator View is the primary Tasks surface and the default landing route. Its job is to answer **"what can actually happen next, and what needs my attention right now?"** — a glance-able operational dashboard whose structure *is* the task graph.

It surfaces, in priority order:
- **Structural failures and escalations** — unsatisfiable blockers, dependency cycles, long-waiting human gates, claims about to expire, stale or unpicked ready work
- **Gates** — the external waits, human ones first, because resolving them is the operator's job
- **What's actively in flight**, what's **ready to pick up**, and what's **blocked and why**
- Recent completions and cancellations as confirmation, not as primary content

The view consumes (reads):
- `lithos_task_list(status="open", with_claims=true)` — the master open set, including epics and gates, with claims inline
- `lithos_task_ready(limit=frontier_limit, with_claims=true)` — the feasible frontier
- `lithos_task_blocked(limit=frontier_limit)` — blocked tasks with structured blocker reasons (`task` / `gate` / `blocker_unsatisfiable` / `cycle`)
- `lithos_task_list(status="completed"|"cancelled", resolved_since=…)` — recently resolved work
- `lithos_task_children(epic_id, recursive=true, include_closed=true)` — epic rollups
- `lithos_task_get(task_id)` / `lithos_task_status(task_id)` / `lithos_task_edge_list(task_id)` / `lithos_finding_list(task_id)` — detail surfaces
- `lithos_agent_list()`, `lithos_tags(prefix="project:")`, `lithos_stats()` — filter dropdowns and summary signals
- `lithos_read(id, max_length=…)` — resolving `finding.knowledge_id` to note titles
- the Lithos SSE event stream (§5.8)

Constraints the UI must respect (documented as such; the asks live in the ROADMAP dependency ledger):
- **Expired claims are unobservable.** Lithos filters expired claims out of every read at query time, so a claim past expiry vanishes silently from `task_status` and `with_claims` payloads. Lens MUST NOT render "expired claim" states or infer that a claim was released; the observable substitute is the *claim expiring soon* attention rule (§5.2.2).
- **Timer-gate resolution emits no event.** A timer gate becomes satisfied by query-time evaluation of `ready_at`; nothing is pushed. Lens self-schedules a refresh (§5.2.3).
- **`lithos_task_edge_upsert` emits no event** and **no `lithos_task_edge_delete` exists**; there is **no bulk graph fetch**. See §5.7 and §5C for the workarounds.

### 5.2 Operator View Structure

The Operator View renders, top-to-bottom, the following sections. Each is rendered server-side at page load and updated in place via HTMX OOB swaps fed by the SSE pipeline (§5.8).

```
┌─────────────────────────────────────────────────────────────┐
│  Top-nav: [Tasks] [Tasks · Plan] [Knowledge ▾]   (N) Lens   │
│  Filter bar: project | tag | agent | since   ·  live badge  │
├─────────────────────────────────────────────────────────────┤
│  Epic strip:  auth-rework ▓▓▓░ 5/8   loom-arch ▓░░░ 2/9  …  │
├─────────────────────────────────────────────────────────────┤
│  ⚠ Needs attention  (severity-ordered; reason chips)         │
├─────────────────────────────────────────────────────────────┤
│  ⏸ Gates            (human first w/ waiter counts;           │
│                      timer countdowns)                       │
├─────────────────────────────────────────────────────────────┤
│  ▶ In progress      (open workable, ≥1 active claim)         │
├─────────────────────────────────────────────────────────────┤
│  ● Ready            (the frontier, unclaimed — "next up")    │
├─────────────────────────────────────────────────────────────┤
│  ◼ Blocked          (blocker chips per row)                  │
├─────────────────────────────────────────────────────────────┤
│  ▸ Not classified   (frontier-limit overflow tail)           │
├─────────────────────────────────────────────────────────────┤
│  ▸ Completed (12 in last 30 days)         [collapsed]        │
│  ▸ Cancelled (3 in last 30 days)          [collapsed]        │
└─────────────────────────────────────────────────────────────┘
```

**Single-placement rule.** Every open row appears in **exactly one** section. Epics render only in the strip; gates only in the Gates section; a row promoted into Needs attention is removed from whichever workable section it would otherwise occupy; claimed-but-blocked rows render in In progress with a `blocked` decoration (not in Blocked). Section header counts MUST agree with rendered rows.

#### 5.2.1 Epic strip

- One chip per **open epic**, showing title and a done/total progress fraction computed from `lithos_task_children(epic_id, recursive=true, include_closed=true)` — completed descendants over all descendants.
- Clicking an epic chip scopes the entire dashboard to that epic's descendant set (URL: `?epic=<id>`), composing with the other filters.
- Epics never appear in the workable sections or their counts (Lithos excludes them from both frontiers).
- The epic count is expected to stay small (tens); the per-epic children calls are gathered concurrently.

#### 5.2.2 Needs attention — severity model v2

A **severity-ordered single list** of open rows that trigger any of the following rules, evaluated over the joined dashboard snapshot. Within each severity tier, rows sort oldest-first (most persistent problem first).

| # | Rule | Meaning | Source | Knob (default) |
|---|------|---------|--------|----------------|
| 1 | **Unsatisfiable blocker** | A predecessor or gate was **cancelled** — this task can never become ready without intervention | `task_blocked` blockers, `kind="blocker_unsatisfiable"` | — (intrinsic) |
| 2 | **Dependency cycle** | The blocking chain forms a cycle; the blocker `message` names the members | `task_blocked` blockers, `kind="cycle"` | — (intrinsic) |
| 3 | **Human gate waiting** | An open `gate_type="human"` gate has waited longer than the threshold | gate rows + `created_at` | `gate_waiting_attention_hours` (24) |
| 4 | **Claim expiring soon** | An active claim's `expires_at − now` is below the threshold — likely-abandoned work, surfaced *before* the claim silently vanishes | inline claims | `claim_expiring_soon_minutes` (10) |
| 5 | **Stale open** | A workable open task older than the threshold | master list `created_at` | `stale_open_age_days` (7) |
| 6 | **Ready but unclaimed** | A task on the ready frontier, with zero claims, older than the threshold — the fleet is not picking up available work | ready join | `unclaimed_ready_age_minutes` (60) |

Deliberate changes from the pre-graph model, both forced by observed Lithos semantics:
- The old **`expired-claim` rule is removed** — it can never fire (expired claims are unobservable; see §5.1). Rule 4 is the observable replacement. A Lens-side claim ledger was considered and rejected: it dies on Lens restart and lies after Lithos restarts.
- The old **`unclaimed-old` rule becomes ready-aware** (rule 6). A **blocked** task being unclaimed is *correct behaviour*, not a warning — flagging it was a structural false positive.

Chrome requirements (carried over):
- Each row carries one or more **reason chips** naming the rule(s) fired (e.g. `unsatisfiable`, `cycle`, `gate-waiting`, `claim-expiring`, `stale-open`, `ready-unclaimed`). Chips use semantic colour plus text, never colour alone.
- Rows triggering any rule appear **only** here (single-placement rule).
- When the section is empty, render a thin `All systems healthy — 0 issues` stripe (kept visible for reassurance; do not hide entirely by default).
- A header toggle lets the operator hide the section for routine review; persisted via cookie + URL param.
- The task detail surface for a flagged row includes a **"Why this task is here"** block: reason chips with one-line supporting facts (e.g. `Unsatisfiable — blocker "Design schema" was cancelled`, `Claim expiring — agent-zero · ble-recover · 6m remaining`).

#### 5.2.3 Gates

All open `task_type="gate"` tasks, grouped by gate type with **human gates first**, oldest first within each group.

| Element | Requirement |
|---------|-------------|
| Gate type badge | `human` / `timer` / `ci` / `pr` / `external_task` |
| Waiter count | "blocks N tasks" — from outgoing `waits_on_gate` edges (or the blocked-set blocker entries); clicking expands the waiter list |
| Timer countdown | For `timer` gates, a live countdown to `ready_at` |
| Advisory metadata | Type-specific keys (`approval_required_from`, `provider`, `repo`, `pr_number`, `external_id`, `required_state`, …) summarised on the row, full table on the detail page. These are advisory — Lithos does not read them, and Lens renders them verbatim. |
| Approve action *(writes enabled)* | Human gates carry the approve/complete action from §5C.2 |

**Timer self-refresh requirement.** Timer-gate resolution is evaluated at query time and emits **no event**. The dashboard MUST embed `min(ready_at)` over visible open timer gates and self-schedule a one-shot refresh at that instant, so timer expiry moves waiters from Blocked to Ready without manual reload.

A **cancelled** gate is unsatisfiable — its waiters surface under Needs-attention rule 1. "Proceed anyway" is expressed by *completing* the gate (§5C), never by cancelling it.

#### 5.2.4 In progress / Ready / Blocked

- **In progress** — open workable tasks with ≥1 active claim (claims come inline from `with_claims=true`; there is no per-row fan-out). A claimed task that is *also* blocked renders here with a `blocked` decoration — an agent holding a claim on infeasible work is an anomaly that should be legible.
- **Ready** — the frontier: open workable tasks returned by `lithos_task_ready` with zero claims. This is the "next up" queue.
- **Blocked** — open workable tasks returned by `lithos_task_blocked` (excluding rows promoted to Needs attention). Each row carries **blocker chips**: the blocking task's title (with live status) or the gate's name and type, one chip per immediate blocker, sourced from the structured `blockers` array.

#### 5.2.5 Not-classified tail

Open workable tasks present in the master list but absent from both frontier responses. This is only possible when `frontier_limit` truncates `task_ready`/`task_blocked` results. The tail renders with an accuracy banner: `Frontier truncated at <frontier_limit> — these rows could not be classified. Raise [tasks].frontier_limit or narrow your filters.` Lens MUST NOT silently classify these rows. With the default `frontier_limit=500` against the current production frontier (~310 workable open tasks) truncation should be rare.

#### 5.2.6 Completed / Cancelled (collapsed by default)

Both groups render as collapsible section headers with counts. Rows are windowed by **`resolved_since`** (`resolved_at >= now − tasks.default_time_range_days`) — *not* by `created_at` — so a task created months ago and finished yesterday shows up as recent work. Click expands; expansion state persists via cookie + URL param. SSE `task.completed` / `task.cancelled` events animate visible rows into these sections and update header counts even while collapsed. Reopened tasks carry a `reopened` marker (§5.5) and move back out live on `task.reopened`.

### 5.3 Data Contract

On `GET /tasks`, Lens issues **five parallel fetch groups** (gathered concurrently on the shared MCP session):

| # | Data | Call |
|---|------|------|
| 1 | Master open set — every open task incl. epics/gates, claims inline | `lithos_task_list(status="open", with_claims=true)` |
| 2 | Ready partition | `lithos_task_ready(limit=frontier_limit, with_claims=true)` |
| 3 | Blocked partition + structured blockers | `lithos_task_blocked(limit=frontier_limit)` |
| 4 | Recently resolved | `lithos_task_list(status="completed", resolved_since=window)` + `lithos_task_list(status="cancelled", resolved_since=window)` |
| 5 | Agent filter dropdown | `lithos_agent_list()` |

Plus one `lithos_task_children(epic_id, recursive=true, include_closed=true)` per open epic for the strip.

**Partition rules** (the join is pure and testable):
- `lithos_task_ready` and `lithos_task_blocked` return only **workable** (`task_type="task"`) rows and evaluate gate/timer state at query time — so the master open set partitions cleanly: epics → strip; gates → Gates section; workable tasks → exactly one of In progress (≥1 claim), Ready (in ready set), Blocked (in blocked set), or Not classified (in neither — truncation only).
- `task_blocked` does not return claims; the master list supplies them (that is what makes the claimed-but-blocked decoration possible).
- **Lens MUST NEVER re-implement the readiness predicate.** Timer-gate evaluation and the NULL-safe gate handling live inside Lithos; readiness re-derived from edges in Lens will be wrong at the worst moments.

Filtering (project / tag / agent / epic scope) applies **client-side in Lens over the joined snapshot** rather than being pushed upstream: one fetch serves all projections, and no single upstream call can express the metadata-OR-tag project match (§5B.1). This is cheap at hundreds of open tasks; beyond low thousands the remedy is the upstream bulk-fetch ask (ROADMAP ledger), not Lens-side caching heroics.

### 5.4 Row Anatomy and Filters

#### 5.4.1 Row anatomy

Every list row renders a compact, scannable line (specific layout left to the implementer; the data-shape contract is fixed):

| Element | Notes |
|---------|-------|
| **Project chip** | The task's project per §5B.1, rendered as a dedicated leftmost chip. Background colour = stable hash of slug. Rows without a project render `(no project)`. Conflicting conventions emit a telemetry warning (§5B.1). |
| **Type badge** | `epic` / `gate:<gate_type>` badges where applicable (workable tasks carry no badge) |
| **Title** | Truncated to one line; full title in tooltip |
| **Status / section decorations** | `blocked` decoration on claimed-but-blocked rows; `reopened` marker; blocker chips on Blocked rows; reason chips in Needs attention |
| **Latest finding line** *(open rows)* | One line: `<agent> — <summary>` plus relative timestamp, from the server-side rolling buffer (§5.8.4). Updates on `finding.posted`. |
| **Agent chips (collapsed by role)** | Single chip per agent on the row, with role markers `created` / `claimed` / `latest`. Agents listed in `[tasks].human_agents` render with a person-icon prefix and distinct background. Clicking an agent chip filters across all roles. |
| **Active claims** | Compact `aspect → agent` list with time-to-expiry, from inline claims |
| **Tags** | Chips for tags other than the reserved project/human keys |
| **Created at** | Relative time; absolute on hover |

> [!note] Ergonomics slotting
> The latest-finding line, recent-findings drawer, agent role chips, side panel, title-badge notifications, and debounced metric recompute are requirements of this document; their delivery is sequenced by ROADMAP (T2, "operator ergonomics" strand). Requirements here do not imply a delivery order.

#### 5.4.2 Filters

Filters appear in a sticky filter bar. All filters compose and reflect in the URL for shareability.

| Filter | Behaviour |
|--------|-----------|
| **Project** | Multi-select dropdown. A row matches when its project per §5B.1 (metadata **or** tag convention, per `project_convention`) matches. URL: `?project=lithos-loom&project=ganglion`. |
| **Tag** | Free-text with `key:value` parsing; excludes the reserved project and human-actionable keys. URL: `?tag=cli`. |
| **Agent** | Dropdown sourced from `lithos_agent_list`; matches **creator OR claimer** by default (role-narrow toggle: `creator` / `claimer` / `poster` / `any`). URL: `?agent=agent-zero&agent_role=any`. |
| **Since** | Created-at lower bound (`lithos_task_list(since=…)` semantics); open sections ignore it by default. |
| **Epic scope** | `?epic=<id>` — set by clicking an epic chip; scopes all sections to the epic's descendants. |
| **Hide Needs attention** | Toggle; cookie + URL param. |

Removed: the **status filter** (sections express status structurally; `?status=` is accepted only as a section-collapse hint) and the legacy **`claimed_state`** parameter (parsed and ignored — see §4.4). Filters preserve section structure even when scoped — an empty section renders a `no rows match current filters` placeholder rather than disappearing, so filtering never hides a warning silently.

### 5.5 Task Detail: Side Panel + Full-Page Route

Clicking a row opens a **right-side panel** by default (`/tasks?selected=<task_id>`); an **Expand** button navigates to the full-page route (`/tasks/{task_id}`). Both render the same content fragments — single template path, two surfaces. Closing the panel clears the `selected` param and preserves list state.

Data contract: `lithos_task_get(task_id)` + `lithos_task_status(task_id)` (claims) + `lithos_task_edge_list(task_id, direction="both")` + `lithos_finding_list(task_id)`, gathered concurrently. An unknown ID returns the `task_not_found` envelope and MUST render a not-found panel, not HTTP 500.

#### 5.5.1 Panel content

| Section | Content |
|---------|---------|
| Header | Title, status, **type badge** (task / epic / gate + `gate_type`), creating agent, `created_at`, project chip, `reopened` marker when applicable, **Expand** button |
| **Why this task is here** *(Needs attention only)* | Reason chips with one-line supporting facts (§5.2.2) |
| **Why can't this run** *(blocked tasks)* | The blocker chain (§5.5.2) |
| Hierarchy | Parent breadcrumb + children table (§5.5.3) |
| Gate context *(gates)* | §5.5.4 |
| Provenance | `discovered_from` both directions: "Discovered while working on: X" (incoming) and "Spawned follow-ons: …" (outgoing) |
| Tags / Description / Metadata | Full tag list; markdown-rendered description; `metadata` key-value table |
| Active claims | `aspect / agent / expires_at / time remaining`; refreshed on SSE claim events. The list shows **active claims only** — expired claims are unobservable and Lens must not imply otherwise. |
| Resolution | `resolved_at`, `outcome` (completed), cancellation timestamp. The cancel *reason* is event-only in Lithos — Lens MAY show it live from the event but MUST NOT promise it survives a reload. |
| Findings | Full timeline (§5.6) |
| Actions *(writes enabled)* | The applicable §5C actions for the task's state |

#### 5.5.2 Blocker chain — text baseline + mini-graph

- **Text chain (no-JS baseline, required):** one line per immediate blocker with live status — e.g. `blocked by "Design schema" (open, claimed by agent-zero)`, `waiting on gate "Human review" (human, waiting 2d)`, `blocker "Old spike" was cancelled — unsatisfiable`, `cycle: A → B → A`. Sourced from the task's `task_blocked` entry when available, else from incoming `blocks`/`waits_on_gate` edges plus per-predecessor `lithos_task_get`.
- **Lazy per-level expansion:** each unfinished blocker line carries an expander that loads *its* blockers one level deeper (HTMX fragment), bounded at **depth ≤ 5**; cycles render an explicit callout instead of recursing.
- **Mini-graph (progressive enhancement):** a Cytoscape 1–2-hop dependency neighbourhood rendered above the text chain, using the §5.7 styling vocabulary. The text chain remains the accessible baseline. Sequencing: ROADMAP T2.

#### 5.5.3 Hierarchy

- **Parent breadcrumb:** the incoming `parent_child` chain recursed to the root epic. Hierarchy is a single-parent forest (enforced upstream via `parent_exists`), so this is a simple chain, not a DAG walk.
- **Children table:** `lithos_task_children(task_id, recursive=false)` with per-child status and type; a "show full subtree" toggle switches to `recursive=true`.

#### 5.5.4 Gate context

For `task_type="gate"` tasks: the `gate_type` badge; a live countdown to `ready_at` for timer gates; the advisory metadata rendered as a key-value table (verbatim — Lithos does not interpret these keys and neither does Lens); and the **waiter list** — tasks blocked by this gate, via `lithos_task_edge_list(task_id, direction="outgoing", types=["waits_on_gate"])`, each with live status. The detail page should make it easy to judge what resolving the gate would unblock.

### 5.6 Findings Timeline

`lithos_finding_list(task_id)` is called when the detail surface opens and refreshed on relevant SSE events for the open task.

- Findings render chronologically. Each entry shows posting agent, timestamp (relative + absolute on hover), and summary text.
- **Knowledge link** *(only when `finding.knowledge_id` is non-null)*: a clickable label opening `/note/{knowledge_id}`. The label is the note title, resolved via `lithos_read(id=knowledge_id, max_length=1)` (truncated reads return complete frontmatter metadata — this is the cheap title fetch), cached per panel. On read failure the label falls back to "View document" with a non-blocking warning.
- Reopen history: the durable `[Reopened]` findings posted by `lithos_task_reopen` render with a distinct marker and drive the row's `reopened` marker.
- No paging controls; very long timelines MAY collapse older findings behind a "Show older findings" disclosure.
- **Most-significant findings** *(LLM, optional; ROADMAP X1)*: when `llm.enabled && llm.findings_curation_enabled`, the timeline header shows an **All findings / Most significant** toggle. "Most significant" passes the findings list to the configured LiteLLM provider and returns the K highest-signal findings (completions, decisions, surprises, contradictions) each with a one-line rationale. Hidden when LLM is disabled.

### 5.7 Task Graph Page

`GET /tasks/graph?project=<slug>` or `GET /tasks/graph?epic=<id>` renders the dependency DAG for exactly one scope (a scope is required; the unscoped route renders a scope picker).

**Data assembly**
- **Node set:** for project scope, the project's tasks per §5B.1 (open tasks always; recently-resolved per the `resolved_since` window, toggleable); for epic scope, `lithos_task_children(epic_id, recursive=true, include_closed=true)` plus the epic itself.
- **Edge set:** there is **no bulk graph fetch upstream** (ROADMAP ledger ask) — Lens fans out `lithos_task_edge_list(task_id, direction="both")` per node, bounded by a semaphore of `[graph].fetch_concurrency` (16), and **dedupes** edges (each edge is returned from both of its endpoints). Edge records are `{from_task_id, to_task_id, type, direction, metadata, created_by, created_at}`.
- **Ghost nodes:** edges whose far endpoint lies outside the scope render that endpoint as a dimmed ghost node (fetched via `lithos_task_get` for its title/status) rather than dropping the edge — cross-scope dependencies must be visible.
- **Snapshot cache:** the assembled `{tasks, edges}` snapshot is cached in-process per scope with TTL `[graph].cache_ttl_s` (30s), invalidated early by any task event touching a node in the snapshot.
- **Size guard:** scopes larger than `[graph].max_tasks` (300) are refused with a "narrow your scope" panel instead of a degraded render.

**No-JS baseline (required)**
- **Topological text layers:** Kahn's algorithm over `blocks` + `waits_on_gate` edges; each layer lists its tasks with status. Cycle members cannot be layered — they are excluded from the layering and rendered in an explicit "dependency cycle" callout naming the members.
- **Hierarchy tree:** an indented `parent_child` tree for the scope.

**Cytoscape rendering (progressive enhancement)**
- Layout: `breadthfirst` (the DAG's natural shape); no physics simulation.
- Node **colour = status** (open, completed, cancelled; blocked tasks tinted; in-progress tasks with a subtle pulse), node **shape = type** (ellipse `task`, round-rect `epic`, diamond `gate`).
- Edge style per type: **solid** `blocks`, **dashed** `waits_on_gate`, **dotted** `discovered_from`, **thin light** `parent_child` (toggleable off — hierarchy is noise when reading dependency flow).
- Click = side panel (task summary, blockers, link to detail); double-click = navigate to `/tasks/{task_id}`.
- On task events touching the snapshot, show a **"graph changed — refresh"** pill rather than auto-re-layouting under the operator's cursor.

### 5.8 Live Updates & Event Pipeline

#### 5.8.1 Connection model

Lens holds one server-side subscription to Lithos `GET /events`, passing a server-side `types=` filter for the consumed set. Browser tabs connect to Lens's own `GET /tasks/events` re-broadcast endpoint, which emits normalized events plus recomputed metric fragments.

Consumed upstream event types (tasks surface): `task.created`, `task.claimed`, `task.released`, `task.completed`, `task.cancelled`, `task.updated`, `task.reopened`, `finding.posted`, `agent.registered`. (Knowledge surfaces add `note.*` and `edge.upserted` — §8.5.) Payload gotchas are catalogued in §16.1.1.

#### 5.8.2 Scope-aware normalization

The legacy drop-if-no-`task_id` rule is replaced by a **scope-aware** rule:
- **Task-scoped** types (all `task.*`, `finding.posted`) require `task_id`; events without it are dropped with a warning.
- **System-scoped** types (`agent.registered`) pass through with `task_id=""` and `requires_refresh=false` — they invalidate the agent-dropdown data only and MUST NOT trigger a dashboard refresh.
- The **`lens.*` namespace is reserved for Lens-internal synthetic events** and MUST never collide with upstream types. Current members: `lens.refresh` (reconnect backstop, §5.8.3) and `lens.edge_upserted` (§5C.3 — emitted after Lens's own edge writes because no upstream task-edge event exists).

Normalized browser events preserve the Lithos event `id` for dedupe and carry `requires_refresh=true` when the upstream payload is too sparse for a complete UI update (`task.updated` carries only `task_id`, so it always forces a refetch). Browser handlers MUST tolerate duplicates and out-of-order reconciliation.

#### 5.8.3 Reconnection

- On disconnect: exponential backoff per `events.reconnect_backoff_ms`; a `Live updates paused — reconnecting` badge; polling fallback every `tasks.auto_refresh_interval_s` with a transient `Refreshed via fallback` toast per successful poll.
- On reconnect: Lens sends **`Last-Event-ID`** so Lithos replays its ring buffer from the last received event, **and** broadcasts one synthetic `lens.refresh` to browser subscribers as a correctness backstop (replay beyond the buffer is impossible, so a full refresh is the only guarantee).

#### 5.8.4 Server-side recompute and rolling buffers

- SSE events mark derived metrics dirty; a debounce window (`tasks.metrics_debounce_ms`, 2000ms) batches bursts; one recompute per window pushes OOB fragments to all open tabs. Manual refresh, page load, and reconnect bypass the debounce.
- A **recent-findings rolling buffer** (server-side ring buffer, size `tasks.recent_findings_drawer_size`, warmed at boot over `tasks.recent_findings_warmup_window_h`) powers the collapsible **Recent findings drawer** and the per-row latest-finding line, and feeds the Planning View's stalled detection. It survives tab refresh and stays consistent across tabs.
- Timer-gate self-refresh (§5.2.3) is scheduled client-side from a server-rendered `min(ready_at)` attribute.

### 5.9 Notifications

- **Title-badge** (always on by default; `[tasks].notifications.title_badge`): the page `<title>` becomes `(N) Lithos Lens` while there are unseen Needs-attention items; tab focus clears it.
- **Desktop notifications** (opt-in; wiring sequenced at ROADMAP X1): an "Enable notifications" affordance in the header. Once granted, Lens fires notifications on **transition events only** (never steady-state), retargeted at the graph-native triggers:
  - a row **enters Needs attention** (any rule),
  - a **human gate** enters the waiting state,
  - a task becomes **unblocked** (moves Blocked → Ready, including via a completion's `unblocked[]`).
  Body format: `<task title> — <reason>`; click opens `/tasks?selected=<task_id>`. Grant state lives in `localStorage`; all other preferences live in cookies + URL.

### 5.10 Cross-View Linking

- **Tasks → Knowledge:** findings with non-null `knowledge_id` link to `/note/{knowledge_id}` (§6). Straight UUID passthrough — no inference.
- **Knowledge → Tasks:** notes whose `metadata.source` records a producing task render a "Produced by task" chip linking to `/tasks/{task_id}` (§6.6).

### 5.11 API (reads)

| Endpoint | Purpose |
|----------|---------|
| `GET /tasks` | Operator View dashboard |
| `GET /tasks?selected=<task_id>` | Same page with the side panel pre-opened |
| `GET /tasks/{task_id}` | Full-page detail route (also serves the panel fragment) |
| `GET /tasks/{task_id}/findings` | Findings timeline fragment |
| `GET /tasks/{task_id}/blockers?depth=<n>` | Lazy blocker-chain expansion fragment (depth ≤ 5) |
| `GET /tasks/findings/recent` | Recent-findings drawer fragment (rolling buffer) |
| `GET /tasks/graph?project=<slug>\|epic=<id>` | Dependency graph page (§5.7) |
| `GET /tasks/plan` | Planning View (§5A) |
| `GET /tasks/events` | SSE re-broadcast endpoint for browser tabs |
| `POST /api/tasks/findings/curate` | LLM findings curation (only when `llm.enabled`; ROADMAP X1) |

Write endpoints are specified in §5C.7 and exist only when `[writes] enabled = true`.

---

## 5A. Tasks View — Planning View

The Planning View answers **"what should happen next?"** across the agent fleet. It lives at `/tasks/plan` and consumes the same data contract and SSE pipeline as the Operator View — it is a different rendering of the same joined snapshot, extended with the dependency-graph machinery from §5.7.

### 5A.1 Purpose & Scope

Three stacked sections answer three sub-questions, top to bottom:

1. **What do I (a human) need to act on?** — Human-actionable section, now led by the human-gate queue.
2. **Where is throughput stuck?** — Project breakdown with starvation, bottleneck, and stalled signals.
3. **What's the overall shape of work?** — Throughput overview on `resolved_since` windows.

### 5A.2 Layout

```
┌─────────────────────────────────────────────────────────────┐
│  Top-nav: [Tasks] [Tasks · Plan] [Knowledge ▾]              │
│  Filter bar: project | window | hide dormant                │
├─────────────────────────────────────────────────────────────┤
│  👤 Human-actionable                                         │
│     1. Human gates awaiting approval (oldest first,          │
│        each with "unblocks N" waiter count)                  │
│     2. Open tasks tagged `[tasks].human_actionable_tag`      │
│     3. Tasks claimed by a human agent (resume your work)     │
├─────────────────────────────────────────────────────────────┤
│  📊 Project breakdown                                        │
│     per project: ready / in-flight / blocked depths,         │
│     starvation · agent-overload · keystone · stalled flags   │
├─────────────────────────────────────────────────────────────┤
│  📈 Throughput overview                                      │
│     per project over the resolved_since window:              │
│     completed, cancelled, completion ratio,                  │
│     median time-to-resolve, median ready-age                 │
└─────────────────────────────────────────────────────────────┘
```

### 5A.3 Human-actionable section

- **Human-gate queue (top):** every open `gate_type="human"` gate, oldest first, each showing its waiter count ("approving this unblocks N tasks") and — when writes are enabled — the approve action (§5C.2). This queue is the single most operator-relevant list in the product; it MUST come first.
- **Tagged tasks:** open tasks carrying `[tasks].human_actionable_tag` (default `human`), grouped by project, oldest first.
- **Human-claimed tasks:** open tasks claimed by an agent listed in `[tasks].human_agents` — so a human can resume their own work.
- Empty state: `Nothing for you to do right now ✓`.

### 5A.4 Project breakdown

For every known project (§5B.1 universe), one row showing ready / in-flight / blocked depths (from the shared frontier join) and flag chips:

| Flag | Rule |
|------|------|
| **Starvation (v2)** | Project has **> 0 open workable tasks but 0 ready-and-unclaimed tasks** — nothing can be picked up. Sub-classified on the chip: `fully-blocked` (nothing is ready) vs `fully-claimed` (ready work exists but every ready task is claimed). The old queue-depth rule is replaced: with a graph, "unclaimed" only matters on the frontier. |
| **Agent overload** | In-flight depth ≥ `bottleneck_min_inflight` (3) AND one agent holds ≥ `bottleneck_concentration` (0.7) of those claims. Tooltip names the dominant agent. |
| **Keystone task** | The open task with the most **open transitive dependents** via `blocks`/`waits_on_gate` (computed over the §5.7 graph snapshot). Rendered as `keystone: "<title>" — unblocks N`. Completing keystones is the highest-leverage scheduling move; the chip links to the task detail. |
| **Stalled** | ≥ 1 in-progress task in the project with no `finding.posted` in the last `stalled_no_findings_hours` (24), per the rolling buffer. Stalled rows also get a row decoration on the Operator View but are never promoted into Needs attention. |

Hover on any flag → tooltip with the rule facts (which agent, which task, which chain).

### 5A.5 Throughput overview

For every project, over the last `tasks.throughput_window_days` (30) using **`resolved_since`** filtering (not `created_at` — resolution time is the honest window):

| Field | Value |
|-------|-------|
| Completed / Cancelled counts | Tasks with `resolved_at` in the window, by terminal status |
| Completion ratio | `completed / (completed + cancelled)` (or `—` when both zero) |
| Median time-to-resolve | Median `resolved_at − created_at` over tasks resolved in the window |
| Median ready-age | Median age of the project's currently ready-and-unclaimed tasks (how long available work sits) |

Ordering: completed desc, then ratio desc, then alphabetical. Dormant projects (zero resolved in window) show `0 / 0` by default; a `Hide dormant` toggle (cookie + URL) suppresses them. Sparklines remain deferred.

### 5A.6 Project discovery and caching

The project universe is the union of `lithos_tags(prefix="project:")` and the distinct `metadata.project` values observed in the loaded snapshot (§5B.1), fetched once per request and shared with the Operator View. Cache invalidation: page load, manual refresh, and `task.created` events introducing a never-seen project.

### 5A.7 API

| Endpoint | Purpose |
|----------|---------|
| `GET /tasks/plan` | Server-rendered Planning View |
| `GET /tasks/plan/projects` | Project breakdown fragment (independently refreshable) |
| `GET /tasks/plan/throughput` | Throughput overview fragment |

### 5A.8 OTEL spans

`lens.tasks.plan`, `lens.tasks.plan.projects`, `lens.tasks.plan.throughput` (see §15).

---

## 5B. Project Tracking Conventions

These conventions are **normative for Lens** and assumed across the Tasks views and the Knowledge Browser. Except where Lens itself creates tasks (§5C), they are conventions agents must follow.

### 5B.1 Two live project conventions — reconciliation rules

Two project conventions are in active use in the production corpus, **and their counts disagree** (observed live: lithos-loom has 87 tasks via `metadata.project` and 68 via the `project:lithos-loom` tag):

1. **Metadata convention:** `metadata.project = "<slug>"` — what Lithos itself understands (`lithos_task_ready(project=…)` shorthand, `lithos_task_spawn` project inheritance).
2. **Tag convention:** a `project:<slug>` tag — the original Lens convention, still widespread.

Requirements:
- `[tasks].project_convention` selects the honoured convention: `"metadata"`, `"tag"`, or `"both"` (default `"both"`).
- Under `"both"`, a task's **project** is `metadata.project` when present, else the `project:<slug>` tag value.
- When both are present **and disagree**, Lens uses the metadata value and emits a telemetry warning (`lens.tasks.project_convention_conflict`) — never silently drops either.
- The project **universe** (filter dropdowns, Planning View rows) is the **union** of both conventions' slugs, so no project is invisible to its own view.
- Tasks created by Lens (§5C.2) MUST write **both** conventions until upstream unifies them (ROADMAP dependency ledger tracks the unification ask).

### 5B.2 Project documents

All project-related knowledge documents are stored under `projects/<project-slug>/` and tagged `project:<project-slug>` plus relevant category tags. Documents describing a project's overall context also receive the tag `project-context`.

### 5B.3 Project tasks

Tasks must carry their project at creation time — under the `"both"` posture, that means both the tag and the metadata key:

```
lithos_task_create(
  title="Implement BLE reconnect logic",
  agent="agent-zero",
  tags=["project:ganglion", ...],
  metadata={"project": "ganglion"},
)
```

### 5B.4 Project slug naming

Slugs are derived from the project title: lowercase, spaces → hyphens, special characters removed or replaced.

| Title | Slug |
|-------|------|
| `Ralph++` | `ralph-plus-plus` |
| `Kindred Code` | `kindred-code` |
| `Restore SGI Indy` | `restore-sgi-indy` |

### 5B.5 Known active projects

Illustrative, not exhaustive — the live universe comes from §5B.1 discovery:

| Project | Slug |
|---------|------|
| Lithos Core | `lithos-core` |
| Lithos Ecosystem | `lithos-ecosystem` |
| Lithos Loom | `lithos-loom` |
| Lithos Lens | `lithos-lens` |
| Ganglion | `ganglion` |
| Influx | `influx` |
| Kindred Code | `kindred-code` |
| Ralph++ | `ralph-plus-plus` |
| Restore SGI Indy | `restore-sgi-indy` |
| NAO bridge | `nao-bridge` |
| Cardinal | `cardinal` |
| … | … |

### 5B.6 Ideas

Speculative items not yet linked to a project live under `ideas/` with the tag `idea`, confidence `0.5–0.7`. If softly related to a project they may carry the `project:<slug>` tag alongside `idea`. When promoted, the document moves to `projects/<slug>/`.

### 5B.7 Lens reading patterns

| Goal | Method |
|------|--------|
| All tasks for a project (tag convention) | `lithos_task_list(tags=["project:<slug>"])` |
| All tasks for a project (metadata convention) | `lithos_task_list(metadata_match={"project": "<slug>"})` |
| Ready tasks for a project | `lithos_task_ready(project="<slug>")` *(metadata shorthand)* |
| Effective project set under `"both"` | Union of the two list queries, deduped by id — or client-side filtering over the dashboard snapshot (§5.3), which is what Lens does |
| All docs for a project | `lithos_list(path_prefix="projects/<slug>/")` |
| Search within a project | `lithos_search(query="...", path_prefix="projects/<slug>/")` |
| Project context docs | `lithos_list(path_prefix="projects/", tags=["project-context"])` |

### 5B.8 Multi-project tasks

A task carrying multiple `project:*` tags (or a tag conflicting with metadata) is unusual but supported: the Operator View renders one chip per distinct project and emits a telemetry warning; the Planning View shows the task once per group it claims membership of (visible duplication beats hidden behaviour).

### 5B.9 Configurability

`[tasks].project_tag_key` (default `"project"`) makes the tag-key configurable per deployment; `[tasks].project_convention` selects the reconciliation posture (§5B.1).

---

## 5C. Curated Write Actions

> [!important] This section replaces every previous "strictly read-only" statement
> Lens is **read-only by default** and becomes an operator console for a **small curated action set** when `[writes] enabled = true`. This is deliberately neither read-only nor CRUD: the actions are the ones an operator needs while looking at the dashboard, and nothing more. Sequencing: ROADMAP T3.

### 5C.1 Posture and security boundary

- **Default off.** With `[writes] enabled = false` (the default), no mutating route is registered — POSTs to write paths return **404**, not 403. Templates render no write affordances.
- **In scope:** approve/complete human gates, reopen, cancel, create task/epic/gate, add dependency edges.
- **Out of scope (permanently, absent new requirements):** claim/renew/release (agents manage their own claims), `lithos_task_update` (editing titles/descriptions/tags), deleting tasks (no such tool exists), deleting or re-typing task edges (**no `lithos_task_edge_delete` exists upstream** — see 5C.2), bulk operations.
- **Security boundary — stated explicitly:** Lens has **no authentication or authorization**. It is designed for a single-operator, trusted local network. Anyone who can reach the Lens port can perform any enabled action. Deployments MUST NOT expose Lens beyond the trusted network, and the settings view MUST state this boundary whenever writes are enabled. The only request-level protections are the Origin/Referer check and operator attribution (5C.6), which are hygiene, not security.

### 5C.2 Actions

| Action | Lithos tool | Surfaces |
|--------|-------------|----------|
| Approve / complete gate | `lithos_task_complete(task_id, agent=<operator>)` | Gates section rows, gate detail, Planning human-gate queue |
| Reopen | `lithos_task_reopen(task_id, agent=<operator>)` | Detail page of completed/cancelled tasks |
| Cancel | `lithos_task_cancel(task_id, agent=<operator>, reason=…)` | Detail page and row overflow menu of open tasks |
| Create task / epic / gate | `lithos_task_create(title, agent=<operator>, description?, tags?, metadata?, task_type?, depends_on?, parent_task_id?)` | "New task" affordance on the dashboard; "add child" on epic detail |
| Add dependency edge | `lithos_task_edge_upsert(from_task_id, to_task_id, type, agent=<operator>)` | Task detail "add dependency" affordance |

Per-action requirements:

- **Approve / complete gate.** The primary write. On success, surface the returned **`unblocked[]`** as a toast — `Unblocked N tasks` with the first few titles — and offer **Undo**, implemented as `lithos_task_reopen` on the gate (which re-blocks the waiters; see reopen semantics below). Lens deliberately offers **no complete action for ordinary tasks** — agents finish their own work; the operator's completion surface is gates only.
- **Reopen.** On success, surface the returned **`reblocked[]`** — `Re-blocked N dependents` — since reopening a *completed* blocker/gate takes previously-ready dependents back off the frontier. Reopening a *cancelled* blocker instead **un-strands** its dependents (`blocker_unsatisfiable` → waiting) and re-blocks nothing; the UI copy MUST distinguish the two, because reopen-the-cancelled-blocker is the standard remediation for Needs-attention rule 1.
- **Cancel — consequence-aware.** When `[writes].confirm_cancel = true` (default), the confirm step computes the count of **open transitive dependents** via `blocks`/`waits_on_gate` (from the §5.7 graph machinery) and states the consequence plainly: `Cancelling will strand N dependent tasks (they become permanently blocked until re-routed)`, listing the first few. The confirmation MUST work without JavaScript (a server-rendered confirm page: `GET …/cancel` → confirm form → `POST`). The optional `reason` is sent, but Lithos persists it **only in the event payload** — the UI must not promise it survives a reload.
- **Create.** A single form for `task_type` ∈ `task` / `epic` / `gate`, with `depends_on[]` (predecessor picker), `parent_task_id` (parent picker), tags, and project (written to **both** conventions per §5B.1). For gates, gate metadata is **validated client-side first** — `gate_type` must be one of `human`/`timer`/`ci`/`pr`/`external_task`; `ready_at` is required for `timer` and must parse as an ISO datetime — then revalidated server-side by Lithos (`invalid_input`). Advisory keys are passed through verbatim.
- **Add dependency edge.** Edge types offered: `blocks`, `parent_child`, `discovered_from` (a `waits_on_gate` edge additionally requires the *from* task to be a gate and is offered only from gate detail pages). **There is no edge delete** — the UI MUST say so before the write ("dependency edges cannot be removed once created"), and a `parent_exists` rejection is a dead end (re-parenting requires an edge delete that doesn't exist). Both gaps are top asks in the ROADMAP dependency ledger; Lens documents them honestly rather than working around them.

### 5C.3 Write architecture

- **Route gating:** POST routes are registered at startup only when `[writes] enabled = true` (§4.1 step 9).
- **Form-encoded POSTs** (`application/x-www-form-urlencoded`); no JSON write API.
- **Dual-mode responses:** an HTMX request receives an updated fragment; a plain form submission receives a **303 See Other** redirect (POST-redirect-GET) to the affected page. Every write path works without JavaScript.
- **Refresh-after-write, never optimistic:** after any successful write, Lens re-fetches the affected data and renders from fresh reads. Write results (e.g. `unblocked[]`) inform toasts, not state.
- **Cross-tab convergence:** other open tabs converge via the normal SSE pipeline — upstream events cover complete/cancel/reopen/create. **Edge writes emit no upstream event**, so after a successful `lithos_task_edge_upsert` Lens publishes a synthetic internal **`lens.edge_upserted`** event (carrying `from_task_id`, `to_task_id`, `type`) through its own hub so all tabs refresh. If a `task_edge.upserted` event ever lands upstream (ledger ask), the synthetic path retires.

### 5C.4 Error envelope mapping

Lithos write failures return `{status: "error", code, message}`. Lens MUST map each code to operator-actionable copy — never surface a bare code, and never a 500:

| Code | Operator-facing copy (shape) |
|------|------------------------------|
| `cycle` | "This dependency would create a cycle: *\<message members\>*. Nothing was changed." |
| `parent_exists` | "*\<Task\>* already has a parent. Re-parenting isn't possible — Lithos has no edge delete (see ROADMAP ledger)." |
| `self_edge` | "A task can't depend on itself." |
| `not_a_gate` | "*\<Task\>* isn't a gate — `waits_on_gate` edges must start from a gate task." |
| `task_not_found` | "Task no longer exists (it may have been removed since you loaded the page)." |
| `invalid_input` | Field-level re-render of the form with the upstream message (e.g. invalid gate metadata) |
| *(unknown code)* | Show code + message verbatim with a "report this" hint — forward-compatible with new upstream codes |

### 5C.5 Operator identity

Writes are attributed to a **named human operator**, distinct from the Lens service agent:

- Resolution order: **`lens_operator` cookie** → **`[writes].default_operator`** → an inline **"Acting as"** prompt that blocks the first write until an identity is supplied (then sets the cookie).
- On first use of an identity, Lens registers it: `lithos_agent_register(id=<operator>, type="human")`.
- Every write passes `agent=<operator>`. The service agent (`lithos-lens`, type `web-ui`) is used for reads and registration only — audit trails MUST be able to distinguish "Lens the process" from "the human driving it".
- The current identity is displayed near every write affordance, with a "switch" affordance.

### 5C.6 Concurrency, integrity, and audit

- **`expected_status` pre-check:** every write form carries the task status the operator saw. The handler re-fetches `lithos_task_get` and aborts with a conflict page ("this task is now *\<status\>* — reload") when the status differs. Lithos has no compare-and-set on tasks; this pre-check narrows (but cannot close) the race window, and the docs say so.
- **Origin/Referer check:** all POSTs require a same-host `Origin` (or `Referer`) header; mismatches are rejected with 403. This is CSRF hygiene on a trusted network, not an auth mechanism.
- **Audit log:** every write attempt emits one structured log line — operator, action, `task_id`, argument summary, and the full result envelope — plus an OTEL span `lens.writes.<action>` (§15).

### 5C.7 API (writes — registered only when enabled)

| Endpoint | Action |
|----------|--------|
| `POST /tasks/{task_id}/approve` | Approve human gate — calls `lithos_task_complete` on the gate. The handler **rejects a non-gate task** (409); there is deliberately no generic task-complete route (§5C.2) |
| `POST /tasks/{task_id}/reopen` | Reopen |
| `GET /tasks/{task_id}/cancel` → `POST /tasks/{task_id}/cancel` | Consequence-aware cancel (confirm page + submit) |
| `GET /tasks/new` → `POST /tasks/new` | Create task / epic / gate |
| `POST /tasks/{task_id}/edges` | Add dependency edge |

---

# Part C — Knowledge Browser

The Knowledge Browser is the second surface. Sections are ordered by delivery track (see ROADMAP K1–K4 and the deferred pool): **Note View** and **Search** first, then the **Graph View**, then Feed / Feedback / Conflict Resolution, with Comparison and Reading Paths preserved in the deferred pool. Every section assumes the Common Core (§1–§4).

---

## 6. Note View

### 6.1 Purpose

`/note/{id}` is the canonical document page — the target of finding links, search results, feed cards, graph nodes, and wiki-links. It must render a real, readable document: this replaces the minimal plaintext renderer from the first Tasks milestones.

Data contract: one `lithos_read(id=…)` for the document, one `lithos_related(id, depth=1)` for the related panel, plus the capped title fan-out (§6.5). Note: the `lithos_read` response has **no top-level `path` field** — when Lens needs the note's path it must take it from metadata or a list query, not assume it on the read payload.

### 6.2 Markdown rendering — XSS posture (requirement)

- Rendering is **server-side** with `markdown-it-py`, configured `commonmark` preset plus the `table` and `strikethrough` extensions.
- **Raw HTML in note bodies is escaped, never rendered.** Note content is untrusted input (agents write it); Lens's safety posture is *never emit unescaped note content*, which removes the need for a sanitizer dependency.
- The link validator (`validateLink`) MUST reject `javascript:` and `data:` URL schemes (and any scheme not in an allow-list of `http`, `https`, `mailto`, and relative paths).
- If markdown parsing fails for any reason, the fallback is HTML-escaped plaintext — never raw passthrough.

### 6.3 Wiki-link handling

`lithos_read.links` entries are **unresolved** — each is `{target, display}` with no note id (upstream ask in the ROADMAP ledger). Lens therefore resolves wiki-links per-click through a resolver route:

**`GET /knowledge/resolve?target=<target>&from=<source-note-id>`**

1. **UUID-shaped target** → redirect to `/note/{target}`.
2. **Path probe:** `lithos_read(path=target + ".md", max_length=1)` — on success, redirect to `/note/{id}`. (Truncated reads return complete frontmatter metadata, so this probe is cheap.)
3. **Disambiguation:** gather candidates from the source note's `lithos_related(from, include=["links"])` outgoing set and `lithos_list(title_contains=target)`. Exactly one candidate → redirect; multiple → a disambiguation page listing candidates with paths; zero → an "unresolved link" page offering a search link.

**Rendering rule:** wiki-links are spliced at the **token level** — `[[target]]` / `[[target|display]]` patterns are replaced only inside *text* tokens of the parsed token stream. Lens MUST NOT regex over raw markdown (that would rewrite wiki-link-shaped text inside code fences and inline code).

### 6.4 Metadata chips and lede

| Element | Source |
|---------|--------|
| `note_type` chip | frontmatter (`observation` / `agent_finding` / `summary` / `concept` / `task_record` / `hypothesis`) |
| `status` chip (colour-coded) | `active` / `archived` / `quarantined` |
| Confidence | `confidence` rendered as a percentage |
| `access_scope` chip | `shared` / `task` / `agent_private` |
| `namespace` chip | explicit or path-derived |
| Tags | one chip per tag, each linking to `/knowledge?tag=<tag>` |
| Lede | `summaries.short` rendered above the body when present |
| Supersedes | when `supersedes` is set, a link to the superseded note ("replaces: …") |
| Authorship | `author`, `contributors`, `created_at` / `updated_at` |

### 6.5 Related panel

One `lithos_related(id, depth=1)` populates four groups:

- **Outgoing wiki-links** and **back-links** (incoming) — titles are included in the response.
- **Provenance** — `sources` (derived-from) and `derived` (derives), plus `unresolved_sources` rendered as inert stubs.
- **Typed LCMA edges** — incoming/outgoing edge records with `type`, `weight`, and `conflict_state`. Edge records carry endpoint ids only, so Lens resolves endpoint titles via a **capped, cached `lithos_read(id, max_length=1)` fan-out** (`[knowledge].related_title_fanout_cap`, default 20; beyond the cap, render id stubs with lazy resolution on demand).
- Each entry links to its note page; an "open in graph" affordance links to `/knowledge/graph?focus=<id>` (§8).

### 6.6 Produced-by-task chip

When `metadata.source` contains a task id, Lens calls `lithos_task_get` and renders a "Produced by task: *\<title\>*" chip linking to `/tasks/{task_id}`. A `task_not_found` result (or a non-task `source` value) degrades to no chip — `source` is a free-form provenance field.

### 6.7 Cited-by panel — blocked upstream

**Requirement:** a note page SHOULD show "cited by findings in tasks X, Y" — the reverse of the finding → note link.

**Status: blocked upstream.** `lithos_finding_list` requires `task_id` (no `knowledge_id` filter exists), and `finding.posted` events lack `knowledge_id` — so there is no way to answer "which findings cite this note" short of scanning every task's findings. This requirement is **gated on the ROADMAP dependency-ledger ask**; Lens MUST NOT ship the O(all-tasks) scan workaround.

### 6.8 Retrieval-stats panel

`lithos_node_stats(node_id)` supplies a small stats panel — `salience`, `retrieval_count`, `cited_count`, `misleading_count`, `last_retrieved_at` — explaining why retrieval does or doesn't surface this note. `doc_not_found` renders default values. Arrives with the cognitive-search milestone (ROADMAP K3).

---

## 7. Knowledge Search

### 7.1 `/knowledge` landing

- A search box lives in the global nav on every Knowledge page; submitting goes to `/knowledge?q=…`.
- **With a query:** `lithos_search(query, mode="hybrid", limit=[knowledge].search_limit)`. Result cards show title, path, score, `updated_at`, an `is_stale` marker, and the snippet.
- **Snippets MUST be rendered escaped.** Verified live: `lithos_search` snippets contain raw markdown (headings, wiki-link syntax, code). Lens HTML-escapes snippet text (query-term highlighting, if any, is applied *after* escaping). Snippets are never fed through the markdown renderer.
- **Without a query:** a recently-updated list via `lithos_list(limit=[knowledge].recent_limit)` ordered by `updated`.
- Filters: `q` and `tag` only (`?tag=` maps to the `tags=` argument of search/list). Richer filtering belongs to the feed (§9).

### 7.2 Cognitive search evolution

Later (ROADMAP K3), `lithos_retrieve` becomes the **default engine** for `/knowledge?q=`:

- On any retrieve error, Lens falls back **silently** to `lithos_search` with a small "fast search" badge; a persistent toggle lets the user force fast search.
- Result cards gain the LCMA audit fields: **scout chips** (`scouts`), expandable **reasons**, and a **salience bar** — the retrieve result shape is `lithos_search`-compatible plus these additive fields.
- The response-level `receipt_id`, `temperature`, and `terrace_reached` render as **footer provenance text only** — no click-through, because no MCP read surface for receipts exists (ROADMAP ledger).

> [!warning] Working-memory rule (hard requirement)
> Lens MUST **never pass `task_id` to `lithos_retrieve`**. When `task_id` is set, Lithos upserts per-`(task_id, node_id)` working-memory rows — human browsing must not pollute any task's working memory. Lens passes only `query`, `limit`, optional `tags`/`path_prefix`/`namespace_filter`, and `agent_id=<lens service agent>` for audit and access-scope gating.

### 7.3 Answer synthesis and complexity (LLM, optional)

When `llm.enabled = true`, a "Synthesise answer" toggle passes the top-N snippets to the configured LLM with a citation-required prompt and renders the answer above the (still visible) result list; citations click through to note pages. If `llm.synthesis_prefer_mcp = true` and Lithos exposes a synthesis tool, Lens prefers the MCP path. A session-scoped complexity slider (1–5, default `llm.default_complexity`) modulates verbosity in every LLM prompt. LLM failures hide the synthesis block, keep the results, and show a non-blocking badge. Hidden entirely when LLM is disabled. Sequencing: ROADMAP X1+.

---

## 8. Knowledge Graph View

### 8.1 Purpose and modes

Interactive visualisation of the knowledge base as a typed, weighted graph. Two modes:

- **Focus (ego-graph) mode — first-class:** `/knowledge/graph?focus=<note-id>` renders the neighbourhood of one note. This is the mode reachable from note pages and the one sized for the real corpus (~2,900 notes).
- **Global mode — second:** `/knowledge/graph` renders the whole (capped) edge set for cluster/contradiction reconnaissance.

### 8.2 Data assembly

- **Focus mode:** `lithos_related(focus, depth=1..2)` supplies wiki-links, provenance, and typed edges around the focus; the frontier expands via `lithos_related` on neighbours up to the requested depth, capped by `[knowledge].graph_focus_max_nodes` (250).
- **Global mode:** `lithos_edge_list(namespace?)` for typed edges, capped by `[knowledge].graph_global_max_nodes` (500).
- Node detail panel: `lithos_related` + `lithos_node_stats` on demand.

### 8.3 Rendering

Nodes are sized by `node_stats.salience` (confidence fallback) and coloured by namespace or profile tag. Edge styling:

| Edge | Style |
|------|-------|
| Wiki-link | thin grey |
| `derived_from` / provenance | dotted grey |
| `related_to` | 🔵 blue |
| `builds_on` | 🟢 green |
| `contradicts` | 🔴 red — **unresolved** `conflict_state` renders dashed and emphasized, and an unresolved-contradictions counter shows in the toolbar; **resolved** renders muted with its resolution label (`accepted_dual` / `superseded` / `refuted` / `merged`) |
| `uses_method` | 🟡 yellow |
| `analogous_to` | 🟣 purple |
| *(unknown type)* | neutral with a text label — forward-compatible |

### 8.4 Interactions

- Click a node → side panel (summary, related, stats, "open note"); double-click → `/note/{id}`.
- **Bidirectional selection:** clicking a related-note row in the panel highlights and centres the corresponding node without rebuilding the layout.
- "Centre on this" rebuilds the ego-graph around the selected node.
- Filter panel: edge type, namespace, tag, date.
- **Centrality overlay** (toggle): betweenness centrality computed client-side over the loaded subgraph (`cy.elements().bc()`); top-K nodes get a halo. Recomputed when the visible subgraph changes; no MCP calls.
- `contradicts` edges expose the conflict-resolution panel (§11).

### 8.5 Freshness

The graph subscribes to `note.created` / `note.updated` / `note.deleted` and `edge.upserted` through the shared pipeline. **Watcher-emitted note events may lack `id`** (they carry only `path`), so per-node patching is best-effort; the fallback is a debounced refetch of the current scope. As on the task graph page, changes show a "graph changed — refresh" pill rather than auto-re-layouting.

### 8.6 Caps and degradation

Exceeding a node cap degrades to a "refine your filters" banner with a truncated sample — never a browser-melting render. Cytoscape comfortably handles the configured caps; the caps exist to keep the *layout* legible, not just performant.

---

## 9. Feed View

*Sequencing: ROADMAP K4. Requirements preserved in condensed form.*

- Time-ordered cards over `lithos_list(path_prefix?, tags?, since?, limit, offset)` — title, updated date, tags, confidence, and a lede (`summaries.short` when present, else a content excerpt via `lithos_read`).
- Filter bar: tag chips, date range, `note_type`; pagination via `offset` with "Showing N of M" from `total`.
- Cards click through to `/note/{id}`; feedback buttons per §10; "open in graph" per §8.
- **Influx integration conventions are optional.** Profile membership as `profile:<name>` tags, per-profile scores in a `## Profile Relevance` body section, `## Abstract` extraction, and `**Local file:** /archive/...` links are Influx conventions, not Lithos guarantees. The feed MUST render gracefully when they are absent, and archive links appear only when the optional `/archive` mount (§3) exists.

---

## 10. Feedback Mechanism

### 10.1 Overview

Lens is where humans generate relevance feedback. For Influx-authored notes, feedback is profile-scoped and stored as tags on the existing note: `influx:rejected:<profile>` / `influx:accepted:<profile>`. The scheme is Influx-centric and optional; the mechanism (tag patches) is general.

### 10.2 Writing feedback — `lithos_note_update` patches

> [!important] The old read-then-rewrite-whole-note contract is obsolete
> Feedback writes use **`lithos_note_update`**, which patches frontmatter (tags / metadata / title / status) **without resending the body**. Lens never round-trips note content to change a tag.

Flow:

1. `lithos_read(id, max_length=1)` — fetch current tags and `version` (truncated reads return complete metadata).
2. Compute the new tag list (add/remove the feedback tags).
3. `lithos_note_update(id, agent=<operator>, tags=<new list>, expected_version=<version>)`.
4. On `{status: "version_conflict"}` — re-read and retry once; then surface the error.

The `tags` argument replaces the full list (that is why step 1 exists); `metadata` patches, when used, are additive per-key merges. Feedback writes follow the §5C gating and operator-identity contract — they are writes, attributed to the human operator, registered only when writes are enabled.

### 10.3 Entry points and API

| Entry point | Mechanism |
|-------------|-----------|
| Feed view | 👍 / 👎 buttons on each card |
| Note view / graph panel | 👎 button |

```
POST /api/feedback
{ "note_id": "<uuid>", "profile": "<profile>", "verdict": "accepted" | "rejected" }
```

Response: `{"status": "ok"}` or `{"status": "error", "message": "..."}` — errors are shown in the UI, never swallowed. Profile-scoped views SHOULD exclude notes carrying `influx:rejected:<active-profile>` by default.

---

## 11. Conflict Resolution UI

*The first knowledge write (deferred pool — see ROADMAP); follows the §5C write gating and operator-identity contract.*

When `contradicts` edges exist, Lens exposes a resolution panel on the relevant notes and on the edge in the graph view (§8.4):

```python
lithos_conflict_resolve(
    edge_id=edge_id,
    resolution="superseded",   # accepted_dual | superseded | refuted | merged
    resolver=operator_id,
    winner_id=winning_note_id,  # required when resolution == "superseded"
)
```

UI affordances:

- Resolution dropdown with the four valid values
- Winner picker (required for `superseded`), choosing between the edge's `from_id` and `to_id`
- An optional free-form reason field is a UI-only annotation and is not persisted (`lithos_conflict_resolve` accepts no reason parameter); persisted resolution notes are deferred
- Side-by-side inspection of the endpoints before resolving (full comparison view is deferred — §12; a two-pane read is sufficient here)

On success Lithos emits `edge.upserted` carrying the new `conflict_state`, so the graph view redraws the edge with its resolution badge through the normal freshness path (§8.5). Unresolved contradictions surface via the graph toolbar counter and a banner on affected note pages. Error envelope handling per §5C.4 conventions (`invalid_input`, `not_found`, `update_failed`).

---

## 12. Deferred Surfaces — Comparison & Reading Paths

*Deferred pool (see ROADMAP). Requirements preserved in condensed form so they can re-enter the sequence without re-design.*

- **Note comparison:** place 2–N notes side-by-side (cap configurable) with three tabs — metadata (shared values highlighted), content (collapsed excerpts via `lithos_read`), and LLM-generated themes & shared-concepts (hidden when LLM disabled). Entry points: feed multi-select, graph multi-select, and the conflict panel's "compare endpoints". Read-only.
- **Reading paths:** an ordered traversal over a note subset — modes `salience` (via `lithos_node_stats`), `chronological`, `edge-traversal` (BFS over `builds_on`/`derived_from`), and `llm` (pedagogical ordering; LLM-gated). Output is a shareable ordered page; saved paths persist as a Lens-authored note with a structured block in the body.
- **Semantic projection** (UMAP/t-SNE layouts) remains blocked on Lithos exposing embeddings via MCP.

---

# Part D — Reference

Cross-cutting concerns and reference tables. Milestone sequencing lives in [`docs/ROADMAP.md`](./ROADMAP.md).

---

## 13. Settings View

Read-only. Displays:

- Lithos connection state: URL, MCP session status, detected capability set (`graph_available` — whether the connected Lithos serves the task-graph tools), SSE subscription state and last successful event time
- Effective Tasks-view tuning (`frontier_limit`, attention thresholds, `project_convention`, debounce, drawer size) with deprecation notices for any parsed-and-ignored legacy knobs (§4.4)
- Graph page settings (`[graph]`) and Knowledge settings (`[knowledge]`)
- **Writes posture:** whether `[writes]` is enabled, the current operator identity, and — when enabled — the explicit trusted-network security-boundary statement from §5C.1
- LLM flags (enabled, provider, model, complexity default) — values only, never API keys
- Influx profile/threshold/model display parsed from `/etc/influx/config.toml` — **only when the optional mount exists** (§3); the section is hidden otherwise

Editing happens via TOML/env outside the container. Lens does not write to config.

---

## 14. Resilience & Error Handling

| Failure | Behaviour |
|---------|-----------|
| Lithos unreachable | Banner "Lithos is offline"; degraded panels; boot survives (§4.1); write affordances disabled; retry transparently |
| **Lithos < 0.4 (frontier tools missing)** | Detect via tool-not-found on `lithos_task_ready`; set `graph_available=false`; render the legacy flat open/completed/cancelled list with a **"graph features need Lithos ≥ 0.4"** notice. No graph sections, no graph pages, no write actions that depend on graph context. |
| `lithos_task_ready` / `lithos_task_blocked` errors (transient) | Dashboard renders the master open list flat with a warning banner; no silent classification |
| `lithos_task_children` errors | Epic chip renders without a progress fraction; tooltip explains |
| `lithos_task_get` → `task_not_found` | Not-found panel with a link back to `/tasks` (never HTTP 500) |
| `lithos_task_edge_list` fan-out partial failure (graph page) | Render the partial graph with a "N tasks could not be fetched" banner |
| Write tool errors | Mapped per §5C.4; network failure mid-write renders "the action may or may not have applied — refresh to see current state" (writes are not idempotent) |
| Expired claims | Invisible by design (§5.1) — claim lists are labelled "active claims"; Lens never renders an expired/released inference |
| `lithos_retrieve` errors | Silent fallback to `lithos_search` with a "fast search" badge (§7.2) |
| `lithos_node_stats` → `doc_not_found` | Render default salience/count values |
| Wiki-link resolver finds zero/multiple candidates | Unresolved page / disambiguation page (§6.3) — never a 500 |
| Markdown rendering failure | HTML-escaped plaintext fallback (§6.2) |
| `lithos_read` for a finding link fails | "View document" fallback label; one warning per panel |
| `lithos_finding_list` fails | Detail renders metadata sections + findings retry affordance |
| Watcher note events without `id` | Debounced refetch fallback (§8.5) |
| Graph caps exceeded | "Refine your filters" banner + truncated sample (task graph: refuse render over `[graph].max_tasks`) |
| `lithos_note_update` → `version_conflict` | Re-read, retry once, then surface (§10.2) |
| Feedback / conflict write fails | Toast error with retry; never silently dropped |
| LLM disabled / provider error | LLM-gated surfaces hidden / per-feature fallback with non-blocking toast; misconfiguration at startup logs and effectively disables LLM |
| SSE disconnect | Paused badge; polling fallback; on reconnect: `Last-Event-ID` replay + one full refresh (§5.8.3) |
| SSE unsupported by Lithos build | Disable SSE after initial failure; polling only; state surfaced in settings |
| Archive file missing (optional mount) | Link renders; `/archive/...` 404 shows a placeholder |

---

## 15. Observability

### OTEL — Opt-In, Additive

Same pattern as Lithos and Influx: `LITHOS_LENS_OTEL_ENABLED=true` enables it; optional packages via `uv sync --extra otel`; `LITHOS_LENS_OTEL_CONSOLE_FALLBACK=true` prints spans to stdout.

**Key spans:**

| Span | Description |
|------|-------------|
| `lens.request` | Each HTTP request |
| `lens.tasks.list` | Dashboard assembly (five-call fan-out); attributes: per-section counts, truncation flag |
| `lens.tasks.frontier_join` | The pure join/classification pass; attributes: join duration, unclassified count |
| `lens.tasks.detail` | Task detail fetch; attributes: blocker/children counts |
| `lens.tasks.blockers` | Lazy blocker-chain expansion fragment |
| `lens.tasks.graph` | Task graph page assembly; attributes: scope, node/edge counts, cache hit |
| `lens.tasks.findings` / `lens.tasks.findings_recent` | Findings timeline / drawer + warm-up |
| `lens.tasks.event` | Single SSE event handled (attribute `event.type`) |
| `lens.tasks.refresh` | Manual / polling-fallback refresh |
| `lens.tasks.metrics_recompute` | Debounced recompute (attribute `trigger=sse\|manual\|reconnect\|warmup`) |
| `lens.tasks.plan` / `.projects` / `.throughput` | Planning View computations |
| `lens.tasks.project_convention_conflict` | Metadata-vs-tag disagreement warning (§5B.1) |
| `lens.writes.<action>` | One span per write attempt (`complete`, `reopen`, `cancel`, `create`, `edge_upsert`); attributes: operator, result code |
| `lens.events.connect` | SSE connection lifecycle |
| `lens.knowledge.note` / `.resolve` / `.search` / `.related` | Note page render / wiki-link resolution / search / related panel |
| `lens.graph.knowledge` / `lens.graph.centrality` | Knowledge graph assembly / centrality overlay |
| `lens.retrieve` | Cognitive search call |
| `lens.llm.*` | LLM calls (curation, synthesis) |
| `lens.feedback.write` | Feedback write |
| `lens.archive.serve` | Archive file serve (optional mount) |

### Logging

- stdout only; structured JSON via `python-json-logger`; `LITHOS_LENS_LOG_LEVEL` controls verbosity
- Every write attempt additionally emits the structured audit line from §5C.6

### Health Endpoint

```
GET /health → {
  "status": "ok",
  "lithos": "ok" | "degraded" | "unreachable",
  "events": "live" | "reconnecting" | "disabled",
  "llm": "disabled" | "ok" | "error"
}
```

The `lithos` status derives from a cached `lithos_stats()` probe refreshed every 30 seconds; `events` reports the SSE subscription state; `llm` reports provider reachability when enabled.

---

## 16. API Reference

### 16.1 Lithos MCP API — Lens Usage

**Reads:**

| Tool | Key args used by Lens | Purpose |
|------|----------------------|---------|
| `lithos_task_list` | `status`, `tags`, `agent`, `since`, `resolved_since`, `with_claims`, `metadata_match` | Master open set (with inline claims); resolved windows |
| `lithos_task_get(task_id)` | — | Single-task fetch with explicit `task_not_found` envelope (detail pages, ghost nodes, pre-write checks) |
| `lithos_task_status(task_id)` | — | Full record **with active claims** (detail refresh on claim events) |
| `lithos_task_ready` | `limit`, `with_claims`, (`project`, `tags`) | The feasible frontier — Ready section; never re-derived in Lens |
| `lithos_task_blocked` | `limit`, (`project`, `tags`) | Blocked tasks with structured `blockers[]` (`kind`: `task` / `gate` / `blocker_unsatisfiable` / `cycle`) |
| `lithos_task_children(task_id)` | `recursive`, `include_closed` | Epic rollups; children tables; epic-scope node sets |
| `lithos_task_edge_list(task_id)` | `direction`, `types` | Edges touching a task: `{from_task_id, to_task_id, type, direction, metadata, created_by, created_at}`; graph assembly, gate waiters, provenance |
| `lithos_finding_list(task_id, since?)` | — | Findings timeline; rolling-buffer warm-up. **Requires `task_id`** — no reverse (`knowledge_id`) lookup exists (§6.7) |
| `lithos_stats()` | — | Health probe; summary signals |
| `lithos_agent_list` | `type`, `active_since` | Agent filter dropdown |
| `lithos_tags(prefix?)` | — | Project universe (tag convention); tag filters |
| `lithos_read` | `id` \| `path`, `max_length`, `agent_id` | Note pages; cheap title/metadata fetches (`max_length=1` still returns complete frontmatter metadata); wiki-link path probe. Response carries **no top-level `path`**; `links` entries are unresolved `{target, display}`. |
| `lithos_search(query)` | `mode="hybrid"`, `limit`, `tags`, `path_prefix` | `/knowledge` search; retrieve fallback. **Snippets contain raw markdown — render escaped** (§7.1) |
| `lithos_list` | `path_prefix`, `tags`, `since`, `limit`, `offset`, `title_contains` | Recently-updated lists; feed; wiki-link disambiguation |
| `lithos_related(id)` | `include`, `depth`, `namespace` | Related panel; knowledge ego-graphs |
| `lithos_retrieve(query)` | `limit`, `tags`, `path_prefix`, `namespace_filter`, `agent_id` — **never `task_id`** (§7.2) | Cognitive search |
| `lithos_edge_list` | `from_id`, `to_id`, `type`, `namespace` | Knowledge graph edges; conflict listing |
| `lithos_node_stats(node_id)` | — | Salience/retrieval stats panel; node sizing |

**Writes (curated set — §5C):**

| Tool | Key args | Lens action |
|------|----------|-------------|
| `lithos_task_complete(task_id, agent)` | `outcome?` | Used by Lens **only to approve human gates** (the `/approve` route rejects non-gates, §5C.7); surfaces returned `unblocked[]` |
| `lithos_task_cancel(task_id, agent)` | `reason?` *(event-only, not persisted)* | Consequence-aware cancel |
| `lithos_task_reopen(task_id, agent)` | — | Reopen; surfaces returned `reblocked[]` |
| `lithos_task_create(title, agent)` | `description?`, `tags?`, `metadata?`, `task_type?`, `depends_on?`, `parent_task_id?` | Create task / epic / gate |
| `lithos_task_edge_upsert(from_task_id, to_task_id, type, agent)` | `metadata?` | Add dependency edge; **emits no upstream event**; **no delete counterpart exists** |
| `lithos_agent_register(id)` | `name?`, `type?` | Service-agent registration at boot (`type="web-ui"`); operator registration on first write (`type="human"`) |

**Later-milestone writes (Part C):**

| Tool | Purpose |
|------|---------|
| `lithos_note_update(id, agent, title?, tags?, status?, metadata?, expected_version?)` | Feedback tag patches (§10) — never round-trips the note body |
| `lithos_conflict_resolve(edge_id, resolution, resolver, winner_id?)` | Conflict resolution (§11); `winner_id` required for `superseded` |

#### 16.1.1 SSE event reference

Lens consumes the Lithos SSE stream at `${LITHOS_LENS_LITHOS_URL}${LITHOS_LENS_SSE_EVENTS_PATH}` with a server-side `types=` filter, and replays via `Last-Event-ID` on reconnect (bounded by Lithos's in-memory ring buffer — hence the full-refresh backstop, §5.8.3).

| Event | Payload | Consumer / gotchas |
|-------|---------|--------------------|
| `task.created` | `task_id`, `title` | Insert skeleton row; reconcile on debounced refresh |
| `task.claimed` / `task.released` | `task_id`, `agent`, `aspect` | Claim chips; In-progress membership |
| `task.completed` | `task_id`, `agent`, `outcome`, `cited_nodes`, `misleading_nodes`, `receipt_id` | Section moves; **`cited_nodes` / `misleading_nodes` / `receipt_id` arrive as JSON-encoded strings** (e.g. `"[\"node-1\"]"`, `"null"`) — decode defensively; `outcome` is a plain string or null |
| `task.cancelled` | `task_id`, `agent`, `reason` | Section moves; **`reason` exists only here — it is not persisted** and will not survive a reload |
| `task.updated` | `task_id` **only** | Always `requires_refresh=true` — the payload cannot drive a UI patch |
| `task.reopened` | `task_id`, `agent`, `prior_status`, `prior_outcome` | Moves rows back out of Completed/Cancelled; `reopened` marker |
| `finding.posted` | `finding_id`, `task_id`, `agent` | Rolling buffer; latest-finding line; open-timeline refresh. **No `knowledge_id`** — gates §6.7 |
| `agent.registered` | `agent_id`, `name` | System-scoped (§5.8.2): forwarded with `task_id=""`, `requires_refresh=false`; refreshes agent dropdown data only |
| `note.created` / `note.updated` / `note.deleted` | `id`, `title`, `path` (tool paths); **watcher-emitted events may carry only `path`, no `id`** | Knowledge graph/search freshness; debounced-refetch fallback for id-less events |
| `note.renamed` | `id`, `src_path`, `dest_path` | Knowledge freshness |
| `edge.upserted` | `edge_id`, `from_id`, `to_id`, `type`, (`namespace` \| `conflict_state`) | **Knowledge-graph event only** — note UUIDs, not task ids. There is **no task-edge event**; do not route this to task surfaces |

Additional payload notes: task events carry empty `tags`, so upstream `?tags=` filtering cannot scope task streams by project — project scoping is client-side in Lens.

**Synthetic internal events (`lens.*` — reserved namespace, never sent upstream):**

| Event | Emitted when |
|-------|--------------|
| `lens.refresh` | On every SSE reconnect, as the correctness backstop |
| `lens.edge_upserted` | After Lens's own successful `lithos_task_edge_upsert` (no upstream event exists) so all tabs converge |

### 16.2 Lens Internal HTTP Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Default view (`ui.default_view`, defaults to `tasks`) |
| `GET /tasks` | Operator View dashboard (`?selected=` opens the side panel; `?epic=` scopes) |
| `GET /tasks/{task_id}` | Full-page task detail |
| `GET /tasks/{task_id}/findings` | Findings timeline fragment |
| `GET /tasks/{task_id}/blockers` | Lazy blocker-chain expansion fragment |
| `GET /tasks/findings/recent` | Recent-findings drawer fragment |
| `GET /tasks/graph` | Task dependency graph page (`?project=` \| `?epic=`) |
| `GET /tasks/plan` (+ `/projects`, `/throughput` fragments) | Planning View |
| `GET /tasks/events` | SSE re-broadcast to browser tabs |
| `POST /tasks/{task_id}/approve` (gate-only) \| `/reopen` \| `/cancel` (+ `GET …/cancel` confirm), `GET/POST /tasks/new`, `POST /tasks/{task_id}/edges` | Curated writes (§5C.7) — **registered only when `[writes] enabled = true`** |
| `GET /note/{id}` | Note View (§6) |
| `GET /knowledge` | Search / recently-updated landing (`?q=`, `?tag=`) |
| `GET /knowledge/resolve` | Wiki-link resolver (`?target=`, `?from=`) |
| `GET /knowledge/graph` | Knowledge graph (`?focus=` for ego mode) |
| `POST /api/feedback` | Feedback write (§10; write-gated) |
| `POST /api/conflict/resolve` | Conflict resolution (§11; write-gated) |
| `POST /api/tasks/findings/curate` | LLM findings curation (`llm.enabled` only) |
| `GET /settings` | Read-only settings view |
| `GET /archive/{path}` | Stream archived files (optional mount only) |
| `GET /health` | Health probe |

---

## Appendix A — Key Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` / `uvicorn` | Web framework / ASGI server |
| `httpx` + `httpx-sse` (or equivalent) | Lithos MCP transport and SSE event consumption |
| `jinja2` | HTML templating |
| `pydantic` | Request/response validation |
| `markdown-it-py` | Server-side markdown rendering, safe-by-default (§6.2) |
| `python-json-logger` | Structured JSON logging |
| `opentelemetry-*` | OTEL (optional extra: `uv sync --extra otel`) |
| `litellm` | Provider-agnostic LLM calls (optional extra: `uv sync --extra llm`) |
| Cytoscape.js *(vendored static asset)* | Graph visualisation (task DAG, knowledge graph, mini-graphs); client-side centrality |
| HTMX + SSE extension *(vendored static assets)* | Dynamic HTML; live tile/row updates |
| App CSS *(vendored)* | Styling without a build step or CDN dependency |

> [!note] Module layout
> The authoritative module/package layout lives in [`docs/SPECIFICATION.md`](./SPECIFICATION.md) and is enforced by the `docs/architecture.toml` guardrail tests; this document intentionally does not duplicate it.

> [!note] Frontend asset recommendation
> For a local-first operational tool, production builds serve pinned, vendored JS/CSS from `static/` rather than CDNs — usable offline, no third-party runtime dependency, reproducible upgrades. Tailwind's CDN mode is prototype-only; use explicit app CSS or a checked-in precompiled bundle.
