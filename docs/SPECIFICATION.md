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
- `GET /tasks/{task_id}`
  Renders a task detail page.
- `GET /tasks/{task_id}/findings`
  Renders the findings fragment used by the task detail page.
- `GET /tasks/{task_id}/blockers`
  Renders one expanded level of a task's blocker chain (HTMX fragment).
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

- structured JSON logging at a configurable level
- task filter/debug logging around dashboard requests
- an optional OpenTelemetry request middleware

Logging remains the main implemented observability path. The OTel middleware
covers requests only; the feature-level `lens.knowledge.*` instrumentation the
K1 PRD specifies is **not** wired (see §10).

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

Two gaps are narrower than a milestone and tracked as tasks:

- the `lens.knowledge.*` telemetry the K1 PRD specifies is not wired (§8)
- the fake↔real contract matrix runs manually against a live server rather
  than on a schedule against a seeded one

These belong to future milestones and should not be assumed to exist merely
because they are described in `docs/REQUIREMENTS.md`.

## 11. Compatibility Statement

This specification describes the behavior of Lithos Lens `0.3.0` as currently
implemented in this repository — the 0.1.0 foundation plus the **T1**
graph-native operator view and the **K1** knowledge note view and search.

If the implementation and this document diverge, the implementation should be
treated as authoritative in the short term and this specification should be
updated to realign with shipped behavior.
