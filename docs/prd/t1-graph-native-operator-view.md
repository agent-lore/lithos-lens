---
title: T1 — Graph-Native Operator View
milestone: T1
status: draft
target_version: 0.2.0
references:
  - docs/ROADMAP.md (milestone sequence, upstream dependency ledger)
  - docs/REQUIREMENTS.md §5 (Tasks View — Operator View)
  - docs/REQUIREMENTS.md §5B (Project Tracking Conventions)
  - lithos docs/SPECIFICATION.md §5.4 (task tools), §7 (schema), §8 (events)
tracked_in: lithos
task_tags: [project:lithos-lens, milestone:t1]
labels: [milestone-t1, tasks-view]
---

# T1 — Graph-Native Operator View

## Problem Statement

Lithos 0.4.0 ships a task graph — `blocks` / `parent_child` /
`discovered_from` / `waits_on_gate` edges, `task` / `epic` / `gate` task
types, and computed ready/blocked frontiers with classified blockers — and
the production corpus already uses it heavily: ~330 open tasks, 21 epics,
live `blocks` chains driving lithos-loom work. The Lens dashboard knows none
of this. It renders a flat open/completed/cancelled list whose only notion of
work state is "does a claim exist", inferred through a `claimed_state` filter
and a per-row claim fan-out capped at `visible_cap`.

Concretely:

- A task that cannot run (blocked by an open predecessor or an unresolved
  gate) is indistinguishable from a task that is ready to pick up. The
  operator's most basic question — **"what can actually happen next?"** — is
  unanswerable.
- Structural failures are invisible: a task whose blocker was **cancelled**
  will never become ready and nobody can see why; a dependency **cycle**
  deadlocks its members silently.
- **Human gates** — tasks explicitly waiting for a person — appear as
  ordinary open rows. The one section of the graph that is the operator's
  *job* has no surface at all.
- **Epics** appear as open tasks that never complete, polluting every count.
- The old attention model's top rule (expired claim) can never fire: Lithos
  filters expired claims out of every read at query time, so a claim past
  expiry simply vanishes.
- New lifecycle events (`task.updated`, `task.reopened`) are dropped by the
  Lens event pipeline, so reopened tasks silently reappear only on manual
  refresh.

T1 rebuilds the dashboard and task detail on the graph, read-only. Graph
*pages* (Cytoscape DAG rendering) are T2; write actions are T3.

## Solution

Replace the flat status groups with a section structure derived from the
ready/blocked frontier, assembled from five parallel Lithos calls:

```
[ Epic strip ]  auth-rework ▓▓▓░ 5/8   loom-arch ▓░░░ 2/9   …
⚠ Needs attention   (unsatisfiable blocker → cycle → waiting human gate
                     → claim expiring → stale open → ready-but-unpicked)
⏸ Gates             (human gates first w/ waiter counts; timer countdowns)
▶ In progress       (open workable tasks with ≥1 active claim)
● Ready             (the ready frontier, unclaimed — "next up")
◼ Blocked           (waiting on predecessors/gates; blocker chips per row)
▸ Not classified    (frontier-limit overflow tail; accuracy banner)
▸ Completed (12 in last 30 days)    [collapsed]
▸ Cancelled (3 in last 30 days)     [collapsed]
```

Partition semantics come from Lithos, never re-derived: `lithos_task_ready`
and `lithos_task_blocked` return only workable (`task_type="task"`) rows and
evaluate gate/timer state at query time, so Lens joins their id-sets against
the master open list instead of re-implementing the readiness predicate.
Every open row appears in exactly one section (single-placement rule).

The task detail page is rebuilt around the same graph: a "why can't this
run" blocker chain (text tree, lazily expanded), parent breadcrumb and
children table, gate context, spawn provenance, and the existing findings
timeline. The event pipeline learns the new lifecycle events and
`Last-Event-ID` replay.

## User Stories

