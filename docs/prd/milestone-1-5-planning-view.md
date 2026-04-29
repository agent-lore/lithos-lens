---
title: Milestone 1.5 — Planning View
milestone: M1.5
status: draft
target_version: 0.3.0
references:
  - docs/REQUIREMENTS.md §5A (Tasks View — Planning View)
  - docs/REQUIREMENTS.md §5B (Project Tracking Conventions)
  - docs/REQUIREMENTS.md §17 (Implementation Plan)
depends_on:
  - milestone-1-operator-view.md
labels: [needs-triage, milestone-1-5, tasks-view, planning]
---

# Milestone 1.5 — Planning View

## Problem Statement

Once the Operator View answers "are my agents alive and making progress?", the next operator question is **"what should happen next?"** — and the Operator View deliberately does not answer it. Trying to make one view answer both questions produced the muddled Current-Situation tile panel that M1 is replacing.

I run many projects in parallel and I want to know:

1. **What manual work do I (a human) need to pick up?** Some tasks are tagged for human action, and some I have already claimed myself. There is no surface that aggregates these across projects.
2. **Where is system throughput stuck?** A project with one queued task and zero in-flight work is *starving*. A project with five in-flight tasks where four are claimed by one agent has a *bottleneck*. A project whose in-progress task has not posted a finding in 24h is *stalled*. None of these are visible without manually counting rows.
3. **What is the overall shape of work across projects?** Which projects are shipping? Which have not produced anything in weeks? The Operator View is deliberately tuned for *now* and does not answer this either.

The Operator View does not have room for any of these without losing focus, and the questions justify a dedicated layout.

## Solution

Ship a Planning View at `/tasks/plan`, reachable from the same top-nav as `/tasks` and (eventually) the Knowledge Browser. Three stacked sections answer the three sub-questions above:

```
👤 Human-actionable
   open tasks tagged `[tasks].human_actionable_tag`,
   grouped by project, oldest first;
   includes tasks already claimed by a human-agent

📊 Project breakdown
   per project: queue depth, in-flight depth,
   flag chips: starvation / bottleneck / stalled

📈 Throughput overview
   per project: completed count, cancelled count,
   completion ratio over the rolling window
   (default 30d), ordered by completed desc;
   dormant projects shown by default
```

The Planning View shares the M1 server-side machinery: the same SSE subscription, the same recent-findings rolling buffer (which now powers stalled detection), the same project-tag fetch (`lithos_tags(prefix="project:")`). Switching between Operator and Planning resets view-specific filter state — the views answer different questions and should not co-mingle filters.

The Operator View also gains a small enhancement from this milestone: stalled rows get a row decoration (joining the existing reason chips), but stalled does **not** promote a row into Needs attention — it is a softer signal.

## User Stories

