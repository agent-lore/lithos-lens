---
title: K1 — Knowledge Note View + Search
milestone: K1
status: draft
target_version: 0.3.0
references:
  - docs/ROADMAP.md (milestone sequence, upstream dependency ledger)
  - docs/REQUIREMENTS.md Part C (Knowledge Browser — Note View, Search)
  - lithos docs/SPECIFICATION.md §5.1 (read/search), §5.2 (tags/related), §3.2 (note model)
tracked_in: lithos
task_tags: [project:lithos-lens, milestone:k1]
labels: [milestone-k1, knowledge-browser]
depends_on: []
---

# K1 — Knowledge Note View + Search

## Problem Statement

Lens's second role — knowledge browser — barely exists. The corpus holds
~2,900 notes with rich frontmatter (note_type, status, confidence, entities,
provenance), wiki-links, typed edges, and multi-mode search, and the only
Lens surface is `/note/{knowledge_id}`: raw markdown in a `<pre>` block,
reachable solely by clicking a finding link from a task. Concretely:

- Note bodies render as unformatted text. Wiki-links — the corpus's primary
  navigation structure — are dead strings.
- There is no way to *find* a note: no search box, no browsing surface. The
  "Knowledge" nav item has been a disabled placeholder since 0.1.0.
- None of the note's frontmatter (type, status, confidence, tags,
  provenance, supersedes) is displayed, so a quarantined hypothesis looks
  identical to a high-confidence shared observation.
- `lithos_related` — links, back-links, provenance, and typed edges in one
  call — is unconsumed; a note gives no way to discover its neighborhood.
- The task→knowledge stitching is one-directional: findings link to notes,
  but a note produced by a task doesn't link back.

K1 is the thin slice: make notes real documents and make the corpus
searchable. The knowledge *graph view* is K2; cognitive search
(`lithos_retrieve`) is K3; feed/feedback/cited-by are K4.

## Solution

Rebuild `/note/{knowledge_id}` as a document page — rendered markdown with
clickable wiki-links, frontmatter chips, and a related panel — and add a
`/knowledge` landing page with hybrid search and recently-updated browsing.
Enable the Knowledge nav item; put a search box in the nav on every page.

