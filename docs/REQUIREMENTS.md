---
title: Lithos Lens — Requirements Document
version: 0.4.0
date: 2026-04-22
status: draft
tags: [lithos-lens, requirements, design, architecture]
---

# Lithos Lens — Requirements Document

> [!abstract] Project Summary
> **Lithos Lens** is a local web UI for browsing and curating a Lithos knowledge base. It provides a feed view (time-ordered list of recently ingested items filterable by profile/tag/score), an interactive graph visualisation (Cytoscape.js over LCMA typed edges), a cognitive search bar backed by `lithos_retrieve`, and feedback controls (👍 / 👎) that write back to Lithos. Lens is a pure Lithos MCP client — it has zero runtime dependency on the Influx ingestion container and reads everything (notes, tags, edges, run history, feedback) directly from Lithos.

---

## Table of Contents

- [[#1. Goals & Non-Goals]]
- [[#2. Architecture Overview]]
- [[#3. Infrastructure & Deployment]]
- [[#4. Configuration]]
- [[#5. Feed View]]
- [[#6. Graph View]]
- [[#7. Cognitive Search]]
- [[#8. Feedback Mechanism]]
- [[#9. Conflict Resolution UI]]
- [[#10. Settings View]]
- [[#11. Resilience & Error Handling]]
- [[#12. Observability]]
- [[#13. API Reference]]
- [[#14. Implementation Plan]]

---

## 1. Goals & Non-Goals

### Goals

- Provide a low-latency local browser UI for a Lithos knowledge base
- Feed view: time-ordered cards filterable by profile, date, tag, score, source
- Interactive graph view with Cytoscape.js, rendering LCMA typed edges
- Cognitive search bar using `lithos_retrieve` (seven-scout PTS retrieval with reranking)
- Feedback controls — mark items as relevant / not relevant; write back to Lithos
- Conflict resolution UI for LCMA `contradicts` edges
- Expose a read-only view of the shared Influx configuration (profiles, thresholds, feeds)
- Operate purely as a Lithos MCP client — no dependency on Influx runtime
- Minimal stack: FastAPI + HTMX + Cytoscape.js; no heavy JS framework, no build step

### Non-Goals

- Editing note content inline (that's Obsidian's job — or direct MCP tools)
- Running its own ingestion — it never writes new notes from scratch
- Hosting an external collaboration surface — single-user, local-only
- Authoring feedback for knowledge items that Influx did not ingest — v1 assumes feedback is Influx-centric; a later version can generalise
- Deep editing of LCMA edges (users can resolve conflicts; creating/deleting arbitrary edges is out of scope for v1)

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
│  │  MCP API     │     │  batch job   │     │  HTTP server    │   │
│  └──────────────┘     └──────────────┘     └────────┬────────┘   │
│          ▲                                           │            │
│          └───────────────────────────────────────────┘            │
│                       Lithos MCP API only                         │
└──────────────────────────────────────────────────────────────────┘
                                                       │
                                                       ▼
                                                ┌──────────────┐
                                                │   BROWSER    │
                                                │  (human UI)  │
                                                └──────────────┘
```

> [!important] Lens is Influx-independent
> **Lithos Lens has zero runtime dependency on the Influx ingestion container.** It is a pure Lithos MCP client. All data — paper notes, run history, feedback, graph edges — comes from Lithos. The UI and ingestion pipeline can be restarted, updated, or fail independently. Lens does mount the `influx-archive` volume read-only so it can serve archived PDFs/HTMLs directly, but that is a file-system dependency on a shared volume, not a runtime dependency on the Influx process.

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deployment | Separate Docker container | Independent restartability; clean separation from Influx |
| Lithos communication | Lithos MCP API (SSE transport) | Same transport as Influx; single source of truth |
| Graph rendering | Cytoscape.js | Best for knowledge graphs; handles typed LCMA edges; scales to ~10K nodes |
| Frontend | FastAPI + HTMX + Cytoscape.js | No build step; minimal stack; dynamic HTML without a JS framework |
| Styling | Tailwind CSS via CDN | No build step |
| Config format | TOML (read-only, shared with Influx) | Consistent with Influx and Lithos conventions |
| OTEL | Opt-in, additive, optional packages | Consistent with Lithos conventions |
| Environments | `.env.dev` / `.env.prod` | Consistent with Lithos conventions |

---

## 3. Infrastructure & Deployment

### Container

| Container | Base image | Purpose |
|-----------|-----------|--------|
| `lithos-lens` | `python:3.12-slim` | Web UI, feed view, graph view, feedback API |

### Shared Volumes

| Volume | Lens mount | Purpose |
|--------|------------|---------|
| `influx-archive` | `/archive` (ro) | Serve archived PDFs/HTMLs inline |
| `influx-config` | `/etc/influx` (ro) | Read the shared Influx TOML config for the settings view |

### Environment Files

**`.env.dev`:**
```env
LENS_ENVIRONMENT=dev
LENS_HOST_PORT=7843
LENS_CONTAINER_NAME=lithos-lens
LENS_OTEL_ENABLED=false
OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318
```

**`.env.prod`:**
```env
LENS_ENVIRONMENT=production
LENS_HOST_PORT=7843
LENS_CONTAINER_NAME=lithos-lens
LENS_OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

### `docker-compose.yml`

```yaml
# lithos-lens — knowledge browser UI
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
      - LENS_AGENT_ID=${LENS_AGENT_ID:-lithos-lens}
      - LENS_OTEL_ENABLED=${LENS_OTEL_ENABLED:-false}
      - OTEL_EXPORTER_OTLP_ENDPOINT=${OTEL_EXPORTER_OTLP_ENDPOINT:-http://host.docker.internal:4318}
      - LENS_LOG_LEVEL=${LENS_LOG_LEVEL:-INFO}
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

Lens has minimal configuration of its own. The main runtime config comes from environment variables (Lithos URL, ports, telemetry). The **Influx TOML config is mounted read-only** so the settings view can display profiles, thresholds, models, and feed lists.

```toml
# /etc/lithos-lens/config.toml  (optional — most settings come from env)

[ui]
default_view = "feed"           # feed | graph
feed_page_size = 50
graph_max_nodes = 500           # safety cap for graph render

[search]
default_limit = 20
namespace_filter = []           # optional; empty = all namespaces

[telemetry]
enabled = false                 # overridden by LENS_OTEL_ENABLED
console_fallback = false
service_name = "lithos-lens"
export_interval_ms = 30000
```

---

## 5. Feed View

### 5.1 Purpose

Time-ordered list of recently ingested items. The default landing view.

### 5.2 Data Source

```python
items = lithos_list(
    path_prefix=f"papers/{profile}" if profile else "papers/",
    tags=selected_tags or None,
    since=selected_since or None,
    limit=config.ui.feed_page_size,
    offset=page_offset,
)
```

### 5.3 UI Elements

| Element | Behaviour |
|---------|-----------|
| Filter bar | Profile dropdown, date range, tag chips, score slider |
| Paper card | Title, authors, source, score, tags, collapsed abstract |
| Expandable abstract | HTMX swap loads full frontmatter content from `lithos_read(id=...)` |
| Archive link | If `local_file` is set in frontmatter, link opens via `/archive/...` (the read-only mount) |
| Source link | Opens `source_url` externally |
| 👍 / 👎 buttons | Write back via `lithos_write` (see §8) |
| "Related" teaser | Calls `lithos_related(id=..., include=["edges", "links"], depth=1)` |
| "Open in graph" link | Jumps to graph view centred on this note's id |

### 5.4 Pagination

HTMX infinite scroll with `offset` passed as a query param. The `total` field from `lithos_list` is used to display "Showing N of M".

---

## 6. Graph View

### 6.1 Purpose

Interactive visualisation of the knowledge base as a typed, weighted graph. Helps surface clusters, contradictions, and lineage at a glance.

### 6.2 Data Sources

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

### 6.3 Rendering

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

### 6.4 Interactions

- Click a node → side panel with detail, `lithos_related`, `lithos_node_stats`
- Filter panel: profile, date, tag, score, edge type, namespace
- "Centre on this" action rebuilds the graph around a single node at depth 2
- If the raw edge count exceeds `ui.graph_max_nodes`, the view degrades to a paged sample with a warning banner

### 6.5 Performance Notes

- Cytoscape.js comfortably handles ~10K nodes on a modern browser
- For larger graphs, apply `namespace_filter` or `path_prefix` before fetch
- Edge fetching happens once per view load; subsequent filter changes operate on the client-side dataset

---

## 7. Cognitive Search

### 7.1 Search Bar

Every view has a search bar. Backed by `lithos_retrieve` (LCMA MVP1) rather than plain `lithos_search`, because retrieval runs seven scouts with reranking and gives an audit receipt:

```python
results = lithos_retrieve(
    query=search_query,
    limit=config.search.default_limit,
    agent_id="lithos-lens",
    tags=[f"profile:{active_profile}"] if active_profile else None,
)
```

### 7.2 Results Rendering

Each result is rendered as a compact card:

- Title + score
- `snippet` (from Lithos)
- `reasons` (LCMA-specific audit — which scouts fired)
- `scouts` list (chips)
- Click-through to feed-view detail or graph-view node

### 7.3 Search Scope

A toggle exposes `namespace_filter` so the user can scope search to a single profile's namespace or leave it broad.

---

## 8. Feedback Mechanism

### 8.1 Overview

Lens is the only place in the system where humans generate feedback. Feedback is stored as additional tags on the existing Lithos note (specifically `influx:rejected` and optionally `influx:accepted` for positive reinforcement).

### 8.2 Writing Feedback — Critical Contract

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

### 8.3 Feedback Entry Points

| Entry point | Mechanism |
|-------------|-----------|
| Feed view | 👍 / 👎 buttons on each paper card |
| Graph view | 👎 button in node detail panel |
| Notification link | `[not relevant]` link from Agent Zero digest → `POST /api/feedback` |

### 8.4 Feedback API

```
POST /api/feedback
{
  "note_id": "<uuid>",
  "verdict": "accepted" | "rejected",
  "note": "<optional user comment>"
}
```

Response: `{"status": "ok"}` on success, `{"status": "error", "message": "..."}` on failure. Errors are shown in the UI — never silently swallowed.

### 8.5 Downstream Effect on Influx

Influx picks up feedback at the start of each run by calling `lithos_list(tags=["influx:rejected", f"profile:{name}"])` — see Influx requirements §12. Lens does not have to notify Influx directly.

---

## 9. Conflict Resolution UI

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

On success, the edge is re-fetched and redrawn with a resolution badge. Unresolved contradictions are surfaced in a "Needs attention" banner on the feed view.

---

## 10. Settings View

Read-only. Displays:

- Current Influx profiles (names, descriptions, thresholds) — parsed from `/etc/influx/config.toml`
- Current feed list per profile
- Current model assignments
- Current telemetry flags
- Last Influx run info (pulled from Lithos notes at `path: "influx/runs"`)

Editing happens by changing the TOML file outside the container. Lens does not write to config.

---

## 11. Resilience & Error Handling

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

---

## 12. Observability

### OTEL — Opt-In, Additive

Same pattern as Lithos and Influx:

- `LENS_OTEL_ENABLED=true` enables it
- Optional packages installed via `uv sync --extra otel`
- `LENS_OTEL_CONSOLE_FALLBACK=true` prints spans to stdout

**Key spans:**

| Span | Description |
|------|-------------|
| `lens.request` | Each HTTP request |
| `lens.feed.list` | Feed-view data fetch |
| `lens.graph.edges` | Graph-view edge fetch |
| `lens.retrieve` | Cognitive search call |
| `lens.feedback.write` | Feedback write to Lithos |
| `lens.archive.serve` | Archive file serve |

### Logging

- stdout only; structured JSON via `python-json-logger`
- `LENS_LOG_LEVEL` controls verbosity

### Health Endpoint

```
GET /health → {"status": "ok", "lithos": "ok" | "degraded" | "unreachable"}
```

The `lithos` status is derived from a single `lithos_stats()` call on start-up plus a cached result refreshed every 30 seconds.

---

## 13. API Reference

### 13.1 Lithos MCP API — Lens Usage

| Tool | Required args | Purpose |
|------|---------------|---------|
| `lithos_list(path_prefix?, tags?, since?, limit?, offset?)` | none | Feed view paper listing |
| `lithos_read(id)` | `id` | Paper detail view; also used when building feedback writes |
| `lithos_retrieve(query, limit?, agent_id?, tags?)` | `query` | Cognitive search bar |
| `lithos_search(query, mode?, tags?, ...)` | `query` | Fallback search when `retrieve` errors |
| `lithos_edge_list(namespace?, type?)` | none | Graph edge data for Cytoscape |
| `lithos_related(id, include?, depth?, namespace?)` | `id` | Node detail panel — related papers |
| `lithos_node_stats(node_id)` | `node_id` | Node salience and retrieval stats |
| `lithos_conflict_resolve(edge_id, resolution, resolver, winner_id?)` | first three | Contradiction resolution UI |
| `lithos_write(title, content, agent, id?, tags?, confidence?, expected_version?)` | `title`, `content`, `agent` | Feedback writes — must re-pass title+content from a prior `lithos_read` |
| `lithos_tags(prefix?)` | none | Tag cloud / filter panel |
| `lithos_agent_register(id, name?, type?)` | `id` | Optional startup registration |
| `lithos_stats()` | none | Health endpoint status probe |

### 13.2 Lens Internal HTTP Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Feed view |
| `GET /graph` | Graph view |
| `GET /search?q=...` | Search results |
| `GET /note/{id}` | Note detail panel |
| `GET /settings` | Read-only settings view |
| `POST /api/feedback` | Feedback write endpoint |
| `POST /api/conflict/resolve` | Conflict resolution submission |
| `GET /archive/{path}` | Stream archived files from the mounted volume |
| `GET /health` | Health probe |

---

## 14. Implementation Plan

### Milestone 1 — Feed View + Feedback (v0.1)
*Goal: human-readable feed with feedback mechanism*

- [ ] Project scaffold: `pyproject.toml`, `Dockerfile`, FastAPI app skeleton
- [ ] Lithos MCP client wrapper (`app/lithos_client.py`) with SSE transport
- [ ] Feed view: paper list from `lithos_list`, filterable by profile/date/tag/score
- [ ] Expandable abstract via HTMX + `lithos_read`
- [ ] Archive file streaming via `/archive/...` from the read-only mount
- [ ] 👍 / 👎 feedback buttons → read-then-write pattern with `lithos_write`
- [ ] `POST /api/feedback` endpoint with version-conflict retry
- [ ] Settings view (read-only) from `/etc/influx/config.toml`
- [ ] Health endpoint with Lithos probe
- [ ] `docker-compose.yml` with read-only `influx-archive` and `influx-config` mounts
- [ ] Structured JSON logging to stdout

### Milestone 2 — Cognitive Search (v0.2)
*Goal: high-quality search with LCMA audit*

- [ ] Search bar wired to `lithos_retrieve`
- [ ] Fallback to `lithos_search` on retrieval errors
- [ ] Namespace/profile scope toggle
- [ ] Result cards with scout chips and reasons
- [ ] Tag cloud from `lithos_tags`

### Milestone 3 — Graph View (v0.3)
*Goal: visual knowledge graph with LCMA edges*

- [ ] Graph view with Cytoscape.js
- [ ] Nodes sized by `lithos_node_stats.salience`, coloured by profile
- [ ] Edges from `lithos_edge_list` — typed and colour-coded
- [ ] Click node → side panel with `lithos_related(include=["edges", "links", "provenance"])`
- [ ] Filter panel (profile, date, tag, score, edge type)
- [ ] Safety cap via `ui.graph_max_nodes`

### Milestone 4 — Conflict Resolution (v0.4)
*Goal: surface and resolve contradictions*

- [ ] "Needs attention" banner for unresolved `contradicts` edges
- [ ] Node-detail resolution panel calling `lithos_conflict_resolve`
- [ ] Winner picker for `superseded` resolution
- [ ] Badge rendering on resolved edges in the graph

### Milestone 5 — Observability (v0.5)
*Goal: production-ready telemetry*

- [ ] `app/telemetry.py` — mirrors Lithos OTEL pattern
- [ ] `@traced` decorator on key request paths
- [ ] OTEL metrics: requests, feedback writes, search latency
- [ ] Request-level structured logs

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
│   ├── telemetry.py
│   ├── routers/
│   │   ├── feed.py
│   │   ├── graph.py
│   │   ├── search.py
│   │   ├── feedback.py
│   │   ├── conflict.py
│   │   └── settings.py
│   └── templates/
│       ├── base.html
│       ├── feed.html
│       ├── graph.html
│       ├── search.html
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
| `httpx` | Lithos MCP transport (SSE) |
| `jinja2` | HTML templating |
| `pydantic` | Request/response validation |
| `python-json-logger` | Structured JSON logging |
| `opentelemetry-*` | OTEL (optional extra) |
| Cytoscape.js (CDN) | Graph visualisation |
| HTMX (CDN) | Dynamic HTML without a JS framework |
| Tailwind CSS (CDN) | Styling |
