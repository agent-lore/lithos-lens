# Lithos Lens — Roadmap

Version: 1.0.0
Date: 2026-07-07
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
updates, reconnect and polling fallback). `docs/SPECIFICATION.md` describes
this state precisely.

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
| 1 | **T1** | Tasks | Graph-native Operator View (read-only) | prd | [t1-graph-native-operator-view.md](./prd/t1-graph-native-operator-view.md) | 0.2.0 |
| 2 | **K1** | Knowledge | Note view, wiki-links, related panel, search | prd | [k1-knowledge-note-view.md](./prd/k1-knowledge-note-view.md) | 0.3.0 |
| 3 | **T2** | Tasks | Graph pages, planning view rebase, operator ergonomics | planned | — | 0.4.0 |
| 4 | **T3** | Tasks | Curated write actions | planned | — | 0.5.0 |
| 5 | **K2** | Knowledge | Knowledge graph view + knowledge event wiring | planned | — | — |
| 6 | **K3** | Knowledge | Cognitive search (`lithos_retrieve`) + node stats | planned | — | — |
| 7 | **X1** | Both | LLM finding-curation + desktop notifications | planned | — | — |
| 8 | **K4** | Knowledge | Feed, feedback, cited-by panel | planned | — | — |
| 9+ | pool | Knowledge | Conflict-resolution UI, note comparison, reading paths | deferred | — | — |

### T1 — Graph-Native Operator View (read-only)

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

### K1 — Knowledge Note View + Search

`/note/{id}` becomes a real document page: server-rendered markdown (safe by
default), clickable wiki-links via a Lens-side resolver route, metadata chips,
a related/back-links panel from `lithos_related`, and a "produced by task"
chip. A `/knowledge` landing page adds `lithos_search` and recently-updated
browsing; the Knowledge nav item goes live. Full detail: the K1 PRD.

### T2 — Graph Pages, Planning View, Operator Ergonomics

Three strands:

- **Graph pages**: `/tasks/graph?project=<slug>|epic=<id>` renders the
  dependency DAG (Cytoscape; topological text layers as the no-JS baseline;
  status/type-encoded nodes; cycle callouts; dimmed cross-scope ghost nodes),
  backed by a cached per-task `edge_list` fan-out. Task detail gains a 1–2-hop
  dependency mini-graph above its text blocker chain.
- **Planning view rebase** (`/tasks/plan`): starvation redefined on the ready
  frontier (fully-blocked vs fully-claimed sub-classification), keystone-task
  metric ("completing this unblocks N tasks", from the shared graph snapshot),
  agent-overload flag, stalled detection, throughput on `resolved_since` with
  median time-to-resolve and median ready-age. Human-actionable section gains
  the human-gate queue.
- **Operator ergonomics** carried forward from the pre-graph operator-view
  plan: recent-findings rolling buffer + drawer, latest-finding line per row,
  agent chips with role markers and human-agent distinction, task side panel
  (`?selected=`), title-badge notifications, debounced server-side metric
  recompute.

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
Lands after the surfaces it augments (T2's recompute machinery, T1's
attention model).

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
| 3 | No bulk graph fetch | `lithos_task_graph(project \| task_ids)` → `{tasks, edges}` | T2 assembles graphs via N per-task `edge_list` calls (semaphored, cached, ~100 calls/project). One indexed SQL join upstream collapses this to one call. |
| 4 | Expired claims unobservable (lazy query-time filtering) | Expose recently-expired claims, or a `claim.expired` event | The old "expired claim" attention rule is impossible; T1 substitutes a pre-expiry warning. True abandoned-work detection stays blocked. |
| 5 | Timer-gate resolution emits no event (query-time evaluation) | `gate.resolved` event | T1 self-schedules a dashboard refresh at `min(ready_at)` of visible timer gates. |
| 6 | `task_cancel.reason` not persisted (event payload only) | Persist cancel reason | Lens shows it live but loses it on reload. |
| 7 | Project convention split: `metadata.project` (filters, spawn inheritance) vs `project:` tags (existing corpus) — both in active use with disagreeing counts | Pick one canonical convention | Lens honors both (`project_convention = "both"`), warns on disagreement. |
| 8 | No MCP response resolves inline `[[target]]` wiki-links to note ids (`lithos_read.links[]` is `{target, display}`) | Add `id \| null` per link entry | K1 ships a Lens-side resolver route (UUID / path probe / title disambiguation); inline links resolve per-click rather than per-render. |
| 9 | `lithos_finding_list` requires `task_id`; `finding.posted` lacks `knowledge_id` | Optional `knowledge_id` filter; add `knowledge_id` to the event payload | Gates K4's cited-by panel entirely. |
| 10 | Retrieval receipts have no MCP read surface | Receipt read tool (optional) | K3 shows `receipt_id` as text without click-through. |

Minor, noted: `task.updated` carries only `task_id` (forces refetch — fine at
current scale); task events carry empty `tags`, so `/events?tags=` cannot
scope task streams by project.

## 5. Legacy milestone mapping

Two historical numbering schemes existed; both are retired.

| Old reference | Where it went |
|---|---|
| Legacy checklist M0 (Common Core), M1 (Tasks MVP), M2 (Tasks SSE) | Shipped in 0.1.0 — §2 above |
| Legacy checklist M3 (Optional LLM) | **X1** |
| PRD `milestone-1-operator-view.md` (section-structured operator view) | Rewritten graph-native as **T1**; ergonomics strand (drawer, agent chips, side panel, title badge, recompute) moved to **T2** |
| PRD `milestone-1-5-planning-view.md` (planning view) | Rebased on the task graph into **T2** |
| PRD `milestone-3-llm-curation-and-desktop-notifications.md` | **X1** |
| REQUIREMENTS §17 implementation plan (old M0–M11) | Deleted; tasks milestones → T1–T3, knowledge milestones → K1–K4 + pool, LLM milestones → X1 |
