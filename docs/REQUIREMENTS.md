---
title: Lithos Lens — Requirements Document
version: 0.5.0
date: 2026-04-26
status: draft
tags: [lithos-lens, requirements, design, architecture]
---

# Lithos Lens — Requirements Document

> [!abstract] Project Summary
> **Lithos Lens** is a local web UI for browsing and curating a Lithos knowledge base. It provides a feed view (time-ordered list of recently ingested items filterable by profile/tag/score), an interactive graph visualisation (Cytoscape.js over LCMA typed edges), a cognitive search bar backed by `lithos_retrieve`, and feedback controls (👍 / 👎) that write back to Lithos. Lens is a pure Lithos MCP client — it has zero runtime dependency on the Influx ingestion container and reads everything (notes, tags, edges, run history, feedback) directly from Lithos. From v0.5 onwards Lens may also call an LLM directly **when explicitly enabled** (`LENS_LLM_ENABLED=true`) to provide answer synthesis, note comparison, and explanation-depth control; with the flag off Lens remains a pure MCP client.

> [!note] v0.5 changelog
> v0.5 incorporates ideas surfaced from a review of the Paperlens prototype (`/paperlens`): answer synthesis with citations, multi-note comparison, an expertise-level slider, curated reading paths, graph centrality overlay, and bidirectional node↔panel selection. Quiz/flashcard generation and embedding storage from Paperlens are explicitly out of scope.

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
- [[#9. Note Comparison]]
- [[#10. Reading Paths]]
- [[#11. Conflict Resolution UI]]
- [[#12. Settings View]]
- [[#13. Resilience & Error Handling]]
- [[#14. Observability]]
- [[#15. API Reference]]
- [[#16. Implementation Plan]]

---

## 1. Goals & Non-Goals

### Goals

- Provide a low-latency local browser UI for a Lithos knowledge base
- Feed view: time-ordered cards filterable by profile, date, tag, score, source
- Interactive graph view with Cytoscape.js, rendering LCMA typed edges
- Cognitive search bar using `lithos_retrieve` (seven-scout PTS retrieval with reranking)
- Feedback controls — mark items as relevant / not relevant; write back to Lithos
- Conflict resolution UI for LCMA `contradicts` edges
- Multi-note **comparison** view (metadata + content; LLM-driven theme and concept analysis when LLM is enabled)
- Curated **reading paths** through a node subset — algorithmic by default, LLM-curated when LLM is enabled
- Graph **centrality overlay** to highlight bridge nodes between clusters
- Optional LLM-backed **answer synthesis** and **explanation-depth control**, behind a single config flag, gracefully degrading when disabled
- Expose a read-only view of the shared Influx configuration (profiles, thresholds, feeds)
- Operate purely as a Lithos MCP client when `LENS_LLM_ENABLED=false` — no dependency on Influx runtime
- Minimal stack: FastAPI + HTMX + Cytoscape.js; no heavy JS framework, no build step

### Non-Goals

- Editing note content inline (that's Obsidian's job — or direct MCP tools)
- Running its own ingestion — it never writes new notes from scratch
- Hosting an external collaboration surface — single-user, local-only
- Authoring feedback for knowledge items that Influx did not ingest — v1 assumes feedback is Influx-centric; a later version can generalise
- Deep editing of LCMA edges (users can resolve conflicts; creating/deleting arbitrary edges is out of scope for v1)
- Quiz / flashcard generation (a Paperlens feature; not appropriate for a knowledge browser)
- Hosting embeddings or running its own vector index — UMAP-style semantic projections are deferred and depend on Lithos exposing embeddings via a future MCP tool

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
                              (LLM API, optional) ─────┤
                                                       ▼
                                                ┌──────────────┐
                                                │   BROWSER    │
                                                │  (human UI)  │
                                                └──────────────┘
```

> [!important] Lens is Influx-independent
> **Lithos Lens has zero runtime dependency on the Influx ingestion container.** It is a pure Lithos MCP client. All data — paper notes, run history, feedback, graph edges — comes from Lithos. The UI and ingestion pipeline can be restarted, updated, or fail independently. Lens does mount the `influx-archive` volume read-only so it can serve archived PDFs/HTMLs directly, but that is a file-system dependency on a shared volume, not a runtime dependency on the Influx process.

> [!note] Optional LLM client
> When `LENS_LLM_ENABLED=true`, Lens additionally talks to an LLM provider (Anthropic / OpenAI / Ollama) for synthesis, comparison, and complexity-tuned output. With the flag off, all LLM-dependent UI surfaces are hidden and Lens remains a pure MCP client. When Lithos exposes a synthesis tool (`lithos_synthesize` or equivalent) in a later MVP, Lens prefers the MCP path and treats the local LLM as a fallback.

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
| LLM access | Optional, env-gated client (`LENS_LLM_*`); provider-agnostic wrapper | Lithos MVP 1 does not provide synthesis or comparison tools; Lens needs LLM access to deliver Q&A synthesis, note comparison, and complexity-adjusted output. When Lithos ships MVP-3 synthesis, Lens prefers the MCP path. |
| Centrality computation | Client-side in Cytoscape | Lithos exposes edges via `lithos_edge_list` but no centrality scores; computing in the browser avoids a new MCP tool and operates on the already-loaded graph |

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

# Optional LLM client — disabled by default
LENS_LLM_ENABLED=false
# LENS_LLM_PROVIDER=anthropic        # anthropic | openai | ollama
# LENS_LLM_MODEL=claude-haiku-4-5-20251001
# LENS_LLM_API_KEY=                  # provider-dependent; not needed for ollama
# LENS_LLM_MAX_TOKENS=2048
```

**`.env.prod`:**
```env
LENS_ENVIRONMENT=production
LENS_HOST_PORT=7843
LENS_CONTAINER_NAME=lithos-lens
LENS_OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318

# Optional LLM client — disabled by default
LENS_LLM_ENABLED=false
# LENS_LLM_PROVIDER=anthropic
# LENS_LLM_MODEL=claude-haiku-4-5-20251001
# LENS_LLM_API_KEY=
# LENS_LLM_MAX_TOKENS=2048
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

Lens has minimal configuration of its own. The main runtime config comes from environment variables (Lithos URL, ports, telemetry, optional LLM). The **Influx TOML config is mounted read-only** so the settings view can display profiles, thresholds, models, and feed lists.

```toml
# /etc/lithos-lens/config.toml  (optional — most settings come from env)

[ui]
default_view = "feed"           # feed | graph
feed_page_size = 50
graph_max_nodes = 500           # safety cap for graph render
graph_centrality_overlay = false  # off by default; toggle in graph filter panel
reading_path_default = "salience" # salience | chronological | edge-traversal | llm
compare_max_notes = 4           # cap on the multi-select for comparison

[search]
default_limit = 20
namespace_filter = []           # optional; empty = all namespaces

[llm]
enabled = false                 # overridden by LENS_LLM_ENABLED
provider = "anthropic"          # anthropic | openai | ollama
model = "claude-haiku-4-5-20251001"
default_complexity = 3          # 1=beginner … 5=expert; per-session override allowed
max_tokens = 2048
synthesis_prefer_mcp = true     # use lithos_synthesize when available, else local LLM

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
| Multi-select toggle | Enables comparison mode (see §9); shows checkbox on each card and a floating "Compare N notes" action |
| "Related" teaser | Calls `lithos_related(id=..., include=["edges", "links"], depth=1)` |
| "Open in graph" link | Jumps to graph view centred on this note's id |
| "Generate path" action | Opens reading-path picker (see §10) seeded from the current filter set or the selected card |

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
- Ctrl/cmd-click adds nodes to the comparison multi-select (see §9)
- "Path from here" action seeds a reading path (see §10) from the selected node
- If the raw edge count exceeds `ui.graph_max_nodes`, the view degrades to a paged sample with a warning banner

### 6.5 Performance Notes

- Cytoscape.js comfortably handles ~10K nodes on a modern browser
- For larger graphs, apply `namespace_filter` or `path_prefix` before fetch
- Edge fetching happens once per view load; subsequent filter changes operate on the client-side dataset

### 6.6 Centrality Overlay

When `ui.graph_centrality_overlay = true` (or the user toggles "Highlight bridge nodes" in the filter panel), Lens computes betweenness centrality client-side over the currently-loaded edge set using Cytoscape's built-in `cy.elements().bc()`. The top-K nodes (default 5%, configurable via the toggle) render with a halo / outline ring. Centrality is recomputed whenever the visible subgraph changes (e.g. when an edge-type filter is toggled). No new MCP calls are required. Adds OTEL span `lens.graph.centrality`.

### 6.7 Bidirectional Node ↔ Panel Selection

The existing flow is one-way: click node → side panel updates. v0.5 adds the reverse: clicking a related-paper row in the side panel highlights and centres the corresponding node in the graph (without rebuilding the layout). This makes the panel the primary navigation surface as well as a read-only detail view. No new data fetch — pure UI wiring on the already-loaded graph.

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

### 7.4 Answer Synthesis (LLM, optional)

When `llm.enabled = true`, the search bar shows a "Synthesise answer" toggle alongside the result list. When toggled on, Lens:

1. Calls `lithos_retrieve` as normal.
2. Passes the top-N snippets (each with `id`, `path`, `snippet`, `reasons`) to the configured LLM along with the query.
3. The system prompt **requires** that every claim in the synthesised answer carry an inline citation referencing the snippet `id` (e.g. `[1]`, `[2]`).
4. Renders the synthesised answer as a single block above the result list, with click-through citations linking to the corresponding feed-detail or graph-node view. The underlying retrieve result list stays visible below for transparency.

If `llm.synthesis_prefer_mcp = true` and Lithos exposes a synthesis MCP tool (e.g. `lithos_synthesize`), Lens calls that tool first and only falls back to the local LLM if it returns `not_supported` or errors.

**Failure modes**: LLM error → hide the synthesised block, keep the result list, show a non-blocking warning badge ("Synthesis unavailable — showing raw results"). Adds OTEL span `lens.llm.synthesize`.

### 7.5 Complexity Slider (LLM, optional)

A session-scoped slider (1 = beginner … 5 = expert), default = `llm.default_complexity`, is exposed in the search bar and in any LLM-augmented panel (synthesis result, comparison themes tab, LLM-curated paths). The selected value is persisted via cookie or query param and injected into every LLM prompt as a system instruction modulating verbosity and technicality. The slider has no effect when `llm.enabled = false` and is hidden entirely in that mode.

Rationale: Paperlens demonstrated this is a high-value, low-cost UX lever — the same retrieved evidence yields visibly different explanations for different audiences without changing the underlying data.

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

## 9. Note Comparison

### 9.1 Purpose

Place two or more notes side-by-side to surface what they share and where they diverge. Particularly useful for evaluating papers that look similar in the feed view but differ in method, scope, or claim, and for inspecting both endpoints of a `contradicts` edge before resolving it.

### 9.2 Entry Points

| Entry point | Mechanism |
|-------------|-----------|
| Feed view | Multi-select toggle reveals checkboxes on cards; floating "Compare N notes" action opens the comparison view |
| Graph view | Ctrl/cmd-click on nodes to add to the selection; "Compare selected" button in the side panel |
| Search results | "Compare with…" action on any result card |
| Conflict resolution UI | "Compare endpoints" button on a `contradicts` edge auto-loads its `from_id` and `to_id` |

The maximum number of notes that can be compared is configurable via `ui.compare_max_notes` (default 4).

### 9.3 Layout

The comparison view is a horizontally scrollable side-by-side panel with three tabs:

- **Metadata** — titles, authors, source, ingestion date, profile, tags, score, ids. Shared values (e.g. co-authors, overlapping tags) are highlighted. No LLM required.
- **Content** — collapsed/expandable abstracts and bodies fetched via `lithos_read`; tunable character limit per pane to avoid runaway panels. No LLM required.
- **Themes & Concepts** *(only when `llm.enabled = true`)* — Lens passes the selected notes' titles + abstracts (or full content under a per-call token budget) to the LLM with a structured prompt that returns:
  - A bullet list of dominant themes per note
  - A shared-concepts table (concept → notes mentioning it → terminology variant in each note)
  - A unique-concepts list per note
  - One paragraph summarising similarities, differences, and complementarity
  When `llm.enabled = false` this tab is hidden.

### 9.4 API

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

## 10. Reading Paths

### 10.1 Purpose

Surface an ordered traversal through a subset of notes — a "what should I read next, and in what order?" view. Distinct from search (which ranks for relevance) and from the graph (which shows topology without ordering). Inspired by Paperlens's curated learning paths but generalised to any Lithos namespace.

### 10.2 Entry Points

| Entry point | Mechanism |
|-------------|-----------|
| Feed view | "Generate path" button in the filter bar uses the active filter set as the candidate pool |
| Graph view | "Path from here" action on a selected node uses that node as the seed and walks outward |
| Settings view | "Generate path for profile" link uses the profile namespace as the pool |

### 10.3 Modes

| Mode | Description | LLM required |
|------|-------------|--------------|
| `salience` | Order by `lithos_node_stats.salience` desc | No |
| `chronological` | Order by ingestion date asc (matches a "build-up" reading style) | No |
| `edge-traversal` | BFS / topological walk over `builds_on` and `derived_from` edges starting from the seed node | No |
| `llm` | Pass node titles + summaries to the LLM with a "produce a pedagogical reading order with one-line justifications" prompt | Yes |

Default mode is `ui.reading_path_default` (default `salience`). The picker UI lets the user override per-request. The `llm` mode is hidden when `llm.enabled = false`.

### 10.4 Output

Output is a single ordered list rendered as a printable, shareable page at `GET /path/{slug}`. Each step shows position, title, one-line justification (manual for non-LLM modes, LLM-generated otherwise), and a deep link to the note's feed-detail view.

The user can save a path. Saved paths persist as a Lithos note under `path: "lens/paths/<slug>"` via `lithos_write`, with the seed, mode, filter set, and ordered ids in the frontmatter — making them durable, searchable, and shareable across Lithos clients.

### 10.5 API

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

## 11. Conflict Resolution UI

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
- "Compare endpoints" button (see §9.2) for side-by-side inspection before resolving

On success, the edge is re-fetched and redrawn with a resolution badge. Unresolved contradictions are surfaced in a "Needs attention" banner on the feed view.

---

## 12. Settings View

Read-only. Displays:

- Current Influx profiles (names, descriptions, thresholds) — parsed from `/etc/influx/config.toml`
- Current feed list per profile
- Current model assignments
- Current telemetry flags
- Current LLM flags (`llm.enabled`, provider, model, complexity default) — values only, no API key disclosure
- Last Influx run info (pulled from Lithos notes at `path: "influx/runs"`)

Editing happens by changing the TOML file outside the container. Lens does not write to config.

---

## 13. Resilience & Error Handling

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
| `llm.enabled = false` | Synthesis toggle, comparison "Themes" tab, complexity slider, and `llm` reading-path mode are hidden; remaining UI fully functional |
| LLM provider error (transient) | Per-feature failure: synthesis hides and result list still renders; comparison falls back to metadata + content tabs only; reading path falls back to `salience` mode; non-blocking toast with retry |
| LLM provider misconfigured at startup | Log error, set effective `llm.enabled = false`, surface warning in settings view |
| Centrality computation fails | Disable overlay; toast warning; rest of graph unaffected |

---

## 14. Observability

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
GET /health → {"status": "ok", "lithos": "ok" | "degraded" | "unreachable", "llm": "disabled" | "ok" | "error"}
```

The `lithos` status is derived from a single `lithos_stats()` call on start-up plus a cached result refreshed every 30 seconds. The `llm` status reports the configured provider's reachability when `llm.enabled = true`, else `"disabled"`.

---

## 15. API Reference

### 15.1 Lithos MCP API — Lens Usage

| Tool | Required args | Purpose |
|------|---------------|---------|
| `lithos_list(path_prefix?, tags?, since?, limit?, offset?)` | none | Feed view paper listing |
| `lithos_read(id)` | `id` | Paper detail view; also used when building feedback writes and comparison content |
| `lithos_retrieve(query, limit?, agent_id?, tags?)` | `query` | Cognitive search bar |
| `lithos_search(query, mode?, tags?, ...)` | `query` | Fallback search when `retrieve` errors |
| `lithos_edge_list(namespace?, type?)` | none | Graph edge data for Cytoscape; also feeds client-side centrality |
| `lithos_related(id, include?, depth?, namespace?)` | `id` | Node detail panel — related papers; seed for `edge-traversal` reading paths |
| `lithos_node_stats(node_id)` | `node_id` | Node salience and retrieval stats; drives `salience` reading-path mode |
| `lithos_conflict_resolve(edge_id, resolution, resolver, winner_id?)` | first three | Contradiction resolution UI |
| `lithos_write(title, content, agent, id?, tags?, confidence?, expected_version?)` | `title`, `content`, `agent` | Feedback writes; also persists saved reading paths under `lens/paths/<slug>` |
| `lithos_tags(prefix?)` | none | Tag cloud / filter panel |
| `lithos_agent_register(id, name?, type?)` | `id` | Optional startup registration |
| `lithos_stats()` | none | Health endpoint status probe |
| `lithos_synthesize(query, snippet_ids, agent_id?)` *(future, MVP 3+)* | `query`, `snippet_ids` | Preferred over local LLM for answer synthesis when present; Lens falls back to local LLM otherwise |

### 15.2 Lens Internal HTTP Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Feed view |
| `GET /graph` | Graph view |
| `GET /search?q=...` | Search results |
| `GET /note/{id}` | Note detail panel |
| `GET /settings` | Read-only settings view |
| `POST /api/feedback` | Feedback write endpoint |
| `POST /api/conflict/resolve` | Conflict resolution submission |
| `POST /api/synthesize` | Answer synthesis (only when `llm.enabled` or `lithos_synthesize` is available) |
| `POST /api/compare` | Multi-note comparison |
| `POST /api/path` | Reading-path generation |
| `GET /path/{slug}` | Render a saved reading path |
| `GET /archive/{path}` | Stream archived files from the mounted volume |
| `GET /health` | Health probe |

---

## 16. Implementation Plan

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

### Milestone 6 — Reading Paths & Centrality (v0.6)
*Goal: ordering and structural insight without new dependencies*

- [ ] Bidirectional node ↔ side-panel selection
- [ ] Centrality overlay toggle in graph filter panel; client-side betweenness via Cytoscape
- [ ] `POST /api/path` with `salience`, `chronological`, and `edge-traversal` modes
- [ ] Reading-path picker UI in feed view and graph view
- [ ] Persisted path notes under `lens/paths/<slug>` via `lithos_write`
- [ ] `GET /path/{slug}` printable / shareable page

### Milestone 7 — LLM Features (v0.7)
*Goal: answer synthesis, comparison, complexity-tuned output*

- [ ] `app/llm_client.py` — provider-agnostic wrapper (anthropic / openai / ollama)
- [ ] Optional install via `uv sync --extra llm`
- [ ] `LENS_LLM_*` env wiring; gated UI elements hidden when disabled
- [ ] Complexity slider, session-scoped, injected into all LLM prompts
- [ ] Answer synthesis: "Synthesise" toggle in search bar; calls `lithos_synthesize` when present, else local LLM
- [ ] Multi-note comparison "Themes & Concepts" tab
- [ ] LLM-curated reading-path mode
- [ ] LLM status surfaced in `/health` and settings view

### Milestone 8 — Semantic Projection (v0.8, deferred)
*Goal: alternative graph view by semantic similarity*

> [!note] Depends on a Lithos work-item
> This milestone is blocked until Lithos exposes a `lithos_embeddings(node_ids)` MCP tool (or equivalent). It is not a Lens-only deliverable.

- [ ] `lithos_embeddings` MCP tool available in Lithos
- [ ] 2D UMAP / t-SNE projection of nodes
- [ ] Toggle between force-directed and semantic-projection layouts
- [ ] Cluster overlay derived from projection density

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
│   ├── llm_client.py            # optional, gated by LENS_LLM_ENABLED
│   ├── telemetry.py
│   ├── routers/
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
| `httpx` | Lithos MCP transport (SSE); also LLM HTTP client |
| `jinja2` | HTML templating |
| `pydantic` | Request/response validation |
| `python-json-logger` | Structured JSON logging |
| `opentelemetry-*` | OTEL (optional extra: `uv sync --extra otel`) |
| `anthropic` / `openai` / `ollama` | LLM provider SDKs (optional extra: `uv sync --extra llm`; install one) |
| Cytoscape.js (CDN) | Graph visualisation; also provides `cy.elements().bc()` for client-side centrality |
| HTMX (CDN) | Dynamic HTML without a JS framework |
| Tailwind CSS (CDN) | Styling |

> [!note] LLM provider neutrality
> The LLM client is wrapped in `app/llm_client.py` so the rest of the app calls a small `synthesize()` / `compare_themes()` / `order_path()` interface. Swapping providers is an env change, not a code change.
