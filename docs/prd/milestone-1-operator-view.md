---
title: Milestone 1 — Operator View MVP
milestone: M1
status: draft
target_version: 0.2.0
references:
  - docs/REQUIREMENTS.md §5 (Tasks View — Operator View)
  - docs/REQUIREMENTS.md §5B (Project Tracking Conventions)
  - docs/REQUIREMENTS.md §17 (Implementation Plan)
labels: [needs-triage, milestone-1, tasks-view]
---

# Milestone 1 — Operator View MVP

## Problem Statement

The current Tasks dashboard is a flat three-group list (`open` / `completed` / `cancelled`) with a "Current Situation" tile panel and a `claimed_state` filter. It tells me roughly *how many* tasks exist in each bucket, but it does not answer the operator's actual question: **"are my agents alive and making progress, and is there anything that needs my attention right now?"**

Concretely:

- Expired claims, stale open tasks, and unclaimed-old tasks are invisible. I have to eyeball every row to spot them.
- I run multiple projects in parallel (Ganglion, Influx, Kindred Code, Ralph++, NAO bridge, …) and the dashboard does not surface which project a task belongs to without reading the tag strip.
- Findings only appear in the detail panel — I cannot see "what just happened across all my agents" without clicking into each task.
- Agents appear only as a single "Creating agent" column. I cannot see at a glance who claimed the task and who posted the most recent finding, and I cannot tell my own manual claims (as a human) apart from agent-zero's automated claims.
- The `claimed_state` filter is a workaround for the missing structural distinction between "in progress" and "queued" work, and it silently classifies rows past the visible cap as unknown.
- When SSE drops, the dashboard freezes silently — there is no indication that the data is stale apart from a small badge.

The dashboard needs to be reshaped around the operator's actual job: **glance, spot trouble, drill in only when needed.**

## Solution

Replace the flat three-group list with a section-structured Operator View at `/tasks` that puts the most important rows first:

```
⚠ Needs attention   (severity-ordered: expired-claim → stale-open → unclaimed-old)
▶ In progress       (open, has active claims)
▶ Queued            (open, no active claims)
▶ Unknown claim state (rows past visible_cap)
▶ Completed (12 in last 30 days)        [collapsed]
▶ Cancelled (3 in last 30 days)         [collapsed]
```

Every row carries a project chip (`project:<slug>`), a one-line latest-finding summary (`<agent> — <summary>`), and a single agent chip per agent with role markers (`created` / `claimed` / `latest`). Human-agents render with a person-icon prefix and a distinct background.

A collapsible "Recent findings" drawer shows the last N `finding.posted` events across all tasks, fed by a server-side rolling buffer so it survives tab refresh and stays consistent across multiple open tabs.

A title-badge notification (`(N) Lithos Lens`) appears in the browser tab whenever there are unseen Needs-attention items, so the dashboard talks back even when buried under other tabs.

Filters reflect the new structure: project becomes a first-class top-level filter; the legacy `claimed_state` filter is dropped (the In progress / Queued / Unknown sections express the same intent structurally and refuse to silently classify unknown rows). Filter state and section-collapse state both reflect in the URL for shareability.

## User Stories

