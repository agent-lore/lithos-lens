---
title: T3 — Curated Write Actions
milestone: T3
status: draft
target_version: 0.5.0
references:
  - docs/ROADMAP.md (milestone sequence; upstream dependency ledger — gaps #1, #2, #6)
  - docs/REQUIREMENTS.md §5C (curated write actions — the contract this PRD executes), §5B.1 (project conventions), §5.2.3 (gates), §13 (settings), §14 (degraded states), §15 (telemetry)
  - docs/SPECIFICATION.md §5.3–§5.8 (the shipped read-only surfaces the actions attach to)
  - docs/prd/t2-task-relationship-graphs.md (edge cache, side panel, downstream impact — reused here)
  - lithos src/lithos/tools/tasks.py, src/lithos/coordination.py (write tools; read at 0.5.0 @ a4d2d62 — see Further Notes)
tracked_in: lithos
task_tags: [project:lithos-lens, milestone:t3]
labels: [milestone-t3, tasks-view]
epic: (created when this draft is accepted)
depends_on: [T1, T2]
upstream: [bd66d57c (lithos-core task-edge delete — gates slice W8 only)]
---

# T3 — Curated Write Actions

## Problem Statement

Lens shows the operator exactly what needs them and then makes them leave to
do it. T1 put human gates at the top of the board and T2 made the graph
around a task legible, but every surface is read-only, so the operator's
actual moves happen somewhere else:

- **Resolving a human gate is the most frequent operator action and Lens
  cannot do it.** A loom run that needs a decision raises a human gate,
  Lens shows it within seconds, and the operator then ticks it in Obsidian
  or asks an agent to call `lithos_task_complete`. The board that announced
  the stop is not the place the stop is cleared.
- **Mistakes and dead ends are fixed by asking an agent.** Reopening a task
  completed by accident, reviving a cancelled blocker whose dependents are
  stranded (the standard remedy for Needs-attention rule 1), cancelling
  work that is no longer wanted — each is a one-line tool call the operator
  cannot make from the page that shows the problem.
- **Cancelling is blind.** A cancelled predecessor blocks its dependents
  forever. Nothing states that consequence before the cancel, and the
  operator finds out from the attention section afterwards.
- **Filing work means leaving the board.** A task noticed while reading the
  dashboard is created through an agent session or a GitHub issue, and its
  project is tagged under whichever convention that path happens to use.
- **Dependencies seen in the graph cannot be drawn in it.** T2 shows that
  two tasks should be ordered; recording it needs a tool call with the
  direction typed correctly by hand.

Three facts about Lithos shape everything below. Task writes have **no
compare-and-set** beyond "is it still open". **Edge writes emit no event**
(ledger #1) and, today, **cannot be undone** (ledger #2, `bd66d57c`). And
Lens has **no authentication** — the boundary is the trusted network, and a
write surface makes that boundary matter more than it did.

## Solution

Lens becomes an operator console for five actions and nothing else:
**complete a gate**, **reopen** a resolved task, **cancel** an open
task with its consequences stated first, **create** a task, epic or gate,
and **add a dependency** between two tasks. The actions are part of the
product from 0.5.0 — there is no read-only mode to switch back to (D2).

Every action is a plain form POST that works without JavaScript and answers
with a redirect to a page rendered from fresh reads. What the write returned
(the tasks a gate freed, the dependents a reopen re-blocked) is shown once,
as a **receipt** at the top of that page. Nothing is optimistic: the receipt
informs, the page states what Lithos says now.

Writes are attributed to a **named human operator** — never to the Lens
service agent — resolved from a cookie or a configured default and
registered in Lithos as `type="human"`. The current identity is shown beside
every affordance. Each attempt, successful or not, leaves one audit log line
and one span. When Lithos refuses a write, the operator reads a sentence
about what happened and what was (not) changed, never a bare code and never
a 500.

```
/tasks?project=lithos-loom                          Acting as dave · switch
┌──────────────────────────────────────────────────────────────────────┐
│ ✓ Completed gate "Decide: re-develop PR #431?" (a3f9c1)               │
│   Unblocked 2 tasks: Story S7 (c0ffee), Docs sweep (9b1d22)           │
│   [Reopen gate] — re-blocks its waiters; does not recall work started │
├──────────────────────────────────────────────────────────────────────┤
│ Gates                                                                │
│  ● human  Approve release notes (7e11aa)   2 tasks wait   [Complete] │
│  ● pr     lens#105 checks running          1 task waits  [Proceed…]  │
│ Ready                                                   [+ New task] │
│  ▸ Tag autocomplete (e8629b)                                    [⋯]  │
│      └ Cancel…  → "Cancelling strands 3 tasks directly, 2 more behind │
│                    them; releases 1 active claim (agent-zero)"        │
└──────────────────────────────────────────────────────────────────────┘
```

## User Stories

### Posture and identity

1. As a deployer, I want the page where the operator identity is set to
   state the trusted-network boundary, so that "anyone who can reach this
   port can perform these actions" is not a surprise found in a
   requirements document.
2. As an operator, I want to choose the name my actions are recorded under
   once, have it remembered, and see it beside every action, so that the
   audit trail names me and I always know who I am acting as.
3. As an operator on a deployment with `default_operator` set, I want to
   act without being prompted, so that a single-operator install has no
   ceremony.
4. As an operator, I want to switch identity from any page, so that a
   shared screen does not attribute someone else's actions to me.
5. As an operator, I want Lens to refuse an identity that belongs to an
   agent (or to Lens itself), so that I cannot attribute my actions to an
   agent or silently turn its registry entry into a human.
6. As an auditor, I want every operator registered in Lithos as
   `type="human"` before their first write, so that agent pickers and the
   Planning View's human definition see a typed human rather than an
   auto-registered blank.
7. As a deployer, I want a POST from another origin refused, so that a page
   open in another tab cannot drive Lens with my browser.

### Complete a gate

8. As an operator, I want a **Complete** action on every open gate that
   only a person resolves — human and external-task gates — on its row, in
   the side panel and on its detail page, so that the board that announced
   the wait is where I clear it.
9. As an operator, I want the gate's description visible where I act, so
   that I complete it knowing what its author says completing means (a
   loom gate's description names the choices; completing is the retry
   gesture).
10. As an operator, I want to add an optional one-line note that is stored
    as the gate's outcome, so that the decision carries its reason.
11. As an operator, I want to see which tasks the completion unblocked, by
    title, so that I know what I just set in motion.
12. As an operator, I want a **Reopen gate** action on that receipt, with
    its limits stated, so that a mis-click is correctable and I am not told
    it is an undo when work may already have started.
13. As an operator, I want a timer, CI or PR gate completable only through
    a **Proceed anyway** confirm step that names what would otherwise
    resolve it and which tasks it releases, so that overriding a machine
    wait is a decision I made and never a mis-click.
14. As an operator, I want that override recorded as one in the gate's
    outcome, so that the record does not claim the wait resolved on its
    own.
15. As an operator, I want no complete action on ordinary tasks, so that I
    cannot finish an agent's work for it.

### Reopen

16. As an operator, I want to reopen a completed task from its detail page
    and be told which dependents that re-blocked, so that an accidental
    completion is reversible with its side effect stated.
17. As an operator, I want to reopen a cancelled blocker and be told its
    dependents are waiting again rather than stranded, so that the
    standard remedy for a stranded task is one click and says what it did.
18. As an operator, I want the two cases worded differently before I act,
    so that I do not expect "re-blocked" when the effect is "un-stranded".

### Cancel

19. As an operator, I want cancel to show, before I confirm, how many open
    tasks it strands directly and how many more sit behind them, naming the
    first few, so that the consequence is a decision and not a discovery.
20. As an operator, I want the confirm step to say whose active claims the
    cancel releases, so that I do not pull work out from under an agent
    unknowingly.
21. As an operator cancelling an epic, I want to be told its open children
    are not cancelled with it, so that I do not assume a cascade that does
    not exist.
22. As an operator cancelling a gate, I want to be told that its waiters
    become unsatisfiable and that completing it is how they proceed, so
    that I do not use cancel to mean "never mind, go ahead".
23. As an operator, I want the consequence count labelled a lower bound
    when Lens could not read everything, so that a partial answer is not
    presented as the whole one.
24. As an operator, I want to give a reason and be told it is not stored on
    the task, so that I am not promised a record that will not exist after
    a reload.
25. As an operator without JavaScript, I want cancel to be a confirm page
    and a form, so that the consequence step cannot be skipped by a
    disabled script.

### Create

26. As an operator, I want a **New task** form on the dashboard for a task,
    epic or gate, pre-filled with the project I am filtered to, so that
    filing work does not mean leaving the board.
27. As an operator, I want the project written under both conventions, so
    that what I create is found by every filter and every agent.
28. As an operator, I want to name a parent and predecessors by short id or
    by picking from the tasks on the board, so that a new task lands in the
    graph already connected.
29. As an operator, I want **Add child** on an epic's detail page, so that
    the parent is filled in for me.
30. As an operator creating a gate, I want the gate type validated and
    `ready_at` required for a timer before anything is sent, with the form
    re-rendered and my input kept when Lithos still refuses it, so that I
    never retype a form to fix one field.
31. As an operator who double-submits or loses the connection mid-create, I
    want Lens to tell me whether the task was created rather than create it
    twice or leave me guessing.

### Add a dependency

32. As an operator, I want to add a relation from a task's detail page by
    completing a sentence — "this task is blocked by ▁", "this task blocks
    ▁", "this task's parent is ▁" — so that direction is chosen in words,
    not in `from`/`to`.
33. As an operator, I want a confirm step that restates the relation with
    both titles and what it means for readiness, so that a wrong-direction
    edge is caught before it is written.
34. As an operator, I want a refused edge explained — a cycle with its
    members, an existing parent, a self-edge, a non-gate — and told nothing
    changed, so that I can fix the request.
35. As an operator, I want to remove the edge I just added, from its
    receipt, so that a mistake in the one write that used to be permanent
    is correctable.
36. As an operator with the graph open in another tab, I want that tab to
    learn of the new edge, so that the picture is not silently stale after
    my own action.

### Every write

37. As an operator, I want a write on a task that changed since I loaded
    the page to be refused with its current status, so that I act on what
    is, not on what I saw.
38. As an operator, I want a network failure mid-write reported as "may or
    may not have applied" together with what Lens can see now, so that I
    neither assume success nor blindly retry.
39. As an operator, I want every page after a write rendered from fresh
    reads, so that what I see is Lithos's state and not Lens's guess.
40. As an auditor, I want one structured log line and one span per attempt
    — operator, action, task, argument summary, result — so that "who did
    this, through Lens" has an answer.

## Implementation Decisions

The decisions that needed Dave's call were settled with him on
2026-10-01 and are not re-opened per slice: no "writes off" mode (D2);
the Complete / Reopen wording and the split between person-resolved and
machine-owned gates (D7); `metadata.lens_request_id` for idempotent create
(D10); the edge slice gated on `bd66d57c` (D1, D11); and lithos-core
`6383a81b` is not a T3 dependency (Further Notes). The rest follow from
REQUIREMENTS §5C or from what the Lithos source does.

### D1. Scope: five actions, one detachable

0.5.0 ships complete-gate, reopen, cancel, create and add-dependency.
The first four have no upstream dependency. Add-dependency (slice W8) is
gated on lithos-core `bd66d57c` (task-edge delete) and is last in the
sequence: if upstream slips, 0.5.0 ships the other four and the edge slice
follows as 0.5.x. Nothing else in the milestone waits on it.

### D2. Posture: always on, no "writes off" mode

- **There is no `[writes] enabled` flag** (Dave, 2026-10-01). REQUIREMENTS
  §5C.1 specified writes as default-off with route gating; that is dropped.
  Lens is a single-operator console on a trusted network, its one deployer
  is its one operator, and a second posture would double the surface to
  specify and test — conditional route registration, affordance-free
  templates, a 404-versus-405 wrinkle on `/tasks/new` — to protect a
  deployment that does not exist. Write routes are registered like every
  other route group, and the affordances are part of the page.
- What still decides whether an affordance renders is the task's state and
  whether an operator identity resolves (D3) — never configuration.
- `new` joins the reserved task-path segments, so `/tasks/new` is the
  create form and never "the task whose id is `new`". Such an id stays
  reachable through the existing id-in-query alias.
- **Origin check.** Every POST Lens registers under this milestone requires
  an `Origin` header whose host matches the request's `Host` (falling back
  to `Referer` when `Origin` is absent); a mismatch or the absence of both
  is refused with 403 before any Lithos call. This is CSRF hygiene, not
  authentication, and the operator page says so.
- **Boundary statement.** With no off switch, the trusted-network boundary
  is the only protection there is, so it is stated where the operator
  meets it: on the operator page (D3). Settings is still a disabled
  placeholder (REQUIREMENTS §13 describes a future view) and T3 does not
  build it; when it exists it takes the statement over.

### D3. Operator identity

- **Resolution:** the `lens_operator` cookie, else `[writes].
  default_operator`, else none. The cookie is user input: it is validated
  on every read (lowercase slug, bounded length) and an invalid value is
  treated as absent.
- **No identity → no affordances.** When none resolves, write affordances
  are replaced by one "choose an operator to act" link to the operator
  page. A POST that still arrives without an identity is answered with a
  redirect to that page and **is not replayed** — the operator repeats the
  action once they have a name. Replaying a write the operator made under
  no identity is a write Lens decided to make.
- **Operator page** (`GET`/`POST /operator`): shows the current identity
  and where it came from, sets or switches it (cookie, one year, `HttpOnly`,
  `SameSite=Lax`), and carries the boundary statement. A `next` parameter
  returns the operator to where they were; it is accepted only as a
  same-origin relative path.
- **Registration precedes the first write.** Lithos auto-registers an
  unknown `agent` on any write, untyped. So before an identity's first
  write in this process, Lens registers it with `type="human"`; the set of
  verified identities is held in memory and rebuilt on demand after a
  restart. If registration fails the write is refused — "could not register
  the operator identity; nothing was changed".
- **Impersonation guard.** Re-registering an existing id with a type
  overwrites that agent's type upstream. Before registering, Lens reads the
  agent list: an id equal to the Lens service agent, or one that exists
  with a type other than `human`, is refused on the operator page ("that id
  belongs to an agent"). If the agent list cannot be read, a new identity
  is not accepted; one already verified keeps working.
- The identity is rendered near every affordance through one shared
  partial, with the switch link.

### D4. One write funnel

Every action goes through one function with one shape: resolve the
operator, run the pre-check, make the single Lithos call, classify the
result, write the audit line and the span, publish any synthetic event,
mint the receipt, and answer. Route handlers only parse their form and
choose where to redirect. A second path to Lithos for writes is the defect
this decision exists to prevent — the audit line and the pre-check are only
guarantees if nothing can go around them.

- **Pre-check (`expected_status`).** Every form on an existing task carries
  the status the operator saw. The funnel re-reads the task; a different
  status is answered with a conflict page — "this task is now *completed*
  (by agent-zero, 14:02)" — and no write. Complete additionally requires
  `task_type = gate` at that read (409 otherwise) and, for a machine-owned
  gate, the confirmation D7 describes.
  The window between the read and the write stays open; Lithos closes it
  for complete and cancel (both apply only to an `open` task) and for
  reopen (refuses an `open` one), and D6 turns those refusals into the same
  conflict page.
- **Dual-mode answer.** A plain form POST gets `303 See Other` to a page
  rendered from fresh reads — the form's `next` when it is a same-origin
  relative path, else the task's detail page. An HTMX POST (used only for
  row-level actions, so the operator keeps their place on the board) gets
  the receipt fragment and a trigger that runs the existing
  debounce-bypassing reconcile. Lens does not hand-assemble "the updated
  row": rows come from the same reconcile every event already drives.
- **Never optimistic.** A write result informs the receipt and nothing
  else.

### D5. Receipts

A redirect loses the response body, and the response body is where
`unblocked[]` and `reblocked[]` are. A small in-memory **receipt store**
(bounded count, short TTL) holds each write's outcome under a random id;
the redirect carries `?receipt=<id>` and the target page renders the
receipt as a banner above its content, with any follow-up action as an
ordinary form. An unknown or expired id renders nothing.

Receipts are feedback, not state: they make no claim the page below them
does not re-derive from fresh reads, and a restart loses them without
making anything wrong. That is the difference from the Lens-side trackers
the ROADMAP rejects (claim ledger, lifecycle tracker), which would be
believed after they went stale.

`unblocked[]` and `reblocked[]` are lists of **task ids**. The receipt
resolves titles from the snapshot the target page already loads, falls
back to a bounded number of `task_get` reads, and shows the short id alone
when a title cannot be had.

### D6. Error mapping, from what Lithos 0.5.0 actually returns

A pure, table-driven mapper turns `(action, error code, message, what a
re-read shows)` into operator copy and an HTTP answer. §5C.4's table is
extended to the codes the source raises:

| Code | Raised by | Operator-facing copy (shape) |
|---|---|---|
| `task_not_found` | all | Complete and cancel return this for "not found **or not open**" — one code for two facts. The funnel re-reads the task: if it exists, the conflict page ("this task is now *\<status\>*"); if not, "this task no longer exists". |
| `task_not_resolved` | reopen | Conflict page: "this task is already open." |
| `invalid_input` | create | The form re-rendered with the upstream message on the field it names, input kept. |
| `ambiguous_id_prefix` | create, edge | "‘\<prefix\>’ matches more than one task" with the envelope's `candidates` as choices. |
| `cycle` | edge | "This dependency would create a cycle. Nothing was changed." with the upstream message verbatim; task ids in it get the existing short-id link treatment as presentation only — Lens does not parse the message for logic. |
| `parent_exists` | edge, create | "*\<Task\>* already has a parent." plus the route to change it (D11). |
| `self_edge` | edge | "A task can't depend on itself." |
| `not_a_gate` | edge | "*\<Task\>* isn't a gate — only a gate can be waited on." |
| `invalid_edge_type` | edge | Treated as a Lens defect: the form offers only valid types. Unknown-code path, logged at error. |
| *(unknown code)* | any | Code and message verbatim with a "report this" hint. |
| *(no envelope: transport failure or timeout)* | any | "The action may or may not have applied." followed by what a re-read shows now (D10 covers create, where there is nothing to re-read by id). |

No write error is a 500. A refused write always says that nothing was
changed, because that is the fact the operator needs first.

### D7. Complete a gate

- **Gates only, split by who resolves them.** The route completes a task
  only when it is an open gate; there is no complete action for ordinary
  tasks. What the operator is offered depends on the gate type:

  | Gate type | Who resolves it | Lens offers |
  |---|---|---|
  | `human` | a person, by definition | **Complete** — one action |
  | `external_task` | whoever learns the outside wait is over; nothing in Lithos resolves it | **Complete** — one action |
  | `timer` | itself, at `ready_at` | **Proceed anyway…** — confirm page |
  | `ci`, `pr` | whatever watches the check or the PR | **Proceed anyway…** — confirm page |
  | anything else | unknown to Lens | **Proceed anyway…** — the cautious path |

  Human-only would have left the corpus's own gates without an action: an
  external-task gate ("resolve this when the boards arrive") is typed that
  way precisely so it does not nag like a human gate, and only a person
  can close it. And completing a gate is the one way to release its
  waiters — cancelling strands them (REQUIREMENTS §5.2.3).
- **Proceed anyway is a confirm page, like cancel.** `GET
  /tasks/{id}/approve` renders it; its form is the only one that carries
  the confirmation, and a POST for a machine-owned gate without it is
  redirected to the page rather than performed. The page states, from what
  Lens can read and nothing more:
  - what would otherwise resolve the gate — a timer's `ready_at` (and, when
    that has already passed, that the gate no longer blocks anything and
    completing only closes it); a PR gate's PR link; for CI and unknown
    types, the gate's own description;
  - the waiters completing it releases, by title, from the same read the
    gate row's waiter list uses, with that read's "at least N" and
    "unverified" labels carried over;
  - that whatever watches this gate will find it closed.

  Lens does not describe how the gate's author reacts. A loom `pr` gate's
  waiter is the story itself, so proceeding makes the story ready again
  while its PR is still open; loom's specification does not say what its
  watcher then does, and the page does not guess. It names the PR and the
  released waiters and leaves the decision with the operator.
