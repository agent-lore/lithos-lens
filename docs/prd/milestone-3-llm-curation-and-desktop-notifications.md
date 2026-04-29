---
title: Milestone 3 — Optional LLM client, Tasks curation, and Desktop notifications
milestone: M3
status: draft
target_version: 0.4.0
references:
  - docs/REQUIREMENTS.md §5.5.3 (Most-significant findings, optional LLM)
  - docs/REQUIREMENTS.md §5.2.5 (Notifications)
  - docs/REQUIREMENTS.md §17 (Implementation Plan)
depends_on:
  - milestone-1-operator-view.md
labels: [needs-triage, milestone-3, tasks-view, llm, notifications]
---

# Milestone 3 — Optional LLM client, Tasks curation, and Desktop notifications

## Problem Statement

Two related operator pain points remain after M1 ships:

1. **Findings timelines on long-running tasks become noisy.** A task that has run for several hours produces findings ranging from "checked X" trivialities to "completed Y" or "discovered contradiction in Z" — the things that actually matter. Today I have to scan the whole timeline to spot the real signal.
2. **The Operator View only talks back when I'm looking at the page.** Title-badge notifications (M1) catch the case where Lens is in another tab. They do not catch the case where Lens is in another window, on another desktop, or in the background while I work in an IDE. By the time I notice a `(3) Lithos Lens` in the tab bar, the agent has already been stuck for some time.

Both pain points share the same machinery: M3 introduces the optional LLM client and the desktop-notification permission flow. They land together because they're both "give the operator more channels for the dashboard to talk back" features and both require the small configuration / permission UX that's cleaner to wire once.

## Solution

Two features under one milestone:

### Tasks "Most significant findings" curation (LLM-backed)

Add a toggle in the task detail panel and full-page route: **All findings / Most significant**. When toggled on, Lens passes the full findings list (summaries + agents + timestamps) to a configured LiteLLM provider with a prompt asking for the K findings with the largest signal — completion announcements, decisions, surprises, contradictions — each with a one-line rationale. The complexity slider (also added in M3) modulates verbosity. With LLM disabled, the toggle is hidden; the rest of the timeline is unchanged.

When Lithos exposes a synthesis MCP tool (`lithos_synthesize` or equivalent) in a future MVP, Lens prefers the MCP path and treats the local LLM as fallback.

### Desktop notifications (opt-in)

Add an "Enable notifications" affordance to the Operator View header. Once granted, Lens fires a desktop notification whenever a row *enters* Needs attention (a transition, not steady-state). Notification body: `<task title> — <reason>`. Clicking the notification opens `/tasks?selected=<task_id>`. Grant state lives in `localStorage` (per-browser-install). All other notification preferences live in cookies, consistent with the M1 persistence policy.

Both features sit behind config flags that default to off / opt-in. With LLM disabled and notifications declined, M3 changes nothing about M1 behaviour.

## User Stories

### LLM curation

1. As an operator viewing a task with a long findings timeline, I want a "Most significant" toggle in the timeline header, so that I can collapse a noisy timeline down to the entries that actually matter.
2. As an operator, I want each curated finding to carry a one-line rationale (e.g. "Completion announcement", "Detected contradiction in approach"), so that I understand why the model picked it.
3. As an operator, I want the curation toggle hidden entirely when `llm.enabled = false`, so that the UI does not promise capability the deployment does not have.
4. As an operator, I want a complexity slider (1 = beginner … 5 = expert) in the curation panel, so that I can tune the verbosity of the rationales to my context.
5. As an operator, I want curation failures (provider error, timeout) to fall back to the full timeline with a non-blocking warning toast, so that a temporary LLM outage never blocks me from seeing the raw findings.
6. As an operator, I want curation latency surfaced in the panel (e.g. "Curated by anthropic/claude-haiku in 1.4s"), so that I can judge whether to wait for it on long tasks.
7. As an operator deploying Lens with Lithos exposing a `lithos_synthesize` tool, I want Lens to prefer the MCP path over the local LLM, so that synthesis runs on the canonical Lithos surface when available and saves me from configuring my own LLM credentials.
8. As an operator, I want LLM status surfaced in `/health` and the settings view, so that I can debug "why is the toggle missing?" without reading logs.

### Desktop notifications