1. As an operator, I want a "Needs attention" section at the top of the dashboard, so that I can spot expired claims, stale open tasks, and unclaimed-old tasks without scanning the entire list.
2. As an operator, I want each Needs-attention row to carry reason chips (`expired-claim`, `stale-open`, `unclaimed-old`), so that I can immediately understand why the row was flagged.
3. As an operator, I want the Needs-attention section ordered by severity (expired-claim → stale-open → unclaimed-old), then oldest first within each tier, so that the most active failures surface first.
4. As an operator, I want flagged rows to appear *only* in Needs attention and not also in In progress / Queued, so that I do not double-count or visually scan the same row twice.
5. As an operator, I want a thin "All systems healthy — 0 issues" stripe to remain visible when Needs attention is empty, so that I know the section is active and absence of flags is a real signal.
6. As an operator, I want to toggle the Needs-attention section on or off entirely, so that I can do routine review without it taking attention.
7. As an operator, I want open tasks split into "In progress" (has claims) and "Queued" (no claims) sections, so that I can see what's being worked on versus what's pending without resorting to a hidden filter.
8. As an operator, I want rows past the visible cap to render in their own "Unknown claim state" tail with an accuracy banner, so that I never silently classify rows whose claim state I haven't fetched.
9. As an operator running multiple projects in parallel, I want a project chip on every row in a dedicated leftmost slot, so that I can see at a glance which project each task belongs to.
10. As an operator, I want the project chip background coloured by stable hash of the project slug, so that I can recognise projects visually without reading the chip text.
11. As an operator, I want a top-level project filter dropdown sourced from `lithos_tags(prefix="project:")`, so that I can scope every section of the dashboard to one or more projects.
12. As an operator, I want each open row to show a one-line latest-finding summary (`<agent> — <summary>` with relative timestamp), so that I can see what each agent last said without opening the detail panel.
13. As an operator, I want the latest-finding line to update in place when a `finding.posted` SSE event arrives, so that the dashboard reflects new agent activity in real time.
14. As an operator, I want a collapsible "Recent findings" drawer showing the last N findings across all tasks, so that I can scan recent agent activity globally without clicking into each task.
15. As an operator, I want to click a row in the drawer to open the parent task's detail panel, so that I can drill in from a finding to its task with one click.
16. As an operator, I want the recent-findings drawer to be fed by a server-side rolling buffer, so that it survives tab refresh and stays consistent across multiple open browser tabs.
17. As an operator, I want each row to show a single chip per agent that appears on the row, with role markers (`created`, `claimed`, `latest`) collapsed into one chip, so that the row stays compact even when one agent fills multiple roles.
18. As an operator, I want agents listed in `[tasks].human_agents` to render with a person-icon prefix and a distinct chip background, so that I can immediately tell my own manual claims apart from automated agent claims.
19. As an operator, I want clicking any agent chip on a row to filter the list to that agent across all roles (creator OR claimer OR latest-finding-poster), so that I can see everything that agent is involved in regardless of role.
20. As an operator, I want a role-narrow toggle in the filter bar (`creator` / `claimer` / `poster` / `any`), so that I can constrain the agent filter to a single role when I need to.
21. As an operator, I want to click on a task row to open a right-side panel with full detail, so that I can drill in without losing the list scroll position or filter state.
22. As an operator, I want an "Expand" button on the side panel that navigates to a full-page route at `/tasks/{task_id}`, so that I can deep-dive into a long findings timeline or share the URL.
23. As an operator viewing a Needs-attention task in the detail panel, I want a "Why this task is here" block at the top, so that I can immediately understand which rules fired and the supporting facts (e.g. "Stale open — 9 days since created").
24. As an operator, I want Completed and Cancelled groups rendered as collapsed section headers showing counts (e.g. "Completed (12 in last 30 days)"), so that recently-closed work is reachable but does not crowd the live dashboard.
25. As an operator, I want SSE `task.completed` and `task.cancelled` events to animate visible rows transitioning out of the open sections and update the collapsed section header counts, so that I have visual confirmation of state changes.
26. As an operator, I want the Tasks page title to update to `(N) Lithos Lens` whenever there are unseen Needs-attention items, so that I can see the count from the browser tab even when Lens is not the active tab.
27. As an operator, I want the title-badge counter to clear when I return focus to the Lens tab, so that "unseen" tracks what I have actually looked at.
28. As an operator, when SSE disconnects, I want a "Live updates paused — reconnecting" badge to remain visible, so that I know the dashboard data is potentially stale.
29. As an operator during a disconnect, I want the polling fallback to fire a transient 1-second toast on each successful refresh, so that I can confirm the dashboard is still updating even though SSE is down.
30. As an operator, I want filter state and section-collapse state both reflected in the URL, so that I can bookmark or share specific dashboard views.
31. As an operator, I want filters to compose and to leave section headers visible (with a "no rows match current filters" placeholder) when scoped to empty, so that filtering never silently hides a Needs-attention warning.
32. As an operator visiting `/tasks` with no tasks at all in Lithos, I want a "No tasks yet" panel with a single help line and a link to project-tracking conventions, so that I know the page is healthy and understand how tasks are created.
33. As an operator visiting `/tasks` when no tasks are open, I want each open section to show "All clear — no open tasks", so that the layout stays consistent and the absence of work is visible.
34. As an operator visiting `/tasks` when Lithos is unreachable, I want the existing degraded banner with a clear empty state below, so that I know the dashboard is not broken — Lithos is offline.
35. As an operator with rows past `tasks.visible_cap`, I want the Unknown claim state section to be excluded from the Needs-attention rules entirely, so that Lens never silently classifies rows whose claim state it has not loaded.
36. As an operator, I want the `claimed_state` URL parameter dropped (or ignored when present), so that links from the legacy dashboard fall back gracefully to the new section structure.
37. As an operator, I want every row to render gracefully if a task has multiple `project:*` tags — multiple chips visible, with a soft warning emitted to telemetry — so that mis-tagged tasks are not silently hidden under one project.
38. As an operator, I want the project filter, the project chips, and any future project-aware view to share a single per-request fetch of `lithos_tags(prefix="project:")`, so that the dashboard stays cheap to load.

