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
  Renders the task dashboard and accepts filter query parameters. With
  `?selected=<task_id>` the side panel is rendered open beside the board
  (§5.6.1).
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
- `GET /tasks/{task_id}?fragment=panel[&scope=project:<slug>|epic:<id>][&include_resolved=1|0][&snapshot=<fingerprint>]`
  Renders the task's side-panel partial (§5.6.1) — the same reads as the detail
  page through a template that extends no layout. This is what a dashboard row
  click fetches. `scope=` names the graph the **downstream impact** line is
  counted over (§5.6.1); a panel fetched without it states no impact, because N
  is a count within one fetched graph. `include_resolved` travels with the
  scope so the panel assembles the same graph the page it was opened from did,
  and `snapshot=` is the fingerprint of the ANSWER that page is showing — its
  nodes and edges AND the blocked rows M is read from. The scope name fixes
  which tasks are asked for, not which ones come back, so reads that no longer
  reproduce that fingerprint state "this graph has changed" instead of a count
  (§5.6.1).
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

**Needs attention** applies seven ordered rules, most severe first. Two are
intrinsic — `unsatisfiable` (a predecessor or gate was cancelled, so the task
can never become ready) and `cycle` (the blocking chain closes on itself) — and
the rest are threshold-driven from config: `gate-waiting`, `pr-needs-decision`,
`claim-expiring`, `stale-open`, and `ready-unclaimed`. A promoted row carries one
chip per rule that fired, with a one-line supporting fact, and the list sorts by
severity then oldest-first within a tier.

`pr-needs-decision` is the gate-waiting escalation seen through a PR gate: it
fires immediately when loom's `reconciliation_state` is `needs_human` (loom has
already concluded it cannot proceed alone — there is no wait to serve), and after
`gate_waiting_attention_hours` of `gate_failed`, dated from the state's own
`reconciliation_since`. The supporting fact is loom's `reconciliation_detail`
verbatim, and the promoted row keeps its state badge. Its chip reads **`PR needs
a decision`** — the slug is the markup hook, not the wording.

The two **gate** rules (`gate-waiting`, `pr-needs-decision`) are evaluated on the
flat fallback board too. They read the master open list's metadata and the clock,
which a failed frontier read does not touch, so an outage must not silently
suppress the board's most urgent line; the rules whose evidence the outage *did*
destroy stay silent there, because their source sections and blocker records are
empty.

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
  earliest still-future timer deadline. A `pr` gate also carries loom's
  **reconciliation state** — `needs_human` / `gate_failed` / `behind` /
  `reconciling` / `resolving_conflict` / `awaiting_review` / `ready_to_merge`,
  written by loom as flat gate metadata (PRD S7) — as a coloured badge with the
  age of the state and loom's one-line reason, and PR gates order by that state's
  severity before age (the two in-flight states are one tier, ordered against
  each other by age). The badge renders only when `reconciliation_pr_url` and
  the gate's `pr_url` are both present and equal, so a state about a replaced
  PR — or one Lens cannot tie to a PR at all — is withheld (its raw keys stay
  visible as advisory metadata), and only while the gate is **open**, since
  loom stops sweeping a resolved gate and its keys freeze into history; a state
  Lens does not recognise renders as its own text, verbatim, in grey rather
  than failing. The vocabulary and its colours are
  one mapping in `pr_reconciliation.py`, which the templates read — the same
  badge appears in the side panel's gate context, on the detail page, and on a
  gate the severity model promoted
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
- **Blocks:** — the level-1 dependents (outgoing `blocks` / `waits_on_gate`)
  with the live status each was read with, beneath the chain, so the text
  baseline covers both directions of the same relationship. The two blocker
  edge types read opposite ways round here: a *dependent* is never a reason
  this task cannot run, so the "satisfied" and "unsatisfiable" verdicts — which
  are claims about this task's own predecessors — are not applied to it
- **provenance** in both directions (`discovered_from`)
- **gate context**, for a gate: its `gate_type`, and for a `pr` gate loom's
  reconciliation badge with its detail line — the same badge the board renders,
  from the same template and under the same "is this state about this PR?" rule
