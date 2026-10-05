---
title: K2 — Knowledge Graph View
milestone: K2
status: draft
target_version: 0.6.0
references:
  - docs/ROADMAP.md (milestone sequence; upstream dependency ledger — gaps #1, #8, and the new #13–#16)
  - docs/REQUIREMENTS.md §8 (Knowledge Graph View — the contract this PRD executes, rewritten in the same PR), §6.5 (related panel "open in graph"), §11 (conflict resolution — read side only here), §13 (settings), §14 (degraded states), §15 (telemetry)
  - docs/SPECIFICATION.md §5.7 (the shipped knowledge surface), §5.8 (live updates), §5.10–§5.12 (the task graph: cache, scope, page — the patterns reused here)
  - docs/prd/k1-knowledge-note-view.md (note page, related panel, resolver — K2 attaches to them)
  - docs/prd/t2-task-relationship-graphs.md (text baseline first, Cytoscape as enhancement, caps as refusals — the posture K2 inherits)
  - lithos src/lithos/tools/memory_edges.py, read_search.py, edge_store.py, events.py, lcma/edge_inference.py (read at 0.6.0 @ d2c49bb, 2026-10-05 — see Further Notes)
tracked_in: lithos
task_tags: [project:lithos-lens, milestone:k2]
labels: [milestone-k2, knowledge-browser]
epic: (created when this draft is accepted)
depends_on: [K1, T2]
upstream: []   # nothing gates a slice; four asks are recorded in the ledger
---

# K2 — Knowledge Graph View

## Problem Statement

The corpus is a graph and Lens shows it one note at a time. Lithos holds
5,818 notes and **9,442 typed edges** between 3,893 of them (62% of notes
carry at least one), inferred by the LCMA enrichment pass and reinforced by
agents citing notes together. K1 made each note a readable document with a
related panel listing its own edges by title, type and weight — one hop,
as text, at the bottom of a long page. Nothing shows the shape around it:

- **Neighbourhoods are invisible.** A note's related panel says it
  `supports` three notes and is `refined` by one. Whether those four form a
  cluster, whether two of them contradict each other, whether the note is a
  hub or a leaf — none of that can be seen without opening each neighbour
  in turn. The related panel is a list; the question is a picture.
- **Contradictions have no queue.** 129 `contradicts` edges exist and 128
  of them are unreviewed. They are the one edge type that asks a human for
  a decision, and Lens has no page that lists them, shows the two notes side
  by side, or says which clusters they sit in. The conflict-resolution write
  is deferred to the pool (REQUIREMENTS §11), but the *read* — what is in
  conflict, and why Lithos thinks so — has no surface either.
- **Inferred edges carry a rationale nobody can read.** Every inferred edge
  stores `evidence = {rationale, model, confidence}` — the sentence that
  explains why two notes were linked. Lens shows the type and weight and
  drops the sentence.
- **The vocabulary on the page is not the vocabulary in the store.**
  REQUIREMENTS §8.3 colours `builds_on`, `uses_method` and `analogous_to`;
  none exists. The live types are `supports`, `related_to`, `analogy_to`,
  `refines`, `is_example_of`, `depends_on`, `derived_from`, `contradicts`,
  plus three stragglers — and the type column is a free-form string, so
  the list is open. A graph page that hard-codes the spec's palette draws
  the wrong legend on day one.
- **Edge changes are not live.** Lens's event hub deliberately drops the
  knowledge `edge.upserted` event, so an enrichment pass that adds forty
  edges to the note on screen changes nothing until a reload.

Four facts about Lithos shape everything below. **There is no bulk
neighbourhood or subgraph tool**: `lithos_related` is one node per call
and returns no titles on its edges, and `lithos_edge_list` has no limit,
offset or ordering — with no filter it returns the whole table (9,442 rows,
4.9 MB) in one payload. **Edge types are unvalidated strings**; the known
ones split into directed (`supports`, `refines`, `is_example_of`,
`depends_on`, `derived_from`) and symmetric (`related_to`, `contradicts`,
`analogy_to`, stored with `from_id <= to_id`). **Edges outlive notes**:
deleting a note leaves every edge to it dangling. And **salience is flat**:
90% of nodes sit at the 0.30 floor, so sizing nodes by salience (§8.3 as
written) would draw a graph of identical circles.

## Solution

Lens gets `/knowledge/graph`, built the way `/tasks/graph` was: a complete
server-rendered **text baseline** that a hermetic reviewer can assert
against, with a **Cytoscape canvas** drawn from the same embedded payload as
enhancement. Two modes, one page:

- **Focus mode** (first-class): `?focus=<note-id>` renders the ego-graph of
  one note — its typed edges one or two hops out, its wiki-links and
  provenance one hop out — with every neighbour named, typed and coloured by
  what it is. This is the mode note pages link to.
- **Scoped global mode**: `?type=contradicts`, `?namespace=<ns>` or both
  renders every edge in that slice of the table. An unscoped request renders
  a **scope picker** built from live facets — each edge type and namespace
  with its edge count, and the unresolved-contradictions count — because a
  9,442-edge hairball is not a view of anything.

Both modes read from one **edge-table snapshot**: Lens fetches the whole
edge table with a single unfiltered `lithos_edge_list` call, holds it
server-side under a TTL with single-flight, patches it from `edge.upserted`
events, and answers every graph read from it. One call replaces the N
per-node `lithos_related` fan-out REQUIREMENTS §8.2 planned; the snapshot is
also the only way to build the picker's facets, since no tool enumerates
types or namespaces. Node titles, types, status and namespace come from a
capped, cached `lithos_read(id, max_length=1)` fan-out — the same cheap read
K1's related panel uses — kept fresh by `note.*` events, which carry the
title.

Clicking a node opens a **node panel**; clicking an edge opens an **edge
panel** that shows the stored rationale, model and confidence for an
inferred edge, and for a `contradicts` edge puts both notes' ledes side by
side under the conflict's state. Resolving the conflict is not in K2 — it
is the first knowledge write and stays in the deferred pool — but the panel
is shaped so that the pool's resolve action drops into it without a
redesign.

## User Stories

### Seeing a neighbourhood

1. As a reader on a note page, I want an **Open in graph** link, so that
   the related panel's list becomes a picture of the same neighbourhood.
2. As a reader, I want the graph around a note to show its typed edges with
   their **type, direction and weight** legible — arrowheads on directed
   types only, a thinner line for a weaker edge — so that "A supports B" and
   "A is analogous to B" read differently.
3. As a reader, I want every node **named by its title** and coloured by
   its **namespace** (or, on a toggle, its note type), with its status
   visible when it is not `active`, so that I can tell a quarantined
   hypothesis in `research/` from a shared summary in `influx`.
4. As a reader, I want a **depth control** (one hop by default, two on
   request) and to see how many nodes each depth would draw before I ask
   for it, so that a hub with fifty neighbours does not surprise me with
   five hundred.
5. As a reader, I want **wiki-links** drawn thin and grey and
   **provenance** (`derived_from`) drawn dotted, distinct from inferred
   typed edges, so that structure an author wrote is told apart from
   structure a model inferred.
6. As a reader, I want a **minimum-weight filter** that hides faint edges by
   default and states what it hid ("38 edges below 0.1 hidden"), so that the
   2,000-odd consolidation edges at weight 0.03 do not bury the signal.
7. As a reader, I want a **provenance filter** (inferred / consolidation /
   authored / frontmatter) so that I can see only what a person asserted,
   or only what the model guessed.
8. As a reader, I want **search within the drawn graph** that highlights
   matching titles, and a **focus** interaction that lights a node's
   neighbours and dims the rest, as on the task graph.
9. As a reader, I want **Centre on this** on any node to re-draw the ego
   graph around it, so that walking the graph is a sequence of clicks.
10. As a reader, I want a node whose note no longer exists drawn as a
    **missing-note ghost** (bare id, dashed outline, no title) rather than
    dropped or faked, so that a dangling edge is visible as what it is.

### Reading an edge

11. As a reader, I want to click an edge and read **why it exists**: for an
    inferred edge the stored rationale sentence, the model and its
    confidence; for a reinforced `related_to` edge, that it was built by
    agents citing both notes together and how strongly; for a frontmatter
    `derived_from` edge, that the author declared it.
12. As a reader, I want the edge panel to show **both endpoints** by title
    with their ledes, each a link to its note page, so that I can judge the
    relation without leaving the graph.

### Contradictions

13. As an operator, I want `/knowledge/graph?type=contradicts` to render
    the **contradictions queue** as its text baseline — every `contradicts`
    edge, unresolved first, each as "A contradicts B", with namespace, weight
    and the rationale — so that the one edge type needing a human is a list
    I can work down.
14. As an operator, I want unresolved contradictions drawn **red and
    dashed**, resolved ones muted and labelled with their resolution
    (`accepted_dual` / `superseded` / `refuted` / `merged`), and a count of
    unresolved ones in the page toolbar, so that the state of the queue is
    visible from the canvas.
15. As an operator, I want the edge panel for a `contradicts` edge to put
    the two notes' ledes **side by side** under the conflict state, so that
    the two-pane read REQUIREMENTS §11 asks for exists before the resolve
    action does.
16. As an operator, I want a note page whose note is an endpoint of an
    unresolved contradiction to say so in a **banner** linking to the edge,
    so that I meet the conflict where I read the note.

### Scope and limits

17. As an operator, I want an unscoped `/knowledge/graph` to show a **scope
    picker**: each edge type and each namespace with its edge count, the
    unresolved-contradictions count, and a search box to choose a focus
    note — so that the first click lands on something drawable.
18. As an operator, I want a scope that would exceed the node cap
    **refused with its count** and the filters that would bring it under,
    never rendered degraded or truncated silently.
19. As an operator, I want the page to state **when its edge table was
    fetched** and that it may be up to the TTL stale, so that a freshly
    inferred edge that is not yet drawn is explained rather than mysterious.

### Freshness

20. As a reader with the graph open, I want a **"graph changed — refresh"**
    pill when an edge in my scope is upserted or a note in it is updated,
    renamed or deleted, rather than a canvas that re-lays itself out under
    my cursor.
21. As a reader, I want a note title that changes upstream to be right the
    next time it is drawn, without waiting for a cache to expire.

### Every page

22. As a reviewer with no JavaScript, I want the whole graph as **text**:
    the focus note, then each neighbour grouped by relation with its title,
    direction and weight, then the wiki-links and provenance, then the
    hidden-edge counts — so that the page is complete without the canvas.
23. As an operator, I want every read that fails to cost **only its own
    section** — a title fan-out that times out leaves nodes labelled by id,
    a `lithos_related` failure drops the wiki-link layer with a note — so
    that one slow backend never blanks the graph.

## Implementation Decisions

### D1. Scope: two modes, one snapshot, no writes

K2 ships the focus mode, the scoped global mode, the scope picker, the
node and edge panels, the contradictions queue (as the `type=contradicts`
text baseline), the note-page entry points and banner, and the knowledge
event wiring. It ships **no write**: conflict resolution stays in the
deferred pool, and the edge panel is designed so the pool's action is an
addition, not a redesign (D10). It ships **no salience** (D8) and **no
centrality overlay** (Out of Scope).

### D2. The edge-table snapshot

**Decision.** One unfiltered `lithos_edge_list()` call fetches the entire
edge table into a server-side **`EdgeTableSnapshot`**: a frozen tuple of
edge records plus derived indexes — by endpoint, by type, by namespace —
and the facet counts the picker needs. The snapshot lives in process under
`[knowledge].graph_edge_table_ttl_s` (default 300 s) with single-flight
(concurrent requests during a fetch await the one in flight) and is the
sole source for every graph read: ego-graph assembly, scoped global mode,
facets, the contradictions queue, the note-page conflict banner, and the
"hidden edges" counts.

**Why one call.** Measured 2026-10-05: the whole table is 9,442 rows and
4.9 MB of JSON, parsed server-side in well under a second. An ego graph at
depth 2 around a median node (degree 4, p90 9, max 54) would otherwise be
10–60 `lithos_related` calls each returning untitled edges; the picker's
facets cannot be built any other way at all, because no tool enumerates
types or namespaces (ledger #13). The snapshot turns both into dictionary
lookups and makes the staleness bound one number stated on the page.

**Bound.** `[knowledge].graph_edge_table_max_edges` (default 50,000). A
table over the bound is **not** loaded: the page renders a "graph too large
to index" refusal naming the count, and only `type=`/`namespace=`-filtered
global reads are served, each as a direct filtered `lithos_edge_list` call
with no facets and no focus mode. The table grew by 4,240 edges in
September; at that rate the default bound is roughly a year away, and
ledger #13 (a paginated or aggregated edge list) is the answer before then,
not a bigger number.

**Freshness.** `edge.upserted` carries `{edge_id, from_id, to_id, type,
namespace, conflict_state}` — enough to insert or replace the row's
identity and conflict state, **not** its weight, provenance or evidence.
The snapshot patches those fields in place and marks the row `partial`
until the next full fetch; a partial row draws with its last-known weight
(or the type's default when new) and its panel says the rationale is
pending. Changes that emit no event — `related_to` reinforcement,
`derived_from` projection, weight decay from misleading feedback (ledger
#15) — converge on the TTL, and the page says so (story 19).

**Not a `GraphCache` reuse.** T2's per-task cache is keyed by task and
evicted per task because the task graph has no bulk fetch; this table has
one. The snapshot is a new, smaller module (`knowledge_edges.py`) that
borrows T2's single-flight and tally patterns, not its shape.

### D3. Ego-graph assembly

`?focus=<id>&depth=1|2` (default `[knowledge].graph_default_depth` = 1):

1. **Typed edges** from the snapshot: the focus's edges; at depth 2, each
   neighbour's edges too. Nodes = focus ∪ endpoints. Filters (D6, D7) apply
   **before** the cap so that "hide faint edges" is a way under it.
2. **Wiki-links and provenance** from one `lithos_related(focus,
   include=["links","provenance"], depth=1)`: `links.outgoing/incoming`
   and `provenance.sources/derived` as `{id, title}` — titles included,
   so these need no fan-out. Depth 2 does **not** expand these layers
   (`lithos_related` at depth 2 returns a flat reachable set with no
   intermediate pairs, so there is nothing to draw an edge from). The
   layers are one hop, and the legend says so.
3. **Cap.** Over `[knowledge].graph_focus_max_nodes` (250) → refused with
   the count and the depth/filter that would bring it under (story 18).
   The picker and the depth control show each depth's would-be node count
   from the snapshot before the user asks (story 4) — a lookup, not a
   render.
4. **Node facts** via the title cache (D4).

### D4. The note title cache and the missing-note ghost

Edges carry ids, not titles, and `lithos_list` cannot select by id. The
per-node read is `lithos_read(id, max_length=1)`, which returns complete
frontmatter (verified in K1) — title, `note_type`, `status`, `namespace`,
`confidence`, `summaries.short`, `tags`. A **`NoteFactsCache`** (id →
those fields) backs every graph render: semaphored fan-out (T2's gate),
TTL `[knowledge].graph_note_facts_ttl_s` (default 3600 s — titles rarely
change and events patch them), and a per-render cap
`[knowledge].graph_title_fanout_cap` (default 300, above the 250 node cap
so a full ego graph is always titled). `note.created`/`note.updated`
events carry `{id, title, path}` and update the cached title and path in
place; `note.renamed` updates the path; `note.deleted` marks the entry
**missing**.

**Missing-note ghost.** A read answering `doc_not_found` (edges outlive
notes; nothing deletes them) caches the id as missing. The node draws with
a dashed outline, its short id as label, and "note not found" in its
panel; the text baseline lists it under "edges to missing notes". It is
never dropped: dropping it would hide that the store holds a dangling
edge. The `lithos_related` wiki-link layer never produces ghosts
(unresolved targets are skipped upstream).

### D5. Edge vocabulary, direction and legend

**The type column is an unvalidated string.** Lens therefore keeps a
**known-type table** and treats everything else as unknown:

| Type | Direction | Drawn as | Live count (2026-10-05) |
|---|---|---|---|
| `supports` | directed, from → to | solid, arrowhead | 2,961 |
| `related_to` | symmetric (stored `from <= to`) | solid, no arrowhead | 2,318 |
| `analogy_to` | symmetric (stored `from <= to`) | solid, no arrowhead | 1,720 |
| `refines` | directed | solid, arrowhead | 924 |
| `is_example_of` | directed | solid, arrowhead | 783 |
| `depends_on` | directed | solid, arrowhead | 317 |
| `derived_from` | directed, derived → source | **dotted**, arrowhead | 286 |
| `contradicts` | symmetric (stored `from <= to`) | **dashed red** unresolved; muted + label resolved | 129 |
| wiki-link | directed as written | thin grey | (from `lithos_related`) |
| *(unknown)* | as stored | neutral grey, arrowhead, labelled with the raw type | `assesses` 3, `summarizes` 1 |

Direction and symmetry are facts from the Lithos source (edge inference
stores `contradicts` and `analogy_to` symmetric; reinforcement stores
`related_to` symmetric; the rest take the LLM's verdict; `derived_from`
is the provenance projection) and are cited in the contract file, not
re-derived. An unknown type is drawn **as stored** with an arrowhead and
its panel says "direction as recorded". The **legend lists only the types
present** in the drawn graph, in the live order above, one plain-language
line each ("A supports B: A is evidence for B") — the T2 rule.

The palette is fixed per known type so a colour means the same thing on
every page; namespace colouring applies to nodes, type colouring to edges,
and the two palettes do not overlap.

### D6. Weight: line width and the default minimum

Weight is one REAL column in [0, 1]; for inferred edges it is the model's
confidence (written only at ≥ 0.6); for `related_to` it starts at 0.5
(citation reinforcement) or **0.03** (task consolidation) and grows by 0.03
per repeat; `derived_from` is fixed at 1.0. Measured: 2,176 of the 2,318
`related_to` edges are below 0.1.

**Decision.** Line width maps weight linearly over [0.1, 1.0]; weight
below `[knowledge].graph_min_weight_default` (**0.1**) is hidden by
default, with the count stated ("2,103 edges below 0.1 hidden") and a
`min_weight=` control that goes to 0. The default is a judgement about
noise, recorded here for Dave to overturn: the 0.03 consolidation edges
record "these two notes were cited by the same task" — useful as a signal
in retrieval, a hairball on a canvas.

### D7. Provenance filter

`provenance_type` is free text; the live values are `inferred` (6,833),
`consolidation` (2,303), `frontmatter` (286), `authored` (5),
`conversation-derived` (7), `manual*` (7). The filter offers the values
present in the snapshot, grouped as **inferred / reinforced / declared /
other**, each with its count; all on by default. Hidden counts are stated
with the weight-hidden count (story 6).

### D8. Node size: degree, not salience

REQUIREMENTS §8.3 sized nodes by `node_stats.salience`. Measured: 90.3%
of nodes sit at the 0.30 floor, so the picture would be uniform; and
`lithos_node_stats` is one node per call with no contract vendored.
**Decision.** Nodes are sized by **degree within the drawn graph** (a
computed fact the page states as such: "size: connections in this view")
on a small range so hubs read as hubs. Salience arrives with K3, where
`lithos_node_stats` gets its contract and the note page its stats panel;
K3 may add salience as an alternative size toggle. REQUIREMENTS §8.3 is
rewritten to match (this PR).

### D9. Node colour and status

Nodes colour by **namespace** by default (the dominant live distinction:
`influx` vs `user/…` vs `projects/…` vs `research`), with a toggle to
colour by **`note_type`**. Both come from the title-cache read. A node
whose `status` is not `active` wears it: `archived` greyed, `quarantined`
with the same red ring the note page's chip uses. `access_scope` is not
drawn (the graph is single-operator; K1 shows the chip only when not
`shared`).

### D10. Node and edge panels

The page has **one selection parameter per kind**: `?selected=<note-id>`
opens the node panel, `?edge=<edge_id>` the edge panel; both are
server-rendered beside the canvas on a full request (the no-JS baseline,
as T2's `focus=` panel is) and swapped in by HTMX on click. They are
mutually exclusive; the later click wins and the URL carries one.

**Node panel**: title (link to `/note/{id}`), chips (type, status,
namespace, confidence — the K1 chip partial), lede, "Centre on this"
(→ `?focus=<id>` preserving depth and filters), degree in this view, and
the note's relations in this view grouped by type as text (the related
panel's grouping, restricted to the drawn graph).

**Edge panel**: the relation sentence ("*A* supports *B*", direction per
D5), type, weight, namespace, provenance (actor and type), created /
updated, and the **evidence**: parsed as JSON when it is one — `rationale`
as a paragraph, `model` and `confidence` as chips — else as escaped text,
else "no rationale recorded". Below: both endpoints as cards (title,
chips, lede, link). For `contradicts`: the conflict state leads
("unresolved" for null; the four resolution values labelled), and the two
cards sit **side by side**. The panel's layout reserves the space where
the pool's "Resolve…" action will go and says, for now, "Resolving a
contradiction is not yet a Lens action" — so REQUIREMENTS §11's two-pane
read exists before its write does. A `partial` row (D2) says its
rationale is pending the next fetch.

### D11. Scoped global mode and the picker

`?type=<t>` and/or `?namespace=<ns>` select rows from the snapshot;
nodes = their endpoints; the same filters, cap
(`[knowledge].graph_global_max_nodes`, 500) and refusal apply. **No
unscoped render**: with neither parameter and no `focus`, the page is the
**scope picker** — a table of edge types with counts, a table of namespaces
with counts (top 20 by count, the rest behind a disclosure), the
unresolved-contradictions count as a link to `?type=contradicts`, and the
knowledge search box whose results offer "open in graph" per hit. Measured:
`namespace=influx` alone is 8,717 edges and would be refused; the picker
shows that count so the refusal is not a surprise, and offers
type-within-namespace combinations.

**Contradictions queue.** `?type=contradicts` is the queue: its text
baseline lists every edge unresolved-first, newest-first within state, as
"A contradicts B · ns · weight · rationale (first sentence)" with a link to
`?type=contradicts&edge=<id>`; its canvas is the same edges. No separate
route — one scope, two renderings, as everywhere on the page.

### D12. Text baseline

As on `/tasks/graph`, the text is the page and the canvas is drawn from
its payload. In order: the scope statement (focus title or filters; depth;
fetched-at and TTL); the refusal panel if any; the legend (types present);
**the focus note** with its chips; **neighbours by relation**, one `<ul>`
per type in D5 order, each entry "→ *title* (weight)" or "← *title*" by
direction, ghosts marked; **wiki-links** (outgoing, incoming) and
**provenance** (sources, derived) as K1 names them; **hidden**: the
weight- and provenance-hidden counts and the depth-2 would-be count; then
the `<script type="application/json">` payload (nodes with facts, edges
with type/weight/provenance/conflict_state/direction, legend, hidden
counts, as_of). Every title is a link to its note; every edge entry links
to its edge panel. The task-graph page's short-id convention does **not**
apply (notes have titles; a ghost shows its id prefix).

### D13. Entry points

- **Note page**: the related panel's heading gains **Open in graph**
  (→ `?focus=<id>`) and each typed-edge row gains an "in graph" link to
  that edge's panel (`?focus=<id>&edge=<edge_id>`). A note that is an
  endpoint of an **unresolved `contradicts`** edge (looked up in the
  snapshot — no extra Lithos call) gets a banner above the body: "This note
  is contradicted by *B* — view", linking to the edge panel. If the
  snapshot is unavailable the banner is simply absent.
- **Knowledge landing**: a "Browse the graph" link to the picker and the
  unresolved-contradictions count beside it.
- **Search results**: each card gains "in graph".
- **Nav**: unchanged. The nav's **Graph** item is the task graph and keeps
  its name; the knowledge graph is reached from the Knowledge surface. (A
  rename to "Task graph" is cheap and is offered as a follow-up, not done
  here.)

### D14. Events

The hub currently drops knowledge `edge.upserted` on purpose. K2 adds a
**knowledge scope** to `normalize_lithos_event`: `note.created`,
`note.updated`, `note.deleted`, `note.renamed` and `edge.upserted` are
consumed with `task_id=""`, `requires_refresh=False` for the dashboard,
and a `scope="knowledge"` marker. Server-side consumers: the snapshot (D2)
and the title cache (D4). Browser side: a new `GET /knowledge/events`
stream fed from the same upstream subscription and the same hub, filtered
to the knowledge scope — the dashboard's `/tasks/events` is unchanged and
never receives knowledge frames. The graph page subscribes and shows the
**"graph changed — refresh"** pill when a frame names a node or edge in its
drawn set (or any frame, in global mode); no auto-relayout. Note events
carry tags and edge events carry **empty** tags, so Lens's upstream
subscription keeps using no `?tags=` filter (it already does — a tag filter
would drop every edge event).

### D15. Modules

New Foundation modules, mapped in `docs/architecture.toml` under
`Knowledge` and registered in import-linter: `knowledge_edges.py` (the
snapshot: records, indexes, facets, patching, bound), `knowledge_facts.py`
(the note title cache and ghost), `knowledge_graph.py` (ego and scoped
assembly, filters, caps, view model and payload), `knowledge_edge_types.py`
(the known-type table, direction, legend lines). New Web module
`knowledge_graph_routes.py` (page, panels, events stream). New static
`knowledge_graph.js` — the task graph's `graph.js` is 1,836 lines of task
semantics (layers, chains, cycles, claims) and is not generalised; the
knowledge canvas shares Cytoscape and the visual conventions, not the
code. Shared CSS is extended, not duplicated. The `[knowledge]` config
block gains the knobs named above; `graph_focus_max_nodes` and
`graph_global_max_nodes`, which REQUIREMENTS already lists, become real.
The guardrail budgets (`modules_over_800_lines`, component counts) are
prompts for a decision, not limits (`docs/architecture.toml` says so);
`knowledge.py` sits at 799 lines and K2 does not add to it.

### Config

```toml
[lithos-lens.knowledge]
graph_focus_max_nodes = 250          # existing in REQUIREMENTS; now real
graph_global_max_nodes = 500         # existing in REQUIREMENTS; now real
graph_default_depth = 1              # 1 or 2
graph_min_weight_default = 0.1       # edges below are hidden by default (D6)
graph_edge_table_ttl_s = 300         # snapshot staleness bound (D2)
graph_edge_table_max_edges = 50000   # snapshot refused above this (D2)
graph_note_facts_ttl_s = 3600        # title/facts cache (D4)
graph_title_fanout_cap = 300         # lithos_read(max_length=1) reads per render (D4)
```

Env overrides follow the existing `LITHOS_LENS_KNOWLEDGE_*` prefix rule
(the docs↔config guardrail test enforces the listing).

### Routes

| Route | Purpose |
|---|---|
| `GET /knowledge/graph` | picker (no scope); focus mode (`focus=`, `depth=`); scoped global (`type=`, `namespace=`); filters `min_weight=`, `provenance=`, `colour=namespace\|type`; selection `selected=` or `edge=` |
| `GET /knowledge/graph/panel?selected=<id>&…scope` | node panel fragment (HTMX) |
| `GET /knowledge/graph/panel?edge=<edge_id>&…scope` | edge panel fragment (HTMX) |
| `GET /knowledge/events` | browser SSE stream, knowledge scope (D14) |

Note page and landing changes are to existing routes (D13).

### MCP / SSE dependencies

New client method `edge_list` (`lithos_edge_list`) with **its contract
transcribed in the same PR** (`tests/contracts/lithos_edge_list.json`: the
four optional filters, the 12-key row, null for NULL columns, the
no-limit/no-order facts, the `ambiguous_id_prefix` envelope, all cited to
`memory_edges.py` / `edge_store.py` at d2c49bb). `lithos_related` is
already wired; its contract is **refreshed** (prefix ids accepted since
8ce96bd; NULL columns are `null`, not `""`). `lithos_read` is already
wired; its contract gains the `created_at`/`updated_at` names and the
top-level `path` the server now returns. No `lithos_node_stats` (K3), no
`lithos_conflict_resolve` (pool), no `lithos_search mode=graph` (it walks
wiki-links only and returns rows, not edges — not a graph source).
Upstream events consumed: the five knowledge types in D14; their payloads
are documented from `events.py` in the SPECIFICATION's event table, not in
a contract (events are HTTP, outside the contract scheme).

### Telemetry

`lens.knowledge.graph` as attributes on the request span: mode
(`picker|focus|global`), depth, node and edge counts, hidden counts,
refusal reason, snapshot age; `lens.knowledge.edge_table` as a named span
per fetch (duration, row count, bytes, outcome) with a gauge for snapshot
age and a counter for patches by event type; `lens.knowledge.note_facts`
counters (hits, reads, ghosts, cap reached). The page's text states the
same snapshot age it exports.

## Testing Decisions

The bar is K1's: every behaviour below has a test that **fails when it is
reverted**; the architecture budgets hold; no coverage percentage.

- **Snapshot (pure + fake)**: indexes agree with the rows; facets count
  what the rows contain; single-flight (two concurrent reads, one upstream
  call); TTL expiry refetches; `edge.upserted` inserts a new row as
  `partial` and replaces conflict_state on an existing one; a table over
  the bound is refused and filtered reads still work; a fetch failure keeps
  the previous snapshot and marks it stale.
- **Edge types (pure, table-driven)**: each known type's direction and
  style; symmetric types render no arrowhead; unknown type renders as
  stored with the raw label; the legend lists exactly the types present,
  in order.
- **Ego assembly (pure)**: depth 1 vs 2 node sets on a fixture graph;
  filters applied before the cap; refusal names count and remedy; the
  would-be counts per depth; ghosts for unknown ids retained; wiki-link
  and provenance layers one hop regardless of depth.
- **Title cache (fake)**: fan-out under the semaphore; cap reached leaves
  id-labelled nodes and says so; `doc_not_found` → ghost; `note.updated`
  patches the title without a read; `note.deleted` → ghost on next draw.
- **Page (TestClient + fake dataset with every known type, an unknown
  type, a ghost, an unresolved and a resolved contradiction)**: the text
  baseline names every node and edge the payload carries and they agree;
  the picker renders facets and counts; `type=contradicts` lists
  unresolved first; `namespace=` over the cap is refused; `selected=`
  renders the node panel server-side; `edge=` renders the edge panel with
  the rationale parsed from evidence JSON and falls back to escaped text
  for a non-JSON evidence string; a `contradicts` edge renders both cards
  side by side and the "not yet an action" line; hidden counts match the
  filters.
- **Entry points**: note page shows Open in graph and per-row links; the
  contradiction banner appears only for an unresolved edge and is absent
  when the snapshot is unavailable; landing shows the browse link and
  count.
- **Events (fake upstream)**: the five knowledge types are consumed with
  `requires_refresh=False`; `/tasks/events` subscribers never receive them;
  `/knowledge/events` subscribers do; the snapshot and title cache observe
  them; a frame naming a drawn node sets the pill (JS harness, pattern of
  `test_tasks_js.py`).
- **Contracts**: `tests/test_lithos_contracts.py` round-trips the new
  `lithos_edge_list` payload and the refreshed `lithos_related` and
  `lithos_read` payloads through the real client; `make contracts-verify`
  recorded in the S1 PR.
- **Visual**: e2e screenshots of the picker, a focus graph with legend and
  panels, and `type=contradicts`, at 1440 and 768; the stylesheet-coverage
  test constrains new classes.
- **Guardrails**: `make check && make diagrams` with no generated drift;
  the new modules mapped in `architecture.toml`.

## Tracer-bullet vertical slices

Seven slices. Each updates `docs/SPECIFICATION.md` for what it ships and
passes `make check && make diagrams` with no generated drift. The text
baseline carries the acceptance criteria, as on T2 and T3, because the
review gate is hermetic.

1. **S1 Edge table snapshot + contracts.** `edge_list` client method; the
   `lithos_edge_list` contract; `lithos_related` and `lithos_read` contract
   refresh; `knowledge_edges.py` (records, indexes, facets, TTL,
   single-flight, bound, event patching API); the fake's knowledge edge
   dataset; the known-type table (`knowledge_edge_types.py`); config knobs.
   *Independent.* Acceptance: the Snapshot, Edge types and Contracts
   cases; `make contracts-verify` recorded.
2. **S2 Note facts cache + ego assembly.** `knowledge_facts.py`,
   `knowledge_graph.py` focus assembly, filters, caps, ghosts, view model
   and payload. *Needs S1.* Acceptance: the Title cache and Ego assembly
   cases.
3. **S3 `/knowledge/graph` text baseline, picker and scoped mode.**
   `knowledge_graph_routes.py`; picker with facets; focus and global text
   renderings; contradictions queue ordering; refusals; telemetry.
   *Needs S2.* Acceptance: the Page cases except the panels.
4. **S4 Canvas.** `knowledge_graph.js`: Cytoscape over the payload,
   legend, palettes, arrowheads by direction, weight → width, dashed-red
   unresolved contradictions, degree sizing, namespace/type colour toggle,
   weight and provenance controls, search, focus/dim, Centre on this, cap
   messaging. *Needs S3.* Acceptance: visual-review artifacts; the JS
   harness cases for state ↔ URL.
5. **S5 Node and edge panels.** Server-rendered `selected=` / `edge=`
   panels; HTMX fragments; evidence parsing; the contradiction two-pane
   layout with its "not yet an action" line. *Needs S3; independent of
   S4.* Acceptance: the panel cases.
6. **S6 Entry points.** Note page Open in graph and per-row links; the
   unresolved-contradiction banner; landing browse link and count; search
   card links. *Needs S1 (banner) and S3 (links).* Acceptance: the Entry
   points cases.
7. **S7 Knowledge events.** Hub knowledge scope; `/knowledge/events`;
   snapshot and title-cache patching wired to live frames; the refresh
   pill. *Needs S1, S2 and S4.* Acceptance: the Events cases.

Ready at milestone start: **S1**. S2 → S3 is the spine; S4, S5 and S6 are
independent of each other after S3; S7 is last.

## Out of Scope

- **Conflict resolution** (`lithos_conflict_resolve`) — the first
  knowledge write; deferred pool. It inherits T3's funnel and operator
  identity and lands in the edge panel D10 shapes for it.
- **Salience** (node size or panel) and `lithos_node_stats` — K3, which
  vendors that contract.
- **Centrality overlay** (client-side betweenness) — REQUIREMENTS §8.4
  lists it; with p90 degree 9 and a 250-node cap it adds little over
  degree sizing. Moved to the deferred pool; revisit if the graphs get
  denser.
- **An unscoped global render** — the picker is the unscoped page (D11).
- **Depth above 2**, and wiki-link/provenance layers beyond one hop
  (`lithos_related` cannot supply the pairs).
- **Filtering by tag or date** (REQUIREMENTS §8.4 "filter panel") — edges
  carry neither; nodes would need a per-node read to filter on, which is
  the fan-out the cap bounds. Namespace, type, weight and provenance are
  the filters.
- **Semantic projection** (UMAP/t-SNE) — blocked on embeddings over MCP.
- **Edge editing or deletion** — no MCP delete tool for knowledge edges;
  no requirement.
- **Multi-select and note comparison** — deferred pool (§12).
- **Reworking the knowledge landing or note page beyond D13** — tracked as
  separate UX tasks (the search-box and back-link bugs filed 2026-10-05
  are not K2 slices).

## Further Notes

- **Facts read from the Lithos source** (0.6.0 @ `d2c49bb`, 2026-10-05),
  which the contracts must cite rather than this list: `lithos_edge_list`
  takes `from_id`, `to_id`, `type`, `namespace` (all optional; prefixes of
  six or more characters resolve for the id filters) and returns
  `{results: [...]}` with no limit, offset, order or total, each row with
  twelve keys (`edge_id` as `edge_<12hex>`, `from_id`, `to_id`, `type`,
  `weight`, `namespace`, `created_at`, `updated_at`, `provenance_actor`,
  `provenance_type`, `evidence`, `conflict_state`), `evidence` a JSON
  string or null, NULL columns as `null`; the edge type is an unvalidated
  string with uniqueness on `(from_id, to_id, type, namespace)`; endpoints
  need not exist and edges are not deleted with their notes;
  `conflict_state` is NULL on inferred edges and one of `accepted_dual`,
  `superseded`, `refuted`, `merged` after `lithos_conflict_resolve` (the
  retrieval scout treats `accepted_dual` as still open); inferred edges
  are written at confidence ≥ 0.6 with `evidence = {rationale, model,
  confidence}`; `lithos_related` returns titles on `links` and
  `provenance` entries and none on `edges`, its `depth` applies to links
  and provenance only and returns a flat set at depth > 1; `edge.upserted`
  is emitted by `lithos_edge_upsert`, inferred-edge assertion and
  `conflict_resolve` with `{edge_id, from_id, to_id, type, namespace,
  conflict_state}` and empty tags, and **not** by `related_to`
  reinforcement, `derived_from` projection or weight decay; `note.*`
  events carry `{id, title, path}` (`note.deleted` without tags;
  `note.renamed` with `src_path`/`dest_path`, watcher only).
- **Live measurements** (prod, 2026-10-05): 5,818 notes; 9,442 typed
  edges over 3,893 distinct nodes; types and counts as in D5; namespaces
  `influx` 8,717, `digests` 216, `user/planning/daily/2026` 177, `user/daily`
  98, others under 100; provenance `inferred` 6,833 / `consolidation`
  2,303 / `frontmatter` 286; degree median 4, p90 9, max 54, two nodes over
  50; 2,176 of 2,318 `related_to` edges under weight 0.1; 129
  `contradicts`, 128 with null `conflict_state`, 109 in `influx`; edges
  created per month: Aug 2,317, Sep 4,240, Oct (to the 5th) 579; the
  wiki-link graph has 128 edges and 49 unresolved targets — wiki-links are
  marginal structure in this corpus, typed edges are the graph.
- **Contract drift found while drafting**, fixed in S1: `lithos_read.json`
  names `created`/`updated` where the server emits `created_at`/
  `updated_at` and omits the top-level `path` it now returns;
  `lithos_related.json` shows `""` where the server returns `null`.
  Neither has bitten because the client normalises; both would mislead the
  next author.
- **Why the task graph's `graph.js` is not reused.** It encodes layers,
  the longest chain, cycle condensations, claims and ghosts-by-scope — the
  task graph's semantics. The knowledge graph has none of those and has
  weight, provenance, symmetry and conflict state instead. Sharing
  Cytoscape, the CSS conventions and the "text first, canvas from the same
  payload" rule is the right amount of reuse; sharing code would make both
  harder to change.
- **Why no `/knowledge/contradictions` route.** A queue is a scope
  (`type=contradicts`) with a text rendering that happens to be a list.
  One page, one payload shape, one set of tests; the link on the landing
  page is the "route".
- **Upstream asks added to the ledger** (none gates a slice): **#13** a
  bounded `lithos_edge_list` (limit/offset, or an aggregate facets tool —
  `EdgeStore.count` and `list_edges_between` exist unexposed); **#14** a
  neighbourhood tool with titles (the planned WS7 `lithos_related`
  `neighbours[]`); **#15** events for the edge changes that emit none
  (reinforcement, projection, weight decay); **#16** `lithos_list` cannot
  filter by namespace, note type or status and returns only `extra` as
  metadata.
- **Normative docs updated with this PRD.** REQUIREMENTS §8 is rewritten:
  the edge table (8.3) to the live vocabulary with the unknown-type rule
  and direction facts; data assembly (8.2) to the snapshot and title
  cache; node size to degree with salience at K3; the filter panel to
  namespace/type/weight/provenance; freshness (8.5) to the event facts
  above; caps (8.6) to refusals. ROADMAP §3 links this PRD on the K2 row
  and §4 gains ledger #13–#16. `docs/SPECIFICATION.md` changes slice by
  slice.
- **Spec drift.** When K2 ships, SPECIFICATION §5.7 gains the graph page,
  §5.8 the knowledge event scope and stream, and the user manual is
  regenerated.
