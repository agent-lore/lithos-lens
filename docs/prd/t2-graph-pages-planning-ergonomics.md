---
title: T2 — Graph Pages, Planning View, Operator Ergonomics
milestone: T2
status: draft
target_version: 0.4.0
references:
  - docs/ROADMAP.md (milestone sequence, upstream dependency ledger — gaps #1, #3)
  - docs/REQUIREMENTS.md §5.5 (task detail), §5.7 (task graph page), §5.8 (event pipeline), §5.9 (notifications), §5A (Planning View), §5B (project conventions)
  - docs/SPECIFICATION.md §5.3–§5.8 (T1's shipped behaviour, which T2 extends)
  - docs/prd/t1-graph-native-operator-view.md (what T1 deferred here: Out of Scope)
  - lithos docs/SPECIFICATION.md §5.4 (task tools), §8 (events)
tracked_in: lithos
task_tags: [project:lithos-lens, milestone:t2]
labels: [milestone-t2, tasks-view]
epic: 44a943fc-6055-4603-b3d7-9aabdecd73e9
depends_on: [T1]
---

# T2 — Graph Pages, Planning View, Operator Ergonomics

## Problem Statement

T1 made the dashboard graph-native: every open row sits in the section the
ready/blocked frontier puts it in, and the detail page says why. What T1
deliberately left text-only is the *shape* of the graph — and the live corpus
(~330 open tasks across ~20 projects, live cross-project `blocks` chains, 21
epics) has a shape that a list cannot show. Concretely:

- **There is no way to see a project's dependency structure.** The blocker
  chain on a detail page walks one task's ancestors, one level at a time. The
  question "what does this project's month look like, and where is the
  critical path?" has no surface. The operator answers it today by opening
  detail pages one at a time, or by asking an agent.
- **"What should happen next?" is unanswered.** The dashboard says what *can*
  happen (the ready frontier) but not which of it matters: which single task,
  if finished, frees the most downstream work; which project has open work
  but nothing anyone can pick up; which in-flight task has gone quiet. The
  Planning View that existed before the task graph (`/tasks/plan`) was
  retired unbuilt because its metrics were defined on claim counts, which
  the graph made meaningless.
- **Findings are invisible until you open a task.** `finding.posted` events
  flow through the pipeline, but nothing on the board shows what an agent
  said last, so an agent that has been silent for a day looks identical to
  one that posted ten minutes ago.
- **Every row click is a page navigation** and every agent chip is a bare id.
  Small things, but they are the difference between a dashboard and a
  console for someone managing twenty projects from it.

Two constraints shape everything below. Lithos has **no bulk graph fetch**
(ROADMAP ledger gap #3): a graph is assembled from one `lithos_task_edge_list`
call per node. And **edge writes emit no event** (gap #1): an edge another
agent adds is invisible to Lens until something else happens to either
endpoint. T2 works inside both rather than around them, and says so on the
surface.

T2 is the *tasks* surface. The knowledge graph (K2) and write actions (T3)
are not here.

## Solution

Three strands, two of them hard scope for 0.4.0 and one explicitly flex.

**Graph pages** (hard). `/tasks/graph?project=<slug>|epic=<id>` renders the
dependency DAG of one scope, assembled from a **per-task edge cache** (not a
per-scope snapshot — the cache is shared by every surface that reads edges).
The page's baseline is server-rendered text: a cycle callout, topological
layers computed with strongly-connected components condensed so cycle
members and their dependents still get a layer, and the `parent_child`
hierarchy tree. Cytoscape (already vendored, 3.30.3) is progressive
enhancement over the same embedded JSON payload. Cross-scope endpoints render
as one-hop ghost leaves; satisfied edges are dropped. The detail page gains a
two-up-one-down dependency mini-graph above its text chain.

**Planning view rebase** (hard). `/tasks/plan` is a different rendering of
the same five-call snapshot the dashboard already loads, plus one
corpus-wide dependency snapshot from the edge cache. Starvation is
redefined on the ready frontier; the keystone metric reports *two* honest
numbers (dependents downstream, and how many become ready immediately);
agent overload excludes humans; stalled detection reads a latest-finding map
warmed for in-progress tasks only; throughput uses `resolved_since`. The
human-actionable section is led by the human-gate queue.

**Operator ergonomics** (flex). The one piece the planning view needs — the
recent-findings buffer — is a foundational slice. The rest (latest-finding
row line + drawer UI, agent chips with role markers and the human/agent
distinction, the `?selected=` side panel, the title badge) are each
independently droppable to a 0.4.x point release without invalidating the
milestone. The server-side debounced recompute REQUIREMENTS §5.8.4 describes
is deferred to X1, where the transition detector it exists to serve is built.

```
/tasks/graph?project=lithos-loom
┌─────────────────────────────────────────────────────────────────┐
│ Graph · lithos-loom   [include resolved ☐] [hierarchy edges ☐]  │
│ ⚠ 1 dependency cycle: A → B → A                                  │
├─────────────────────────────────────────────────────────────────┤
│ (Cytoscape canvas — breadthfirst, colour=status, shape=type)    │
│                       [show as text]                            │
├─────────────────────────────────────────────────────────────────┤
│ Layer 0  ● influx#41 (ghost · influx)   ● Design schema (open)   │
│ Layer 1  ▶ Implement BLE (claimed: agent-zero)  [cycle: A ⇄ B]   │
│ Layer 2  ◼ Ship 0.4 (blocked)  ◼ Write docs (blocked via cycle)  │
│ Hierarchy: EPIC loom-arch ├─ Design schema ├─ Implement BLE …    │
└─────────────────────────────────────────────────────────────────┘
```

## User Stories

### Graph pages

1. As an operator, I want `/tasks/graph?project=<slug>` to render the
   project's open dependency DAG, so that I can see the shape of a
   project's work instead of inferring it from rows.
2. As an operator, I want `/tasks/graph?epic=<id>` to render an epic's
   subtree including its closed children, so that I can see an initiative's
   progress and critical path in one picture.
3. As an operator, I want the unscoped `/tasks/graph` to offer a scope
   picker (projects from the snapshot, open epics), so that the page is
   reachable from the nav without a bookmark.
4. As an operator without JS (or in a PR screenshot, or a screen reader), I
   want the graph as topological text layers with status and type per task,
   so that the page's baseline is complete and reviewable on its own.
5. As an operator, I want a dependency cycle rendered as a bracketed group
   inside its own layer with its members in a deterministic order plus one
   representative path, and its dependents layered below it marked "blocked
   via cycle", so that a cycle never makes its downstream work vanish from
   the layering — and, when Lithos reports a cycle Lens cannot see through
   its ghosts, or the scoped blocked read truncates or fails, I want the page
   to say so rather than imply "no cycle".
6. As an operator, I want an edge whose far endpoint is outside the scope
   to render that endpoint as a dimmed ghost node carrying its project chip,
   with links to its detail page and to *its* project's graph, so that
   cross-project dependencies are visible and crossable by choice.
7. As an operator, I want a satisfied edge (predecessor `completed`) dropped
   from the default graph and restored by an `include_resolved` toggle, so
   that the default picture shows what can still run, and the toggle shows
   what did.
8. As an operator, I want a scope larger than `[graph].max_tasks` refused
   with a "narrow your scope" panel naming the count, so that a graph is
   never rendered unreadably.
9. As an operator with JS, I want the same graph drawn by Cytoscape —
   `breadthfirst` layout from the server-computed layers, colour = status,
   shape = type, edge style per type, `parent_child` edges toggleable off,
   cycle members grouped in a compound node with T1's `cycle` styling — so
   that the picture and the text cannot disagree.
10. As an operator, I want click on a node to open its summary (title,
    status, blockers, detail link) and double-click to navigate, so that
    the graph is a navigation surface, not a poster.
11. As an operator, I want a "graph changed — refresh" pill when a task
    event touches a node on the page, rather than an auto re-layout under my
    cursor, so that the graph stays stable while I read it.
12. As an operator, I want `?focus=<task_id>` to highlight and centre one
    node, so that the detail page can hand me to the graph with context.
13. As an operator, I want the detail page to show a mini-graph of the
    task's blockers two hops up and dependents one hop down, capped with an
    honest remainder and a focus link to the full graph, so that "why can't
    this run" and "what does finishing this free" are one glance.

### Planning view

14. As an operator, I want `/tasks/plan` to lead with the human-gate queue
    (oldest first, each with its waiter count), then tasks tagged for a
    human, then tasks a human has claimed, so that what needs *me* is the
    first thing on the page.
15. As an operator, I want each project row to say whether it is starved,
    sub-classified `fully-blocked` (nothing is ready) or `fully-claimed`
    (ready work exists but every ready task is claimed), so that "nothing
    can be picked up" names its cause.
16. As an operator, I want each project's keystone task shown as
    `keystone: "<title>" — N downstream`, with the tooltip splitting N into
    tasks that become ready immediately and the rest, so that the
    highest-leverage completion is named honestly.
17. As an operator, I want an agent-overload flag when one non-human agent
    holds most of a project's in-flight claims, so that a fleet bottleneck
    is visible.
18. As an operator, I want a stalled flag when an in-progress task's latest
    finding is older than `stalled_no_findings_hours`, and a separate
    `no findings yet` label when it has none (claims carry no start time,
    so that silence has no measurable length), so that a quiet agent is
    distinguishable from a working one without Lens guessing.
19. As an operator, I want throughput per project over the
    `resolved_since` window — completed, cancelled, completion ratio, median
    time-to-resolve, median open-age of ready work (labelled as the proxy it
    is) — with dormant projects hidden by default, so that the overall
    shape of work is a table I can read.
20. As an operator, I want planning-view fragments to refresh on the same
    debounced client reconcile the dashboard uses, so that the page is live
    without a second event pipeline.

### Ergonomics

21. As an operator, I want a `finding.posted` event to produce a drawer
    entry with the finding's text (refetched, since the event carries none),
    so that the last thing an agent said is visible without opening the
    task.
22. As an operator, I want every in-progress task's latest finding known
    from the moment Lens starts, so that the stalled flag is exact after a
    restart rather than silent for a day.
23. As an operator, I want agent chips to carry a role marker from the
    registry's `type` and a distinct glyph for humans (registry
    `type="human"` or `[tasks].human_agents`), so that a human and an agent
    never look alike.
24. As an operator, I want each dashboard row to show its latest finding as
    one line, and a collapsible drawer of recent findings across the board,
    so that the board reads as a feed as well as a state.
25. As an operator, I want clicking a row to open a side panel
    (`/tasks?selected=<id>`) with an Expand link to the full page, and the
    panel to render open server-side when the URL carries `selected`, so
    that browsing tasks does not mean leaving the board.
26. As an operator, I want the page title to become `(N) Lithos Lens` while
    there are unseen Needs-attention items, cleared on focus, so that a
    background tab tells me something changed.

## Implementation Decisions

Every decision in this section was settled with Dave on 2026-09-02 and is
not re-opened per slice.

### D1. Scope cut line for 0.4.0

0.4.0 ships when strands A (graph pages) and B (planning view) are live.
Strand C slices are each individually droppable to 0.4.x; the month-end
review judges T2 shipped on A + B alone. The one ergonomics piece B depends
on — the findings buffer — is slice B1, not flex.

### D2. Per-task edge cache, not a per-scope snapshot

A new Foundation module (`graph_cache.py`) holds one entry per `task_id`:
its deduped `lithos_task_edge_list(task_id, direction="both")` result, with
a TTL of `[graph].cache_ttl_s` (30s) and single-flight (two concurrent
readers of the same task share one in-flight call). It lives on `AppState`
beside the `EventHub`.

A **scope** is a pure function over the master task list and the cache
(`graph_scope.py`): it fans out only for cache misses, under the existing
`[graph].fetch_concurrency` (16) semaphore. Project graph, epic graph, the
detail mini-graph, and the planning view's corpus-wide dependency snapshot
are all scopes over the same cache, so a project page warms ~100 entries,
the planning view warms the rest, and the mini-graph reuses whatever is
warm. There is no per-scope memoisation: assembly over cached entries is
cheap at this scale.

**Two size bounds, because two kinds of scope.** A *page* scope (project,
epic) is rendered, so it is refused above `[graph].max_tasks` (300, ghosts
counted) with the "narrow your scope" panel — a graph that cannot be read
should not be drawn. The *corpus* scope is internal: nothing renders it, the
planning view only computes over it, and its node set is the master open
list the dashboard already holds (~330 today, which already exceeds
`max_tasks`). It is therefore **exempt from `max_tasks`** and bounded
instead by `[graph].corpus_max_tasks` (1000). Above that bound the corpus
fan-out is skipped and the one metric that needs it — keystone — renders as
*not computed* with a banner naming the bound; everything else on the
planning view still renders (D10a).
That is the honest ceiling: beyond ~1000 open tasks the answer is ledger gap
#3, not a bigger fan-out.

**Invalidation, stated honestly:**
- Any consumed task event carrying a `task_id` evicts that task's entry (a
  hook the hub calls before fan-out). A `lens.refresh` flushes everything.
- **Edge upserts emit no event** (ledger gap #1). An edge added by another
  agent is invisible until the TTL expires or a task event lands on either
  endpoint. The graph page's "graph changed" pill fires on task events
  only. The staleness bound is the TTL, and the page says "as of HH:MM:SS".

This is one new cross-component edge (Events → GraphCache) against the
`cross_component_edges` budget in `docs/architecture.toml`; raise it with the
reason in the same diff.

### D3. Text layering is the first-class baseline

The graph page renders, in this order: the cycle callout (if any), the
topological layers as ordered lists (status, type, claim, project chip for
ghosts), the `parent_child` hierarchy tree, and a
`<script type="application/json">` payload `{nodes, edges, layers, cycles,
ghosts, as_of}`. Acceptance criteria and pytest assert on the text; the
Cytoscape layer is verified through the e2e visual-review artifacts
(`e2e/`, `develop_artifacts_path`) that lens already runs through loom. When
Cytoscape initialises it draws from the payload and collapses the text
layers behind a "show as text" toggle; the text stays in the DOM.

### D4. Cycles: SCC condensed into its own layer

Two sources, with a stated division of labour:

- **Lithos is the authority on *whether* a task is in a cycle.** The graph
  page reads `lithos_task_blocked` **scoped to the page** — `project=<slug>`
  and, under the `"both"` convention, a second read with
  `tags=["project:<slug>"]`, unioned (§5B.7's pattern; epic scope uses the
  epic's own project) — each with `limit=frontier_limit`. Every in-scope
  task whose blockers include `kind="cycle"` is marked *in a cycle* with the
  blocker's `message` (which carries the cycle path as Lithos saw it),
  regardless of what Lens's own topology finds. **This read can truncate or
  fail, and the page says so:** a response of `len == limit` is treated as
  truncated (the dashboard's own rule), and a truncated or failed read
  renders a banner ("cycle signal incomplete: blocked read truncated /
  unavailable") and gives every in-scope task *absent from the response* a
  `cycle status unknown` marker — never an implied "no cycle". Cycles Lens
  finds by SCC are still drawn. A scoped read on the live corpus is tens of
  rows, so truncation is a scale event, not a routine one; when it happens
  it is visible.
- **Lens computes the *shape* of cycles it can see.** `graph_layout.py`
  (pure) runs Tarjan's SCC over the `blocks` + `waits_on_gate` edges of the
  **fetched topology** — in-scope nodes' edges only; ghost edges are never
  fetched (D5). Every SCC of size > 1, or a self-loop, is a cycle Lens can
  draw. This promise is explicitly bounded: a cycle that passes through two
  or more out-of-scope tasks (`A(in) → B(ghost) → C(ghost) → A`) is invisible
  to SCC, because `B → C` is never read. Such a task is still marked in a
  cycle by the Lithos signal above, and the callout lists it under *cycle
  through tasks outside this scope* with Lithos's path message, so the
  page says what it cannot draw.

Kahn's layering runs with each SCC condensed to one node, and each
Lithos-flagged cycle member that belongs to no SCC treated as its own
single-node condensation, so every cycle member receives a layer and its
dependents are layered below it (marked "blocked via cycle", mirroring the
`kind="cycle"` blocker `task_blocked` reports for them).

Text renders an SCC as a bracketed group inside its layer. **Display order
is deterministic:** members sorted by (`created_at`, `id`), followed by one
representative cycle path — a DFS from the smallest member over adjacency
sorted the same way — rendered as `A → B → A`. A general SCC may contain
several cycles; the page shows one path and the full member set, never an
order that depends on iteration. The payload marks each member with a
`cycle` id; Cytoscape draws members inside a compound parent with the
`cycle` attention styling, and the page passes explicit `roots` (every
in-degree-zero node of the condensed graph, plus one representative per
*cyclic* condensation — an SCC of size > 1, a self-loop, or a
Lithos-flagged single node — since a cycle has no in-degree-zero member of
its own) so `breadthfirst` never guesses.

SCC detection is topology over an edge set, not readiness; readiness and
cycle *membership* stay Lithos's. **Lens still never re-implements the
readiness predicate.**

### D5. Ghost nodes: one hop, leaf-only

A ghost is the far endpoint of an edge whose near endpoint is in scope.
Lens never fetches a ghost's own edges, so a ghost is always a leaf on its
far side and fan-out is bounded by the scope, not the corpus. Ghosts
participate in layering (a ghost with only outgoing edges lands in the top
layer). Open ghosts get title/status from the master open list for free;
only resolved far endpoints need a `task_get`, through the same semaphore.
Ghosts count toward `max_tasks`. A ghost renders dimmed with its project
chip and two links: its detail page, and `/tasks/graph?project=<its slug>`.
No configurable hop count. The cost of leaf-only ghosts is the bounded
cycle promise in D4: Lens draws cycles within fetched topology and relies on
Lithos's `task_blocked` signal for the rest.

### D6. Scope membership and satisfied edges

- Project scope: the project's tasks per §5B.1 (epics and gates included),
  **open only by default**; `include_resolved=1` adds tasks resolved within
  the dashboard's `resolved_since` window.
- Epic scope: `lithos_task_children(epic, recursive=True,
  include_closed=True)` plus the epic; **closed children included by
  default**; the same toggle, opposite default.
- **A satisfied edge is dropped, not ghosted.** An edge whose predecessor is
  `completed` says nothing about what can run; when the predecessor is out
  of scope the edge goes. Ghosting applies to far endpoints that are `open`
  (live blocker) or `cancelled` (the unsatisfiable case T1 promotes — a
  graph that hid it would contradict the dashboard). With `include_resolved`
  on, completed predecessors inside the window return as real nodes.
  `discovered_from` and `parent_child` follow the same rule.

### D7. Detail mini-graph: two up, one down

Incoming `blocks`/`waits_on_gate` to depth 2, outgoing to depth 1, the parent
epic as a single labelled node, no `discovered_from`. Capped at 40 nodes
through T1's shared remainder tail, with a link to
`/tasks/graph?project=<slug>&focus=<id>`. Same cache, same payload shape,
same client module as the graph page; its text baseline is the existing
blocker chain, so it renders no layers of its own.

### D8. Planning view loads the dashboard's snapshot plus one corpus scope

`load_dashboard`'s five calls unchanged, plus a whole-corpus dependency
scope (every open task's edges) from the cache. Keystone and starvation come
from that; throughput from the resolved-window list already fetched; the
human-gate queue reuses `gates.py`'s rows. **No `lithos_tags` call**: the
project universe is what the snapshot observes under both conventions. A
project with no open task and no resolution in the window is dormant by
§5A.5's own definition and hidden by default.

### D9. Keystone: two numbers, corpus-wide

Candidates: every open task and open gate in the project (epics excluded —
they carry no `blocks` edges). Dependents are counted transitively over
`blocks` + `waits_on_gate` across the **whole corpus**. The chip reads
`keystone: "<title>" — N downstream`; the tooltip splits *M become ready
immediately* (dependents whose `task_blocked` entry lists this task as their
only unsatisfied blocker — a direct read of data Lithos returns) from *N−M
further down the chain*, and names the projects the dependents fall in.
Ties break oldest-first; no chip when no candidate has an open dependent.
Degradation per D10a: N comes from edges and survives frontier truncation;
M needs a complete blocked frontier and is withheld without one.

### D10. Starvation v2, overload, stalled, throughput

- **Starvation:** > 0 open workable tasks and 0 ready-and-unclaimed.
  `fully-blocked` when the ready set is empty; `fully-claimed` otherwise.
- **Overload:** in-flight depth ≥ `bottleneck_min_inflight` (3) and one
  **non-human** agent holds ≥ `bottleneck_concentration` (0.7) of those
  claims. Humans are excluded: an operator holding five claims is not a
  fleet bottleneck.
- **Stalled:** an in-progress task whose latest finding (D11) is older than
  `stalled_no_findings_hours` (24). **Stalled fires on evidence only.** Two
  further states are distinct and labelled, never folded into stalled:
  *no findings yet* — the finding history is known to be empty. Claims
  expose `expires_at` but no `claimed_at` (vendored `task_list` contract),
  so Lens cannot tell "claimed a minute ago" from "silent for two days";
  the row says `no findings yet` with a tooltip stating exactly that, and
  a claim start time is a new upstream ask (ROADMAP ledger #12).
  *finding status unknown* — the warmup or refetch for that task failed;
  the row says so, and the read is retried on the next event or reconcile
  for that task. A failed read is never recorded as an empty history.
  Row decoration on the dashboard; never promoted into Needs attention
  (T1's model is unchanged).
- **Throughput:** per §5A.5 — `resolved_since` window, completion ratio,
  median `resolved_at − created_at`, and **median open-age of ready work**:
  the median `now − created_at` over the project's ready-and-unclaimed
  tasks, labelled exactly that on the page. This is a *proxy*, and the PRD
  names it as one: `lithos_task_ready` returns `created_at` only (see the
  vendored contract), so "how long has this been ready" is unobservable —
  an old task that became ready seconds ago reports its open-age, not
  seconds. A readiness timestamp is a new upstream ask (ROADMAP ledger
  #11); Lens does not fake one with a lifecycle tracker (D13). `Hide
  dormant` on by default, cookie + URL. Sparklines stay deferred.

### D10a. Degraded semantics when a frontier read truncates

The reused snapshot calls `task_ready` and `task_blocked` with
`frontier_limit` (500); either side can truncate independently, and the
dashboard already marks each side's counts approximate when it does. The
planning view inherits that per-side flag and degrades **per metric**,
never asserting an exact result from a cut read:

| Metric | Depends on | Ready truncated | Blocked truncated |
|---|---|---|---|
| Ready / blocked depths | that side's frontier | `≥ N`, approximate marker | `≥ N`, approximate marker |
| Starvation + sub-class | ready frontier + master list | **unknown** — chip not shown; tooltip says why | unaffected |
| Keystone `N downstream` | edge cache | unaffected | unaffected |
| Keystone `M immediately` | blocked frontier (sole-blocker fact) | unaffected | **withheld** — chip shows N only; tooltip names the truncation |
| Median open-age of ready work | ready frontier | approximate marker | unaffected |
| Agent overload, stalled | master list claims, findings map | unaffected | unaffected |
| Throughput counts, ratio, time-to-resolve | resolved lists (no limit) | unaffected | unaffected |

"Unknown" is a fourth state beside fired / not-fired / not-computed, and it
renders as absence plus an explanation, never as "not starved". A row whose
project is entirely inside a truncated tail shows depths of `≥ 0`.

### D11. Findings buffer: latest-finding-per-task, in-progress warmup

`finding.posted` carries `finding_id, task_id, agent` only. On each event
the hub-side consumer calls `lithos_finding_list(task_id)` and takes the
**newest** entry — `max(created_at)`, ties broken by `finding_id` — into a
`latest_finding[task_id] = {finding_id, agent, timestamp, summary,
knowledge_id}` map and onto a ring buffer of `recent_findings_drawer_size`
(50). The vendored `finding_list` contract promises no order, so Lens sorts;
the pipeline tolerates replay and out-of-order delivery, so the ring
**deduplicates by `finding_id`** and a refetch that yields an entry already
present is a no-op. The map records three outcomes per warmed task: a
finding, *known empty* (the read succeeded with no rows), or *unknown* (the
read failed) — D10's stalled rule reads the distinction. **Warmup covers in-progress tasks only**: one `finding_list` per
claimed workable task at boot and on `task.claimed`. That makes the stalled
flag exact from the first render; the drawer is a by-product with an honest
header ("findings since HH:MM"), not a claim about the corpus's last 24h.
No warmup-window knob exists.

### D12. Human identity: registry ∪ config

An agent is human when its `lithos_agent_list` entry has `type == "human"`
(what T3 will register operators as) **or** its id is in
`[tasks].human_agents`. Role markers on chips are the registry `type`
string verbatim with a distinct human glyph; unregistered ids get a plain
chip, never a guessed role. Human-claimed tasks feed the human-actionable
queue.

### D13. No server-side debounced recompute in T2

The planning view's fragments (`/tasks/plan/projects`,
`/tasks/plan/throughput`, the human-actionable section) and the graph
page's pill join the existing client-side debounced reconcile in `tasks.js`.
Metrics are pure functions over the snapshot and the cache, so a recompute
per tab per window is mostly cache hits. Server-side recompute with OOB
fragment push is deferred to X1, the first feature that needs a
server-held *transition* detector (a row entering attention, a task
becoming ready — note `unblocked[]` is in the `task_complete` response, not
the `task.completed` event, so that detector is a frontier diff in Lens).
`metrics_debounce_ms` stays as the client debounce knob it already is.

### D14. Side panel and title badge

`GET /tasks?selected=<id>` server-renders the panel open (the no-JS
baseline); a row click fetches `GET /tasks/{id}?fragment=panel` (same
template, partial block) and pushes `selected` onto the URL; close clears
it and preserves list state; Expand navigates to `/tasks/{id}`. The
mini-graph initialises on `htmx:afterSwap`. The title badge counts
Needs-attention ids the tab has not had focus for; state is client-only.

### Config

```toml
[lithos-lens.graph]
cache_ttl_s = 30                   # per-task edge cache TTL
max_tasks = 300                    # PAGE scope size guard (ghosts count)
corpus_max_tasks = 1000            # internal corpus scope bound (planning metrics)
fetch_concurrency = 16             # edge_list fan-out semaphore

[lithos-lens.tasks]
human_agents = []                  # ids treated as human, unioned with registry type
human_actionable_tag = "human"
bottleneck_min_inflight = 3
bottleneck_concentration = 0.7
stalled_no_findings_hours = 24
throughput_window_days = 30
recent_findings_drawer_size = 50
metrics_debounce_ms = 2000         # client reconcile debounce (existing behaviour, now named)
```

Env overrides follow the shipped `LITHOS_LENS_<SECTION>_<KNOB>` convention
(`tests/test_config_env_prefix.py` guards docs↔config parity). The title
badge has no knob; it is always on.

### Routes

| Endpoint | Purpose |
|---|---|
| `GET /tasks/graph` | Scope picker |
| `GET /tasks/graph?project=<slug>\|epic=<id>[&include_resolved=1][&focus=<id>]` | Graph page |
| `GET /tasks/{task_id}/minigraph` | Mini-graph fragment (payload + container) |
| `GET /tasks/{task_id}?fragment=panel` | Side-panel partial |
| `GET /tasks/plan` | Planning View |
| `GET /tasks/plan/projects`, `GET /tasks/plan/throughput` | Fragments |
| `GET /tasks/findings/recent` | Drawer fragment |

### MCP / SSE dependencies

No new Lithos tools. `lithos_task_edge_list`, `lithos_task_children`,
`lithos_task_get`, `lithos_task_blocked` (the graph page's authoritative
cycle signal, D4 — a new *scoped* use of an existing contract: `project=`
and `tags=` filters, already canonical in the vendored request),
`lithos_finding_list`, `lithos_agent_list` all have vendored contracts. New consumer of the existing `finding.posted` and
`task.claimed` events (warmup trigger). The hub gains one pre-fan-out hook
(cache eviction).

### Telemetry

`lens.tasks.graph` (scope, node/edge/ghost/cycle counts, cache hits/misses,
fan-out count, refused), `lens.tasks.graph_cache` (size, evictions),
`lens.tasks.plan` / `.plan.projects` / `.plan.throughput` (§5A.8),
`lens.tasks.findings_buffer` (warmup count, refetch failures). Spans and
counters follow the `metrics.py` conventions from #72.

## Testing Decisions

Same bar as T1: no coverage number; every listed behaviour has a test that
fails when it is reverted, and the `docs/architecture.toml` budgets hold.
The fake (`fake_lithos.py` / `fake_dataset.py`) is the readiness oracle and
gains: a dependency cycle, a cross-project `blocks` edge, a cancelled
predecessor, a resolved predecessor inside and outside the window, and a
human-typed agent — so the e2e visual pipeline sees every branch on one
board.

- **graph_cache (pure + async):** hit/miss/TTL; single-flight (two
  concurrent scopes → one `edge_list` per task, asserted on the fake's call
  log); eviction on task event; flush on `lens.refresh`; semaphore bound.
- **graph_scope (pure):** project open-only vs `include_resolved`; epic
  closed-by-default; satisfied edge dropped; open/cancelled far endpoint
  ghosted; ghost edges never fetched; ghosts count toward the guard; guard
  refuses at `max_tasks + 1`; the corpus scope is NOT refused at
  `max_tasks + 1` and IS skipped at `corpus_max_tasks + 1`.
- **graph_layout (pure, table-driven):** DAG layers; self-loop and 2-cycle
  and 3-cycle each an SCC; dependents of a cycle layered below it; ghosts
  top/bottom; roots list; member order and representative path are
  identical under reversed input order; a Lithos-flagged cycle member with
  no SCC is condensed alone and layered.
- **Graph page rendering:** layers as ordered lists with status; cycle
  callout text names members in order; ghost row carries project chip and
  both links; picker on no scope; refusal panel; payload JSON parses and
  matches the text (same node set, same layer per node).
- **Mini-graph:** two-up-one-down membership; cap at 40 with tail text;
  focus link.
- **Planning (pure, table-driven):** every flag fires and respects its
  knob; starvation sub-classes; keystone two numbers (a task with 3
  transitive dependents of which 1 has it as sole blocker → `3 downstream`,
  `1 immediately`); cross-project dependent counted; human excluded from
  overload; stalled from latest-finding age; medians on a known window;
  **open-age proxy** (a task created 30 days ago whose last blocker
  completed a minute ago reports 30 days, labelled open-age); **truncation**
  — ready-only truncated → starvation unknown, open-age approximate,
  keystone both numbers; blocked-only truncated → starvation exact, keystone
  N only; corpus over `corpus_max_tasks` → keystone not computed, every
  other metric present.
- **Findings buffer:** `finding.posted` → `finding_list` refetch → entry with
  summary; newest = `max(created_at)` with an unordered fake response; a
  replayed event is a no-op on the ring (dedup by `finding_id`); warmup
  calls `finding_list` for claimed tasks only (call log); `task.claimed`
  triggers warmup for that task; an empty successful read records *known
  empty*; a failed read records *unknown* and is retried on the next
  reconcile.
- **Stalled states:** newly claimed + no findings → `no findings yet`, not
  stalled; a claim whose `expires_at` is days away + no findings → still
  `no findings yet`, not stalled (no `claimed_at` exists to say otherwise);
  a finding read failure → `finding status unknown`, not stalled; a finding
  25h old → stalled.
- **Graph-page cycle signal:** a scoped `task_blocked` response of exactly
  `limit` rows → truncation banner and `cycle status unknown` on every
  in-scope task absent from it; a failed read → unavailable banner, SCC
  cycles still drawn; under `"both"` convention a task carrying only the
  `project:` tag is covered by the second read.
- **Human identity:** registry `type=human` alone; config alone; both;
  unregistered id → plain chip.
- **Side panel:** `?selected=` server-renders open; fragment route returns
  the partial; unknown id → not-found panel, not 500.
- **Events (extend test_tasks_sse.py):** task event evicts the cache entry;
  planning fragments reconcile on the client debounce (`test_tasks_js.py`).
- **Visual (e2e, through loom's artifact review):** graph page with the
  fake's cycle + ghost + resolved branches; mini-graph on a blocked task;
  planning view with every flag present.

## Tracer-bullet vertical slices

Twelve slices. "Independent" means dispatchable at milestone start.
Each slice updates `docs/SPECIFICATION.md` for what it ships and passes the
lens parity command (`make check && make diagrams` with no generated drift).

### Strand A — graph pages (through loom by the 20 Sep checkpoint)

1. **A1 Per-task edge cache + scope assembly.** `graph_cache.py`,
   `graph_scope.py`; `[graph]` config + env overrides; hub eviction hook;
   `architecture.toml` mapping + budget bump. *Independent.*
   Acceptance: two concurrent project scopes over the fake fetch each
   task's edges exactly once; a `task.updated` for a node evicts it and the
   next scope refetches only that task; a page scope of `max_tasks + 1`
   (ghosts included) is refused with the count; the corpus scope over a
   fake of `max_tasks + 1` open tasks assembles, and over
   `corpus_max_tasks + 1` is skipped with a reason; a completed
   out-of-scope predecessor's edge is absent while a cancelled one is a
   ghost.
2. **A2 Topology.** `graph_layout.py`: SCC, condensed Kahn, hierarchy tree,
   roots, deterministic member order + representative path, single-node
   condensation for Lithos-flagged members outside any SCC. Pure over
   `EdgeRecord`/`TaskRecord`/`BlockerRecord`. *Independent.*
   Acceptance: A→B→A with C blocked by B yields layers `[{A,B} cycle]`,
   `[C blocked-via-cycle]`; a DAG of depth 4 yields four layers; a ghost
   with only outgoing edges is in layer 0; the same SCC fed in reversed
   edge order renders the same member list and path; an in-scope A with a
   `kind="cycle"` blocker but no SCC (its cycle runs through two ghosts) is
   layered and marked, not dropped.
3. **A3 `/tasks/graph` text baseline.** Route, picker, layers, callout,
   ghosts, `include_resolved`, refusal panel, JSON payload, nav item,
   `as_of`, scoped `task_blocked` reads for the authoritative cycle signal
   with truncation/failure banners. *Needs A1, A2.*
   Acceptance: the fake project renders one `<ol>` per layer with status
   per task; the cycle callout names members in sorted order with one path;
   a cross-scope cycle (A in scope, B and C ghosts, Lithos reporting A
   `kind="cycle"`) appears in the callout under "through tasks outside this
   scope" with Lithos's message and draws no SCC group; with the fake's
   scoped blocked read returning exactly `limit` rows, the page shows the
   truncation banner and marks the absent in-scope task `cycle status
   unknown` (not "no cycle"); with the read failing, the unavailable banner
   shows and the SCC cycle is still rendered; the ghost row shows its
   project chip and links to `/tasks/graph?project=<slug>`; the embedded
   payload's node set equals the text's; no scope → picker lists the fake's
   projects and open epics.
4. **A4 Cytoscape enhancement.** Vendor script loaded on the graph page
   only; `graph.js`: draw from payload with explicit roots, styling
   vocabulary (colour=status, shape=type, edge style per type), compound
   cycle nodes, `parent_child` toggle, click summary / dblclick navigate,
   `focus=`, "graph changed" pill from event `task_id` ∩ node ids, show-as-
   text toggle. *Needs A3.*
   Acceptance: e2e artifacts show the fake's cycle as a compound node and
   the ghost dimmed; `?focus=` centres the node; a `task.updated` for a node
   shows the pill and does not re-layout; text remains in the DOM when the
   canvas is up.
5. **A5 Detail mini-graph.** `/tasks/{id}/minigraph` fragment, two-up-one-
   down scope, cap 40 + tail, focus link, rendered above the text chain.
   *Needs A4.*
   Acceptance: a task blocked by B (blocked by C) with dependent D renders
   nodes {C, B, task, D, parent}; a task with 60 dependents renders 40 and
   "20 more not shown"; the focus link carries the project slug and id.

### Strand B — planning view

6. **B1 Findings buffer.** `findings_buffer.py`: `finding.posted` refetch,
   latest-per-task map, ring buffer, in-progress warmup at boot and on
   `task.claimed`, `/tasks/findings/recent` fragment,
   `recent_findings_drawer_size`. *Independent.*
   Acceptance: a `finding.posted` on the fake produces a buffer entry with
   the finding's summary, chosen by `max(created_at)` from an unordered
   response; the same event replayed leaves the ring unchanged; boot
   against a fake with 2 claimed and 5 unclaimed tasks calls
   `finding_list` exactly twice; a subsequent `task.claimed` calls it once
   more; a claimed task with an empty history records *known empty*; a
   claimed task whose read raises records *unknown* and is refetched on the
   next reconcile.
7. **B2 Human identity + agent chips.** `human_agents` ∪ registry `type`;
   role markers on every chip (dashboard rows, gates, detail, claims).
   *Independent.*
   Acceptance: an agent registered `type=human` renders the human glyph
   with no config; an id in `human_agents` renders it with no registry
   entry; `claude-code` renders as a role marker; an unregistered id renders
   a plain chip.
8. **B3 Planning metrics.** `planning.py` (pure): starvation v2, overload
   (humans excluded), keystone (two numbers, corpus-wide), stalled,
   throughput medians (open-age proxy, labelled), universe from the
   snapshot, per-metric truncation degradation (D10a), corpus-bound
   degradation. *Needs A1, B1, B2.*
   Acceptance: the table-driven cases in Testing Decisions; the keystone
   case yields `3 downstream / 1 immediately`; a human holding 4 of 5
   claims does not flag overload; a 30-day-old task made ready a minute ago
   reports 30 days under the open-age label; the four stalled-state cases
   (newly claimed + none, old claim + none, read failure, 25h-old finding)
   yield `no findings yet` / `no findings yet` / `finding status unknown` /
   stalled; with the fake's ready read
   truncated and blocked complete, starvation is *unknown* and keystone
   shows both numbers; with blocked truncated and ready complete, starvation
   is exact and keystone shows N only; with `corpus_max_tasks` set below the
   fake's open count, keystone reads *not computed* and every other metric
   renders.
9. **B4 `/tasks/plan` route.** Three sections, human-gate queue first
   (oldest-first, waiter counts), tagged and human-claimed lists, project
   rows with flag chips and tooltips, throughput table with `Hide dormant`
   (cookie + URL), fragments, nav item, client reconcile wiring.
   *Needs B3.*
   Acceptance: the fake renders a human gate above a tagged task above a
   human-claimed task; a starved project shows `fully-claimed` when its only
   ready task is claimed; a dormant project is absent by default and present
   with `?dormant=1`; the throughput column header reads "open-age of ready
   work"; a truncated ready read renders depths as `≥ N` with the
   approximate marker and no starvation chip; a `task.completed` event
   refreshes the projects fragment within the debounce window.

### Strand C — flex (each droppable to 0.4.x)

10. **C1 Latest-finding row line + drawer UI.** Row line from the
    latest-finding map; collapsible drawer over the buffer with the "since
    HH:MM" header. *Needs B1.*
    Acceptance: a row whose task has a warmed finding shows agent + relative
    time + summary; a row without one shows nothing; the drawer header
    carries the boot time.
11. **C2 Side panel `?selected=`.** SSR-open baseline, panel fragment,
    Expand, close preserves filters, mini-graph init on swap. *Needs A5.*
    Acceptance: `GET /tasks?selected=<id>` contains the panel with the
    task's title; the fragment route returns the partial only; closing keeps
    `?project=` intact (JS test); unknown id → not-found panel.
12. **C3 Title badge.** `(N) Lithos Lens` for unseen attention rows,
    cleared on focus. *Independent.*
    Acceptance: JS test — an attention row arriving while unfocused sets
    `(1)`; focus clears it; a steady-state refresh does not re-badge.

Ready at milestone start: A1, A2, B1, B2, C3. Critical path to the
checkpoint: A1 → A3 → A4 → A5.

## Out of Scope

- **Server-side debounced recompute + OOB fragment push** (REQUIREMENTS
  §5.8.4) — X1, with the transition detector desktop notifications need
  (D13).
- **Desktop notifications** — X1. The title badge is the only T2
  notification surface.
- **`lithos_tags` project discovery** — the snapshot-observed universe is
  sufficient (D8); a dormant project beyond the window is a §5B question.
- **Configurable ghost depth** — one hop, by decision (D5).
- **Per-scope snapshot memoisation** — the per-task cache makes assembly
  cheap; revisit only if `lens.tasks.graph` spans say otherwise.
- **A findings warmup-window knob** — not introduced (D11).
- **A claim start time upstream** (new ledger gap #12) — claims expose
  `expires_at` only, so an in-progress task with no findings has no
  observable silence duration; T2 labels it `no findings yet` rather than
  guessing.
- **Bulk graph fetch upstream** (ledger gap #3) — the ~100-call fan-out is
  accepted for September, per the epic; the cache + span counters are the
  evidence for the upstream ask if it is slow in practice.
- **Task-edge events upstream** (ledger gap #1) — the TTL is the workaround;
  the PRD states the staleness bound rather than hiding it.
- **A readiness timestamp upstream** (new ledger gap #11) — true ready-age
  needs Lithos to record when a task last became ready; T2 ships the
  labelled open-age proxy instead and never fakes it with a Lens-side
  lifecycle tracker (it would die on restart and lie after a Lithos
  restart, the same reason T1 rejected a claim ledger).
- **Fetching ghost edges to complete cross-scope cycles** — the cycle
  promise is bounded to fetched topology (D4); Lithos's `task_blocked` is
  the authority for the rest.
- **All write actions** — T3. **Knowledge graph** — K2.
- **Sparklines** in the throughput table — still deferred.

## Further Notes

- **Scale posture.** A project scope is ~100 `edge_list` calls cold under a
  16-wide semaphore against local SQLite; the corpus scope the planning view
  needs is ~330 — already above the page guard, which is why it has its own
  bound (D2). Both are one-off per TTL window and shared across surfaces.
  Beyond `corpus_max_tasks` the planning view degrades visibly; beyond low
  thousands of open tasks, the answer is ledger gap #3, not a smarter cache.
- **Normative docs updated with this PRD.** REQUIREMENTS §3 (env block), §4
  (config reference), §5.1 (consumed reads), §5.2, §5.4.1 (agent chips),
  §5.7 (cache, ghost rule, cycle presentation), §5.8.1, §5.8.4 (buffer;
  recompute deferred to X1), §5.9 (badge always on), §5A.2–§5A.6
  (wireframe, human identity, keystone, stalled states, open-age, dormant
  default, discovery), §16.1, and ROADMAP §3 T2 + ledger #11/#12 were
  changed in the same PR, so implementers and slice reviewers have one
  authority: REQUIREMENTS is the contract, this PRD is its execution plan,
  and the two agree.
- **Why the text baseline carries the acceptance criteria.** Loom's review
  gate is hermetic and headless. A slice whose only observable output is a
  canvas has nothing a panel can assert; that is the shape that produced the
  K1 panel escapes now in the eval corpus. The Cytoscape layer is checked by
  the visual-review artifacts instead.
- **Why not re-implement readiness for the layering.** D4's SCC detection is
  topology over an edge set; `task_blocked` stays the authority on *which*
  task is blocked and supplies the "sole blocker" fact keystone's second
  number needs. Lens computes shape, never state.
- **Spec drift.** Each slice rewrites the relevant `docs/SPECIFICATION.md`
  section (§5.6 detail, a new §5.x graph page, a new §5.x planning view,
  §5.8 for the eviction hook and findings consumer) and the user manual is
  regenerated (`/regen-manual`) when A3 and B4 land.
- **Checkpoint measurement.** `bcaf9379` (2026-09-20) measures the
  intervention rate on strand A's slices through loom against August's 43%.
  Slice granularity here is deliberately T1-like rather than fatter, because
  the rate is counted per run.