- **children**, for an epic
- **findings**, with links to any note a finding produced
- related note links where available

Every bounded list on the page states its own remainder through one shared
tail, so the page-size claim has a single definition. The dependents list is
one of them: the outgoing edge count is agent-written like the incoming one.

Detail rendering is read-only in the current implementation.

#### 5.6.1 Side panel

The same reads back a **side panel**, so a relationship can be read without
leaving the board. One implementation for two host pages (§5.5 of
REQUIREMENTS), rendered from one template that extends no layout:

- `GET /tasks?selected=<task_id>` renders the board with the panel already
  open, and `GET /tasks/graph?…&focus=<task_id>` does the same beside the
  canvas. That is the no-JS baseline — a shared link, a screen reader and a
  browser with scripting off all land on the same thing — and each host page
  has exactly ONE selection parameter: `selected` on the dashboard, `focus` on
  the graph page (§5.12); neither carries the other's.
- `GET /tasks/{task_id}?fragment=panel` answers with the partial and nothing
  else. A row click fetches it — from the URL the SERVER wrote onto the row, so
  the id encoding and the board's preserved filters have one definition — swaps
  it into the board and pushes `selected` onto the URL with `pushState`. The
  push happens after the swap, so a failed fetch never leaves the address bar
  claiming an open panel.
- Closing (the Close link, or Escape) clears `selected` and nothing else: the
  filters, the epic scope, the resolved-since window and the **fragment** are
  rebuilt from the live URL. The fragment counts because the summary cards link
  to a section of the board (`#task-group-blocked`), so it is generated state
  saying where the operator is. `selected` is deliberately not a preserved
  filter, so no generated link carries one selection into the next page. Back
  and forward re-apply the URL's selection without a reload.
- **An open that does not move the URL writes no history entry.** Reopening the
  task the address bar already names — clicking the selected row again, or
  retrying one whose Back-navigation fetch failed under its own URL — still
  fetches, because the panel may be absent or stale. It does not push: a second
  identical entry is invisible until the operator leaves, and then the Back that
  should clear the selection lands on the twin, matches the intent, and appears
  to do nothing until pressed again.
- **The latest intent owns the panel.** Panels are fetched, so two can be in
  flight at once and answer in either order. Every open and every close takes a
  generation, and no response writes the panel or the URL unless its generation
  is still current — checked at *both* suspension points, since the response
  arriving and its body being read are separate moments. What the panel is MEANT
  to show is tracked separately from what it is showing, and `popstate` compares
  against the intent: a Forward back onto the selection already on screen still
  supersedes an open running under it. A reconcile is validated against the URL
  it actually fetched as well as its generation — a click in flight has not
  pushed its URL yet, so a reconcile started in that window carries the previous
  selection's panel under the next one's generation. The board fragment applies
  either way: it does not depend on the selection.
- **A panel that never arrives.** A rejected fetch, a non-OK response and a body
  that fails to read are one outcome, and what it costs depends on who asked. A
  click has pushed nothing yet, so the panel and the URL still agree and both
  stay — only the intent is walked back to what is on screen, or the next
  Forward onto the task that failed would match it and leave the previous task's
  panel under its URL. Back and forward move the URL *before* the panel code
  runs, so there the panel on screen already names a different task than the
  address bar: it is cleared, along with the selection, because an empty panel
  under a URL the operator can retry is a missing answer and the previous task's
  panel is a wrong one.
- **The browser never assembles a panel URL.** Rows carry one built by the
  server, and the host carries the one built for the request's own selection —
  which is what reopens a deep-linked task that has no row on this board. The
  residual fallback uses the query-alias route, whose path and key the server
  hands down, because that is the one form that addresses every id: a task
  called `graph` fetched as `/tasks/graph` would return the graph PAGE.
- The header carries the identity §5.5.1 asks for, and says so even when the
  answer is empty: a task belonging to no project under the configured
  convention renders an explicit `(no project)` chip rather than nothing, so
  projectless work is distinguishable from a field that failed to render.