Wiki-link resolution is the one hard problem: no MCP response maps an inline
`[[target]]` to a note id (upstream ask #8 in the ROADMAP ledger). K1 ships a
Lens-side resolver route instead — every wiki-link is rendered as a link to
`GET /knowledge/resolve?target=…&from=…`, which resolves server-side
per-click and redirects, disambiguates, or reports an unresolved link.

All read-only; no new JS beyond existing HTMX patterns.

## User Stories

1. As a reader, I want note bodies rendered as HTML (headings, lists,
   tables, code blocks), so that notes read as documents instead of raw
   markdown.
2. As a reader, I want agent-authored note content to be safe by default —
   raw HTML escaped, `javascript:`/`data:` link schemes neutralized — so
   that a hostile or sloppy note can't script the browser.
3. As a reader, I want every `[[wiki-link]]` rendered as a clickable link,
   so that I can follow the corpus's own navigation structure.
4. As a reader clicking a wiki-link with several plausible targets, I want a
   disambiguation page listing the candidates, so that ambiguity is a choice
   rather than a wrong redirect.
5. As a reader clicking a wiki-link with no resolvable target, I want an
   "unresolved link" page with a pre-filled search link, so that a dangling
   link becomes a search instead of a dead end.
6. As a reader, I want metadata chips on every note — note_type,
   status (color-coded active/archived/quarantined), confidence, access
   scope when not shared, namespace, tags — so that I can judge a note's
   standing at a glance.
7. As a reader, I want `summaries.short` rendered as a lede when present, so
   that enriched notes summarize themselves.
8. As a reader, I want a related panel — outgoing links, back-links,
   provenance (sources / derived / unresolved), and typed edges with type
   and weight — so that one call's worth of neighborhood is visible without
   a graph view.
9. As a reader, I want edge and link endpoints shown by title (not bare
   UUIDs), so that the related panel is scannable.
10. As a reader, I want a "Produced by task" chip linking to `/tasks/{id}`
    when the note's `source` metadata names a real task, so that
    note→task provenance works in both directions.
11. As a reader, I want a `supersedes` link when present, so that I can walk
    conflict-resolution history.
12. As a reader, I want note tags to link to `/knowledge?tag=…`, so that
    tags are a browsing surface.
13. As an operator, I want a search box in the nav on every page, so that
    knowledge search is reachable from the tasks surface.
14. As an operator, I want `/knowledge?q=…` to render hybrid-search result
    cards (title, snippet, updated, link), so that finding a note takes one
    query.
15. As a reader, I want search snippets rendered as escaped text, so that
    raw markdown or markup in a snippet can't break the results page.
16. As an operator, I want `/knowledge` without a query to show recently
    updated notes, so that the landing page is a browsing surface, not an
    empty search form.
17. As an operator, I want the Knowledge nav item enabled and active-state
    aware, so that both Lens roles are first-class in the chrome.
18. As an operator, I want per-section degradation (a failed related-panel
    call renders an inline error while the body still renders), so that one
    slow backend never blanks the page.

## Implementation Decisions

### Markdown rendering: markdown-it-py

`markdown-it-py` (new dependency, `markdown-it-py>=3`), configured
`MarkdownIt("commonmark").enable("table").enable("strikethrough")`. Chosen
over `mistune` and `python-markdown` because it is safe by default for
agent-authored content: the commonmark preset escapes raw HTML, and the
built-in `validateLink` rejects `javascript:`/`vbscript:`/`file:`/`data:`
hrefs — no sanitizer dependency (`bleach` is EOL; `nh3` adds a Rust wheel).
Rendering is server-side; output drops into the existing Jinja template —
the no-build-step identity is untouched.

### Wiki-link resolution

Verified fact: `lithos_read` returns `links: [{target, display}]` —
unresolved strings; `lithos_related` returns resolved link *ids* without the
original target text. No response maps inline `[[target]]` → id (ledger ask
#8: add `id | null` per entry).

**Rendering**: parse with `md.parse()`, walk inline **text tokens only**,
split on `\[\[([^\]|]+)(?:\|([^\]]+))?\]\]`, splice in link tokens. Never
regex the raw markdown — code fences and inline code are separate token
types and must stay untouched.

**Resolution** (per-click, in `GET /knowledge/resolve?target=…&from=…`):

1. UUID-shaped target → 302 to `/note/{target}`.
2. `lithos_read(path=target + ".md")` probe → on success 302 to
   `/note/{id}` (covers the dominant `[[folder/note]]` path convention).
3. Cross-check the source note's `lithos_related(from, include=["links"])`
   outgoing set plus `lithos_list(title_contains=<last path component>)`:
   one confident candidate → 302; several → disambiguation page; none →
   unresolved page with a `/knowledge?q=` link.

Per-click resolution means Lens never re-implements (and drifts from)
Lithos's internal resolution precedence for the ambiguous cases. When ask #8
lands upstream, inline links become direct `/note/{id}` hrefs at render time
and unresolved links get distinct styling; the resolver route remains for
old rendered pages.

### Note page composition

New Foundation module **`knowledge.py`** mirroring `tasks.py` (frozen
dataclasses, pure normalizers, a `KnowledgeLithosClientProtocol`,
`load_note_detail()` orchestrator with per-section state). Must be
registered in both the import-linter contract (`pyproject.toml`) and
`docs/architecture.toml` — the guardrail tests enforce both.

Data: `lithos_read(id)` (verified: full frontmatter arrives in `metadata`
even under `max_length` truncation) + one `lithos_related(id, depth=1)`.
Endpoint titles for links/edges resolve via a per-request-cached
`lithos_read(id, max_length=1)` fan-out, capped at
`related_title_fanout_cap` (default 20); beyond the cap, bare ids render
with a "+N more" note. "Produced by task": when `metadata.source` is set,
validate via `lithos_task_get` and render the chip only on success
(`task_record` notes get a distinct chip style).

### Search and landing page

- Nav search box (`base.html`): plain GET form → `/knowledge?q=…`.
- `/knowledge` with `q`: `lithos_search(query, mode="hybrid",
  limit=search_limit)` result cards — title, **escaped** snippet (verified:
  snippets contain raw markdown), updated_at, link. With `tag`:
  passed as a search filter / `lithos_list(tags=[tag])` when no query.
- `/knowledge` without `q`: "Recently updated" via
  `lithos_list(limit=recent_limit)` (verified: items carry `path`, `updated`,
  `tags`).
- Filters in K1: `q` and `tag` only.

### Config

```toml
[lithos-lens.knowledge]
search_limit = 20
recent_limit = 20
related_title_fanout_cap = 20
```

### MCP dependencies

New client methods (+ protocol + fakes): `search_notes` (`lithos_search`),
`related` (`lithos_related`), `list_notes` (`lithos_list`). Already wired:
`lithos_read`. The produced-by-task chip also needs the `lithos_task_get`
client method (a trivial one-call wrapper first introduced by T1); K1 does
**not** depend on T1 landing first — if `lithos_task_get` is not yet present,
K1 adds it, and the chip degrades to hidden when the method is unavailable
(see slice 5). No SSE changes — the note page is request/response in K1
(knowledge event wiring is K2).

### Telemetry

`lens.knowledge.note` (render, related-panel timing, fanout size),
`lens.knowledge.search` (mode, result count), `lens.knowledge.resolve`
(outcome: uuid | path | disambiguated | unresolved).

## Testing Decisions

- **Wiki-link tokenizer (pure, table-driven)**: `[[target]]`,
  `[[target|display]]`, links inside code fences and inline code untouched,
  nested brackets, multiple links per paragraph.
- **XSS posture**: raw `<script>` in a note body renders escaped;
  `[javascript:…](…)` and `data:` hrefs neutralized; snippet with markup
  renders escaped on the results page.
- **Resolver decision table** (fake client): UUID target redirects;
  path-probe hit redirects; single title candidate redirects; multiple →
  disambiguation page listing all; zero → unresolved page with search link.
- **Note page rendering** (TestClient + fake, pattern of
  `test_tasks_mvp.py`): metadata chips for each frontmatter field; lede;
  related panel sections; back-links; bare-id fallback past the fanout cap;
  produced-by chip only when `lithos_task_get` succeeds; per-section error
  states.
- **Search page**: cards render from fake results; empty-query recent list;
  tag filter round-trips; empty-result state.
- **Nav**: Knowledge item enabled, active on `/knowledge` and `/note/*`;
  search box present on tasks pages.

Coverage ≥ 80% on `knowledge.py` and the wiki-link tokenizer.

## Tracer-bullet vertical slices

1. **Markdown rendering.** markdown-it-py dependency; `knowledge.py` module
   registered in import-linter + architecture.toml; `/note/{id}` renders
   HTML body. Acceptance: headings/tables render; `<script>` is escaped;
   `javascript:` href neutralized.
2. **Wiki-link tokenizer + resolver route.** Token-splice rendering; the
   three-step resolver; disambiguation + unresolved pages. Acceptance: the
   decision table passes; links inside code fences stay literal.
3. **Metadata chips + lede.** note_type/status/confidence/scope/namespace/
   tags chips; `summaries.short`; `supersedes` link. Acceptance: a
   quarantined note renders visibly quarantined; tags link to
   `/knowledge?tag=`.
4. **Related panel.** One `lithos_related` call; four sections; title
   resolution with cap. Acceptance: back-links section lists incoming link
   titles; 25 edges with cap 20 renders "+5 more".
5. **Produced-by-task chip.** `metadata.source` → `lithos_task_get`
   validation (adding the `lithos_task_get` client method if T1 has not
   already landed it). Acceptance: chip links to the task; invalid source
   renders no chip; the chip is omitted entirely if `lithos_task_get` is
   unavailable.
6. **`/knowledge` search + recent.** Nav box, result cards with escaped
   snippets, recent list, `tag` filter. Acceptance: query renders cards;
   no query renders recent; snippet markup is escaped.
7. **Nav enablement + degraded states.** Knowledge nav live; per-section
   errors; Lithos-unreachable banner. Acceptance: related-panel failure
   still renders the body; nav shows active state.

Slice 1 is foundational; 2–5 depend on it; 6–7 are independent.

## Out of Scope

- **Knowledge graph view** (Cytoscape over note edges) — K2.
- **Knowledge SSE** (note.*/edge.upserted live updates) — K2.
- **`lithos_retrieve` cognitive search, salience/reasons UI, node stats** —
  K3. K1 deliberately uses plain `lithos_search`.
- **Feed view, pagination, feedback affordances** — K4.
- **"Cited by findings in tasks X, Y"** — K4, gated on upstream ask #9
  (`lithos_finding_list(knowledge_id=…)`); the O(all-tasks) scan workaround
  is explicitly rejected.
- **Any knowledge writes** (conflict resolution, note editing) — deferred
  pool.
- **Note comparison, reading paths** — deferred pool.

## Further Notes

- **Why per-click resolution**: resolving every wiki-link at render time
  would cost one probe per link per page view and would still guess on
  ambiguity. Per-click defers the cost to actual navigation and keeps Lens
  honest about ambiguity. Upstream ask #8 makes render-time resolution free;
  the design anticipates it.
- **Verified live facts** (2026-07-07): corpus ≈ 2,862 notes;
  `lithos_read(max_length=1)` returns complete frontmatter (cheap title
  fetches are safe); `lithos_read` responses carry no `path` field (the
  resolver's path probe uses the request side only); search snippets contain
  raw markdown.
- **`lithos_retrieve` and working memory**: even in K3, Lens must never pass
  `task_id` to retrieve — it writes agent working-memory rows. Stated here
  because K1's search plumbing is what K3 extends.
- **Relationship to T1**: K1 and T1 are on independent tracks and touch
  disjoint modules (`knowledge.py` vs `frontier.py`); `depends_on: []` is
  therefore accurate. The only shared primitive is the `lithos_task_get`
  client method (produced-by-task chip), added by whichever milestone lands
  first. The ROADMAP sequences T1 before K1 for focus, not because of a hard
  dependency.
- **Spec drift**: when K1 ships, `docs/SPECIFICATION.md` §5.7 (note view)
  must be rewritten and the user manual regenerated.
