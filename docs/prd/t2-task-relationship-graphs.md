---
title: T2 — Task Relationship Graphs
milestone: T2
status: draft
target_version: 0.4.0
references:
  - docs/ROADMAP.md (milestone sequence; upstream dependency ledger — gaps #1, #3)
  - docs/REQUIREMENTS.md §5.5 (task detail: side panel, mini-graph), §5.7 (task graph page), §5.8 (event pipeline), §5B (project conventions)
  - docs/SPECIFICATION.md §5.3–§5.8 (T1's shipped behaviour, which T2 extends)
  - docs/prd/t1-graph-native-operator-view.md (what T1 deferred here: Out of Scope)
  - lithos docs/SPECIFICATION.md §5.4 (task tools), §8 (events)
tracked_in: lithos
task_tags: [project:lithos-lens, milestone:t2]
labels: [milestone-t2, tasks-view]
epic: 44a943fc-6055-4603-b3d7-9aabdecd73e9
depends_on: [T1]
supersedes: the three-strand draft of 2026-09-02 (narrowed 2026-09-04 — see Further Notes)
---

# T2 — Task Relationship Graphs

## Problem Statement

T1 made the dashboard graph-native: every open row sits in the section the
ready/blocked frontier puts it in, and the detail page says why. What T1
deliberately left text-only is the *shape* of the graph — and the live corpus
(~330 open tasks across ~20 projects, live cross-project `blocks` chains, 21
epics) has a shape that a list cannot show. Concretely:

- **There is no way to see a project's dependency structure.** The blocker
  chain on a detail page walks one task's ancestors, one level at a time.
  "What does this project's month look like, and what is its longest
  blocking chain?" has no surface. The operator answers it today by opening
  detail pages one at a time, or by asking an agent.
- **Cross-project dependencies are invisible from either side.** A loom task
  that blocks three lens tasks shows up nowhere as such; project boundaries
  hide exactly the relationships that bite.
- **"What does finishing this free?" is unanswerable.** The detail page
  shows blockers (upstream) but nothing downstream, so the highest-leverage
  completion is a guess.
- **Every row click is a page navigation.** Exploring relationships means
  leaving the board and losing your place.

Two constraints shape everything below. Lithos has **no bulk graph fetch**
(ROADMAP ledger gap #3): a graph is assembled from one `lithos_task_edge_list`
call per node. And **edge writes emit no event** (gap #1): an edge another
agent adds is invisible to Lens until something else happens to either
endpoint. T2 works inside both rather than around them, and says so on the
surface.

T2 is a **relationship-first** release. The operational analytics that
shared its earlier draft — planning view, findings feed, stalled/overload/
throughput, human identity, title badge — are a separate follow-up
(ROADMAP **T2b**) so that the graph's value can be judged on its own.

## Solution

`/tasks/graph?project=<slug>|epic=<id>` renders the dependency graph of one
scope, assembled from a **per-task edge cache** shared by every surface that
reads edges. The page's baseline is server-rendered text: a cycle callout,
topological layers computed with strongly-connected components condensed so
cycle members and their dependents still get a layer, the longest blocking
chain, and the `parent_child` hierarchy tree. Cytoscape (already vendored,
3.30.3) is progressive enhancement over the same embedded JSON payload,
with arrowheads, a plain-language legend, dependency edges by default and
hierarchy/provenance as overlays, isolated tasks folded away, and a real
exploration mode: focus a node to light its ancestors and descendants,
search to jump, click to open the **side panel**.

The side panel is one implementation for dashboard rows and graph nodes:
`?selected=<id>` server-renders it open, and it shows the task's blockers,
its dependents, its parent epic, its **downstream impact within the scope**
("frees N, M immediately"), and a link to the full page. The detail page
gains a two-up-one-down **mini-graph** above its text blocker chain,
rendered by the same client module.

Cross-scope endpoints render as one-hop ghost leaves; satisfied edges are
dropped; Lithos's `task_blocked` stays the authority on cycle membership
while Lens computes the shape of what it can see, and every place the data
is stale, capped, or unreachable says so on the page.

```
/tasks/graph?project=lithos-loom&focus=<id>
┌──────────────────────────────────────────────────────────────────────┐
│ Graph · lithos-loom   [search ▁▁▁▁]  [resolved ☐] [hierarchy ☐]       │
│                       [provenance ☐] [3 isolated tasks ▸]            │
│ ⚠ 1 dependency cycle: A → B → A          as of 14:02:11              │
│ Legend: A ──▶ B means "A blocks B"; ╌╌▶ waits on gate; ghost = other  │
│         project (one hop)                                            │
├──────────────────────────────────┬───────────────────────────────────┤
│ (Cytoscape canvas — layered,     │ ▸ Implement BLE          [expand] │
│  focus lit: ancestors/descendants│   open · claimed agent-zero       │
│  bright, rest dimmed; longest    │   Blocked by: Design schema (open)│
│  chain traced)   [show as text]  │   Blocks: Ship 0.4, Write docs    │
│                                  │   Parent: EPIC loom-arch          │
│                                  │   Frees 4 in scope, 2 immediately │
│                                  │   On the longest chain (3 of 5)   │
├──────────────────────────────────┴───────────────────────────────────┤
│ Longest blocking chain (5): influx#41 → Design schema → Implement BLE │
│                              → Ship 0.4 → Announce                    │
│ Layer 0  ● influx#41 (ghost · influx)   ● Design schema (open)        │
│ Layer 1  ▶ Implement BLE (claimed)      [cycle: A ⇄ B]                │
│ Layer 2  ◼ Ship 0.4 (blocked)  ◼ Write docs (blocked via cycle)       │
│ Hierarchy: EPIC loom-arch ├─ Design schema ├─ Implement BLE …         │
└──────────────────────────────────────────────────────────────────────┘
```

## User Stories

### Graph page

1. As an operator, I want `/tasks/graph?project=<slug>` to render the
   project's open dependency graph, so that I can see the shape of a
   project's work instead of inferring it from rows.
2. As an operator, I want `/tasks/graph?epic=<id>` to render an epic's
   subtree including its closed children, so that I can see an initiative's
   progress and sequencing in one picture.
3. As an operator, I want the unscoped `/tasks/graph` to offer a scope
   picker (projects from the snapshot, open epics), so that the page is
   reachable from the nav without a bookmark.
4. As an operator without JS (or in a PR screenshot, or a screen reader), I
   want the graph as topological text layers with status and type per task,
   plus the longest blocking chain and the hierarchy tree, so that the
   page's baseline is complete and reviewable on its own.
5. As an operator, I want a dependency cycle rendered as a bracketed group
   inside its own layer with its members in a deterministic order plus one
   representative path, and its dependents layered below it marked "blocked
   via cycle", so that a cycle never makes its downstream work vanish — and
   when Lithos reports a cycle Lens cannot see through its ghosts, or the
   scoped blocked read truncates or fails, I want the page to say so rather
   than imply "no cycle".
6. As an operator, I want an edge whose far endpoint is outside the scope
   to render that endpoint as a dimmed ghost node carrying its project chip,
   with links to its detail page and to *its* project's graph, so that
   cross-project dependencies are visible and crossable by choice.
7. As an operator, I want a satisfied edge (predecessor `completed`) dropped
   from the default graph and restored by an `include_resolved` toggle, so
   that the default picture shows what can still run.
8. As an operator, I want the **longest blocking chain** in the scope
   computed and named (in text) and traced (on the canvas), so that
   "what is the critical sequence here" is answered by arithmetic, not by
   eye.
9. As an operator, I want a scope larger than `[graph].max_tasks` refused
   with a "narrow your scope" panel naming the count, so that a graph is
   never rendered unreadably.

### Interactive rendering

10. As an operator with JS, I want the same graph drawn by Cytoscape —
    layered from the server-computed layers, colour = status, shape = type,
    **arrowheads on every edge**, and a persistent plain-language legend
    ("A ──▶ B means A blocks B") — so that edge direction cannot be
    misread and the picture and the text cannot disagree.
11. As an operator, I want `blocks` and `waits_on_gate` drawn by default,
    with `parent_child` and `discovered_from` as toggleable overlays, so
    that the default view is dependency flow and not a hairball of
    hierarchy.
12. As an operator, I want tasks with no dependency edge in the scope
    folded into an "N isolated tasks" disclosure — hidden by default on a
    project graph, shown by default on an epic graph — so that unrelated
    work does not crowd the relationships, and epic progress is not
    misrepresented.
13. As an operator, I want `?focus=<id>` to be an **exploration mode**:
    the node centred, its ancestors and descendants lit, everything else
    dimmed, and the longest chain through it traced — so that "what
    surrounds this task" is one URL I can share.
14. As an operator, I want a search box that jumps to a task by title or id
    within the scope (setting `focus=`), so that a hundred-node graph is
    navigable without scanning.
15. As an operator, I want click on a node to open the side panel and
    double-click to navigate to the detail page, so that the graph is a
    navigation surface, not a poster.
16. As an operator, I want a "graph changed — refresh" pill when a task
    event touches a node on the page, rather than an auto re-layout under my
    cursor, so that the graph stays stable while I read it.

### Side panel and mini-graph

17. As an operator, I want clicking a dashboard row or a graph node to open
    a side panel (`?selected=<id>`) with the task's status and claims, its
    blockers and their live status, its dependents, its parent epic, and an
    Expand link — and I want `GET /tasks?selected=<id>` to render it open
    server-side — so that browsing relationships never means leaving the
    board.
18. As an operator, I want the panel to state the task's **downstream
    impact within the current scope** — "frees N, M immediately", with M
    from Lithos's sole-blocker fact — so that the highest-leverage
    completion is named honestly rather than guessed.
19. As an operator, I want the detail page to show a mini-graph of the
    task's blockers two hops up and dependents one hop down, capped with an
    honest remainder and a focus link to the full graph, so that "why can't
    this run" and "what does finishing this free" are one glance.

## Implementation Decisions

Every decision below was settled with Dave (2026-09-02, narrowed
2026-09-04) and is not re-opened per slice.

### D1. Scope: one strand, relationship-first

0.4.0 ships the graph page, its interactive rendering and exploration mode,
the shared side panel, and the detail mini-graph. Planning view, findings
feed, stalled/overload/throughput, human identity and the title badge are
**T2b** (ROADMAP §3). Their durable requirements stay in REQUIREMENTS §5A,
§5.8.4 and §5.9 — including the honesty decisions already recorded there
(open-age proxy, stalled fires on evidence only, per-metric truncation
degradation) — and their PRD is written when T2b comes up.

### D2. Per-task edge cache, not a per-scope snapshot

A new Foundation module (`graph_cache.py`) holds one entry per `task_id`:
its deduped `lithos_task_edge_list(task_id, direction="both")` result, with
a TTL of `[graph].cache_ttl_s` (30s) and single-flight (two concurrent
readers of the same task share one in-flight call). It lives on `AppState`
beside the `EventHub`.

A **scope** is a pure function over the master task list and the cache
(`graph_scope.py`): it fans out only for cache misses, under the
`[graph].fetch_concurrency` (16) semaphore. Project graph, epic graph and
the detail mini-graph are all scopes over the same cache, so a project page
warms ~100 entries and the mini-graph reuses whatever is warm. There is no
per-scope memoisation. A page scope is refused above `[graph].max_tasks`
(300, ghosts counted) with the "narrow your scope" panel.

**Invalidation, stated honestly:**
- Any consumed task event carrying a `task_id` evicts that task's entry (a
  hook the hub calls before fan-out). A `lens.refresh` flushes everything.
- **Edge upserts emit no event** (ledger gap #1). An edge added by another
  agent is invisible until the TTL expires or a task event lands on either
  endpoint. The "graph changed" pill fires on task events only. The
  staleness bound is the TTL, and the page shows "as of HH:MM:SS".

This is one new cross-component edge (Events → GraphCache) against the
`cross_component_edges` budget in `docs/architecture.toml`; raise it with the
reason in the same diff.

### D3. Text is the first-class baseline

The graph page renders, in order: the cycle callout (if any) and any
cycle-signal banner; the legend; the longest blocking chain; the
topological layers as ordered lists (status, type, claim, project chip for
ghosts; isolated tasks in their disclosure); the `parent_child` hierarchy
tree; and a `<script type="application/json">` payload `{nodes, edges,
layers, cycles, ghosts, longest_chain, isolated, as_of}`. Acceptance
criteria and pytest assert on the text; the Cytoscape layer is verified
through the e2e visual-review artifacts (`e2e/`, `develop_artifacts_path`)
lens already runs through loom. When Cytoscape initialises it draws from the
payload and collapses the text behind a "show as text" toggle; the text
stays in the DOM.

### D4. Cycles: Lithos is the authority, Lens computes the shape

- **Authority.** The page reads `lithos_task_blocked` **scoped to the
  page**: for a project scope, `project=<slug>` and, under the `"both"`
  convention, a second read with `tags=["project:<slug>"]`, unioned
  (§5B.7's pattern). For an **epic** scope, one such read pair **per
  distinct project observed in the subtree** (Lens supports multi-project
  tasks; an epic's children need not share its project) — a handful at
  most, unioned. Each read uses `limit=frontier_limit`; `len == limit` is
  truncation (the dashboard's rule). Every in-scope task whose blockers
  include `kind="cycle"` is marked *in a cycle* with the blocker's
  `message`, regardless of what Lens's topology finds. A truncated or
  failed read renders a banner ("cycle signal incomplete: blocked read
  truncated / unavailable for project X") and gives every in-scope task
  absent from the response — or belonging to a project whose read failed —
  a `cycle status unknown` marker, never an implied "no cycle".
- **Shape.** `graph_layout.py` (pure) runs Tarjan's SCC over the `blocks` +
  `waits_on_gate` edges of the **fetched topology** (in-scope nodes' edges;
  ghost edges are never fetched, D5). Every SCC of size > 1, or a
  self-loop, is a cycle Lens can draw. The promise is bounded: a cycle
  through two or more out-of-scope tasks (`A(in) → B(ghost) → C(ghost) →
  A`) is invisible to SCC; such a task is still marked by the Lithos signal,
  listed in the callout under *cycle through tasks outside this scope*, and
  never dropped.
- **Layering.** Kahn's runs with each SCC condensed to one node, and each
  Lithos-flagged member outside any SCC condensed alone, so every cycle
  member receives a layer and its dependents are layered below it, marked
  "blocked via cycle". Display order is deterministic: members sorted by
  (`created_at`, `id`) plus one representative path (DFS from the smallest
  member over adjacency sorted the same way). The payload marks members
  with a `cycle` id; Cytoscape draws them in a compound parent with T1's
  `cycle` styling. `roots` = every in-degree-zero node of the condensed
  graph plus one representative per *cyclic* condensation.

SCC and longest chain are topology over an edge set, not readiness; cycle
*membership* stays Lithos's. **Lens still never re-implements the readiness
predicate.**

### D5. Ghost nodes: one hop, leaf-only

A ghost is the far endpoint of an edge whose near endpoint is in scope.
Lens never fetches a ghost's own edges, so a ghost is always a leaf on its
far side and fan-out is bounded by the scope. Ghosts participate in
layering and in the longest chain (a chain may start at a ghost). Open
ghosts get title/status from the master open list; only resolved far
endpoints need a `task_get`, through the same semaphore. Ghosts count
toward `max_tasks`. A ghost renders dimmed with its project chip and two
links: its detail page and `/tasks/graph?project=<its slug>`. No
configurable hop count; the cost is the bounded cycle promise in D4.

### D6. Scope membership and satisfied edges

- Project scope: the project's tasks per §5B.1 (epics and gates included),
  **open only by default**; `include_resolved=1` adds tasks resolved within
  the dashboard's `resolved_since` window.
- Epic scope: `lithos_task_children(epic, recursive=True,
  include_closed=True)` plus the epic; **closed children included by
  default**; same toggle, opposite default.
- **A satisfied edge is dropped, not ghosted.** An edge whose predecessor is
  `completed` says nothing about what can run; when the predecessor is out
  of scope the edge goes. Ghosting applies to far endpoints that are `open`
  (live blocker) or `cancelled` (T1's unsatisfiable case — a graph that hid
  it would contradict the dashboard). All four edge types follow the rule.

### D7. Longest blocking chain

Over the condensed DAG in topological order, longest path by node count
through `blocks` + `waits_on_gate` (a cyclic condensation counts as one
node; ghosts count). Ties break by the smallest (`created_at`, `id`) at
each step, so the chain is deterministic. Rendered as one line of text
("Longest blocking chain (5): A → B → …"), traced on the canvas, and, in
focus mode, the longest chain *through the focused node* replaces it. The
panel says "on the longest chain (k of n)" when the selected task is on it.
This is labelled "within this scope" everywhere — it is not a corpus-wide
critical path, and the PRD does not claim one.

### D8. Interactive rendering

- **Edges:** arrowheads always; `blocks` solid, `waits_on_gate` dashed;
  `parent_child` (thin, light) and `discovered_from` (dotted) are overlays,
  **off by default**, toggled in the toolbar and remembered in the URL
  (`overlays=hierarchy,provenance`). The text hierarchy tree is unaffected
  by the overlay toggle; it is always rendered.
- **Legend:** persistent, plain language, one line per visible edge type,
  plus the ghost and cycle conventions.
- **Isolated tasks:** a task with no `blocks`/`waits_on_gate` edge in the
  scope is *isolated*. Project scope hides them by default; epic scope
  shows them by default; both render an "N isolated tasks" disclosure and a
  toggle (`isolated=1|0`). Isolated tasks still count toward `max_tasks`
  and still appear in the payload.
- **Focus mode (`focus=<id>`):** centre the node, class its ancestors and
  descendants (transitive, within the fetched topology) as lit and every
  other node as dimmed, trace the longest chain through it, open the panel.
  Escape or clearing the search returns to the unfocused view.
- **Search:** a toolbar input matching title substring or id prefix over
  the payload's nodes; selecting a match sets `focus=` via `pushState`.
- **Events:** the page matches event `task_id`s against its node ids and
  shows the "graph changed — refresh" pill; it never re-layouts on its own.
- **Layout:** `breadthfirst` with the server's `roots`; no physics.

### D9. Side panel: one implementation for rows and nodes

`GET /tasks?selected=<id>` server-renders the panel open (the no-JS
baseline). A row click or a node click fetches `GET /tasks/{id}?fragment=
panel` (same template as the detail page, partial block) and pushes
`selected` onto the URL; close clears it and preserves list and graph
state; Expand navigates to `/tasks/{id}`. The panel shows: header (title,
type badge, status, project chip), active claims, **blockers** with live
status (the T1 text chain, level 1), **dependents** (outgoing
`blocks`/`waits_on_gate`, level 1, with status), **parent** breadcrumb,
**downstream impact** (D10) when opened from a graph or with a scope in the
URL, and findings count with a link. On the graph page the panel sits
beside the canvas; on the dashboard it overlays the right edge, as
REQUIREMENTS §5.5 specifies. The mini-graph initialises on `htmx:afterSwap`
when the panel is opened from the detail route.

### D10. Downstream impact, scoped and labelled

For the selected task, N = open transitive dependents via `blocks` +
`waits_on_gate` **within the current scope's fetched topology** (ghosts
included as leaves), M = those whose scoped `task_blocked` entry lists this
task as their sole unsatisfied blocker. Rendered "frees N in scope, M
immediately". When the blocked read for the relevant project truncated or
failed, M is withheld and the label says why. No corpus-wide keystone: that
needs a corpus scope with its own bound and is T2b's business. Gates are
eligible (completing a gate is the operator's own move); epics carry no
`blocks` edges and show no impact.

### D11. Detail mini-graph: two up, one down

Incoming `blocks`/`waits_on_gate` to depth 2, outgoing to depth 1, the
parent epic as a single labelled node, no `discovered_from`. Capped at
`[graph].mini_graph_max_nodes` (40) through T1's shared remainder tail,
with a link to `/tasks/graph?project=<slug>&focus=<id>`. Same cache, same
payload shape, same client module (arrowheads, legend, colour/shape
vocabulary); its text baseline is the existing blocker chain plus a new
"Blocks:" line listing level-1 dependents, so it renders no layers of its
own.

### D12. No server-side recompute in T2

The graph page's pill and the panel refresh through the existing
client-side debounced reconcile in `tasks.js`. Server-side recompute is
sequenced at X1 (REQUIREMENTS §5.8.4).

### Config

```toml
[lithos-lens.graph]
cache_ttl_s = 30                   # per-task edge cache TTL
max_tasks = 300                    # page-scope size guard (ghosts count)
fetch_concurrency = 16             # edge_list fan-out semaphore
mini_graph_max_nodes = 40          # detail mini-graph cap
```

Env overrides follow the shipped `LITHOS_LENS_<SECTION>_<KNOB>` convention
(`tests/test_config_env_prefix.py` guards docs↔config parity).
`corpus_max_tasks` is **not** introduced in T2; it belongs to T2b's corpus
scope.

### Routes

| Endpoint | Purpose |
|---|---|
| `GET /tasks/graph` | Scope picker |
| `GET /tasks/graph?project=<slug>\|epic=<id>[&include_resolved=1][&focus=<id>][&overlays=…][&isolated=1\|0]` | Graph page |
| `GET /tasks/{task_id}/minigraph` | Mini-graph fragment (payload + container) |
| `GET /tasks/{task_id}?fragment=panel[&scope=project:<slug>\|epic:<id>]` | Side-panel partial (scope enables impact) |
| `GET /tasks?selected=<task_id>` | Dashboard with the panel open (SSR) |

### MCP / SSE dependencies

No new Lithos tools. `lithos_task_edge_list`, `lithos_task_children`,
`lithos_task_get`, `lithos_task_blocked` (a new *scoped* use of an existing
contract: `project=` and `tags=` are canonical in the vendored request),
`lithos_task_status`, `lithos_finding_list` (count only, for the panel) all
have vendored contracts. The hub gains one pre-fan-out hook (cache
eviction).

### Telemetry

`lens.tasks.graph` (scope kind, node/edge/ghost/cycle/isolated counts,
longest-chain length, cache hits/misses, fan-out count, blocked-read
truncation, refused), `lens.tasks.graph_cache` (size, evictions),
`lens.tasks.panel` (opened-from: row/node/url), `lens.tasks.minigraph`
(node count, capped). Spans and counters follow the `metrics.py`
conventions from #72.

## Testing Decisions

Same bar as T1: no coverage number; every listed behaviour has a test that
fails when it is reverted, and the `docs/architecture.toml` budgets hold.
The fake (`fake_lithos.py` / `fake_dataset.py`) is the readiness oracle and
gains: a dependency cycle, a cross-project `blocks` edge, a cancelled
predecessor, a resolved predecessor inside and outside the window, an epic
with a child in another project, isolated tasks, and a chain of depth 5 —
so the e2e visual pipeline sees every branch on one board.

- **graph_cache:** hit/miss/TTL; single-flight (two concurrent scopes → one
  `edge_list` per task, asserted on the fake's call log); eviction on task
  event; flush on `lens.refresh`; semaphore bound.
- **graph_scope (pure):** project open-only vs `include_resolved`; epic
  closed-by-default; satisfied edge dropped; open/cancelled far endpoint
  ghosted; ghost edges never fetched; ghosts count toward the guard; refuses
  at `max_tasks + 1`; isolated set computed from dependency edges only.
- **graph_layout (pure, table-driven):** DAG layers; self-loop, 2-cycle and
  3-cycle each an SCC; dependents of a cycle layered below it; ghosts
  top/bottom; roots list; member order and representative path identical
  under reversed input; a Lithos-flagged member with no SCC condensed alone
  and layered; **longest chain** of a known depth-5 DAG; chain through a
  focused node; deterministic tie-break; a cyclic condensation counts once.
- **Graph page rendering:** one `<ol>` per layer with status; callout names
  members in sorted order with one path; a cross-scope cycle (A in scope,
  B and C ghosts, Lithos reporting A `kind="cycle"`) is listed under
  "through tasks outside this scope" and draws no SCC group; a scoped
  blocked read of exactly `limit` rows → truncation banner and `cycle
  status unknown` on absent in-scope tasks; a failed read → unavailable
  banner, SCC cycle still rendered; an epic whose child is in another
  project → a blocked read for that project too (call log), and if that
  read fails only that child is unknown; ghost row shows project chip and
  both links; the longest-chain line names the depth-5 chain; the isolated
  disclosure is collapsed on project scope and open on epic scope with the
  right count; the legend lists exactly the visible edge types; payload
  node set equals the text's; picker on no scope; refusal panel.
- **Side panel:** `?selected=` server-renders open with title, blockers,
  dependents, parent; fragment route returns the partial only; unknown id
  → not-found panel, not 500; closing keeps `?project=` and `?focus=` (JS
  test); with `scope=`, impact reads "frees N in scope, M immediately" for
  a fixture where a task has 3 transitive dependents of which 1 has it as
  sole blocker → `3`, `1`; with that project's blocked read truncated, M is
  withheld with the reason.
- **Mini-graph:** two-up-one-down membership; cap at 40 with tail text;
  focus link; "Blocks:" line lists level-1 dependents in the text baseline.
- **Client (`test_tasks_js.py` pattern, extended):** focus classes
  ancestors/descendants lit and others dimmed for a fixture graph; search
  match sets `focus=`; overlay toggles add/remove `parent_child` edges and
  update the URL; the pill appears on a matching `task_id` and the layout
  is not re-run; Escape clears focus.
- **Visual (e2e, through loom's artifact review):** project graph with
  cycle + ghost + isolated disclosure + legend + arrowheads; focus mode on
  a mid-chain node; the panel beside the canvas; mini-graph on a blocked
  task.

## Tracer-bullet vertical slices

Seven slices, one strand. "Independent" means dispatchable at milestone
start. Each slice updates `docs/SPECIFICATION.md` for what it ships and
passes the lens parity command (`make check && make diagrams` with no
generated drift).

1. **A1 Per-task edge cache + scope assembly.** `graph_cache.py`,
   `graph_scope.py`; `[graph]` config + env overrides; hub eviction hook;
   `architecture.toml` mapping + budget bump; isolated-set computation.
   *Independent.*
   Acceptance: two concurrent project scopes over the fake fetch each
   task's edges exactly once; a `task.updated` for a node evicts it and the
   next scope refetches only that task; a page scope of `max_tasks + 1`
   (ghosts included) is refused with the count; a completed out-of-scope
   predecessor's edge is absent while a cancelled one is a ghost; a task
   with only a `parent_child` edge is isolated.
2. **A2 Topology.** `graph_layout.py`: SCC, condensed Kahn, hierarchy tree,
   roots, deterministic member order + representative path, single-node
   condensation for Lithos-flagged members, **longest blocking chain** (and
   through a given node). Pure over `EdgeRecord`/`TaskRecord`/
   `BlockerRecord`. *Independent.*
   Acceptance: A→B→A with C blocked by B yields layers `[{A,B} cycle]`,
   `[C blocked-via-cycle]`; a DAG of depth 4 yields four layers; a ghost
   with only outgoing edges is in layer 0; the same SCC in reversed edge
   order renders the same member list and path; an in-scope A with a
   `kind="cycle"` blocker but no SCC is layered and marked; the depth-5
   fixture's longest chain is the known one and the chain through its
   layer-2 node is the known sub-chain.
3. **A6 Side panel (dashboard first).** `?selected=` SSR baseline, panel
   fragment route with blockers / dependents / parent / claims, Expand,
   close preserves state, row click wiring on the dashboard. No graph
   dependency: dependents come from the task's own `edge_list`.
   *Independent.*
   Acceptance: `GET /tasks?selected=<id>` contains the panel with the
   task's title, its blockers with live status, and its level-1 dependents;
   the fragment route returns the partial only; unknown id → not-found
   panel; closing keeps `?project=` intact (JS test).
4. **A3 `/tasks/graph` text baseline.** Route, picker, legend, longest
   chain line, layers, callout + cycle-signal banners, ghosts, isolated
   disclosure (project hidden / epic shown), `include_resolved`, refusal
   panel, JSON payload, nav item, `as_of`, scoped `task_blocked` reads
   (per project in the subtree for epics). *Needs A1, A2.*
   Acceptance: the graph-page rendering cases in Testing Decisions, in
   full — layers, callout order, cross-scope cycle, truncation and failure
   banners, multi-project epic read, ghost links, longest chain, isolated
   disclosure defaults, legend, payload/text equality, picker, refusal.
5. **A4 Cytoscape rendering + interaction.** Vendor script loaded on the
   graph page only; `graph.js`: draw from payload with explicit roots,
   arrowheads, styling vocabulary, compound cycle nodes, longest-chain
   trace, overlays off by default with URL state, isolated toggle, click →
   panel (reusing A6) / dblclick → navigate, "graph changed" pill,
   show-as-text toggle, panel beside canvas. *Needs A3, A6.*
   Acceptance: e2e artifacts show the fake's cycle as a compound node, the
   ghost dimmed, arrowheads and the legend; toggling hierarchy adds the
   `parent_child` edges and `overlays=hierarchy` to the URL; a
   `task.updated` for a node shows the pill and does not re-layout; text
   remains in the DOM when the canvas is up; clicking a node opens the
   panel with that task.
6. **A7 Exploration mode.** `focus=`: centre + lit/dimmed classes + chain
   through the node + panel open; search box → `focus=`; Escape clears;
   **downstream impact** in the panel (`scope=` param, N from topology, M
   from the scoped blocked read, withheld on truncation). *Needs A4.*
   Acceptance: focusing the depth-5 fixture's layer-2 node lights exactly
   its ancestors and descendants and dims the rest (JS test on classes);
   the chain line changes to the chain through it; typing a title prefix
   and selecting sets `focus=`; the panel reads "frees 3 in scope, 1
   immediately" for the impact fixture and withholds M when that project's
   blocked read is truncated.
7. **A5 Detail mini-graph.** `/tasks/{id}/minigraph` fragment, two-up-one-
   down scope, cap + tail, focus link, "Blocks:" text line, rendered above
   the text chain on the detail page and on panel-open from the detail
   route. *Needs A4.*
   Acceptance: a task blocked by B (blocked by C) with dependent D renders
   nodes {C, B, task, D, parent}; a task with 60 dependents renders 40 and
   "20 more not shown"; the focus link carries the project slug and id;
   the text baseline lists D under "Blocks:".

Ready at milestone start: **A1, A2, A6**. Critical path to the 20 Sep
checkpoint: A1 → A3 → A4 → A7. A5 can land after the checkpoint without
changing what it measures.

## Out of Scope

- **T2b — operational insights** (ROADMAP §3): Planning View (`/tasks/plan`:
  human-actionable section, starvation, corpus-wide keystone, overload,
  stalled, throughput), the findings buffer and drawer, latest-finding row
  line, human identity and agent role chips, title badge. Their
  requirements and the honesty decisions already made for them live in
  REQUIREMENTS §5A, §5.8.4, §5.9 and §5.4.1; ROADMAP ledger #11 and #12
  are theirs.
- **Corpus-wide keystone and `corpus_max_tasks`** — need a corpus scope;
  T2b. T2's impact number is scoped to the page and says so (D10).
- **A corpus-wide critical path** — the longest chain is within-scope by
  construction and labelled so (D7).
- **Server-side debounced recompute + OOB fragment push** — X1 (D12).
- **Desktop notifications** — X1.
- **Configurable ghost depth** — one hop, by decision (D5).
- **Fetching ghost edges to complete cross-scope cycles** — the cycle
  promise is bounded to fetched topology; Lithos's `task_blocked` is the
  authority for the rest (D4).
- **Bulk graph fetch upstream** (ledger gap #3) — the ~100-call fan-out is
  accepted for September; the cache and span counters are the evidence for
  the upstream ask if it is slow in practice.
- **Task-edge events upstream** (ledger gap #1) — the TTL is the workaround;
  the page states the staleness bound rather than hiding it.
- **All write actions** — T3. **Knowledge graph** — K2.

## Further Notes

- **Why this draft is narrower than the 2026-09-02 one.** The first draft
  bundled three strands — graph pages, planning view, ergonomics — and two
  review rounds showed that most of its correctness machinery (a second
  size bound, a per-metric truncation table, three findings-buffer states,
  two new ledger asks) existed to make five project-row metrics honest.
  For the goal T2 actually serves — understanding task relationships — the
  graph, the panel and the mini-graph carry nearly all the value, and the
  analytics can be judged separately once the graph exists. The decisions
  already made for those metrics were not discarded: they are in
  REQUIREMENTS, and T2b's PRD inherits them. Reviewed with Dave
  2026-09-04.
- **Scale posture.** A project scope is ~100 `edge_list` calls cold under a
  16-wide semaphore against local SQLite, one-off per TTL window and shared
  with the mini-graph. There is no corpus scope in T2. Beyond low thousands
  of open tasks, the answer is ledger gap #3, not a smarter cache.
- **Why the text baseline carries the acceptance criteria.** Loom's review
  gate is hermetic and headless. A slice whose only observable output is a
  canvas has nothing a panel can assert; that is the shape that produced
  the K1 panel escapes now in the eval corpus. The Cytoscape layer is
  checked by the visual-review artifacts instead.
- **Why not re-implement readiness for the layering.** SCC detection and
  the longest chain are topology over an edge set; `task_blocked` stays the
  authority on *which* task is blocked and supplies the sole-blocker fact
  the impact number's second figure needs. Lens computes shape, never state.
- **Hairball risk, and what answers it.** Correct construction is not
  legibility. The levers are: dependency edges only by default, isolated
  tasks folded, layered (not force) layout, the size guard, focus mode with
  dimming, search, and the longest chain as a spine to read along. If a
  hundred-node project is still unreadable with all of those, the next
  lever is a per-epic default scope, not more styling.
- **Normative docs updated with this PRD.** REQUIREMENTS §5.5 (panel
  content, mini-graph text line), §5.7 (longest chain, overlays, isolated
  tasks, focus mode, search, legend, epic multi-project cycle read), §4
  (`mini_graph_max_nodes`; `corpus_max_tasks` marked T2b), and ROADMAP §3
  (T2 narrowed, T2b added) were changed in the same PR. REQUIREMENTS is the
  contract; this PRD is its execution plan; the two agree.
- **Checkpoint measurement.** `bcaf9379` (2026-09-20) measures the
  intervention rate on these slices through loom against August's 43%.
  Slice granularity is deliberately T1-like rather than fatter, because the
  rate is counted per run.