- **Every row on the board opens one**, the Gates section included: a gate is a
  task, and "what is this gate holding up?" is the Blocks list the panel
  already answers. The contract a row opts into is `data-task-id` plus the
  server-built `data-panel-url` — not the `data-task-row` hook the live-event
  handlers use to rewrite claim and status chrome in place, which a gate row
  does not carry.
- **Expand** leaves for the full page. Every other link inside the panel is an
  ordinary link too; the click handler intercepts the row title and the row
  itself, never a tag chip or a link within the panel. Nor a `<summary>`: the
  gate row's waiter list is a `<details>` that expands with no JavaScript, and
  its disclosure control keeps that behaviour.

The panel states: the header (title, status, type badge with `gate_type`,
project chip under the configured convention, creating agent), the parent
breadcrumb, the blockers with live status (level 1, no per-level expander —
the walk lives on the full page), **Blocks** (the level-1 dependents), the
**downstream impact** when it was given a scope (below), the
active claims, and the finding COUNT linked to the full timeline. The
**downstream impact** is stated only when the request named a `scope=`, and it
is two figures from two authorities (§5.7 of REQUIREMENTS):

- **N** is Lens's own walk — the open transitive dependents of this task over
  the scope's **active projection** of `blocks` + `waits_on_gate`, within the
  graph that scope fetched, downstream ghosts counted as the leaves they are.
  It reads `≥ N` in two states, and both are the scope's rather than the walk's
  to know: the **scope is incomplete** — any task's `edge_list` read failed,
  wherever it sits, because an unread edge list is precisely the evidence that
  the projection Lens can see may not be all of it — or a node reached only
  over an `unknown` edge is downstream, which is **named, never counted** ("not
  counted, relation unreadable: …") because Lens cannot classify the relation
  in either direction.
- **M** is Lithos's — the dependents whose scoped `task_blocked` row names this
  task as their SOLE unsatisfied blocker, read from the same coverage set
  §5.12 assembles (which is why the downstream ghosts' projects are in it: a
  cross-project dependent this task solely blocks is counted like any other).
  Where a dependent's project read truncated, failed, was never made, or could
  not be issued at all (a projectless task), M is **withheld** with the covered
  count stated — not reported low, because a partial M reads exactly like a
  whole one and the operator is choosing what to work on next from it.

Rendered "frees N in this graph, M immediately". A **completed** focal task
states "completed; no pending impact" and a **cancelled** one "its dependents
are unsatisfiable" — neither has pending edges, so neither carries a
future-tense number — and an **epic** carries no impact line at all, since a
zero there would read as "finishing this frees nobody" rather than "this is not
that kind of task". Beside it, "on the longest chain (k of n)" gives the task's
position on the SCOPE's chain (§5.12) when it is on it; a task is trivially on
the chain through itself, so stating that would state nothing. And when the
scope is incomplete, or an `unknown` edge touches the focused task's own
neighbourhood, the panel says that what the canvas lights is a **lower bound**
of what surrounds it — a dimmed node must not read as "unrelated" when Lens
only failed to look.

The graph page's own render computes this from the scope and cycle signal it
already holds; a panel fetched on its own rebuilds that scope, which the
per-task edge cache (§5.10) makes affordable because the graph the operator is
looking at is warm. A rebuild is not the same answer by default, though: the
scope NAME only fixes which tasks are asked for, and the canvas deliberately
does not re-lay-out under the operator (§5.12.1) — so every panel URL the graph
page emits carries `snapshot=`, a fingerprint of what that render answered.
It covers **both** authorities, because they move independently and only one of
them is cached: the node set (id, status, completeness, ghost kind, project
slugs) and the edge set (endpoints, type, state) that N is walked over, and the
blocked rows for those nodes — each row's blockers by kind, predecessor, type
and status — together with the coverage set and each read's outcome, which is
what decides whether M is stated at all. The blocked half is load-bearing: an
edge upsert emits no event (§5.10) and a warm edge cache can reproduce a
byte-identical graph while Lithos's sole-blocker row has gained a second
blocker, moving M with nothing on the canvas to show for it. So the comparison
is made **after** the blocked read, not before it, and when it fails both
figures are withheld and the panel states "this graph has changed — refresh",
because "frees N **in this graph**, M immediately" would otherwise name one
graph while the picture beside it shows another. Titles, claims, blocker
messages and error reasons are excluded — they move no figure, and a
fingerprint that changed on every heartbeat would withhold the line permanently
rather than when it is wrong. The material is serialised as **canonical JSON**
before hashing: a task id is an arbitrary non-empty string (§5.1) and nothing
normalises control characters out of one, so a digest that joined its fields on
a separator would read `a -> b<sep>c` and `a<sep>b -> c` as the same edge and
let a moved graph pass the check.

The same rule binds the panel's two HALVES, which are two reads whichever route
serves them: the figures come from the graph assembly's view of the focal task,
and the header beside them — title, type, status badge — from a later, separate
`task_get`. A task that resolves between the two would put "Completing this
frees N …" under a `completed` badge, or the resolved wording under an `open`
one. So the impact is kept only while the badge's own status is the state it
was counted for, and a disagreement is answered by which way it runs. A badge
that has **resolved** states D10's own wording for that state — "completed; no
pending impact", or the cancelled wording — because that answer needs no
arithmetic, and a refresh notice there would be the forbidden future tense in
another sentence; the parts of the line that belong to the graph rather than to
the focal status (the chain position, the lower-bound note) carry over
unchanged. Every other disagreement — figures counted for a resolved task under
an **open** badge, a status Lens cannot name, a task the panel could not read
at all — has no fact to state and degrades to the withheld line above. Either
way the impact costs the line and nothing else: a scope that fails, is refused,
or does not hold the task renders the rest of the panel unchanged.

An unknown id renders the **not-found panel** at HTTP 200 on both routes —
never a 500, and never at the cost of the board beside it — and a read that
merely failed says so instead, because "this task does not exist" is Lithos's
answer rather than a transport outcome. The open panel carries its own refresh
fragment, so the reconcile that keeps the board live keeps the panel's blocker
and dependent statuses live too without rebuilding it under the cursor.

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
and reviewable with no JavaScript, and the Cytoscape rendering (§5.12.1) is
enhancement drawn from the same embedded payload, so the picture and the text
cannot disagree.

**Scope and URL state.** `?project=<slug>` or `?epic=<id>`; with neither, the
page renders a picker listing every project the snapshot observes under both
§5B.1 conventions plus its open epics. `include_resolved` and `isolated`
default by scope KIND, opposite ways round — a project graph is about what can
still run (resolved hidden, isolates folded), an epic graph about an
initiative's progress (closed children shown, isolates open). `focus=` is the
page's single selection parameter and `selected=` is accepted as a
compatibility alias that the route **redirects away** (307 to the same URL with
`focus=` and no `selected=`) before it reads anything: both clients read
`focus`, so a page served under the alias would render a panel with no node lit
and a Close that pushed a URL still carrying the alias. A request carrying a
selection **server-renders that task's side panel** beside the canvas (§5.6.1's panel, this page's no-JS baseline, counted as a
`url` open), with that panel's downstream impact computed from this render's
own scope, and a read that fails there costs the panel rather than the graph; `overlays=hierarchy,provenance` is carried for the client layer.
A `focus=` the scope actually holds also **replaces the chain line with the
longest chain THROUGH that task** (§5.11), named as such: a chain through a
mid-graph task is routinely shorter than the graph's longest, and an
unlabelled number there would understate it. A `focus=` naming a task this
graph does not hold leaves the scope's own chain standing.
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
chain with its `exact | lower_bound` flag and its condensations' members, the
**active projection's own longest-path DP** (`active_chain`: every task's
condensation, the next step of the longest walk into and out of each, and the
scope's own chain — what lets a client-side focus transition trace the chain
through the newly focused node without a reload and without re-deriving D7),
roots, isolated, incomplete and `as_of`. Each node additionally carries its claims and the detail URL
`tasks.task_detail_path` built for it — the two things the canvas needs and
the topology does not imply. The toolbar states `as_of` — the OLDEST
contributing fetch — because edge upserts emit no upstream event and the TTL
is the staleness bound.

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