1. As an operator, I want open tasks partitioned into Ready / In progress /
   Blocked sections computed by Lithos, so that "what can happen next" is the
   structure of the page rather than something I infer from claim chips.
2. As an operator, I want each Blocked row to carry blocker chips (the
   blocking task's title, or the gate's name and type), so that I can see at
   a glance what each task is waiting for.
3. As an operator, I want a task whose blocker was cancelled to surface in
   Needs attention with an `unsatisfiable` chip naming the cancelled blocker,
   so that permanently stuck work is impossible to miss.
4. As an operator, I want dependency cycles surfaced in Needs attention with
   the cycle members named, so that deadlocked chains are visible instead of
   silently never becoming ready.
5. As an operator, I want a Gates section listing open gates — human gates
   first, oldest first, each showing how many tasks it blocks — so that the
   work waiting *on me* is one glance away.
6. As an operator, I want timer gates to show a live countdown and the
   dashboard to refresh itself when the earliest visible `ready_at` passes,
   so that timer expiry moves tasks from Blocked to Ready without me
   refreshing (Lithos emits no event for this).
7. As an operator, I want human gates that have waited longer than a
   threshold to also appear in Needs attention, so that a forgotten approval
   eventually escalates.
8. As an operator, I want an epic strip showing each open epic as a progress
   chip (`5/8` done, from its recursive subtree), so that epics roll up
   instead of polluting the open sections.
9. As an operator, I want clicking an epic chip to scope the dashboard to
   that epic's descendants, so that I can review one initiative in isolation.
10. As an operator, I want claims rendered inline from `with_claims=true`
    with no per-row fan-out and no "Unknown claim state" tail, so that claim
    display is complete and the `visible_cap` accuracy machinery disappears.
11. As an operator, I want a claim nearing its `expires_at` to flag the row
    in Needs attention, so that likely-abandoned work surfaces *before* the
    claim silently vanishes (expired claims are unobservable in Lithos).
12. As an operator, I want a *ready* task that has sat unclaimed past a
    threshold flagged — but a *blocked* unclaimed task never flagged — so
    that the "fleet not picking up work" signal has no structural false
    positives.
13. As an operator, I want a claimed-but-blocked task to render in In
    progress with a `blocked` decoration, so that an agent holding a claim on
    infeasible work is legible as the anomaly it is.
14. As an operator, I want Completed and Cancelled sections windowed by
    `resolved_since` (not `created_at`), so that a task created months ago
    and finished yesterday shows up as recent work.
15. As an operator, I want a reopened task to carry a `reopened` marker and
    move sections live when `task.reopened` arrives, so that lifecycle
    reversals are visible in real time.
16. As an operator, I want the task detail page to state why a task cannot
    run — one line per blocker with live status ("blocked by *Design schema*
    (open, claimed by agent-zero)", "waiting on gate *Human review*",
    "blocker cancelled — unsatisfiable", "cycle: A → B → A") — so that the
    detail page answers the question the row chip raises.
17. As an operator, I want to expand any unfinished blocker in that chain to
    see *its* blockers (lazily, a level at a time, bounded depth), so that I
    can walk a dependency chain without loading a whole graph page.
18. As an operator, I want the detail page to show the parent breadcrumb up
    to the root epic and a children table with per-child status, so that
    hierarchy is navigable in both directions.
19. As an operator viewing a gate's detail page, I want its `gate_type`,
    timer `ready_at`, advisory metadata, and the list of tasks waiting on it,
    so that I can judge what resolving the gate would unblock.
