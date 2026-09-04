# Lithos Lens — Roadmap

Version: 1.3.0
Date: 2026-09-04
Status: Active

This is the only document that tracks milestone sequence and status. It was
introduced when the pre-task-graph planning docs were retired (see §5 for the
mapping from the old milestone numbering).

## 1. Purpose and document ownership

Each concern has exactly one owning document:

| Concern | Owner |
|---|---|
| Shipped behavior (the truth about what the code does) | [`docs/SPECIFICATION.md`](./SPECIFICATION.md) |
| Durable product requirements (what Lens should be) | [`docs/REQUIREMENTS.md`](./REQUIREMENTS.md) |
| Milestone sequence, status, and upstream dependencies | this document |
| Execution detail for the next milestone on each track | PRDs in [`docs/prd/`](./prd/), written just-in-time |

PRDs exist only for the next milestone per track. Milestones further out live
here as summaries and get their PRD when they come up — a deliberate response
to an earlier round of PRDs that went stale when Lithos shipped its task graph
before they were implemented.

Execution work for an in-flight milestone is tracked as **Lithos tasks**
(tags `project:lithos-lens`, `milestone:<id>`), not GitHub issues — see
[`docs/agents/issue-tracker.md`](./agents/issue-tracker.md). Lens dogfoods the
task graph it visualizes.

## 2. Shipped state

Lens 0.1.0 delivered the original three-milestone plan (previously tracked in
a now-retired `IMPLEMENTATION_CHECKLIST.md`): the **Common Core** (FastAPI app,
typed TOML+env config, structured logging, single-session MCP-over-SSE client,
startup agent registration, degraded boot, `/health`), the **Tasks MVP**
(flat open/completed/cancelled dashboard with `claimed_state` filtering,
claim enrichment, task detail with findings and note links, minimal
`/note/{id}` renderer), and **Tasks SSE** (shared upstream `/events`
subscription, normalized browser fan-out at `/tasks/events`, optimistic row
updates, reconnect and polling fallback).

**Lens 0.3.0** adds the two milestones below it in the table: **T1**, the
graph-native operator view — the dashboard rebuilt on Lithos's ready/blocked
frontiers, the six-rule Needs-attention severity model, the Gates section, the
epic rollup strip, per-side truncation marking, and a task detail page carrying
blocker chains, provenance and children — and **K1**, the knowledge surface:
`/note/{id}` as a real document page with safe server-rendered markdown,
wiki-link resolution, metadata chips and a related panel, plus the `/knowledge`
landing page with search, recent notes and tag browse.

`docs/SPECIFICATION.md` describes this state precisely, and is the document to
update when behavior changes.

Everything below builds on that foundation. The driver for the current
sequence is **Lithos 0.4.0's task graph**: typed task edges (`blocks`,
`parent_child`, `discovered_from`, `waits_on_gate`), task types
(`task`/`epic`/`gate`), computed ready/blocked frontiers with classified
blockers, gates with timer auto-resolve, spawn/reopen lifecycle, and new
events — none of which the 0.1.0 dashboard understands. The production
deployment already runs at a scale that makes this urgent: ~330 open tasks
across ~20 projects, with live `blocks` chains and epics in active use.

## 3. Milestone sequence

Two tracks — **T** (tasks surface) and **K** (knowledge surface) — plus one
cross-surface milestone (**X**). Order below is the intended landing order;
T and K milestones touch disjoint modules and may overlap in practice.

| # | Id | Surface | Content | Status | PRD | Target |
|---|----|---------|---------|--------|-----|--------|
| 1 | **T1** | Tasks | Graph-native Operator View (read-only) | **shipped** | [t1-graph-native-operator-view.md](./prd/t1-graph-native-operator-view.md) | 0.3.0 |
| 2 | **K1** | Knowledge | Note view, wiki-links, related panel, search | **shipped** | [k1-knowledge-note-view.md](./prd/k1-knowledge-note-view.md) | 0.3.0 |
| 3 | **T2** | Tasks | Task relationship graphs: graph pages, exploration mode, side panel, mini-graph | **next** | [t2-task-relationship-graphs.md](./prd/t2-task-relationship-graphs.md) | 0.4.0 |
| 3b | **T2b** | Tasks | Operational insights: planning view rebase, findings feed, operator ergonomics | planned | — | 0.4.x |
| 4 | **T3** | Tasks | Curated write actions | planned | — | 0.5.0 |
| 5 | **K2** | Knowledge | Knowledge graph view + knowledge event wiring | planned | — | — |
| 6 | **K3** | Knowledge | Cognitive search (`lithos_retrieve`) + node stats | planned | — | — |
| 7 | **X1** | Both | LLM finding-curation + desktop notifications | planned | — | — |
| 8 | **K4** | Knowledge | Feed, feedback, cited-by panel | planned | — | — |
| 9+ | pool | Knowledge | Conflict-resolution UI, note comparison, reading paths | deferred | — | — |