9. As an operator running Lens in a background tab, I want an "Enable notifications" affordance in the Operator View header, so that I can opt in to desktop notifications when I want to be alerted while working in another window.
10. As an operator, I want notifications to fire only when a row *enters* Needs attention (a transition), not every SSE event, so that the channel does not become noise.
11. As an operator, I want each notification body to read `<task title> — <reason>` (e.g. "Implement BLE reconnect logic — Claim expired"), so that I can decide whether to switch tabs from the notification alone.
12. As an operator, I want clicking a notification to open `/tasks?selected=<task_id>`, so that I land directly on the relevant task in the side panel.
13. As an operator who has revoked notification permission, I want the "Enable notifications" affordance to reappear on next page load, so that I can re-enable when I want to without hunting through browser settings.
14. As an operator, I want desktop notifications to be turn-off-able via `[tasks].notifications.desktop_optin = false`, so that a deployment that doesn't want the affordance can remove it entirely.
15. As an operator who never grants the permission, I want the title-badge notifications from M1 to keep working unchanged, so that opting out of desktop alerts does not regress the in-page experience.

## Implementation Decisions

### Modules to introduce

- **LiteLLM-backed LLM client** — `app/llm_client.py` (already named in §2). Provider-agnostic wrapper exposing a small interface: `curate_findings(findings, complexity) -> CuratedFindings` (this milestone), with `synthesize(query, snippets, complexity)` and `compare_themes(notes)` reserved for future Knowledge Browser milestones. Validates configuration at startup (model string, key when required); does not require a paid completion call to pass readiness.
- **Findings curation feature module** — pure orchestration: takes a `FindingRecord` list and a complexity level, builds the prompt, calls the LLM client, parses the structured response into a `CuratedFindings` dataclass with `(finding_id, rationale)` tuples plus model metadata. Errors surface as an `error: str` field, not exceptions.
- **MCP-synthesis preference layer** — small adapter that prefers `lithos_synthesize` when the Lithos build exposes it and falls back to the local LLM client otherwise. Detection uses an MCP capabilities probe at startup; result is cached.
- **Notification preferences module** — small server-side helper for reading/writing the `desktop_optin` and `title_badge` cookies. (localStorage grant state is browser-only; no server-side module needed.)

### Modules to modify

- `web.py` — add `POST /api/tasks/findings/curate` endpoint. Update task detail and findings templates to render the curation toggle when `llm.enabled` and `llm.findings_curation_enabled` are both true. Add the "Enable notifications" affordance to the Operator View header.
- `state.py` — instantiate the LLM client only when `config.llm.enabled` is true; surface LLM status in the health snapshot.
- `events.py` — extend the metric recompute to emit a "Needs-attention transition" signal (a row entering Needs attention from another section). The browser-side notification handler subscribes to this signal via `/tasks/events`.
- `config.py` — already has the `llm.*` block from M0; add `[tasks.notifications].desktop_optin` and ensure `findings_curation_enabled` is wired.
- Templates — extend `tasks/findings.html` with the curation toggle and rendered rationales; add a `tasks/notifications_optin.html` partial for the header affordance.
- Static — small JavaScript module for desktop notifications: read grant state from `localStorage`, request permission on click, register a `notification_show(payload)` callback wired to the `/tasks/events` stream.

### LLM provider contract

Lens delegates provider-specific behaviour to LiteLLM. The model string drives the routing:

- `LENS_LLM_MODEL=anthropic/claude-haiku-4-5-20251001` → Anthropic
- `LENS_LLM_MODEL=openai/gpt-4o-mini` → OpenAI
- `LENS_LLM_MODEL=ollama/llama3.1` → local Ollama
- `LENS_LLM_MODEL=openrouter/anthropic/claude-3.5-sonnet` → OpenRouter

Provider-specific extras (`LENS_LLM_BASE_URL`, `LENS_LLM_API_KEY`, `LENS_LLM_EXTRA_HEADERS_JSON`) are passed through. No provider-specific code branching inside Lens.

### Curation prompt contract

The curation prompt asks for:

- A JSON array of objects with `finding_id` and `rationale` (one-line, max 80 chars).
- The K most-significant findings, where K is roughly `min(8, ceil(N/3))` for N total findings — tunable.
- Significance defined as: completion announcements, decisions, surprises, contradictions, errors, blocked-on signals.
- Verbosity modulated by the complexity slider (1=terse, 5=technical/detailed).

Schema validation happens server-side; malformed responses fall back gracefully.

### Notification permission lifecycle

- Affordance visible only when `[tasks].notifications.desktop_optin = true` AND grant state is not `granted`.
- Click → `Notification.requestPermission()`. On `granted`, store `{state: "granted", granted_at: ISO}` in `localStorage`.
- On `denied`, store `{state: "denied", denied_at: ISO}` and do not auto-show the affordance again for ≥ 7 days (cookie-based suppression).
- On page load, if `Notification.permission` reads `denied` but `localStorage` says `granted`, treat as revoked and re-show the affordance.