- **The button says "Complete", not "Approve".** Completing a gate means
  what its author says it means. For a loom needs-human gate, completion is
  the retry gesture — it re-dispatches — and the gate's description names
  the operator's choices. Lens states what it does and shows the
  description beside the action; it does not put "approve" in the
  operator's mouth. The route keeps §5C.7's path.
- **Outcome.** The optional note is sent as `outcome`. With no note the
  outcome is "Completed via Lens by \<operator\>" for a direct completion
  and "Completed early via Lens by \<operator\> — proceed anyway;
  \<gate type\> gate had not resolved" for an override, so the gate row in
  Lithos records how it was resolved and never claims a wait ended that
  did not.
- **Receipt.** "Unblocked N tasks" with the first few titles, and a
  **Reopen gate** form. It is labelled a reopen, not an undo, with one
  line of why: it re-blocks the waiters but does not recall anything their
  agents started in between (a loom resolver reacts to the completion
  event within seconds).
- **Surfaces:** the gate row (Gates section and wherever attention
  promotes it — one partial), the side panel, the detail page. The
  Planning View's human-gate queue is T2b; it reuses the same partial when
  it exists.

### D8. Reopen

Offered on the detail page of a completed or cancelled task, and as the
follow-up on a completion receipt. The copy differs by prior status, before
and after the write:

