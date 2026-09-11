# Lithos Lens - Specification

Version: 0.3.0  
Date: 2026-08-31  
Status: Aligned with Implementation (T1 and K1 shipped)

## 1. Purpose

Lithos Lens is a web UI for operating and browsing a running Lithos system.

This document describes the current behavior that exists in the `lithos-lens`
codebase today. It is intentionally narrower than
[`docs/REQUIREMENTS.md`](./REQUIREMENTS.md), which contains broader product
requirements and future intent.

## 2. Goals

The current implementation is optimized for:

- Providing an operator-facing dashboard for Lithos task activity.
- Showing what tasks are open, recently completed, or recently cancelled.
- Showing known claim state, findings, and related note links where available.
- Surfacing live task updates in the browser without requiring page reloads.
- Remaining a thin integration layer over Lithos, with minimal or no required
  changes to Lithos itself.

## 3. Non-Goals

The current implementation does not attempt to provide:

- A full knowledge browser over all Lithos notes.
- Archive browsing, file serving, or document preview workflows.
- Authentication or authorization.
- Multi-user session management.
- Rich write operations back into Lithos.
- Required LLM functionality. LLM support is present in configuration only and
  is currently optional and disabled by default.

## 4. Runtime Model

Lithos Lens is a standalone FastAPI application that talks to an existing
Lithos server over HTTP.

At a high level:

1. The browser talks to Lithos Lens.
2. Lithos Lens fetches task and note data from Lithos using its HTTP APIs.
3. Lithos Lens maintains a single shared subscription to Lithos `/events`.
4. Lithos Lens fans normalized task-related events out to connected browsers
   over a browser-facing SSE endpoint.

Lens does not currently maintain its own durable application database. Its
state is derived from Lithos, in-process caches, and runtime configuration.

## 5. Implemented Surface

### 5.1 HTTP Routes

The current application exposes these routes:

- `GET /`
  Renders the Tasks dashboard. This is currently the default application view.
- `GET /health`
  Returns Lens health information suitable for container or service checks.
- `GET /tasks`
  Renders the task dashboard and accepts filter query parameters.
- `GET /tasks/events`
  Browser-facing Server-Sent Events endpoint for live task updates.
- `GET /tasks/{task_id}` (and `GET /tasks/id?task_id=<id>`)
  Renders a task detail page. The alias carries the ids no path can address,
  — the ids that collide with a static page under `/tasks/`
  (`tasks.RESERVED_TASK_PATH_SEGMENTS`: `graph`, `events`, and the alias's own
  `id`). Starlette matches the static route first, so without it a task called
  `graph` would have no reachable detail page while every link to it silently
  opened the graph. The alias is a single static segment carrying the id in the
  QUERY, deliberately: ASGI percent-decodes before routing, so a
  `/tasks/id/<id>` form would itself be matched by an id like `id/graph` and
  serve the wrong task at HTTP 200. Slash-bearing and dot-segment ids keep the
  documented path and stay unroutable (Lithos b1a65c6d, closed won't-fix —
  every id Lens can be handed is a server-generated UUID).
  `tasks.task_detail_path` is the one place that decides, shared by the board,
  the graph page and the knowledge produced-by chip.
- `GET /tasks/{task_id}/findings`
  Renders the findings fragment used by the task detail page.
- `GET /tasks/{task_id}/blockers`
  Renders one expanded level of a task's blocker chain (HTMX fragment).
- `GET /tasks/graph`
  Renders the dependency graph of one scope — `?project=<slug>` or
  `?epic=<id>` — and, with no scope, a picker of the projects and open epics
  the snapshot observes. Registered BEFORE `/tasks/{task_id}`, which would
  otherwise match `graph` as a task id.
- `GET /knowledge`
  Renders the knowledge landing page: hybrid search, recently-updated notes,
  and tag browse.
- `GET /knowledge/resolve`
  Resolves a wiki-link target to a note, or renders the disambiguation /
  not-found page when it cannot.
- `GET /note/{knowledge_id}`
  Renders a note: server-side markdown, frontmatter metadata chips, the
  related panel, and provenance.

One further route, `POST /tasks/events/publish`, is registered **only** when
fake-Lithos app mode is enabled (`LITHOS_LENS_FAKE_LITHOS`). It is a harness
seam for the browser suite and does not exist in a normal deployment.

No authenticated routes currently exist. Lens takes unauthenticated requests
across a trusted-network boundary; see `docs/REQUIREMENTS.md` §5C.1. Two
process-level bounds exist in place of authentication: a concurrent-render cap
that answers 503 rather than queueing, and a ceiling on concurrent SSE
subscribers.

### 5.2 Configuration

Lens loads configuration from the first of:

1. the path in `LITHOS_LENS_CONFIG`, if set
2. `./lithos-lens.toml`
3. `~/.lithos-lens/lithos-lens.toml`
4. `/etc/lithos-lens/lithos-lens.toml`

A set of `LITHOS_LENS_*` environment variables override individual values after
the file is read; the containerized deployment uses `LITHOS_LENS_CONFIG` plus a
mounted data directory. Two further variables are read outside the config model
by the entry point: `LENS_PORT` and `LENS_HOST` select the bind, defaulting to
8000 and every interface.

The current configuration model includes:

- `storage.data_dir`
- `logging.level`
- `lithos.url`
- `lithos.mcp_sse_path`
- `lithos.sse_events_path`
- `lithos.agent_id`
- `tasks.auto_refresh_interval_s`
- `tasks.visible_cap`
- `tasks.frontier_limit`
- `tasks.default_time_range_days`
- `tasks.default_status_groups`
- `tasks.project_convention`
- `tasks.project_tag_key`
- `tasks.gate_waiting_attention_hours`
- `tasks.claim_expiring_soon_minutes`
- `tasks.stale_open_age_days`
- `tasks.unclaimed_ready_age_minutes`
- `tasks.dispatch_trigger_tag_prefixes`
- `graph.cache_ttl_s`
- `graph.max_tasks`
- `graph.fetch_concurrency`
- `graph.mini_graph_max_nodes`
- `knowledge.related_title_fanout_cap`
- `knowledge.search_limit`
- `knowledge.recent_limit`
- `events.enabled`
- `events.reconnect_backoff_ms`
- `llm.enabled`
- `llm.provider`
- `llm.model`
- `llm.api_key`
- `llm.base_url`
- `llm.extra_headers_json`
- `llm.max_tokens`
- `telemetry.enabled`
- `telemetry.endpoint`
- `telemetry.console_fallback`
- `telemetry.service_name`
- `telemetry.export_interval_ms`
- `ui.default_view`
- `health.refresh_interval_s`

Defaults, ceilings, and the env-override names are defined in
`src/lithos_lens/config_schema.py` and `src/lithos_lens/config.py`; the shipped
values are documented inline in `lithos-lens.example.toml`. Every integer knob
has a maximum as well as a minimum, so a mistyped value fails at load rather
than at render.

### 5.3 Tasks Dashboard

The Tasks view is the primary implemented feature in Lens. Since T1 it is
**graph-native**: the board is assembled from Lithos's computed ready and
blocked frontiers plus the master open list, not from a flat status listing.

Open work is partitioned into sections, and a row appears in exactly one of
them (the single-placement rule — a task that needs attention renders *only*
there, so an unsatisfiable task cannot be mistaken for one merely waiting):

- **Needs attention** — rows the severity model promoted (see below)
- **Ready** — on the ready frontier, nothing blocking
- **In progress** — claimed
- **Blocked** — on the blocked frontier, with its blockers named
- **Claims unknown** — claims were requested but not returned; the row says so
  rather than rendering a confident "unclaimed" chip
- **Unclassified** — in neither frontier, so Lens will not assert why

Completed and cancelled tasks render in their own lists over a resolved-at
window.

**Needs attention** applies six ordered rules, most severe first. Two are
intrinsic — `unsatisfiable` (a predecessor or gate was cancelled, so the task
can never become ready) and `cycle` (the blocking chain closes on itself) — and
four are threshold-driven from config: `gate-waiting`, `claim-expiring`,
`stale-open`, and `ready-unclaimed`. A promoted row carries one chip per rule
that fired, with a one-line supporting fact, and the list sorts by severity then
oldest-first within a tier.