## Implementation Decisions

### Modules to introduce or extract

The current `tasks.py` is doing too much — list shaping, filter parsing, claim filtering, finding resolution. M1 introduces deep, testable modules around the new domain concepts:

- **Section classification module** — pure functions that take an `EnrichedTask` (current) plus a "now" timestamp and the threshold config, and return a `SectionAssignment(section, reasons)` value. Encapsulates the rule set for `expired-claim`, `stale-open`, `unclaimed-old`, plus the In-progress / Queued / Unknown classifications. Stable interface; testable in isolation; rule changes touch one place. The threshold config is a small frozen dataclass derived from `[tasks]`.
- **Recent-findings rolling buffer** — server-side ring-buffer holding the last `tasks.recent_findings_drawer_size` entries. Simple interface: `append(finding)`, `snapshot() -> tuple[FindingRecord, ...]`, `latest_for_task(task_id)`. Backed by a fixed-capacity deque protected by an asyncio lock. Survives task/finding events; warmed at boot.
- **Metric recompute / debounce module** — owns the dirty flag, the debounce window (`metrics_debounce_ms`), and the recompute task. Listens to the `EventHub` and emits recomputed dashboard fragments on its own internal channel. Manual refresh / page load / SSE reconnect can call `recompute_now()` to bypass debounce.
- **Agent-role aggregation module** — pure function `agent_chips_for(task, claims, latest_finding, human_agents) -> tuple[AgentChip, ...]`, where `AgentChip(name, roles, is_human)`. Lives next to section classification; both feed row rendering.

### Modules to modify

- `tasks.py` — drop `_apply_claim_filter` and `claimed_state` from `TaskFilters`. Keep filter parsing and dashboard loading; delegate row classification to the section module. Add OR-across-roles agent matching. Add latest-finding lookup via the rolling buffer.
- `events.py` — extend `EventHub` to also feed the rolling buffer and the recompute scheduler. Boot-time warm-up calls `lithos_finding_list(task_id, since=now - recent_findings_warmup_window_h)` for each currently-open task within `visible_cap`.
- `web.py` — add `GET /tasks/findings/recent` (drawer fragment). Update `/tasks` to render the new section structure. Update the side-panel route (`/tasks?selected=<task_id>`) and ensure the full-page route (`/tasks/{task_id}`) reuses the same `detail.html` fragment.
- `config.py` — add new `[tasks]` knobs: `project_tag_key`, `stale_open_age_days`, `unclaimed_warning_minutes`, `metrics_debounce_ms`, `recent_findings_drawer_size`, `recent_findings_warmup_window_h`, `human_actionable_tag` (used in M1.5), `human_agents`, `[tasks.notifications].title_badge`, `[tasks.notifications].desktop_optin` (only `title_badge` actively wired in M1).
- Templates — restructure `tasks/dashboard.html` (new section layout, project filter, drawer toggle), introduce `tasks/sections/needs_attention.html` and `tasks/sections/in_progress.html` etc. as fragments, restructure `tasks/row.html` (project chip, latest-finding line, agent chips with role markers), add `tasks/findings_recent.html` for the drawer, add `tasks/why_attention.html` for the detail-panel block.
- Static — small CSS additions for project chip palette (hash-based), human-agent chip variant, drawer panel.