- **Completed →** "Reopening puts this task back to open. Tasks that became
  ready when it completed will be blocked again." Receipt: "Re-blocked N
  dependents" with titles.
- **Cancelled →** "Reopening returns this task to open. Its dependents stop
  being permanently blocked and wait on it again." Receipt: "N dependents
  are waiting on this again" — computed from the task's active outgoing
  dependency edges, because `reblocked[]` is empty by design in this case.

Lithos records the reopen as a finding and clears the outcome; the confirm
copy says the prior outcome is kept in that finding.

### D9. Cancel, with consequences

- **Confirm page.** `GET /tasks/{id}/cancel` renders the consequences and
  the form; `POST` performs the cancel. With `confirm_cancel = false` the
  affordance posts directly and the same facts appear on the receipt
  instead.
- **Consequences** come from one module — a bounded downstream walk from
  the task over **active** `blocks` and `waits_on_gate` edges (T2's edge
  states), through the per-task edge cache, with the focal task's own entry
  re-read first:
  - *Stranded directly:* open dependents one hop out. They become
    permanently blocked.
  - *Behind them:* further open transitive dependents.
  - The walk crosses project boundaries — a cancel strands whoever depends
    on the task, not only tasks in the same scope — and is bounded by a
    node budget. Over budget, or on any failed edge read, both numbers are
    lower bounds and are rendered "≥ N" with the reason, the T2 rule.