### T1 — Graph-Native Operator View (read-only) — SHIPPED in 0.3.0

Rebuild the dashboard on `lithos_task_ready` / `lithos_task_blocked` instead
of claim-state inference. Sections: epic rollup strip → Needs attention
(graph-aware severity model: unsatisfiable blockers, cycles, waiting human
gates, expiring claims, stale open, ready-but-unpicked) → Gates → In progress
→ Ready → Blocked → collapsed Completed/Cancelled on `resolved_since`. Task
detail gains blocker chains (lazily expanded text tree), hierarchy, gate
context, and spawn provenance. Event pipeline learns `task.updated`,
`task.reopened`, `agent.registered`, and `Last-Event-ID` replay. The
`claimed_state` filter and `visible_cap` claim fan-out are retired. Full
detail: the T1 PRD.

Landed across twelve slices; `docs/SPECIFICATION.md` §5.3–§5.6 describes the
shipped behavior. One deviation from the plan above: `claimed_state` and the
visible cap were **not** retired — both still exist and are documented, because
the degraded groups (claims unknown, unclassified) need them to say what Lens
does not know. Nothing from the milestone remains open.

### K1 — Knowledge Note View + Search — SHIPPED in 0.3.0

`/note/{id}` becomes a real document page: server-rendered markdown (safe by
default), clickable wiki-links via a Lens-side resolver route, metadata chips,
a related/back-links panel from `lithos_related`, and a "produced by task"
chip. A `/knowledge` landing page adds `lithos_search` and recently-updated
browsing; the Knowledge nav item goes live. Full detail: the K1 PRD.

Landed across seven slices; `docs/SPECIFICATION.md` §5.7 describes the shipped
behavior. One item remains open: the `lens.knowledge.*` telemetry the PRD
specifies was never wired (Lithos task `cdce170a`). It is tracked as a gap
rather than as unshipped scope — every user-facing surface in the milestone is
live.

### T2 — Task Relationship Graphs

PRD: [t2-task-relationship-graphs.md](./prd/t2-task-relationship-graphs.md)
(2026-09-02; **narrowed 2026-09-04**: the first draft bundled graph pages,
the planning view and ergonomics, and two review rounds showed most of its
correctness machinery served five project-row metrics rather than the
graph — so the analytics moved to T2b and 0.4.0 became relationship-first;
the W38 checkpoint is unaffected, it measures graph slices). One strand,
**relationship-first**, so the graph's value can be judged on its own:

- **Graph pages**: `/tasks/graph?project=<slug>|epic=<id>` renders the
  dependency graph — topological text layers with cycles condensed into
  their own layer, the longest blocking chain, and the hierarchy tree as the
  no-JS baseline; Cytoscape as enhancement with arrowheads, a plain-language
  legend, dependency edges by default and hierarchy/provenance as overlays,
  isolated tasks folded away, a **focus** exploration mode (ancestors and
  descendants lit, the rest dimmed), and search. Cross-scope endpoints are
  one-hop ghosts; Lithos's `task_blocked` stays the authority on cycle
  membership. Backed by a per-task `edge_list` cache shared by every graph
  surface.
- **Side panel** (`?selected=`): one implementation for dashboard rows and
  graph nodes — blockers, dependents, parent, and the task's **downstream
  impact within the scope** ("frees N, M immediately").
- **Detail mini-graph**: two hops upstream, one downstream, above the text
  blocker chain.

Seven slices; five of the correctness decisions (per-task cache and its
staleness bound, bounded cycle promise, satisfied-edge drop, ghost rule,
scoped and truncation-aware blocked read) carry over from the first draft.