### Filter and URL contract

- Project filter: multi-select dropdown sourced from `lithos_tags(prefix="project:")`, URL `?project=ganglion&project=influx`.
- Status filter: multi-select section-group selector — `?status=open` shows the four open-related sections; `?status=completed,cancelled` expands those flat groups inline.
- Tag filter: free-text `?tag=cli` (excludes reserved keys handled by their own affordances).
- Agent filter: `?agent=agent-zero&agent_role=any` where role is one of `any` (default), `creator`, `claimer`, `poster`.
- Side-panel state: `?selected=<task_id>`.
- Section-collapse state: `?collapsed=completed,cancelled,needs-attention` (extensible).
- `claimed_state` URL parameter is silently ignored if present (no breakage, no migration banner).

### MCP / SSE dependencies

- `lithos_task_list(status, tags, agent, since)` — already wired.
- `lithos_task_status(task_id)` — already wired; per-row fan-out up to `visible_cap`.
- `lithos_finding_list(task_id, since)` — already wired for detail panel; M1 also calls it during boot warm-up of the rolling buffer.
- `lithos_read(id)` — already wired for finding link titles.
- `lithos_agent_list()` — already wired.
- `lithos_tags(prefix="project:")` — **new dependency**. Cached per request. If it errors, project filter dropdown falls back to "no projects" but per-row chips still render from existing tag data.
- `lithos_stats()` — used only for the agent count in M1 (the rest of the situation panel goes away).
- SSE event types subscribed: `task.created`, `task.claimed`, `task.released`, `task.completed`, `task.cancelled`, `finding.posted`. Already wired in `events.py`.

### Notifications

- Title-badge: pure JavaScript on the dashboard page reads the Needs-attention count from a server-rendered data attribute and updates `document.title` accordingly. Cleared on `visibilitychange` → focused. No browser permission needed.
- Desktop notifications wiring: deferred to M3.

### Persistence policy

- URL params: filter state, side-panel selection, section-collapse state.
- Cookies: notification-toggle preference, agent-role-filter mode, hide-Needs-attention preference.
- localStorage: reserved for desktop-notification grant state in M3; not used in M1.

### Telemetry

- `lens.tasks.list` — already exists; extend to record section counts and the project filter.
- `lens.tasks.detail` — already exists.
- `lens.tasks.findings` — already exists.
- `lens.tasks.findings_recent` (new) — drawer fetch and warm-up.
- `lens.tasks.metrics_recompute` (new) — attribute `trigger=sse|manual|reconnect|warmup`.
- `lens.tasks.event` (existing) — per-event handling.
- `lens.events.connect` (existing) — connection lifecycle.

## Testing Decisions

Tests assert **external behaviour** — the data shape returned by classification rules, the HTML structure rendered by templates, the buffer state after a sequence of events, the URL parsed from filter inputs. Implementation details (private helper functions, intermediate dicts) are not tested.

### What to test

