---
name: regen-manual
description: Regenerate docs/user-manual/manual.md from scratch by crawling the running Lithos Lens web UI in a browser, capturing real screenshots of every view, and writing each section from the bundled template. Explicit invocation only — user must type /regen-manual.
disabledModelInvocation: true
---

# Regenerate the user manual

You are regenerating the end-user manual for **Lithos Lens** from scratch. Output: `docs/user-manual/manual.md` plus screenshots under `docs/user-manual/screenshots/`.

Lithos Lens is a **read-only** local web UI (FastAPI) for observing Lithos coordination state — a Tasks dashboard, task detail pages, finding timelines, and a knowledge-note renderer. It never creates, claims, mutates, completes, or cancels anything. So the manual documents *viewing and navigating*, not CRUD: there is no "create/edit/delete" path to capture.

This is a full rewrite. Do **not** read the previous `manual.md` and do **not** diff against it — the bundled template is the single source of truth for structure, so just produce the new manual. The only thing you reuse from the existing folder is the `screenshots/` directory, which you overwrite as you recapture.

## Pre-flight (abort if it fails)

Lens needs a running Lithos server for non-degraded screenshots. Confirm Lens is up and its Lithos link is healthy. Lens serves on `http://localhost:8000` by default; if you start it on a different port, substitute it everywhere below.

```bash
curl -s --max-time 5 http://localhost:8000/health
```

A healthy response is `{"status":"ok", "lithos":"ok", ...}`. If the request fails, Lens is not running — start it (in its own terminal) and retry:

```bash
uv run lithos-lens     # serves on http://localhost:8000
```

If `lithos` is anything other than `"ok"`, Lens will render **degraded-mode** pages (no task data). You may still proceed to capture the degraded states for the "When Lithos is unavailable" section, but the main views need `lithos:"ok"`. If you cannot get a healthy Lithos, abort with this message and STOP:

> Cannot regenerate the manual: Lithos Lens is not serving a healthy Lithos connection. Start a Lithos server, point `lithos-lens.toml` (`[lithos-lens.lithos] url`) at it, run `uv run lithos-lens`, and try again.

Also confirm there is at least one task visible on the dashboard (so detail/findings pages have something to show). If the dashboard is empty, note that in your report — the manual can still be generated, but task-detail and findings screenshots will be sparse.

## Step 1 — Load the template

Read the bundled template `.claude/skills/regen-manual/manual-template.md`. It defines the exact shape of the output: the title block, the intro, the "do not edit by hand" note, the Contents list, and the per-view section pattern (overview paragraph + one screenshot embed + step-by-step prose). Every section you write must follow that pattern.

## Step 2 — Discover the live route inventory

Enumerate the user-facing pages from the source rather than hardcoding them. Read `src/lithos_lens/web.py` and list every route decorated with `response_class=HTMLResponse`. As of writing the **standalone pages** are:

- `/` and `/tasks` — the Tasks dashboard (same view; `/` is an alias)
- `/tasks/{task_id}` — a single task's detail page. This page also renders the task's **finding timeline** inline (the `tasks/findings.html` fragment is `{% include %}`-ed into `tasks/detail.html`).
- `/note/{knowledge_id}` — the knowledge-note renderer

Exclude non-page routes: `/health` (JSON), `/tasks/events` (an SSE stream), and `/tasks/{task_id}/findings` — the last one returns the **bare findings fragment** (unstyled, no page chrome) for live refresh, *not* a standalone page. Document the finding timeline from the detail page, not from that route. Cross-check against the Jinja templates under `src/lithos_lens/templates/` (note which are fragments vs. full pages) so you miss no rendered view. The nav also shows **Knowledge** and **Settings** links, but they are `aria-disabled` placeholders (`href="#"`) — only **Tasks** is live; say so rather than documenting them as working.

Pick a concrete `task_id` to document by reading it off the live dashboard (the task rows link to `/tasks/<id>`). Prefer a task that has findings, so the detail page's finding timeline is non-empty. For the note renderer, follow a finding's note link if one exists; otherwise pick any knowledge document id and open `/note/<id>` directly.

## Step 3 — Capture and document each view

Drive the running UI with a browser-automation MCP (e.g. `claude-in-chrome` or `chrome-devtools-mcp`). For each view:

- Navigate to `http://localhost:8000/<route>`.
- Set the viewport to **1280×800** before capturing.
- Take a **full-page PNG** screenshot.

Capture, at minimum:

- `dashboard.png` — the Tasks dashboard with its default filters.
- `dashboard-filters.png` — the dashboard after applying a filter (e.g. a status group, a tag chip, or an agent), to show filtering works via the URL query string.
- `task-detail.png` — a single task's detail page (title, description, metadata, claims).
- `findings.png` — the finding timeline section *of the detail page* (pick a task that has findings). The page can be long; capture or crop the timeline so it reads cleanly on its own.
- `note.png` — the knowledge-note renderer for a real note id (skip only if no finding links to a note; say so in the report).
- `degraded.png` — *optional* — a view rendered while Lithos is offline/degraded, for the "When Lithos is unavailable" section.

Save each to `docs/user-manual/screenshots/<name>.png` (lowercase, hyphenated, descriptive). Aim for 5–7 screenshots total. Lens is read-only, so crawling never changes any Lithos state.

## Step 4 — Write `manual.md`

Write `docs/user-manual/manual.md` from scratch, following the bundled template exactly.

Top of `manual.md` must contain:

1. `# Lithos Lens User Manual`
2. A one-paragraph intro: what Lens is for (a read-only window onto Lithos coordination state) plus the "do not edit by hand — regenerated by `/regen-manual`" line.
3. A Contents list linking to each `##` section.

Then, in order: **Getting around** (the page chrome — header, the health/status indicator, the nav between Tasks and a note), the **Tasks dashboard** (what the columns/status groups mean, how to filter by status group, claimed state, tag, agent and time range — all reflected in the URL — and that rows update live as Lithos emits events), **Task detail**, **Finding timeline**, **Knowledge notes**, and a short **When Lithos is unavailable** section describing degraded mode.

Tone: instructional second-person ("Click a task row to open its detail page..."). Audience: **operators watching Lithos**, not contributors. Do NOT document FastAPI internals, the SSE re-broadcast mechanism, config fields, or other developer concerns — point such readers to `README.md` and `docs/`.

## Step 5 — Clean up orphans

Delete any file in `docs/user-manual/screenshots/` that is not referenced from the regenerated `manual.md`:

```bash
git status docs/user-manual/screenshots/
```

Confirm only intended additions/deletions are present.

## Step 6 — Report

Print one summary line in this format:

> Manual regenerated from scratch: N sections (<list>), P screenshots captured.

Then stop. Do NOT auto-commit — the user will review the diff and commit themselves.
