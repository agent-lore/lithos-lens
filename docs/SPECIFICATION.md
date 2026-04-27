# Lithos Lens - Specification

Version: 0.1.0  
Date: 2026-04-27  
Status: Aligned with Implementation

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
- `GET /note/{knowledge_id}`
  Renders a minimal note page backed by Lithos note reads.

No authenticated routes currently exist.

### 5.2 Configuration

Lens loads configuration from:

1. `./lithos-lens.toml`
2. `~/.lithos-lens/lithos-lens.toml`
3. `/etc/lithos-lens/lithos-lens.toml`

Environment variables may also be used for deployment, following the project
README and container examples.

The current configuration model includes:

- `storage.data_dir`
- `logging.level`
- `lithos.url`
- `lithos.mcp_sse_path`
- `lithos.sse_events_path`
- `lithos.agent_id`
- `tasks.auto_refresh_interval_s`
- `tasks.visible_cap`
- `tasks.default_time_range_days`
- `tasks.default_status_groups`
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

Defaults are implementation-defined in `src/lithos_lens/config.py`.

### 5.3 Tasks Dashboard

The Tasks view is the primary implemented feature in Lens.

It currently provides:

- Summary cards for:
  - open task count
  - known claimed count
  - known unclaimed count
  - recently completed count
  - recently cancelled count
  - registered agent count
- Grouped task lists for:
  - open
  - completed
  - cancelled
- Per-task display of:
  - title
  - excerpt/summary text
  - status
  - creating agent
  - creation timestamp
  - claim state where known
  - finding count/status
  - tags

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
  preserved when clicking a tag.
- Tags with the `project:` prefix are rendered with distinct visual styling but
  are otherwise filtered the same way as other tags.

### 5.5 Visible Cap and Claim-State Semantics

Lens is designed for deployments with tens to low hundreds of tasks, not
thousands.

The dashboard enforces a visible-cap model:

- Open-task task counts represent all matching open tasks, not just visible
  rows.
- Claim-state enrichment is attempted for visible open tasks.
- If claim enrichment cannot be determined beyond the visible cap or because of
  fetch failures, the dashboard surfaces that condition as part of the summary.

This is a pragmatic operational dashboard model rather than a full audit UI.

### 5.6 Task Detail View

The task detail page currently shows:

- task title and body/summary content
- status metadata
- creating agent
- created timestamp
- tags
- claim state where known
- findings list
- related note links where available

Detail rendering is read-only in the current implementation.

### 5.7 Note View

`/note/{knowledge_id}` exists as a minimal read path.

Current behavior:

- Lens asks Lithos to read the corresponding note.
- Lens renders a simple note page when the note exists.
- This is not yet a full knowledge browser.

### 5.8 Live Updates

Lens currently implements live task updates using SSE.

Current architecture:

- Lens opens a single shared upstream subscription to Lithos `/events`.
- Lens filters and normalizes task-relevant events.
- Lens republishes them to browser clients via `GET /tasks/events`.

The currently recognized event types are:

- `task.created`
- `task.claimed`
- `task.released`
- `task.completed`
- `task.cancelled`
- `finding.posted`

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
- note read capability
- agent registry/statistics endpoints used by the dashboard
- an `/events` SSE stream carrying task-related events

Lens is intentionally conservative in what it assumes from Lithos. When data is
ambiguous or partially missing, Lens treats parsing and enrichment as best
effort and continues rendering what it can.

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

- configurable log levels
- task filter/debug logging around dashboard requests
- optional telemetry configuration scaffolding

Telemetry configuration exists in the codebase, but telemetry is not the
primary operational mechanism today. Logging remains the main implemented
observability path.

## 9. Testing State

The current repository includes meaningful automated tests for:

- common application wiring
- tasks dashboard behavior
- task filtering and rendering behavior
- task SSE normalization and fan-out behavior

The implemented tests are intended to exercise real behavior with lightweight
fakes, rather than shallow mock-only checks.

## 10. Known Gaps Relative to Requirements

The following requirement areas are not yet implemented in the current state:

- first-class knowledge browser/feed
- archive-backed file serving and in-browser document viewing
- saved reading paths
- LLM-assisted curation, summaries, or browsing assistance
- authentication
- broader cross-note browsing and filtering

These belong to future milestones and should not be assumed to exist merely
because they are described in `docs/REQUIREMENTS.md`.

## 11. Compatibility Statement

This specification describes the behavior of Lithos Lens `0.1.0` as currently
implemented in this repository.

If the implementation and this document diverge, the implementation should be
treated as authoritative in the short term and this specification should be
updated to realign with shipped behavior.