1. As an operator, I want a Planning View at `/tasks/plan`, reachable from the top-nav, so that I can switch between "what's going on now" and "what should happen next" with one click.
2. As an operator, I want switching between Operator and Planning views to reset view-specific filter state, so that filters tuned for one question do not silently distort the other.
3. As a human collaborator on the agent fleet, I want a Human-actionable section at the top of the Planning View showing open tasks tagged `[tasks].human_actionable_tag` (default `human`), so that I can see the manual work waiting for me across all projects in one place.
4. As a human collaborator, I want the Human-actionable section to also include open tasks already claimed by an agent listed in `[tasks].human_agents`, so that I can resume work I have already taken without losing track of it.
5. As a human collaborator, I want the Human-actionable section grouped by project, oldest first within each project, so that I can prioritise older work and stay focused inside a single project when I want to.
6. As a human collaborator with no human-actionable tasks waiting, I want a "Nothing for you to do right now ✓" empty state, so that the section reads as a definite "all clear" rather than a missing render.
7. As an operator, I want a Project breakdown section showing queue depth and in-flight depth per project, so that I can see where work is piling up and where it is being processed.
8. As an operator, I want a "starvation" flag on any project with a non-empty queue and zero in-flight work, so that I can spot projects where the agent fleet is not processing the queue.
9. As an operator, I want a "bottleneck" flag on any project where in-flight depth is at least `[tasks].bottleneck_min_inflight` (default 3) and one agent holds at least `[tasks].bottleneck_concentration` (default 0.7) of those claims, so that I can spot single-agent overload.
10. As an operator, I want a "stalled" flag on any project that has at least one in-progress task with no `finding.posted` in the last `[tasks].stalled_no_findings_hours` (default 24h), so that I can spot agents that are claimed but not making progress.
11. As an operator, I want hovering over any flag chip to surface the rule details (which agent dominates the bottleneck, which task is stalled, etc.), so that I can drill into "why" without leaving the Planning View.
12. As an operator, I want a Throughput overview section showing completed count, cancelled count, and completion ratio per project over `[tasks].throughput_window_days` (default 30d), so that I can see which projects are shipping and which are churning.
13. As an operator, I want the Throughput overview ordered by completed count desc, then completion ratio desc, then alphabetical, so that the most productive projects surface first.
14. As an operator, I want dormant projects (zero activity in the window) shown by default with `0 / 0`, so that I can spot projects that have stalled at the macro level — not just within a few open tasks.
15. As an operator, I want a "Hide dormant" toggle that suppresses dormant projects, so that I can focus on active projects when I want a tighter view.
16. As an operator, I want the Planning View filter bar to support project (multi-select), created-at range (within window), and the Hide-dormant toggle, so that I can scope all three sections consistently.
17. As an operator, I want a stalled row decoration on the Operator View for in-progress tasks that meet the stalled rule, so that "stalled" is visible from both views even though only the Planning View flags it at the project level.
18. As an operator, I want the project list across both Planning sections sourced from `lithos_tags(prefix="project:")` (the universe of projects ever to have had a task), so that long-dormant projects do not silently vanish from the Throughput overview.
19. As an operator, I want the Planning View to share the M1 server-side recent-findings rolling buffer for stalled detection, so that no new MCP calls are added beyond what the Operator View already costs.
20. As an operator, I want the Planning View's Project breakdown section to refresh in place when SSE events change queue depth, in-flight depth, or stalled status, so that the view is live without requiring full-page reload.
21. As an operator, I want the Planning View's Throughput overview to refresh on `task.completed` and `task.cancelled` SSE events within the debounce window, so that throughput counts stay current without a polling loop.

## Implementation Decisions

### Modules to introduce

- **Planning aggregation module** — pure functions that take the dashboard task list (already loaded for the Operator View), the recent-findings rolling buffer, and config thresholds, and return:
  - `human_actionable_groups(tasks, claims, human_agents, human_actionable_tag) -> dict[project_slug, list[EnrichedTask]]`
  - `project_breakdown(tasks, claims, buffer, thresholds, now) -> list[ProjectBreakdownRow]` where each row carries queue depth, in-flight depth, and flag chips.
  - `throughput_overview(tasks, window_start, now) -> list[ThroughputRow]` where each row carries completed/cancelled counts and ratio.
- **Stalled detection** — pure function `is_stalled(task, claims, buffer, threshold_hours, now) -> bool`. Used by both the Project breakdown flag and the Operator View row decoration. Lives next to the section classifier from M1 since they share the rolling buffer dependency.

### Modules to modify