#### 5.12.1 Cytoscape rendering

With JavaScript, `static/graph.js` draws the embedded payload with the vendored
Cytoscape 3.30.3 — loaded on this page, and only when there is a graph to draw
(not the picker, not a refusal, not an empty scope). It adds nothing the text
does not already state: status, type, ghost-ness, cycle membership, the longest
chain and the isolated set are all payload fields, and the only thing the
client derives is which nodes an `active` dependency edge in this graph points
at. Lens still never re-implements the readiness predicate.

- **Layout** is `breadthfirst`, directed, from the server's own `roots`, run
  **once** and never again — no physics, and no re-layout for any later event.
  The layout sees the dependency edges only: an epic's `parent_child` edges are
  added afterwards, or hierarchy would decide the shape of a picture that is
  about dependency flow. What it decides is the ORDER of the nodes across a
  rank; the **rank itself is the payload's `layer`**, because the server layers
  by longest path and `breadthfirst` ranks by shortest — on `A → B → C → D`
  plus `A → D` the library draws D level with B while the text underneath says
  layer 3, and a picture contradicting the layers it is printed above is what
  §5.12's "the picture and the text cannot disagree" rules out. (The library's
  own `maximal` option is the server's rule, but it is abandoned as soon as the
  graph has a cycle, which a dependency graph routinely does.) A slot in a rank
  is a **condensation**, not a node: a cycle takes one place in its layer and
  its members stack inside a **compound parent** there, exactly as the server
  layers it. Ranks are stacked **cumulatively** — each is as tall as its
  tallest condensation — because nothing bounds an SCC below the node guard and
  a fixed pitch lets a large cycle's stack spill into the rank above and the
  rank below, which is the contradiction this placement exists to prevent.
