# Lithos Lens - Specification

Version: 0.5.0  
Date: 2026-10-05  
Status: Aligned with Implementation (T1, K1, T2 and T3 shipped)

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
  (`tasks.RESERVED_TASK_PATH_SEGMENTS`: `graph`, `events`, `new`, and the
  alias's own `id`). Starlette matches the static route first, so without it a task called
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
- `GET /tasks/{task_id}/minigraph`
  Renders the detail page's **mini-graph** (§5.6.2) — the embedded payload and
  the canvas container the shared graph client draws into, plus the fragment's
  own text: the legend, the focus link into the full project graph, and the
  tail counting whatever the cap left out. An HTMX fragment on the same terms
  as the two above, fetched after the page rather than assembled inside it, so
  the text baseline never waits on the picture drawn above it.
- `GET /tasks/graph`
  Renders the dependency graph of one scope — `?project=<slug>` or
  `?epic=<id>` — and, with no scope, a picker of the projects and open epics
  the snapshot observes. Registered BEFORE `/tasks/{task_id}`, which would
  otherwise match `graph` as a task id.
- `GET /operator`, `POST /operator`
  Shows the operator identity writes are attributed to, where it came from
  (the `lens_operator` cookie, or `[writes].default_operator`), and the
  trusted-network boundary statement §5C.1 requires. The POST sets or switches
  the identity — a cookie, one year, `HttpOnly`, `SameSite=Lax` and
  deliberately **not** `Secure` (Lens serves plain HTTP, where a Secure cookie
  would not be stored at all; the cookie is an attribution label, not a
  credential). `?next=` returns the operator where they were and is honoured
  only as a same-origin relative path — on the redirect and on a refusal
  re-render alike, so correcting a refused id still returns the operator where
  they came from. An id the impersonation guard refuses, or one that is not a
  valid operator id, re-renders the form with the reason and sets no cookie
  (HTTP 400). The **submitted value itself** must match the id rule: it is not
  trimmed first, because accepting `" dave "` would set an identity other than
  the one typed.
- `POST /tasks/{task_id}/approve`
  Completes an open **gate** through the write funnel (§5.15). The path keeps
  REQUIREMENTS §5C.7's name; the action the operator sees says **Complete**,
  never "Approve". A gate a person resolves — `human` or `external_task` —
  completes directly. Any other gate type (`timer`, `ci`, `pr`, or one Lens
  does not know) completes only when the form carries `confirm=proceed-anyway`,
  which only the confirm page below posts; without it the POST is redirected
  to that page (303, or `HX-Redirect` for HTMX) with `next` carried along, and
  nothing is written. A task that is not a gate is answered 409. A plain form
  POST answers `303 See Other` to the form's `next` when it is a same-origin
  relative path, else the task's detail page, carrying `?receipt=<id>`; an HTMX
  POST (a board row's action) answers a fragment, always 200.
- `GET /tasks/{task_id}/approve`
  The **Proceed anyway** confirm page for an open machine-owned gate (§5.15),
  server-rendered and complete without JavaScript. `?next=` (same-origin
  relative only, else the gate's page) is where its form and its "Keep
  waiting" link return the operator. A `human` or `external_task` gate has
  nothing to confirm and is redirected (303) to its detail page, where the
  direct action is; a task that is not a gate, or a gate that is no longer
  open, answers 409 saying why, an unknown id 404, and a failed read 503 — each
  with no form.
- `POST /tasks/{task_id}/reopen`
  Reopens a **completed or cancelled** task of any type through the write
  funnel (§5.15, Reopen). No confirm page and no `GET`: the case's copy is
  rendered beside the button. Answered like the approve POST — 303 to `next`
  (same-origin relative, else the task's page) with `?receipt=<id>`. An
  already-open task is the conflict page on both paths: a stale
  `expected_status` caught by the pre-check ("This task is now open.", no
  Lithos call), or Lithos's `task_not_resolved` for a task reopened in the
  window after it ("This task is already open.").
- `GET /tasks/{task_id}/cancel`
  The **Cancel** confirm page for an open task of any type (§5.15, Cancel),
  server-rendered and complete without JavaScript, and a read in either
  `[writes].confirm_cancel` mode. `?next=` (same-origin relative only, else
  the task's page) is where its form and its "Keep it" link return the
  operator. A task that is no longer open is the conflict page ("This task is
  now *\<status\>*.", 409) stating no consequence; an unknown id is the
  conflict page's "This task no longer exists." (404); a failed read is 503.
- `POST /tasks/{task_id}/cancel`
  Cancels an open task through the write funnel: one `lithos_task_cancel` as
  the operator, with the optional `reason` folded to one line and bounded at
  500 characters. With `confirm_cancel = true` it is performed only when the
  form carries `confirm=cancel`, which only the confirm page posts; without it
  the POST is redirected (303) to that page with `next` carried along, and
  nothing is written. Answered like the approve POST — 303 to `next` with
  `?receipt=<id>` — and always a plain form, never HTMX.
- `GET /tasks/new`
  The **create form** for a task, an epic or a gate (§5.15, Create). `new` is a
  reserved task-path segment: the path is this form, never the detail page of
  a task whose id happens to be `new`, which is reachable through
  `/tasks/id?task_id=new` like every other page word. `?project=` and
  `?parent=` pre-fill it. With no operator identity it renders only the
  "choose an operator" link and no form.
- `POST /tasks/new`
  Creates one task, epic or gate through the write funnel's task-less entry
  point (§5.15, Create), de-duplicated on the form's request id. Success is
  303 to the new task's page with `?receipt=<id>`; a refusal re-renders the
  form with the input kept (422, or the funnel refusal's own status); an
  unknown outcome is the "not visible yet" page (200). With `intent=restart`
  (**Start again**) it makes no call and re-renders the form with the input
  kept under a new request id. Plain form only, never HTMX. The form carries
  **no `expected_status`**: §5C.6's stale-status pre-check is about forms on an
  existing task, and there is no task yet.
- `GET /tasks/{task_id}/edges/new`
  The **relation confirm step** for an open task (§5.15, Add a dependency): a
  read. `?relation=` names the sentence (`blocked_by`, `blocks`, or — on a
  gate only — `waited_on_by`), `?other=` the other task (full id or a prefix
  of at least six characters) and `?next=` where the write returns the
  operator. A sentence the page does not offer, or no other task, re-renders
  the entry form (400); an other task Lithos cannot resolve re-renders it with
  the message — or an ambiguous prefix's candidates — under the input (422),
  and naming the task itself is refused there as `self_edge` (422). Every
  such re-render carries the §5.14 notice above the form, "Nothing was
  changed." first, with the input kept.
  A task that is no longer open is the conflict page (409); an unknown id is
  "This task no longer exists." (404); a failed read is 503.
- `POST /tasks/{task_id}/edges`
  Adds one dependency edge through the write funnel: `from_task_id`,
  `to_task_id`, `type` and `expected_status`, as the confirm step's form posts
  them. Answered like the approve POST — 303 to `next` with `?receipt=<id>`
  — and always a plain form, never HTMX.
- `GET /knowledge`
  Renders the knowledge landing page: hybrid search, or "Your notes" then
  "Recent intake", with tag (`?tag=`) and namespace (`?namespace=`) filters.
- `GET /knowledge/resolve`
  Resolves a wiki-link target to a note, or renders the disambiguation /
  not-found page when it cannot.
- `GET /note/{knowledge_id}`
  Renders a note: server-side markdown, frontmatter metadata chips, the
  related panel, and provenance.

Every page that extends the base layout — the board, task detail, the graph,
knowledge, a note, the operator page — accepts `?receipt=<id>` and renders that
write's receipt above its content, once (§5.15). An unknown, expired or
already-shown id renders nothing and is not an error.

One further route, `POST /tasks/events/publish`, is registered **only** when
fake-Lithos app mode is enabled (`LITHOS_LENS_FAKE_LITHOS`). It is a harness
seam for the browser suite and does not exist in a normal deployment.

No authenticated routes currently exist, and none are planned: Lens takes
unauthenticated requests across a trusted-network boundary (`docs/REQUIREMENTS.md`
§5C.1). **Anyone who can reach the Lens port can perform any action it offers**
— which, since the first curated write (§5.15), includes changing Lithos: any
such client can complete a human or external-task gate, under any operator name
it chooses. The operator page states that where the operator meets it. Two
process-level bounds exist in place of authentication: a concurrent-render cap
that answers 503 rather than queueing, and a ceiling on concurrent SSE
subscribers.

Two request-level checks ride alongside them on the write surface, and both are
**hygiene, not security**:

- **Origin check.** Every POST Lens registers under the curated write actions
  requires an `Origin` header whose host *and port* match the request's `Host`
  — falling back to `Referer` only when no `Origin` header is sent at all, with
  each side's missing port implied from its scheme, compared
  case-insensitively. A mismatch, an `Origin: null`, a present but empty or
  otherwise unparseable value, or the absence of both headers
  is answered 403 before any Lithos call. It stops a page open in another tab
  driving Lens with the operator's browser; it stops nothing else.
- **Operator attribution.** Writes name a human operator rather than the Lens
  service agent (§5.13), so an audit trail can tell "Lens the process" from
  "the person driving it". It is a label the browser supplies, not a proof of
  who sent the request.

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
- `tasks.description_preview_chars`
- `tasks.default_status_groups`
- `tasks.project_convention` *(deprecated: parsed and ignored, REQUIREMENTS §4.4)*
- `tasks.project_tag_key`
- `tasks.gate_waiting_attention_hours`
- `tasks.claim_expiring_soon_minutes`
- `tasks.stale_open_age_days`
- `tasks.unclaimed_ready_age_minutes`
- `tasks.agent_inactive_days`
- `tasks.dispatch_trigger_tag_prefixes`
- `graph.cache_ttl_s`
- `graph.max_tasks`
- `graph.fetch_concurrency`
- `graph.mini_graph_max_nodes`
- `writes.default_operator` *(operator identity used when a browser has no
  `lens_operator` cookie; validated at load against the same rule as the
  cookie, so a value that is not a lowercase slug fails the load)*
- `writes.confirm_cancel` *(default `true`: Cancel opens its consequence confirm
  page; `false`: the affordance posts directly and the consequences are stated
  on the receipt instead — §5.15, Cancel)*
- `knowledge.related_title_fanout_cap`
- `knowledge.search_limit`
- `knowledge.recent_limit`
- `knowledge.list_chip_fanout_cap` *(default 40, 1-200: how many landing rows
  get metadata chips, one `lithos_read` each — §5.7)*
- `knowledge.intake_path_prefixes` *(default `["articles/", "papers/",
  "digests/"]`: a note under one is intake on the landing, as is one tagged
  `ingested-by:*` — §5.7; `[]` leaves only the tag)*
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

Defaults and ceilings are defined in `src/lithos_lens/config_schema.py`, the
TOML parsing in `src/lithos_lens/config.py`, and the env-override pass in
`src/lithos_lens/config_env.py` (every override name is a string literal there,
which the docs↔code guardrail scans by AST); the shipped
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

**Every task is named by its title and its short id.** The short id is the first
8 characters — the prefix loom's gate and log lines, the findings, the ROADMAP
and the PR bodies all type, and the one Lithos resolves back (any unambiguous
prefix of 6 or more) — so a row can be related to `28105098` at a glance. The id
is metadata, so it renders **first in the surface's metadata group**: the
right-hand meta column of a row here and in Gates, the detail page's and the
side panel's meta line (§5.6), a graph layer entry's badges (§5.12). Where a
surface has no such group it sits with that surface's other facts about the task
— a leading column in the children table, after the status on a blocker /
Blocks / provenance line or on a gate's waiter, after the link in a breadcrumb
trail or in the sentence a scoped-epic banner states. A row that names a SECOND
task states that task's id too, and not only its own: a blocker chip carries the
predecessor's id beside its title, and the `unsatisfiable`/`cycle` supporting
fact states it in the same element, spliced into the sentence right after the
name — `Blocker "Design schema" 28105098 was cancelled …`. The reason carries
the predecessor as an ID, not as prose, so that one element is the same one
every other surface renders and no sentence is marked safe HTML to get there. A
name that already IS an id (the predecessor is not in the snapshot) adds
nothing. It is selectable monospace
text carrying the whole id in its tooltip, so select-and-copy yields exactly the
prefix everything else uses, and the title itself is never rewritten.

Two kinds of surface are deliberately exempt, and only these:

- the **Epic rollup strip's** progress chips, which have no room for another
  visible token: the chip's `title` carries the epic's id — the WHOLE id, since
  a tooltip is the only identity fallback a chip with no visible id has;
- the **Cytoscape canvas** (§5.12.1), whose node labels stay the title alone —
  an extra token on every node crowds the picture, and a click opens the side
  panel, which leads with the id.

A **path expression** — the cycle callout's `A → B → A` walk, the longest
blocking chain's node list (§5.12) — is not an exemption: every step of it
names a task, so every step states that task's id. The chain sentence is
rewritten client-side on every focus transition, so the client rebuilds it as
markup rather than as text and the ids come back with it; a line that came back
without them would contradict the markup the page shipped with.

**A task's description is Markdown, everywhere Lens shows one.** Agent-written
descriptions (loom's, and the tasks the operator files through Claude Code) are
Markdown — a lead-in paragraph, bold labels, numbered lists, code spans,
acceptance bullets — so every surface that shows one renders it through the same
safe renderer the note page uses (raw HTML escaped, the §6.2 link-scheme
allow-list, escaped plaintext if the parse raises), inside a `.markdown-body`
container. Two differences from a note body, both deliberate: single newlines
are kept as line breaks, because a description is as often hand-typed lines as
it is a document; and `[[wiki-link]]` stays literal text, because it is a note
concept resolved against a source note's own outgoing links and a task has none.

**A long description is truncated on the board, never on the detail page.** The
row shows as much as `tasks.description_preview_chars` allows (default 600; `0`
never truncates) and links on with `… see more` — an ordinary anchor to the
task's detail page, so the no-JavaScript path is plain navigation. The cut is
made on the Markdown *source* at a **block boundary**: whole top-level blocks —
paragraphs, list blocks, fenced code, tables, headings — are taken while the
running length fits the budget, so a preview never ends inside a list, a fence
or a table, and the first block is always shown even when it alone exceeds the
budget. The preview is a genuine **prefix of the source**, not a rejoining of
the blocks: the budget therefore counts the separators the author wrote, and
what sits between blocks — a CommonMark reference definition (`[id]: …`) emits
no block of its own — travels with them. A definition the cut leaves *behind*
is carried too, as link context for the preview's own render, so truncating
never turns a rendered link back into literal markup.

`tasks.js` upgrades the anchor into an in-place expander (the full
rendered body rides along in the row, hidden) offering `see less`. The Gates
rows and the side panel render the same partial and so truncate identically;
the detail page passes no budget, because it is the canonical full view that
`see more` leads to — the rule the note page already follows.

**Every row states its project**, and §5B.1 says which value that is where a
single one is needed: `metadata.project` when present, else the
`<project_tag_key>:<slug>` tag. So the row's chip strip opens with
`row_project_chips` — the metadata reading of the task, the value that WINS —
and continues with `row_tag_chips`, every tag except the one that would say
that same project a second time. Both are bound to the configured tag key
where the template environment is wired, as is the CLASS a TASK tag chip wears
(`task_tag_chip_class`, which answers the same question — is this tag a
project? — and must not answer it from a different key). No template decides
between the two conventions, and the row's reading is the one the side panel's
chip, the quick-switch strip and `?project=` itself make. Four consequences,
each of which the tags-only row got wrong:

- a row carrying only `metadata.project` — loom's issue-mirrored work, which
  `?project=` has matched since `project_convention` was retired as a
  membership knob — names the project it is filtered by, instead of rendering
  no project chip at all;
- a row whose two conventions DISAGREE leads with the metadata value rather
  than stating only the tag value that lost, and keeps the losing value's tag
  chip behind it: §5B.1 orders the two, it drops neither (the disagreement is
  separately reported as `lens.tasks.project_convention_conflict`);
- a row belonging to SEVERAL projects (§5B.8) still leads with the metadata
  one. The chip never stands down because some tag happens to spell the same
  slug — upstream tag order is not precedence, and a second project tag would
  otherwise stand in front of the winner. Only the duplicate tag is dropped,
  matched on its parsed slug, so each project is chipped exactly once;
- a row with no `metadata.project` renders exactly the strip it always had, its
  own project tag chip — project-styled under the configured key, linking to
  that tag's board — included.

The metadata chip is not a link: ADDING a project to the query is the
quick-switch strip's move and carries that strip's filter-budget rules with it,
while this chip's job is to say which project the row is in. The Gates
section's rows render the same strip and take the same chip. A row with no
project under either convention renders none — REQUIREMENTS §5.4.1's
`(no project)` placeholder is said out loud by the side panel (§5.6.1) and is
not yet rendered on the board's rows.

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
  each other by age). An open `human` or `external_task` gate's row carries
  the **Complete** action (§5.15) — in the Gates section and, through the same
  partial, where Needs attention promoted it. The badge renders only when `reconciliation_pr_url` and
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
- a **Project quick-switch strip** below it, enumerating the projects inside the
  board's current scope so switching between them does not mean retyping the
  Project box. A tag like `roadmap-2026-09` spans a handful of projects, and the
  set is already derivable from the snapshot — `task_projects` under
  `convention="both"`, §5B.1's universe rule (the union of both conventions, so
  no project is invisible to its own view), which is the same call the Project
  datalist and the graph scope picker make, so a project carried only in
  `metadata.project` counts like a tagged one — the strip and the datalist
  beside it state the same universe, so the two controls never disagree about
  which projects exist. (That universe is also what `?project=` MATCHES:
  `matches_projects` reads both conventions whatever the config says, so no
  offering control can name a project the filter refuses. It could, until
  `project_convention` was retired as a membership knob — parsed and ignored,
  REQUIREMENTS §4.4.) Each
  chip carries the project's **open-row count** within that scope — the open
  sections plus Gates; terminal rows contribute nothing — and the chips order by
  count then slug. The scope is **every active filter except `project`**, so the
  strip answers "which projects are in what I am looking at" and does not shrink
  as the operator clicks between them: clicking an unselected chip adds its slug
  to `?project=` (projects OR, the comma form), clicking a selected one removes
  just that slug, and a **Clear** affordance — shown whenever `project` is set —
  removes the parameter alone, leaving every other filter where it was — and
  the strip with its Clear stays on a board whose scope holds no project at
  all, because it is then the only way back. Every chip has open rows behind
  it, so none leads to an empty board (the same rule §5.2.1 gives the epic
  strip, evaluated under the same generation). The strip is hidden when the
  scope holds fewer than two projects and no project filter is active.
  Generated project links carry the board's filter state and nothing else (the
  `request_filters` allowlist, plus `all_agents`): the panel selection, an
  expansion `chain`, a retired filter and any unrecognised key stay behind.
  Adding a project is the only one of the three that makes the query longer, so
  it is the only one bounded by `MAX_FILTER_QUERY_BYTES` (§5.4): on a board
  whose other filters already fill that budget the chip is drawn with its slug
  and its count but **without a link**, rather than with one the router would
  refuse — the bytes would have to come out of a filter the board was asked
  for. Removing a project and clearing the filter only shrink the query, so
  they are offered on any board that renders the strip. The same treatment —
  slug and count, no link, the reason in the chip's title — goes to a slug the
  filter cannot carry in any spelling: one containing a comma, which
  `?project=` reads as its separator.

  Each of the three strips — active filters, epics, projects — **opens with a
  visible label** (`Scoped to`, `Epics`, `Projects`) that is also the section's
  accessible name (`aria-labelledby`). They stack with nothing else between
  them and their chips share one pill language, so the label is the only thing
  that says which strip a chip belongs to; and the epic strip can render as
  its "N epics have no tasks on this board" note alone (a project-filtered
  board with no open epic of its own), which without the labels reads as a
  caption for the project chips beneath it — every project in the corpus,
  mistaken for epics that leaked through the filter.
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
  Free-text tag filter. One `tag` parameter is one LITERAL tag (a comma is tag
  content, not a separator, and `""` is a real tag), so the active tags ride as
  hidden inputs and are removed from the chip strip; the box that ADDS one is a
  separate `add_tag` parameter, because a blank `tag` would otherwise be
  indistinguishable from the empty-tag filter. The box autocompletes from the
  `tags` datalist (below).
- `project`
  Project scope. A row matches when it carries the slug under EITHER §5B.1
  convention — `metadata.project` or the reserved `<project_tag_key>:<slug>`
  tag — which is the same union the `projects` datalist, the quick-switch strip
  and the graph scope picker are built from, so every offered value leads to
  its own rows. Multi-select and OR: `?project=a,b` (or repeated `project`
  pairs) shows either project's rows. The quick-switch strip (§5.3) is the
  fast way to set it, and the Project box still accepts anything typed — the
  strip reflects whatever the filter holds, however it got there.
- `epic`
  Scopes the board to one epic's children, from the rollup strip.
- `agent`
  Creator-OR-claimer agent filter; the value is an agent id (or any string the
  operator types), matched unchanged.
- `all_agents`
  Not a row filter: it widens the Agent PICKER to every registration (below).
- `since`
  **Resolved** lower bound — terminal rows only, by `resolved_at`.
- `created_since`
  **Created** lower bound — every section, open rows included, by `created_at`.

Filter behavior:

- Filters are parsed by Lens and also applied defensively inside Lens after data
  is fetched from Lithos.
- Both date filters accept ISO `YYYY-MM-DD` and UI-friendly `DD/MM/YYYY` input,
  and the visible dashboard fields render `DD/MM/YYYY`.
- The two date windows are different questions, and each field's LABEL says
  which rows it windows rather than naming only its date:
  - **Created since (open + terminal, by creation)** — `created_since`. Applied
    client-side over the snapshot already loaded, so it changes no Lithos read;
    it narrows every section, and a row it hides is hidden from the section it
    belonged to rather than moved to another one. It is opt-in: absent means no
    created window, and input it cannot parse falls back to that same default
    rather than inventing a narrowing. A ROW's `created_at` is validated in
    full and normalized to UTC before its date is compared, so a stamp written
    in another offset falls on the side of the window its instant actually
    belongs to; a row whose stamp is missing or unreadable is dropped while the
    window is active — nothing else applies this filter, so a row Lens cannot
    evaluate has not been shown to satisfy it and must not be counted as though
    it had. (The resolved window keeps such a row, because there the server
    applied `resolved_since` and returned it anyway.)
  - **Resolved since (terminal only, by resolution)** — `since`. The one filter
    pushed upstream (`lithos_task_list`'s native `resolved_since`), so it also
    bounds what the completed/cancelled reads fetch; it never narrows open rows,
    which are the live frontier rather than a time window. It always has a
    value, defaulting to the configured lookback and clamped by
    `MAX_SINCE_LOOKBACK_DAYS`.
  - Both may be active together, and a terminal row must then pass each on its
    own date.
- The **filter-bar datalists** — `projects`, `tags` and `agents` — all answer
  "what can I narrow to from here?" over the rows THIS load fetched (the open
  snapshot plus the windowed terminal rows, deduped by id) and BEFORE the
  filters narrow anything, so selecting one value never collapses the list of
  values you can switch to. None of them costs a Lithos read of its own.
  - `projects` is the union of both conventions' slugs (§5B.1) — the same
    reading `?project=` matches on, so nothing offered here is a dead end.
  - `tags` is the sorted, deduped union of the loaded rows' tags, raw and
    unnormalized — the box has to offer exactly what it would submit, and
    upstream types a tag as a bare string, so whitespace and case are
    significant and the empty tag is carried like any other (what the BOX does
    with a blank value is the unchanged `tag`/`add_tag` split above). Because
    the universe precedes the filters, a tag that spans projects
    (`milestone:t2`, `needs-human`) is discoverable from a board scoped to a
    project that does not carry it, and so is one carried only by a row in the
    resolved window. A tag on a row the window did NOT return was never loaded
    and is not offered; the vocabulary is snapshot-scoped, not corpus-wide.
  - `agents` is the picker described next, which is ordered and windowed rather
    than rendered verbatim.
- The **Agent picker** (the `agents` datalist) is `lithos_agent_list` ordered
  and windowed, not verbatim. Lithos' agent list is a registration log — a row
  per probe, per session and per host, with no dedupe — so rendering it raw
  buried the live identities under the dead ones. Each option is labelled with
  when that registration was last active and the list is sorted by it, most
  recent first — one timestamp drives both, so the order is the order of the
  times shown. "Last active" is derived from data the board already loaded, in
  signal order: an inline claim the agent still holds, dated by the load that
  observed it (upstream gives a claim no start time, so the instant Lens saw it
  held is the honest stamp — and being the newest possible one, a live claim
  leads the list and the label says `claim held`); else the newest `created_at`
  of a task it created, over every row the load fetched (open snapshot and both
  resolved windows); else the registration's own `last_seen_at`, which the
  label calls out as a registration rather than work. No per-agent Lithos call
  is made for it, and findings are deliberately not consulted — they are not in
  the snapshot.
  - An agent with no activity inside `tasks.agent_inactive_days` (default 30)
    is omitted from the datalist: a `<datalist>` cannot render an option faded,
    so hiding is the behaviour. `?all_agents=1` includes them — a query
    parameter, so the choice survives a reload and works with no JavaScript,
    and the filter form re-emits it so applying a filter keeps it. It is not
    one of the preserved filter keys: it narrows no row, so it is not carried
    into every generated link and does not make a board count as filtered.
  - When two or more registrations share a `name`, every one of their labels
    also shows the agent id, so duplicates are visibly distinct.
  - The option VALUE is still the bare agent id, so the `agent` filter and what
    it matches are unchanged.
- An active `created_since` appears in the active-filter chip strip, and
  removing that chip drops only that window — `since` and every other filter
  stay applied.
- Clicking a task tag in list or detail view navigates back to `/tasks` with
  that tag as the only active tag filter.
- Existing `status`, `agent`, `since`, `created_since`, and `claimed_state`
  filters are preserved when clicking a tag, and carried across navigation into
  the detail and note views.
- TASK tags spelling a project under the configured `<project_tag_key>:`
  prefix (§5B.9) are rendered with distinct visual styling but are otherwise
  filtered the same way as other tags. The styling reads the CONFIGURED key,
  not a hard-coded `project:`, so it marks the tags that actually decide
  project membership in that deployment and no others. The Knowledge note
  page's tag chips are classed separately, by the literal `project:` prefix
  §5B.2 fixes for a project document: `[tasks].project_tag_key` spells the task
  convention, so renaming it must not restyle a note's tags — the two surfaces
  get two globals (`task_tag_chip_class`, `knowledge_tag_chip_class`) over the
  one keyed helper.
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
- **Description preview** — a board row shows at most
  `tasks.description_preview_chars` of a description, cut at a Markdown block
  boundary, and says so with a `see more` link to the full page. The detail
  page, which that link leads to, applies no such bound.
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

- task title and body/summary content — the description rendered as Markdown
  and never truncated (§5.3) — status metadata, creating agent, created
  timestamp, tags, and claim state where known. The meta line under the heading
  **leads with the task's short id** (§5.3), as the side panel's does and as
  every row on the board does; the parent breadcrumb above it states the id of
  each ANCESTOR it names, and not of its own last entry — that is this task,
  whose id is in the meta line two lines below
- **why this task is here** — the Needs-attention reasons, when the board
  promoted it, with the same supporting facts the chips carry
- **blockers**, each labelled: a satisfied predecessor (the edge survives
  completion and is still shown, but never as a reason the task cannot run), an
  unsatisfiable one, or a cycle
- **the mini-graph** (§5.6.2), above the chain: this task's neighbourhood —
  blockers two hops up, dependents one hop down, the parent epic — drawn by
  the same client module the graph page uses. Progressive enhancement over the
  chain below it, never a replacement for it
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

The page has one write action: an open `human` or `external_task` gate carries
**Complete** in its header (§5.15), beside the acting identity, when an
operator identity resolves. It is the same partial the board's gate row and the
side panel render; nothing else on the page writes.

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
- The panel states the task's **description** the way a row does — the same
  partial, the same Markdown rendering and the same preview budget (§5.3) — so
  reading a relationship without leaving the board does not mean losing the
  text that explains it.
- `GET /tasks/{task_id}?fragment=panel` answers with the partial and nothing
  else. A row click fetches it — from the URL the SERVER wrote onto the row, so
  the id encoding and the board's preserved filters have one definition — swaps
  it into the board and pushes `selected` onto the URL with `pushState`. The
  push happens after the swap, so a failed fetch never leaves the address bar
  claiming an open panel.
- **Every branch of the panel carries Close**, the degraded ones included: an
  offline answer, an unreadable task and an unknown id are all OPENED panels —
  the client swaps them in and pushes the selection behind them — and the swap
  removes whatever control the page loaded with. Escape is only half of that
  contract; a panel with no Close leaves a focused canvas nothing but the
  keyboard can clear.
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
  answer is empty: a task belonging to no project under either convention
  renders an explicit `(no project)` chip rather than nothing, so
  projectless work is distinguishable from a field that failed to render.
- **Every row on the board opens one**, the Gates section included: a gate is a
  task, and "what is this gate holding up?" is the Blocks list the panel
  already answers. A row's panel URL carries the board's preserved filters, so
  the panel's own links come back inside the scope the operator is browsing; a
  GRAPH-hosted one carries none of them, because that page's `project=` /
  `epic=` are scope selectors from a query vocabulary of its own rather than
  board filters — copying one in as a filter would hand the detail route a
  query it may refuse, and a refused fragment is an empty panel beside a node
  the canvas is still focusing. The contract a row opts into is `data-task-id` plus the
  server-built `data-panel-url` — not the `data-task-row` hook the live-event
  handlers use to rewrite claim and status chrome in place, which a gate row
  does not carry.
- **Expand** leaves for the full page. Every other link inside the panel is an
  ordinary link too; the click handler intercepts the row title and the row
  itself, never a tag chip or a link within the panel. Nor a `<summary>`: the
  gate row's waiter list is a `<details>` that expands with no JavaScript, and
  its disclosure control keeps that behaviour.

The panel states: the header (title, status, type badge with `gate_type`,
project chip under §5B.1's conventions, metadata first, creating agent), the parent
breadcrumb, the blockers with live status (level 1, no per-level expander —
the walk lives on the full page), **Blocks** (the level-1 dependents), the
**downstream impact** when it was given a scope (below), the
active claims, and the finding COUNT linked to the full timeline. The
**downstream impact** is stated only when the request named a `scope=`, and it
is two figures from two authorities (§5.7 of REQUIREMENTS):

- **N** is Lens's own walk — the open transitive dependents of this task over
  the scope's **active projection** of `blocks` + `waits_on_gate`, within the
  graph that scope fetched, downstream ghosts counted as the leaves they are —
  counted when reached and never walked THROUGH, because Lens read no edge list
  for one: an edge that appears to leave a ghost was reported by somebody
  else's list, and following it would count a task this graph does not reach
  from here.
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
future-tense number — and an **epic** states no FIGURES at all, since a zero
there would read as "finishing this frees nobody" rather than "this is not that
kind of task". Beside it, "on the longest chain (k of n)" gives the task's
position on the SCOPE's chain (§5.12) when it is on it; a task is trivially on
the chain through itself, so stating that would state nothing. And when the
scope is incomplete, or an `unknown` edge touches the focused task's own
neighbourhood, the panel says that what the canvas lights is a **lower bound**
of what surrounds it — a dimmed node must not read as "unrelated" when Lens
only failed to look. Both of those last two are claims about the CANVAS rather
than about N, so a focused **epic** carries them like any other node: it is on
the scope's longest chain whenever the chain holds it — a scope whose blocking
projection is one node wide puts it there — and its neighbourhood degrades the
same way. What an epic never carries is the future-tense clause: where a task
whose panel no longer matches the graph on screen reads "refresh to see what
completing this frees" (below), an epic says only that the graph has changed,
and withholds the canvas notes with the figures — they describe the assembly
this panel just made, which is not the one being drawn.

The graph page's own render computes this from the scope and cycle signal it
already holds; a panel fetched on its own rebuilds that scope, which the
per-task edge cache (§5.10) makes affordable because the graph the operator is
looking at is warm. A rebuild is not the same answer by default, though: the
scope NAME only fixes which tasks are asked for, and the canvas deliberately
does not re-lay-out under the operator (§5.12.1) — so every panel URL the graph
page emits carries `snapshot=`, a fingerprint of what that render answered.

It is **two digests**, joined, because they move independently and only one of
them is cached — and because a panel that finds them moved has two different
things to say:

- the **canvas** half: the node set (id, status, completeness, ghost kind) and
  the edge set (endpoints, type, state). This is the picture — what is drawn,
  what N is walked over, which nodes light under a focus, where the longest
  chain runs.
- the **answer** half: the project slugs coverage is matched by (kept apart by
  the convention that carries each, §5B.1, because coverage belongs to the read
  and a slug rewritten from `metadata.project` to a `project:<slug>` tag
  changes which read could have answered for that task), the coverage set, each
  read's outcome, and the blocked rows for those nodes — each row's blockers by
  kind, predecessor, type and status — which together decide M and whether it
  is stated at all.

The answer half is load-bearing: an edge upsert emits no event (§5.10) and a
warm edge cache can reproduce a byte-identical graph while Lithos's
sole-blocker row has gained a second blocker, moving M with nothing on the
canvas to show for it. So the comparison is made **after** the blocked read,
not before it, and when either half fails both figures are withheld and the
panel states "this graph has changed — refresh", because "frees N **in this
graph**, M immediately" would otherwise name one graph while the picture beside
it shows another. A focused **epic** is the limit of that rule: D10 states no
figures for it, so a move in the answer half leaves its line untouched
entirely.

**What the canvas shows is the CLIENT's to state.** D8's "what the canvas
lights is a lower bound" and D7's "on the longest chain (k of n)" are claims
about the PICTURE, not about the figures — and the picture is the static
payload the browser is focusing, which the panel's own rebuild may no longer
be: it can hold a different topology, or nothing at all when the scope has
since been refused, failed, or lost the focal node. So every panel the graph
page fetches carries `canvas_bound=lower|exact` and `canvas_chain=<k>:<n>`
beside its `scope=` and `snapshot=`, and those are what the panel renders.
(Named apart from the blocker trail's `chain`, and deliberately outside the
filter-query byte budget: they are re-emitted into nothing, and an annotation
Lens appends to its own URL must never push a request the page was already
served under past that ceiling and have the panel refused.) Both come from
the server either way — the lower bound rides in the payload per node, and the
position is read off the chain the payload ships — so the browser restates D7
and D8 rather than reimplementing them. A panel asked without them (any caller
that is drawing nothing) answers from its own assembly, keeping the notes when
the canvas half of the fingerprint still holds and dropping them with the
figures when it does not; it never invents a claim about a picture nobody
named. The server-rendered `focus=` page needs none of this: its assembly IS
what it draws.

Those two statements are also independent of every later READ. A `task_get`
that fails renders "Task unavailable" — and a health probe that has gone red
since the page loaded renders the offline panel — and both still render them,
because the operator is looking at a focused, lit neighbourhood either way and
neither fact needs a live Lithos call: they describe the page's own static
payload and they arrived in the request. What those panels do not render is a
figure or a "refresh": there is no focal status to count against, and neither
a failed read nor an outage is what a refresh of the GRAPH would resolve, so
the line degrades to its notes alone and the panel's own markup carries the
error.

Titles, claims, blocker messages and error reasons are excluded from both
halves — they move no figure, and a fingerprint that changed on every heartbeat
would withhold the line permanently rather than when it is wrong. Each half's
material is serialised as **canonical JSON** before hashing: a task id is an
arbitrary non-empty string (§5.1) and nothing normalises control characters out
of one, so a digest that joined its fields on a separator would read
`a -> b<sep>c` and `a<sep>b -> c` as the same edge and let a moved graph pass
the check. (The two HALVES are joined on a separator, which is safe where the
fields are not: a hex digest cannot contain one.)

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
at all — has no fact to state and degrades to the withheld line above. The
resolved wording is owed even when the panel could assemble **no impact at
all**: a task that resolves leaves an `include_resolved=0` project graph the
moment it does (nothing names an isolate), so the rebuild finds no node to
count over — and an empty slot under a `completed` badge is the same omission
as a future-tense one. A panel that asked for no `scope=` is untouched by that
(the dashboard's panel states no impact), and an epic is excluded from it
exactly as it is from the count. Either way the impact costs the line and
nothing else: a scope that fails, is refused, or does not hold the task renders
the rest of the panel unchanged.

An unknown id renders the **not-found panel** at HTTP 200 on both routes —
never a 500, and never at the cost of the board beside it — and a read that
merely failed says so instead, because "this task does not exist" is Lithos's
answer rather than a transport outcome. The open panel carries its own refresh
fragment, so the reconcile that keeps the board live keeps the panel's blocker
and dependent statuses live too without rebuilding it under the cursor.

#### 5.6.2 Mini-graph

`GET /tasks/{task_id}/minigraph` renders the picture the detail page shows
above its blocker chain. It is a **scope** in the sense §5.10 gives the word —
assembled over the same per-task edge cache, serialised into the same embedded
payload, drawn by the same client module — so the neighbourhood above the chain
and the project graph one link away cannot disagree about a colour, a shape or
which way an arrow reads.

- **Membership: two up, one down.** Incoming `blocks` / `waits_on_gate` edges
  to depth 2, outgoing to depth 1, and the parent epic as a single labelled
  node. `discovered_from` is excluded in both directions: the page renders
  provenance as its own text section, and a non-blocking relation inside a
  picture read as blocking would be misread. Depth 2 is enumerated from EVERY
  depth-1 blocker — the ones the cap cut included, and the one an earlier TIER
  draws (Lithos puts no type restriction on `blocks`, so a parent epic may
  also block its own child) — because the remainder below counts the whole
  neighbourhood the rule names. Tier membership decides which node an id is
  drawn as; the frontier decides whose blockers are depth 2, and they are not
  the same question. Only the RECORDS behind depth 2 wait until a slot could
  still hold one.
- **The parent epic is an ancestor, not the immediate parent.** `epic` is a
  task type rather than a level of the hierarchy, so `epic -> task -> task` is
  a legal shape and the nearest parent is routinely a plain task. The
  `parent_child` chain is walked up to the first epic, bounded by the same
  depth limit and seen-set as the detail page's breadcrumb. When the epic is
  further up than the immediate parent it is drawn as a labelled node with no
  edge: the tasks between are not members of this scope, and an edge straight
  from the epic to the focal task would be a relation Lithos never wrote.
- **An absent hierarchy node says which absence it is.** Exactly one ending
  means the task HAS no parent epic: the chain runs off the top of the forest
  without passing one, and the fragment simply draws no hierarchy node. The
  other three — the walk's depth bound, a `parent_child` loop, and a read that
  failed (an ancestor's `task_get` or `edge_list`, or the FOCAL task's own
  edge list, which is where the first parent edge would have been) — mean Lens
  could not decide, and the fragment says so in its own line ("this task's
  parent epic could not be determined", with which of the three it was). The
  picture is identical either way, so silence there would report a task with an
  epic as a task without one; the reason also rides on the render's span.
  Which of the four it is, is decided on the ancestor left PENDING — including
  after the last hop the bound allows. A walk that spends its final hop
  reaching the top of the forest has answered, and one that spends it arriving
  back at a task it already visited has found a loop; only an ancestor left
  genuinely unexplored is the bound's own outcome.
- **The cap counts the focal task.** `[graph].mini_graph_max_nodes` (40) is the
  whole picture, filled in one deterministic priority — focal task, parent
  epic, depth-1 blockers, depth-1 dependents, depth-2 blockers, each tier in
  (`created_at`, `id`) order — so which nodes an operator sees does not depend
  on which edge Lithos happened to list first. What the cap left out is counted
  through the page's one shared tail, which states the size that actually bound
  this list rather than the 25-row neighbour page every other list on the page
  uses. That tail counts NEIGHBOURS: the cap includes the focal task, the
  sentence is about the tasks around it, and the remainder — the figure the cap
  is accountable for — is the same number either way. A cap of one is a legal
  configuration and the boundary of that arithmetic: no neighbour is drawn, and
  the tail says so rather than falling back to the page's default size.
- **A read budget, beside the node cap.** The cap bounds the PICTURE, and the
  work behind it is not the same thing: the first-hop record reads and the
  depth-two edge reads are both sized by the edges, which Lithos does not cap,
  so a mini-graph drawing one node could otherwise queue thousands of calls
  (a semaphore bounds how many run at once, never how many are queued). A
  render may therefore queue at most `MAX_MINI_GRAPH_READS` reads — an
  internal safety net like §5.10's ghost-resolution bound, not an operator's
  dial — counted before each phase enqueues anything, and counted over the
  WHOLE frontier rather than over its cold half: a cache hit is not a
  reservation, since the entry can reach its TTL or be flushed by a task event
  in the await between the count and the gather, and a ceiling that holds for
  some interleavings only is not a ceiling. Past it the fragment is
  REFUSED: it draws no picture and states the read count instead, offering the
  focus link, and the blocker chain below it is unaffected. Refusing is what
  the exact remainder costs — a tail that promises "21 more not shown" may not
  itself take unbounded work to count, so Lens declines rather than drawing
  part of a neighbourhood it never finished reading. The refusal rides on the
  render's span and is its own `outcome` on the counter.
- **There are no ghosts.** Every node is a task the neighbourhood named, drawn
  as itself; a depth-1 dependent is a leaf because the scope STOPS there, not
  because Lens could not read it. The two completeness markers mean what they
  mean everywhere else: a node whose `task_get` failed is drawn with `status
  unknown` (never dropped — hiding a possibly-live blocker is the wrong way to
  err) and every edge touching it is `unknown`; a node whose `edge_list` failed
  is marked `edges unknown`, and the fragment says how many there were rather
  than letting a partial neighbourhood read as a whole one.
- **No cycle verdict.** Cycle membership is Lithos's, from a scoped
  `task_blocked` read (§5.11), and a per-task fragment has no scope to make one
  with. The mini-graph draws the shape its edges show and marks no node "in a
  cycle" — the graph page one link away is where that verdict is rendered.
- **Its text is only what it alone knows.** No layers, no chain line, no node
  list: the blocker chain and the `Blocks:` line below it are the accessible
  baseline, and restating either here would be one claim rendered twice. What
  the fragment does state is the legend, the `as of` staleness bound, the
  remainder tail, and a focus link to `/tasks/graph?project=<slug>&focus=<id>`
  — omitted, with a sentence saying why, for a task that belongs to no project
  and therefore has no scope to open.
- **Client-side it is the graph page's module in a narrower mode.** It boots on
  the `htmx:afterSwap` that delivers the fragment (its canvas does not exist at
  load), reads its state from the payload rather than from the detail page's
  URL, writes nothing to history, opens no side panel, and claims its container
  so a later swap cannot draw a second instance over a live one. The detail
  page's reconcile replaces the whole detail fragment, so the mini-graph is
  re-fetched and re-drawn with it — deliberately, because the picture and the
  chain beneath it may not disagree, and unlike the graph page there is no
  exploration state to lose. The layout is deterministic from the server's
  roots, so an unchanged neighbourhood redraws in the same places. That swap
  is a hand-made one (`tasks.js` parses and inserts the fragment itself), so
  no HTMX cleanup runs behind it: the swap announces itself on `document`
  (`lens:fragment-replaced`) and the mini-graph DISPOSES of the picture that
  removal detached — the Cytoscape instance, its resize observer and the
  claimed-node animation, whose completion callback is what re-arms it.
  Teardown is decided by the old container having left the document, not by a
  replacement arriving, because four states draw no replacement at all: the
  fragment's offline branch, its assembly-error branch, its refusal branch,
  and a request that never lands. Otherwise a tab left open would accumulate one live instance,
  observer and animation per task event. The
  exploration classes are off — every node on a mini-graph is in the focal
  task's neighbourhood, so lighting them would say nothing.
- **The automatic fit is bounded at both ends.** A neighbourhood too large for
  the box overflows and says so rather than being scaled below legibility; a
  sparse one — most commonly the focal task alone — is never MAGNIFIED past
  the model scale the styling vocabulary defines, which is what a populated
  mini-graph renders at. Without the ceiling one node was blown up to fill a
  fixed-height panel, with a label larger than the page's own title. Only the
  automatic fit is bounded: a zoom the operator chooses is theirs. The box
  itself then gives back the height a small picture does not need, so the
  glance is a small picture rather than a small picture in an empty panel.

### 5.7 Knowledge Surface

K1 replaced the minimal note path with a browsable knowledge surface.

`GET /note/{knowledge_id}` renders a note with:

- server-side markdown (headings, tables, code); raw HTML is escaped and
  `javascript:` hrefs are neutralized
- the body's **first H1 collapsed when it repeats the title**: when the first
  block of the parsed body is an H1 whose rendered text (markup dropped,
  entities decoded, a `[[wiki-link]]` read as the text its anchor shows)
  equals the frontmatter `title` (trimmed, whitespace collapsed,
  case-sensitive), it is omitted, since the header already shows the title. A token-level rule, so an H1-shaped line in
  a code fence, an H1 further down, or one that differs is kept. The
  comparison is with the frontmatter title as Lithos sent it: an empty title
  matches an empty H1, and the header's "Untitled document" label for it is
  not a title, so an H1 spelling that label is kept
- **wiki-links** (`[[target]]`) resolved through `/knowledge/resolve`, which
  renders a disambiguation page when a target is ambiguous and a not-found
  panel when it resolves to nothing
- **frontmatter metadata chips** (note type, status, access scope, namespace,
  confidence), a short-summary lede above the body, a `supersedes`
  back-reference, and an authorship line
- a **related panel** — the note's neighborhood, sectioned by relationship,
  with back-links and a bounded title fanout that falls back to bare ids past
  the cap. Each group carries an id (`related-links`, `related-backlinks`,
  `related-sources`, `related-derived`, `related-unresolved`, `related-edges`;
  the panel itself is `#related`). At and above the stylesheet's two-column
  breakpoint (`min-width: 701px`, the complement of the task pages'
  `max-width: 700px` collapse) the panel is a second column beside the
  article, sticky to the top of the viewport and scrolling in its own box when
  taller than the window; below it, the panel follows the body. The DOM order
  is article then aside at every width, so the no-JS reading order is unchanged.
  Each typed-edge row (direction, type, weight, conflict) carries a closed
  `<details>` **"why?"** disclosure built from the edge row's own
  `provenance_type` / `provenance_actor` / `evidence` (no extra Lithos call;
  `knowledge_edge_evidence`): first how the edge came to be — "inferred by
  <actor>", "reinforced by citation" (`consolidation`), "declared in
  frontmatter" (`frontmatter`), otherwise "<type> by <actor>" — then the
  evidence. `evidence` is a JSON string or null; an inferred edge's is
  `{"rationale", "model", "confidence"}` (lithos `lcma/edge_inference.py`),
  shown as the rationale paragraph with model and confidence chips, any
  missing (or mistyped) key omitted — an object with none of the three shows
  no evidence at all. Any failure to read it — not JSON, JSON nested deeper than
  the decoder's recursion limit, not an object, or a `confidence` number no
  float holds (`1e400`, `NaN`) — shows the string raw
  as escaped text, and never fails the rest of the panel. A row with nothing
  to show (null or empty evidence, no provenance) has no disclosure; a
  reinforcement or frontmatter edge (null evidence) shows its provenance line
  and no rationale
- a **related summary line** directly under the metadata chips, from the
  panel's already-loaded data (no extra Lithos call): "Related: 2 outgoing
  links · 1 source · 3 typed edges" — one item per non-empty group, in the
  panel's order and wording (outgoing link(s), back-link(s), source(s),
  derived from, unresolved, typed edge(s)), each count the group's full size
  including its "+N more" overflow and each an in-page link to that group's id.
  A note with no relations reads "Related: none"; a failed related read reads
  "Related: could not be loaded". K2's "open in graph" link joins this line
  after the counts
- a **produced-by chip** when the note came from a task and that task reads
  back successfully
- a **back link** naming where it returns to: with `?task=` (a finding's
  document link, `request_filters.note_url`), "Back to <task title>" — this
  takes priority; otherwise with a `next=` that passes `write_guards.safe_next`
  (the landing's result links carry the landing URL, q and tag preserved, via
  `request_filters.knowledge_note_url`), "Back to search results" / "Back to
  notes tagged <tag>"; otherwise "Back to knowledge" → `/knowledge`. Note-to-note
  hops (related panel, wiki-links, the resolver) carry no `next`, and the
  resolver's own pages link back to `/knowledge` too

`GET /knowledge` is the landing page:

- **one search form**: the page's own `form.knowledge-search` (input, button,
  and the active `tag` / `namespace` as hidden inputs) is the landing's only
  `role="search"` form — the landing overrides base.html's `nav_search` block,
  so the header's nav search box, which GETs the same `/knowledge?q=` without
  the filters, is not rendered here. Every other page (the note page, the
  resolver's pages, the task pages) keeps the nav box. With an empty `q` the
  landing's input is `autofocus`; with a query it is not
- **hybrid search** (`?q=`) renders result cards — title, path, escaped
  snippet, updated date — from `lithos_search` (`knowledge.search_limit`).
  A snippet drops a leading `# <title>` line that repeats the card's title
  (the note page's rule, found by parsing the snippet); it otherwise stays
  escaped text, never rendered, and is shown whole as Lithos windowed it —
  Lens adds no truncation
- with no query, **two sections** — title, path, updated date — newest first
  over the whole corpus: **"Your notes"** (non-intake), then **"Recent
  intake"**, each cut to `knowledge.recent_limit`. A note is intake when it
  carries an `ingested-by:*` tag or its path starts with one of
  `knowledge.intake_path_prefixes`; one that is both is intake, once. The
  sections partition the one `recent_notes` walk (`knowledge_landing.
  build_recent_landing`), which, asked for no limit, returns the whole
  tag-filtered corpus newest-first — 12 pages for the 5,818-note live corpus,
  bounded by the 40-page (20,000-note) runaway guard — so no extra Lithos call
  and no section starved by the other. Each heading links to its section alone
  (`?section=notes|intake`, filters kept)
- a **namespace filter** `?namespace=<ns>` on the landing and on search: a
  row ("all", then the top 8 namespaces present in what was fetched, most
  notes first — the intake namespace is shown, not hidden), the active entry
  `aria-current`. The namespace is path-derived — the first path segment,
  matched as the prefix `<ns>/` (`lithos_list` cannot filter frontmatter
  namespace, ROADMAP ledger #16). Search sends it as `lithos_search`'s
  `path_prefix`; the browse sections filter on path Lens-side after the walk,
  so the row still counts every namespace — over every fetched note, whichever
  section is shown (a `?section=` view offers the landing's row). `?tag=` narrows the sections and a
  search to one tag and composes with it; the "Filtered by" line names both,
  and the search form carries both as hidden inputs
- **metadata chips** on every card and row: the note page's chip partial
  (`knowledge/note_chips.html` — type, status colour-coded, scope when not
  `shared`, namespace, confidence; not `supersedes`) in its compact one-line
  variant (`.note-chips-compact`). Neither `lithos_search` rows nor
  `lithos_list` items carry these fields (ROADMAP ledger #16), so
  `knowledge_metadata.load_list_chips` reads each row's frontmatter with a
  `lithos_read(id, max_length=1)` — the related panel's cheap read, under the
  same process-wide MCP call gate — once per distinct id per request, for the
  first `knowledge.list_chip_fanout_cap` ids (default 40). Rows past the cap
  render chipless under a "Chips shown for the first N notes." line; a failed
  read leaves only its own row chipless, never the list. The number of reads
  is the landing span's `lens.chips.fanout`

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

- **membership** — a project scope is the project's tasks per §5B.1 (either
  convention; the picker one link back offers exactly this set), open only
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

Every task the text names — a layer entry, the predecessor on an edge line, a
hierarchy row, a cycle callout's member, a step of its arrow walk, a task the
longest-chain sentence names, a hit offered by the search box, an epic in the
scope picker — carries its **short id** (§5.3), ahead of the status badge
wherever the entry has one. Ghosts included: a ghost is one hop outside the
scope and carries no status of its own, so the id is often all the operator
has. One thing on this page does not carry it (§5.3's exemption): the canvas.

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
and a Close that pushed a URL still carrying the alias. The selection value is
read and redirected **byte for byte**: a task id is an arbitrary non-empty
string (§5.1), so trimming it would look a different node up and — through that
redirect — write the trimmed id into the URL bar permanently; only an absent or
empty parameter is "no focus". A request carrying a
selection **server-renders that task's side panel** beside the canvas (§5.6.1's panel, this page's no-JS baseline, counted as a
`url` open), with that panel's downstream impact computed from this render's
own scope, and a read that fails there costs the panel rather than the graph.
The panel does not depend on the canvas having anything to draw: a scope that
renders "nothing to draw" — a project whose last task resolved while
`include_resolved` is off — still opens the panel its `focus=` names, and the
resolved wording that panel owes (§5.6.1). The panel CONTROLLER loads with it:
`tasks.js` and its configuration are emitted whenever this page rendered a
panel host, because Close and Escape remove `focus` by `pushState` and that is
its job — without it the Close link would follow its href and reload the page.
Only the canvas half (Cytoscape and `graph.js`) is gated on there being nodes
to draw. A REFUSED scope renders neither panel nor canvas, having no host to
put one in. `overlays=hierarchy,provenance` is
carried for the client layer.
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
`project=<slug>` plus `tags=["<project_tag_key>:<slug>"]` — both halves
always, because membership is both conventions — each at
`tasks.frontier_limit`, with `len == limit` treated as truncation. The
pair is unioned **per task**: the two calls are independent reads rather than
one snapshot, so a task's blockers are merged across every response that names
it (a `kind="cycle"` blocker arriving on either side is Lithos's verdict), and
two rows are the SAME blocker when their kind, predecessor, edge type and
predecessor status agree. The human-readable `message` is deliberately not part
of that identity: it is presentation text sampled with each read — a gate's
carries its `ready_at` — and counting one blocker twice because its wording
moved between the two calls would withhold §5.5.1's "immediately" from a
dependent this task alone is blocking. A
task is cycle-status *known* when it appears in any response, or when some read
that **could have matched it** answered in full — and that is a question about
the convention each read expresses (§5B.1): `project=<slug>` is the metadata
convention, `tags=["<key>:<slug>"]` the tag one. An empty response from a
filter the task cannot match is not coverage — which is why both halves are
always issued. `project_convention` used to drop one of them, leaving a child
that carried its slug under the unissued convention in scope with its cycle
status resting on a read that could never have named it; retiring the knob
(REQUIREMENTS §4.4) closed that with the membership gap it came from.
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

The detail mini-graph (§5.6.2) is instrumented the same way and for the same
reason — it is a multi-phase fan-out too. Each fragment opens one
**`lens.tasks.minigraph`** span carrying `lens.minigraph.*`: the task id, the
outcome, the node count, whether the cap bound, how many neighbours it left
out, and that render's own cache hits, misses and neighbour reads. The counter
beside it is `lens_tasks_minigraph_renders_total` (`outcome` in `rendered` |
`capped` | `refused` | `offline` | `error`), with `capped` split out because
how often 40 nodes is not enough is the only evidence there is for whether the
knob is set near the corpus's shape, and `refused` split out because it is the
work bound rather than the node cap — no picture at all, and the reads that
would have been queued ride on the span beside it. The node count stays on the span: it is a
distribution per task, which is the same cardinality the rule above forbids.

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
  relation is not known in either direction. That last class is **transitive**:
  past an endpoint Lens could not read, the edges beyond it were reported by
  somebody else's list and "which way round" is not a question the payload can
  answer, so everything the unknown frontier goes on to reach is unknown too —
  while a node the active walk lit keeps its lighting, an independently known
  path being knowledge the frontier does not take away. The walk crosses `active` edges
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
  from its start. The two domains read the same keystrokes differently: a title
  is prose, so its query is trimmed; an id is an arbitrary non-empty string
  (§5.1), so its prefix is matched against the query verbatim, and only a truly
  empty box offers nothing. Selecting a match (click, or Enter for the first one) is
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

### 5.13 Operator Identity and Write Posture

T3-W1 shipped the posture the write actions attach to; the first task write
that uses it, completing a gate, is §5.15. Three facts describe the posture:

**There is no read-only mode and no `[writes] enabled` flag.** The write route
group is registered like the graph and knowledge groups; what decides whether
an affordance renders is the task's state and whether an operator identity
resolves — never configuration. The trusted-network boundary is therefore the
only protection there is, which is why it is stated on `/operator` (§5.1).

**Identity resolution** is `lens_operator` cookie → `[writes].default_operator`
→ none. An operator id must match `^[a-z0-9][a-z0-9-]{0,62}$`; anything else is
invalid, and invalid means *absent* when read from the cookie (the configured
default then applies), a **load failure** for `default_operator`, and the form
re-rendered with the reason on `POST /operator`. The cookie is validated on
every read, not once when it was set. With no identity resolved, the page
chrome renders a single "choose an operator to act" link in place of any write
affordance.

**The impersonation guard and register-once.** Lithos auto-registers an unknown
`agent` on any write, untyped, so Lens registers an identity with
`lithos_agent_register(id=<operator>, type="human")` before its first write in
the process; the ids it has **registered** are held in memory, so the cost is
one upstream call per identity, not per write, and the whole operation
(lookup, register, record) is serialised per process — two first writes for one
identity, a double submit or two tabs, produce exactly one registration. A
failed registration refuses the write — "could not register the operator
identity; nothing was changed" — and is not remembered, so the next write
tries again.

What is deliberately **not** remembered is an identity some earlier check
merely approved. `POST /operator` runs the same guard up front so a refusal is
immediate, but that answer is about the moment it ran: the registration seam
always does its own lookup. Caching the page's acceptance would suppress
exactly the lookup that protects an agent's registry entry, across a window as
wide as the time between visiting the page and the first write.

Before accepting an identity, Lens looks the id up **exactly**, with
`lithos_agent_info` — not `lithos_agent_list`, which hides archived agents by
default, so an archived agent's id would read as unregistered and be re-typed
by the registration that follows. The lookup's "not found" answer is the tool
returning `None` rather than an error envelope — over MCP, a result with no
content and `structuredContent` `{"result": null}` (or that wrapper, or a bare
`null`, as text) — and is mapped to "absent" by the client alone. An empty
result carrying no null, like any unparseable or error result, is a failed
lookup. An identity is accepted when the lookup finds nothing, or finds
an agent already typed `human` (archived or not — the type is the whole
question). Lens's own service agent's id is refused, and so is any id that
exists with another type or with none ("that id belongs to an agent"). A lookup
that **fails** refuses a *new* identity, because Lens cannot tell "absent" from
"unreadable" and only one of those is safe to register; an identity this
process has already registered keeps working, because a write under it makes
no call at all. The guard runs at the registration seam — so it covers a
configured `default_operator`, which never passes through the operator page —
and again up front on `POST /operator`, so a refusal is immediate.

**Known limit, by decision (Dave, 2026-10-03).** `lithos_agent_register` has no
conditional ("create only", or "only if still untyped") form, so the window
between the lookup and the registration **inside one seam call** is open: an
agent that registers the operator's chosen id in that instant has its type
overwritten to `human` by Lens's call. Lens does not close this window and
ships no protocol for it — narrowing it is the most a client-side pre-check
can do, exactly as the `expected_status` pre-check narrows (and cannot close)
the write race. Closing it needs an upstream conditional registration.

### 5.14 Write Refusal Copy

The first piece of **T3** (curated write actions) to ship, ahead of the actions
themselves: the mapping from a Lithos write failure to what the operator reads
(§5C.4). The write funnel (§5.15) answers with it: the pure mapper
(`lithos_lens.write_errors`) for what Lithos returns, a public builder there for
the refusals the funnel makes itself before Lithos sees a write, and the pages
both feed — the conflict and unknown-outcome pages, plus a refused page for a
lifecycle action, which has no form of its own to re-render.

A write to Lithos has three endings, and this covers the two that are not
success:

| Ending | Answer | Status |
|--------|--------|--------|
| Lithos refused it, and the task is not in the state the operator acted on | the **conflict page** (`writes/conflict.html`) | 409 |
| Lithos refused the content of the request | a **notice on the form that was submitted** (`writes/notice.html`), with the operator's input kept | 422 |
| Lithos never answered — transport failure or timeout | the **unknown-outcome page** (`writes/unknown_outcome.html`) | 200 |

The mapper reads the **whole error envelope**, not a code and a message: fields
such as `candidates` carry what the copy needs, and a field upstream adds later
reaches the copy without a signature change. The codes mapped are the ones
Lithos 0.5.0 raises on a write:

| Code | Copy |
|------|------|
| `task_not_found` | Complete and cancel answer this both for "missing" and for "not open" — one code for two facts, which the envelope cannot separate. Lens re-reads the task and the copy splits on the result: it exists → "this task is now *\<status\>*"; it is gone → "this task no longer exists"; the re-read itself failed → neither is claimed. Create and the edge actions spend the same code on a task the request **refers to** — a prefix that matches nothing, a parent or predecessor that does not exist, an edge endpoint that does not — so for them it is a refusal on the submitted form, input kept: "Lithos couldn't find a task this refers to." with the upstream message shown whole (it names the parameter and the id; plain, since the id links nowhere) and attached to the parameter it names (`parent_task_id`, `depends_on`, `from_task_id`, `to_task_id`), form level otherwise. Only a re-read showing the edge's own task gone answers it with the conflict page's "this task no longer exists". |
| `task_not_resolved` | "This task is already open." (reopen) |
| `invalid_input` | The upstream message, shown whole and attached to the write parameter it names, for a form re-render with input kept. Lithos sends no field key with this code, so the parameter is found in the message: `parent_task_id`, `depends_on`, `from_task_id` and `to_task_id` (a too-short id) or `metadata.gate_type` and `metadata.ready_at` (gate metadata). It is read only from the positions where Lithos's diagnostics name a parameter, anchored to Lithos's own text at the start (`<field> '<raw>' is too short`, `<field> references nonexistent …`, `a gate task requires <field>`, `a 'timer' gate requires a parseable <field>`) or the end (`No task matches id prefix '<raw>' (<field>).`) of the message — never by searching it — so operator input that spells another parameter's name is never taken for the field. A message that names none of them renders at form level |
| `ambiguous_id_prefix` | "'\<prefix\>' matches more than one task", with the envelope's `candidates` rendered as choices, each naming its task by title and short id. The prefix is the envelope's when it carries one, else the value the form sent |
| `cycle` | "This dependency would create a cycle." then the upstream message **verbatim**. Task ids inside it get the existing short-id link treatment; that is presentation only (`uuid.UUID` decides what is an id, and nothing Lens does reads the message) |
| `parent_exists` | "*\<Task\>* already has a parent." then the upstream message, which names the existing parent by id (linked with the short-id treatment), and a hint saying how to replace it: remove the current parent relation first, then add the new one |
| `self_edge` | "A task can't depend on itself." |
| `not_a_gate` | "*\<Task\>* isn't a gate — only a gate can be waited on." |
| `invalid_edge_type` | A Lens defect (the relation form offers only valid types): the unknown-code path, logged at `error` |
| *(unknown code)* | The code and message verbatim with a "report this" hint — forward-compatible with codes upstream adds. The message is plain text: a full id in it stays full, because the short-id treatment would shorten the thing the report has to quote, and its line breaks and runs of spaces are kept (`white-space: pre-wrap` on every quoted upstream message) |

Three rules hold across every row:

- **No write error is a 500.** A refusal is an answer, not a Lens failure. An
  unmapped code is answered like any other refusal rather than raised, and the
  conflict page is 409 rather than 404 even for a task that is gone: the URL
  was right when the page was rendered, and a 404 reads as "no such page".
- **A refused write says that nothing was changed**, first and before the
  reason. One element (`.write-outcome-claim`) in one shared partial renders
  that sentence, so every surface the copy reaches states it.
- **The unknown-outcome case claims neither** that the write applied nor that
  it did not. It says "the action may or may not have applied" and then what a
  re-read shows now, with nothing joining the two — Lithos writes are not
  idempotent (§14 of the requirements), so the page offers no retry. Its 200 is
  the absence of a claim: a 4xx would blame a request that was fine and a 5xx
  would say the action failed, which is the one thing it must not say.

### 5.15 Write Funnel, Receipts, Complete a Gate, Reopen, Cancel, Create and Add a Dependency

The task writes (T3-W4 onwards). Every curated write goes through **one function**
(`lithos_lens.write_funnel.WriteFunnel.submit`, or `submit_create` for a create,
which has no task to pre-check — see Create below); route handlers parse their form
and describe their action, and never call a Lithos write themselves. The steps,
in order:

1. **Origin** — the Origin/Referer check (§5.1), first, so a refused attempt is
   still recorded.
2. **Operator** — cookie → `[writes].default_operator` → none (§5.13). With
   none, a plain POST is redirected (303) to `/operator?next=<the form's next,
   else the task's page>` and an HTMX POST answers `HX-Redirect` to it; the
   write is **not replayed** — the operator repeats it once they have a name.
3. **Form** — the form's `expected_status` (the status the operator saw) must be
   a task status.
4. **Registration** — the identity is guarded and registered once (§5.13).
5. **Pre-check** — the task is re-read with `lithos_task_get`. A status other
   than `expected_status` is the conflict page — "This task is now
   *\<status\>*." — and **no write**. The conflict copy names the status only:
   Lithos's task record carries no actor or update time, and Lens invents none.
   The action then says whether it applies to the task as read, and whether it
   needs a confirmation the form did not carry — in which case a plain POST is
   redirected (303) to the action's confirm page and an HTMX POST answers
   `HX-Redirect` to it, and nothing is written (Proceed anyway, below).
6. **The single Lithos call.**
7. **Classification** — an error carrying a non-empty envelope is Lithos
   refusing, mapped by §5.14 (a `task_not_found` re-reads the task first, so a
   gate completed in the window after the pre-check is the same conflict page).
   An error with an **empty** envelope — a timeout, no session, an `isError`
   result, an unparseable answer (`invalid_response`) — or any other exception
   is no answer at all: the unknown-outcome page, with what a re-read shows now.
8. **Record** — exactly one structured audit line (`lens_event:
   "lens.writes.audit"`: operator, action, task id, an argument summary of ids
   and lengths — never free text — the status expected and observed, the
   result, its code and the result envelope — what Lithos answered, on success
   (the completion's `task_id`, `title`, `updated_at`, `unblocked` ids) as on a
   refusal), one span
   `lens.writes.<action>` (`lens.write.operator`, `.task_id`, `.result`,
   `.code`, and for complete `.gate_type` and `.override`), and one
   `lens_writes_total` increment — for **every** attempt, refusals included.
9. **Receipt** — minted from what the write returned (see Receipts, below).
10. **Answer** — a plain form POST gets `303 See Other` to a page rendered from
    fresh reads with `?receipt=<id>` merged into its query (any earlier
    `receipt=` replaced); an HTMX POST gets the receipt — or the refusal's copy
    — as a fragment, **always 200** (htmx swaps no 4xx/5xx body), with
    `HX-Trigger: lens:reconcile`. `tasks.js` listens for that event and runs its
    immediate, coalesced reconcile, so the board is re-rendered from fresh reads;
    no row is ever assembled from a write's answer. htmx fires that event on the
    form that posted, so when a reconcile replaced the row while the write was
    in flight (detaching that form), the answer's swap into the receipt slot
    runs the same immediate reconcile instead — exactly one of the two fires
    per answer. An Origin refusal is answered this way too for an HTMX POST
    (200, its notice and the trigger); a plain POST keeps the 403.

Every way an attempt ends, for a plain POST:

| Case | Status | Copy | Result (code) |
|---|---|---|---|
| stale `expected_status` | 409 | conflict page: "This task is now *\<status\>*." | `conflict` |
| the task is gone at the pre-check | 409 | conflict page: "This task no longer exists." | `conflict` |
| upstream `task_not_found` on a task that exists and is not open | 409 | the same conflict page, via the re-read | `conflict` |
| not a gate | 409 | "This task isn't a gate — only gates can be completed here." | `rejected` (`not_a_gate`) |
| a machine-owned gate posted without the confirmation | 303 to the confirm page | — | `rejected` (`confirmation_required`) |
| a cancel posted without the confirmation (`confirm_cancel = true`) | 303 to the confirm page | — | `rejected` (`confirmation_required`) |
| a cancel whose form claims the task's own resolved status | 409 | "Only an open task can be cancelled; this one is *\<status\>*." | `rejected` (`not_open`) |
| `expected_status` missing or not a status | 400 | "The form didn't say what status you saw — reload and try again." | `rejected` (`bad_form`) |
| the pre-check read fails | 503 | "Lens couldn't read the task, so it didn't try the write." | `rejected` (`precheck_failed`) |
| an identity that belongs to an agent | 403 | the operator page's refusal copy | `rejected` (`identity_refused`) |
| the identity lookup or registration fails | 503 | "Could not register the operator identity; nothing was changed." | `rejected` (`registration_failed`) |
| upstream refusal with an envelope | 422 | §5.14 | `rejected` (the upstream code) |
| no answer (empty envelope, transport failure) | 200 | the unknown-outcome page with the re-read | `unknown` |
| foreign or absent Origin | 403 | — | `refused_origin` |
| no identity | 303 | — | `no_operator` |

Every refusal says "Nothing was changed." first; the unknown outcome says the
action may or may not have applied. Each answers as the conflict page, the
unknown-outcome page, or (for a refusal) a page carrying the same notice.

**Receipts.** A redirect loses the response body, so the funnel files the
write's outcome in an in-memory receipt store under a random id, and the page
the redirect lands on renders it as a banner in the layout's one **receipt
slot** — above the content, outside every refreshed fragment, so the reconcile
neither wipes nor repeats it. An HTMX row action swaps its answer into the same
slot. A receipt is **shown once** (consumed as it renders); the store holds at
most 64 and drops one not shown within five minutes (module constants, not
config). Receipts are feedback, not state: a restart loses them and nothing
becomes wrong. A completion's receipt reads "Completed gate *\<title\>*", what
was recorded as its outcome and under whose name, and "Unblocked *N* tasks"
naming the first five by title — read with `lithos_task_get` when the receipt is
minted, because the pages a receipt lands on hold no task index — with "and *N*
more" for the rest; a task whose title cannot be read is named by its short id
alone. A completion's receipt carries a **Reopen gate** follow-up (below).

**Complete.** Offered as one action — a **Complete** button, an optional
one-line note, a line pointing at the gate's own description (completing a gate
means what its author says it means; for a loom needs-human gate it is the
retry gesture), and the identity chip — on an **open** gate of type `human` or
`external_task`, decided by one helper over (task type, status, gate type) and
rendered by one partial on the gate row (the Gates section, and the ordinary
row Needs attention promotes a gate into), the side panel and the detail page.
It renders only when an operator identity resolves; with none, the chrome's
single "choose an operator" link is the only prompt. A gate row posts through
HTMX so the operator keeps their place on the board (its no-JS form returns to
the same board); the panel and the detail page post plain forms. A note typed
but not yet sent survives the live refresh: a reconcile that re-renders the
row, panel or detail fragment carries the draft (and the caret, if the field
had focus) into the fresh field for the same task. A click inside
a row's form — the button or the note — never opens the side panel. The note,
whitespace folded to one line and bounded at 500 characters, is sent as the
completion's `outcome`; with no note the outcome is "Completed via Lens by
*\<operator\>*". The re-read at the pre-check is what binds: a task that is not
an open gate is refused there, and a gate of any other type is sent to its
confirm page, whatever the page that posted offered.

**Proceed anyway** (T3-W4b). A `timer` gate resolves itself at `ready_at`; a
`ci` or `pr` gate is resolved by whatever watches the check or the PR.
Completing one by hand overrides a machine wait — allowed, because completing
is the only way to release a gate's waiters (cancelling strands them), but a
decision, never a mis-click. So an open gate of those types — and of any type
Lens does not know, the cautious path — carries a **Proceed anyway…** link in
place of the Complete form, on the same three surfaces through the same
partial, decided by the counterpart helper (`gate_completion.proceeds_anyway`);
a gate gets one or the other, never both, and like Complete the link renders
only when an operator identity resolves. It is an ordinary link, so on a board
row it navigates rather than opening the side panel. It opens the confirm page
(`GET /tasks/{task_id}/approve`), which states, from what Lens can read and
nothing more:

- **what would otherwise resolve the gate** — a timer's `ready_at`, and when
  that has already passed, that *the gate no longer blocks anything; completing
  it only closes it*; a timer whose `ready_at` is absent or unparseable says
  Lens can't tell when it would resolve. A PR gate's `metadata.pr_url` as a
  link (only an absolute `http`/`https` url is linked; any other value is shown
  as text). For CI and unknown types, the gate's own description;
- **the waiters completing it releases**, by title, from the same read the
  gate row's waiter list uses (the whole open list and the blocked frontier at
  `[tasks].frontier_limit`, falling back to the gate's `waits_on_gate` edges),
  with that read's labels carried over: "blocks at least *N* tasks",
  "blocks *N* tasks (unverified)", or "waiter count unavailable" — never a
  confident zero;
- that **whatever watches this gate will find it closed**.

It does not describe how the gate's author reacts. A loom `pr` gate's waiter is
the story itself, so proceeding makes the story ready again while its PR is
still open; what loom's watcher then does is not in its specification, and the
page names the PR and the released waiters and leaves the decision with the
operator. Its form — the only one carrying the confirmation — has the
`expected_status` the page read, the `next`, the optional note and a
**Complete anyway** button, beside a "Keep waiting" link and the identity
chip; with no identity the page states the same facts and offers no form. With
no note, an override's outcome is "Completed early via Lens by
*\<operator\>* — proceed anyway; *\<gate type\>* gate had not resolved" (an
untyped gate is named "untyped"), so the record never claims a wait ended that
did not. The span and the audit line record the gate type and
`override: true` (`lens.write.override`, `write_override`) — for an
unconfirmed attempt as for a performed one; a direct completion records
`override: false`. The receipt is the same as a direct completion's.

**Reopen** (T3-W5). Offered on the **detail page** of any completed or
cancelled task — task, epic or gate — decided by one helper
(`write_routes.offers_reopen`) and rendered only when an operator identity
resolves; it is not offered on board rows or in the side panel. A **Reopen**
button posts the status the page read, beside copy worded by that status,
because the two cases do opposite things to the dependents:

- **completed** — "Reopening puts this task back to open. Tasks that became
  ready when it completed will be blocked again."
- **cancelled** — "Reopening returns this task to open. Its dependents stop
  being permanently blocked and wait on it again." (Reopening a cancelled
  blocker is the remedy for Needs-attention rule 1: the effect is
  un-stranding, not re-blocking.)

When the task has an outcome the copy adds that it is cleared and that Lithos
keeps it in the `[Reopened]` finding it records; with none it says nothing
about an outcome. The write is one `lithos_task_reopen`; the span is
`lens.writes.reopen` with no action-specific attributes, the audit line's
arguments are the task id, its observed status is the prior status, and its
envelope is Lithos's answer with `reblocked` as a list.

The receipt reads "Reopened *\<title\>*" and who reopened it. What the task
was is worded as what Lens **read** just before the write — "Lens read it as
*\<status\>*", and when it had an outcome, that Lithos keeps the outcome it
cleared in its `[Reopened]` finding, quoting the outcome Lens read — because an
agent can resolve the task again between that read and the call, and nothing
Lithos returns says what the task was (the finding is free text any client can
post). The case is the status the pre-check read, unless Lithos's answer
proves otherwise: `reblocked` is non-empty only for a task that was completed
when the reopen applied, so a task read as cancelled but re-blocking
dependents is the completed case, and the receipt says the read was stale and
quotes no outcome. Then, by that case:

- **completed** — "Re-blocked *N* dependents" naming them, from Lithos's
  `reblocked` ids (titled at mint like a completion's releases: first five,
  "and *N* more", short id alone on a failed read); with none, "Re-blocked no
  dependents — nothing that waits on this task had become ready.", followed by
  the open dependents waiting on it again, read as in the cancelled case below
  (with no line when there are none). An empty `reblocked` does not prove the
  task was completed — an agent may have cancelled it after the pre-check — so
  the receipt states what is true either way rather than drop the dependents
  this reopen may have un-stranded.
- **cancelled** — Lithos's `reblocked` is empty by design here, so the receipt
  reads the task's outgoing `blocks` / `waits_on_gate` edges after the write,
  through the detail page's bounded dependents reader — one entry per dependent
  task, however many of the two edge types reach it — and names the **open**
  ones: "*N* dependents are waiting on this again"; "At least *N* …" when that
  page is truncated or a dependent's read failed; with none, "No dependents are
  waiting on this again — nothing depends on it." A failed edge read does not
  make the reopen unknown: the receipt still reports it and says "Lens couldn't
  read its dependents just now."

**Reopen gate.** A completion's receipt — in either answer mode — carries an
ordinary form posting to the reopen route with `expected_status=completed` and,
as its `next`, the page the completion returned to (for an HTMX row action,
the board, never the POST's own path). It is labelled **Reopen gate**, not
"Undo", with one line of why: it re-blocks the waiters, but does not recall
anything their agents started in between. Completing a gate and then reopening
it this way leaves the board as it started, and the second receipt names the
tasks the first did. The detail page can show this form and its own Reopen at
once; each has its own hook (`data-reopen-gate`, `data-reopen-action`).

**Cancel** (T3-W6). A cancelled predecessor blocks its dependents forever, so
the cancel states its consequence before it is made. Offered on the **detail
page** and in a **board row's overflow menu** of any **open** task — task, epic
or gate — decided by one helper (`cancel_routes.offers_cancel`) and rendered
only when an operator identity resolves. The overflow menu is new: a no-JS
`<details>` whose summary is **⋯**, shared by the ordinary row (open rows only;
the same template renders the Completed and Cancelled sections) and the gate
row, holding only the Cancel affordance. A reconcile that re-renders the board
collapses an open menu. With `[writes].confirm_cancel = true` (default) the
affordance is a **Cancel…** link to the confirm page; with it `false` it is a
plain form posting directly, with the optional reason field.

The confirm page (`GET /tasks/{task_id}/cancel`) states, from one consequence
read (`cancel_consequences.load_cancel_consequences`):

- **"Cancelling strands *N* tasks directly, *M* more behind them."** — *N* the
  open dependents one hop out, which become *permanently blocked until
  re-routed*, *M* the further open transitive dependents, each list naming its
  first five by title, oldest `created_at` first (then id), with "and *K*
  more". The walk goes downstream from the task over **active** `blocks` /
  `waits_on_gate` edges (§5.12's edge states), through the per-task edge cache
  with the task's own entry evicted and re-read first, and **crosses project
  boundaries**: which dependents are open comes from one cross-project
  `lithos_task_list(status="open")`, so a dependent in another project counts.
  A visited set keeps a cycle from counting a task twice, and the task itself
  is never counted. Nothing open behind it reads "Cancelling strands no open
  tasks — nothing open depends on it."
- **Lower bounds.** The walk visits at most `[graph].max_tasks` dependents and
  runs for at most 10 seconds (a module constant). Past either, or after any
  failed edge read, both numbers are rendered "≥ *N*" followed by "Both
  numbers are lower bounds:" and the reason — "Lens couldn't read the
  dependencies of *K* tasks (*\<code\>*)", "the walk stopped at its budget of
  *K* tasks", "the walk took too long". Where the graph page refuses a scope
  over its bound, this walk degrades. If the open list itself cannot be read
  the page says Lens couldn't work out what the cancel strands, and why, and
  still offers the cancel.
- **The active claims the cancel releases**, by agent — "It releases *N*
  active claims:" with each agent and its aspects — read with
  `lithos_task_status` before the cancel (once it lands they are gone); "No
  agent holds an active claim on it." when there are none, and a failed read is
  said to have failed, never stated as none.
- for a task with open children (any type): **"Its *N* open children are not
  cancelled with it:"**, naming the first five (`lithos_task_children`);
- for a gate: **"Its waiters become unsatisfiable. Completing the gate, not
  cancelling it, is how they proceed."** (§5.2.3).

Its form carries the `expected_status` the page read, the `next`, the
confirmation, and an optional **Reason** field labelled "The reason is recorded
in the event stream only — not stored on the task." (ROADMAP ledger #6), with a
**Cancel task** button beside a "Keep it" link and the identity chip; with no
identity the page states the same facts and offers no form. No future-tense
consequence is shown for a task that is not open: the page is the conflict
page. The write is one `lithos_task_cancel(task_id, agent=<operator>,
reason=…)` (an empty reason is not sent); the span is `lens.writes.cancel`
with no action-specific attributes, and the audit line's arguments are
`{"task_id", "reason_chars"}` — the reason's text reaches Lithos and nothing
else. The pre-check binds: a stale `expected_status` is the conflict page, and
a form claiming the status a resolved task already has is refused, both with
no call.

The receipt reads "Cancelled *\<title\>*" and who cancelled it, and — when a
reason was sent — that it was recorded in the event stream only, not stored on
the task. After a confirmed cancel that is all: the confirm page already stated
the consequences. With `confirm_cancel = false` there is no confirm page, so
the same facts are read inside the write, just **before** the
`lithos_task_cancel` call (the only moment the claims still exist), and the
receipt states them instead, through the same partial in the past tense ("This
cancel stranded …", "Released *N* active claims", "Its waiters are now
unsatisfiable …"). A failed read there degrades the facts to lower bounds or
"couldn't work out"; it never becomes the cancel's failure. The confirm page
still renders in that mode — it is a read.

**Create** (T3-W7). One form, `GET`/`POST /tasks/new`, for a **task**, an
**epic** or a **gate**: title, type, description (Markdown, rendered as
descriptions are everywhere), project, tags, parent, predecessors ("Blocked
by"), and a gate fieldset (gate type; `ready_at` for a timer). It is complete
without JavaScript: the gate fieldset is shown to every type, labelled "only
for gates", and a small script of its own (`create_form.js` — not `tasks.js`,
which would open the event stream) hides it while another type is selected.
The server drops the gate fields for a non-gate, because the no-JS form posts
them anyway.

- **Affordances.** **New task** in the dashboard's header — carrying
  `?project=<slug>` only when exactly one project is selected — and **Add
  child** on an **open epic**'s detail page, carrying `?parent=<epic id>`.
  Both render only when an operator identity resolves.
- **Inputs.** Tags and predecessors are **one per line** (a comma can be part
  of a tag); parent is one input. Parent and predecessors are full ids or
  prefixes of at least six characters, which Lithos resolves — there is no
  task datalist and no search endpoint. Project is optional free text,
  validated as a lowercase slug, with a datalist of the projects of the open
  tasks (one cross-project `lithos_task_list(status="open")`, the union of
  both §5B.1 conventions — the board filter's own derivation). A project with
  no open task is not listed but can be typed; a failed or slow read (3 s)
  renders the form without the list. The list is read by the `GET` only: a
  form re-rendered by a `POST` — a refusal, Lens's or Lithos's, or Start again
  — makes no read of its own and carries no list (the typed project is kept).
- **Validated in Lens before any call:** a title; a type of `task` / `epic` /
  `gate`; a project slug; for a gate, a gate type a **person** may create —
  `human`, `external_task` or `timer` (a hand-made `ci` or `pr` gate has
  nothing watching it; loom creates its own `pr` gates) — and for a timer a
  `ready_at`, a `datetime-local` value read as **UTC** and sent as ISO with an
  offset. A past `ready_at` is allowed: that timer is simply already ready.
  Each refusal re-renders the form (422) with the input kept and the message
  under its field, and makes no Lithos call.
- **The write** is one `lithos_task_create(title, agent=<operator>,
  description, tags, metadata, task_type, depends_on, parent_task_id)`. The
  project is written under **both conventions** (§5B.1): `metadata.project =
  <slug>` and the `<[tasks].project_tag_key>:<slug>` tag. The task's metadata
  is the project, `lens_request_id` and the gate's `gate_type` / `ready_at` —
  nothing else; the form has no input for advisory keys.
- **Lithos stays the authority.** A refusal with an envelope re-renders the
  form, input kept, with the §5.14 copy at the top and the upstream message
  under the input it names (`parent_task_id` → Parent, `depends_on` → Blocked
  by — which does not say which predecessor — `metadata.gate_type`,
  `metadata.ready_at`). An `ambiguous_id_prefix` lists its candidates (short
  id and title, as text) under the field whose typed value they all start
  with; the operator corrects the field. `invalid_task_type` and
  `invalid_metadata_key` take the unknown-code path at form level. The
  identity guard and registration refusals re-render the form too.
- **De-duplication on a request id — by Lens, in memory, best effort.** The
  server mints a request id (`uuid4().hex`) each time it renders the form; the
  form carries it and the task stores it as `metadata.lens_request_id`
  (provenance — nothing reads it back). A POST without a well-formed one is
  `bad_form` (400) with no call. One in-process coordinator remembers what
  each id came to, in a map bounded at 512 settled entries (oldest first, no
  TTL):
  - **no entry** — this submit makes the call, shielded so a browser that
    goes away cannot cancel it for the others;
  - **in flight** — the submit waits and receives that call's outcome; it
    makes no call of its own;
  - **created** — it lands on that task, with no call;
  - **unknown outcome** (a timeout, a dead session, an unparseable answer) —
    never sent again under that id;
  - **refused** — forgotten, so the re-rendered form keeps its id and a
    corrected submit may create.

  The guarantee, as it is: a double-click, a resubmit or the back button
  lands on the one task the first submit created. A create whose outcome Lens
  never learned can still be duplicated if the operator starts again while it
  lands, and **a Lens restart between a submit and its resubmit forgets the
  request id**, so that resubmit creates a second task. Closing either needs
  an idempotency key on `lithos_task_create` upstream.
- **Not visible yet.** After an unknown outcome, create's own page (200) says
  "Lens could not confirm this task was created — it is not visible yet. If
  it was created, it will appear on the board." It never says the task was
  not created. It links to the board (filtered to the project when there is
  one) and offers **Start again**: a POST with `intent=restart` and the full
  input, which re-renders the form with the input kept under a **new**
  request id — the operator's own decision to risk a duplicate. It makes no
  call and is not an attempt.
- **Answer and receipt.** Success is 303 to the new task's detail page with a
  receipt: "Created *\<type\>* *\<title\>*", who created it and in which
  project, and — for a submit that landed on an earlier one's task — that the
  form had already been submitted and no second task was created. The type,
  project and creator it states are those of the create that ran, so a
  resubmit of an edited form under the same id — or after the operator label
  was switched — still describes the task that exists; that resubmit's own
  attempt is recorded under the identity that sent it. The page
  below is the new task's own, read fresh, so its parent and predecessors are
  stated there. After a create with a parent or predecessors, those tasks
  leave the edge cache (`task.created` evicts only the new id), so their
  pages show the new edge at once.
- **Record.** The span is `lens.writes.create`; its `task_id` is the new id
  once known, and it carries `lens.write.request_id` and, for a submit that
  made no call, `lens.write.dedup` = `joined` / `remembered` (result `ok`; a
  span and audit attribute, never a counter label). The audit line's
  arguments are ids, type and lengths — `task_type`, `title_chars`,
  `description_chars`, `project`, `tag_count`, `parent_task_id`,
  `depends_on`, `gate_type` — never the title or description text; it has no
  expected or observed status. A Lens validation refusal is recorded
  `rejected` (`invalid_form`).

**Add a dependency** (T3-W8). Direction is the mistake this action exists to
prevent, so the operator never picks an edge's `from` or `to`: they pick a
**sentence** about the task whose page they are on and name the other task,
and the relation is restated before anything is written.

- **Affordance.** On an **open** task's detail page, in the header's action
  block, when an operator identity resolves: one small form — a sentence and
  the other task's id or prefix — that GETs the confirm step. The sentences
  (`relation_sentences`), each one edge: "This task is blocked by ▁"
  (`blocks`, other → this), "This task blocks ▁" (`blocks`, this → other),
  and, only when `task_type` is `gate` (a gate with no `metadata.gate_type`
  included), "▁ waits on this gate" (`waits_on_gate`, this gate → other).
  There is no `parent_child` sentence (a parent is set at create) and no
  `discovered_from`.
- **The confirm step** resolves the other task with `lithos_task_get` —
  Lithos resolves a prefix of at least six characters; an
  `ambiguous_id_prefix` lists its candidates (id and title, as text) under
  the input for the operator to retype, and `invalid_input` or an unmatched
  prefix puts Lithos's message there — and reads the focal task's edges
  **fresh** (its cache entry evicted, then one `lithos_task_edge_list` under
  the 5 s link-read deadline). If the relation is already there it says
  **"This relation already exists (added by *\<agent\>*, *\<date\>*);
  nothing was written."** — no parenthesis when the edge carries no
  `created_by` / `created_at` — and offers no form. Otherwise it restates the
  sentence with both titles and short ids, and what the relation means for
  readiness, by the blocker's status: an open blocker — "*B* will not be
  ready until *A* completes"; a completed one adds no wait; a cancelled one
  strands its dependent (`blocker_unsatisfiable`) until it is reopened; a
  waiter waits until its gate resolves — completed, or a timer gate past its
  `ready_at`, which is said when it already has. A dependent that is itself
  resolved is told it waits on nothing now. Its form posts the resolved
  **full** ids, the type and the focal task's status as `expected_status`.
- **The write** goes through the funnel (`expected_status` checked, the task
  must be open, and the relation must be one the task's page offers —
  `bad_relation` otherwise, with no call). Its `perform` re-reads the focal
  task's edges fresh, then: the relation already there is a **success that
  writes nothing** (`ok`, `lens.write.already_exists = true`, the same
  receipt sentence); otherwise one `lithos_task_edge_upsert(from_task_id,
  to_task_id, type, agent=<operator>)` with **no metadata** — the edge's
  `created_by` records the operator. **A fresh read that fails writes
  nothing** — an upsert could replace the metadata of an edge Lens could not
  see — and is refused 503 `precheck_failed`: "Lens couldn't check whether
  this relation exists — nothing was written." The funnel lets `perform`
  answer that refusal itself rather than classifying it as the write's own
  failure. A refusal from Lithos takes its §5.14 row: `cycle` (the message
  verbatim), `self_edge`, `not_a_gate`, `task_not_found` (a missing endpoint,
  with Lithos's message; the focal task gone is the conflict page) and
  `invalid_edge_type` (the Lens-defect path).
- **Convergence.** An edge write emits no event, upstream or Lens's own.
  After the upsert — applied, refused or unanswered — **both endpoints leave
  the edge cache**, so the page the redirect lands on renders from fresh
  reads; other tabs converge on the cache's 30 s TTL or their next event.
  An already-existing relation evicts the other endpoint (its entry may
  predate the relation).
- **Receipt.** "+ Added dependency: *A* blocks *B*" (or "*W* waits on gate
  *G*") and who added it; or "Already related: …" with the already-exists
  sentence. **No removal is offered:** a mis-drawn edge is removed outside
  Lens with `lithos_task_edge_delete` (Lithos 0.6.0).
- **Not serialised, and one residual.** Two concurrent submits of one
  relation may both upsert it — the same edge, the same (empty) metadata.
  Another agent inserting the same relation between Lens's fresh read and
  its upsert has its metadata replaced by Lens's; closing that needs an
  upstream `created` signal on the upsert response.
- **Record.** The span is `lens.writes.edge_upsert`; the audit arguments are
  `from_task_id`, `to_task_id` and `type`.

## 6. Current Lithos Dependencies

Lens currently assumes the availability of an existing Lithos deployment that
provides:

- a reachable base HTTP URL
- task listing and task-status read capabilities
- **task-graph reads**: the computed ready and blocked frontiers with
  classified blockers, task types (`task`/`epic`/`gate`), typed task edges, and
  children — the Lithos 0.4 surface the whole graph-native dashboard rests on
- note read, search, and neighborhood capability for the knowledge surface
- agent registry/statistics endpoints used by the dashboard, including the
  exact single-agent lookup (`lithos_agent_info`) the operator-identity guard
  reads and the typed registration it writes
- an `/events` SSE stream carrying task-related events
- **five task writes**, attributed to the operator: `lithos_task_complete`
  (with an `outcome`), whose `unblocked` id list a completion's receipt names,
  `lithos_task_reopen`, whose `reblocked` id list a completed task's reopen
  receipt names, `lithos_task_cancel` (with an optional `reason`),
  `lithos_task_create`, and `lithos_task_edge_upsert`, which emits no event
  (§5.15)
- id-prefix resolution in `lithos_task_get` (Lithos 0.5.0 and later), which
  the relation confirm step relies on to resolve the task the operator typed

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
- **Curated writes** — `lens_writes_total` by `action` and `result` (`ok` |
  `conflict` | `rejected` | `unknown` | `refused_origin` | `no_operator`), one
  per attempt (§5.15). The operator, the task and a rejection's code are span
  attributes, never labels.
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

Fake-Lithos app mode (`LITHOS_LENS_FAKE_LITHOS`) is backed by an in-memory
client that is **writable**, not just readable. Its fixture dataset stays
frozen — it is the demo artifact — and each client instance holds its own
overlay of what the Lithos write tools changed (statuses, outcomes and resolved
stamps, minted tasks, inserted edges and replaced edge metadata, released
claims, posted findings). Reads answer from seed plus overlay — a task the fake
minted included, which later writes complete, cancel and reopen like any seeded
one; `lithos_stats` keeps the fixture's figures but moves the coordination
counters it states (`active_tasks`, `open_claims`, `expired_claims`) by what
the writes changed — and the readiness oracle answers from seed plus overlay
too: a task nothing touched keeps the fixture's verdict verbatim, while one whose own status or whose blockers'
status moved is recomputed from the effective blocking edges — including the
one blocker whose answer is the clock's rather than a status's, an **open**
`timer` gate, which stops blocking once its `metadata.ready_at` has passed,
with or without a write (cancel that gate and its waiter is stranded again:
cancellation wins over the clock). As upstream, only open `task`-typed rows
reach either frontier — never a gate or an epic. So completing a gate changes
what the ready and blocked reads return and reports exactly the waiters that
are now ready, reopening it reports them re-blocked (upstream's rule: open
dependents whose only blocker is now the reopened task, and nobody after a
cancellation), and cancelling a predecessor leaves its dependent's blocker
unsatisfiable.

The refusals are upstream's too — the same envelopes, message text included,
raised in upstream's validation order — including its id domain: every id a
write carries goes through one resolver, where a full id (36 characters) is
handed to the calling tool's own lookup, a value shorter than six characters is
`invalid_input`, and anything between is searched as a prefix — matching
nothing is the resolver's own `task_not_found`, matching several is
`ambiguous_id_prefix` naming up to five `{id, title}` candidate records. What a
write stores is upstream's as well: a `timer` gate's `ready_at` is kept
rewritten to UTC at second precision, as upstream stores it. Each complete,
cancel and reopen commits an `updated_at` strictly after the task's previous
one even when the clock repeats or runs backward, exactly as upstream advances
it. Writes publish the event the real server emits — the same body, field for
field, including the pre-serialized fields upstream puts on a completion, and
nothing at all for an edge write, which emits no event upstream — through the
in-process hub, and each one, refusals included, is recorded in the instance's
write log. Two instances never share an overlay, so one test's write cannot
reach another's board.

The implemented tests exercise real behavior with lightweight fakes rather than
shallow mock-only checks, and the working practice is to demonstrate a new
guard fails when reverted rather than assuming it binds.

## 10. Known Gaps Relative to Requirements

The following requirement areas are not yet implemented in the current state:

- **removing a dependency edge** (T3 cut it, 2026-10-05): the curated write
  actions — posture and identity (§5.13), refusal copy (§5.14), the write
  funnel with its receipts, Complete and Proceed anyway, Reopen, Cancel,
  Create and Add a dependency (§5.15) — ship now, but an edge is removed
  outside Lens with `lithos_task_edge_delete`. Create's de-duplication is in
  memory only: there is no lookup of `metadata.lens_request_id` in Lithos, so
  a restart forgets the request ids it had seen (§5.15, Create).
- knowledge graph view and knowledge event wiring (K2)
- cognitive search (`lithos_retrieve`) and node stats (K3)
- feed, feedback, and cited-by panel (K4)
- archive-backed file serving and in-browser document viewing
- saved reading paths
- LLM-assisted curation, summaries, or browsing assistance (X1) — the LLM
  config block exists and is disabled by default; nothing consumes it
- authentication
- the planning view rebase, findings feed and operator ergonomics (T2b) —
  one piece of it is live, the Gates section's PR reconciliation state (§5.3)

One gap is narrower than a milestone and tracked as a task:

- the fake↔real contract matrix runs manually against a live server rather
  than on a schedule against a seeded one

This belongs to a future milestone and should not be assumed to exist merely
because they are described in `docs/REQUIREMENTS.md`.

## 11. Compatibility Statement

This specification describes the behavior of Lithos Lens `0.5.0` as currently
implemented in this repository — the 0.1.0 foundation, the **T1** graph-native
operator view and **K1** knowledge note view and search of 0.3.0, the **T2**
task relationship graphs of 0.4.0 (§5.10–§5.12, §5.6.1, §5.6.2), and the
**T3** curated write actions: the write posture, the operator identity and its
guard (§5.13, §5.1), the write-refusal copy (§5.14), and the write funnel with
its receipts and the five actions it carries — Complete and Proceed anyway,
Reopen, Cancel, Create and Add a dependency (§5.15). Removing an edge is the
one piece of the T3 contract not shipped (§10).

If the implementation and this document diverge, the implementation should be
treated as authoritative in the short term and this specification should be
updated to realign with shipped behavior.