- **Section classification module (deep, pure)** — table-driven tests covering: claim with `expires_at` in the past → `expired-claim`; claim still valid → no expiry flag; open task `created_at > 7d ago` → `stale-open`; open task with no claims and `created_at > 60m ago` → `unclaimed-old`; row triggering multiple rules carries multiple reason chips; row past visible cap is never classified into Needs attention; healthy in-progress row → `in_progress`; healthy unclaimed row → `queued`; threshold config overrides change classification correctly.
- **Recent-findings rolling buffer** — append beyond capacity evicts oldest; `latest_for_task(task_id)` returns the most recent for that task; concurrent appends (asyncio) do not corrupt buffer; warm-up over a window populates buffer with sorted entries.
- **Metric recompute / debounce** — N rapid `dirty()` calls within debounce window trigger one `recompute()`; `recompute_now()` bypasses the debounce; SSE reconnect fires `recompute_now()`.
- **Agent-role aggregation** — single agent with all three roles collapses to one chip with three role markers; multiple agents each appear once with their respective roles; `is_human` flag set when agent name appears in `[tasks].human_agents`.
- **Filter parsing** — project multi-select round-trips through URL; legacy `claimed_state` URL param is ignored without error; `agent_role` defaults to `any`; section-collapse state round-trips.
- **End-to-end dashboard rendering** — `TestClient` with a `TaskFakeLithosClient` fixture (already in `tests/test_tasks_mvp.py`) extended to: one expired-claim row appears only under Needs attention; reason chips visible; project chips visible; latest-finding line rendered; collapsed Completed header shows count; empty-Lithos shows "No tasks yet" panel; Lithos-unreachable shows degraded banner.
- **SSE-driven OOB updates** — using the existing `test_tasks_sse.py` patterns: `task.claimed` event moves a row from Queued to In progress; `finding.posted` updates the row's latest-finding line; `task.completed` removes the row from open sections and increments the Completed header.
- **Notifications** — DOM assertion in dashboard page: `document.title` updates via the embedded data attribute when N > 0; clears on `visibilitychange`.
- **Empty states** — explicit tests for all four cases listed in §5.9.4a.

### Prior art

- `tests/test_tasks_mvp.py` already uses a `TaskFakeLithosClient` and `TestClient` from FastAPI to render the dashboard; extend it for the new section structure rather than starting fresh.
- `tests/test_tasks_sse.py` already exercises `parse_lithos_sse_frame` and `EventHub`; extend with rolling-buffer + recompute coverage.
- `tests/conftest.py` already provides a TOML config fixture that clears env-var leakage; reuse for any new config knobs.

### Coverage target

≥ 80% line coverage on the new modules (section classification, rolling buffer, metric recompute, agent-role aggregation). End-to-end tests cover at least the eight acceptance bullets in §17 M1.

## Tracer-bullet vertical slices

These slices are sized for individual tickets. Each delivers user-visible value when merged.

1. **Section classification module + new section data model.** Introduce the deep classifier, plumb it through `load_dashboard`, replace the `claimed_state` filter with the structural sections. Acceptance: a row with an expired claim renders only in Needs attention with a reason chip in the dashboard HTML; a row with no claims renders in Queued. Drop the old `Current Situation` tile panel and `claimed_state` form control.
2. **Project chip and project filter.** Add the configurable `project_tag_key`, `lithos_tags(prefix="project:")` fetch, project filter dropdown, per-row project chip with hash-based palette, multi-`project:*` soft warning. Acceptance: clicking a project chip applies a `?project=` filter; rows without a project tag render `(no project)`.
3. **Agent chips with role markers + human-agent visual + OR-across-roles filter.** Replace the single "Creating agent" column with collapsed agent chips. Acceptance: a single agent with `created` + `claimed` roles renders one chip with two role markers; clicking the chip applies `?agent=X` and matches creator OR claimer OR latest-poster.
4. **Recent-findings rolling buffer + boot warm-up + drawer endpoint.** Server-side buffer, boot-time `lithos_finding_list` fetch per open task, `GET /tasks/findings/recent` HTMX fragment, drawer toggle in the dashboard header. Acceptance: drawer opens populated even on a fresh page load; `finding.posted` events stream into the drawer.
5. **Latest-finding line on each open row.** Use the rolling buffer to render `<agent> — <summary>` on each row; update in place via OOB swap on `finding.posted`. Acceptance: SSE event for a visible task updates the row's latest-finding line within ~1s.
6. **Right-side panel + Expand-to-full-page route.** Wire `?selected=<task_id>` to render the dashboard with the side panel pre-opened; reuse `detail.html` between panel and `/tasks/{task_id}`; add `Expand` button. Acceptance: clicking a row updates the URL with `selected=`; closing the panel clears the param.
7. **"Why this task is here" detail-panel block.** When a task is in Needs attention, render a header block with reason chips + supporting facts. Acceptance: detail panel for a stale-open task shows `Stale open — N days since created`.
8. **Collapsible Completed / Cancelled with header counts.** Default-collapsed; click expands; URL-reflected. SSE-driven count updates even when collapsed. Acceptance: a task transitioning to completed updates the header count without expanding the section.
9. **Server-side debounced metric recompute.** Wire SSE → dirty → debounce → recompute → OOB push. Acceptance: 5 SSE events within `metrics_debounce_ms` trigger one recompute; manual refresh bypasses debounce.
10. **Title-badge notifications.** Read Needs-attention count from server-rendered attribute, update `document.title`, clear on focus. Acceptance: when N rows are in Needs attention, the browser tab shows `(N) Lithos Lens`; tab focus clears it.
11. **Disconnect "Refreshed via fallback" toast.** Polling fallback already runs; add the transient 1-second toast on each successful fallback refresh. Acceptance: with SSE killed, the toast appears every `auto_refresh_interval_s`.
12. **Empty-state coverage.** Explicit rendering for "no tasks at all", "tasks but none open", "all open healthy", "Lithos unreachable". Acceptance: the four template branches render the specified copy.