- **Colour is status** (open / completed / cancelled, plus the dashed `unknown`
  style for a ghost whose status could not be read), **shape is type** (ellipse
  task, round-rectangle epic, diamond gate); a node something in this graph
  blocks is tinted, and a claimed one pulses (suppressed under
  `prefers-reduced-motion`). Ghosts are dimmed and carry their project on the
  label. `unknown` edges take the `unknown` style, inactive ones are faded, and
  the longest blocking chain is traced — over **active dependency edges**
  only (it is the longest *blocking* chain, so a `parent_child` or
  `discovered_from` edge running between the same two tasks is not a step of
  it), matched by **condensation**, since the chain is a walk over the
  condensed graph and names a cycle by its representative while the edge that
  enters it may land on any member. The condensation it matches on is the
  chain's OWN — the payload's `longest_chain.members`, parallel to its
  `nodes` — and not a node's `cycle`, which is the all-edge SCC the picture is
  drawn from: an open `A → B` inside a loop closed by an inactive `B → C` and
  `C → A` is one drawn cycle and a live two-chain at once, and reading the
  chain through the drawn cycle there would trace an answer the text does not
  state.
- **Every edge carries an arrowhead**; `blocks` is solid and `waits_on_gate`
  dashed. `parent_child` (thin, light) and `discovered_from` (dotted) are
  **overlays, off by default**, toggled in the toolbar and remembered in the
  URL as `overlays=hierarchy,provenance`. Both overlays' edges and their
  context ghosts are already in the payload, so a toggle is a client-side
  show/hide with **no fetch**, and `popstate` re-applies whatever the URL
  says. An overlay draws in the endpoints it needs — an isolated node folded
  away by default is revealed when a switched-on overlay connects it, since
  hiding it would hide the edges just asked for. A context ghost appears only
  with its overlay.
- The **isolated toggle** (`isolated=1|0`) moves the canvas and the text
  disclosure together; the plain-language **legend** is persistent; and **show
  as text** collapses the text layers behind the canvas without removing them
  — the text stays in the DOM.