`ready-unclaimed` carries one further condition beyond its threshold: the ready
task must carry a tag with one of `tasks.dispatch_trigger_tag_prefixes`, the
prefixes a fleet dispatches on. Untagged ready work is nobody's promise to pick
up, so it stays in Ready however old it is (rule 5 still covers "open too
long"), and the chip's supporting fact names the trigger tag it did find.
Configuring an empty prefix list widens the rule back to every ready task.

Two never-fire policies keep the list trustworthy: a timestamp Lens cannot
parse never triggers an age rule, and a degraded row is promoted only on
evidence its degradation cannot touch — a claims-unknown row is eligible for the
two structural rules alone, and an unclassified row is never promoted.

The dashboard also renders:

- a **Gates section** for open gates (timer, CI, PR, external, human), showing
  what each is waiting on; a human gate past its threshold is promoted into
  Needs attention instead, and the browser schedules a single refresh at the
  earliest still-future timer deadline
- an **Epic rollup strip** summarizing epics by child progress, with a scope
  link that filters the board to one epic
- **summary counters** for each section, marked as approximate when the
  frontier read they derive from was truncated

The dashboard is intentionally optimized for operational awareness rather than
deep paging through large historical task lists.

### 5.4 Task Filters

The current dashboard supports these filters:

- `status`
  Multi-select across `open`, `completed`, and `cancelled`.
- `claimed_state`
  `any`, `known_claimed`, or `known_unclaimed`.
- `tag`
  Free-text tag filter.
- `project`
  Project scope, honoring the configured convention (metadata key, reserved
  tag, or both).
- `epic`
  Scopes the board to one epic's children, from the rollup strip.
- `agent`
  Creating agent filter.
- `since`
  Creation-date lower bound.

Filter behavior:

- Filters are parsed by Lens and also applied defensively inside Lens after data
  is fetched from Lithos.
- `since` accepts ISO `YYYY-MM-DD` and UI-friendly `DD/MM/YYYY` input.
- The visible dashboard field renders `DD/MM/YYYY`.
- Open tasks, completed tasks, and cancelled tasks all honor the `since` filter.
- Clicking a task tag in list or detail view navigates back to `/tasks` with
  that tag as the only active tag filter.
- Existing `status`, `agent`, `since`, and `claimed_state` filters are
  preserved when clicking a tag, and carried across navigation into the detail
  and note views.
- Tags with the `project:` prefix are rendered with distinct visual styling but
  are otherwise filtered the same way as other tags.
- A filter query beyond a fixed byte budget is **refused** with a banner
  offering an unfiltered link, rather than silently trimmed — a partially
  applied filter would render a board that misrepresents its own scope.

### 5.5 Bounds on What Is Read and Shown

Lens is designed for deployments with tens to low hundreds of tasks, not
thousands, and every bound it imposes is stated in the UI rather than applied
silently.

- **Visible cap** — open-task counts represent all matching open tasks, not
  just visible rows; claim enrichment is attempted for visible open tasks only,
  and the dashboard surfaces when it could not be determined.
- **Frontier limit** — pushed into the ready/blocked frontier reads. When a
  read comes back truncated, the sections derived from it mark their counts as
  approximate, per side rather than board-wide, so a complete count is not
  labelled as an estimate because the other side was cut.
- **Link page size** — bounded neighbour lists on the detail page state the
  remainder they are not showing ("N more not shown"), because a "why can't
  this run?" list that quietly drops blockers is worse than a slow one.
- **Graph scope size** — a dependency-graph scope whose rendered node set
  (ghosts counted) is larger than `graph.max_tasks` is refused with that exact
  count rather than rendered unreadably; a task set already over the guard is
  refused before any edge is read at all. Nothing else decides what a page
  *contains*: the ghost-status reads behind the exact count are the
  classification itself (drop a completed predecessor, ghost a cancelled one),
  so within a render they are all made — leaving one unread would draw an
  `unknown` ghost where the contract requires the edge to be absent.

  A page is also refused when *classifying* it would cost more than one render
  may spend — more out-of-set endpoints than the read budget, or longer than
  the budget on that phase. This third refusal exists because the size guard is
  an availability guard, and an availability guard that itself requires
  unbounded work protects nothing: `task_edge_list` caps no edge count and edge
  endpoints are chosen by whoever wrote them, so a scope whose *node* set is
  comfortably inside `graph.max_tasks` can still name unboundedly many far
  endpoints. Refusing is the honest answer where a cheap one is not available:
  Lens says it did not classify the scope rather than rendering a graph whose
  missing reads show up as fabricated `unknown` nodes.

  What is bounded short of refusal is what the reads can cost everything else:
  **all graph reads across all concurrent renders share a fixed reservation of
  the MCP session** (half of it) — including an epic scope's own membership
  reads, which run before the per-render limiter exists and are the one path
  that could otherwise take the whole session — on top of the per-render
  `graph.fetch_concurrency` semaphore and a per-read deadline, so a large graph
  fan-out queues behind itself rather than timing out the dashboard, the detail
  page and the fleet's own traffic. Note that a per-read deadline does not start
  until the read acquires those gates, which is why the classification phase
  carries its own budget rather than relying on them. The upstream answer to the
  whole shape is a bulk graph read (§5.10).

This is a pragmatic operational dashboard model rather than a full audit UI.

### 5.6 Task Detail View

The task detail page is built on the same graph reads as the dashboard, so the
two cannot disagree about why a task is where it is. It shows:

- task title and body/summary content, status metadata, creating agent, created
  timestamp, tags, and claim state where known
- **why this task is here** — the Needs-attention reasons, when the board
  promoted it, with the same supporting facts the chips carry
- **blockers**, each labelled: a satisfied predecessor (the edge survives
  completion and is still shown, but never as a reason the task cannot run), an
  unsatisfiable one, or a cycle
- **the blocker chain**, expandable one level at a time to a bounded depth; a
  level that would revisit the chain reports the cycle instead of walking it
- **provenance** in both directions (`discovered_from`)
- **children**, for an epic
- **findings**, with links to any note a finding produced
- related note links where available

Every bounded list on the page states its own remainder through one shared
tail, so the page-size claim has a single definition.

Detail rendering is read-only in the current implementation.

### 5.7 Knowledge Surface

K1 replaced the minimal note path with a browsable knowledge surface.

`GET /note/{knowledge_id}` renders a note with:

- server-side markdown (headings, tables, code); raw HTML is escaped and
  `javascript:` hrefs are neutralized
- **wiki-links** (`[[target]]`) resolved through `/knowledge/resolve`, which
  renders a disambiguation page when a target is ambiguous and a not-found
  panel when it resolves to nothing
- **frontmatter metadata chips** (note type, status, access scope, namespace,
  confidence), a short-summary lede above the body, a `supersedes`
  back-reference, and an authorship line
- a **related panel** — the note's neighborhood, sectioned by relationship,
  with back-links and a bounded title fanout that falls back to bare ids past
  the cap
- a **produced-by chip** when the note came from a task and that task reads
  back successfully

`GET /knowledge` is the landing page: hybrid search over notes, a
recently-updated list, and tag browse.

### 5.8 Live Updates

Lens currently implements live task updates using SSE.

Current architecture:

- Lens opens a single shared upstream subscription to Lithos `/events`.
- Lens filters and normalizes task-relevant events.
- Lens republishes them to browser clients via `GET /tasks/events`.

The currently recognized event types are task-scoped:

- `task.created`
- `task.claimed`
- `task.released`
- `task.completed`
- `task.cancelled`
- `task.updated`
- `task.reopened`
- `finding.posted`

plus one system-scoped type, `agent.registered`, which is forwarded with an
empty `task_id` and never triggers a dashboard refresh (it invalidates the
agent-dropdown data only). Task-scoped events arriving without a `task_id` are
dropped with a warning.

On reconnect Lens sends `Last-Event-ID` so Lithos replays its ring buffer from
the last received event, and broadcasts one synthetic `lens.refresh` to browser
subscribers as the correctness backstop for gaps wider than that buffer. The
`lens.*` namespace is reserved for these Lens-internal synthetic events; Lens
sanitizes the id and type it puts on the wire, so an upstream payload cannot
forge a frame in that namespace (or any other).

Only an id that came from an upstream frame's own `id:` field, and that is
usable as a request header, is kept as the replay cursor — a Lens-synthesized
id is a browser dedupe key, not a position in Lithos's buffer — and the cursor
is dropped when a connection attempt fails before the stream comes up, so no
single value can wedge the hub in a permanent reconnect loop. Synthetic
refreshes are rate-limited to one per `LENS_REFRESH_MIN_INTERVAL_S` so a
flapping upstream cannot turn them into a refetch storm across open dashboards,
but they are only ever deferred, never dropped: reconnects inside a window
coalesce into a single broadcast delivered on its trailing edge, so every
disconnected interval — including one that gave up its replay cursor — still
results in a refresh.

Browser behavior currently includes:

- live status indicator
- optimistic task-row updates where practical
- fragment refresh/reconciliation when needed
- reconnect handling
- polling/degraded fallback behavior when live updates are unavailable

The event pipeline is task-focused. Lens does not yet expose a general-purpose
knowledge-event stream.

### 5.9 Health and Degraded States

Lens distinguishes several runtime states in the UI and internal health model:

- Lens application health
- Lithos reachability
- live event stream connectivity
- LLM enabled/disabled state

The Tasks dashboard surfaces these states so an operator can tell whether the
page is live, reconnecting, or degraded.

### 5.10 Task Dependency Graph Assembly

This is the data layer under `/tasks/graph` (§5.12), shared with every future
graph surface (the detail mini-graph, the side panel's impact line).

Lithos has no bulk graph fetch, so a graph is assembled one
`lithos_task_edge_list(task_id, direction="both")` call per node. Those calls
go through a **per-task edge cache** (`graph_cache.py`) on `AppState`:

- one entry per `task_id`, holding that task's deduped edge list and the
  instant it was read;
- a process-wide reservation on the shared MCP session that every graph read
  passes through — the cache's `edge_list` calls and the scope's ghost
  `task_get`s alike — so graph pages cannot take the whole session from the
  surfaces that are not graph pages (§5.5);
- a TTL of `graph.cache_ttl_s` measured on the monotonic clock (the wall-clock
  `fetched_at` is what the page shows, and a wall clock can step backwards),
  and single-flight, so concurrent readers of the same task share one upstream
  call — per generation: an eviction retires the flight, so a reader arriving
  after an event starts its own read rather than being answered from one that
  predates the event. Concurrent reads of one task id are capped; past the cap
  a reader waits for a slot and then reads for itself, so the bound costs
  latency rather than the invalidation guarantee;
- eviction driven by the event stream — the `EventHub` evicts a consumed task
  event's `task_id` **before** it fans the event out to browsers, and a
  `lens.refresh` flushes everything. Eviction also **retires** any read of
  that task already in flight, so the browser refresh the event triggers
  starts a new read instead of joining the one that predates it;
- a bounded number of entries, evicted least-recently-used, because which task
  ids get cached is chosen by the request rather than by Lens;
- a failed read is never cached (not even as an empty list): an empty edge
  list means "no edges", a failure means Lens does not know.

Edge upserts emit no upstream event, so the TTL is the staleness bound for an
edge another agent adds, and the cache records `fetched_at` so a page can say
when its picture is from rather than imply freshness.

A **scope** (`graph_scope.py`) is what one graph page would render, computed
over the master task list plus that cache and fanning out only for misses:

- **membership** — a project scope is the project's tasks per §5B.1, open only
  unless `include_resolved`; an epic scope is
  `lithos_task_children(recursive, include_closed)` plus the epic, with closed
  children included by default;
- **edge state**, read off both endpoints — `active` (dependent open,
  predecessor open or cancelled), `inactive` with its reason (`satisfied` when
  the predecessor completed, which takes precedence; else
  `dependent_resolved`), or `unknown` when an endpoint's status could not be
  read. Only `blocks` and `waits_on_gate` carry readiness meaning;
- **ghosts**, one hop and leaf-only — a ghost's own edges are never read, so
  the fan-out is bounded by the scope. Open far endpoints come from the master
  list at no cost; only resolved ones need a `task_get`, each far endpoint is
  read once however many edges name it, and a read that FAILS leaves the ghost
  shown with `status unknown` and `unknown` dependency edges. An inactive edge
  pointing out of the scope is dropped rather than ghosted, and context —
  the immediate out-of-set parent and `discovered_from` source of an included
  node — is added upstream only, never an out-of-set child or follow-on. A
  task the scope rule REMOVED is not an out-of-set task and names no ghost:
  an epic subtree is recursive, so `include_resolved=0` can exclude a closed
  child that is itself the parent of an open grandchild, and the upstream
  context rule would otherwise re-admit through that grandchild the very
  child the toggle promised to hide (edge included). Project scope excludes
  nothing this way — there a completed parent outside the open-only task set
  is a genuine out-of-scope task, and its context ghost is required;
- **completeness**, carried in the result rather than beside it — `incomplete`
  names every node whose edge read failed, such a node is never classified
  isolated, a ghost whose `task_get` failed is shown with `status unknown` and
  its dependency edges as `unknown`, and `as_of` is the oldest contributing
  `fetched_at`.

The demo fixture set (fake-Lithos app mode) carries a second cluster for this
layer: a dependency cycle, a cross-project `blocks` edge, a cancelled
predecessor, resolved predecessors inside and outside the window, an epic with
a child in another project, isolated tasks, and a chain of depth 5.

### 5.11 Task Graph Topology

`graph_layout` computes the shape of a fetched task graph — what `/tasks/graph`
(§5.12) renders. It is pure: a node set, the fetched edges, and Lithos's own
`task_blocked` verdict in; cycles, layers, roots, the longest blocking chain and
the hierarchy tree out.

- **Dependency edges** (`blocks`, `waits_on_gate`) are classified from BOTH
  endpoints: `active` (open dependent, open-or-cancelled predecessor — a
  cancelled predecessor blocks forever, a completed one blocks nothing),
  `inactive` with a reason (`satisfied` when the predecessor completed, else
  `dependent_resolved`; satisfied wins when both hold, because the dependency
  was met whatever the dependent then did), or `unknown` when either endpoint's
  status could not be read.
- **Cycles** — membership is Lithos's verdict and is never overridden: a task
  carrying a `kind="cycle"` blocker is a cycle member here even when Lens's own
  Tarjan pass finds no component, because a cycle closing through out-of-scope
  tasks is invisible to a graph that never fetches a ghost's edges. The shape —
  members ordered by `(created_at, id)` plus one representative path — is Lens's,
  and is empty for a flagged member with no component, where Lithos's message is
  all there is. The representative path is one linear DFS pass, not a search
  over the component's paths: these edges are agent-written, and a walk whose
  cost grew with the shape of the cycle would hand their author the render.
- **Layers** are Kahn's algorithm over the graph with each cycle condensed to a
  single node, so every cycle member gets a layer and its dependents are layered
  below it and marked *blocked via cycle* — a cycle never takes downstream work
  off the page. Layering uses every fetched dependency edge whatever its state,
  because layers describe the planned sequence rather than what blocks now.
  `roots` are the in-degree-zero condensations plus one representative per cycle.
- **The longest blocking chain** is the longest path by node count over the
  condensed **active** projection, so a completed three-chain never outranks the
  open two-chain beside it; a cycle counts once, ghosts count, and ties break on
  the smallest `(created_at, id)` sequence read forward, so the traced chain is
  stable across renders and focusing a node already on it does not move it. The
  active projection is condensed on its OWN components, not on the layering's:
  a cycle whose loop closes through a completed task is one node to the layers
  and a live chain here. The chain through a given node is available for focus
  mode.
- **The chain carries its own confidence.** It is a lower bound — rendered
  "≥ N", never as an exact claim — whenever a node's edges could not be read OR
  any `unknown` dependency edge exists **anywhere** in the fetched graph: an
  unknown edge sits in neither projection, so a disconnected one could itself be
  the longest active component, and "it is off the current chain" proves nothing.
- **The hierarchy tree** is the `parent_child` forest, indented. These edges
  carry no readiness meaning, so completion never drops one; a node whose parent
  is out of scope is a root, and a malformed parent loop yields a shorter tree
  rather than dropping tasks.

### 5.12 Task Graph Page

`GET /tasks/graph` renders one scope's dependency graph as **server-rendered
text**. That is the first-class baseline, not a fallback: the page is complete
and reviewable with no JavaScript, and the Cytoscape rendering (a later T2
slice) is enhancement drawn from the same embedded payload, so the picture and
the text cannot disagree.

**Scope and URL state.** `?project=<slug>` or `?epic=<id>`; with neither, the
page renders a picker listing every project the snapshot observes under both
§5B.1 conventions plus its open epics. `include_resolved` and `isolated`
default by scope KIND, opposite ways round — a project graph is about what can
still run (resolved hidden, isolates folded), an epic graph about an
initiative's progress (closed children shown, isolates open). `focus=` is the
page's single selection parameter and `selected=` is accepted as an alias it
canonicalises; `overlays=hierarchy,provenance` is carried for the client layer.
A scope over `graph.max_tasks` (ghosts counted), or one whose out-of-set
endpoints would cost more classification reads than one render may spend, is
**refused** with a "narrow your scope" panel naming the count — never rendered
degraded. Three of the four refusals happen after the edge fan-out, and each
still reports what discovering it cost (below), because a guard whose telemetry
claims a hundred reads were free cannot be tuned.

**What the page states, in this order:** the cycle callout and any
cycle-signal banner; the legend (one plain-language line per edge type
actually present, plus the ghost and cycle conventions); the longest blocking
chain; the topological layers as one `<ol>` per layer; the "N isolated tasks"
disclosure; the `parent_child` hierarchy tree, always rendered; and a
`<script type="application/json">` payload carrying nodes (with completeness
and layer), edges (with state and reason), layers, cycles, ghosts, the longest
chain with its `exact | lower_bound` flag, roots, isolated, incomplete and
`as_of`. The toolbar states `as_of` — the OLDEST contributing fetch — because
edge upserts emit no upstream event and the TTL is the staleness bound.

Each node row carries its status, type, claims and, for a ghost, its project
chip with links to the ghost's detail page and to its own project's graph. Its
incoming dependency edges are listed under it, because the text has no arrows
to read direction from: an `inactive` edge is faded and labelled with its
reason (`satisfied` / `dependent resolved`), and an `unknown` one names the
endpoint whose status could not be read — the predecessor, or the dependent
itself when it is the unresolved downstream ghost (both causes are D6's, and
the line states the one it has rather than always blaming the predecessor).

**Cycle authority is Lithos's** (§5.7). The page reads `lithos_task_blocked`
**scoped**, one read pair per project in the coverage set — every §5B.1
project among the in-scope tasks AND the downstream ghosts — where a pair is
`project=<slug>` plus, under the `"both"` convention, `tags=["<project_tag_key>:<slug>"]`,
each at `tasks.frontier_limit`, with `len == limit` treated as truncation. The
pair is unioned **per task**: the two calls are independent reads rather than
one snapshot, so a task's blockers are merged across every response that names
it (a `kind="cycle"` blocker arriving on either side is Lithos's verdict). A
task is cycle-status *known* when it appears in any response, or when some read
that **could have matched it** answered in full — and that is a question about
the convention each read expresses (§5B.1): `project=<slug>` is the metadata
convention, `tags=["<key>:<slug>"]` the tag one. An empty response from a
filter the task cannot match is not coverage, which is what a single-convention
posture makes load-bearing: under `"metadata"` only the `project=` half is
issued, so a child carrying only `project:<slug>` is covered by nothing and is
marked unknown rather than silently reported cycle-free.
A scoped read answers about a **project**, not about this graph, so it returns
rows for tasks the page never fetched and for ghosts whose own edges it never
read: only rows naming an **in-scope, non-ghost** task are this graph's
authority. A ghost carries neither cycle marker — believing its row would draw
a cycle for a node Lens has no edges for and then mark the real work below it
*blocked via cycle* on the strength of it.
The coverage set is read **whole or not at all**. It is derived from task tags,
so its size is chosen by whoever wrote the task rather than by whoever
configured the page — so a scope naming more projects than `max_tasks` (one
project per task is §5B.1's shape, so more projects than tasks means tags
rather than structure) is **refused before the first call**, with a project
count, rather than read in part and rendered: a prefix of the coverage set
would report the projects it skipped as cycle-free. Inside that guard the phase
is bounded in TIME by one deadline covering the fan-out gates as well as the
calls — an internal safety net, not a `[graph]` knob. A read that deadline
catches still **queued** is reported as *never made*, distinct from a read that
was issued and failed: both leave their tasks `cycle status unknown`, but only
one of them is a question Lithos was ever asked, and the banner and the counter
both say which.
Every covered task carrying a `kind="cycle"` blocker is marked *in a cycle*
with Lithos's own message whatever Tarjan found — and **only** such a task: the
row's marker reads the verdict, never the condensation, so an SCC Lens can draw
while every blocked read failed renders its group and its `cycle status
unknown` markers without a row ever claiming membership Lithos did not report.
A cycle Lens can see is bracketed in its layer and its dependents marked
*blocked via cycle*. A flagged cycle with no fetched component is split on the
blocker's own endpoint rather than on the absence of shape, and only one of the
two answers is a claim: when every partner Lithos names is outside the in-scope
task set the callout says "through tasks outside this scope" (D4's bounded
promise); otherwise it says *shape unavailable*, which asserts nothing about
where the loop runs or why the shape is missing — the blocker names one
immediate predecessor, so an in-scope one leaves the rest of the path unknown,
and a stale edge-empty cache entry is indistinguishable here from a failed edge
read. A truncated read, a read the phase deadline left unissued, a failed read,
and a task no scoped read can reach (no project under either convention) each
produce a **banner**; which ROWS are then
marked `cycle status unknown` follows the per-task rule above, not the banner —
a task a truncated response returned keeps its verdict, and a task another
complete applicable read covered stays known. Each partial-read banner
therefore states what that read was rather than which rows were marked (any
per-task rule there is false for some combination of outcomes), and one further
banner states the rule with its real count: *N tasks are marked cycle status
unknown — no response returned them, and no complete read that could have
matched them was made*. Never an implied "no cycle".

**Every partial claim is labelled.** A node whose edge read failed renders in
the layering with `edges unknown` and is never folded into the isolated
disclosure; a task Lithos flags as a cycle member is layered rather than folded
even when no edge of its own was fetched (D4 beats the edge-absence heuristic,
and the TTL makes that state reachable: edge upserts emit no event); a node
whose incoming edge is `unknown` **because its predecessor could not be read**
is marked *blocked by unresolvable predecessor* — an edge into an unreadable
ghost is equally unknown and says so about the endpoint it is actually about; and the chain line reads "≥ N, incomplete: K tasks'
edges unreadable, J edges unresolvable" whenever either applies. The chain is
labelled *within this graph* — Lens claims no corpus-wide critical path.

**Reads per render:** one `lithos_task_list(status="open", with_claims=true)`,
plus the two bounded `resolved_since` windows for the **picker** (§5B.1's
project universe is open tasks *plus* the resolved window, so a project whose
last task finished yesterday is still offered) and for a project scope that
asks to include resolved tasks; the scope's cached `edge_list` fan-out and
ghost `task_get`s (§5.10); `task_children` for an epic scope, whose open
children are merged with their master rows so the claims those reads do not
carry are not rendered as "unclaimed"; and the coverage set's blocked read
pair.

Each render opens one **`lens.tasks.graph`** span — the multi-phase exception
to §8's no-child-span rule — carrying `lens.graph.*`: scope kind and key,
outcome, node / edge / ghost / cycle / isolated counts, chain length and
exactness, whether the cycle signal was incomplete, and this render's own cache
hits, misses, ghost reads and fan-out (counted per request through a tally
passed down the call, never as a delta of the process-wide cache counters,
which a concurrent render also moves; ghost reads are counted where the call is
ISSUED, so a classification phase stopped by its deadline reports what it
actually spent). A refusal carries the same counts plus its reason. Two counters accompany it:
`lens_tasks_graph_renders_total` (`scope`, `outcome`) and
`lens_tasks_graph_cycle_reads_total` (`outcome`), whose total is the scoped
blocked calls actually ISSUED — a plan item the phase deadline caught still
queued is not one of its three outcomes, and is carried by the span field
`lens.graph.cycle_reads_unmade` instead. The scope KEY is a span
attribute only: one Prometheus series per project is the cardinality failure
§8's rule exists to prevent.

## 6. Current Lithos Dependencies

Lens currently assumes the availability of an existing Lithos deployment that
provides:

- a reachable base HTTP URL
- task listing and task-status read capabilities
- **task-graph reads**: the computed ready and blocked frontiers with
  classified blockers, task types (`task`/`epic`/`gate`), typed task edges, and
  children — the Lithos 0.4 surface the whole graph-native dashboard rests on
- note read, search, and neighborhood capability for the knowledge surface
- agent registry/statistics endpoints used by the dashboard
- an `/events` SSE stream carrying task-related events

Lens is intentionally conservative in what it assumes from Lithos. When data is
ambiguous or partially missing, Lens treats parsing and enrichment as best
effort and continues rendering what it can — and says so on the surface rather
than presenting a degraded read as a complete one.

Every request and response shape for these calls is pinned by a vendored
contract under `tests/contracts/`, transcribed from the Lithos source with a
citation. A client method without a contract fails the suite, so fakes and
fixtures cannot drift from the payloads the server actually sends.

## 7. Frontend Model

The current frontend is server-rendered HTML with progressively enhanced
JavaScript.

Key characteristics:

- FastAPI + Jinja templates for primary rendering
- static CSS for presentation
- lightweight browser JavaScript for SSE, fragment refresh, and date-picker
  synchronization
- no SPA framework

The application is designed to remain usable in partially degraded conditions
even if live updates are unavailable.

## 8. Observability

Lens currently includes:

- structured JSON logging at a configurable level, every record stamped with
  the `trace_id` / `span_id` of the request that produced it when one is active
  (the same record exported over OTLP reaches Loki as `traceid` / `spanid`,
  written by the collector's exporter from the OTLP record's native trace
  context — a query has to match whichever sink it reads)
- task filter/debug logging around dashboard requests
- OpenTelemetry traces, metrics and log export to an OTLP collector

OpenTelemetry is a **required** dependency, not an optional extra, and is on by
default. `telemetry.enabled` governs export rather than whether the
instrumentation exists: with no endpoint configured, providers are still
installed and spans still carry ids, which is what keeps logs correlatable on a
machine with no collector running. Setting it false is the one escape hatch.

HTTP server spans come from `opentelemetry-instrumentation-fastapi`, which
names spans and sets `http.route` from the route TEMPLATE — `/note/{knowledge_id}`,
not the concrete path. That is a hard requirement rather than a convenience:
Tempo's metrics generator turns span names into Prometheus series, so a name
carrying a note id would mint one series per note. The same rule governs
metric labels — bounded sets only (route template, tool name, outcome enum);
unbounded values belong on spans, which are stored per-trace and never become
series.

Three request paths are deliberately untraced: `/health` (polled by the
container healthcheck and by every page render, so constant volume carrying no
information), `/static/` (served from disk, no Lithos call), and `/tasks/events`
(an SSE stream that lives as long as the browser tab — its span would stay open
for hours and sit in every latency histogram, making p95 meaningless).

Metrics are Prometheus-native (`lens_*_total`, `lens_*_seconds`), and follow
Lens's failure modes rather than its routes:

- **Lithos transport** — per-tool call counts by outcome (`ok` | `timeout` |
  `tool_error` | `transport_error` | `cancelled`), call latency, time queued at
  the process-wide call gate, reconnects, and `lens_lithos_session_up`.
- **Event hub** — events published by type and delivered per subscriber; drops
  by reason (`no_task_id`, `oversized_frame`, `subscriber_queue_full`,
  `content_encoding_refused`, `subscriber_limit`); the current subscriber count
  against its ceiling; and `lens_event_stream_up`.
- **Admission control** — metered requests by outcome (`admitted` | `refused`).
- **Task graph** — page renders by scope kind and outcome (`rendered` |
  `refused` | `picker` | `offline` | `error`), and the scoped blocked reads
  behind the cycle signal by outcome (`ok` | `truncated` | `failed`), so "how
  often is this signal partial here?" is answerable without reading banners off
  screenshots.

The drop counters do not replace the rate-limited warnings §8 describes above,
they complete them: rate limiting is correct for the log and it necessarily
discards the RATE, since the record carries a running total. The log keeps the
readable detail; the counter carries how often.

Two connections, two gauges. `lens_lithos_session_up` is the MCP tool session;
`lens_event_stream_up` is the `/events` SSE stream behind the `events` health
field. They can disagree — tool calls healthy while event delivery reconnects
presents as a board that renders but never updates — so neither substitutes for
the other.

All three gauges (these two and `lens_event_subscribers`) are **observable**:
the owning object registers a callback, and the SDK reads the authoritative
state at every collection. They are not written at transitions. A synchronous
gauge reports only a value set since the last collection, so one written at
transitions stops being exported as soon as the transitions stop — and a
session that is simply CONNECTED produces none. Measured against the shared
stack, `lens_lithos_session_up` expired out of Prometheus about five minutes
after connecting, while the process was healthy and its counters were still
exporting. Seeding to 0 before the first connection attempt distinguishes
"never connected" from "not deployed", but only until the first collection
after the last transition, which is not when anyone is looking. With a callback
the gauge answers whenever it is asked, and an absent series once again means
what it should: nothing is reporting.

Every histogram carries explicit bucket boundaries (`telemetry.HISTOGRAM_BUCKETS`),
because the SDK's defaults — `(0, 5, 10, 25, … 10000)` — are shaped for
milliseconds and Lens records seconds. Left on the defaults, every healthy
observation falls into the single bucket `(0, 5]` and `histogram_quantile`
interpolates inside it, reporting a p50 of 2.5s and a p95 of 4.75s whatever the
real latency was. Those numbers look like measurements and are not: against the
live stack, calls averaging 130ms and a call gate averaging 2.8 microseconds
both reported p95 = 4.75s. Boundaries are chosen per instrument from where the
mass actually sits, with a boundary ON each operational threshold — the 15s call
deadline, the 20-read related-panel cap — so "at the limit" is a bucket edge
rather than something interpolated across.

Every metric label comes from a bounded set: a tool name from Lens's own client
surface, an outcome enum, or an event type mapped through Lens's allowlist
(anything else becomes `other`). This is enforced by a test, not assumed —
`publish` is a public method and fake-Lithos app mode exposes a route that
builds an event straight from request JSON.

- **Knowledge surface** (the K1 PRD's three points) — note renders by outcome
  (`rendered` | `not_found` | `error` | `offline`), related-panel latency and
  backend fan-out, landing requests by branch (`search` | `browse` | `offline`
  | `error`), and wiki-link resolutions by the arm that decided (`uuid` |
  `path` | `title` | `disambiguated` | `unresolved` | `empty` | `offline`).

That last one needs `ResolveOutcome.via`, added for it: `kind` answers
`redirect` for the uuid, path and single-title arms alike, so the distinction
the PRD asks for did not previously survive. It matters because a corpus
resolving entirely by uuid is working for a different reason than one resolving
by title, and only the second is evidence the wiki-link convention is being
used as intended.

These three set attributes on the request's own server span rather than opening
child spans. A child span would nest ~1:1 with the server span for a handler
that does one unit of work, and Tempo's metrics generator mints a Prometheus
series per span name — the duplication removed in #71 for the ASGI `http send`
children, for the same reason. A child span earns its place when a handler
grows a phase worth timing separately from the request.

Search queries are kept off metric labels entirely (one series per distinct
search is the cardinality failure the rule above exists to prevent). On spans
they are retained but **bounded** by `MAX_LOGGED_VALUE_CHARS`, applied through
the instrumentation's request hook: the auto-instrumentation copies the request
target onto the span verbatim, so the unbounded query string that `logging.py`
bounds for the log was reaching the collector unbounded by a path that never
inherited that ceiling.

Warnings driven by conditions Lens does not control — a stalled browser queue,
a malformed upstream event, a refused subscriber — are rate-limited in time,
one record per condition per interval, each carrying a running total and the
count suppressed since the last. The upstream chooses how often these fire; it
does not get to choose how fast Lens writes to the operator's log.

## 9. Testing State

The current repository includes meaningful automated tests for common
application wiring, the graph-native dashboard and its severity model, task
filtering and rendering, the knowledge surface, SSE normalization and fan-out,
transport bounds, and admission control.

Beyond ordinary unit and integration tests, three mechanisms carry weight:

- **Guardrails as tests** — `tests/guardrail/` regenerates the component
  diagram, domain model, and architecture metrics from the code, and CI fails
  when the committed artifacts drift. Hard budgets in `docs/architecture.toml`
  (module size, cross-component edges, cycles, cross-module private reaches)
  fail the build when breached; raising one is an explicit, reviewed edit.
- **Contracts** — see §6.
- **Browser suite** — a Playwright suite runs the real application in
  fake-Lithos mode and captures screenshot artifacts at four viewport widths.

The implemented tests exercise real behavior with lightweight fakes rather than
shallow mock-only checks, and the working practice is to demonstrate a new
guard fails when reverted rather than assuming it binds.

## 10. Known Gaps Relative to Requirements

The following requirement areas are not yet implemented in the current state:

- **write actions of any kind** — every surface is read-only (T3)
- knowledge graph view and knowledge event wiring (K2)
- cognitive search (`lithos_retrieve`) and node stats (K3)
- feed, feedback, and cited-by panel (K4)
- archive-backed file serving and in-browser document viewing
- saved reading paths
- LLM-assisted curation, summaries, or browsing assistance (X1) — the LLM
  config block exists and is disabled by default; nothing consumes it
- authentication

- the task dependency graph's **interactive** layer — T2: `/tasks/graph`
  renders its server-rendered text baseline (§5.12) over the assembly (§5.10)
  and topology (§5.11) beneath it, but the Cytoscape canvas, exploration mode,
  the shared side panel and the detail mini-graph are later slices

One gap is narrower than a milestone and tracked as a task:

- the fake↔real contract matrix runs manually against a live server rather
  than on a schedule against a seeded one

This belongs to a future milestone and should not be assumed to exist merely
because they are described in `docs/REQUIREMENTS.md`.

## 11. Compatibility Statement

This specification describes the behavior of Lithos Lens `0.3.0` as currently
implemented in this repository — the 0.1.0 foundation plus the **T1**
graph-native operator view and the **K1** knowledge note view and search.

If the implementation and this document diverge, the implementation should be
treated as authoritative in the short term and this specification should be
updated to realign with shipped behavior.