### Trigger semantics for desktop notifications

A notification fires when:

1. The metric recompute detects that task X is now in Needs attention.
2. Task X was *not* in Needs attention in the previous server-side metrics snapshot.
3. The browser has permission and the user's `desktop_optin` cookie is true.

Notifications do not fire on every SSE event, only on this transition. The transition event is published via `/tasks/events` as a normalised lens event (`type: lens.tasks.attention_entered`).

### Accessibility

- Curation toggle is a real `<button>` with `aria-pressed` reflecting state.
- Curated rationales render as a list with the original finding linked by `finding_id`.
- Notification affordance uses a real button labelled "Enable desktop notifications"; success / failure states announced via `aria-live`.

### Telemetry

- `lens.tasks.curate` — curation call (attribute `provider`, `model`, `latency_ms`, `findings_in`, `findings_out`).
- `lens.llm.synthesize` — reserved for future milestones; not used in M3.
- `lens.tasks.attention_entered` — counter incremented per transition; no notification-specific span (browser-side).
- LLM error counter on `/health` LLM section.

## Testing Decisions

Tests assert **external behaviour** — the JSON response shape from the curation endpoint, the rendered HTML when the toggle is on/off, the transitions emitted by the metric recompute, the notification permission lifecycle. The LLM provider itself is mocked at the LiteLLM boundary; no real network calls in tests.

### What to test

- **LLM client wrapper** — table-driven: missing required config (`model` when `enabled=true`) → startup logs error and `health.llm = "error"`; valid config → `health.llm = "ok"` without making a paid call; transient provider error → wrapped in a `LLMError` with retry hint, not a raw exception.
- **Curation feature module** — given a fixture findings list and a mocked LiteLLM response, the module returns the expected `CuratedFindings` dataclass with rationales and the K-by-N rule honoured; malformed JSON response → falls back to "synthesis unavailable" with no exception leaking.
- **MCP-synthesis preference layer** — when `lithos_synthesize` capability is advertised, curation calls the MCP path; when not, calls the local LLM; on `not_supported` from MCP, falls back to local LLM.
- **`POST /api/tasks/findings/curate` endpoint** — happy path renders rationales; LLM disabled returns 404; LLM provider error returns the structured error object (no 500); complexity slider value is propagated into the prompt.
- **Detail-panel UI gating** — toggle hidden when `llm.enabled=false`; toggle visible when `llm.enabled=true` AND `findings_curation_enabled=true`; toggle hidden when `findings_curation_enabled=false` even with LLM enabled.
- **Needs-attention transition emission** — given a sequence of metric snapshots where task X moves from In progress to Needs attention, the recompute emits exactly one `lens.tasks.attention_entered` event; subsequent recomputes with X still in Needs attention emit zero further events; X moving back to In progress and then back into Needs attention emits one new event.
- **Notification affordance** — affordance present when `desktop_optin=true` and grant is not present; absent when `desktop_optin=false`; absent when grant is `granted`; reappears on `denied` after the 7-day suppression window.
- **Notification permission flow (Playwright/JSDOM)** — clicking the affordance triggers `Notification.requestPermission`; on `granted`, `localStorage` populated; subsequent transitions trigger `new Notification(...)` with the expected `body` and `data.task_id`.
- **Click-through navigation** — synthetic notification click resolves to `/tasks?selected=<task_id>` (asserted by intercepting the click handler in the test browser).

### Prior art

- The Operator View test fixture (`TaskFakeLithosClient`) extends naturally to mock the LLM client at the wrapper boundary; reuse the fixture pattern from M1 / M1.5 tests.
- The LiteLLM mock can be a small stub class that records calls and returns canned responses, parameterised per test.

### Coverage target

≥ 80% line coverage on the LLM client wrapper, the curation feature module, and the MCP-synthesis preference layer. End-to-end tests cover the curation toggle gating, transition emission, and notification permission lifecycle.

## Tracer-bullet vertical slices