- **Focus mode** (`focus=<id>`) is the page's exploration state, and every
  transition is one `pushState`: a node click, a search hit, `focus=` in the
  URL at load. The focused node is **centred** (a pan, never a re-layout), its
  ancestors and descendants over the **active projection** are classed
  `focus-lit`, everything else `focus-dimmed`, and anything Lens cannot place
  relative to it `focus-unknown` — a node whose own edge list failed, or one
  reached only over an `unknown` edge, is neither lit nor dimmed, because its
  relation is not known in either direction. The walk crosses `active` edges
  only, so a completed predecessor is not an ancestor however many hops the
  drawn graph offers. Closing the panel, or Escape, removes `focus` and every
  class with it; `popstate` re-applies `focus`, `overlays` and `isolated` from
  the static payload with **no reload**. Focusing a node the page is not
  DRAWING reveals it in the same transition, by whichever parameter hides it: a
  folded isolate takes `isolated=1`, and a context ghost takes the overlay
  whose edge anchors it — one history entry either way, so Back cannot undo
  half the move, and a deep link onto one replaces the entry it arrived on
  rather than adding a twin.
- **The chain follows the focus.** The line above the layers and the trace on
  the canvas both state the longest chain THROUGH the focused node (§5.11), and
  a focus transition never reloads the page — so the client recomputes both
  from the payload's `active_chain` on every transition, and clearing the focus
  restores the scope's own chain. The answer is still the server's: walking
  those pointers reproduces `longest_blocking_chain(through=…)`, tie-breaks
  included. Only the focus-dependent parts of the sentence are rewritten; the
  lower-bound wording beside them describes the scope (an unreadable edge
  anywhere) and does not move with the focus. The sentence names the **focused
  task**, while the chain it lists names each step by its **condensation's
  representative** — the two differ exactly when the focus is a
  non-representative member of a live cycle, and a line naming the
  representative there would disagree with a reload of the URL it was just
  pushed under.
- **Search** is a toolbar input matching a title SUBSTRING or an id PREFIX over
  the payload's own nodes — a title is remembered in fragments, an id is pasted
  from its start — and selecting a match (click, or Enter for the first one) is
  an ordinary focus transition. No fetch and no server round trip; it is
  revealed by the client, because with no scripting there is nothing to jump to
  and the browser's own find searches the text baseline.