Slices 1, 2, and 4 are foundational — others depend on them. Slices 3, 5, 6, 7, 8, 10, 11, 12 are independent once the foundation lands and can be parallelised.

## Out of Scope

- **Planning View (`/tasks/plan`)** — owned by M1.5. M1 leaves a top-nav slot but does not render the route.
- **Desktop notifications** — opt-in flow, Notification API permission, transition-driven firing logic. Owned by M3.
- **LLM-curated "most significant findings"** — owned by M3.
- **Sparklines or per-day completion charts** — deferred indefinitely; M1 is counts only where applicable.
- **Knowledge → Tasks back-link badge** ("Produced by task X" in Knowledge Browser views). Already deferred to a later milestone in §17.
- **Stalled detection on the Operator View** — M1 adds the rolling buffer that powers stalled detection, but the row decoration and Project breakdown flag both ship with M1.5.
- **Outcome / completion timestamp / cancellation reason fields** — not exposed by current Lithos read tools; rendered opportunistically when present, not required by M1 acceptance.
- **Stale-cache fallback when Lithos is unreachable** — explicitly excluded; M1 just shows the degraded banner with empty state below.

## Further Notes

### Dependencies on M0 (already shipped)

M1 builds on the Common Core delivered in commits `5883522` (Milestone 0) and `382fcd4` / `7b838d4` / `03a5e34` (M1 + M2 of the legacy spec). Specifically:

- `events.py` already holds the SSE subscription and fan-out plumbing — extend, do not replace.
- `tasks.py` already holds list loading, claim fan-out, finding resolution — extend the data model, replace the `claimed_state` filter, delegate classification to the new module.
- `state.py` already wires startup / shutdown — extend for boot-time rolling-buffer warm-up.
- Templates already exist for the legacy three-group dashboard — restructure rather than rewrite from scratch.

### Spec → ADR drift

If any of the threshold defaults change after M1 ships (`stale_open_age_days`, `unclaimed_warning_minutes`, `metrics_debounce_ms`), record the rationale as an ADR under `docs/adr/`. The defaults in `[tasks]` are knobs intentionally; do not bake them into rule logic.

### Performance envelope

- ≤ 200ms time-to-first-byte on `/tasks` with 50 open tasks under the visible cap.
- Drawer fetch ≤ 50ms (in-process buffer read; no MCP call).
- Metric recompute completes in ≤ 30ms for ≤ 500 total tasks.
- Boot-time warm-up budget: ≤ 2s wall-clock for 50 open tasks (parallel `lithos_finding_list` fan-out).

### Accessibility (carry-over from `[a11y]` baseline)

- Section headers are buttons with `aria-expanded`.
- Reason chips use semantic colour + text label, never colour alone.
- Title-badge updates announced via `aria-live="polite"` on the section header.
- Keyboard navigation: arrow keys move between rows; Enter opens side panel; Escape closes panel.