- **Also stated:** the active claims the cancel releases, by agent; for a
  task with open children, that they are not cancelled; for a gate, that
  its waiters become unsatisfiable and that completing it is how they
  proceed (REQUIREMENTS §5.2.3).
- **Reason.** Optional; sent; labelled "recorded in the event stream only —
  not stored on the task" (ledger #6).
- **Surfaces:** the detail page and the row's overflow menu, both as a
  link to the confirm page.
- No future-tense consequence is shown for a task that is not open; the
  pre-check answers that case.

### D10. Create

- **One form** (`GET`/`POST /tasks/new`) for task, epic and gate: title,
  type, description (Markdown, as descriptions now render), project, tags,
  parent, predecessors, and a gate fieldset (gate type; `ready_at` for a
  timer). The no-JS form shows the gate fieldset labelled "only for gates";
  script hides it for the other types.
- **Pre-fill from context:** `?project=` from the board's filter,
  `?parent=` from an epic's **Add child** link.
- **Project is written under both conventions** — `metadata.project` and
  the `<project_tag_key>:<slug>` tag — per §5B.1.
- **Parent and predecessors** are entered as full or short ids, with a
  datalist of the board's open tasks (title and short id) as enhancement.
  Lithos resolves prefixes; an ambiguous one comes back with candidates
  (D6). No new search endpoint.