- **Click** a node and its panel opens beside the canvas through the same
  implementation the dashboard's rows use (§5.6.1), pushing `focus=` after the
  swap; **double-click** navigates to the task's page via the URL the server
  built. The node lights on the first tap, but the panel waits for the click to
  settle as a single one (Cytoscape's 250ms multi-click window): a panel opened
  mid-gesture narrows the canvas, and the refit that follows would move the
  node out from under the second click. That window is measured by TIME alone,
  so a pair inside it counts as a double-click only when both clicks were on
  the SAME node; a pair spanning two nodes (or the background and a node) is
  the second one's single click, and opens its panel. A tap on a node also
  **supersedes any panel open still in flight** — an earlier click's, or the
  client's own fallback for a `focus=` the server could not render: an answer
  landing between the two halves of a double-click would narrow the canvas at
  exactly the moment the debounce exists to protect. A panel that has already
  arrived is left alone — only the unpainted request is dropped, and dropping
  it moves nothing on screen.
  Every panel transition is announced back to the canvas, because it
  moves the URL by `pushState` and `pushState` fires no `popstate`: without it,
  closing the panel would clear `focus` and leave the node still lit. A
  `focus=` already in the URL is answered by the SERVER (§5.12), and the client
  fetches that panel only when the server did not.
- **Every toolbar link follows the live URL.** The overlay and isolated links
  are rebuilt on each render, and so are the two the server wrote and the
  client never re-applies — the resolved toggle and the refresh pill — because
  every other control here moves the URL without a reload, and a pill still
  pointing at the address the page loaded on would silently drop the overlays
  and the focus set on the way to needing it.
- **The automatic fit never scales below legibility.** Cytoscape scales text
  with the viewport, so fitting a graph into a narrow box shrinks its labels
  with it — at 320px the demo graph fitted to zoom 0.26 and drew a 10-unit font
  at under three pixels, which communicates none of what the canvas is for. The
  fit therefore stops at the zoom that still renders a label at 10px; past that
  the graph overflows its box and is panned, and the page says so ("showing part
  of the graph") whenever anything is measurably out of view — recomputed on
  every Cytoscape `viewport` change, not only on a fit, because panning a
  clipped graph back into view makes it whole and zooming in on a fitted one
  takes it out again. "Out of view" is a test of POSITION against the canvas
  rect, not of size: a graph smaller than its box is off screen all the same
  once it has been panned past the edge. A drag is always a PAN — nodes are
  ungrabbable and box selection is off — because "once placed, nothing moves"
  is what lets the ranks be trusted against the text layers. Every label the
  canvas draws — a cycle box's caption included — uses the one font size the
  floor is derived from, or it would be sub-legible exactly when the floor
  binds. Zooming out further is the operator's to do; only the automatic
  scaling is bounded. A re-fit — which is what opening the panel beside the
  canvas causes, since it narrows the box — is followed by re-centring the
  focused node, or the fit would quietly undo the centring the click that
  opened the panel just applied.
- **The panel a node click fetches carries this page's scope and snapshot**, so
  its downstream impact (§5.6.1) counts over the answer on screen — or, when
  its own reads no longer reproduce that fingerprint, says so rather than
  stating figures beside a picture they do not belong to. A node has no DOM row to read a
  server-built URL off, so the page hands `tasks.js` the scope, its
  `include_resolved` and the fingerprint directly; the URL is otherwise the
  query alias, which is the one form that addresses every id.
- **Cytoscape is handed opaque element ids**, never a task's own. A task id is
  an arbitrary non-empty string (§5.1), and the shipped 3.30.3 throws inside
  `breadthfirst` on an element called `__proto__`, `constructor` or `toString`
  — its internal maps are prototype-bearing. Synthesising ids also makes the
  compound parents and the edges collision-free by construction, where a key
  built from `from::to` would merge the payload edges `a::b → c` and
  `a → b::c` and silently drop one of them. Every client-side lookup keyed by
  an id uses a null-prototype map for the same reason, and an ordered PAIR of
  ids — the chain's steps — is a nested map rather than a joined string, which
  could not tell the step `a>b → c` from the step `a → b>c`.
- **Events** raise a "graph changed — refresh" pill when a consumed task
  event's `task_id` is a node on the page, and do nothing else: this page tells
  `tasks.js` not to reconcile, because re-rendering the board's way would
  re-fetch a whole graph assembly per event and move the canvas under the
  operator's cursor. Edge upserts emit no event at all, so the pill is a hint
  and `as_of` remains the page's real staleness bound.

  The stream itself opens on **`DOMContentLoaded`**, not at the end of
  `tasks.js`: deferred scripts all run before that event, and on this page the
  subscriber is in the last of them, behind a ~400KB library. A stream opened
  earlier would consume a matching event — and record its id in the dedup set —
  while the library was still in flight, with nothing to replay it to and no
  reconcile to cover for it. (`"interactive"`, not `"loading"`, is the state a
  deferred script runs in; a file injected between `DOMContentLoaded` and `load`
  is caught by a `load` backstop.)

The side panel (§5.6.1) is counted by **`lens_tasks_panel_opens_total`**
(`source` in `url` | `fragment`) — the SSR baseline and the click-fetched
partial, which are the two things the request can actually distinguish. The
PRD's finer row-vs-node split is the client's knowledge, not a fact on the
request, and the task id is absent for the same cardinality reason as the
graph's scope key.

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
- lightweight browser JavaScript for SSE, fragment refresh, the side panel, and
  date-picker synchronization
- one vendored library, Cytoscape, loaded by the task graph page alone (§5.12.1)
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
