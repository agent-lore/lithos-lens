---
name: regen-manual
description: Regenerate docs/user-manual/manual.md from scratch by crawling the running Lithos Lens web UI in a browser, capturing real screenshots of every view, and writing each section from the bundled template. Explicit invocation only — user must type /regen-manual.
disabledModelInvocation: true
---

# Regenerate the user manual

You are regenerating the end-user manual for **Lithos Lens** from scratch. Output: `docs/user-manual/manual.md` plus screenshots under `docs/user-manual/screenshots/`.

Lithos Lens is a local web UI (FastAPI) for observing Lithos coordination state — a Tasks dashboard, the task dependency graph, task detail pages with finding timelines, knowledge search, note pages and the knowledge graph — plus a small set of **curated write actions** (new task/epic/gate, cancel, reopen, add a dependency, proceed past a gate) that are offered only once an operator identity is chosen. The manual documents viewing and navigating first, then the write actions **as affordances and confirmation pages only**: never submit a write while regenerating.

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

Enumerate the user-facing pages from the source rather than hardcoding them: grep `src/lithos_lens/*_routes.py` and `web.py` for `@router.get(`/`@app.get(` routes returning HTML. As of writing the **standalone pages** are:

- `/` and `/tasks` — the Tasks dashboard (same view; `/` is an alias); `/tasks/new` — the create form (operator only)
- `/tasks/graph` — the dependency graph (scope picker; `?project=` or `?epic=`)
- `/tasks/{task_id}` — a task's detail page, which renders the **finding timeline** inline, the nearby mini-graph, and the write affordances
- `/operator` — the operator identity page
- `/knowledge`, `/knowledge/tags`, `/note/{knowledge_id}` — knowledge search, tags, and the note page
- `/knowledge/graph` — the knowledge graph (`?namespace=`/`?type=` scope, `&selected=` node panel, `&edge=` edge panel, `&canvas=full` full page)

Exclude non-page routes: `/health` (JSON), the SSE streams, and the HTMX fragments (`/tasks/{id}/findings`, `/tasks/{id}/blockers`, `/tasks/{id}/minigraph`, `/knowledge/graph/panel`, the write POST endpoints). Cross-check against the Jinja templates under `src/lithos_lens/templates/` (fragments vs. full pages). **Settings** in the nav is still a disabled placeholder — say so.

Pick a concrete `task_id` to document by reading it off the live dashboard (the task rows link to `/tasks/<id>`). Prefer a task that has findings, so the detail page's finding timeline is non-empty. For the note renderer, follow a finding's note link if one exists; otherwise pick any knowledge document id and open `/note/<id>` directly.

## Step 3 — Capture and document each view

Drive the running UI with a browser-automation MCP (e.g. `claude-in-chrome` or `chrome-devtools-mcp`). For each view:

- Navigate to `http://localhost:8000/<route>`.
- Set the viewport to **1280×800** before capturing.
- Take a **full-page PNG** screenshot.

Scope every task view to **one project with few open tasks and `status=open`** (an unfiltered board on a busy Lithos is tens of thousands of pixels tall) and clip long pages to their first ~1,800 px. **Choose screenshots that show project/engineering content only** — never personal notes, private agent facts or planning notes; the manual is published in a public repo. Set an operator identity for the write-affordance captures by adding a `lens_operator` cookie (the `/operator` form POST is rejected without a browser `Origin` header); this does not write to Lithos.

Capture, at minimum:

- `dashboard.png` — the Tasks dashboard scoped to a project, open only.
- `dashboard-filters.png` — the same board with a different filter applied (status/resolved-since), clipped to cards + filter bar + strips.
- `task-graph.png` — `/tasks/graph` for a project whose graph is readable (a ghost node and one blocking edge is ideal).
- `task-detail.png` — a completed task with a dependency and findings, operator set (shows **Reopen**).
- `findings.png` — the `section#findings` element of that detail page.
- `task-new.png` — `/tasks/new` with an operator set (do **not** submit).
- `operator.png` — `/operator`.
- `knowledge.png` — `/knowledge?q=<project query>` (search results, not the recent-notes list), clipped.
- `knowledge-tags.png` — `/knowledge/tags?prefix=project`, clipped.
- `note.png` — a project note page.
- `knowledge-graph.png` — `/knowledge/graph?namespace=<project namespace>&selected=<note id>` (canvas + node panel), clipped.
- `knowledge-graph-full.png` — the same graph after clicking **Full page**, viewport only.
- `degraded.png` — the dashboard of a second Lens instance started with a Lithos URL that does not answer (`LENS_PORT=8011 LITHOS_LENS_CONFIG=<copy with url pointing at a dead port>`).

Save each to `docs/user-manual/screenshots/<name>.png` (lowercase, hyphenated, descriptive). A Playwright script over `e2e/node_modules/playwright` (viewport 1280×800, `fullPage: true`, `clip` for long pages) is the reliable way to get full-page captures; a browser-automation MCP only captures the viewport. Reading never changes Lithos state; the write affordances are documented from their forms and confirmation pages only.

## Step 4 — Write `manual.md`

Write `docs/user-manual/manual.md` from scratch, following the bundled template exactly.

Top of `manual.md` must contain:

1. `# Lithos Lens User Manual`
2. A one-paragraph intro: what Lens is for (a read-only window onto Lithos coordination state) plus the "do not edit by hand — regenerated by `/regen-manual`" line.
3. A Contents list linking to each `##` section.

Then, in order: **Getting around**, **The Tasks dashboard** (summary cards, epics/projects strips, the sections and their promotion rules, filters held in the URL, live updates), **The dependency graph**, **Task detail**, **Finding timeline**, **Acting as an operator** (identity, what it is and is not, each write affordance and its confirmation), **Knowledge** (search, facets, tags), **Knowledge notes**, **The knowledge graph** (scope picker, toolbar, node and edge panels, expansion, text view, full page), and **When Lithos is unavailable**.

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