1. **LiteLLM-backed LLM client wrapper + health probe.** Shipped without any UI. Acceptance: with `LENS_LLM_ENABLED=true` and a valid model string, `/health` reports `llm="ok"`; with `enabled=false`, reports `"disabled"`; with invalid config, reports `"error"` and the error reason in settings view.
2. **`POST /api/tasks/findings/curate` endpoint with hardcoded prompt.** No UI yet; tests the endpoint and the curation feature module. Acceptance: a fixture findings list returns curated rationales; provider error returns structured error.
3. **Findings panel curation toggle.** Wire the toggle to the endpoint; render rationales when toggled on; handle LLM-error fallback. Acceptance: toggle visible only when both `llm.enabled` and `findings_curation_enabled`; LLM error shows non-blocking warning.
4. **Complexity slider (session-scoped).** Slider in the curation panel; cookie-persisted; injected into curation prompt. Acceptance: changing the slider re-runs curation with the new value; setting persists across page reload.
5. **MCP synthesis preference layer.** Capability probe at startup; preference logic; fallback test with mocked Lithos returning `not_supported`. Acceptance: when the mocked Lithos advertises synthesis, curation routes through MCP; otherwise local.
6. **Needs-attention transition emission.** Metric recompute tracks previous snapshot per task; emits `lens.tasks.attention_entered` events. Acceptance: deterministic emission per the test cases above; events visible in `/tasks/events`.
7. **Notification permission affordance.** Header button gated by config and grant state; `localStorage` write on grant. Acceptance: click triggers `Notification.requestPermission`; affordance hides on grant.
8. **Browser-side notification firing.** JS handler subscribes to `lens.tasks.attention_entered` via `/tasks/events`; fires `new Notification(...)` with body and click handler. Acceptance: simulated transition fires a notification with the expected body; click resolves to `/tasks?selected=`.
9. **Settings view extension.** Surface LLM status, model name (no API key), notification config, and findings curation flag. Acceptance: settings page renders the new fields; values match config.

Slices 1–4 are independent of the notification slices. Slices 6–8 form a chain: 6 must land before 7/8 are useful. Slice 9 is a small surface that can land last.

## Out of Scope

- **Knowledge Browser LLM features** — answer synthesis, comparison "Themes & Concepts", LLM-curated reading paths. Owned by M9 in §17.
- **LLM streaming responses** — M3 uses non-streaming completion; streaming is an optimisation for later.
- **Per-finding feedback (👍 / 👎 on curated rationales)** — out of scope; feedback in v1 is for knowledge items, not curation rationales.
- **Custom prompts via UI** — the curation prompt is a code-owned template tuned by Lens. Operators can swap providers via env, not prompts.
- **Email / SMS / Slack notifications** — only browser desktop notifications in M3.
- **Mobile push notifications** — out of scope. Lens is desktop browsers.
- **Server-side notification queueing for offline browsers** — out of scope. Notifications are best-effort and fire only when a browser tab is connected to `/tasks/events`.

## Further Notes

### Dependencies on M1

M3 depends on:

- The metric recompute scheduler (slice M1-9) — reused for transition tracking.
- The `/tasks/events` re-broadcast endpoint — reused for the new `lens.tasks.attention_entered` event type.
- The Operator View header — host for the notification affordance.
- The detail panel and findings template — host for the curation toggle.

The LLM client itself does not depend on M1.5 (Planning View). M3 can ship before M1.5, after M1.5, or interleaved — there are no ordering constraints between M1.5 and M3.

### Cost envelope

- Curation prompt: typically < 2k input tokens, < 500 output tokens. With Haiku-class models, cost per curation < $0.001.
- Cache: same task + same complexity → cache the curated result for 60s. SSE `finding.posted` invalidates the cache for that task.
- No automatic curation — it only runs when the operator toggles. No background curation, no eager precomputation.

### Provider portability

- The LLM client never imports provider-specific SDKs directly — all calls go through LiteLLM. Swapping Anthropic ↔ OpenAI ↔ Ollama is an env-var change.
- LiteLLM is an *optional* extra dependency installed via `uv sync --extra llm`. With `LENS_LLM_ENABLED=false`, the import is lazy and the package not being installed is not a startup failure.

### Privacy

- LLM calls send finding summaries and agent names to whatever provider is configured. Operators choosing self-hosted Ollama keep all data on-machine; operators choosing cloud providers should know the data goes to that provider.
- Lens never sends API keys or secrets in prompts. The curation prompt is structured to operate on the finding summaries only.

### Spec → ADR drift

- The choice to lump desktop notifications into the LLM milestone is a scope-management call, not a technical one. If the LLM work proves heavier than expected, splitting M3 into M3a (LLM) and M3b (Desktop notifications) is acceptable.
- The MCP-synthesis preference is a forward-compatibility hook. If Lithos never ships `lithos_synthesize`, the preference layer remains as a no-op shim and adds < 50 LOC.