- `web.py` — add three endpoints: `GET /tasks/plan` (full Planning View), `GET /tasks/plan/projects` (Project breakdown HTMX fragment), `GET /tasks/plan/throughput` (Throughput overview HTMX fragment). Add Planning View entry to the top-nav of all Tasks routes.
- `tasks.py` (or a new `tasks_plan.py` router-side module) — orchestrate the data load. Crucially, it must reuse the same `lithos_task_list` calls and the same `lithos_task_status` fan-out as the Operator View — the Planning View is a different rendering of the same loaded data. Switching between views re-fetches but the result of the per-row claim fan-out is structured to be reused.
- `events.py` — extend the metric recompute to include Planning View aggregates (debounced together with Operator View metrics; no separate scheduler).
- `config.py` — add `[tasks]` knobs: `bottleneck_min_inflight`, `bottleneck_concentration`, `stalled_no_findings_hours`, `throughput_window_days`, `human_actionable_tag`, `human_agents`. (`human_actionable_tag` and `human_agents` are also referenced by the Operator View's human-agent visual but are wired in this milestone.)
- Templates — new `tasks_plan/dashboard.html`, `tasks_plan/human_actionable.html`, `tasks_plan/projects.html`, `tasks_plan/throughput.html`. Reuse `tasks/row.html` from M1 for the Human-actionable section.

### Filter and URL contract

- `?project=<slug>` (multi-value) — scopes all three sections.
- `?since=<date>` — bounded to `throughput_window_days`; defaults to that.
- `?hide_dormant=1` — Hide-dormant toggle for Throughput overview.

### MCP / SSE dependencies

No new MCP tools beyond what M1 uses. The Planning View runs on:

- `lithos_task_list(status=...)` — the existing three calls (open / completed / cancelled), reused from the Operator View load path.
- `lithos_task_status(task_id)` — already done as part of the visible-cap fan-out.
- `lithos_tags(prefix="project:")` — already cached per request from M1.
- The recent-findings rolling buffer from M1 — drives stalled detection.
- SSE event types: same as M1.

If `lithos_task_list(status="completed")` returns a small slice (Lithos has no `completed_at` filter today), the Throughput overview is limited to completed tasks whose `created_at` is within the window. This is documented as a Lithos-side limitation, not a Lens MVP requirement.

### Telemetry

- `lens.tasks.plan` — page render
- `lens.tasks.plan.projects` — Project breakdown computation
- `lens.tasks.plan.throughput` — Throughput overview computation
- `lens.tasks.metrics_recompute` — extended to include Planning aggregates (one trigger handles both views)

## Testing Decisions

Tests assert **external behaviour** — the data shape returned by aggregation rules and the HTML structure rendered by templates. Aggregation rules are pure and table-driven.

### What to test

- **Stalled detection** — table-driven: in-progress task with last finding 23h ago → not stalled; 25h ago → stalled; in-progress with no buffer entries within window → stalled; not-in-progress task → never stalled regardless of buffer.
- **Project breakdown rules** — starvation: queue=1 in-flight=0 → flag fires; queue=0 in-flight=0 → no flag; bottleneck: in-flight=3 with 3/3 by one agent → flag fires; in-flight=3 with 2/3 by one agent → does not fire; in-flight=2 with 2/2 by one agent → does not fire (below `min_inflight`); stalled flag fires when any in-progress task in project meets the stalled rule.
- **Throughput aggregation** — given a synthetic task list with N completed in window and M cancelled in window, ratio is N/(N+M) (or `—` when both zero); ordering: most-completed-first, then ratio, then alphabetical; dormant projects (zero in window) appear with `0 / 0` when `hide_dormant=False`, are excluded when `hide_dormant=True`.
- **Human-actionable selection** — open task with `human_actionable_tag` → included; open task without the tag → excluded; open task without the tag but claimed by an agent in `human_agents` → included; non-open task with the tag → excluded.
- **End-to-end Planning View rendering** — `TestClient` extension that sets up a `TaskFakeLithosClient` with multi-project tasks across statuses, a known set of claims, and a synthetic finding buffer; assert that all three sections render the expected projects, flags, and counts.
- **Operator View stalled row decoration** — same fake fixture; in-progress task meeting the stalled rule renders the decoration on the Operator View; same task does **not** appear in Needs attention.
- **Top-nav switching** — visiting `/tasks?project=ganglion`, then `/tasks/plan`, the Planning View renders without the `project=ganglion` filter applied (view switching resets filters).

### Prior art

- The M1 `TaskFakeLithosClient` fixture already supports multiple tasks, claims, findings — extend it for the Planning View test cases rather than starting fresh.
- The M1 section classification tests are pure-function table-driven; mirror that style for stalled detection, starvation, and bottleneck rules.

### Coverage target

≥ 80% line coverage on the planning aggregation module and stalled detection. End-to-end tests cover at least the six acceptance bullets in §17 M1.5.

## Tracer-bullet vertical slices

1. **Stalled detection rule + Operator View row decoration.** Pure function, tests, and the row decoration. Acceptance: in-progress task with no finding in 25h renders a `stalled` decoration on the Operator View; same task does not appear in Needs attention.
2. **`/tasks/plan` shell + Human-actionable section.** New router, base template, top-nav entry, Human-actionable selection rules and rendering. Acceptance: open tasks tagged `human` appear grouped by project; tasks claimed by a `human_agents` member also appear; empty state reads "Nothing for you to do right now ✓".
3. **Project breakdown section with starvation flag.** Aggregation function for queue/in-flight depths per project; starvation rule; HTMX fragment endpoint. Acceptance: project with 1 queued task and 0 in-flight renders a `starvation` flag; tooltip shows rule details.
4. **Bottleneck detection flag.** Add to the existing aggregation function; render flag chip with hover tooltip naming the dominant agent. Acceptance: project with 5 in-flight tasks where 4 are claimed by one agent renders a `bottleneck` flag.
5. **Stalled flag in Project breakdown.** Reuse the M1.5-1 stalled rule at the project aggregate level. Acceptance: project with one stalled in-progress task renders a `stalled` flag.
6. **Throughput overview section.** Aggregation function; HTMX fragment endpoint; default ordering; dormant projects rendered with `0 / 0`. Acceptance: counts and ratios match a synthetic fixture; dormant projects appear by default.
7. **Hide-dormant toggle.** Cookie + URL persistence; suppression in the rendered fragment. Acceptance: toggling hides dormant projects and round-trips through the URL.
8. **View-switching filter reset.** Top-nav links from `/tasks/plan` to `/tasks` (and vice versa) drop view-specific query params. Acceptance: navigating from `/tasks?project=ganglion` to `/tasks/plan` lands without `project=ganglion`.
9. **Planning View live updates.** Wire the metric recompute to also push Planning fragments via OOB swap on dashboard tabs that have the Planning View open. Acceptance: SSE `task.claimed` event updates Project breakdown depths within the debounce window.

Slices 1, 2, and 3 are foundational. Slices 4, 5, 6 depend on the project breakdown skeleton from slice 3. Slices 7, 8, 9 are independent.

## Out of Scope

- **Per-project sparklines or daily-completion charts** — explicitly deferred. M1.5 is counts only.
- **`due:<date>` overdue flag** — depends on a tagging convention not yet in place. Defer until conventions exist.
- **"Single-agent project" diagnostic flag** — judged as noise on the Planning View; skipped.
- **Saved filter presets** — not in M1.5.
- **Per-project drill-down page** (e.g. `/tasks/plan/project/ganglion`) — not in M1.5; click on a project chip applies a filter on the existing views instead.
- **Knowledge Browser cross-links to projects** — owned by the Knowledge Browser milestones (M4+).
- **LLM-driven recommendation of "what to work on next"** — not in scope; Planning View is rule-based.

## Further Notes

### Dependencies on M1

The Planning View depends on:

- The recent-findings rolling buffer (slice M1-4).
- The cached `lithos_tags(prefix="project:")` fetch (slice M1-2).
- The section classification module (so it can correctly identify "in-progress" vs "queued" rows when computing per-project depths).
- The SSE-driven metric recompute scheduler (slice M1-9).

If M1 ships in a partial state, the Planning View should not start. There are no hidden alternatives — the Planning View is a different rendering of the data the Operator View loads.

### Performance envelope

- ≤ 200ms time-to-first-byte on `/tasks/plan` with 500 tasks across 16 projects.
- Aggregation completes in ≤ 30ms (pure functions over already-loaded data).
- Throughput aggregation does not introduce extra MCP calls — it reuses the `lithos_task_list(status=completed/cancelled)` results already loaded for the Operator View.

### Telemetry note

If the Planning View ends up frequently re-fetching `lithos_task_list(status="completed")` because the Operator View load path is per-request, factor the load path so both views share a per-request `DashboardSnapshot` cache. This is a refactor, not a new dependency.

### Configuration migration

Existing deployments with `[tasks]` config get the new knobs at their defaults silently; nothing in M1.5 is breaking. The first deployment that wants to change `bottleneck_concentration` etc. updates the TOML.