20. As an operator, I want spawn provenance on the detail page ("Discovered
    while working on: X" / "Spawned follow-ons: …"), so that emergent work is
    traceable to its origin.
21. As an operator, I want the detail page fetched via `lithos_task_get` with
    a proper not-found envelope, so that deep links to deleted tasks fail
    cleanly (and the three-list scan in `find_task` is deleted).
22. As an operator, I want the `agent` filter to match creator **or**
    claimer, so that "everything agent-zero is involved in" is one filter.
23. As an operator, I want project filtering to honor both `metadata.project`
    and `project:` tags (both conventions are live in the corpus, and they
    disagree), so that no task is invisible to its project view.
24. As an operator, I want legacy `?claimed_state=` URLs silently ignored, so
    that old bookmarks degrade gracefully.
25. As an operator, I want `task.updated` and `task.reopened` events to
    trigger the same debounced reconciliation as other task events, so that
    title edits and reopens propagate without reload.
26. As an operator, I want the Lens upstream subscription to resume from
    `Last-Event-ID` on reconnect with a full-refresh broadcast as backstop,
    so that brief disconnects don't drop events or thundering-rerender tabs.
27. As an operator on a pre-task-graph Lithos, I want Lens to detect missing
    frontier tools and fall back to the flat 0.1.0 dashboard with a "graph
    features need Lithos ≥ 0.4" notice, so that version skew degrades rather
    than breaks.
28. As an operator, I want summary counters (attention / gates / in progress
    / ready / blocked) in the header, and a "Not classified" tail with an
    accuracy banner if the frontier `limit` ever truncates, so that counts
    stay honest at any scale.

## Implementation Decisions

### Data assembly: five parallel calls

`load_dashboard` fans out (asyncio.gather):

| Data | Call |
|---|---|
| Master open set — every open task incl. epics/gates, claims inline | `lithos_task_list(status="open", with_claims=True)` |
| Ready partition | `lithos_task_ready(limit=frontier_limit, with_claims=True)` |
| Blocked partition + structured blockers | `lithos_task_blocked(limit=frontier_limit)` |
| Recently closed | `lithos_task_list(status="completed"\|"cancelled", resolved_since=window)` |
| Agent filter dropdown | `lithos_agent_list()` |

Plus one `lithos_task_children(epic_id, recursive=True, include_closed=True)`
per open epic for the strip (epic count is small; gathered concurrently).

Join model, in a new Foundation module **`frontier.py`** (pure functions,
pattern of `tasks.py`): index the master set by id; workable open tasks
classify as `in_progress` (≥1 inline claim — `task_blocked` doesn't return
claims, the master list supplies them), else `ready` / `blocked` by frontier
membership, else `unclassified` (only possible under `frontier_limit`
truncation). Epics and gates never enter the workable sections — Lithos
excludes them from both frontiers, so the partition is clean by construction.
**Never re-implement the readiness predicate in Lens** — timer gates and
NULL-safety are evaluated inside Lithos at query time; re-deriving readiness
from edges is a correctness trap.

### Needs-attention severity model v2

Ordered rules, evaluated in `frontier.py` over the joined snapshot:

| # | Rule | Source | Knob (default) |
|---|------|--------|----------------|
| 1 | Unsatisfiable blocker (`kind="blocker_unsatisfiable"`) | `task_blocked` blockers | — |
| 2 | Dependency cycle (`kind="cycle"`; message names members) | `task_blocked` blockers | — |
| 3 | Human gate waiting too long | gate rows + `created_at` | `gate_waiting_attention_hours` (24) |
| 4 | Claim expiring soon (`expires_at − now` below threshold) | inline claims | `claim_expiring_soon_minutes` (10) |
| 5 | Stale open (workable, old `created_at`) | master list | `stale_open_age_days` (7) |
| 6 | Ready but unclaimed too long | ready join | `unclaimed_ready_age_minutes` (60) |

Deliberate changes from the pre-graph model, both forced by observed Lithos
semantics: **`expired-claim` is removed** (expired claims are filtered out of
every read at query time — unobservable; rule 4 is the observable
replacement, and a Lens-side claim ledger is rejected: it dies on restart and
lies after Lithos restarts). **`unclaimed-old` becomes ready-aware** (rule 6)
— a blocked task being unclaimed is correct behavior, not a warning. Rules 1
and 2 flag structural failures that need operator intervention; their rows
are *removed* from Blocked and promoted (single-placement rule). Chrome
carries over from the previous design: reason chips, severity-then-oldest
ordering, "All systems healthy" stripe when empty, hide toggle, "Why this
task is here" block on the detail page.

### Task detail

Load via `lithos_task_get` + `lithos_task_status` (claims) +
`lithos_task_edge_list(task_id, direction="both")` + `lithos_finding_list`,
gathered. Delete `find_task` (three-list scan) and `_enrich_open_tasks`
(claim fan-out). Layout adds, over the existing page: task-type badge
(task/epic/gate + `gate_type`); blocker block — one line per immediate
blocker with live status, sourced from the task's `task_blocked` entry when
blocked (or `edge_list` incoming `blocks`/`waits_on_gate` + per-predecessor
`task_get` otherwise); each unfinished blocker line carries an HTMX expander
loading *its* blockers one level deeper (bounded depth 5; cycles render a
callout instead of recursing); parent breadcrumb (incoming `parent_child`,
recursed to root — the single-parent forest guarantee makes this a simple
chain); children table (`lithos_task_children(recursive=False)`, "show full
subtree" toggle); gate context (countdown for timers, advisory metadata
key/value table, waiters via `edge_list(direction="outgoing",
types=["waits_on_gate"])`); `discovered_from` provenance both directions;
`resolved_at` + `outcome`; reopened marker (from the `[Reopened]` finding).
All text-first — the Cytoscape mini-graph is T2.

### Event pipeline

- Consume `task.updated` and `task.reopened` (both carry `task_id`, flow
  through the existing normalizer; both `requires_refresh=true`).
- Consume `agent.registered` via a new allow-list `SYSTEM_EVENT_TYPES`:
  the drop-if-no-`task_id` rule becomes scope-aware — task-scoped types still
  require `task_id`; system events pass with `task_id=""` and
  `requires_refresh=False` (they invalidate the agent-dropdown data only).
- Do **not** subscribe to `edge.upserted` — it is the knowledge-graph event
  (note ids in payload). No task-edge event exists upstream (ledger ask #1);
  the normalizer maps a future `task_edge.upserted` to
  `task_id = to_task_id` when it lands.
- Extend the upstream `types=` filter to the nine consumed types.
- `EventHub` records the last received event id and sends `Last-Event-ID` on
  reconnect (replays from Lithos's ring buffer); every reconnect still
  broadcasts one synthetic `lens.refresh` to browser subscribers as the
  correctness backstop (replay beyond the buffer is impossible). The `lens.*`
  namespace is reserved for Lens-internal synthetic events.
- Timer-gate self-refresh: the dashboard embeds `min(ready_at)` over visible
  open timer gates as a data attribute; `tasks.js` schedules a one-shot
  fragment refresh at that instant.
- Browser JS: `task.updated`/`task.reopened` → `scheduleReconcile()`; unknown
  types keep falling through to `requires_refresh` handling.

### Filters and URL contract

- Survive: `project` (multi; matches `metadata.project` **or** `project:`
  tag — warn to telemetry when both present and disagreeing), `tag`, `agent`
  (creator OR claimer), `since`.
- Die: `claimed_state` (parsed and ignored), status-as-filter (sections
  express it; `?status=` accepted only as a section-collapse hint),
  `visible_cap` claim enrichment and the claim-accuracy banner machinery.
- URL: `?project=x&project=y&tag=cli&agent=agent-zero&since=2026-06-01&collapsed=completed,cancelled`.
- Project/tag/agent filtering applies client-side (in Lens, over the joined
  snapshot) rather than pushed upstream: one fetch serves all projections,
  and no upstream call can express the metadata-OR-tag project match. Free at
  ~330 open tasks.

### Config

```toml
[lithos-lens.tasks]
frontier_limit = 500              # limit for task_ready / task_blocked
gate_waiting_attention_hours = 24
claim_expiring_soon_minutes = 10
unclaimed_ready_age_minutes = 60
stale_open_age_days = 7
project_convention = "both"       # metadata | tag | both
```

`visible_cap` is deprecated: parsed with a deprecation log line, unused. The
default `frontier_limit=500` clears the current production frontier (~310
workable open tasks) with headroom; truncation is survivable (Not-classified
tail + banner) but should be rare.

### MCP / SSE dependencies

New client methods on `LithosClient` (+ protocol + fakes): `task_ready`,
`task_blocked`, `task_get`, `task_children`, `task_edge_list`. New
normalizers: `BlockerRecord` (all four kinds), `EdgeRecord` (all four types),
`TaskRecord` gains `task_type` and `resolved_at`. Feature detection: if
`lithos_task_ready` fails with a tool-not-found error, set a
`graph_available=False` flag for the process and render the legacy flat
dashboard with the version notice (story 27).

SSE: consumed types become `task.created`, `task.claimed`, `task.released`,
`task.completed`, `task.cancelled`, `task.updated`, `task.reopened`,
`finding.posted`, `agent.registered`.

### Telemetry

`lens.tasks.list` extended with per-section counts and truncation flag;
`lens.tasks.frontier_join` (join duration, unclassified count);
`lens.tasks.detail` extended with blocker/children counts;
`lens.tasks.project_convention_conflict` (story 23 warning).

## Testing Decisions

Tests assert external behavior: section membership in rendered HTML, blocker
chain text, event-driven fragment moves. Extend the existing
`TaskFakeLithosClient` (tests/test_tasks_mvp.py) with the five new tool
methods, edges, blockers, task types, and `resolved_since` filtering; the
fake is the readiness oracle — Lens must not compute readiness, so the fake
returns explicit ready/blocked sets.

- **frontier.py (pure, table-driven)**: every classification branch —
  in-progress beats ready/blocked; unclassified only under truncation;
  epics/gates never workable; attention rules 1–6 each fire and each
  respect their knob; single-placement (an unsatisfiable row is in attention,
  not Blocked); claimed-but-blocked decoration.
- **Normalizers**: round-trip all four blocker kinds and all four edge types;
  `task_type`/`resolved_at` defaults for older payloads.
- **Dashboard rendering**: blocked task renders in Blocked with predecessor
  title chip; completing the predecessor in the fake moves it to Ready;
  gate row shows "blocks N"; epic chip shows `5/8`; resolved_since windows
  (created 60d ago, resolved yesterday → visible).
- **Detail**: blocker lines with live status; lazy expansion two levels;
  cycle callout instead of recursion; parent breadcrumb; gate waiters;
  provenance lines; not-found envelope → not-found panel.
- **Events** (extend test_tasks_sse.py): `task.reopened` moves a row out of
  Completed within the debounce window; `agent.registered` forwarded with
  empty task_id and no dashboard refresh; `Last-Event-ID` sent on reconnect;
  reconnect broadcasts `lens.refresh`.
- **Degraded**: frontier tools missing → flat fallback + notice; Lithos
  unreachable → existing banner; empty corpus → empty states.

Coverage ≥ 80% on `frontier.py` and the new normalizers.

## Tracer-bullet vertical slices

1. **Client graph reads + data model.** `task_ready` / `task_blocked` /
   `task_get` / `task_children` / `task_edge_list` client methods;
   `TaskRecord.task_type`/`resolved_at`; `BlockerRecord`/`EdgeRecord`
   normalizers; fake extensions. Acceptance: normalizers round-trip all four
   blocker kinds and edge types.
2. **Frontier join + Ready/In progress/Blocked sections.** `frontier.py`;
   three workable sections with blocker chips; delete `claimed_state`,
   `_apply_claim_filter`, `_enrich_open_tasks`. Acceptance: open-predecessor
   task renders in Blocked with the predecessor's title chip; completing the
   predecessor (fake) moves it to Ready.
3. **Needs attention v2.** Six-rule model, chips, de-dup, healthy stripe.
   Acceptance: cancelled-blocker task renders only in Needs attention with an
   `unsatisfiable` chip; a fresh blocked unclaimed task does not appear.
4. **Gates section.** Gate rows with type badge, waiter counts, timer
   countdown + one-shot self-refresh at `min(ready_at)`. Acceptance: human
   gate lists "blocks N tasks"; lapsing a timer refreshes the dashboard.
5. **Epic rollup strip.** Recursive children counts, progress chips,
   click-to-scope. Acceptance: epic with 5/8 done renders `5/8`; clicking
   scopes sections to descendants.
6. **Event pipeline upgrade.** New types, scope-aware normalizer, `types=`
   extension, `Last-Event-ID` + reconnect broadcast. Acceptance: `task.reopened`
   moves a row live; `agent.registered` causes no dashboard refresh.
7. **Task detail rebase (text-first).** `task_get`+`task_status` loading,
   type badges, level-1 blocker chain, breadcrumb, children table,
   provenance, outcome. Delete `find_task`. Acceptance: blocked task's detail
   lists each blocker with live status; spawned task shows its source.
8. **Blocker chain lazy expansion.** HTMX per-level, depth ≤5, cycle-safe.
   Acceptance: A←B←C expands two levels; a cycle renders the callout.
9. **Filters rebase.** Project (metadata ∪ tag), agent creator-OR-claimer,
   `claimed_state` ignored. Acceptance: `?agent=X` matches a task X merely
   claims; `?claimed_state=…` is a no-op.
10. **Resolved-since windows + reopened markers.** Acceptance: task created
    60d ago, resolved yesterday appears in the 30d window; reopened task
    carries the marker.
11. **Counters + truncation tail.** Header counts; Not-classified tail.
    Acceptance: with `frontier_limit=2` and 3 blocked tasks, one row lands in
    the tail with the banner.
12. **Empty/degraded states.** No tasks; all healthy; Lithos unreachable;
    frontier tools missing → flat-list fallback + "needs Lithos ≥ 0.4".
    Acceptance: all four branches render.

Slices 1–2 are foundational; 3–5 and 7 depend on them; 6, 9–12 are
independent once 2 lands; 8 depends on 7.

## Out of Scope

- **Cytoscape rendering** — the `/tasks/graph` DAG page and the detail-page
  mini-graph are T2. T1's blocker chains are text (they remain the no-JS
  baseline afterwards).
- **Planning view rebase** (starvation/keystone/throughput) — T2.
- **Operator ergonomics strand** (recent-findings rolling buffer + drawer,
  latest-finding row line, agent role chips, side panel `?selected=`,
  title-badge notifications, debounced server-side metric recompute) — T2.
  Their requirements survive in REQUIREMENTS §5.
- **All write actions** (gate approval, reopen, cancel, create, edges) — T3.
- **LLM curation, desktop notifications** — X1.
- **Bulk graph fetch / task-edge events** — upstream asks (ROADMAP ledger
  #1, #3); T1 neither needs nor works around them.

## Further Notes

- **Scale posture**: the live deployment has ~330 open tasks across ~20
  projects. The five-call assembly is one round-trip each on the shared MCP
  session against indexed SQLite; the joined snapshot is small. Client-side
  filtering and `frontier_limit=500` are sized for low thousands; beyond
  that, the answer is upstream ask #3 (bulk graph fetch), not Lens-side
  caching heroics.
- **Project convention**: `metadata.project` and `project:` tags disagree in
  the live corpus today (e.g. lithos-loom: 87 via metadata vs 68 via tag).
  `project_convention="both"` is a permanent-looking workaround; ledger ask
  #7 tracks unification.
- **Error envelopes**: Lithos 0.4.0 failures are
  `{status: "error", code, message}`. The client's error mapping must expose
  `code` (needed for `task_not_found` vs tool-missing detection here, and a
  prerequisite T3 builds on).
- **Spec drift**: when T1 ships, `docs/SPECIFICATION.md` §5.3–5.5 must be
  rewritten to describe the section model, and the user manual regenerated
  (`/regen-manual`).