- **Validation** runs in Lens before the call — title present, type known,
  gate type in the five, `ready_at` present and parseable for a timer —
  and Lithos remains the authority: its `invalid_input` re-renders the form
  with input kept.
- **Idempotent on a request id.** The form carries a random request id,
  written to the task as `metadata.lens_request_id`. Before creating, and
  after a transport failure, Lens looks for a task carrying it: a
  double-submit lands on the task already created, and "may or may not
  have applied" becomes "it was created" or "it was not".
- **After success:** redirect to the new task's detail page with a receipt.

### D11. Add a dependency

- **Sentence forms**, on the task's detail page. Each maps to one edge:
  "this task is blocked by ▁" and "this task blocks ▁" (`blocks`), "this
  task's parent is ▁" and "▁ is a child of this task" (`parent_child`),
  "this task was discovered from ▁" (`discovered_from`), and, on a gate's
  detail page only, "▁ waits on this gate" (`waits_on_gate`).
- **Two steps.** The first resolves the other task and renders the relation
  back with both titles and its readiness meaning ("*B* will not be ready
  until *A* completes"); the second writes. Direction is the mistake this
  form exists to prevent, so it is restated before the write, not after.
- **Removal is the receipt's follow-up.** With `bd66d57c` landed, the
  receipt offers "Remove this dependency", and a `parent_exists` refusal
  names the existing parent with the way to replace it. General edge
  management — removing arbitrary edges from the relations list — is not in
  T3. The delete tool's contract is transcribed from the Lithos source when
  it exists; this PRD does not describe its shape.
- **Convergence.** Edge writes emit no upstream event. After a successful
  edge write the hub publishes the synthetic `lens.edge_upserted` event
  (both endpoint ids and the type), which evicts both endpoints from the
  edge cache and reaches every tab through the normal SSE path: boards
  reconcile, a graph page showing either endpoint shows its "graph changed"
  pill. Only the hub mints `lens.*` events; the funnel asks it to.

### D12. Client surface, contracts, and a fake that changes

- **Client:** five write methods plus operator registration, each returning
  a typed result record and raising the existing coded tool error. They
  are called only by the funnel.
- **Contracts:** one vendored contract per write tool
  (`lithos_task_complete`, `_reopen`, `_cancel`, `_create`,
  `_edge_upsert`), transcribed from the Lithos source with citations, and a
  human-registration request variant on the existing
  `lithos_agent_register` contract. Every error envelope in D6 appears in
  the contract that can raise it. `make contracts-verify` is run against a
  live server when they are added.
- **The fake becomes mutable.** Its seed dataset stays frozen; each fake
  instance holds a mutable overlay that the writes change, and the fake's
  readiness oracle answers from seed plus overlay. Writes return the
  canonical payloads — `unblocked` and `reblocked` computed by the oracle,
  every D6 error reachable — and emit the same events the real server
  does through the fake-mode hub (and none for an edge write). This is the
  largest piece of test infrastructure in the milestone and the reason the
  action slices can be thin.

### D13. Modules

Deep modules, each testable without a browser: **operator identity**
(resolve, validate, guard, register-once), the **write funnel**, the
**error mapper** (pure), the **receipt store**, **cancel consequences**
(over the existing edge-cache and scope types), the **create-form model**
(form → validated create request, both project conventions) and the
**relation sentences** (sentence → `from`/`to`/`type` and back). Write
routes are their own route group, registered like the graph and knowledge
groups. New modules are mapped in `docs/architecture.toml` in the slice
that adds them; where a budget moves, the slice argues the raise or the
extraction on the merits, in the diff.

### Config

```toml
[lithos-lens.writes]
default_operator = ""    # identity used when no cookie is set
confirm_cancel = true    # consequence confirm page before a cancel
```

Env overrides follow the shipped convention
(`LITHOS_LENS_WRITES_DEFAULT_OPERATOR`, `…_CONFIRM_CANCEL`).
`default_operator` is validated at load like the cookie. There is no
`enabled` key (D2). No new `[graph]` knob: the consequence walk uses
the existing page-scope bound as its node budget.

### Routes

| Endpoint | Purpose |
|---|---|
| `GET /operator`, `POST /operator` | Show, set or switch the operator identity; boundary statement |
| `GET /tasks/{task_id}/approve`, `POST /tasks/{task_id}/approve` | Proceed-anyway confirm page (machine-owned gates); complete a gate (409 for a non-gate) |
| `POST /tasks/{task_id}/reopen` | Reopen a completed or cancelled task |
| `GET /tasks/{task_id}/cancel`, `POST /tasks/{task_id}/cancel` | Consequence confirm page; cancel |
| `GET /tasks/new`, `POST /tasks/new` | Create task / epic / gate |
| `GET /tasks/{task_id}/edges/new`, `POST /tasks/{task_id}/edges` | Relation confirm step; add the edge |
| `POST /tasks/{task_id}/edges/remove` | Remove an edge Lens just added (W8, with `bd66d57c`) |

Any read page accepts `?receipt=<id>`.

### MCP / SSE dependencies

New client calls: `lithos_task_complete`, `lithos_task_reopen`,
`lithos_task_cancel`, `lithos_task_create`, `lithos_task_edge_upsert`, and
— in W8 — the edge-delete tool `bd66d57c` ships. Existing calls used by
the funnel: `lithos_task_get`, `lithos_task_status`,
`lithos_task_edge_list`, `lithos_agent_list`, `lithos_agent_register`,
`lithos_task_list` (create's request-id lookup, by `metadata_match`).
Upstream events already cover complete, cancel, reopen and create; the hub
gains one synthetic event (`lens.edge_upserted`).

### Telemetry

One span per attempt, `lens.writes.<action>` (`complete`, `reopen`,
`cancel`, `create`, `edge_upsert`, `edge_remove`): operator, task id,
for `complete` the gate type and whether it was an override, and the
result (`ok`, `conflict`, `rejected` with the code, `unknown` for a
transport failure, `refused_origin`, `no_operator`). One counter by action
and result — the operator is a span attribute, not a metric label. One
structured audit log line per attempt: operator, action, task id, argument
summary (ids, type, lengths — not description text), the status expected
and observed, and the result envelope.

## Testing Decisions

Same bar as T1 and T2: no coverage number; every listed behaviour has a
test that fails when it is reverted; tests assert on what a request
returns and on the fake's recorded calls, not on internals. Acceptance is
written against server-rendered text and the call log, because loom's
review gate is hermetic and headless.

- **Posture:** a POST with a foreign `Origin`, or with neither `Origin`
  nor `Referer`, is 403 with no Lithos call (call log); `/tasks/new` is
  the create form and never routed as a task id.
- **Identity:** cookie beats default beats none; an invalid cookie is
  absent; no identity renders the single "choose an operator" link and no
  forms; a POST without identity redirects to the operator page and makes
  no write; first write registers `type="human"` exactly once per process
  (call log) and a second write does not; the service agent's id and an
  id held by a non-human agent are refused; an unreadable agent list
  refuses a new identity; `next` to another origin is ignored.
- **Funnel:** stale `expected_status` → conflict page naming the current
  status, no write call; a transport failure → the "may or may not have
  applied" page with the re-read status; every attempt, including each
  refusal, emits exactly one audit line and one span with the right
  result; a plain POST answers 303 and the target renders the receipt; an
  HTMX POST answers the receipt fragment with the reconcile trigger.
- **Error mapper (pure, table-driven):** every row of D6, including
  `task_not_found` splitting on the re-read, `ambiguous_id_prefix`
  rendering its candidates, and an unknown code rendered verbatim.
- **Contracts:** the five new files pass the existing contract suite —
  canonical requests equal the recorded outbound arguments, success
  payloads round-trip, each error envelope surfaces as its coded error.
- **Fake:** completing a gate changes what the ready and blocked reads
  return and reports exactly the waiters that became ready; reopening it
  reports them re-blocked; cancelling a predecessor makes its dependent's
  blocker unsatisfiable; reopening a cancelled predecessor reports nothing
  re-blocked; an edge that closes a cycle is refused with `cycle`; two
  fake instances do not share an overlay.
- **Complete:** offered as one action on an open human or external-task
  gate on the row, the panel and the detail page; completing a plain task
  by URL is 409; the note becomes the outcome and the default outcome
  names the operator; the receipt lists the unblocked titles and offers
  Reopen gate.
- **Proceed anyway:** a timer, CI or PR gate — and a gate whose type Lens
  does not know — shows the link and no direct form; a POST for one
  without the confirmation redirects to the confirm page and makes no
  write (call log); the page names the timer's `ready_at`, or the PR
  link, and the waiters it releases with their read's labels; a timer
  already past `ready_at` says the gate no longer blocks anything; a
  confirmed POST completes the gate and the default outcome says "early"
  and names the gate type; the audit line and span record the override.
- **Reopen:** completed → receipt names the re-blocked dependents;
  cancelled → receipt names the dependents now waiting, and the two
  confirm copies differ; reopening an already-open task is the conflict
  page.
- **Cancel:** the confirm page for the fake's depth-5 chain states the
  direct and transitive counts and names the first few; a cross-project
  dependent is counted; a failed edge read renders "≥"; active claims are
  listed by agent; an epic with open children says they are kept; a gate
  says its waiters become unsatisfiable; the reason field carries the
  not-stored note; with `confirm_cancel = false` the POST succeeds without
  the GET and the receipt carries the same facts.
- **Create:** project lands under both conventions (call log); a timer
  gate without `ready_at` is refused before any call; an upstream
  `invalid_input` re-renders with input kept; an ambiguous parent prefix
  renders candidates; a repeated request id creates one task; `?parent=`
  and `?project=` pre-fill.
- **Edge:** each sentence produces the right `from`, `to` and `type` (call
  log); the confirm step renders both titles and the readiness sentence;
  each refusal renders its copy and says nothing changed; a successful
  write evicts both endpoints from the edge cache and publishes
  `lens.edge_upserted`; a graph tab showing either endpoint shows the pill
  (JS test in the existing pattern); the receipt's remove action removes
  the edge.
- **Visual (e2e, through loom's artifact review):** a gate row with the
  action and identity; a completion receipt; the cancel confirm page; the
  proceed-anyway confirm page; the create form with the gate fieldset;
  the relation confirm step.

## Tracer-bullet vertical slices

Nine slices. "Independent" means dispatchable at milestone start. Each
slice updates `docs/SPECIFICATION.md` for what it ships and passes
`make check && make diagrams` with no generated drift.

1. **W1 Posture and operator identity.** `[writes]` config and env
   overrides; the write route group; the Origin check; reserved `new`;
   the operator page with the boundary statement; cookie and default
   resolution; the impersonation guard and register-once; the identity
   partial in the page chrome. *Independent.*
   Acceptance: the Posture and Identity cases in Testing Decisions, in
   full.
2. **W2 Error mapper.** The pure mapper for every row of D6 and the
   conflict / unknown-outcome page templates it feeds. *Independent.*
   Acceptance: the Error mapper cases; each rendered page states whether
   anything was changed.
3. **W3 Lithos write surface.** The five client methods and their result
   records; the five contracts and the registration variant; the mutable
   fake with oracle-computed `unblocked` / `reblocked`, every D6 error, and
   event emission. *Independent.*
   Acceptance: the Contracts and Fake cases; `make contracts-verify`
   recorded in the PR.
4. **W4 Complete a gate.** The write funnel (pre-check, call,
   classification, audit line, span, counter, dual-mode answer), the
   receipt store and banner, and the direct Complete action for human and
   external-task gates on the three surfaces with note and default
   outcome. Every other gate type is refused by the route until W4b.
   *Needs W1, W2, W3.*
   Acceptance: the Funnel and Complete cases, except the receipt's Reopen
   form (W5).
   - **W4b Proceed anyway.** The confirm page and its confirmation
     field, the per-type "what would resolve this" statement, the waiter
     list, the override outcome, and the link on the three surfaces for
     timer, CI, PR and unknown gate types. *Needs W4.*
     Acceptance: the Proceed anyway cases.
5. **W5 Reopen.** The reopen route and detail-page action with the two
   copies; the cancelled-case "now waiting" count; Reopen gate on the
   completion receipt. *Needs W4.*
   Acceptance: the Reopen cases; completing then reopening a fake gate
   leaves the board as it started and the second receipt names the same
   tasks the first did.
6. **W6 Consequence-aware cancel.** The consequences module, the confirm
   page, the POST, the detail and overflow-menu links,
   `confirm_cancel = false`. *Needs W4.*
   Acceptance: the Cancel cases.
7. **W7 Create.** The form model, the form with gate fieldset and
   pre-fill, both conventions, pickers, validation, request-id
   idempotency, New task on the dashboard and Add child on epic detail.
   *Needs W4.*
   Acceptance: the Create cases.
8. **W8 Add a dependency.** Relation sentences, the two-step form, the
   refusals, the synthetic event and cache eviction, removal from the
   receipt with the delete tool's contract. *Needs W4; blocked upstream by
   `bd66d57c`.*
   Acceptance: the Edge cases.

Ready at milestone start: **W1, W2, W3**. After W4, **W4b, W5, W6 and W7**
are independent of each other. W8 is last and detachable (D1).

## Out of Scope

- **Completing ordinary tasks** — agents finish their own work.
- **Describing what a gate's author does when it is completed by hand** —
  the proceed-anyway page states what Lens can read (D7), not loom's or
  any other watcher's reaction.
- **Claim, renew, release** — agents manage their own claims.
- **Editing titles, descriptions or tags** (`lithos_task_update`), and
  **deleting tasks** (no such tool).
- **General edge management** — removing or re-typing arbitrary edges, and
  re-parenting as a single action. T3 removes only an edge it just added.
- **Bulk operations.**
- **Authentication or authorization** — the boundary is the trusted
  network, stated on the operator page.
- **A read-only mode** — no `[writes] enabled` flag and no route gating
  (D2).
- **The Settings view** — still a placeholder; T3 puts the boundary
  statement on the operator page instead.
- **The Planning View's human-gate queue** — T2b; it inherits the action.
- **Persisting cancel reasons** — ledger #6; Lens does not post a finding
  to carry one.
- **Knowledge writes** (feedback, conflict resolution) — K4 and the
  deferred pool; they inherit this funnel and identity contract.
- **Server-side recompute and desktop notifications** — X1.

## Further Notes

- **Facts read from the Lithos source** (0.5.0 @ `a4d2d62`, 2026-10-01),
  which the contracts must cite rather than this list:
  complete and cancel apply only to an `open` task and answer
  `task_not_found` for both "missing" and "not open"; both release every
  claim on the task; reopen answers `task_not_resolved` for an open task,
  posts a `[Reopened]` finding and clears the outcome; `unblocked` and
  `reblocked` are lists of task ids; cancel's `reason` reaches the log and
  the event only; create and edge-upsert resolve id prefixes and can
  answer `ambiguous_id_prefix` with `candidates`; edge-upsert can also
  answer `invalid_edge_type`, and its `cycle` message names members by full
  id; cancelling does not cascade to children; every write auto-registers
  an unknown `agent` untyped; re-registering an id with a type overwrites
  the stored type.
- **`6383a81b` (`lithos_task_blocked(task_id)`) is not a T3 dependency.**
  The September state review listed it as one. No slice reads it: Lithos
  returns `unblocked[]` / `reblocked[]` itself, and the cancel walk and
  the reopen count use edges.
- **Normative docs updated with this PRD**, so the contract and its
  execution plan agree. REQUIREMENTS §5C was rewritten: no `[writes]
  enabled` flag (5C.1), the gate-type split and the Complete / Reopen
  wording (5C.2), the write funnel and receipts (5C.3), the error codes
  Lithos actually returns (5C.4), the operator page, no-replay rule and
  impersonation guard (5C.5), and the operator, proceed-anyway and
  relation-confirm routes (5C.7). The flag and the "human gates only"
  wording were also removed where the rest of the document repeated them
  — the goals and design-decision table, the §4 config block and env
  listings, the startup contract, the gate-row and detail-page tables,
  the Planning human-gate queue, §5.11, the knowledge-write sections,
  §13, §15 and §16. ROADMAP §3 (the T3 row and paragraph) and ledger #2
  changed with them. `docs/SPECIFICATION.md` describes shipped behaviour
  and changes slice by slice.
- **Candidate upstream asks** surfaced while drafting: a distinct code for
  "not open" on complete and cancel; structured members on the `cycle`
  envelope; a registration that cannot silently re-type an existing agent.
  None gates a slice.
- **Why the fake is a slice of its own.** Every action's acceptance is
  "the board afterwards says X". That needs a fake whose reads change when
  it is written to, and it needs it before the first action slice, or each
  one invents its own partial mutation.
- **Why the text carries the acceptance criteria.** As in T2: a slice whose
  only observable output is an interaction gives a hermetic review nothing
  to assert. Every action here has a no-JS path, so every action has a
  request and a rendered page to test.
- **Rollout.** With no flag, each action reaches production with the
  deploy that carries its slice; W4 is the first deploy after which a
  browser on the network can change the task store. The one deployment
  step is setting `LITHOS_LENS_WRITES_DEFAULT_OPERATOR` in the gitignored
  prod env file, so the operator is not prompted.
- **Spec drift.** When T3 ships, `docs/SPECIFICATION.md`'s "every surface
  is read-only" statements and its no-authenticated-routes paragraph are
  rewritten, and the user manual regenerated.
