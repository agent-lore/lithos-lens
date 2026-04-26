---
title: Lithos Lens — Requirements Document
version: 0.6.0
date: 2026-04-26
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
> v0.6 promotes the document into four parts (Common Core, Tasks View, Knowledge Browser, Reference) and adds the **Tasks View** as a peer of the Knowledge Browser. The implementation order is **Tasks View first, Knowledge Browser second** — both ride on the same FastAPI app, MCP client, and shared SSE event subscription, so the common core (§1–§4) is built once and both views slot in. The MCP tool surface grows by `lithos_task_list`, `lithos_task_status`, `lithos_finding_list`, and access to the Lithos SSE event stream; no Lithos API changes are required for the Tasks view MVP.

---

## Table of Contents

### Part A — Common Core
- [[#1. Goals & Non-Goals]]
- [[#2. Architecture Overview]]
- [[#3. Infrastructure & Deployment]]
- [[#4. Configuration]]

### Part B — Tasks View
- [[#5. Tasks View]]

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
- Live, read-only dashboard over Lithos tasks, claims, and findings
- Flexible filtering (status, tags including `key:value` shorthand, creating agent, date range)
- Summary panel showing aggregate counts (open / claimed / completed today / completed this week / cancelled)
- Task detail panel exposing full metadata, active claims, and the findings timeline
- Findings link out to the Lens Knowledge Browser via explicit `finding.knowledge_id` (no inference, no heuristics)
- Auto-update via the shared SSE event subscription, with a configurable manual-refresh fallback

#### Knowledge Browser
- Feed view: time-ordered cards filterable by profile, date, tag, score, source
- Interactive graph view with Cytoscape.js, rendering LCMA typed edges
- Cognitive search bar using `lithos_retrieve` (seven-scout PTS retrieval with reranking)
- Feedback controls — mark items as relevant / not relevant; write back to Lithos
- Conflict resolution UI for LCMA `contradicts` edges
- Multi-note **comparison** view (metadata + content; LLM-driven theme and concept analysis when LLM is enabled)
- Curated **reading paths** through a node subset — algorithmic by default, LLM-curated when LLM is enabled
- Graph **centrality overlay** to highlight bridge nodes between clusters

### Non-Goals

- Editing note content inline (that's Obsidian's job — or direct MCP tools)
- Running its own ingestion — Lens never writes new notes from scratch
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
> **Lithos Lens has zero runtime dependency on the Influx ingestion container.** It is a pure Lithos MCP client. All data — paper notes, run history, feedback, graph edges, tasks, claims, findings — comes from Lithos. The UI and ingestion pipeline can be restarted, updated, or fail independently. Lens does mount the `influx-archive` volume read-only so it can serve archived PDFs/HTMLs directly, but that is a file-system dependency on a shared volume, not a runtime dependency on the Influx process.

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
| Styling | Tailwind CSS via CDN | No build step |
| Config format | TOML (read-only, shared with Influx) | Consistent with Influx and Lithos conventions |
| OTEL | Opt-in, additive, optional packages | Consistent with Lithos conventions |
| Environments | `.env.dev` / `.env.prod` | Consistent with Lithos conventions |
| LLM access | Optional, env-gated client (`LENS_LLM_*`); provider-agnostic wrapper | Lithos MVP 1 does not provide synthesis, comparison, or curation tools; Lens needs LLM access for "most significant findings", Q&A synthesis, comparison themes, and complexity-adjusted output. When Lithos ships MVP-3 synthesis, Lens prefers the MCP path. |
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
| `app/llm_client.py` *(optional)* | Provider-agnostic LLM wrapper (anthropic / openai / ollama); only loaded when `LENS_LLM_ENABLED=true` |
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

# Tasks view
LENS_TASKS_AUTO_REFRESH_INTERVAL_S=30     # manual fallback when SSE disconnects
LENS_TASKS_VISIBLE_CAP=200                # cap on rows that fetch claims inline
LENS_TASKS_DEFAULT_TIME_RANGE_DAYS=30     # completed-task hide-by-default window

# Optional LLM client — disabled by default
LENS_LLM_ENABLED=false
# LENS_LLM_PROVIDER=anthropic             # anthropic | openai | ollama
# LENS_LLM_MODEL=claude-haiku-4-5-20251001
# LENS_LLM_API_KEY=                       # provider-dependent; not needed for ollama
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
LENS_TASKS_VISIBLE_CAP=200
LENS_TASKS_DEFAULT_TIME_RANGE_DAYS=30

LENS_LLM_ENABLED=false
# LENS_LLM_PROVIDER=anthropic
# LENS_LLM_MODEL=claude-haiku-4-5-20251001
# LENS_LLM_API_KEY=
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
      - LENS_TASKS_VISIBLE_CAP=${LENS_TASKS_VISIBLE_CAP:-200}
      - LENS_TASKS_DEFAULT_TIME_RANGE_DAYS=${LENS_TASKS_DEFAULT_TIME_RANGE_DAYS:-30}
      - LENS_LLM_ENABLED=${LENS_LLM_ENABLED:-false}
      - LENS_LLM_PROVIDER=${LENS_LLM_PROVIDER:-}
      - LENS_LLM_MODEL=${LENS_LLM_MODEL:-}
      - LENS_LLM_API_KEY=${LENS_LLM_API_KEY:-}
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
auto_refresh_interval_s = 30    # manual refresh fallback when SSE disconnects
visible_cap = 200               # rows for which lithos_task_status is fetched inline
default_time_range_days = 30    # completed tasks older than this hidden by default
findings_page_size = 25         # findings timeline pagination
default_status_groups = ["open", "completed", "cancelled"]  # display order

[events]
sse_path = "/events"            # Lithos SSE event stream path (overridden by env)
reconnect_backoff_ms = [500, 1000, 2000, 5000, 10000]  # exponential backoff schedule

[llm]
enabled = false                 # overridden by LENS_LLM_ENABLED
provider = "anthropic"          # anthropic | openai | ollama
model = "claude-haiku-4-5-20251001"
default_complexity = 3          # 1=beginner … 5=expert; per-session override allowed
max_tokens = 2048
synthesis_prefer_mcp = true     # use lithos_synthesize when available, else local LLM
findings_curation_enabled = true  # enables "most significant findings" view in tasks

[telemetry]
enabled = false                 # overridden by LENS_OTEL_ENABLED
console_fallback = false
service_name = "lithos-lens"
export_interval_ms = 30000
```

---

# Part B — Tasks View

The Tasks View is the first user-facing view delivered. It is a read-only dashboard over the Lithos coordination layer (tasks, claims, findings). It lives at `/tasks` (and at `/` until the Knowledge Browser ships) and is reachable from the top-nav view switcher. It consumes only existing MCP tools and the Lithos SSE event stream — no new Lithos APIs are required for MVP.

---

## 5. Tasks View

### 5.1 Purpose & Scope

The Tasks View gives users and operators a live picture of:
- What work is in flight across all agents
- What has completed (and how recently)
- What is waiting to be claimed
- Which findings have been posted against each task, and where to follow them in the Knowledge Browser

> [!warning] Strictly read-only
> The view does **not** create, mutate, or claim tasks. Any agent that needs to manage tasks does so via the Lithos MCP API directly. This boundary is structural, not just stylistic — Lens does not import any task-mutation tool.

The view consumes:
- `lithos_task_list(filter…)` — primary list query
- `lithos_task_status(task_id)` — fetched per visible row up to `tasks.visible_cap` to render inline claim badges
- `lithos_finding_list(task_id)` — fetched on demand when the detail panel opens
- `lithos_read(id)` — used to resolve `finding.knowledge_id` UUIDs to note titles for the link label
- `lithos_stats()` — feeds the summary panel
- `lithos_agent_register(...)` listing — sources the "creating agent" filter dropdown
- Lithos SSE event stream — drives auto-update (see §5.6)

### 5.2 Summary Panel

A row of tiles at the top of the view:

| Tile | Source |
|------|--------|
| Open | `lithos_task_list(status="open")` count |
| Claimed (any aspect) | `lithos_task_list(status="open", has_claims=true)` count |
| Completed today | `lithos_task_list(status="completed", since=<00:00 today>)` count |
| Completed this week | `lithos_task_list(status="completed", since=<start of ISO week>)` count |
| Cancelled | `lithos_task_list(status="cancelled")` count |

Tiles update on page load, on manual refresh, and on every relevant SSE event. Clicking a tile applies the corresponding filter to the task list below.

### 5.3 Task List

#### 5.3.1 Default ordering and grouping

Tasks display in three groups, each sorted by `created_at` desc:
1. **Open** (subdivided so claimed tasks render with a visible claim indicator)
2. **Completed**
3. **Cancelled**

The `tasks.default_status_groups` config controls which groups are visible and in what order.

#### 5.3.2 Row content

| Column | Notes |
|--------|-------|
| Title | Truncated to one line; full title in tooltip |
| Status badge | Open / claimed / completed / cancelled |
| Creating agent | Sourced from `task.agent`; clickable filter |
| Tags | Chips; `key:value` shorthand renders as `project: influx` |
| Created at | Relative time with absolute on hover |
| Claims indicator *(open tasks only)* | Compact list of `aspect → claiming agent`, fetched via `lithos_task_status` per row up to `tasks.visible_cap` |
| Outcome *(completed only)* | One-line summary; full text in detail panel |
| Duration *(completed only)* | Human-readable from `created_at` → `completed_at` |

#### 5.3.3 Filters

Filters appear in a sticky filter bar above the list:

| Filter | Behaviour |
|--------|-----------|
| Status | Multi-select: open / claimed / completed / cancelled |
| Tag | Free-text input with `key:value` parsing — typing `project:influx` filters tasks tagged `project:influx`; chips for active tag filters |
| Creating agent | Dropdown sourced from Lithos's registered-agent list (via `lithos_agent_register` listing or equivalent introspection); free-text fallback if the listing is unavailable |
| Date range | Created-at and / or completed-at; defaults to "last `tasks.default_time_range_days` days". Completed tasks older than this are hidden by default; the user can broaden the range to surface them |

All filters compose. Filter state is reflected in the URL (`?status=open&tag=project:influx`) for shareability.

#### 5.3.4 Visible cap and degradation

The inline claim indicator requires one `lithos_task_status` call per row. Lens batches these in parallel and caps the work at `tasks.visible_cap` (default 200). Beyond the cap:
- Rows past the cap render without the inline claim indicator
- A footer banner reads "Showing claim detail for the first 200 of N rows — narrow your filters or click a row to see claims for the rest"
- Clicking a row past the cap fetches that row's `lithos_task_status` lazily

A future Lithos enhancement to embed active claims in `lithos_task_list` would eliminate this cap; the requirement is flagged but not blocking.

### 5.4 Task Detail Panel

Clicking a row opens a side or bottom panel showing the full task. Sections:

| Section | Content |
|---------|---------|
| Header | Title, status, creating agent, `created_at`, `completed_at`, duration |
| Tags | Full tag list, one chip per tag |
| Description | Markdown-rendered |
| Metadata | `metadata` dict rendered as a key-value table |
| Outcome *(completed only)* | Markdown-rendered |
| Active claims | List of `aspect / claiming agent / claimed_at / expires_at / time remaining`; refreshed on SSE claim events |
| Findings | See §5.5 |

The panel is dismissable; closing it clears the URL fragment so the list state is preserved.

### 5.5 Findings Timeline

`lithos_finding_list(task_id)` is called once when the detail panel opens, and again on every relevant SSE event for the open task.

#### 5.5.1 Default rendering

Findings render chronologically (oldest → newest). Each entry shows:
- Posting agent
- Timestamp (relative + absolute on hover)
- Summary text
- **Knowledge link** *(only when `finding.knowledge_id` is non-null)* — a clickable label that opens the corresponding note in the Knowledge Browser at `/note/{knowledge_id}`. The label is the note title, resolved by a single `lithos_read(id=knowledge_id)` per finding. If the read fails, the label falls back to "View document" with a non-blocking warning toast. *Until the Knowledge Browser ships, the link target falls back to a minimal `/note/{knowledge_id}` route that renders the note's title, content and tags as plain Markdown — enough to dereference the finding without the full browser experience.*

#### 5.5.2 Pagination

Findings are paginated at `tasks.findings_page_size` per page (default 25), oldest-first, with a "Show more" control. For long-running tasks this prevents runaway DOM cost on detail-panel open.

#### 5.5.3 Most-significant findings *(LLM, optional)*

When `llm.enabled = true` **and** `llm.findings_curation_enabled = true`, the timeline header shows a toggle: **All findings** / **Most significant**. The "Most significant" mode passes the full findings list (summaries + agents + timestamps) to the LLM with a prompt that returns the K findings with the largest signal (typically completion announcements, decisions, surprises, contradictions), each with a one-line rationale. The complexity slider (§8.5) modulates verbosity. With LLM disabled the toggle is hidden.

### 5.6 SSE Auto-Update

#### 5.6.1 Connection model

A single SSE connection is held by the shared `app/events.py` utility. The Tasks-view router subscribes to **all** task-related event types (`task.created`, `task.claimed`, `task.released`, `task.completed`, `task.cancelled`, `finding.posted`, plus any future task-related events). Filtering happens client-side in Lens: events that don't match the current filter state are ignored for list updates but always counted into the summary panel.

#### 5.6.2 UI behaviour on event

| Event | UI effect |
|-------|-----------|
| `task.created` matching filters | Insert row at top of Open group; bump Open tile |
| `task.claimed` | Update the row's claim indicator; if detail panel open for that task, refresh Active claims section |
| `task.released` | Remove that aspect from the row's claim indicator |
| `task.completed` | Move row to Completed group with animation; bump Completed-today + Completed-this-week tiles |
| `task.cancelled` | Move row to Cancelled group; bump Cancelled tile |
| `finding.posted` for an open detail panel | Append to findings timeline; refetch only the new finding via the event payload's `finding_id` if present, else refetch the list |
| `finding.posted` not for the open panel | Increment a small "+N findings" badge on the row |

Pushed updates are HTMX OOB swaps for the affected fragments — no full-page reload. Adds OTEL spans `lens.tasks.event` (per event handled) and `lens.tasks.refresh` (per manual / fallback refresh).

#### 5.6.3 Reconnection

On SSE disconnect Lens uses the exponential backoff schedule in `events.reconnect_backoff_ms`. While disconnected, the manual-refresh fallback runs every `tasks.auto_refresh_interval_s` seconds and a "Live updates paused — reconnecting" badge is visible in the header. On successful reconnect the badge clears and the tasks list is fully reloaded once.

### 5.7 Cross-View Linking

#### 5.7.1 Tasks → Knowledge (MVP)

Findings with a non-null `knowledge_id` link directly to `/note/{knowledge_id}`. The browser opens with the note pre-selected. Until the Knowledge Browser ships, `/note/{knowledge_id}` renders a minimal Markdown view (title, tags, content) so finding links remain useful from day one. Once the Knowledge Browser is delivered, the same URL renders the full feed-detail panel — finding links require no change.

This is a straight UUID passthrough — no inference, no text matching, no schema change.

#### 5.7.2 Knowledge → Tasks (deferred)

Notes whose frontmatter records a producing task (`metadata.produced_by_task_id` or equivalent) should display a "Produced by task X" badge in the feed view (§6.3) and graph view, linking back to `/tasks/{task_id}`. This is deferred — it depends on a frontmatter convention that Lithos producers must adopt — but the data path is straightforward when ready.

### 5.8 API

| Endpoint | Purpose |
|----------|---------|
| `GET /tasks` | Server-rendered tasks dashboard (summary + list + filter bar) |
| `GET /tasks/{task_id}` | Detail panel HTMX fragment |
| `GET /tasks/{task_id}/findings` | Findings timeline HTMX fragment, paginated |
| `GET /tasks/events` | Server-Sent-Events endpoint Lens exposes to its own browser tab — re-broadcasts events received from Lithos to the open page |
| `POST /api/tasks/findings/curate` | LLM-curated "most significant findings" endpoint, only when `llm.enabled` |

No `POST` / `PUT` / `DELETE` endpoints touch task state — the read-only contract is enforced at the router level.

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
    path_prefix=f"papers/{profile}" if profile else "papers/",
    tags=selected_tags or None,
    since=selected_since or None,
    limit=config.ui.feed_page_size,
    offset=page_offset,
)
```

### 6.3 UI Elements

| Element | Behaviour |
|---------|-----------|
| Filter bar | Profile dropdown, date range, tag chips, score slider |
| Paper card | Title, authors, source, score, tags, collapsed abstract |
| Expandable abstract | HTMX swap loads full frontmatter content from `lithos_read(id=...)` |
| Archive link | If `local_file` is set in frontmatter, link opens via `/archive/...` (the read-only mount) |
| Source link | Opens `source_url` externally |
| 👍 / 👎 buttons | Write back via `lithos_write` (see §9) |
| Multi-select toggle | Enables comparison mode (see §10); shows checkbox on each card and a floating "Compare N notes" action |
| "Related" teaser | Calls `lithos_related(id=..., include=["edges", "links"], depth=1)` |
| "Open in graph" link | Jumps to graph view centred on this note's id |
| "Generate path" action | Opens reading-path picker (see §11) seeded from the current filter set or the selected card |
| "Produced by" badge *(deferred)* | When a note's frontmatter points at a task, link back to §5 — see §5.7 |

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

- **Nodes**: papers sized by `stats.salience` (or relevance_score fallback), coloured by profile tag (e.g. `profile:ai-robotics` → one hue, `profile:hema` → another)
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
- Filter panel: profile, date, tag, score, edge type, namespace
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

### 8.5 Complexity Slider (LLM, optional)

A session-scoped slider (1 = beginner … 5 = expert), default = `llm.default_complexity`, is exposed in the search bar and in any LLM-augmented panel (synthesis result, comparison themes tab, LLM-curated paths, "most significant findings" in the tasks view). The selected value is persisted via cookie or query param and injected into every LLM prompt as a system instruction modulating verbosity and technicality. The slider has no effect when `llm.enabled = false` and is hidden entirely in that mode.

Rationale: Paperlens demonstrated this is a high-value, low-cost UX lever — the same retrieved evidence yields visibly different explanations for different audiences without changing the underlying data.

---

## 9. Feedback Mechanism

### 9.1 Overview

Lens is the only place in the system where humans generate feedback. Feedback is stored as additional tags on the existing Lithos note (specifically `influx:rejected` and optionally `influx:accepted` for positive reinforcement).

### 9.2 Writing Feedback — Critical Contract

> [!warning] `lithos_write` requires `title` and `content` on every call
> Even when only updating tags on an existing note, `lithos_write` requires `title`, `content`, and `agent` at the MCP boundary. Lens must first `lithos_read(id=...)` the note and pass the existing title+content back through. Omitted optional fields preserve existing values, but `title`/`content`/`agent` are never optional.

```python
existing = lithos_read(id=note_id)
existing_tags = existing["metadata"].get("tags", [])
new_tags = [t for t in existing_tags if t != "influx:rejected"]
if not accepted:
    new_tags.append("influx:rejected")

lithos_write(
    id=note_id,
    title=existing["title"],
    content=existing["content"],
    agent="lithos-lens",
    tags=new_tags,
    confidence=0.0 if not accepted else existing["metadata"].get("confidence", 1.0),
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
  "verdict": "accepted" | "rejected",
  "note": "<optional user comment>"
}
```

Response: `{"status": "ok"}` on success, `{"status": "error", "message": "..."}` on failure. Errors are shown in the UI — never silently swallowed.

### 9.5 Downstream Effect on Influx

Influx picks up feedback at the start of each run by calling `lithos_list(tags=["influx:rejected", f"profile:{name}"])` — see Influx requirements §12. Lens does not have to notify Influx directly.

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

- **Metadata** — titles, authors, source, ingestion date, profile, tags, score, ids. Shared values (e.g. co-authors, overlapping tags) are highlighted. No LLM required.
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

The user can save a path. Saved paths persist as a Lithos note under `path: "lens/paths/<slug>"` via `lithos_write`, with the seed, mode, filter set, and ordered ids in the frontmatter — making them durable, searchable, and shareable across Lithos clients.

### 11.5 API

```
POST /api/path
{
  "seed_id": "<uuid> | null",
  "filter": { "profile": "...", "tags": [...], "since": "..." },
  "mode": "salience | chronological | edge | llm",
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
- Free-form reason field (stored in an attached note if provided)
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
- Last Influx run info (pulled from Lithos notes at `path: "influx/runs"`)

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
| `lens.tasks.detail` | Task detail panel fetch (`lithos_task_status` + first `lithos_finding_list` page) |
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
| `lithos_task_list(status?, tags?, agent?, since?, until?, limit?, offset?)` | none | Tasks view primary list query |
| `lithos_task_status(task_id)` | `task_id` | Active claims for a task; called per visible row up to `tasks.visible_cap`, and on demand for the detail panel |
| `lithos_finding_list(task_id, limit?, offset?)` | `task_id` | Findings timeline in the task detail panel |
| `lithos_stats()` | none | Health endpoint status probe; tasks-summary tile counts |
| `lithos_agent_register(id, name?, type?)` *(listing form)* | `id` | Optional startup registration; the agent listing also drives the Tasks-view "creating agent" filter dropdown |
| `lithos_list(path_prefix?, tags?, since?, limit?, offset?)` | none | Feed view paper listing |
| `lithos_read(id)` | `id` | Paper detail; feedback writes; resolving `finding.knowledge_id` to a title |
| `lithos_retrieve(query, limit?, agent_id?, tags?)` | `query` | Cognitive search bar |
| `lithos_search(query, mode?, tags?, ...)` | `query` | Fallback search when `retrieve` errors |
| `lithos_edge_list(namespace?, type?)` | none | Graph edge data; client-side centrality |
| `lithos_related(id, include?, depth?, namespace?)` | `id` | Node detail panel; seed for `edge-traversal` reading paths |
| `lithos_node_stats(node_id)` | `node_id` | Node salience and retrieval stats; `salience` reading-path mode |
| `lithos_conflict_resolve(edge_id, resolution, resolver, winner_id?)` | first three | Contradiction resolution UI |
| `lithos_write(title, content, agent, id?, tags?, confidence?, expected_version?)` | `title`, `content`, `agent` | Feedback writes; persisted reading paths under `lens/paths/<slug>` |
| `lithos_tags(prefix?)` | none | Tag cloud / filter panel |
| `lithos_synthesize(query, snippet_ids, agent_id?)` *(future, MVP 3+)* | `query`, `snippet_ids` | Preferred over local LLM for answer synthesis when present; Lens falls back to local LLM otherwise |

#### 16.1.1 SSE event stream

Lens consumes the Lithos SSE event stream at `${LITHOS_URL}${LITHOS_SSE_EVENTS_PATH}`. Event types Lens handles today:

| Event | Consumer |
|-------|----------|
| `task.created` / `task.claimed` / `task.released` / `task.completed` / `task.cancelled` | Tasks view list and summary |
| `finding.posted` | Tasks view findings timeline and "+N findings" badge |
| `note.created` / `edge.created` *(future)* | Knowledge Browser live graph updates |

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
> Milestones 0–3 deliver the common core, the Tasks View MVP, observability, and Tasks SSE auto-update. Only after that does the Knowledge Browser begin (Milestones 4–9). Milestone 10 (semantic projection) is deferred and depends on Lithos exposing embeddings.

### Milestone 0 — Common Core (v0.1)
*Goal: shared scaffolding both views build on*

- [ ] Project scaffold: `pyproject.toml`, `Dockerfile`, FastAPI app skeleton
- [ ] `app/main.py` with view-switcher top-nav and `base.html`
- [ ] `app/config.py` — TOML + env loader, typed config object
- [ ] `app/lithos_client.py` — Lithos MCP client (SSE transport for tools)
- [ ] `app/events.py` — single SSE subscription to Lithos's event stream + in-process pub/sub (skeleton; consumers wired in M2)
- [ ] `app/telemetry.py` skeleton with OTEL setup and `@traced` decorator; `lens.request` span on every route
- [ ] `/health` endpoint reporting `lithos`, `events`, `llm` statuses
- [ ] Read-only Settings view skeleton
- [ ] `docker-compose.yml` with `influx-archive` and `influx-config` mounts
- [ ] `run.sh`
- [ ] Structured JSON logging

### Milestone 1 — Tasks View MVP (v0.2)
*Goal: read-only dashboard over Lithos tasks; manual refresh; no SSE yet*

- [ ] `GET /tasks` route + `app/routers/tasks.py`
- [ ] Summary panel tiles from `lithos_task_list` + `lithos_stats`
- [ ] Task list with default ordering (open / completed / cancelled), `created_at` desc within each group
- [ ] Filters: status, tag (with `key:value` parsing), creating agent (dropdown sourced from registered-agent listing), date range (defaulting to last `tasks.default_time_range_days` days)
- [ ] URL-reflected filter state for shareability
- [ ] Inline claim indicator via per-row `lithos_task_status` up to `tasks.visible_cap`; degradation banner past the cap
- [ ] Task detail panel: header, tags, description, metadata, outcome, active claims, findings timeline
- [ ] Findings timeline paginated at `tasks.findings_page_size`; per-finding `lithos_read` for non-null `knowledge_id` to render the title-labelled link
- [ ] Click-through from finding link to a minimal `/note/{knowledge_id}` route (full feed-detail arrives in M5)
- [ ] Manual refresh button + page-load fetch (no SSE yet)
- [ ] Tasks-specific OTEL spans (`lens.tasks.list`, `lens.tasks.detail`, `lens.tasks.findings`, `lens.tasks.refresh`)
- [ ] Set `ui.default_view = "tasks"` so `/` lands here

### Milestone 2 — Tasks SSE Auto-Update (v0.3)
*Goal: live updates from the Lithos SSE event stream*

- [ ] Wire the M0 `app/events.py` skeleton to the live Lithos SSE endpoint
- [ ] Tasks router subscribes to all task / claim / finding event types
- [ ] HTMX OOB swaps for: row insert, row move-between-groups, claim indicator update, summary tile increment, findings timeline append, "+N findings" badge
- [ ] `GET /tasks/events` re-broadcast endpoint for the browser tab
- [ ] Reconnect with exponential backoff; "Live updates paused" badge during disconnect
- [ ] Polling fallback at `tasks.auto_refresh_interval_s` while SSE disconnected
- [ ] OTEL spans `lens.tasks.event`, `lens.events.connect`

### Milestone 3 — Optional LLM client + Tasks curation (v0.4)
*Goal: enable the optional LLM path, starting with the Tasks "most significant findings" curation*

- [ ] `app/llm_client.py` — provider-agnostic wrapper (anthropic / openai / ollama)
- [ ] Optional install via `uv sync --extra llm`
- [ ] `LENS_LLM_*` env wiring; gated UI hidden when disabled
- [ ] Complexity slider, session-scoped, injected into all LLM prompts (initially used only by Tasks curation, but the wiring is reusable for later milestones)
- [ ] "Most significant findings" toggle behind `llm.enabled && llm.findings_curation_enabled`
- [ ] `POST /api/tasks/findings/curate` endpoint
- [ ] LLM status surfaced in `/health` and settings view
- [ ] OTEL span `lens.tasks.curate`

### Milestone 4 — Feed View + Feedback (v0.5)
*Goal: human-readable feed with feedback mechanism — first Knowledge Browser milestone*

- [ ] Feed view: paper list from `lithos_list`, filterable by profile/date/tag/score
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
- [ ] Filter panel (profile, date, tag, score, edge type)
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
- [ ] Persisted path notes under `lens/paths/<slug>`
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
*Depends on producers writing `metadata.produced_by_task_id` (or equivalent) to note frontmatter.*

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
| `anthropic` / `openai` / `ollama` | LLM provider SDKs (optional extra: `uv sync --extra llm`; install one) |
| Cytoscape.js (CDN) | Graph visualisation; client-side centrality via `cy.elements().bc()` |
| HTMX (CDN) + `htmx.org/ext/sse` | Dynamic HTML; SSE extension drives live tile and row updates |
| Tailwind CSS (CDN) | Styling |

> [!note] LLM provider neutrality
> The LLM client is wrapped in `app/llm_client.py` so the rest of the app calls a small `synthesize()` / `compare_themes()` / `order_path()` / `curate_findings()` interface. Swapping providers is an env change, not a code change.
