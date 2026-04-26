# Lithos Lens Implementation Checklist

This checklist tracks implementation progress against `docs/REQUIREMENTS.md`.

A checkbox is complete only when the code exists, relevant tests or manual verification are done, and the acceptance behavior works in the running app. Requirements define what must exist; this checklist tracks what has actually been built.

---

## Milestone 0 — Common Core

### Scaffold

- [x] FastAPI app skeleton
- [x] Base template and top navigation
- [x] Router mounting
- [x] Static asset serving
- [x] Vendored frontend assets under `static/vendor/`
- [x] `docs/vendor-assets.md` with asset versions, source URLs, and checksums

### Config / Startup

- [x] Typed config loader
- [x] Environment variable overrides
- [x] Structured stdout logging
- [x] Lithos MCP client
- [x] Startup auto-registration via `lithos_agent_register`
- [x] Degraded boot when Lithos is offline
- [x] LiteLLM is not imported or initialized when `LENS_LLM_ENABLED=false`

### Health / Observability

- [x] `/health` endpoint
- [x] Cached Lithos health probe
- [x] Event status reporting
- [x] LLM status reporting
- [x] OTEL disabled-by-default path
- [x] Request span middleware

### Acceptance

- [x] App boots with Lithos offline
- [x] `/health` reports `lithos="unreachable"` when Lithos is offline
- [x] `/` and `/tasks` render a degraded banner instead of HTTP 500 when Lithos is offline
- [x] Startup attempts `lithos_agent_register` when Lithos is reachable
- [x] Static templates reference vendored local assets, not public CDN URLs
- [x] `docs/vendor-assets.md` exists and records vendored asset versions/checksums
- [x] With `LENS_LLM_ENABLED=false`, missing LiteLLM dependencies do not prevent boot

---

## Milestone 1 — Tasks MVP

### Query Contract

- [ ] Initial dashboard query flow
- [ ] Direct task lookup flow for `/tasks/{task_id}`
- [ ] Detail panel query flow
- [ ] Claim fan-out up to `tasks.visible_cap`
- [ ] Claimed-state filtering for open tasks
- [ ] Knowledge title resolution for `finding.knowledge_id`

### UI

- [ ] Current situation panel
- [ ] Grouped task list: open, completed, cancelled
- [ ] Filter bar
- [ ] Claim accuracy banner when open rows exceed `tasks.visible_cap`
- [ ] Detail panel
- [ ] Findings timeline without paging controls
- [ ] Knowledge links from findings
- [ ] Minimal `/note/{knowledge_id}` renderer for Tasks-only milestones

### Route Failure Behavior

- [ ] Lithos unreachable renders degraded panel/banner, not HTTP 500
- [ ] Unknown `/tasks/{task_id}` renders not-found panel and link to `/tasks`
- [ ] `lithos_task_status` failure renders task without claim section and shows retry
- [ ] `lithos_finding_list` failure renders task metadata and shows findings retry
- [ ] `lithos_read` failure for finding link renders fallback label

### Acceptance

- [ ] With three open tasks where one has an active claim, the current situation panel shows correct open, known claimed, and known unclaimed state
- [ ] Open tasks are shown regardless of age
- [ ] Completed and cancelled groups honor the default created-at range
- [ ] Claimed-state filter supports `any`, `known_claimed`, and `known_unclaimed`
- [ ] If open task count exceeds `tasks.visible_cap`, claimed-state filtering shows an accuracy banner and does not silently classify unknown rows
- [ ] Direct `/tasks/{task_id}` works for open, completed, and cancelled tasks
- [ ] Unknown `/tasks/{task_id}` renders a not-found panel, not HTTP 500
- [ ] A finding with `knowledge_id` renders a title-labelled note link when `lithos_read` succeeds
- [ ] A finding with `knowledge_id` renders a fallback label when `lithos_read` fails
- [ ] Findings timeline renders without paging controls

---

## Milestone 2 — Tasks SSE

### Event Pipeline

- [ ] Shared Lithos `/events` subscriber
- [ ] In-process event pub/sub
- [ ] `/tasks/events` browser SSE endpoint
- [ ] Normalized Lens event envelope
- [ ] Preserve Lithos event IDs for browser-side dedupe
- [ ] `requires_refresh` flag on sparse events
- [ ] Debounced reconciliation refresh

### UI Updates

- [ ] `task.created` inserts skeleton open row
- [ ] `task.claimed` updates visible claim chips optimistically
- [ ] `task.released` removes visible claim chips optimistically
- [ ] `task.completed` moves or removes visible rows optimistically
- [ ] `task.cancelled` moves or removes visible rows optimistically
- [ ] `finding.posted` increments row findings badge
- [ ] `finding.posted` refetches findings timeline when detail panel is open

### Resilience

- [ ] Disconnect badge
- [ ] Reconnect with exponential backoff
- [ ] Polling fallback while disconnected
- [ ] Full visible-list refresh after reconnect
- [ ] Browser handlers tolerate duplicate events
- [ ] Browser handlers tolerate out-of-order reconciliation responses

### Acceptance

- [ ] `task.created` inserts an open skeleton row without full-page reload and reconciles on next debounced refresh
- [ ] `task.claimed` and `task.released` update visible claim chips optimistically
- [ ] `task.completed` and `task.cancelled` move or remove visible rows without full-page reload
- [ ] `finding.posted` increments row badge
- [ ] `finding.posted` refetches the findings timeline when the detail panel is open
- [ ] `/tasks/events` emits normalized events with Lithos event IDs and `requires_refresh` where appropriate
- [ ] SSE disconnect shows "Live updates paused"
- [ ] Polling fallback refreshes the dashboard during disconnect
- [ ] Reconnect performs a full visible-list refresh

---

## Milestone 3 — Optional LLM

### LiteLLM

- [ ] Lazy import
- [ ] Config validation
- [ ] Model string pass-through to LiteLLM
- [ ] API key wiring
- [ ] Base URL wiring
- [ ] Extra headers JSON wiring
- [ ] Provider errors are surfaced per feature

### Tasks Curation

- [ ] `POST /api/tasks/findings/curate`
- [ ] "All findings" / "Most significant" toggle
- [ ] Complexity slider integration
- [ ] Curation prompt includes findings summaries, agents, and timestamps
- [ ] Curation result includes selected finding IDs and one-line rationales

### Acceptance

- [ ] Disabled mode works without LiteLLM installed
- [ ] Misconfigured provider degrades gracefully
- [ ] LLM provider error falls back to "All findings"
- [ ] OpenAI-compatible provider works through LiteLLM config
- [ ] Anthropic-compatible provider works through LiteLLM config
- [ ] OpenRouter-compatible provider works through LiteLLM config
- [ ] Local Ollama-compatible provider works through LiteLLM config
