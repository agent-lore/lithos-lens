/*
  The graph page's Cytoscape layer (T2-A4) — progressive enhancement over the
  text baseline A3 renders, drawn from the SAME embedded payload so the picture
  and the text cannot disagree (D3).

  Three rules shape everything below.

  1. NOTHING IS FETCHED. The payload already carries every node and every edge
     the page can show, hierarchy and provenance included (D6 resolves both
     overlays' context ghosts on every request precisely so that toggling one is
     a client-side show/hide). So an overlay toggle, the isolated toggle and a
     `popstate` are all the same operation: read the URL, decide what is drawn,
     apply it.

  2. THE LAYOUT RUNS ONCE. `breadthfirst` from the server's own `roots`, no
     physics (D8) — and it decides the ORDER across a rank, not the rank, which
     is the server's `layer` (see the placement below). That is the same rule as
     rule 3: the picture is printed above the text layers and may not contradict
     them. Once placed, nothing moves: a toggle or a resize moves the VIEWPORT
     (a pan and a zoom) and never a node, and a task event raises the "graph
     changed" pill rather than re-laying-out the canvas under the cursor.

  3. NO CLAIM IS INVENTED HERE. Status, type, ghost-ness, cycle membership, the
     longest chain and the isolated set are all payload fields decided by the
     server, which is where the PRD's honesty rules live. The two things this
     file derives are both reachability over the `active` dependency edges the
     payload already states: which nodes something in THIS graph blocks, and
     (T2-A7) what a focused node sits between. Neither is the readiness
     verdict, which stays Lithos's — and neither walks an `unknown` edge,
     because an endpoint Lens could not read makes a relation unknowable
     rather than longer.
*/
/*
  TWO HOSTS, ONE MODULE (T2-A5). The graph page embeds its canvas and its
  payload in the document; the task-detail page's MINI-GRAPH arrives later, as
  an HTMX fragment, and carries the same two elements inside a
  `[data-mini-graph]` section. `initLensGraph` therefore runs on whichever host
  is present — at load for the page, on `htmx:afterSwap` for the fragment — and
  everything downstream of it reads `mini` for the four things that are the
  PAGE's and not the picture's: the URL is the graph page's selection state, so
  the mini-graph neither reads nor writes it; the toolbar, the search box and
  the "graph changed" pill belong to a page that has them; a node click opens a
  side panel this host does not have; and the mini-graph's state comes from the
  payload the server built for it. What is shared is everything that DRAWS —
  the styling vocabulary, the arrowheads, the layout, the fit — which is D11's
  "same client module" and the reason this is not a second file.
*/
const initLensGraph = function () {
  const container = document.querySelector("[data-graph-canvas]");
  // Already drawn: this is a later swap on a page whose canvas is up, and
  // re-running over it would build a second Cytoscape instance on the same
  // element (two layouts, two sets of handlers, one visible picture).
  if (!container || container.dataset.graphDrawn || !window.cytoscape) return;
  // A mini-graph's payload is its own: two canvases in one document would
  // otherwise share whichever payload the document happens to hold first.
  const miniHost = container.closest("[data-mini-graph]");
  const mini = !!miniHost;
  const source = (miniHost || document).querySelector("[data-graph-payload]");
  // Shown only while the picture is bigger than the canvas can hold at a
  // readable size — otherwise it would claim a limit that is not there.
  const panHint = document.querySelector("[data-graph-pan-hint]");
  if (!source) return;
  // Claimed before anything can fail below: a container this run rejected —
  // an unreadable payload, an empty node set — must not be re-attempted by
  // every later swap on the page.
  container.dataset.graphDrawn = "true";

  let payload = null;
  try {
    payload = JSON.parse(source.textContent || "{}");
  } catch (error) {
    // A payload this file cannot read is a page that stays exactly as the
    // server rendered it — the text baseline is the whole page without us.
    return;
  }
  const nodes = (payload && payload.nodes) || [];
  const rawEdges = (payload && payload.edges) || [];
  const scope = (payload && payload.scope) || {};
  if (!nodes.length) return;

  // ── The vocabulary the server and this file share ──────────────────────

  // EVERY lookup below is keyed by something the SERVER chose — a task id or
  // an edge type — and `tasks.py` contracts a task id as an arbitrary non-empty
  // string. A plain `{}` inherits `__proto__`, `constructor` and `toString`
  // from its prototype, so `map[id]` for a task called `constructor` answers
  // with a function nobody stored and `map[id].push` throws. Null-prototype
  // maps have no such answers to give.
  function dict(entries) {
    return Object.assign(Object.create(null), entries || {});
  }

  const OVERLAY_BY_EDGE_TYPE = dict({
    parent_child: "hierarchy",
    discovered_from: "provenance"
  });
  const OVERLAYS = ["hierarchy", "provenance"];
  const DEPENDENCY_EDGE_TYPES = dict({ blocks: true, waits_on_gate: true });
  const SELECTION_PARAM =
    (window.LithosLensTasks || {}).selectionParam || "focus";
  // Mirrors `graph_page._flag`: a value in neither set carries no request at
  // all, so the scope's own default stands rather than flipping to false.
  const TRUE_FLAGS = ["1", "true", "yes", "on"];
  const FALSE_FLAGS = ["0", "false", "no", "off"];

  const byId = dict();
  nodes.forEach(function (node) { byId[node.id] = node; });

  // CYTOSCAPE IS HANDED OPAQUE IDS, never a task's own — and the second reason
  // is not ours to fix.
  //
  // Ours: the payload has no id at all for the two elements this file has to
  // synthesise (a cycle's compound parent, and every edge), and any id built by
  // splicing task ids together can collide — `a::b → c` and `a → b::c` produce
  // one key between them, and Cytoscape keeps whichever arrived first, silently
  // dropping the other edge and its arrowhead.
  //
  // Theirs: a task id is an arbitrary non-empty string (`tasks.py`), and the
  // shipped 3.30.3 throws outright — "f.source is not a function", inside
  // `breadthfirst` — on an element called `__proto__`, `constructor` or
  // `toString`, because its own internal maps are prototype-bearing. No
  // escaping fixes that from out here; only not using the id does.
  //
  // So ids are indices, and everything the page says about a node is looked up
  // through these two maps.
  const elementIdOf = dict();
  const taskIdOf = dict();
  nodes.forEach(function (node, index) {
    const elementId = "n" + index;
    elementIdOf[node.id] = elementId;
    taskIdOf[elementId] = node.id;
  });

  //: The payload node an element stands for, or undefined for one this file
  //: synthesised (a cycle's box).
  function nodeFor(element) {
    return byId[taskIdOf[element.id()]];
  }

  // Both endpoints in the node set, or Cytoscape rejects the edge. The server
  // does not emit a dangling one, but a payload it could not complete is
  // exactly the state this page exists to survive.
  const edges = rawEdges.filter(function (edge) {
    return byId[edge.from] && byId[edge.to];
  });

  const isolated = dict();
  ((payload && payload.isolated) || []).forEach(function (id) {
    isolated[id] = true;
  });

  // "Something in this graph blocks it", straight off the states the server
  // classified (D6). NOT Lithos's readiness verdict, which this page never
  // re-implements — an inactive or unknown edge is deliberately not counted.
  const blocked = dict();
  edges.forEach(function (edge) {
    if (DEPENDENCY_EDGE_TYPES[edge.type] && edge.state === "active") {
      blocked[edge.to] = true;
    }
  });

  // ── The active projection, walked both ways (D8's focus mode) ─────────
  //
  // The SAME edges and the SAME states the server classified — an `active`
  // dependency edge and nothing else. What focus mode adds is reachability
  // over them, which is arithmetic on the payload rather than a new claim:
  // "what does this task sit between?" is exactly the question the picture
  // exists to answer, and the server already said which edges count.
  //
  // The `unknown` edges are kept apart, and deliberately not walked THROUGH:
  // an endpoint Lens could not read makes the relation unknowable, not longer.
  // Their far ends are named (the `unknown` class) so the gap is visible, and
  // the panel says the lit set is a lower bound.
  const activeOut = dict();
  const activeIn = dict();
  const unknownEnds = dict();
  // Every dependency edge, direction dropped: what the unknown frontier
  // spreads along once it has been crossed. Past an endpoint Lens could not
  // read, "which way round" is not a question the payload can answer — the
  // edges beyond it were reported by somebody else's list — so a node hanging
  // off one is related to the focus only through that unreadable hop, whatever
  // direction it hangs in (round-6 correctness f-012).
  const related = dict();
  edges.forEach(function (edge) {
    if (!DEPENDENCY_EDGE_TYPES[edge.type]) return;
    if (edge.state === "active" || edge.state === "unknown") {
      (related[edge.from] = related[edge.from] || []).push(edge.to);
      (related[edge.to] = related[edge.to] || []).push(edge.from);
    }
    if (edge.state === "active") {
      (activeOut[edge.from] = activeOut[edge.from] || []).push(edge.to);
      (activeIn[edge.to] = activeIn[edge.to] || []).push(edge.from);
      return;
    }
    if (edge.state !== "unknown") return;
    (unknownEnds[edge.from] = unknownEnds[edge.from] || []).push(edge.to);
    (unknownEnds[edge.to] = unknownEnds[edge.to] || []).push(edge.from);
  });

  //: Everything reachable from `start` over `adjacency`, `start` included.
  function reachable(start, adjacency, into) {
    const queue = [start];
    into[start] = true;
    while (queue.length) {
      const current = queue.shift();
      (adjacency[current] || []).forEach(function (next) {
        if (into[next]) return;
        into[next] = true;
        queue.push(next);
      });
    }
    return into;
  }

  // D8, exactly: the focused node's ancestors and descendants over the active
  // projection are LIT; a node whose own edges are unreadable, or that is
  // reached only through an `unknown` edge, is UNKNOWN — neither lit nor
  // dimmed, because its relation to the focus is not known either way; and
  // everything else is DIMMED. `null` when nothing is focused, which is the
  // page's ordinary state and carries none of these classes at all.
  function focusSets(focus) {
    if (!focus || !byId[focus]) return null;
    // The two walks keep SEPARATE visited sets and are unioned afterwards.
    // Sharing one would let the descendant walk mark a node the ancestor walk
    // then refuses to expand — and in a cycle that is not a shortcut but a
    // wrong answer: with `F -> X -> F`, X is marked going down, so `A -> X`
    // never gets walked and a genuine ancestor of the focus renders dimmed.
    const lit = dict();
    [reachable(focus, activeOut, dict()), reachable(focus, activeIn, dict())]
      .forEach(function (side) {
        Object.keys(side).forEach(function (id) { lit[id] = true; });
      });
    const unknown = dict();
    // An unreadable edge list is unreadable in BOTH directions, so the node
    // carrying one cannot be placed relative to the focus even when an edge
    // somebody else reported reaches it.
    nodes.forEach(function (node) {
      if (node.completeness === "edges_unknown") unknown[node.id] = true;
    });
    // The frontier: far ends of the `unknown` edges that touch the lit set.
    const queue = [];
    const crossed = dict();
    Object.keys(lit).forEach(function (id) {
      (unknownEnds[id] || []).forEach(function (other) {
        if (lit[other] || crossed[other]) return;
        crossed[other] = true;
        queue.push(other);
      });
    });
    // …and everything the frontier goes on to reach, which D8 classes the same
    // way: "reached only through an `unknown` edge" is transitive, and a node
    // two hops past an unreadable endpoint is no more placeable than the
    // endpoint itself. A node the ACTIVE walk already lit keeps its lighting —
    // an independently known path to the focus is knowledge, and the unknown
    // frontier does not take it away (round-6 correctness f-012).
    while (queue.length) {
      const current = queue.shift();
      unknown[current] = true;
      (related[current] || []).forEach(function (next) {
        if (lit[next] || crossed[next]) return;
        crossed[next] = true;
        queue.push(next);
      });
    }
    return { lit: lit, unknown: unknown };
  }

  // Only a cycle Lens can SHAPE gets a compound parent (D4): a Lithos-flagged
  // member with no component in the fetched topology is condensed alone, and a
  // box drawn round it would be a cycle of one.
  const cycleParent = dict();
  const cycleElementId = dict();
  const cycleMembers = dict();
  ((payload && payload.cycles) || []).forEach(function (cycle, index) {
    if (!cycle.scc) return;
    cycleParent[cycle.id] = "c" + index;
    cycleElementId[cycle.id] = "c" + index;
    cycleMembers[cycle.id] = cycle.members || [];
  });

  // The DISPLAY condensation: the all-edge SCC the picture is drawn from, which
  // is what puts a node inside a compound box and what the placement below
  // stacks in one slot. NOT the chain's — see `chainCondensationOf`.
  function condensationOf(id) {
    const node = byId[id];
    return (node && node.cycle) || id;
  }

  // The chain (D7) is a walk over a CONDENSED graph — a cycle counts as one
  // node and is named by its representative — so it cannot be matched against
  // raw endpoints. `P → B` where B is a non-representative member of A's cycle
  // IS the step `P → A` the chain names, and a client comparing ids would look
  // for an edge that does not exist and leave the trace broken exactly at the
  // cycle boundary. Everything below is keyed by condensation instead.
  //
  // But it is the ACTIVE projection's condensation, and the server states that
  // one separately (`active_chain.of`) precisely because it is NOT the `cycle`
  // each node carries. Those two partitions legitimately differ: an epic graph
  // showing completed children can hold an open `A → B` whose loop closes back
  // through an inactive `B → C` and `C → A`, which is one drawn cycle
  // `{A,B,C}` and a live two-chain `A → B` at the same time. Mapping through
  // `cycle` there would call the chain's own step internal and leave completed
  // `C` accented — the canvas tracing a different answer from the one the text
  // states. So a node the projection does not name stands for itself.
  //
  // And WHICH chain is traced changes with the focus, and a focus transition is
  // client-side (D8) — so the payload ships the DP behind every chain this
  // page can be asked to show (`graph_view.active_chain_payload`) rather than
  // one answer: `of` is the projection's own condensation, `up` / `down` are
  // the next step of the longest walk into and out of each condensation, and
  // `chain` is the scope's own longest. Walking those pointers is not a second
  // implementation of D7 — the tie-breaks are already baked into them — which
  // is what keeps rule 3 true while the line changes under a click.
  const projection = dict(payload.active_chain || {});
  const chainCondensationOfId = dict(projection.of || {});
  const chainUp = dict(projection.up || {});
  const chainDown = dict(projection.down || {});
  const scopeChain = projection.chain || [];

  function chainCondensationOf(id) {
    return chainCondensationOfId[id] || id;
  }

  // The chain the page is currently tracing: through the focused node, or the
  // scope's own when nothing is focused (D7/D8). A focus the projection does
  // not name — a `focus=` for a task this graph never fetched — keeps the
  // scope's, exactly as the server's own render does.
  function chainFor(focus) {
    const start = chainCondensationOfId[focus];
    if (!focus || !start) return scopeChain;
    const walk = function (pointers) {
      const steps = [];
      const seen = dict();
      let cursor = start;
      seen[cursor] = true;
      while (pointers[cursor] && !seen[pointers[cursor]]) {
        cursor = pointers[cursor];
        seen[cursor] = true;
        steps.push(cursor);
      }
      return steps;
    };
    return walk(chainUp).reverse().concat([start], walk(chainDown));
  }

  // The chain drawn right now, in the two shapes the canvas asks of it: which
  // condensations are ON it, and which ordered PAIRS are steps of it. Rebuilt
  // on every render because the focus decides the answer.
  //
  // The steps are a NESTED map, not a joined key. A condensation is named by a
  // task id, and a task id is an arbitrary non-empty string (`tasks.py`):
  // `from + ">" + to` cannot tell the step `a>b → c` from the step `a → b>c`,
  // so an off-chain dependency between the second pair would take the
  // critical-path accent from the first. Two lookups have no separator to be
  // ambiguous about.
  let chain = [];
  let chainCondensations = dict();
  let chainSteps = dict();

  function setChain(nodes) {
    chain = nodes;
    chainCondensations = dict();
    chainSteps = dict();
    chain.forEach(function (id, index) {
      chainCondensations[id] = true;
      if (!index) return;
      const from = chain[index - 1];
      if (!chainSteps[from]) chainSteps[from] = dict();
      chainSteps[from][id] = true;
    });
  }

  function onChain(id) {
    return chainCondensations[chainCondensationOf(id)] === true;
  }

  function stepOnChain(edge) {
    // The chain is the longest BLOCKING chain over the ACTIVE projection (D7),
    // so only an active dependency edge can be a step of it. A `parent_child`
    // or `discovered_from` edge running parallel to one — the same two tasks,
    // a different relation — would otherwise take the critical-path accent and
    // trace a hierarchy as if it blocked something.
    if (!DEPENDENCY_EDGE_TYPES[edge.type] || edge.state !== "active") return false;
    const from = chainCondensationOf(edge.from);
    const to = chainCondensationOf(edge.to);
    // An edge INSIDE a condensation is not a step either: the chain crosses it
    // in one move, and the loop it is drawn from has no direction the chain
    // endorses.
    if (from === to) return false;
    const successors = chainSteps[from];
    return !!successors && successors[to] === true;
  }

  // ── URL state (D8: one URL, re-applied on popstate) ────────────────────

  function flag(raw, fallback) {
    if (raw === null || raw === undefined) return fallback;
    const value = String(raw).trim().toLowerCase();
    if (TRUE_FLAGS.indexOf(value) !== -1) return true;
    if (FALSE_FLAGS.indexOf(value) !== -1) return false;
    return fallback;
  }

  function stateFromUrl() {
    // A mini-graph's state is the SERVER's, not the address bar's: the detail
    // page's URL is about a task, not about a scope, and a stray `focus=` or
    // `overlays=` on it would be another page's parameter read as this
    // picture's. What the server decided travels in the payload (D11: the
    // hierarchy overlay is on, because the parent epic is a member of this
    // scope by decision and would otherwise be drawn with no edge to it).
    if (mini) {
      return {
        overlays: (scope.overlays || []).filter(function (name) {
          return OVERLAYS.indexOf(name) !== -1;
        }),
        isolated: scope.isolated === true,
        includeResolved: scope.include_resolved === true,
        focus: scope.focus || ""
      };
    }
    const params = new URL(window.location.href).searchParams;
    const overlaysRaw = params.get("overlays");
    return {
      // An ABSENT `overlays` means none, never "whatever the payload was built
      // with". Every URL this file pushes spells the parameter out when it is
      // non-empty, so the only address that can lack it is the one the page
      // loaded on — where the payload says none either.
      overlays: (overlaysRaw === null ? [] : overlaysRaw.split(","))
        .map(function (name) { return name.trim(); })
        .filter(function (name) { return OVERLAYS.indexOf(name) !== -1; }),
      // Absent means the SCOPE's default, which the server already resolved
      // into the payload for this page's own URL (project graphs fold isolates
      // away, epic graphs show them).
      isolated: flag(params.get("isolated"), scope.isolated === true),
      // Read for the resolved link's href only — this page never flips it
      // client-side, because resolved tasks are nodes the server did not send.
      includeResolved: flag(
        params.get("include_resolved"), scope.include_resolved === true
      ),
      focus: params.get(SELECTION_PARAM) || ""
    };
  }

  function urlWith(changes) {
    const url = new URL(window.location.href);
    if (changes.overlays !== undefined) {
      if (changes.overlays.length) {
        url.searchParams.set("overlays", changes.overlays.join(","));
      } else {
        url.searchParams.delete("overlays");
      }
    }
    if (changes.isolated !== undefined) {
      url.searchParams.set("isolated", changes.isolated ? "1" : "0");
    }
    if (changes.includeResolved !== undefined) {
      url.searchParams.set("include_resolved", changes.includeResolved ? "1" : "0");
    }
    if (changes.focus !== undefined) {
      if (changes.focus) url.searchParams.set(SELECTION_PARAM, changes.focus);
      else url.searchParams.delete(SELECTION_PARAM);
    }
    return url.pathname + url.search + url.hash;
  }

  //: The overlay each edge touching a node belongs to, in toolbar order —
  //: what it would take to DRAW a node that only an overlay anchors.
  function anchoringOverlays(taskId) {
    const found = dict();
    edges.forEach(function (edge) {
      if (edge.from !== taskId && edge.to !== taskId) return;
      const overlay = OVERLAY_BY_EDGE_TYPE[edge.type];
      if (overlay) found[overlay] = true;
    });
    return OVERLAYS.filter(function (name) { return found[name] === true; });
  }

  // What this URL state has to change for `taskId` to be DRAWN (D8: a search
  // hit or a deep link never points at an invisible node).
  //
  // Two kinds of node are hidden by default and they are hidden for different
  // reasons, so each is revealed by its own parameter: an isolate is folded
  // away by the scope's `isolated` default, and a CONTEXT ghost exists only to
  // anchor an overlay edge and is drawn only while that overlay is on (D6).
  // One overlay is enough — the first that anchors it — because the node is
  // visible as soon as any edge reaching it is.
  function revealChanges(taskId, state) {
    const changes = {};
    const node = byId[taskId];
    if (!node) return changes;
    if (node.ghost_kind === "context") {
      const anchors = anchoringOverlays(taskId);
      const lit = anchors.filter(function (name) {
        return state.overlays.indexOf(name) !== -1;
      });
      if (anchors.length && !lit.length) {
        changes.overlays = state.overlays.concat([anchors[0]]);
      }
    }
    if (isolated[taskId] && !state.isolated) changes.isolated = true;
    return changes;
  }

  // The address a focus transition moves to — ONE entry, however many
  // parameters it takes to get there. Focusing a node the page is not drawing
  // reveals it in the same move (D8); a second push for the reveal would make
  // Back undo half the transition.
  function focusUrl(taskId) {
    const state = stateFromUrl();
    const changes = revealChanges(taskId, state);
    changes.focus = taskId;
    return urlWith(changes);
  }

  // What the panel for `taskId` would be stating ABOUT THIS CANVAS (D7/D8).
  //
  // The panel is a second read of a scope this page already assembled, and the
  // canvas deliberately does not re-lay-out under the operator (D8) — so by the
  // time a click is answered, that rebuild can hold a different picture, or
  // none at all (refused, failed, or no longer holding the node). The two
  // statements the panel makes about the PICTURE are therefore this page's to
  // supply, and both are read straight off the payload rather than recomputed:
  // `bound` is the server's own per-node answer (`graph_snapshot`), and the
  // position is the focus's place on the scope's longest chain, the one the
  // server traced (round-4 correctness f-006).
  function canvasNotes(taskId) {
    const node = byId[taskId];
    if (!node) return null;
    const step = scopeChain.indexOf(chainCondensationOf(taskId));
    return {
      canvas_bound: node.bound ? "lower" : "exact",
      canvas_chain: step < 0 ? "" : step + 1 + ":" + scopeChain.length
    };
  }

  // Every way this page changes the selection goes through here — a node tap,
  // a search hit, the client's own fallback for a `focus=` the server could
  // not answer — so the URL, the panel and the canvas move together. The panel
  // owns the push (it lands AFTER its fetch, so no URL ever claims a panel
  // that failed to open) and announces the change back, which is what re-runs
  // `render`. Without the panel implementation there is nothing to open, and
  // the URL still moves so the lighting follows.
  function focusOn(taskId) {
    if (!byId[taskId]) return;
    const open = panel();
    if (open) {
      open.open(taskId, { url: focusUrl(taskId) });
      return;
    }
    window.history.pushState({}, "", focusUrl(taskId));
    render();
  }

  function toggled(overlays, name) {
    return OVERLAYS.filter(function (overlay) {
      return (overlays.indexOf(overlay) !== -1) !== (overlay === name);
    });
  }

  // ── Elements ───────────────────────────────────────────────────────────

  function nodeClasses(node) {
    const classes = ["graph-node"];
    classes.push("type-" + (node.type || "task"));
    classes.push(
      "status-" +
        (node.completeness === "status_unknown" ? "unknown" : node.status || "unknown")
    );
    if (node.ghost) classes.push("ghost");
    if (node.ghost_kind === "context") classes.push("ghost-context");
    if (node.completeness === "edges_unknown") classes.push("edges-unknown");
    if (node.flagged) classes.push("flagged");
    if (node.blocked_via_cycle) classes.push("blocked-via-cycle");
    if (blocked[node.id]) classes.push("blocked");
    if ((node.claims || []).length) classes.push("claimed");
    // NOT the chain: which chain is traced depends on the focus, and the focus
    // moves without this page being rebuilt (D8). `render` owns that class.
    return classes.join(" ");
  }

  // A ghost's project chip, which is the whole reason a ghost is legible: it
  // says whose work the endpoint is (D5). Wrapped onto its own line rather
  // than squeezed into the node, which has no room for it.
  function nodeLabel(node) {
    const projects = node.projects || [];
    if (node.ghost && projects.length) {
      return node.label + "\n· " + projects.join(" · ");
    }
    return node.label;
  }

  // One opaque id per payload edge, assigned once and looked up by position.
  const edgeElementId = edges.map(function (_edge, index) { return "e" + index; });

  function edgeId(index) {
    return edgeElementId[index];
  }

  //: The payload edge a drawn element stands for — the same indirection
  //: `nodeFor` is, and for the same reason: the element's id is opaque.
  const edgeByElementId = dict();
  edges.forEach(function (edge, index) {
    edgeByElementId[edgeId(index)] = edge;
  });

  // Two sets, and the split is the layout's (below): every node, plus the
  // DEPENDENCY edges, are what the picture's shape is computed from; the
  // overlay edges are added afterwards.
  const elements = [];
  const overlayElements = [];
  //: A cycle box's members, by the OPAQUE element id — what `applyChain` needs
  //: to decide whether the box is on the chain being traced right now.
  const cycleMembersOfElement = dict();
  Object.keys(cycleParent).forEach(function (id) {
    cycleMembersOfElement[cycleParent[id]] = cycleMembers[id] || [];
    elements.push({
      data: { id: cycleParent[id], label: "cycle" },
      classes: "graph-cycle"
    });
  });
  nodes.forEach(function (node) {
    const data = { id: elementIdOf[node.id], label: nodeLabel(node) };
    const parent = cycleParent[node.cycle];
    if (parent) data.parent = parent;
    elements.push({ data: data, classes: nodeClasses(node) });
  });
  edges.forEach(function (edge, index) {
    const overlay = OVERLAY_BY_EDGE_TYPE[edge.type];
    const classes = ["graph-edge", "type-" + edge.type];
    if (edge.state) classes.push("state-" + edge.state);
    if (overlay) classes.push("overlay-" + overlay);
    (overlay ? overlayElements : elements).push({
      data: {
        id: edgeId(index),
        source: elementIdOf[edge.from],
        target: elementIdOf[edge.to],
        type: edge.type,
        overlay: overlay || ""
      },
      classes: classes.join(" ")
    });
  });

  // ── Style: colour = status, shape = type, an arrowhead on every edge ────

  // The node label's size in MODEL units, and the smallest it may be allowed to
  // RENDER at. Cytoscape scales text with the viewport, so a fit that shrinks
  // the graph to the canvas shrinks the labels with it — at 320px the loom
  // graph fitted to zoom 0.26, which drew a 10px font at under three pixels.
  // Everything the canvas exists to communicate (the labels, the arrowheads,
  // which box a cycle member is in) stops being readable well before the
  // picture stops fitting, so the automatic fit stops at this floor and the
  // graph overflows instead. Panning is the operator's; so is zooming out past
  // this, which is deliberately NOT clamped — only the AUTOMATIC scaling is.
  const LABEL_FONT = 14;
  const MIN_RENDERED_FONT = 10;
  const MIN_READABLE_ZOOM = MIN_RENDERED_FONT / LABEL_FONT;
  const FIT_PADDING = 24;

  const INK = "#1e2723";
  const MUTED = "#65716b";
  const LINE = "#ded6c7";
  const ACCENT = "#bd4f2b";
  const OK = "#356a4d";
  const CLOSED = "#41576f";
  const CANCELLED = "#8a4a3b";
  const WARNING = "#d58a1f";

  const style = [
    {
      selector: "node",
      style: {
        label: "data(label)",
        "text-wrap": "wrap",
        "text-max-width": 150,
        "text-valign": "bottom",
        "text-margin-y": 4,
        "font-size": LABEL_FONT,
        "font-family": "ui-sans-serif, system-ui, sans-serif",
        color: INK,
        shape: "ellipse",
        width: 26,
        height: 26,
        "background-color": "#fffaf0",
        "border-width": 1.5,
        "border-color": MUTED
      }
    },
    // Shape = type.
    { selector: "node.type-epic", style: { shape: "round-rectangle", width: 44, height: 26 } },
    { selector: "node.type-gate", style: { shape: "diamond", width: 32, height: 32 } },
    // Colour = status.
    { selector: "node.status-open", style: { "background-color": "#f6e9d2", "border-color": ACCENT } },
    { selector: "node.status-completed", style: { "background-color": "#dcebe0", "border-color": OK } },
    { selector: "node.status-cancelled", style: { "background-color": "#efdcd6", "border-color": CANCELLED } },
    // An unread status is visibly provisional rather than quietly neutral —
    // the same convention the text rows use for a ghost Lens could not read.
    {
      selector: "node.status-unknown",
      style: { "background-color": "#fff0cf", "border-color": WARNING, "border-style": "dashed" }
    },
    // A node something in this graph still blocks, tinted rather than
    // recoloured: its STATUS is still what the fill says.
    { selector: "node.blocked", style: { "background-blacken": -0.12, "border-color": CLOSED } },
    // Cycle membership is Lithos's verdict, so it marks the node whatever the
    // condensation drew.
    { selector: "node.flagged", style: { "border-color": ACCENT, "border-width": 2.5 } },
    { selector: "node.edges-unknown", style: { "border-style": "dashed", "border-color": WARNING } },
    // One hop outside the scope, its own edges never fetched.
    { selector: "node.ghost", style: { opacity: 0.55, "border-style": "dashed" } },
    // T1's cycle convention, as a compound parent: the members are bracketed
    // together and the box is what the legend's "cycle" line describes.
    {
      selector: "node.graph-cycle",
      style: {
        label: "data(label)",
        shape: "round-rectangle",
        "text-valign": "top",
        // The same size as a node's label, and for the same reason: the zoom
        // floor is derived from LABEL_FONT, so anything smaller is guaranteed
        // to render below legibility exactly when the graph is clipped.
        "font-size": LABEL_FONT,
        color: ACCENT,
        "background-color": ACCENT,
        "background-opacity": 0.07,
        "border-width": 2,
        "border-style": "dashed",
        "border-color": ACCENT,
        padding: 10
      }
    },
    // The pulse's resting ring — see `pulse()`; a claimed task is one somebody
    // is working on right now, which a still frame cannot say on its own.
    {
      selector: "node.claimed",
      style: { "overlay-color": ACCENT, "overlay-opacity": 0.06, "overlay-padding": 5 }
    },
    { selector: "node.focused", style: { "border-color": ACCENT, "border-width": 3.5 } },
    // ARROWHEADS ON EVERY EDGE (D8): direction is the one thing about a
    // dependency graph that must not be readable two ways.
    {
      selector: "edge",
      style: {
        "curve-style": "bezier",
        "target-arrow-shape": "triangle",
        "target-arrow-color": MUTED,
        "arrow-scale": 1.1,
        "line-color": MUTED,
        width: 1.6
      }
    },
    { selector: "edge.type-blocks", style: { "line-style": "solid" } },
    { selector: "edge.type-waits_on_gate", style: { "line-style": "dashed" } },
    // The overlays: thin and light for hierarchy, dotted for provenance, so
    // neither competes with the dependency flow when it is switched on.
    {
      selector: "edge.type-parent_child",
      style: { width: 0.9, "line-color": LINE, "target-arrow-color": LINE }
    },
    {
      selector: "edge.type-discovered_from",
      style: {
        "line-style": "dotted",
        "line-color": CLOSED,
        "target-arrow-color": CLOSED,
        width: 1.2
      }
    },
    // Faded history: the plan happened, it constrains nothing now.
    { selector: "edge.state-inactive", style: { opacity: 0.3 } },
    // Neither active nor inactive: an endpoint Lens could not read.
    {
      selector: "edge.state-unknown",
      style: { "line-style": "dashed", "line-color": WARNING, "target-arrow-color": WARNING }
    },
    // The longest blocking chain, traced (D7).
    {
      selector: "edge.chain",
      style: { "line-color": ACCENT, "target-arrow-color": ACCENT, width: 3 }
    },
    { selector: "node.chain", style: { "border-color": ACCENT } },
    // ── Focus mode (D8), LAST so it wins over the vocabulary above ────────
    //
    // Only two of the three classes draw anything. `focus-lit` marks the
    // answer — the label is what carries a node's identity, so it is the
    // label that strengthens — while the work is done by taking the rest
    // DOWN: a ghost stays at its own opacity when lit, because it is still a
    // ghost, and forcing it opaque would trade one honest signal for another.
    { selector: "node.focus-lit", style: { "font-weight": "bold" } },
    { selector: ".focus-dimmed", style: { opacity: 0.15 } },
    // Neither lit nor dimmed: Lens cannot say how this node relates to the
    // focus, and the same provisional style the unreadable statuses use says
    // so rather than a shade between the two, which would read as a degree.
    {
      selector: "node.focus-unknown",
      style: { "border-style": "dashed", "border-color": WARNING, opacity: 0.8 }
    },
    {
      selector: "edge.focus-unknown",
      style: {
        opacity: 0.8,
        "line-color": WARNING,
        "target-arrow-color": WARNING
      }
    }
  ];

  // Revealed before Cytoscape is constructed: it measures the container it is
  // handed, and a hidden one has no size to measure.
  container.hidden = false;

  const cy = window.cytoscape({
    container: container,
    elements: elements,
    style: style,
    // A drag is a PAN, always. Cytoscape makes nodes grabbable by default, so
    // dragging one would move it — and "once placed, nothing moves" is the
    // whole reason the ranks can be trusted against the text layers. Box
    // selection is off for the same reason it is the other default that claims
    // the background drag: panning is how a clipped graph is read, and it may
    // not depend on which part of the canvas the operator happened to grab.
    autoungrabify: true,
    boxSelectionEnabled: false
  });

  // ONE layout, and never again (D8). No physics: a graph that drifts while it
  // is read is a graph nobody can point at.
  //
  // The roots are the SERVER's — every in-degree-zero node of the condensed
  // graph plus one representative per cyclic condensation (D4) — handed over
  // as a collection rather than as the list of ids the payload carries,
  // because `breadthfirst` takes a collection or a selector and silently falls
  // back to roots of its own choosing for anything else. They decide which end
  // of the graph the traversal starts from, and so the left-to-right order the
  // placement below reads off it.
  const rootIds = dict();
  ((payload && payload.roots) || []).forEach(function (id) {
    if (byId[id]) rootIds[id] = true;
  });
  cy.layout({
    name: "breadthfirst",
    directed: true,
    roots: cy.nodes().filter(function (element) {
      const node = nodeFor(element);
      return !!node && rootIds[node.id] === true;
    }),
    padding: 20,
    spacingFactor: 1.15,
    avoidOverlap: true,
    animate: false
  }).run();

  // The overlays join AFTER the layout, and that is the whole reason they are
  // held back: `breadthfirst` reads every edge it is given, so an epic's
  // `parent_child` edges — off by default, and arriving from a node that is
  // itself folded away — would flatten the dependency chain the page is
  // FOR into one row of the epic's children. The shape is the dependency
  // structure, the same one the text layers state; hierarchy and provenance
  // are annotations drawn over it. Adding elements moves nothing that is
  // already placed.
  if (overlayElements.length) cy.add(overlayElements);

  // ── Placement: the SERVER's ranks, the layout's order within them ──────
  //
  // `breadthfirst` ranks by SHORTEST path from a root. The server's layering is
  // Kahn's over the condensed graph — one below the DEEPEST predecessor
  // (`graph_layout._layer`) — so on `A → B → C → D` plus `A → D` the library
  // draws D level with B while the text below it says layer 3. A picture
  // contradicting the layers it is printed above is the one thing D3 does not
  // allow, so the rank comes from `node.layer` and the layout decides only what
  // it is genuinely better at: the ORDER of the nodes across a rank, from the
  // server's own roots.
  //
  // (`breadthfirst`'s own `maximal` option is the server's rule, and would have
  // been the whole fix — but the library abandons it the moment the graph has a
  // cycle, which a dependency graph routinely does, and falls silently back to
  // shortest path. A promise that lapses exactly where this page is most
  // interesting is not one to build on.)
  //
  // A slot is a CONDENSATION, not a node, for the same reason the server layers
  // one: a cycle occupies one place in its layer, and its members stack inside
  // that slot so the compound box hugs them instead of stretching across
  // whatever the layout happened to put between.
  //
  // Which is why the ranks are stacked CUMULATIVELY rather than at a fixed
  // pitch. A rank is as tall as its tallest condensation, and nothing bounds
  // an SCC below the 300-node scope guard: a five-member cycle stacked at a
  // fixed 110 spills 128px past its own band, putting one member above the
  // rank before it and another below the rank after. The band then says the
  // opposite of the layer, which is the whole thing this placement is for.
  const ROW_GAP = 110;
  const COLUMN_PITCH = 170;
  const MEMBER_PITCH = 64;
  const rows = dict();
  const slotMembers = dict();
  cy.nodes().forEach(function (element) {
    const node = nodeFor(element);
    if (!node) return; // a cycle's compound parent, placed by its children
    const slot = condensationOf(node.id);
    if (!slotMembers[slot]) {
      slotMembers[slot] = [];
      const layer = node.layer || 0;
      (rows[layer] = rows[layer] || []).push(slot);
    }
    slotMembers[slot].push(element);
  });
  let top = 0;
  Object.keys(rows)
    .map(Number)
    .sort(function (a, b) { return a - b; })
    .forEach(function (layer) {
      const slots = rows[layer];
      const centre = dict();
      let tallest = 0;
      slots.forEach(function (slot) {
        let x = 0;
        slotMembers[slot].forEach(function (element) { x += element.position().x; });
        centre[slot] = x / slotMembers[slot].length;
        tallest = Math.max(tallest, (slotMembers[slot].length - 1) * MEMBER_PITCH);
      });
      // The layout's left-to-right order, with the id as the tiebreak so a rank
      // it placed in a column renders the same way twice.
      slots.sort(function (a, b) {
        return centre[a] - centre[b] || (a < b ? -1 : 1);
      });
      const middle = top + tallest / 2;
      slots.forEach(function (slot, index) {
        const x = (index - (slots.length - 1) / 2) * COLUMN_PITCH;
        const members = slotMembers[slot];
        members.forEach(function (element, member) {
          element.position({
            x: x,
            y: middle + (member - (members.length - 1) / 2) * MEMBER_PITCH
          });
        });
      });
      top += tallest + ROW_GAP;
    });

  // ── What is drawn (D6/D8), recomputed from the URL on every transition ──

  function visibility(state) {
    const on = dict();
    state.overlays.forEach(function (name) { on[name] = true; });
    const shownEdges = dict();
    const anchored = dict();
    edges.forEach(function (edge, index) {
      const overlay = OVERLAY_BY_EDGE_TYPE[edge.type];
      if (overlay && !on[overlay]) return;
      shownEdges[edgeId(index)] = true;
      anchored[edge.from] = true;
      anchored[edge.to] = true;
    });
    const shownNodes = dict();
    nodes.forEach(function (node) {
      let visible = true;
      if (node.ghost_kind === "context") {
        // A context ghost exists only to anchor an overlay edge (D6), so it
        // appears exactly when that overlay does and never on its own.
        visible = !!anchored[node.id];
      } else if (isolated[node.id]) {
        // Folded away by default on a project graph — unless an overlay that
        // is switched ON connects it, in which case hiding it would hide the
        // very edges the operator just asked for (an epic's whole hierarchy
        // hangs off a node with no dependency edge of its own).
        visible = state.isolated || !!anchored[node.id];
      }
      shownNodes[node.id] = visible;
    });
    return { nodes: shownNodes, edges: shownEdges };
  }

  // Refit the VIEWPORT to whatever is drawn — a pan and a zoom, never a new
  // layout: the nodes keep the positions the one layout gave them, so nothing
  // the operator does rearranges the picture. Without it, an overlay switched
  // off leaves the graph in a corner of its own canvas.
  //
  // Floored at MIN_READABLE_ZOOM. A fit that has to go below it is a picture
  // nobody can read — so the graph is shown at legible size, centred on what is
  // drawn, and overflows the canvas for the operator to pan.
  //
  // Whether it came to that is recorded HERE, beside the decision, rather than
  // by the caller: a resize refits too (the panel opening beside the canvas is
  // one), and a hint left behind by the previous width would claim a limit that
  // is no longer there.
  function drawnNodes() {
    return cy.nodes().filter(function (element) {
      return element.style("display") !== "none";
    });
  }

  // Whether anything is actually OUT of view, in the canvas's own coordinates.
  //
  // Measured, not inferred from the floor having bound the zoom: the floor
  // zooms IN, and a fit that was a hair below it still has everything on
  // screen afterwards. And measured by POSITION, not by size — a graph smaller
  // than the canvas is off screen all the same if it has been panned past the
  // edge, and one the size of the canvas is fully visible only when it also
  // sits inside it.
  const EDGE_TOLERANCE = 1;

  function reportVisibility() {
    const drawn = drawnNodes();
    // An empty canvas hides nothing, so it may not go on saying it does. A
    // scope whose whole picture is isolates leaves exactly that behind when
    // they are folded away again, and the notice — which is the operator's
    // instruction to drag — would be pointing at a graph that is not there.
    let clipped = false;
    if (drawn.length) {
      const extent = drawn.renderedBoundingBox();
      clipped =
        extent.x1 < -EDGE_TOLERANCE ||
        extent.y1 < -EDGE_TOLERANCE ||
        extent.x2 > cy.width() + EDGE_TOLERANCE ||
        extent.y2 > cy.height() + EDGE_TOLERANCE;
    }
    container.dataset.canvasClipped = clipped ? "true" : "false";
    if (panHint) panHint.hidden = !clipped;
  }

  function fitVisible() {
    const drawn = drawnNodes();
    // Nothing to fit the viewport around — but the notice still has to be told
    // so, or it survives the transition that emptied the canvas.
    if (!drawn.length) {
      reportVisibility();
      return;
    }
    cy.fit(drawn, FIT_PADDING);
    if (cy.zoom() < MIN_READABLE_ZOOM) {
      cy.zoom(MIN_READABLE_ZOOM);
      cy.center(drawn);
    }
    reportVisibility();
  }

  // The three focus classes, applied together so a node never carries two of
  // them and never keeps one from a focus that has been cleared.
  const FOCUS_CLASSES = "focus-lit focus-dimmed focus-unknown";

  function applyFocusClass(element, name) {
    element.removeClass(FOCUS_CLASSES);
    if (name) element.addClass(name);
  }

  function nodeFocusClass(sets, id) {
    if (!sets) return "";
    // Unknown FIRST: a node whose own edges are unreadable is unplaceable
    // relative to the focus even when an edge somebody else reported reaches
    // it, so "lit" there would claim a relation Lens cannot see the whole of.
    if (sets.unknown[id]) return "focus-unknown";
    return sets.lit[id] ? "focus-lit" : "focus-dimmed";
  }

  function edgeFocusClass(sets, edge) {
    if (!sets || !edge) return "";
    if (edge.state === "unknown" && (sets.lit[edge.from] || sets.lit[edge.to])) {
      return "focus-unknown";
    }
    return sets.lit[edge.from] && sets.lit[edge.to]
      ? "focus-lit"
      : "focus-dimmed";
  }

  //: The drawn element for a task, or an empty collection for one this page
  //: has no node for.
  function elementFor(taskId) {
    return cy.getElementById(elementIdOf[taskId] || "");
  }

  // Centred, not re-fitted (D8): focus mode is about ONE node's neighbourhood,
  // and the fit it follows is what keeps the rest of the picture available to
  // pan back to. Nothing MOVES — this is the viewport again.
  //
  // Called after every fit, not only from `render`: opening the panel beside
  // the canvas narrows it, and the resize that follows re-fits the whole drawn
  // collection — which would quietly undo the centring the click just applied
  // (round-1 correctness f-003).
  function centreFocus() {
    // Not in a mini-graph. There, focus mode's subject and the whole picture
    // are the same thing — the box was fitted to hold this task's
    // neighbourhood — and centring on the task inside it pushes the blockers
    // above it off the top of a canvas that had room for them (T2-A5).
    if (mini) return;
    const state = stateFromUrl();
    if (!state.focus) return;
    const element = elementFor(state.focus);
    if (!element.length || element.style("display") === "none") return;
    cy.center(element);
    reportVisibility();
  }

  // The longest chain the page currently claims (D7), in both places it is
  // claimed: traced on the canvas and written in the line above the layers.
  // Both follow the focus, because the chain THROUGH the focused node replaces
  // the scope's — and a focus transition never reloads the page, so a trace
  // left at the chain the payload was built for would state a sequence this
  // picture is no longer about (round-1 correctness f-001).
  function applyChain(state) {
    setChain(chainFor(state.focus));
    cy.nodes().forEach(function (element) {
      const node = nodeFor(element);
      if (node) {
        element.toggleClass("chain", onChain(node.id));
        return;
      }
      // A cycle's compound parent. The box is on the chain when any task
      // inside it is: usually the whole box is one chain node — the trace
      // enters and leaves the cycle in one move, and a box drawn plain between
      // two traced edges would read as a break in the sequence — but where the
      // active projection splits this loop up, the chain runs THROUGH the box
      // and the same accent is what says so.
      const members = cycleMembersOfElement[element.id()] || [];
      element.toggleClass("chain", members.some(onChain));
    });
    cy.edges().forEach(function (element) {
      element.toggleClass("chain", stepOnChain(edgeByElementId[element.id()]));
    });
    writeChainLine(state);
  }

  // The text line the canvas is printed above, kept in step with the trace.
  // Only the two parts that DEPEND on the focus are rewritten: the "through
  // X" clause, the length and the node list. The lower-bound wording beside
  // them is the scope's own (an unreadable edge anywhere, D7) and does not
  // move with the focus, so the server's sentence for it stands.
  function writeChainLine(state) {
    const section = document.querySelector("[data-longest-chain]");
    if (!section) return;
    const focused = state.focus && chainCondensationOfId[state.focus];
    if (focused) section.dataset.chainThrough = state.focus;
    else delete section.dataset.chainThrough;
    const clause = section.querySelector("[data-chain-through-label]");
    if (clause) {
      // The FOCUSED task's own label, not its condensation's. The two differ
      // exactly when the focus is a non-representative member of a live cycle,
      // and there the representative is a different task with a different
      // name: the server says "through Ring-B" for `focus=ring-b`
      // (`graph_page` reads `views.get(focus)`), and a client naming Ring-A
      // would make a click disagree with a reload of the URL it just pushed
      // (round-2 correctness f-005). Only the chain's NODE LIST is condensed —
      // that list is a walk over condensations, and each is named by its
      // representative.
      clause.textContent = focused ? " through " + labelOf(state.focus) : "";
    }
    const length = section.querySelector("[data-chain-length]");
    if (length) length.textContent = String(chain.length);
    const nodes = section.querySelector("[data-chain-nodes]");
    if (nodes) nodes.textContent = chain.map(labelOf).join(" → ");
  }

  function labelOf(taskId) {
    const node = byId[taskId];
    return (node && node.label) || taskId;
  }

  function render() {
    const state = stateFromUrl();
    const shown = visibility(state);
    // No lit/dimmed pass in a mini-graph: D8's exploration classes answer
    // "what surrounds this node in a graph of a hundred?", and here the answer
    // is every node on the canvas. Dimming the parent epic — the one member
    // no dependency edge reaches — would fade the very node D11 put there.
    const focus = mini ? null : focusSets(state.focus);
    let nodeCount = 0;
    let ghostCount = 0;
    cy.nodes().forEach(function (element) {
      const node = nodeFor(element);
      if (!node) return; // a cycle's compound parent, which its members carry
      const visible = shown.nodes[node.id];
      element.style("display", visible ? "element" : "none");
      if (visible) {
        nodeCount += 1;
        if (node.ghost) ghostCount += 1;
      }
      if (node.id === state.focus) element.addClass("focused");
      else element.removeClass("focused");
      applyFocusClass(element, nodeFocusClass(focus, node.id));
    });
    let edgeCount = 0;
    cy.edges().forEach(function (element) {
      const visible = !!shown.edges[element.id()];
      element.style("display", visible ? "element" : "none");
      if (visible) edgeCount += 1;
      applyFocusClass(
        element, edgeFocusClass(focus, edgeByElementId[element.id()])
      );
    });
    applyChain(state);
    fitVisible();
    centreFocus();
    container.dataset.canvasState = "ready";
    container.dataset.canvasNodes = String(nodeCount);
    container.dataset.canvasEdges = String(edgeCount);
    container.dataset.canvasGhosts = String(ghostCount);
    container.dataset.canvasCycles = String(Object.keys(cycleParent).length);
    syncToolbar(state);
  }

  function syncToolbar(state) {
    document.querySelectorAll("[data-toggle-overlay]").forEach(function (link) {
      const name = link.dataset.toggleOverlay;
      const on = state.overlays.indexOf(name) !== -1;
      link.dataset.overlayOn = on ? "true" : "false";
      link.setAttribute("aria-pressed", on ? "true" : "false");
      link.textContent = (on ? "Hide " : "Show ") + name;
      // The href follows the state too, so the no-JS meaning of this link and
      // the client-side one never diverge after a toggle.
      link.setAttribute("href", urlWith({ overlays: toggled(state.overlays, name) }));
    });
    document.querySelectorAll("[data-toggle-isolated]").forEach(function (link) {
      link.textContent = state.isolated ? "Hide isolated tasks" : "Show isolated tasks";
      link.setAttribute("href", urlWith({ isolated: !state.isolated }));
    });
    // The two links the SERVER built and the client never re-applies. They are
    // still rebuilt from the live URL on every render, because every other
    // control here moves that URL without a reload: a pill still pointing at
    // the address the page loaded on would silently drop the overlays and the
    // focus the operator set on the way to needing it, which makes its own
    // "this is a refresh" contract false. `include_resolved` genuinely needs
    // the server, so its link stays a navigation — it just has to be a
    // navigation from HERE.
    document.querySelectorAll("[data-toggle-resolved]").forEach(function (link) {
      link.setAttribute(
        "href",
        urlWith({ includeResolved: !state.includeResolved })
      );
    });
    document.querySelectorAll("[data-graph-refresh-pill]").forEach(function (pill) {
      pill.setAttribute("href", urlWith({}));
    });
    // The text disclosure and the canvas answer the same question, so they are
    // never allowed to disagree about whether isolates are being shown.
    const disclosure = document.querySelector("[data-isolated-disclosure]");
    if (disclosure) disclosure.open = state.isolated;
  }

  // ── The text baseline, collapsed behind a toggle (D3) ──────────────────
  //
  // Collapsed, never removed: the text IS the page for a screen reader, a
  // PR screenshot and a browser with no canvas, and it stays in the DOM.

  const textSections = document.querySelectorAll("[data-graph-text]");
  const textToggle = document.querySelector("[data-toggle-text]");
  let textShown = true;

  function showText(next) {
    textShown = next;
    textSections.forEach(function (section) { section.hidden = !next; });
    if (!textToggle) return;
    textToggle.textContent = next ? "Hide text" : "Show as text";
    textToggle.setAttribute("aria-expanded", next ? "true" : "false");
  }

  if (textToggle) textToggle.hidden = false;
  showText(false);

  // ── Search (D8): jump to a task within the payload ─────────────────────
  //
  // Title SUBSTRING or id PREFIX, which is the asymmetry the two identifiers
  // deserve: a title is read and remembered in fragments, an id is copied and
  // pasted from its start. Matching is over the payload's own nodes, so a
  // hundred-node graph is navigable with no fetch and no server round trip,
  // and a hit is an ordinary focus transition — the same one a node click is.
  const searchControl = document.querySelector("[data-graph-search-control]");
  const searchInput = document.querySelector("[data-graph-search]");
  const searchResults = document.querySelector("[data-graph-search-results]");
  const SEARCH_LIMIT = 8;

  function searchMatches(query) {
    // Two domains, so two readings of the same keystrokes. A TITLE is prose:
    // the spaces around what somebody typed are not part of what they meant,
    // so it is matched against the trimmed query. An ID is an arbitrary
    // non-empty string (§5.1) — `" task "` is an id Lens can really be handed,
    // which is why `focus=` carries one byte for byte — so trimming here would
    // make that id's own prefix unsearchable and an all-spaces id unreachable
    // (round-6 correctness f-011). Only a truly EMPTY box offers nothing.
    const raw = String(query || "").toLowerCase();
    const needle = raw.trim();
    if (!raw) return [];
    return nodes
      .filter(function (node) {
        const label = String(node.label || "").toLowerCase();
        return (
          (needle !== "" && label.indexOf(needle) !== -1) ||
          String(node.id).toLowerCase().indexOf(raw) === 0
        );
      })
      .slice(0, SEARCH_LIMIT);
  }

  function renderSearch(query) {
    if (!searchResults) return;
    const items = searchMatches(query).map(function (node) {
      const item = document.createElement("li");
      const hit = document.createElement("button");
      hit.type = "button";
      hit.className = "graph-search-hit";
      hit.textContent = node.label;
      hit.dataset.searchHit = node.id;
      item.appendChild(hit);
      return item;
    });
    searchResults.replaceChildren.apply(searchResults, items);
  }

  function clearSearch() {
    if (searchInput) searchInput.value = "";
    renderSearch("");
  }

  if (searchControl && searchInput) {
    searchControl.hidden = false;
    searchInput.addEventListener("input", function () {
      renderSearch(searchInput.value);
    });
    searchInput.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      // A search box inside a page of links: Enter means "take the first
      // match", never "submit me somewhere".
      event.preventDefault();
      const first = searchMatches(searchInput.value)[0];
      if (!first) return;
      clearSearch();
      focusOn(first.id);
    });
  }

  // ── Interaction ────────────────────────────────────────────────────────

  function panel() {
    return (window.LithosLens || {}).panel || null;
  }

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented) return;
    // Every control below is the graph PAGE's: the overlay and isolated
    // toggles, the search results, the show-as-text button. A mini-graph has
    // none of them, and a handler bound from it would answer for the page's
    // if both were ever on one document.
    if (mini) return;
    // Modified and non-primary clicks keep their browser meaning — these are
    // real links, and "open in a new tab" has to stay that.
    if (event.button !== undefined && event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = event.target;
    if (!target || !target.closest) return;

    const overlayLink = target.closest("[data-toggle-overlay]");
    if (overlayLink) {
      event.preventDefault();
      const state = stateFromUrl();
      window.history.pushState(
        {},
        "",
        urlWith({ overlays: toggled(state.overlays, overlayLink.dataset.toggleOverlay) })
      );
      render();
      return;
    }
    const isolatedLink = target.closest("[data-toggle-isolated]");
    if (isolatedLink) {
      event.preventDefault();
      window.history.pushState({}, "", urlWith({ isolated: !stateFromUrl().isolated }));
      render();
      return;
    }
    const hit = target.closest("[data-search-hit]");
    if (hit) {
      event.preventDefault();
      const taskId = hit.dataset.searchHit;
      clearSearch();
      focusOn(taskId);
      return;
    }
    const toggle = target.closest("[data-toggle-text]");
    if (toggle) {
      event.preventDefault();
      showText(!textShown);
    }
  });

  // Back and forward walk the exploration without a reload: the URL is the
  // state, and everything it names is already in the payload.
  if (!mini) window.addEventListener("popstate", render);

  // And so does every PANEL transition, which moves the same URL by
  // `pushState` — and `pushState` fires no `popstate`. Without this, closing
  // the panel (or Escape) clears `focus` and the panel while the node stays
  // lit: the canvas would claim a selection the page's one selection parameter
  // no longer names.
  const panelApi = mini ? null : panel();
  if (panelApi && panelApi.onChange) panelApi.onChange(render);
  // …and every panel this page opens is told what the canvas beside it shows.
  if (panelApi && panelApi.describe) panelApi.describe(canvasNotes);

  // What the previous tap was on, whatever it was on.
  //
  // Cytoscape's multi-click detector measures TIME and nothing else: two taps
  // inside `multiClickDebounceTime` are a `dbltap` on whatever the second one
  // hit, even when the first hit a different node or the empty background. So
  // the library's own `dbltap` is "two clicks in a row", not "two clicks on
  // this" — and only the page knows which one it meant (round-2 correctness
  // f-002). Recorded on the raw `tap`, which is emitted for every click and
  // always before the `dbltap` that may follow it.
  let previousTapId = "";
  let latestTapId = "";
  cy.on("tap", function (event) {
    const target = event.target;
    const isElement = target && typeof target.isNode === "function";
    previousTapId = latestTapId;
    // The background taps as the CORE, which has no id — and must not compare
    // equal to a node's, or a click on empty canvas would pass for half of
    // that node's double-click.
    latestTapId = isElement ? target.id() : "";
  });

  // The focus ring lands on the FIRST tap, so a click answers immediately …
  cy.on("tap", "node", function (event) {
    // A mini-graph has no selection to move: the ring is on the task whose
    // page this is, and the server put it there. The two gestures below are
    // the page's too — a click opens the side panel this host does not have,
    // and the task's own detail page is the one the operator is already on.
    if (mini) return;
    if (!nodeFor(event.target)) return; // the cycle box, chrome not a task
    // … and this tap OPENS a gesture, so the canvas must not move again until
    // it settles. An open started by an EARLIER click can still be in flight,
    // and the debounce below does not cover it: its response would insert the
    // panel beside the canvas, narrow it and refit it between the two clicks
    // of a double-click, moving this node out from under the pointer exactly
    // as the debounce exists to prevent (round-2 correctness f-001). So the
    // pending open is superseded here, on the raw tap — the panel already on
    // screen is untouched, and the gesture about to settle will say what
    // replaces it.
    const pending = panel();
    if (pending && pending.supersedePending) pending.supersedePending();
    cy.nodes().forEach(function (element) {
      if (element.id() === event.target.id()) element.addClass("focused");
      else element.removeClass("focused");
    });
  });

  // … but the PANEL waits for the click to settle as a single one.
  //
  // Both gestures live on the same node, and opening the panel is not a
  // passive act: it appears BESIDE the canvas (D9), which narrows the canvas,
  // which refits the viewport — moving the node out from under a pointer that
  // is halfway through a double-click. The second click then lands on the
  // background, no `dbltap` is emitted and the operator is left on the graph
  // with a panel they did not ask for (round-2 correctness f-001).
  //
  // `onetap` is Cytoscape's own answer to that: a tap it has held for
  // `multiClickDebounceTime` (250ms) and no second tap arrived, so the gesture
  // IS a single click. The cost is that the panel opens a quarter-second after
  // the click rather than on it — paid deliberately, and only for the panel:
  // the ring above is the immediate acknowledgement, and the fetch this saves
  // on every double-click is one the page would have thrown away anyway.
  //
  // The panel's own `pushState` follows its fetch, which is what keeps a URL
  // from ever claiming a panel that failed to open — and an open that never
  // arrives announces itself too, so the ring is walked back rather than left
  // over a selection that never happened.
  cy.on("onetap", "node", function (event) {
    if (mini) return;
    const node = nodeFor(event.target);
    if (!node) return;
    focusOn(node.id);
  });

  // Double-click leaves for the full page — but only a double-click on THIS
  // node. The tap that preceded it lit the node and nothing else, so there is
  // no panel open to supersede; the navigation is the whole answer, which is
  // the same order the dashboard's title link has always had.
  cy.on("dbltap", "node", function (event) {
    const node = nodeFor(event.target);
    if (!node) return;
    if (mini) {
      // The one navigation a mini-graph offers: a neighbour's own page. It
      // opens no panel on the way, because this host has none — and the focal
      // node's `detail_url` is the page the operator is reading, so a
      // double-click there is a reload and nothing worse.
      if (previousTapId === event.target.id() && node.detail_url) {
        window.location.href = node.detail_url;
      }
      return;
    }
    if (previousTapId !== event.target.id()) {
      // Two clicks, two targets: the operator clicked one node and then
      // another (or the background and then a node) in quick succession, and
      // the library called the pair a double-click on the second. Navigating
      // would leave the page from a node clicked ONCE — and the same debounce
      // has already swallowed that node's `onetap`, so this is where its
      // single click has to be answered instead.
      focusOn(node.id);
      return;
    }
    if (node.detail_url) window.location.href = node.detail_url;
  });

  // ── "Graph changed — refresh" (D8): never an auto re-layout ─────────────

  const pill = document.querySelector("[data-graph-refresh-pill]");
  const live = (window.LithosLens || {}).events;
  if (pill && live) {
    live.subscribe(function (message, type) {
      // Task events only, and only for a node actually on this page. Edge
      // upserts emit no event at all (ROADMAP gap #1), which is why the
      // toolbar's `as of` stamp is the page's real staleness bound and this
      // pill is a hint rather than a guarantee.
      if (typeof type !== "string" || type.indexOf("task.") !== 0) return;
      if (!message || !message.task_id || !byId[message.task_id]) return;
      pill.hidden = false;
    });
  }

  // The partial-view state is the OPERATOR's as much as the layout's: panning
  // a clipped graph back into view makes it whole, and zooming in on a fitted
  // one takes it out of view again. Both move the viewport and neither refits,
  // so the notice is recomputed on `viewport` — Cytoscape's own pan/zoom event
  // — or it would go on claiming whatever the last automatic fit concluded.
  //
  // Coalesced to one frame: `viewport` fires per pan step, and the answer is
  // only ever read by eye.
  let visibilityPending = false;
  cy.on("viewport", function () {
    if (visibilityPending) return;
    if (typeof window.requestAnimationFrame !== "function") {
      reportVisibility();
      return;
    }
    visibilityPending = true;
    window.requestAnimationFrame(function () {
      visibilityPending = false;
      reportVisibility();
    });
  });

  // Cytoscape sizes its canvas to the container once and does not watch it, so
  // the panel opening BESIDE the canvas (D9) — which narrows it — would leave
  // the graph drawn across the panel that just opened. Same for a window
  // resize. Re-measured and re-fitted, never re-laid-out.
  if (typeof window.ResizeObserver === "function") {
    let width = container.clientWidth;
    let height = container.clientHeight;
    new window.ResizeObserver(function () {
      if (container.clientWidth === width && container.clientHeight === height) {
        return;
      }
      width = container.clientWidth;
      height = container.clientHeight;
      cy.resize();
      fitVisible();
      // …and put the focus back in the middle. `fitVisible` centres the whole
      // DRAWN collection, so a resize while focused — which is what opening
      // the panel beside the canvas is — would otherwise undo the centring the
      // click that opened it just applied (round-1 correctness f-003).
      centreFocus();
    }).observe(container);
  }

  // ── First paint ────────────────────────────────────────────────────────

  render();

  // A claimed task breathes, because "somebody is working on this right now"
  // is the one status a still picture cannot carry. Guarded on the animation
  // API and on the operator's motion preference; without either, the resting
  // ring in the stylesheet above is what a claimed node looks like.
  const reduceMotion =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reduceMotion) {
    cy.nodes(".claimed").forEach(function (node) {
      if (typeof node.animate !== "function") return;
      const breathe = function (to, next) {
        node.animate({ style: { "overlay-opacity": to } }, { duration: 900, complete: next });
      };
      const loop = function () { breathe(0.2, function () { breathe(0.06, loop); }); };
      loop();
    });
  }

  // D8's "load with focus=A → A's panel open". The server renders that panel
  // itself (D9's no-JS baseline), so the only case left here is a focus the
  // server did not answer — a scope error on its read, or a `focus` that
  // arrived without one. The push is suppressed either way: the URL already
  // names this selection, and a second identical history entry would make the
  // first Back appear to do nothing.
  const initial = mini ? { focus: "", overlays: [], isolated: true } : stateFromUrl();
  const host = mini ? null : document.querySelector("[data-panel-host]");
  const served = host && host.dataset.panelSelected === initial.focus && host.innerHTML;
  // A deep link onto a task this page is not drawing — a folded isolate, or a
  // context ghost whose overlay is off. The reveal D8 owes it is owed however
  // the panel got there, so it is applied before the open below rather than
  // inside it. REPLACED, not pushed — this is the address the page loaded on,
  // and a second entry for it would make the first Back a no-op.
  const initialReveal = initial.focus
    ? revealChanges(initial.focus, initial)
    : {};
  if (Object.keys(initialReveal).length) {
    window.history.replaceState({}, "", urlWith(initialReveal));
    render();
  }
  if (initial.focus && byId[initial.focus] && !served) {
    const open = panel();
    if (open) open.open(initial.focus, { push: false });
  }

  // The handle the e2e captures and the JS tests read the canvas through: a
  // Cytoscape graph is pixels, and every claim about it has to be asked of the
  // instance rather than of the image. The mini-graph publishes its own rather
  // than overwriting the page's: the two are different pictures, and a capture
  // asking `LithosLensGraph` on a detail page should get nothing rather than a
  // neighbourhood answering for a scope.
  window[mini ? "LithosLensMiniGraph" : "LithosLensGraph"] = {
    cy: cy,
    // Cytoscape's ids are opaque here (see the top of the file), so asking
    // about a TASK goes through these rather than through `getElementById`.
    node: function (taskId) { return cy.getElementById(elementIdOf[taskId] || ""); },
    cycle: function (cycleId) {
      return cy.getElementById(cycleElementId[cycleId] || "");
    },
    shown: function () {
      const drawn = { nodes: [], edges: [] };
      cy.nodes().forEach(function (element) {
        const node = nodeFor(element);
        if (node && element.style("display") !== "none") drawn.nodes.push(node.id);
      });
      cy.edges().forEach(function (element) {
        if (element.style("display") === "none") return;
        drawn.edges.push({
          id: element.id(),
          type: element.data("type"),
          arrow: element.style("target-arrow-shape")
        });
      });
      return drawn;
    },
    positions: function () {
      const at = {};
      cy.nodes().forEach(function (element) {
        const point = element.position();
        const node = nodeFor(element);
        if (node) at[node.id] = [Math.round(point.x), Math.round(point.y)];
      });
      return at;
    }
  };
};

/*
  Boot: now for a canvas the document already holds, and again for every one an
  HTMX swap brings in.

  Both are needed and neither covers the other. The graph page embeds its canvas
  in the document, so this script — deferred — finds it on the first call. The
  detail page's mini-graph is fetched after load (and re-fetched whenever
  `tasks.js` replaces the detail fragment and re-processes it), so the only
  moment it exists is the swap that delivered it. `initLensGraph` claims each
  container it takes, so a swap elsewhere on the page — a panel, a deeper
  blocker level — costs one selector query and nothing else.
*/
(function () {
  initLensGraph();
  document.addEventListener("htmx:afterSwap", initLensGraph);
})();