### T2b — Operational Insights

The two strands split out of T2 on 2026-09-04, sequenced after it so that
the graph is judged before analytics are layered on it. Summary only — the
PRD is written when T2b comes up, and it inherits the decisions already
recorded in REQUIREMENTS §5A, §5.8.4, §5.9 and §5.4.1:

- **Planning view rebase** (`/tasks/plan`): starvation on the ready frontier
  (fully-blocked vs fully-claimed), corpus-wide keystone (two honest numbers)
  over a corpus scope with its own `corpus_max_tasks` bound, agent overload
  (humans excluded), stalled detection that fires on evidence only, throughput
  on `resolved_since` with median time-to-resolve and median **open-age** of
  ready work (a labelled proxy — ledger #11), per-metric degradation when a
  frontier read truncates. Human-actionable section led by the human-gate
  queue.
- **Operator ergonomics**: the recent-findings buffer (latest-finding-per-task,
  in-progress-only warmup; ledger #12), latest-finding line per row + drawer,
  agent chips with role markers and the registry ∪ config human definition,
  title badge. Debounced **server-side** metric recompute stays at X1 with the
  transition detector it serves.

### T3 — Curated Write Actions

Lens's read-only contract relaxes to a small operator-console action set,
gated behind `[writes] enabled` (default false): approve/complete human gates
(surfacing `unblocked[]`), reopen (surfacing `reblocked[]`), cancel with
consequence-aware confirmation ("will strand N dependents"), create
task/epic/gate, and add dependency edges with cycle-rejection surfaced from
the Lithos error envelope. Writes are attributed to a named human operator
(cookie-backed, registered via `lithos_agent_register(type="human")`),
audit-logged, Origin-checked, and always refresh-after-write — no optimistic
mutations. No auth beyond the trusted-network boundary; see REQUIREMENTS
Part B for the full contract.

### K2 — Knowledge Graph View

`/knowledge/graph?focus=<id>` ego-graph first, global mode second: typed LCMA
edges colored per type, wiki-links thin grey, provenance dotted, `contradicts`
edges red with unresolved `conflict_state` emphasized. Data via
`lithos_related` + `lithos_edge_list`; freshness via `note.*`/`edge.upserted`
events (with debounced-refetch fallback for id-less watcher events); node
caps with a "refine your filters" banner.

### K3 — Cognitive Search + Node Stats

`lithos_retrieve` becomes the default `/knowledge` engine (silent fallback to
`lithos_search` on error; "fast search" toggle). Result cards gain scout
chips, expandable reasons, and a salience bar; receipts render as footer
provenance text. Note pages gain a retrieval-stats panel (`lithos_node_stats`).
Hard rule: Lens never passes `task_id` to `lithos_retrieve` — human browsing
must not write agent working memory.

### X1 — LLM Curation + Desktop Notifications

The former M3 PRD, rebased: optional LiteLLM client, "Most significant
findings" curation toggle with complexity slider, MCP-synthesis preference
layer, and opt-in desktop notifications — retargeted at the new attention
triggers (human gate waiting, task entering Needs attention, task unblocked).
Lands after the surfaces it augments (T2's graph, T2b's planning view, T1's
attention model), and **builds the server-side debounced recompute** here —
the transition detector notifications need is the first consumer of a
server-held derived state.

### K4 — Feed, Feedback, Cited-By

The knowledge feed (chronological browsing), feedback affordances via
`lithos_note_update` (frontmatter patch — the old read-then-rewrite contract
is obsolete), and the "cited by findings in tasks X, Y" reverse panel —
**gated on upstream ask #9** (do not ship the O(all-tasks) scan workaround).

### Deferred pool

Conflict-resolution UI (`lithos_conflict_resolve` — the first knowledge
write), note comparison, and reading paths. Requirements are preserved in
REQUIREMENTS Part C; they re-enter the sequence when the knowledge surface
has users.

## 4. Upstream Lithos dependency ledger

Gaps in Lithos that shape or gate Lens milestones. Each should become a task
or issue against the `lithos` repo; Lens documents its workaround until then.

| # | Gap | Ask | Impact on Lens |
|---|-----|-----|----------------|
| 1 | `lithos_task_edge_upsert` emits no event | `task_edge.upserted` event (`from_task_id`, `to_task_id`, `type`, `agent`) | Other agents' dependency edits are invisible until the next task event. T3 covers its own writes with synthetic internal `lens.edge_upserted` events. |
| 2 | No `lithos_task_edge_delete` | Edge delete (or tombstone) tool | Mistaken dependencies are permanent; re-parenting is impossible (`parent_exists` is a dead end). T3 UI must say so honestly. **Top ask.** |
| 3 | No bulk graph fetch | `lithos_task_graph(project \| task_ids)` → `{tasks, edges}` | T2 assembles graphs via N per-task `edge_list` calls (semaphored, per-task cached, ~100 calls/project); T2b's corpus scope would be ~330. One indexed SQL join upstream collapses this to one call. |
| 4 | Expired claims unobservable (lazy query-time filtering) | Expose recently-expired claims, or a `claim.expired` event | The old "expired claim" attention rule is impossible; T1 substitutes a pre-expiry warning. True abandoned-work detection stays blocked. |
| 5 | Timer-gate resolution emits no event (query-time evaluation) | `gate.resolved` event | T1 self-schedules a dashboard refresh at `min(ready_at)` of visible timer gates. |
| 6 | `task_cancel.reason` not persisted (event payload only) | Persist cancel reason | Lens shows it live but loses it on reload. |
| 7 | Project convention split: `metadata.project` (filters, spawn inheritance) vs `project:` tags (existing corpus) — both in active use with disagreeing counts | Pick one canonical convention | Lens honors both (`project_convention = "both"`), warns on disagreement. |
| 8 | No MCP response resolves inline `[[target]]` wiki-links to note ids (`lithos_read.links[]` is `{target, display}`) | Add `id \| null` per link entry | K1 ships a Lens-side resolver route (UUID / path probe / title disambiguation); inline links resolve per-click rather than per-render. |
| 9 | `lithos_finding_list` requires `task_id`; `finding.posted` lacks `knowledge_id` | Optional `knowledge_id` filter; add `knowledge_id` to the event payload | Gates K4's cited-by panel entirely. |
| 10 | Retrieval receipts have no MCP read surface | Receipt read tool (optional) | K3 shows `receipt_id` as text without click-through. |
| 11 | No readiness timestamp: `lithos_task_ready` returns `created_at` only, and nothing records when a task last *became* ready | `ready_since` on ready rows (or a `task.ready` event) | True ready-age ("how long has available work sat") is unobservable. T2b's Planning View ships median **open-age** of ready work, labelled as the proxy it is; a Lens-side lifecycle tracker is rejected for the same reason as the claim ledger (dies on restart, lies after a Lithos restart). |
| 12 | Claims expose `expires_at` but no `claimed_at` (`with_claims` rows and `lithos_task_status`) | `claimed_at` on claim records | An in-progress task with no findings has no observable silence duration, so T2b's stalled rule cannot fire on it; Lens labels it `no findings yet` instead of guessing. |

Minor, noted: `task.updated` carries only `task_id` (forces refetch — fine at
current scale); task events carry empty `tags`, so `/events?tags=` cannot
scope task streams by project.

## 5. Legacy milestone mapping

Two historical numbering schemes existed; both are retired.

| Old reference | Where it went |
|---|---|
| Legacy checklist M0 (Common Core), M1 (Tasks MVP), M2 (Tasks SSE) | Shipped in 0.1.0 — §2 above |
| Legacy checklist M3 (Optional LLM) | **X1** |
| PRD `milestone-1-operator-view.md` (section-structured operator view) | Rewritten graph-native as **T1**; side panel → **T2**; drawer, agent chips, title badge → **T2b**; server-side recompute → **X1** |
| PRD `milestone-1-5-planning-view.md` (planning view) | Rebased on the task graph into **T2b** (originally slotted in T2; split out 2026-09-04) |
| PRD `milestone-3-llm-curation-and-desktop-notifications.md` | **X1** |
| REQUIREMENTS §17 implementation plan (old M0–M11) | Deleted; tasks milestones → T1–T3, knowledge milestones → K1–K4 + pool, LLM milestones → X1 |
