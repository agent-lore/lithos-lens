/*
  The knowledge graph's canvas (K2 S4) — progressive enhancement over the
  text baseline `/knowledge/graph` renders, drawn from the SAME embedded
  payload so the picture and the text cannot disagree (D12).

  Not `graph.js`: the task graph's canvas is task semantics (layers, chains,
  cycles, claims) and is not generalised (PRD D15). The two share Cytoscape,
  the stylesheet's conventions and the "text first, canvas from the same
  payload" rule, and no code.

  Five rules shape everything below.

  1. THE CANVAS DRAWS EXACTLY THE PAYLOAD. Every node and every edge in it,
     nothing else. The server applied the weight and provenance filters
     before its cap (an edge they hide is counted, not shipped), so the canvas
     cannot lower a threshold on its own — and does not raise one either.

  2. EVERY CONTROL NAVIGATES — EXPANSION SWAPS IN PLACE (D16). The
     min-weight slider (on `change`, not while dragging), the provenance
     toggles, the depth and the colour mode each rewrite one key of the
     page's own query and load it: the server re-renders text, payload and
     canvas together, so a reload reproduces the view and the text below
     always agrees with the picture. Show its neighbours, Collapse and
     remove — and a double click on a note that can expand — fetch the same
     route for the new address instead, one request at a time, and commit
     its text, payload, panel and picture as one view before the address is
     pushed; while that request is out, every graph-changing, panel and
     full-page action is inert.

  3. NO STYLE CLAIM IS DERIVED HERE. Direction, symmetry and resolution are
     the server's: each edge carries its `style` (class tokens, stroke,
     arrowhead, label) from `knowledge_edge_types.edge_style`, and a layer
     pair is told from a typed row by `kind`, never by `type`. What this file
     owns is the palette and the sizes: a colour per edge class, a fixed slot
     palette for the node colour mode that shares no colour with it, width
     from weight and size from degree in view.

  4. SURVIVING NOTES NEVER MOVE. A load lays out its base notes once —
     concentric by hop around the focus, or a force-directed `cose` for a
     scoped graph — then places each applied expansion's notes beside the
     note expanded, in URL order. A swap diffs by persistent note and edge
     identity: what stays keeps its position (its size follows its degree),
     what is new is placed by that same rule. A reload reproduces a fresh
     load, not the live picture: the URL restores exploration, not saved
     coordinates. A click opens the S5 panel through htmx and lights the
     selection; it never moves a node.

  5. FULL PAGE IS PRESENTATION (D17). `canvas=full` draws the same view: the
     server only marks the page with it, and this file applies it — a class,
     the heading and scope line moved below the stage — so without the
     script nothing changes. "Full page" / "Exit full page" switches in
     place: the address and every graph link on the page change, the
     viewport is refitted, and no node moves.
*/
(function () {
  "use strict";

  const GRAPH_PATH = "/knowledge/graph";
  const PANEL_PATH = "/knowledge/graph/panel";
  const RENDER_KEY = "render";
  const CANVAS_KEY = "canvas";
  const PROVENANCE_GROUPS = ["inferred", "reinforced", "declared", "other"];

  const INK = "#1e2723";
  const MUTED = "#65716b";
  const PANEL = "#fffaf0";
  const ACCENT = "#bd4f2b";
  const WARNING = "#d58a1f";

  //: One colour per edge class, in the known-type order (PRD D5). A colour
  //: means the same relation on every page.
  const EDGE_COLOURS = {
    "kedge-supports": "#2b6cb0",
    "kedge-related-to": "#6b7f2a",
    "kedge-analogy-to": "#7b4fa0",
    "kedge-refines": "#1f7a72",
    "kedge-is-example-of": "#a8641f",
    "kedge-depends-on": "#3d3f8f",
    "kedge-derived-from": "#6d5843",
    "kedge-contradicts": ACCENT,
    "kedge-unknown": "#8d8d8d",
    "kedge-wiki-link": "#b8b3aa",
    "kedge-provenance": "#9c8768"
  };
  //: A resolved contradiction: muted, its resolution as its label.
  const RESOLVED = "#a3a8a4";

  //: The node colour mode's slots, light fills under dark labels, sharing no
  //: colour with the edge palette (PRD D5). Values past the last slot, and a
  //: node with no value (a ghost, a node not read), share the neutral.
  const NODE_PALETTE = [
    "#f6d365",
    "#9fd8cb",
    "#a9c8f0",
    "#f7b2a8",
    "#d7b8f3",
    "#c3e19a",
    "#f9c38b",
    "#f2b8d6"
  ];
  const NODE_NEUTRAL = "#e4dfd5";
  const ARCHIVED = "#cfcac1";

  // Weight → width, linear over [0.1, 1.0] (PRD D6); a weight the row does
  // not carry (a layer pair, a row an event inserted) draws at the thin end.
  const WEIGHT_FLOOR = 0.1;
  //: The min-weight slider's increment (S4 D4).
  const SLIDER_STEP = 0.05;
  const WIDTH_MIN = 1;
  const WIDTH_MAX = 6;
  //: An unresolved contradiction is drawn wider than its weight alone says.
  const EMPHASIS = 2;
  // Degree in view → node size, on a small range so hubs read as hubs
  // without dwarfing the rest (PRD D8).
  const SIZE_MIN = 18;
  const SIZE_MAX = 42;

  const MAX_FIT_ZOOM = 1.4;
  const FIT_PADDING = 24;
  //: How long a refit or a pan after a toggle or a panel takes, unless the
  //: operator prefers reduced motion.
  const MOTION_MS = 250;
  //: Cytoscape's double-click window (`multiClickDebounceTime`): two taps
  //: this close are a `dbltap`. Until it ends a tap's panel moves nothing,
  //: and does not show over the note tapped.
  const DOUBLE_TAP_MS = 250;
  // Labels are the canvas's content: the fit that opens it may not shrink
  // them past MIN_RENDERED_FONT pixels. A graph that cannot then fit is
  // opened at that zoom on its focus (or its centre), and the pan hint says
  // the rest is a drag away; the operator's own zoom is not bounded by this.
  const NODE_FONT = 13;
  const EDGE_FONT = 12;
  //: Every label's face: `lens.css`'s vendored @font-face first, the
  //: system's sans only while it cannot load. Labels are placed by their
  //: measured boxes, so the face decides where they go.
  const LABEL_FACE = "Lens Inter";
  const LABEL_FONT_FAMILY = '"' + LABEL_FACE + '", ui-sans-serif, system-ui, sans-serif';
  const MIN_RENDERED_FONT = 11;
  const MIN_READABLE_ZOOM = MIN_RENDERED_FONT / Math.min(NODE_FONT, EDGE_FONT);
  //: The nearest a label at the focus sits to its edge's far end, and the
  //: step its place is searched in.
  const END_LABEL_MIN = 12;
  //: The clearance kept around a title: an end label is placed along the
  //: curve, which the straight-line search only approximates.
  const LABEL_CLEARANCE = 6;
  //: The widest a focus graph's rings are stretched to fill a wide canvas.
  const MAX_STRETCH = 2;
  //: The base layout's spacing between notes: its rings', and the step an
  //: expansion's notes are placed out from the note expanded at (D16).
  const NODE_SPACING = 140;
  //: Rings out from that note — the first at the base layout's ring
  //: spacing, each next a note's width further — are searched until a place
  //: is clear. For this many rings a place must also leave its edges'
  //: labels room; past them, clear of every note and title is enough.
  const PLACE_RINGS = 12;
  //: The distance along a ring between the places tried.
  const PLACE_STEP = SIZE_MAX / 2;
  //: A title's widest (`text-max-width`) and its gap under the note.
  const TITLE_MAX_WIDTH = 140;
  const TITLE_MARGIN = 4;
  //: Kept clear around each placed note, beyond its title's clearance: an
  //: edge label's height (on its ground), so a label on the edge between
  //: two notes has somewhere to go between them.
  const LABEL_ROOM = EDGE_FONT + 8;
  //: The badge's height: "+N" or the edge-only mark, over the note (D16).
  const BADGE_HEIGHT = 16;

  // A plain `{}` inherits `__proto__` and friends, and a note id is any
  // string; every id-keyed map here is prototype-free.
  function dict() {
    return Object.create(null);
  }

  function edgeWidth(weight) {
    if (typeof weight !== "number") return WIDTH_MIN;
    const clamped = Math.min(Math.max(weight, WEIGHT_FLOOR), 1);
    return WIDTH_MIN + ((WIDTH_MAX - WIDTH_MIN) * (clamped - WEIGHT_FLOOR)) / (1 - WEIGHT_FLOOR);
  }

  function nodeSizes(nodes) {
    const degrees = nodes.map(function (node) { return node.degree || 0; });
    const low = Math.min.apply(null, degrees);
    const high = Math.max.apply(null, degrees);
    const sizes = dict();
    nodes.forEach(function (node) {
      sizes[node.id] = high === low
        ? (SIZE_MIN + SIZE_MAX) / 2
        : SIZE_MIN + ((SIZE_MAX - SIZE_MIN) * ((node.degree || 0) - low)) / (high - low);
    });
    return sizes;
  }

  function colourValue(node, mode) {
    const value = mode === "type" ? node.note_type : node.namespace;
    return typeof value === "string" && value !== "" ? value : null;
  }

  function byName(a, b) {
    return a < b ? -1 : a > b ? 1 : 0;
  }

  // The values present, most nodes first, ties by name, with their counts.
  function rankedValues(nodes, mode) {
    const counts = dict();
    nodes.forEach(function (node) {
      const value = colourValue(node, mode);
      if (value !== null) counts[value] = (counts[value] || 0) + 1;
    });
    const values = Object.keys(counts).sort(function (a, b) {
      return counts[b] - counts[a] || byName(a, b);
    });
    return { values: values, counts: counts };
  }

  // The applied expansions' ids, in URL order (D16).
  function appliedSteps(view) {
    return (view.expansions || [])
      .filter(function (step) { return step.state === "applied"; })
      .map(function (step) { return step.id; });
  }

  // Whether a fetched view is whole enough to commit (D16): every field the
  // canvas, key and toolbar read present, of the kind and in the range the
  // server writes it (`graph_payload`) — counts whole and never negative,
  // states one of their names — its notes and edges each with an id of its
  // own, and every edge's ends among its notes (the server draws a note for
  // every end, ghosts included). Checked before anything on the page changes.
  const NOTE_STATES = ["available", "focus", "expanded", "complete", "over_cap"];
  const STEP_STATES = ["applied", "unreached", "refused"];
  const FACTS_STATES = ["ok", "pending", "unread", "missing"];
  const EDGE_KINDS = ["typed", "wiki_link", "provenance"];
  const STROKES = ["solid", "dotted", "dashed"];
  //: The depths a focus view is drawn at (`MAX_DEPTH`): its own, and each
  //: one's would-be count, which the server writes for every depth.
  const DEPTHS = [1, 2];

  function wholeView(view) {
    const record = function (item) {
      return item !== null && typeof item === "object" && !Array.isArray(item);
    };
    const text = function (value) { return typeof value === "string"; };
    const name = function (value) { return text(value) && value !== ""; };
    const flag = function (value) { return typeof value === "boolean"; };
    const count = function (value) { return Number.isInteger(value) && value >= 0; };
    const one = function (names) { return function (value) { return names.indexOf(value) !== -1; }; };
    const nullOr = function (test) { return function (value) { return value === null || test(value); }; };
    // `item` a record whose every named field passes its test.
    const shaped = function (item, fields) {
      return record(item) && Object.keys(fields).every(function (key) {
        return Object.prototype.hasOwnProperty.call(item, key) && fields[key](item[key]);
      });
    };
    const weight = function (value) { return typeof value === "number" && isFinite(value); };
    const expansion = function (value) {
      return shaped(value, { state: one(NOTE_STATES), would_count: count, cap: count });
    };
    const style = function (value) {
      return shaped(value, { "class": name, stroke: one(STROKES), arrowhead: flag, label: text });
    };
    const step = function (value) {
      return shaped(value, { id: name, state: one(STEP_STATES), added_nodes: count, added_edges: count }) &&
        (value.would_count === undefined || count(value.would_count));
    };
    // The route's own ranges: a threshold clamped to [0, 1], known groups
    // only, each once, and an empty list read as all of them.
    const groups = function (value) {
      return Array.isArray(value) && value.length > 0 && value.every(function (group, index) {
        return PROVENANCE_GROUPS.indexOf(group) !== -1 && value.indexOf(group) === index;
      });
    };
    const shown = shaped(view, {
      mode: one(["focus"]),
      focus: name,
      depth: one(DEPTHS),
      nodes: function (v) { return Array.isArray(v) && v.length > 0; },
      edges: Array.isArray,
      expansions: Array.isArray,
      legend: Array.isArray,
      hidden: function (v) { return shaped(v, { by_weight: count, by_provenance: count, total: count }); },
      would_be_nodes: function (v) {
        return record(v) && Object.keys(v).length === DEPTHS.length &&
          DEPTHS.every(function (level) { return count(v[String(level)]); });
      },
      filters: function (v) {
        return shaped(v, {
          min_weight: function (w) { return weight(w) && w >= 0 && w <= 1; },
          provenance: groups
        });
      }
    });
    if (!shown) return false;
    const notes = dict();
    const nodesWhole = view.nodes.every(function (node) {
      if (!record(node) || !name(node.id) || node.id in notes) return false;
      notes[node.id] = true;
      return shaped(node, {
        label: text, facts_state: one(FACTS_STATES), ghost: flag, focus: flag, layer_only: flag,
        hop: count, degree: count, note_type: text, status: text, namespace: text,
        expanded: flag, via: nullOr(name), undrawn_nodes: count, undrawn_edges: count,
        expansion: nullOr(expansion)
      });
    });
    if (!nodesWhole) return false;
    const edges = dict();
    const edgesWhole = view.edges.every(function (edge) {
      if (!record(edge) || !name(edge.id) || edge.id in edges) return false;
      edges[edge.id] = true;
      return shaped(edge, {
        from: name, to: name, kind: one(EDGE_KINDS), weight: nullOr(weight), partial: flag, style: style
      }) && edge.from in notes && edge.to in notes;
    });
    return edgesWhole && view.expansions.every(step) &&
      view.legend.every(function (line) { return shaped(line, { type: text, "class": name }); });
  }

  // The values `slotOf` has not seen, in the order they take slots (D16):
  // the base notes' (`via` none) by node count, ties by name (S4 D7); then
  // each applied expansion's, in URL order, ties by name; then any other's.
  // So the same graph colours the same way on every load.
  function unslottedValues(view, mode, slotOf) {
    const steps = appliedSteps(view);
    const groupOf = dict();
    const base = [];
    view.nodes.forEach(function (node) {
      const value = colourValue(node, mode);
      if (value === null || value in slotOf) return;
      const step = node.via ? steps.indexOf(node.via) : -1;
      const group = node.via ? (step === -1 ? steps.length + 1 : step + 1) : 0;
      if (group === 0) base.push(node);
      if (!(value in groupOf) || group < groupOf[value]) groupOf[value] = group;
    });
    const counts = rankedValues(base, mode).counts;
    return Object.keys(groupOf).sort(function (a, b) {
      return groupOf[a] - groupOf[b] ||
        (groupOf[a] === 0 ? (counts[b] || 0) - (counts[a] || 0) : 0) ||
        byName(a, b);
    });
  }

  // A badge on a note (D16): dark text on a light halo, as an image, so
  // the note's own colour shows around it.
  function badgeImage(text) {
    const width = 8 + 8 * text.length;
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + width +
      '" height="' + BADGE_HEIGHT + '" viewBox="0 0 ' + width + " " + BADGE_HEIGHT + '">' +
      '<text x="' + width / 2 + '" y="12" text-anchor="middle" font-family="sans-serif" ' +
      'font-size="12" font-weight="700" fill="' + INK + '" stroke="' + PANEL + '" ' +
      'stroke-width="3" paint-order="stroke">' + text + "</text></svg>";
    return { image: "data:image/svg+xml;utf8," + encodeURIComponent(svg), width: width };
  }

  // What showing a note's neighbours would add, on the note (D16): "+N" for
  // N new visible notes, "↔" when it adds only edges between notes drawn.
  function addsOf(node) {
    if (!node.expansion) return null;
    if (node.undrawn_nodes > 0) {
      return Object.assign({ kind: "adds-notes" }, badgeImage("+" + node.undrawn_nodes));
    }
    if (node.undrawn_edges > 0) return Object.assign({ kind: "adds-edges" }, badgeImage("↔"));
    return null;
  }

  function strokeOfClass(cssClass) {
    if (cssClass === "kedge-derived-from" || cssClass === "kedge-provenance") return "dotted";
    if (cssClass === "kedge-contradicts") return "dashed";
    return "solid";
  }

  // A threshold as the server's text words it (Python's `str(float)`), so
  // the toolbar's line and the text's "Not shown" line read the same.
  function weightText(weight) {
    return Number.isInteger(weight) ? weight.toFixed(1) : String(weight);
  }

  function snapToSlider(value) {
    return Math.round(Number(value) / SLIDER_STEP) / Math.round(1 / SLIDER_STEP);
  }

  function plural(count, one, many) {
    return count + " " + (count === 1 ? one : many);
  }

  // ── The page's query: one key changed, the rest kept ───────────────────

  function queryWith(changes) {
    const params = new URLSearchParams(window.location.search);
    Object.keys(changes).forEach(function (key) {
      const value = changes[key];
      if (value === null || value === undefined || value === "") params.delete(key);
      else params.set(key, value);
    });
    // `canvas` as the server writes it (D17): one `canvas=full`, last, for
    // the presentation applied, and none inline — whatever the address
    // spelled — so the history's string compares see one address as one.
    params.delete(CANVAS_KEY);
    if (fullPage) params.append(CANVAS_KEY, "full");
    return params.toString();
  }

  // One key of the address as the server's parser reads it: the LAST value
  // of a repeated key (a query mapping keeps the last), and a wholly blank
  // value is absent; any other value exactly as sent.
  function queryValue(params, key) {
    const values = params.getAll(key);
    const value = values.length ? values[values.length - 1] : "";
    return blank(value) ? "" : value;
  }

  //: The keys in the order the server's URL builder writes them
  //: (`_URL_KEYS`); `canvas`, last, is `queryWith`'s.
  const URL_KEYS = [
    "focus", "depth", "expand", "type", "namespace", "min_weight",
    "provenance", "colour", "selected", "edge", "pin"
  ];

  // A value as the server's URL builder writes it — Python's `urlencode`,
  // that is `quote_plus` with nothing kept safe: letters, digits and `_.-~`
  // as they are, a space as `+`, anything else `%XX` of its UTF-8 (so `*`
  // is `%2A`, where `URLSearchParams` would keep it and write `~` `%7E`).
  function quotePlus(value) {
    return encodeURIComponent(value)
      .replace(/[!'()*]/g, function (c) { return "%" + c.charCodeAt(0).toString(16).toUpperCase(); })
      .replace(/%20/g, "+");
  }

  // One pair's value as the parser reads it: `+` a space, percent-decoded.
  function pairValue(pair) {
    const at = pair.indexOf("=");
    const raw = (at === -1 ? "" : pair.slice(at + 1)).replace(/\+/g, " ");
    try {
      return decodeURIComponent(raw);
    } catch (error) {
      return raw;
    }
  }

  // Show its neighbours for `pid` (D16), as `knowledge_graph_expand_url`
  // writes it for the view on screen: every earlier request for the note
  // dropped and the note appended last, the note selected, `edge` cleared
  // and the edge it was drawn for kept as `pin`; every other pair exactly
  // as the server wrote it, in its key order (`expand` in request order),
  // the pairs added encoded as it encodes them, and `canvas` last as
  // `queryWith` spells it — one address, one spelling. The view's pairs are
  // the server's own (`data-kgraph-address`, the address it rendered, each
  // swap's), not the address bar's, which may spell the same view otherwise
  // (`min_weight=.70`, `colour=namespace`); the selection is the address
  // bar's, which panel clicks move.
  function expandQuery(pid, drawnEdge) {
    const now = new URLSearchParams(window.location.search);
    const page = document.querySelector("[data-knowledge-graph]");
    const address = page && typeof page.getAttribute === "function"
      ? page.getAttribute("data-kgraph-address")
      : null;
    const search = address
      ? (withoutFragment(address).split("?")[1] || "")
      : window.location.search.replace(/^\?/, "");
    const raw = search.split("&").filter(Boolean);
    const focus = raw.filter(function (pair) { return pairKey(pair) === "focus"; })
      .map(pairValue).pop() || "";
    // The edge the view was drawn for — an `edge=` is its own pin — kept
    // only when the view draws it, as the panel route's `drawn_selection`
    // keeps it: a shared address naming an edge since gone pins nothing,
    // whether or not the first tap's panel has landed yet.
    const chosen = queryValue(now, "edge") || queryValue(now, "pin");
    const pin = chosen && drawnEdge(chosen) ? chosen : "";
    const requested = dict();
    const pairs = [];
    raw.forEach(function (pair) {
      const key = pairKey(pair);
      if (key === CANVAS_KEY || key === "selected" || key === "edge" || key === "pin") return;
      if (key === "expand") {
        // As the parser reads them: first occurrences, blanks and the focus
        // dropped.
        const value = pairValue(pair);
        if (blank(value) || value === focus || value === pid || requested[value]) return;
        requested[value] = true;
      }
      pairs.push([key, pair]);
    });
    pairs.push(["expand", "expand=" + quotePlus(pid)], ["selected", "selected=" + quotePlus(pid)]);
    if (pin) pairs.push(["pin", "pin=" + quotePlus(pin)]);
    const rank = function (pair) {
      const at = URL_KEYS.indexOf(pair[0]);
      return at === -1 ? URL_KEYS.length : at;
    };
    // A stable sort: repeated keys keep their order.
    const ordered = pairs.map(function (pair, index) { return [pair, index]; })
      .sort(function (a, b) { return rank(a[0]) - rank(b[0]) || a[1] - b[1]; })
      .map(function (entry) { return entry[0][1]; });
    if (fullPage) ordered.push(CANVAS_KEY + "=full");
    return ordered.join("&");
  }

  //: Python's whitespace (`str.isspace`, what the parser's `.strip()`
  //: removes) — not JavaScript's `trim`, which differs: it leaves U+001C–
  //: U+001F and U+0085 but strips U+FEFF.
  const PYTHON_BLANK = /^[\t\n\v\f\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/;

  function blank(value) {
    return PYTHON_BLANK.test(value);
  }

  function pageUrl(query) {
    return window.location.pathname + (query ? "?" + query : "");
  }

  function navigate(changes) {
    window.location.assign(pageUrl(queryWith(changes)));
  }

  // The provenance key as the parser reads it: one comma list in group order,
  // left out when every group is on (S4 D4).
  function provenanceValue(groups) {
    const on = PROVENANCE_GROUPS.filter(function (group) { return groups.indexOf(group) !== -1; });
    return on.length === PROVENANCE_GROUPS.length ? null : on.join(",");
  }

  function readPayload() {
    const source = document.querySelector("[data-knowledge-graph-payload]");
    if (!source) return null;
    try {
      return JSON.parse(source.textContent);
    } catch (error) {
      return null;
    }
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function swatch(colour, stroke) {
    const node = element("span", "kgraph-swatch" + (stroke ? " kgraph-swatch-line" : ""));
    if (stroke) {
      node.style.borderTopColor = colour;
      node.style.borderTopStyle = stroke;
    } else {
      node.style.backgroundColor = colour;
    }
    return node;
  }

  function keyItem(swatchNode, text, data) {
    const item = element("li", "kgraph-key-item");
    if (swatchNode) item.appendChild(swatchNode);
    item.appendChild(element("span", "", text));
    Object.keys(data || {}).forEach(function (key) { item.dataset[key] = data[key]; });
    return item;
  }

  // ── The panel's history ─────────────────────────────────────────────
  //
  // ONE policy for every panel request on this page, the text's links and
  // the canvas's clicks alike, because two would disagree (htmx keeps its
  // own idea of the current URL and snapshots the page under it).
  //
  // - htmx pushes nothing here: a panel link's `hx-push-url` is read off it
  //   and set to "false" as its request leaves (htmx reads the attribute when
  //   the response lands), so htmx neither pushes before the swap nor
  //   snapshots the page into its history cache.
  // - This file pushes the request's page URL once THAT request's fragment
  //   is swapped into the live host. A request a later click aborted (the
  //   host's `hx-sync` replace), one the server answered with `HX-Redirect`,
  //   or one still in flight when the page is left, pushes nothing.
  // - Re-selecting what is already shown pushes nothing: no two entries of
  //   this page carry one URL.
  // - Once that swap lands the canvas lights the URL's selection, so the
  //   picture follows a text link or a panel's link as it follows a click.
  // - Any Back or Forward first ABANDONS the panel request in flight,
  //   whatever entry it lands on: the request itself is aborted, and should
  //   its response land anyway htmx is stopped before it reads it
  //   (`htmx:beforeOnLoad`, ahead of `HX-Redirect` and the swap), so it can
  //   neither redirect, replace
  //   the panel nor push. Then, onto an entry whose address (the fragment
  //   aside) differs from the one shown, it reloads: the server renders that
  //   URL's panel, text and picture together. Onto one with the same address
  //   (a `#` move) the panel on screen already is that entry's.

  //: The address the picture on screen was drawn or last pushed for.
  let shownHref = "";
  //: The live panel host and the canvas's htmx source element.
  let panelHost = null;
  let canvasSource = null;
  //: The page URL the canvas click being issued pushes.
  let canvasPage = "";
  //: The panel request whose swap pushes: `{ page, xhr, elt }`.
  let pendingPush = null;
  //: Set once Back or Forward has started a reload: nothing pushes after.
  let retired = false;
  //: The requests a Back or Forward abandoned: their swaps are cancelled.
  const abandoned = [];
  //: Re-lights the canvas from the address (set by `draw`).
  let lightFromUrl = function () {};
  //: The canvas's side of a panel opening or closing (set by `draw`): the
  //: usable area re-measured, the selection kept in it.
  let panelMoved = function () {};
  //: The canvas's side of anything else over it changing size — the pill
  //: shown, a disclosure, the window (set by `draw`).
  let overlaysMoved = function () {};
  //: The control that opened the panel on screen: where Esc or Close
  //: returns the focus. None for a canvas click or a panel the page loaded.
  let panelInvoker = null;
  //: The expansion or collapse in flight (D16) — `{ href, controller }` —
  //: one at a time; none once it commits, fails or is retired.
  let graphRequest = null;
  //: The address a `#` link followed while that request is out — Text
  //: view — is about to announce with a `popstate`: a jump within the page,
  //: not a Back or Forward, so it does not retire the request.
  let jumpingTo = null;
  //: Starts one (set by `draw`): `href` a page address, `anchor` the note
  //: an expansion expands. Answers whether it took the click.
  let changeGraph = function () { return false; };
  //: The controls follow the request's start and end (set by `draw`).
  let busyChanged = function () {};

  function withoutFragment(href) {
    const hash = href.indexOf("#");
    return hash === -1 ? href : href.slice(0, hash);
  }

  function pushUrlOf(elt) {
    if (elt === canvasSource) return canvasPage;
    if (!elt || !elt.getAttribute) return "";
    const own = elt.getAttribute("hx-push-url");
    if (own && own !== "false") {
      elt.setAttribute("data-kgraph-push-url", own);
      elt.setAttribute("hx-push-url", "false");
    }
    return elt.getAttribute("data-kgraph-push-url") || "";
  }

  function trackPanel(event) {
    const detail = event.detail || {};
    if (!panelHost || detail.target !== panelHost) return;
    // While an expansion or collapse is out no panel request leaves: it
    // would answer from the view being replaced (D16). Cancelled, it is
    // not one whose swap pushes.
    if (graphRequest) {
      event.preventDefault();
      return;
    }
    if (retired) return;
    const page = pushUrlOf(detail.elt);
    // What opened it, for the focus to return to on close: a text or panel
    // link; none for a canvas click.
    const invoker = detail.elt === canvasSource ? null : detail.elt;
    pendingPush = page ? { page: page, xhr: detail.xhr, elt: detail.elt, invoker: invoker } : null;
  }

  function pushAfterSwap(event) {
    const detail = event.detail || {};
    if (retired || !pendingPush || detail.xhr !== pendingPush.xhr) return;
    if (detail.target !== panelHost || panelHost.isConnected === false) return;
    const page = pendingPush.page;
    const invoker = pendingPush.invoker;
    pendingPush = null;
    const target = new URL(page, window.location.href).href;
    if (withoutFragment(target) !== withoutFragment(window.location.href)) {
      window.history.pushState({ kgraph: true }, "", page);
    }
    syncRefresh();
    shownHref = window.location.href;
    lightFromUrl();
    panelShown(invoker);
  }

  function dropAbandoned(event) {
    const detail = event.detail || {};
    if (detail.xhr && abandoned.indexOf(detail.xhr) !== -1) event.preventDefault();
  }

  // The panel request in flight, retired: by a Back or Forward, a full-page
  // toggle or a close. Should its response land anyway, `dropAbandoned`
  // stops htmx before it reads it.
  function abandonPending() {
    if (!pendingPush) return;
    const xhr = pendingPush.xhr;
    abandoned.push(xhr);
    // The request itself: `hx-sync` keeps it on the host, not on the
    // element that issued it, so an `htmx:abort` event would miss it.
    if (xhr && typeof xhr.abort === "function") xhr.abort();
    pendingPush = null;
  }

  // The expansion or collapse in flight, retired (D16): by a Back or
  // Forward or a toggle. Its fetch is aborted, and should its answer come
  // anyway it is no longer `graphRequest`: it neither commits, pushes nor
  // touches the controls, which are given back now.
  function retireGraphRequest() {
    const request = graphRequest;
    if (!request) return;
    graphRequest = null;
    if (request.controller) request.controller.abort();
    busyChanged();
  }

  function onTravel() {
    const jumped = jumpingTo !== null && window.location.href === jumpingTo;
    jumpingTo = null;
    syncRefresh();
    if (!panelHost || jumped) return;
    retireGraphRequest();
    abandonPending();
    if (withoutFragment(window.location.href) === withoutFragment(shownHref)) {
      // The entry's panel is the one on screen; the canvas lights it again.
      lightFromUrl();
      return;
    }
    retired = true;
    window.location.reload();
  }

  // ── The panel's close, its focus and Esc (D17) ───────────────────────
  //
  // An explicit open — a text or panel link, a canvas click — puts the
  // keyboard focus on the panel's heading once its fragment is in. Esc
  // (anywhere but a field Esc already clears) or the panel's Close empties
  // the host, pushes the panel's own close link — this view with no
  // selection, keeping as `pin=` the edge whose exemption the drawing needs,
  // which the server wrote because the payload does not know it — and gives
  // the focus back to what opened the panel, or to the toggle, a canvas
  // control, when that was replaced or there was none. Esc never leaves
  // full page.

  function hasPanel() {
    if (!panelHost) return false;
    if (panelHost.childElementCount > 0) return true;
    return String(panelHost.innerHTML || "").trim() !== "";
  }

  function within(root, selector) {
    return root && typeof root.querySelector === "function" ? root.querySelector(selector) : null;
  }

  function panelShown(invoker) {
    panelInvoker = invoker || null;
    if (panelHost && "scrollTop" in panelHost) panelHost.scrollTop = 0;
    panelMoved(true);
    const heading = within(panelHost, "[data-kgraph-panel-heading]") ||
      within(panelHost, "[data-kgraph-panel-close]");
    // Not scrolled to for a canvas click: the panel is beside the picture.
    if (heading && typeof heading.focus === "function") {
      heading.focus({ preventScroll: !invoker });
    }
  }

  function closePanel() {
    if (retired || !hasPanel()) return false;
    const link = within(panelHost, "[data-kgraph-panel-close]");
    const target = link
      ? link.getAttribute("href")
      : pageUrl(queryWith({ selected: null, edge: null }));
    abandonPending();
    panelHost.innerHTML = "";
    if (withoutFragment(new URL(target, window.location.href).href) !==
        withoutFragment(window.location.href)) {
      window.history.pushState({ kgraph: true }, "", target);
    }
    syncRefresh();
    shownHref = window.location.href;
    panelMoved(false);
    const invoker = panelInvoker;
    panelInvoker = null;
    const back = invoker && invoker.isConnected !== false && typeof invoker.focus === "function"
      ? invoker
      : document.querySelector("[data-kgraph-full-toggle]");
    if (back && typeof back.focus === "function") back.focus();
    return true;
  }

  function onKeydown(event) {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    // A panel action: inert while an expansion or collapse is out (D16).
    if (graphRequest) return;
    const target = event.target || {};
    if (target.tagName === "SELECT") return;
    if (target.tagName === "INPUT" && target.type === "search" && target.value) return;
    if (closePanel()) event.preventDefault();
  }

  function onCloseClick(event) {
    const target = event.target;
    const link = target && typeof target.closest === "function"
      ? target.closest("[data-kgraph-panel-close]")
      : null;
    if (!link || !panelHost || typeof panelHost.contains !== "function" ||
        !panelHost.contains(link)) return;
    // A middle, modified or context click opens the link as a link.
    if (modified(event)) return;
    if (graphRequest) {
      // Inert while an expansion or collapse is out (D16).
      event.preventDefault();
      return;
    }
    if (closePanel()) event.preventDefault();
  }

  function modified(event) {
    return event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
  }

  // ── Show its neighbours, Collapse and remove: in place (D16) ─────────
  //
  // The panel's two links and the scope line's and "Not shown"'s remove
  // links are plain links the server wrote; on a drawn page a plain click
  // on one fetches its address and swaps the view in (`changeGraph`), and
  // while a request is out it does nothing at all. A middle, modified or
  // context click opens the link as a link.

  const GRAPH_LINKS = "a[data-kgraph-expand], a[data-kgraph-collapse], a[data-kgraph-expansion-remove]";

  // Any other link to this page's graph — Centre on this, a remedy or depth
  // link, the pill, a reload — loads a view built from the one being
  // replaced: inert too while a request is out. Links to note pages, and
  // a `#` jump such as Text view, stay live.
  function graphAddress(link) {
    const href = link.getAttribute("href") || "";
    // A `#` jump stays on this page; anything else is resolved as the
    // browser would follow it — the pill's href is the full address.
    if (href === "" || href.charAt(0) === "#") return false;
    let target = null;
    try {
      target = new URL(href, window.location.href);
    } catch (error) {
      return false;
    }
    return target.origin === new URL(window.location.href).origin && target.pathname === GRAPH_PATH;
  }

  // The address a `#` link leads to, on this page; none for any other link.
  function fragmentJump(link) {
    const href = link.getAttribute("href") || "";
    if (href.charAt(0) !== "#") return null;
    try {
      return new URL(href, window.location.href).href;
    } catch (error) {
      return null;
    }
  }

  function onGraphLinkClick(event) {
    const target = event.target;
    if (!target || typeof target.closest !== "function" || modified(event)) return;
    const link = target.closest(GRAPH_LINKS);
    if (graphRequest) {
      const any = link || target.closest("a[href]");
      if (any && graphAddress(any)) event.preventDefault();
      else if (any && !event.defaultPrevented) jumpingTo = fragmentJump(any);
      return;
    }
    if (!link || event.defaultPrevented) return;
    if (changeGraph(link.getAttribute("href"), link, null)) event.preventDefault();
  }

  // ── Full page (D17): presentation, carried by every graph link ──────
  //
  // The server writes `canvas=full` into every link it renders, and marks
  // the page; this file applies it. A toggle switches in place, in order:
  // the panel request in flight is retired (as a Back would), the class
  // goes on and the address is pushed, every server-rendered graph link,
  // panel request and push URL on the page is rewritten — and htmx told
  // of each one whose `hx-*` changed — then the pill and `shownHref`
  // follow, and the canvas is resized and refitted with every node where
  // it was. A Back or Forward over a toggle reloads that address, as any
  // other history move does.

  let fullPage = false;
  //: Whether the heading and the scope line are below the stage.
  let textMoved = false;

  // One key of a query string: `+` a space, percent-decoded.
  function pairKey(pair) {
    const raw = pair.split("=")[0].replace(/\+/g, " ");
    try {
      return decodeURIComponent(raw);
    } catch (error) {
      return raw;
    }
  }

  // `href` with its `canvas` rewritten and every other pair exactly as it
  // was spelled: none for inline; `canvas=full` last for full page — before
  // a panel request's `render`, as the server writes them.
  function withCanvas(href, full) {
    const hash = href.indexOf("#");
    const fragment = hash === -1 ? "" : href.slice(hash);
    const base = hash === -1 ? href : href.slice(0, hash);
    const question = base.indexOf("?");
    const path = question === -1 ? base : base.slice(0, question);
    const pairs = question === -1 ? [] : base.slice(question + 1).split("&").filter(Boolean);
    const kept = pairs.filter(function (pair) { return pairKey(pair) !== CANVAS_KEY; });
    if (full) {
      let at = kept.length;
      kept.forEach(function (pair, index) {
        if (at === kept.length && pairKey(pair) === RENDER_KEY) at = index;
      });
      kept.splice(at, 0, CANVAS_KEY + "=full");
    }
    return path + (kept.length ? "?" + kept.join("&") : "") + fragment;
  }

  //: The text a drawn view renders between the stage and its payload, in
  //: order, each there or not as the view has it: what a swap replaces.
  const TEXT_SECTIONS = [
    "[data-kgraph-legend]",
    "[data-kgraph-focus]",
    "[data-kgraph-edges]",
    "[data-kgraph-layers]",
    "[data-kgraph-missing]",
    "[data-kgraph-hidden]"
  ];

  //: The attributes a graph link keeps its addresses in, and the path each
  //: names: the page, or its panel fragment.
  const LINK_ATTRIBUTES = [
    ["href", GRAPH_PATH, false],
    ["hx-push-url", GRAPH_PATH, true],
    ["data-kgraph-push-url", GRAPH_PATH, false],
    ["hx-get", PANEL_PATH, true]
  ];

  function rewriteLinks(full) {
    const page = document.querySelector("[data-knowledge-graph]");
    if (!page || typeof page.querySelectorAll !== "function") return;
    const changed = [];
    const links = page.querySelectorAll("[href], [hx-get], [hx-push-url], [data-kgraph-push-url]");
    Array.prototype.forEach.call(links, function (elt) {
      let reprocess = false;
      LINK_ATTRIBUTES.forEach(function (spec) {
        const value = elt.getAttribute(spec[0]);
        // Only what the server wrote: a root-relative address on that path.
        if (!value || value.split(/[?#]/)[0] !== spec[1]) return;
        const next = withCanvas(value, full);
        if (next === value) return;
        elt.setAttribute(spec[0], next);
        if (spec[2]) reprocess = true;
      });
      if (reprocess) changed.push(elt);
    });
    // htmx read each link's `hx-*` when it processed it: it re-reads a
    // changed one only when asked.
    if (window.htmx && typeof window.htmx.process === "function") {
      changed.forEach(function (elt) { window.htmx.process(elt); });
    }
  }

  function measureHeader() {
    const header = document.querySelector("header.shell-header");
    const root = document.documentElement;
    if (!header || !root || !root.style || typeof header.getBoundingClientRect !== "function") return;
    root.style.setProperty("--kgraph-header-height", header.getBoundingClientRect().height + "px");
  }

  // The page's frame for one mode: the class the stylesheet keys every
  // full-page rule to; the heading and the scope line above the stage
  // inline, the start of the text below it in full page — moved, not
  // reordered by CSS, so focus and reading order follow what is shown; the
  // toolbar's toggle and "Text view".
  function applyFrame(full) {
    fullPage = full;
    const body = document.body;
    if (body && body.classList) body.classList.toggle("kgraph-full", full);
    const page = document.querySelector("[data-knowledge-graph]");
    const stage = document.querySelector("[data-kgraph-stage]");
    if (full !== textMoved && page && stage && stage.parentNode === page &&
        typeof page.querySelector === "function") {
      const text = [page.querySelector(":scope > header"), page.querySelector("[data-kgraph-scope]")]
        .filter(Boolean);
      if (full) stage.after.apply(stage, text);
      else stage.before.apply(stage, text);
      textMoved = full;
    }
    const toggle = document.querySelector("[data-kgraph-full-toggle]");
    if (toggle) {
      toggle.textContent = full ? "Exit full page" : "Full page";
      toggle.setAttribute("aria-pressed", full ? "true" : "false");
    }
    const textLink = document.querySelector("[data-kgraph-text-link]");
    if (textLink) textLink.hidden = !full;
    if (full) measureHeader();
  }

  function reducedMotion() {
    return typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  // ── "Graph changed — refresh" (D14): never an auto re-layout ────────────
  //
  // One `/knowledge/events` subscription per page load. A frame naming a
  // node or edge this page drew — any frame, in a scoped-global view, where
  // a new edge can bring in a note the page never drew — reveals the pill,
  // and so do a `lens.refresh` and a stream that reopens after an error:
  // either way frames may have been missed. "Drawn" is the payload on the
  // page, so it follows each swap (D16); while an expansion or collapse is
  // out, ANY knowledge frame reveals it, since the view coming back may
  // draw what it names, and the answer may predate it. Once shown the pill
  // stays until the page is reloaded: a swap never hides it. The pill is an ordinary link to
  // the page's current address (`syncRefresh`, below); the server
  // re-renders from facts the event marked stale, so nothing is redrawn
  // here.

  const EVENTS_PATH = "/knowledge/events";
  const KNOWLEDGE_EVENTS = [
    "note.created",
    "note.updated",
    "note.deleted",
    "note.renamed",
    "edge.upserted"
  ];

  function namesDrawn(type, data) {
    const payload = readPayload();
    if (!payload || !Array.isArray(payload.nodes)) return false;
    if (payload.mode === "global") return true;
    const nodes = dict();
    payload.nodes.forEach(function (node) { nodes[node.id] = true; });
    const drawn = function (id) { return typeof id === "string" && nodes[id] === true; };
    if (type !== "edge.upserted") return drawn(data.id);
    const edges = dict();
    (payload.edges || []).forEach(function (edge) { edges[edge.id] = true; });
    const edgeId = data.edge_id;
    return (typeof edgeId === "string" && edges[edgeId] === true) ||
      drawn(data.from_id) || drawn(data.to_id);
  }

  // The anchor's own href, kept on the page's address at every history move
  // (a panel's push, Back, Forward, a fragment), so every way of following
  // it — a click, a middle click, the context menu — lands there. Without
  // the fragment: a link that differs from the page only by one scrolls.
  function syncRefresh() {
    const pill = document.querySelector("[data-kgraph-refresh-pill]");
    if (pill) pill.setAttribute("href", withoutFragment(window.location.href));
    return pill;
  }

  function showRefresh() {
    const pill = syncRefresh();
    if (pill && pill.hidden) {
      pill.hidden = false;
      // It can make the toolbar taller: the drawer and the area below move.
      overlaysMoved(true);
    }
  }

  function onKnowledgeFrame(event) {
    let message = null;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      if (graphRequest) showRefresh();
      return;
    }
    const data = message && message.payload;
    if (graphRequest) showRefresh();
    else if (data && typeof data === "object" && namesDrawn(event.type, data)) showRefresh();
  }

  function listen() {
    if (typeof window.EventSource !== "function") return;
    const source = new window.EventSource(EVENTS_PATH);
    let lost = false;
    KNOWLEDGE_EVENTS.forEach(function (type) {
      source.addEventListener(type, onKnowledgeFrame);
    });
    source.addEventListener("lens.refresh", showRefresh);
    source.addEventListener("error", function () { lost = true; });
    source.addEventListener("open", function () {
      if (lost) showRefresh();
      lost = false;
    });
  }

  // ── Labels at the focus ─────────────────────────────────────────────
  //
  // A titled node's title hangs under it, so a label at an edge's middle can
  // land on the focus's title, and one by the far end on that end's title.
  // Each label on an edge at the focus goes to the first place along it,
  // from its far end, whose box is clear of both titles; with none, it stays
  // at the middle.

  function labelBox(element) {
    return element.boundingBox({
      includeNodes: false,
      includeEdges: false,
      includeLabels: true,
      includeOverlays: false
    });
  }

  function apart(a, b) {
    return a.x2 <= b.x1 || b.x2 <= a.x1 || a.y2 <= b.y1 || b.y2 <= a.y1;
  }

  function placeEndLabels(cy) {
    // Places are measured on the rendered curve: none without a renderer.
    if (cy.headless()) return;
    // The labels placed so far, with their clearance: parallel edges with a
    // raw type or a resolution each would otherwise pick one spot and hide
    // each other's words under their grounds.
    const placed = [];
    const keep = function (box) {
      placed.push({
        x1: box.x1 - LABEL_CLEARANCE,
        x2: box.x2 + LABEL_CLEARANCE,
        y1: box.y1 - LABEL_CLEARANCE,
        y2: box.y2 + LABEL_CLEARANCE
      });
    };
    cy.edges().forEach(function (edge) {
      const end = edge.data("labelEnd");
      if (!end) return;
      const own = labelBox(edge);
      const halfW = (own.x2 - own.x1) / 2;
      const halfH = (own.y2 - own.y1) / 2;
      // Every note's body and title: a label clear of its own two ends but
      // under a third note's circle is no more readable.
      const titles = cy.nodes().map(function (node) {
        const box = node.boundingBox({ includeLabels: true, includeOverlays: false });
        return {
          x1: box.x1 - LABEL_CLEARANCE,
          x2: box.x2 + LABEL_CLEARANCE,
          y1: box.y1 - LABEL_CLEARANCE,
          y2: box.y2 + LABEL_CLEARANCE
        };
      });
      // Along the curve as drawn — a parallel edge bows away from the
      // straight line — measured by arc length from the far end, which is
      // how the end label's offset is laid off.
      const from = edge.sourceEndpoint();
      const to = edge.targetEndpoint();
      const controls = edge.controlPoints ? edge.controlPoints() : null;
      const bend = controls && controls.length === 1 ? controls[0] : null;
      const at = function (t) {
        if (!bend) return { x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t };
        const u = 1 - t;
        return {
          x: u * u * from.x + 2 * u * t * bend.x + t * t * to.x,
          y: u * u * from.y + 2 * u * t * bend.y + t * t * to.y
        };
      };
      // Measured from the far end (a label at the focus) or the source.
      const fromEnd = end === "target" ? "target" : "source";
      const candidates = [];
      let travelled = 0;
      let previous = null;
      for (let i = 0; i <= 64; i += 1) {
        const t = i / 64;
        const point = at(fromEnd === "source" ? t : 1 - t);
        if (previous) travelled += Math.hypot(point.x - previous.x, point.y - previous.y);
        candidates.push({ point: point, travelled: travelled });
        previous = point;
      }
      const length = travelled;
      // Nearest the far end first; for a "middle" label, nearest the middle.
      if (end === "middle") {
        candidates.sort(function (a, b) {
          return Math.abs(a.travelled - length / 2) - Math.abs(b.travelled - length / 2);
        });
      }
      for (let index = 0; index < candidates.length; index += 1) {
        const point = candidates[index].point;
        const offset = candidates[index].travelled;
        if (offset < END_LABEL_MIN || offset > length - END_LABEL_MIN) continue;
        const box = {
          x1: point.x - halfW,
          x2: point.x + halfW,
          y1: point.y - halfH,
          y2: point.y + halfH
        };
        const clear = function (other) { return apart(box, other); };
        if (titles.every(clear) && placed.every(clear)) {
          edge.data("labelOffset", offset);
          edge.addClass(fromEnd === "source" ? "label-at-source" : "label-at-target");
          keep(box);
          return;
        }
      }
      // Nowhere clear: it stays at the middle, which later labels avoid.
      keep(own);
    });
  }

  // ── Drawing ─────────────────────────────────────────────────────────────

  function draw() {
    const container = document.querySelector("[data-kgraph-canvas]");
    // Drawn already — by this load, or by a second run of this file over the
    // same element.
    if (!container || container.kgraphDrawn || !window.cytoscape) return;
    const payload = readPayload();
    if (!payload || !Array.isArray(payload.nodes) || !payload.nodes.length) return;
    container.kgraphDrawn = true;
    const previous = window.LithosLensKnowledgeGraph;
    if (previous && previous.cy) previous.cy.destroy();

    const mode = payload.colour === "type" ? "type" : "namespace";
    //: The view on screen: this load's payload, then each swap's (D16).
    let current = payload;

    // ── Elements, by persistent identity (D16) ──────────────────────────
    // Cytoscape's own element ids are opaque: a note id is any string, and
    // the bundle misbehaves on ids such as `__proto__`. The payload's id
    // travels as `pid`; each note's and edge's element id is minted once
    // and kept for the page's lifetime, so a swap diffs one payload against
    // the next by identity, never by array position.
    const nodeIds = dict();
    const edgeIds = dict();
    let minted = 0;
    function elementId(ids, prefix, pid) {
      if (!ids[pid]) {
        minted += 1;
        ids[pid] = prefix + minted;
      }
      return ids[pid];
    }

    // ── Colour slots, kept for the page's lifetime (D16) ────────────────
    // The values present take slots in `unslottedValues`' order; a value
    // keeps its slot through a collapse and every later swap, and a value
    // that came once the slots were all taken keeps the neutral. A note
    // whose refreshed value changed takes its new value's colour.
    const slotOf = dict();
    let slotsTaken = 0;
    function takeSlots(view) {
      unslottedValues(view, mode, slotOf).forEach(function (value) {
        slotOf[value] = slotsTaken < NODE_PALETTE.length ? NODE_PALETTE[slotsTaken] : null;
        slotsTaken += 1;
      });
    }
    function colourOf(value) {
      return (value !== null && slotOf[value]) || NODE_NEUTRAL;
    }

    function nodeDefinition(node, sizes) {
      const classes = [];
      if (node.focus) classes.push("focus");
      if (node.ghost) classes.push("ghost");
      if (node.facts_state === "pending") classes.push("pending");
      if (node.status === "archived") classes.push("archived");
      if (node.status === "quarantined") classes.push("quarantined");
      const adds = addsOf(node);
      if (adds) classes.push(adds.kind);
      return {
        group: "nodes",
        data: {
          id: elementId(nodeIds, "n", node.id),
          pid: node.id,
          label: node.label,
          size: sizes[node.id],
          colour: colourOf(colourValue(node, mode)),
          hop: node.hop || 0,
          via: node.via || "",
          badge: adds ? adds.image : "",
          badgeWidth: adds ? adds.width : 0
        },
        classes: classes.join(" ")
      };
    }

    function edgeDefinition(edge, view, drawn) {
      if (!drawn[edge.from] || !drawn[edge.to]) return null;
      const style = edge.style || {};
      const classes = String(style["class"] || "kedge-unknown").split(/\s+/).filter(Boolean);
      classes.push("stroke-" + (style.stroke || "solid"));
      if (style.arrowhead) classes.push("arrow");
      if (edge.partial) classes.push("partial");
      if (style.label) classes.push("labelled");
      classes.push(edge.kind === "typed" ? "typed" : "layer");
      // A label on an edge at the focus may move toward the OTHER end once
      // laid out (`placeEndLabels`): the middle of such an edge is often
      // where the focus's own title is.
      // Anywhere else a label prefers the middle, moved along its edge only
      // when the middle is not clear.
      let labelEnd = style.label ? "middle" : "";
      if (style.label && view.focus && edge.from === view.focus) labelEnd = "target";
      else if (style.label && view.focus && edge.to === view.focus) labelEnd = "source";
      const unresolved = classes.indexOf("kedge-unresolved") !== -1;
      return {
        group: "edges",
        data: {
          id: elementId(edgeIds, "e", edge.id),
          pid: edge.id,
          source: nodeIds[edge.from],
          target: nodeIds[edge.to],
          width: edgeWidth(edge.weight) + (unresolved ? EMPHASIS : 0),
          label: style.label || "",
          labelEnd: labelEnd,
          labelOffset: END_LABEL_MIN
        },
        classes: classes.join(" ")
      };
    }

    // The data a drawn element may take from a later payload: never its
    // identity or its ends.
    function changeable(data) {
      const kept = Object.assign({}, data);
      delete kept.id;
      delete kept.source;
      delete kept.target;
      return kept;
    }

    // `view`'s elements onto the canvas: what it no longer draws removed;
    // what it still draws updated where it stands (its size from its new
    // degree, its colour from its value); what is new added, unplaced.
    // Answers the nodes added.
    // `view`'s element definitions, built before anything is drawn from
    // them: a swap that cannot build them changes nothing on the page —
    // the colour slots its values took given back too, so a later view's
    // values take them as if it had never come.
    function elementsOf(view) {
      const slotted = Object.keys(slotOf);
      const taken = slotsTaken;
      try {
        takeSlots(view);
        const sizes = nodeSizes(view.nodes);
        const drawn = dict();
        view.nodes.forEach(function (node) { drawn[node.id] = true; });
        const nodes = view.nodes.map(function (node) { return nodeDefinition(node, sizes); });
        const edges = (view.edges || []).map(function (edge) {
          return edgeDefinition(edge, view, drawn);
        }).filter(Boolean);
        return { nodes: nodes, edges: edges };
      } catch (error) {
        Object.keys(slotOf).forEach(function (value) {
          if (slotted.indexOf(value) === -1) delete slotOf[value];
        });
        slotsTaken = taken;
        throw error;
      }
    }

    function applyElements(view, built) {
      const definitions = built || elementsOf(view);
      const nodes = definitions.nodes;
      const edges = definitions.edges;
      const keep = dict();
      nodes.concat(edges).forEach(function (definition) { keep[definition.data.id] = true; });
      cy.remove(cy.elements().filter(function (element) { return !keep[element.id()]; }));
      const added = [];
      nodes.forEach(function (definition) {
        const existing = cy.getElementById(definition.data.id);
        if (!existing.length) {
          added.push(definition);
          return;
        }
        existing.data(changeable(definition.data));
        existing.classes(definition.classes);
      });
      const addedNodes = added.length ? cy.add(added) : cy.collection();
      const addedEdges = [];
      edges.forEach(function (definition) {
        const existing = cy.getElementById(definition.data.id);
        if (existing.length && (existing.data("source") !== definition.data.source ||
            existing.data("target") !== definition.data.target)) {
          cy.remove(existing);
        } else if (existing.length) {
          existing.data(changeable(definition.data));
          existing.classes(definition.classes);
          return;
        }
        addedEdges.push(definition);
      });
      if (addedEdges.length) cy.add(addedEdges);
      return addedNodes;
    }

    const style = [
      {
        selector: "node",
        style: {
          label: "data(label)",
          width: "data(size)",
          height: "data(size)",
          "background-color": "data(colour)",
          "border-width": 1,
          "border-color": MUTED,
          color: INK,
          "font-size": NODE_FONT,
          "font-family": LABEL_FONT_FAMILY,
          "text-valign": "bottom",
          "text-margin-y": 4,
          "text-wrap": "wrap",
          "text-max-width": 140,
          // On its own ground: an edge passing under a title does not cross
          // its letters.
          "text-background-color": PANEL,
          "text-background-opacity": 0.9,
          "text-background-padding": 2,
          "text-background-shape": "roundrectangle"
        }
      },
      { selector: "node.focus", style: { "border-width": 3, "border-color": INK } },
      // A missing note: dashed, its short id as its label, never dropped.
      { selector: "node.ghost", style: { "border-style": "dashed", "border-width": 2 } },
      {
        selector: "node.archived",
        style: { "background-color": ARCHIVED, color: MUTED, opacity: 0.6 }
      },
      // The note page's quarantined chip red, as a ring.
      {
        selector: "node.quarantined",
        style: { "border-width": 3, "border-color": ACCENT, "border-style": "solid" }
      },
      // Last-known facts, re-read pending: a double ring, apart from the
      // ghost's dashed outline.
      {
        selector: "node.pending",
        style: { "border-style": "double", "border-width": 4, "border-color": WARNING }
      },
      // Both at once — a quarantined note whose re-read is pending keeps its
      // last-known status: the ring stays red AND double, so neither mark
      // hides the other.
      {
        selector: "node.pending.quarantined",
        style: { "border-style": "double", "border-width": 5, "border-color": ACCENT }
      },
      // What showing its neighbours would add (D16): a badge on the note,
      // at its own size whatever the note's, never under its title.
      {
        selector: "node.adds-notes, node.adds-edges",
        style: {
          "background-image": "data(badge)",
          "background-fit": "none",
          "background-clip": "none",
          "background-image-containment": "over"
        }
      },
      {
        selector: "edge",
        style: {
          "curve-style": "bezier",
          width: "data(width)",
          "line-color": EDGE_COLOURS["kedge-unknown"],
          "target-arrow-color": EDGE_COLOURS["kedge-unknown"],
          "target-arrow-shape": "none",
          label: "data(label)",
          "font-size": EDGE_FONT,
          "font-family": LABEL_FONT_FAMILY,
          color: MUTED,
          // Level, not along the curve: a rotated word across a title reads
          // as neither.
          "text-rotation": "none",
          "text-background-color": PANEL,
          "text-background-opacity": 0.9,
          "text-background-padding": 2,
          "text-background-shape": "roundrectangle"
        }
      },
      { selector: "edge.arrow", style: { "target-arrow-shape": "triangle" } },
      // Drawn after the unlabelled edges, so no stroke crosses its words.
      { selector: "edge.labelled", style: { "z-index": 1 } },
      {
        selector: "edge.label-at-target",
        style: { label: "", "target-label": "data(label)", "target-text-offset": "data(labelOffset)" }
      },
      {
        selector: "edge.label-at-source",
        style: { label: "", "source-label": "data(label)", "source-text-offset": "data(labelOffset)" }
      },
      { selector: "edge.stroke-dotted", style: { "line-style": "dotted" } },
      { selector: "edge.stroke-dashed", style: { "line-style": "dashed" } }
    ];
    Object.keys(EDGE_COLOURS).forEach(function (cssClass) {
      style.push({
        selector: "edge." + cssClass,
        style: { "line-color": EDGE_COLOURS[cssClass], "target-arrow-color": EDGE_COLOURS[cssClass] }
      });
    });
    style.push(
      // A wiki-link is a thin grey line whatever else is drawn.
      { selector: "edge.kedge-wiki-link", style: { width: WIDTH_MIN } },
      { selector: "edge.kedge-provenance", style: { width: WIDTH_MIN } },
      {
        selector: "edge.kedge-resolved",
        style: { "line-color": RESOLVED, "target-arrow-color": RESOLVED }
      },
      // Inserted by an event, its details pending: faint, not dashed — a
      // dash pattern already means "contradicts" and a dot "derived from".
      { selector: "edge.partial", style: { opacity: 0.45 } },
      // Search hits and the lit selection, LAST so they win.
      {
        selector: "node.match",
        style: {
          "underlay-color": "#f2c14e",
          "underlay-opacity": 0.75,
          "underlay-padding": 7,
          "font-weight": "bold"
        }
      },
      { selector: ".dimmed", style: { opacity: 0.15 } },
      { selector: "node.lit", style: { "font-weight": "bold" } },
      // Every title as wide as a lit or matched one draws it: while laid out.
      { selector: "node.widest", style: { "font-weight": "bold" } },
      { selector: ".picked", style: { "overlay-color": INK, "overlay-opacity": 0.12, "overlay-padding": 5 } }
    );

    // The frame first: Cytoscape measures its container as it is built, so
    // a `canvas=full` load lays out at the full-page size (D17). The toolbar
    // and the key are shown now too, so what lies over the canvas is in
    // place when the view is fitted to what it leaves.
    const page = document.querySelector("[data-knowledge-graph]");
    applyFrame(!!(page && page.dataset && page.dataset.kgraphPresentation === "full"));
    const toolbar = document.querySelector("[data-kgraph-toolbar]");
    if (toolbar) toolbar.hidden = false;
    const key = document.querySelector("[data-kgraph-key]");
    if (key) key.hidden = false;
    const host = document.querySelector("#kgraph-panel");
    panelHost = host;
    // Revealed before Cytoscape is built: it measures its container.
    container.hidden = false;
    if (container.replaceChildren) container.replaceChildren();
    const cy = window.cytoscape({
      container: container,
      elements: [],
      style: style,
      // A drag pans; nothing moves once placed, and taps do not "select" in
      // Cytoscape's sense — the selection is the URL's.
      autoungrabify: true,
      autounselectify: true,
      boxSelectionEnabled: false,
      minZoom: 0.2,
      maxZoom: 3,
      multiClickDebounceTime: DOUBLE_TAP_MS
    });

    // ── Placement beside the note expanded (D16) ────────────────────────
    // New notes go around the note they came from, on the side facing away
    // from the drawn graph's centre: ring by ring at the base layout's
    // spacing, each at the first place clear of every note placed so far —
    // each reserved at the largest size a note can grow to, under its title
    // as measured at its widest — in id order, so the same graph on the
    // same canvas places the same way. Nothing already placed moves.

    // A title's box: measured where there is a renderer, else estimated
    // from its length (the headless harness has no text metrics).
    function titleSize(node) {
      if (!cy.headless()) {
        const box = node.boundingBox({
          includeNodes: false,
          includeEdges: false,
          includeLabels: true,
          includeOverlays: false
        });
        if (Number.isFinite(box.w) && Number.isFinite(box.h) && box.w > 0) {
          return { w: box.w, h: box.h };
        }
      }
      const length = String(node.data("label") || "").length * NODE_FONT * 0.6;
      const lines = Math.max(1, Math.ceil(length / TITLE_MAX_WIDTH));
      return { w: Math.min(length, TITLE_MAX_WIDTH) + 4, h: lines * NODE_FONT * 1.3 + 4 };
    }

    // An edge label's box, measured where there is a renderer.
    function edgeLabelSize(edge) {
      if (!cy.headless()) {
        const box = labelBox(edge);
        if (Number.isFinite(box.w) && Number.isFinite(box.h) && box.w > 0) {
          return { w: box.w, h: box.h };
        }
      }
      const length = String(edge.data("label") || "").length * EDGE_FONT * 0.6;
      return { w: length + 4, h: EDGE_FONT * 1.3 + 4 };
    }

    // The room a note at `at` takes: its body at the largest size, its
    // title under it, and `margin` around both.
    function reserve(title, at, margin) {
      const half = Math.max(SIZE_MAX, title.w) / 2 + margin;
      return {
        x1: at.x - half,
        x2: at.x + half,
        y1: at.y - SIZE_MAX / 2 - margin,
        y2: at.y + SIZE_MAX / 2 + TITLE_MARGIN + title.h + margin
      };
    }

    function around(point, size) {
      return {
        x1: point.x - size.w / 2 - LABEL_CLEARANCE,
        x2: point.x + size.w / 2 + LABEL_CLEARANCE,
        y1: point.y - size.h / 2 - LABEL_CLEARANCE,
        y2: point.y + size.h / 2 + LABEL_CLEARANCE
      };
    }

    // A place is clear when the note's room meets no other note's, and the
    // middle of each labelled edge to a note already placed has room for
    // its label clear of every note and title — the new one's included.
    const ALONG = [0.5, 0.4, 0.6, 0.3, 0.7];

    function placeBeside(anchor, nodes, placed) {
      const ROOM = LABEL_CLEARANCE + LABEL_ROOM / 2;
      const where = dict();
      const rooms = [];
      const bodies = [];
      placed.forEach(function (node) {
        const title = titleSize(node);
        where[node.id()] = true;
        rooms.push(reserve(title, node.position(), ROOM));
        bodies.push(reserve(title, node.position(), LABEL_CLEARANCE));
      });
      const at = anchor.position();
      let centre = at;
      if (placed.length) {
        const xs = placed.map(function (node) { return node.position().x; });
        const ys = placed.map(function (node) { return node.position().y; });
        centre = {
          x: (Math.min.apply(null, xs) + Math.max.apply(null, xs)) / 2,
          y: (Math.min.apply(null, ys) + Math.max.apply(null, ys)) / 2
        };
      }
      // Only the side facing away: within a quarter turn either way of the
      // line out from the centre through the anchor. An anchor at the
      // centre has no such side, and any direction is away.
      const centred = Math.hypot(at.x - centre.x, at.y - centre.y) < 1;
      const away = centred ? 0 : Math.atan2(at.y - centre.y, at.x - centre.x);
      const sweep = centred ? Math.PI : Math.PI / 2;
      let firstRing = 1;
      nodes.sort(function (a, b) { return byName(a.data("pid"), b.data("pid")); })
        .forEach(function (node) {
          const title = titleSize(node);
          const links = node.connectedEdges(".labelled").map(function (edge) {
            const other = edge.source().same(node) ? edge.target() : edge.source();
            return where[other.id()] ? { other: other.position(), size: edgeLabelSize(edge) } : null;
          }).filter(Boolean);
          const clear = function (point, labelled) {
            const room = reserve(title, point, ROOM);
            if (!rooms.every(function (other) { return apart(room, other); })) return false;
            if (!labelled) return true;
            const body = reserve(title, point, LABEL_CLEARANCE);
            // Somewhere along the edge's middle half, as `placeEndLabels`
            // slides a label along its edge.
            return links.every(function (link) {
              return ALONG.some(function (t) {
                const label = around({
                  x: link.other.x + (point.x - link.other.x) * t,
                  y: link.other.y + (point.y - link.other.y) * t
                }, link.size);
                return apart(label, body) && bodies.every(function (other) { return apart(label, other); });
              });
            });
          };
          // Outward until a place is clear: every room placed is bounded,
          // so a ring far enough out always has one. Each note of a group
          // starts on the ring the one before it took — the rings inside it
          // had no room for that one.
          let spot = null;
          for (let ring = firstRing; !spot; ring += 1) {
            const labelled = ring - firstRing < PLACE_RINGS;
            // First as far out as the base layout's rings are apart (its
            // spacing and a note at its largest), then a note's width more.
            const radius = NODE_SPACING + SIZE_MAX * ring;
            const steps = Math.max(8, Math.ceil((2 * Math.PI * radius) / PLACE_STEP));
            // A side with no clear place on this ring: the next ring out.
            for (let turn = 0; !spot; turn += 1) {
              const offset = (turn * 2 * Math.PI) / steps;
              if (centred ? offset > sweep : offset >= sweep) break;
              [1, -1].forEach(function (side) {
                if (spot || (turn === 0 && side === -1)) return;
                const angle = away + side * offset;
                const point = { x: at.x + radius * Math.cos(angle), y: at.y + radius * Math.sin(angle) };
                if (clear(point, labelled)) spot = point;
              });
            }
            if (spot) firstRing = ring;
          }
          node.position(spot);
          where[node.id()] = true;
          rooms.push(reserve(title, node.position(), ROOM));
          bodies.push(reserve(title, node.position(), LABEL_CLEARANCE));
        });
    }

    // Each group of new notes beside its anchor, in order: the base notes'
    // (`via` none, and any whose `via` is not drawn) beside the focus, then
    // each applied expansion's, in URL order, so an anchor is placed before
    // the notes it brought.
    function placeNew(view, added, placed) {
      const steps = appliedSteps(view);
      const groups = dict();
      added.forEach(function (node) {
        const via = node.data("via");
        const anchored = via && byPid(cy.nodes(), via).length ? via : "";
        (groups[anchored] = groups[anchored] || []).push(node);
      });
      const order = Object.keys(groups).sort(function (a, b) {
        const at = function (id) { return id === "" ? -1 : steps.indexOf(id) === -1 ? steps.length : steps.indexOf(id); };
        return at(a) - at(b) || byName(a, b);
      });
      const focus = cy.nodes(".focus");
      let done = placed;
      order.forEach(function (via) {
        const anchor = via ? byPid(cy.nodes(), via) : focus;
        const group = cy.collection(groups[via]);
        if (anchor.length) placeBeside(anchor[0], groups[via], done);
        done = done.union(group);
      });
    }

    // The edge labels placed again around the notes as they now stand:
    // nothing moves but labels.
    function relabel() {
      cy.edges().removeClass("label-at-source label-at-target");
      cy.edges().forEach(function (edge) { edge.data("labelOffset", END_LABEL_MIN); });
      placeEndLabels(cy);
    }

    // ── A fresh load's layout (D16) ─────────────────────────────────────
    // Laid out and labelled with every title at its widest — bold, as a lit
    // or matched title is drawn — so lighting a selection or a search never
    // grows a title onto a label placed beside it. The base notes (`via`
    // none) are laid out as S4's were; then each applied expansion's notes
    // are placed, in URL order, by the rule a live expansion uses. The same
    // graph, address, faces and canvas size lay out the same way.
    applyElements(current);
    cy.nodes().addClass("widest");
    if (current.mode === "focus") {
      const base = cy.nodes().filter(function (node) { return !node.data("via"); });
      const maxHop = base.reduce(function (high, node) {
        return Math.max(high, node.data("hop") || 0);
      }, 0);
      cy.layout({
        name: "concentric",
        eles: base,
        concentric: function (element) { return maxHop + 1 - element.data("hop"); },
        levelWidth: function () { return 1; },
        // Room between rings for the titles under each ring's notes.
        avoidOverlap: true,
        minNodeSpacing: NODE_SPACING,
        padding: FIT_PADDING,
        animate: false
      }).run();
      // Rings into ellipses as wide as the canvas is wide (up to a limit):
      // the room a wide box has goes between the notes, not to its margins.
      const stretch = cy.height() > 0
        ? Math.min(MAX_STRETCH, Math.max(1, cy.width() / cy.height()))
        : 1;
      const centre = cy.nodes(".focus").length
        ? cy.nodes(".focus").position()
        : { x: 0, y: 0 };
      if (stretch > 1) {
        base.positions(function (node) {
          const at = node.position();
          return { x: centre.x + (at.x - centre.x) * stretch, y: at.y };
        });
      }
      placeNew(current, cy.nodes().not(base), base);
    } else {
      // A circle first, so the force-directed pass starts from the same
      // place on every load and draws the same picture.
      cy.layout({ name: "circle", animate: false }).run();
      cy.layout({
        name: "cose",
        animate: false,
        randomize: false,
        padding: FIT_PADDING,
        nodeDimensionsIncludeLabels: true,
        nodeRepulsion: function () { return 400000; },
        idealEdgeLength: function () { return 140; }
      }).run();
      // Its long side along the canvas's long side: a chain laid out down a
      // wide box (or across a narrow one) is mirrored on its diagonal.
      const laid = cy.nodes().boundingBox();
      const tall = laid.h > laid.w;
      if (cy.width() > 0 && cy.height() > 0 && tall !== cy.height() > cy.width()) {
        cy.nodes().positions(function (node) {
          const at = node.position();
          return { x: at.y, y: at.x };
        });
      }
    }
    placeEndLabels(cy);

    // ── What lies over the canvas, and the area it leaves (D17) ─────────
    // Inline, nothing. In full page the toolbar, the key and an open drawer
    // or bottom sheet, each taken off the side of the canvas whose loss is
    // the least area — the toolbar off the top, the drawer off the right,
    // the key and the sheet off the bottom — so a fit, a pan and the pan
    // hint all work in what the operator can see. Measured again whenever
    // one of them may have moved.
    const stage = document.querySelector("[data-kgraph-stage]");
    const panHint = document.querySelector("[data-kgraph-pan-hint]");
    let usable = { x1: 0, y1: 0, x2: cy.width(), y2: cy.height() };
    //: The side of the canvas each overlay was taken off, as last measured.
    let sideOf = new Map();
    //: The height the overlays leave, below zero where they overlap.
    let band = 0;
    //: The width they leave, likewise.
    let span = 0;
    //: The key's side when the selection needs the width beside a drawer:
    //: the bottom, giving back the left (else the cheaper of the two).
    let keyAnchor = null;
    //: The pan hint's, when the selection needs the height below the
    //: toolbar: the left, giving back the top.
    let hintAnchor = null;

    function widthOf(elt) {
      return elt && typeof elt.getBoundingClientRect === "function"
        ? elt.getBoundingClientRect().width
        : 0;
    }

    function heightOf(elt) {
      return elt && typeof elt.getBoundingClientRect === "function"
        ? elt.getBoundingClientRect().height
        : 0;
    }

    // Measures what lies over the canvas and the area it leaves.
    function measureOverlays() {
      const width = cy.width();
      const height = cy.height();
      const open = hasPanel();
      // The drawer sits under the toolbar and the key above the sheet.
      if (stage && stage.classList) stage.classList.toggle("kgraph-panel-open", open);
      if (stage && stage.style) {
        stage.style.setProperty("--kgraph-toolbar-height", heightOf(toolbar) + "px");
        stage.style.setProperty("--kgraph-panel-height", (open ? heightOf(host) : 0) + "px");
        // The key reflows beside a drawer rather than under it.
        stage.style.setProperty("--kgraph-panel-width", (open ? widthOf(host) : 0) + "px");
      }
      const inset = { top: 0, right: 0, bottom: 0, left: 0 };
      sideOf = new Map();
      if (fullPage && typeof container.getBoundingClientRect === "function") {
        const box = container.getBoundingClientRect();
        // The pan hint keeps its place while hidden in full page (lens.css),
        // so the room it takes does not come and go as the operator pans.
        [toolbar, panHint, key, open ? host : null].forEach(function (elt) {
          if (!elt || typeof elt.getBoundingClientRect !== "function") return;
          if (elt.hidden && elt !== panHint) return;
          const over = elt.getBoundingClientRect();
          const x1 = Math.max(over.left - box.left, 0);
          const x2 = Math.min(over.right - box.left, width);
          const y1 = Math.max(over.top - box.top, 0);
          const y2 = Math.min(over.bottom - box.top, height);
          if (x2 <= x1 || y2 <= y1) return;
          // Only a side it is anchored to: the toolbar the top, the pan hint
          // (under it) the top or the left, the key the bottom or (narrow)
          // the left, the panel the right or the bottom.
          const anchored = elt === toolbar
            ? ["top"]
            : elt === panHint ? (hintAnchor ? [hintAnchor] : ["top"])
              : elt === key ? (keyAnchor ? [keyAnchor] : ["bottom", "left"]) : ["right", "bottom"];
          const sides = [
            ["top", y2, y2 * width],
            ["bottom", height - y1, (height - y1) * width],
            ["left", x2, x2 * height],
            ["right", width - x1, (width - x1) * height]
          ].filter(function (side) { return anchored.indexOf(side[0]) !== -1; })
            .sort(function (a, b) { return a[2] - b[2]; });
          inset[sides[0][0]] = Math.max(inset[sides[0][0]], sides[0][1]);
          sideOf.set(elt, sides[0][0]);
        });
      }
      usable = { x1: inset.left, y1: inset.top, x2: width - inset.right, y2: height - inset.bottom };
      band = usable.y2 - usable.y1;
      span = usable.x2 - usable.x1;
      // Overlays that meet leave no area: an empty one where they meet, so
      // what is short is measured, not hidden.
      if (usable.x2 < usable.x1) usable.x1 = usable.x2 = (usable.x1 + usable.x2) / 2;
      if (usable.y2 < usable.y1) usable.y1 = usable.y2 = (usable.y1 + usable.y2) / 2;
    }

    // Says so while — and only while — part of the graph is outside the
    // view (the usable area, in full page): on opening, after every pan,
    // zoom or resize, and after a selection or a search re-weights titles.
    function showPanHint() {
      if (!panHint) return;
      const drawn = cy.elements().renderedBoundingBox();
      panHint.hidden = !(
        drawn.x1 < usable.x1 || drawn.y1 < usable.y1 ||
        drawn.x2 > usable.x2 || drawn.y2 > usable.y2
      );
    }

    // ── Room for the selected note (D17) ─────────────────────────────────
    // The band the overlays leave is made tall enough for the selected
    // note at the zoom it is drawn at: the key, then the sheet, then the
    // toolbar give way — each capped (and scrolled) down to no less than
    // its own controls row — taking only what is short. Each measure starts
    // uncapped, so room given is room returned once it is not needed.
    const CAPS = [
      [key, "--kgraph-key-cap"],
      [host, "--kgraph-panel-cap"],
      [toolbar, "--kgraph-toolbar-cap"]
    ];
    //: The least an open sheet keeps: its first lines.
    const SHEET_FLOOR = 40;
    //: The narrowest a drawer gives way to (18rem): still a readable panel.
    const DRAWER_FLOOR = 288;
    //: Kept clear around the selected note, where there is room for it.
    const ROOM_PADDING = 4;

    //: The key's own least height (`lens.css`): its disclosure row.
    const KEY_FLOOR = 40;

    // An overlay's floor: its own disclosure and toggle rows, padded.
    function floorOf(elt) {
      if (elt === host) return SHEET_FLOOR;
      const top = elt.getBoundingClientRect().top;
      const rows = elt === key
        ? ["[data-kgraph-key-toggle]"]
        : ["[data-kgraph-full-toggle]", "[data-kgraph-controls-toggle]"];
      let bottom = top;
      rows.forEach(function (selector) {
        const row = within(elt, selector);
        if (!row || typeof row.getBoundingClientRect !== "function") return;
        const box = row.getBoundingClientRect();
        if (box.height > 0) bottom = Math.max(bottom, box.bottom);
      });
      return Math.max(bottom - top + 9, elt === key ? KEY_FLOOR : 0);
    }

    function makeRoom(zoom) {
      const capped = stage && stage.style && typeof stage.style.removeProperty === "function";
      if (capped) {
        CAPS.forEach(function (cap) { stage.style.removeProperty(cap[1]); });
        stage.style.removeProperty("--kgraph-drawer-cap");
      }
      keyAnchor = null;
      hintAnchor = null;
      measureOverlays();
      const selection = urlSelection();
      if (!capped || !fullPage || !selection.length) return;
      const model = selection.boundingBox();
      // Short of width (beside a drawer): the key is counted off the
      // bottom, where it also lies, so the band is all the width the drawer
      // leaves; then the drawer narrows, to no less than DRAWER_FLOOR.
      const wide = model.w * zoom + 2 * ROOM_PADDING;
      if (sideOf.get(key) === "left" && wide > span) {
        keyAnchor = "bottom";
        measureOverlays();
      }
      if (sideOf.get(host) === "right" && wide > span) {
        const drawer = host.getBoundingClientRect().width;
        stage.style.setProperty(
          "--kgraph-drawer-cap", Math.max(DRAWER_FLOOR, drawer - (wide - span)) + "px");
        measureOverlays();
      }
      // Short of height below the toolbar: the pan hint is counted off the
      // left, where it also lies, when the note still fits beside it.
      if (sideOf.get(panHint) === "top" && model.h * zoom + 2 * ROOM_PADDING > band) {
        hintAnchor = "left";
        measureOverlays();
        if (wide > span) {
          hintAnchor = null;
          measureOverlays();
        }
      }
      let short = model.h * zoom + 2 * ROOM_PADDING - band;
      if (short <= 0) return;
      // Every overlay across the band is held at its height (or cut), so
      // none grows into the room another gives.
      CAPS.forEach(function (cap) {
        const elt = cap[0];
        const side = sideOf.get(elt);
        if (!elt || (side !== "top" && side !== "bottom")) return;
        const height = heightOf(elt);
        const cut = Math.max(0, Math.min(short, height - floorOf(elt)));
        stage.style.setProperty(cap[1], height - cut + "px");
        short -= cut;
      });
      measureOverlays();
    }

    // ── Lit and dimmed: the selection and its neighbours ────────────────
    function light(selection) {
      cy.elements().removeClass("lit dimmed picked");
      if (selection && selection.length) {
        const lit = selection.isNode()
          ? selection.closedNeighborhood()
          : selection.union(selection.connectedNodes());
        lit.addClass("lit");
        selection.addClass("picked");
        cy.elements().not(lit).addClass("dimmed");
      }
      showPanHint();
    }

    function byPid(collection, pid) {
      return collection.filter(function (element) { return element.data("pid") === pid; });
    }

    // The URL's selection: `edge=` wins over `selected=`, as the parser
    // reads them; `pin=` is never a selection. An id the payload does not
    // draw selects nothing.
    function urlSelection() {
      const query = new URLSearchParams(window.location.search);
      const selectedEdge = queryValue(query, "edge");
      const selectedNode = selectedEdge ? "" : queryValue(query, "selected");
      if (selectedEdge) return byPid(cy.edges(".typed"), selectedEdge);
      if (selectedNode) return byPid(cy.nodes(), selectedNode);
      return cy.collection();
    }

    // Lit from the URL on load and after a Back onto the same address.
    lightFromUrl = function () {
      light(urlSelection());
    };

    // ── The toolbar ─────────────────────────────────────────────────────
    const filters = payload.filters || {};
    const minWeight = typeof filters.min_weight === "number" ? filters.min_weight : WEIGHT_FLOOR;

    const slider = document.querySelector("[data-kgraph-min-weight]");
    const sliderValue = document.querySelector("[data-kgraph-min-weight-value]");
    if (slider) {
      // A threshold off the slider's grid (a hand-typed `min_weight=0.123`,
      // a configured default) is shown as applied, not rounded to a
      // neighbour. Whether it is on the grid is the browser's answer — the
      // value it kept — not a tolerance of ours: when it rounded, the slider
      // takes any value until it is moved, and the first move puts it back
      // on the grid.
      slider.value = weightText(minWeight);
      if (Number(slider.value) !== minWeight) {
        slider.step = "any";
        slider.value = weightText(minWeight);
      }
      if (sliderValue) sliderValue.textContent = weightText(minWeight);
      slider.addEventListener("input", function () {
        if (slider.step === "any") {
          const snapped = snapToSlider(slider.value);
          slider.step = String(SLIDER_STEP);
          slider.value = weightText(snapped);
        }
        if (sliderValue) sliderValue.textContent = slider.value;
      });
      slider.addEventListener("change", function () {
        navigate({ min_weight: slider.value });
      });
    }
    const hiddenCount = document.querySelector("[data-kgraph-weight-hidden]");
    const fullToggle = document.querySelector("[data-kgraph-full-toggle]");

    // The provenance toggles as the view on screen offers them: a swap may
    // bring the fieldset in, or new counts (D16), so each is bound once,
    // whenever it arrives.
    function provenanceToggles() {
      return Array.prototype.slice.call(document.querySelectorAll("[data-kgraph-provenance]"));
    }
    function bindProvenance() {
      provenanceToggles().forEach(function (toggle) {
        if (toggle.kgraphBound) return;
        toggle.kgraphBound = true;
        toggle.addEventListener("change", function () {
          const filters = current.filters || {};
          const groups = Array.isArray(filters.provenance) ? filters.provenance : PROVENANCE_GROUPS;
          const group = toggle.dataset.kgraphProvenance;
          const next = groups.filter(function (name) { return name !== group; });
          if (toggle.checked) next.push(group);
          navigate({ provenance: provenanceValue(next) });
        });
      });
    }

    // Everything the toolbar says or allows from the view on screen: the
    // hidden count, the provenance toggles, the depths' would-be counts —
    // and, while an expansion or collapse is out, every control that
    // changes the graph or the presentation disabled (D16). Search, pan,
    // zoom and the disclosures stay live.
    function renderControls() {
      const busy = !!graphRequest;
      const filters = current.filters || {};
      const groups = Array.isArray(filters.provenance) ? filters.provenance : PROVENANCE_GROUPS;
      if (hiddenCount) {
        const hidden = (current.hidden && current.hidden.by_weight) || 0;
        hiddenCount.textContent = plural(hidden, "edge", "edges") + " below " + weightText(minWeight) + " hidden";
      }
      const toggles = provenanceToggles();
      toggles.forEach(function (toggle) {
        toggle.checked = groups.indexOf(toggle.dataset.kgraphProvenance) !== -1;
      });
      const checked = toggles.filter(function (toggle) { return toggle.checked; });
      toggles.forEach(function (toggle) {
        // The last group on cannot be turned off: an empty list means "all".
        toggle.disabled = busy || (checked.length === 1 && toggle.checked);
      });
      const wouldBe = current.would_be_nodes || {};
      document.querySelectorAll("[data-kgraph-depth-count]").forEach(function (count) {
        const level = count.dataset.kgraphDepthCount;
        const notes = wouldBe[level];
        count.textContent = Number(level) !== current.depth && typeof notes === "number"
          ? ": " + plural(notes, "note", "notes")
          : "";
      });
      if (slider) slider.disabled = busy;
      document.querySelectorAll("[data-kgraph-depth], [data-kgraph-colour]").forEach(function (radio) {
        radio.disabled = busy;
      });
      if (fullToggle) fullToggle.disabled = busy;
    }

    bindProvenance();
    document.querySelectorAll("[data-kgraph-depth]").forEach(function (radio) {
      radio.checked = Number(radio.value) === payload.depth;
      radio.addEventListener("change", function () {
        if (radio.checked) navigate({ depth: radio.value });
      });
    });
    document.querySelectorAll("[data-kgraph-colour]").forEach(function (radio) {
      radio.checked = radio.value === mode;
      radio.addEventListener("change", function () {
        if (radio.checked) navigate({ colour: radio.value === "type" ? "type" : null });
      });
    });
    renderControls();

    const search = document.querySelector("[data-kgraph-search]");
    const searchCount = document.querySelector("[data-kgraph-search-count]");
    function applySearch() {
      const needle = String((search && search.value) || "").trim().toLowerCase();
      cy.nodes().removeClass("match");
      if (!needle) {
        if (searchCount) searchCount.textContent = "";
      } else {
        const hits = cy.nodes().filter(function (element) {
          return String(element.data("label") || "").toLowerCase().indexOf(needle) !== -1;
        });
        hits.addClass("match");
        if (searchCount) searchCount.textContent = plural(hits.length, "match", "matches");
      }
      showPanHint();
    }
    if (search) search.addEventListener("input", applySearch);
    // From the field as it stands: a page the browser restored (a reload
    // after Back) may bring the field's value back with it.
    applySearch();

    // ── The canvas's key: the text legend's lines, the colours present ──
    // Redrawn from the view on screen after each swap (D16).
    function renderKey() {
      const edgeKey = document.querySelector("[data-kgraph-key-edges]");
      if (edgeKey) {
        edgeKey.replaceChildren.apply(edgeKey, (current.legend || []).map(function (line) {
          const cssClass = line["class"];
          const layer = cssClass === "kedge-wiki-link" || cssClass === "kedge-provenance";
          const name = layer
            ? (cssClass === "kedge-wiki-link" ? "wiki-link" : "provenance") + " (layer)"
            : line.type;
          const colour = EDGE_COLOURS[cssClass] || EDGE_COLOURS["kedge-unknown"];
          return keyItem(swatch(colour, strokeOfClass(cssClass)), name, { keyType: line.type });
        }));
      }
      const nodeKey = document.querySelector("[data-kgraph-key-nodes]");
      if (nodeKey) {
        const items = [element("li", "kgraph-key-heading",
          "Colour: " + (mode === "type" ? "note type" : "namespace"))];
        // Every value present, most nodes first; one past the palette's slots
        // is named with the neutral it shares.
        rankedValues(current.nodes, mode).values.forEach(function (value) {
          items.push(keyItem(swatch(colourOf(value)), value, { keyValue: value }));
        });
        const unvalued = current.nodes.some(function (node) {
          return colourValue(node, mode) === null;
        });
        if (unvalued) {
          items.push(keyItem(swatch(NODE_NEUTRAL), "none (not found or not read)", { keyValue: "" }));
        }
        items.push(element("li", "kgraph-key-heading", "Size: connections in this view"));
        nodeKey.replaceChildren.apply(nodeKey, items);
      }
      const markKey = document.querySelector("[data-kgraph-key-marks]");
      if (markKey) {
        const marks = [];
        const present = function (name) { return cy.elements("." + name).length > 0; };
        if (present("ghost")) marks.push(["ghost", "dashed: note not found"]);
        if (present("pending")) marks.push(["pending", "double ring: facts pending"]);
        if (present("quarantined")) marks.push(["quarantined", "red ring: quarantined"]);
        if (present("archived")) marks.push(["archived", "greyed: archived"]);
        if (present("partial")) marks.push(["partial", "faint edge: updated by event; details pending"]);
        if (present("adds-notes")) {
          marks.push(["adds-notes", "+N: showing its neighbours adds N notes to this view"]);
        }
        if (present("adds-edges")) {
          marks.push(["adds-edges", "↔: showing its neighbours adds only edges between notes already drawn"]);
        }
        markKey.replaceChildren.apply(markKey, marks.map(function (mark) {
          return keyItem(null, mark[1], { keyMark: mark[0] });
        }));
      }
    }
    renderKey();

    // ── The fit: everything in the usable area, never below readable ────
    // Cytoscape's own `fit` pads every side alike; this one fits to the
    // area the overlays leave. At most MAX_FIT_ZOOM, and never below
    // MIN_READABLE_ZOOM: a graph that then does not fit is centred on its
    // selection, else its focus, and the pan hint says the rest is a drag
    // away. A refit or a pan the operator caused moves smoothly unless they
    // prefer reduced motion; a load does not. No node moves.
    //: The viewport a refit or pan is moving to, while it moves, and
    //: whether it is a fit: what a change of geometry meanwhile builds on,
    //: rather than the frame it happens to interrupt.
    let inFlight = null;

    function moveTo(viewport, animate, fit) {
      cy.stop(true, false);
      if (animate && !reducedMotion()) {
        const flight = { target: viewport, fit: !!fit };
        inFlight = flight;
        container.dataset.canvasMotion = "moving";
        cy.animate({ zoom: viewport.zoom, pan: viewport.pan }, {
          duration: MOTION_MS,
          complete: function () {
            if (inFlight === flight) inFlight = null;
            container.dataset.canvasMotion = "still";
          }
        });
      } else {
        inFlight = null;
        cy.viewport(viewport);
        container.dataset.canvasMotion = "still";
      }
    }

    function centreOf(box) {
      return { x: (box.x1 + box.x2) / 2, y: (box.y1 + box.y2) / 2 };
    }

    function fitView(animate) {
      // Room for the selection at least at the readable zoom: a fit that
      // needs it opens there, centred on the selection.
      makeRoom(MIN_READABLE_ZOOM);
      const box = cy.elements().boundingBox();
      const roomW = usable.x2 - usable.x1 - 2 * FIT_PADDING;
      const roomH = usable.y2 - usable.y1 - 2 * FIT_PADDING;
      let zoom = Math.min(roomW / box.w, roomH / box.h, MAX_FIT_ZOOM, cy.maxZoom());
      let centre = centreOf(box);
      if (!(zoom >= MIN_READABLE_ZOOM)) {
        zoom = MIN_READABLE_ZOOM;
        const selection = urlSelection();
        const anchor = selection.length ? selection : cy.nodes(".focus");
        if (anchor.length) centre = centreOf(anchor.boundingBox());
      }
      zoom = Math.max(zoom, cy.minZoom());
      const middle = centreOf(usable);
      moveTo({
        zoom: zoom,
        pan: { x: middle.x - zoom * centre.x, y: middle.y - zoom * centre.y }
      }, animate, true);
    }

    // The selection kept in the usable area whenever that area moves — a
    // drawer or sheet opening over it, a disclosure, the pill, the window:
    // room made for it at the zoom it is, then a pan, with as much of
    // FIT_PADDING as there is room for — from where a move in flight is
    // going, not the frame it has reached. A pan alone: the zoom is the
    // operator's (D12), so a note drawn larger than the canvas less every
    // overlay's floor is centred in what there is.
    function revealSelection(animate) {
      const selection = urlSelection();
      const base = inFlight ? inFlight.target : { zoom: cy.zoom(), pan: cy.pan() };
      makeRoom(base.zoom);
      if (!fullPage || !selection.length) return;
      const model = selection.boundingBox();
      const zoom = base.zoom;
      const next = base.pan;
      const shift = function (low, high, from, to) {
        if (high - low > to - from) return (from + to) / 2 - (low + high) / 2;
        const pad = Math.min(FIT_PADDING, (to - from - (high - low)) / 2);
        if (low < from + pad) return from + pad - low;
        if (high > to - pad) return to - pad - high;
        return 0;
      };
      const dx = shift(zoom * model.x1 + next.x, zoom * model.x2 + next.x, usable.x1, usable.x2);
      const dy = shift(zoom * model.y1 + next.y, zoom * model.y2 + next.y, usable.y1, usable.y2);
      // Already clear (or going there): a move in flight runs on.
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
      moveTo({ zoom: zoom, pan: { x: next.x + dx, y: next.y + dy } }, animate);
    }

    // Anything over the canvas, or the canvas itself, changed size: the
    // room re-made from the overlays' own sizes (a held one would hide a
    // disclosure or the pill) and the selection kept in what is left.
    // A fit still in flight is fitted again to the new geometry, not
    // stopped where it has got to.
    // A note's first tap opens its panel at once, and in full page that
    // panel (and the drawer it opens) pans the canvas to keep the note
    // clear. Inside the double-click window that pan would move the note
    // from under the second tap, which then lands beside it and is no
    // double click (D16): until the window ends every such move is held,
    // and the latest is made then. Held, the canvas counts as moving.
    let tapWindowEnds = 0;
    let heldMove = null;
    function holdForTap(move) {
      const wait = tapWindowEnds - Date.now();
      if (!(wait > 0)) return false;
      if (!heldMove) {
        container.dataset.canvasMotion = "moving";
        setTimeout(function () {
          const next = heldMove;
          heldMove = null;
          if (!inFlight) container.dataset.canvasMotion = "still";
          if (next) next();
        }, wait);
      }
      heldMove = move;
      return true;
    }

    // …and the panel itself, opened over the canvas as a drawer or sheet,
    // would take the second press if it opened over the very note the first
    // tap hit: then, and only then, it is kept out of sight — and so out of
    // the pointer's way — until the window ends. Opened anywhere else it is
    // live at once, its links clickable the moment they show.
    let tappedBox = null;
    let concealTimer = null;
    function revealPanel() {
      if (concealTimer !== null) {
        clearTimeout(concealTimer);
        concealTimer = null;
      }
      if (!host || !host.style || host.style.visibility !== "hidden") return;
      host.style.visibility = "";
      // The heading the open could not focus while hidden.
      const heading = within(host, "[data-kgraph-panel-heading]");
      const active = document.activeElement;
      if (heading && typeof heading.focus === "function" && (!active || active === document.body)) {
        heading.focus({ preventScroll: true });
      }
    }
    function concealOverTap() {
      const wait = tapWindowEnds - Date.now();
      if (!(wait > 0) || !tappedBox || !host || !host.style ||
          typeof host.getBoundingClientRect !== "function") return;
      const box = host.getBoundingClientRect();
      const over = { x1: box.left, x2: box.right, y1: box.top, y2: box.bottom };
      if (apart(over, tappedBox)) return;
      host.style.visibility = "hidden";
      if (concealTimer === null) concealTimer = setTimeout(revealPanel, wait);
    }
    // Where the tapped note is drawn, in the window: what the panel must not
    // open over while the second press may still come.
    function noteOnScreen(node) {
      if (typeof container.getBoundingClientRect !== "function") return null;
      const frame = container.getBoundingClientRect();
      const box = node.renderedBoundingBox({ includeLabels: false, includeOverlays: false });
      if (!Number.isFinite(box.x1) || !Number.isFinite(box.y1)) return null;
      return {
        x1: frame.left + box.x1,
        x2: frame.left + box.x2,
        y1: frame.top + box.y1,
        y2: frame.top + box.y2
      };
    }

    function geometryChanged(animate) {
      if (holdForTap(function () { geometryChanged(true); })) return;
      cy.resize();
      if (inFlight && inFlight.fit) fitView(animate);
      else revealSelection(animate);
      showPanHint();
    }

    // Fitted at the widest too: a title lit once the view is open stays in it.
    fitView(false);
    cy.nodes().removeClass("widest");
    if (panHint) {
      showPanHint();
      cy.on("viewport resize", showPanHint);
    }
    lightFromUrl();
    // Lit, the selection is drawn larger than it was fitted: kept clear.
    revealSelection(false);

    // ── Clicks open the S5 panel ────────────────────────────────────────
    const panelSource = document.querySelector("[data-kgraph-panel-source]");
    canvasSource = panelSource;

    function openPanel(changes) {
      const pageQuery = queryWith(changes);
      const panelParams = new URLSearchParams(pageQuery);
      const render = host && host.dataset ? host.dataset.kgraphRender || "" : "";
      if (render) panelParams.set(RENDER_KEY, render);
      const page = pageUrl(pageQuery);
      if (!window.htmx || !panelSource) {
        window.location.assign(page);
        return;
      }
      // Pushed once this request's fragment is in the host (`trackPanel`).
      canvasPage = page;
      window.htmx.ajax("GET", PANEL_PATH + "?" + panelParams.toString(), {
        source: panelSource,
        target: "#kgraph-panel",
        swap: "innerHTML"
      });
    }

    // What the previous tap was on, whatever it was on. Cytoscape's
    // `dbltap` is two taps in time, on whatever the second hit — even when
    // the first hit another note or the background (`graph.js` reads it so
    // too) — so only a pair on ONE note is that note's double click; any
    // other pair is two single clicks, each answered as its tap. Recorded on
    // the raw `tap`, which always comes before the `dbltap` that may follow.
    let previousTapId = "";
    let latestTapId = "";
    cy.on("tap", function (event) {
      const target = event.target;
      previousTapId = latestTapId;
      latestTapId = target && target !== cy && typeof target.id === "function" ? target.id() : "";
    });

    cy.on("tap", "node", function (event) {
      // A panel action: inert while an expansion or collapse is out (D16).
      if (graphRequest) return;
      tapWindowEnds = Date.now() + DOUBLE_TAP_MS;
      tappedBox = noteOnScreen(event.target);
      const pid = event.target.data("pid");
      const now = new URLSearchParams(window.location.search);
      // A node opened from a view drawn for an edge pins that edge, as the
      // text's node links do (`knowledge_graph_url`), so the panel is drawn
      // from the very view on screen.
      const pin = queryValue(now, "edge") || queryValue(now, "pin") || null;
      light(event.target);
      openPanel({ selected: pid, edge: null, pin: pin });
    });
    cy.on("tap", "edge", function (event) {
      // Only a typed row has an edge panel; a layer pair is not one.
      if (graphRequest || !event.target.hasClass("typed")) return;
      light(event.target);
      openPanel({ edge: event.target.data("pid"), selected: null, pin: null });
    });
    cy.on("tap", function (event) {
      if (event.target === cy && !graphRequest) light(null);
    });

    // A double click expands a note the server says can (D16): its first
    // tap selected it, as a single click does, and the expansion's swap
    // supersedes that panel request. A note over the cap sends nothing — its
    // panel says why and offers the reload that asks again — and an
    // expanded, complete or focus note (or one in a scoped view) does
    // nothing more than its taps did. Collapse is the panel's link.
    cy.on("dbltap", "node", function (event) {
      const node = event.target;
      // The pair is over: what it held may move now, and a panel kept out
      // of sight shows.
      tapWindowEnds = 0;
      revealPanel();
      if (previousTapId !== node.id()) return;
      const pid = node.data("pid");
      const shown = current.nodes.filter(function (each) { return each.id === pid; })[0];
      const expansion = shown && shown.expansion;
      if (!expansion || expansion.state !== "available") return;
      const href = pageUrl(expandQuery(pid, function (edgeId) {
        return byPid(cy.edges(".typed"), edgeId).length > 0;
      }));
      if (!changeGraph(href, null, pid)) window.location.assign(href);
    });

    // ── Full page: the toggle, the disclosures, the panel (D17) ─────────
    function setFullPage(full) {
      if (full === fullPage || retired) return;
      // Disabled while an expansion or collapse is out; were it reached
      // anyway, that request is retired as a Back would retire it (D16).
      retireGraphRequest();
      abandonPending();
      lightFromUrl();
      applyFrame(full);
      window.history.pushState(
        { kgraph: true }, "", withCanvas(withoutFragment(window.location.href), full));
      rewriteLinks(full);
      syncRefresh();
      shownHref = window.location.href;
      cy.resize();
      fitView(true);
    }

    const toggle = document.querySelector("[data-kgraph-full-toggle]");
    if (toggle) {
      toggle.addEventListener("click", function () { setFullPage(!fullPage); });
    }

    // "Controls" (below 768px) and "Key": shown in full page only.
    [["[data-kgraph-controls-toggle]", toolbar], ["[data-kgraph-key-toggle]", key]]
      .forEach(function (pair) {
        const button = document.querySelector(pair[0]);
        const box = pair[1];
        if (!button || !box) return;
        button.addEventListener("click", function () {
          const open = button.getAttribute("aria-expanded") !== "true";
          button.setAttribute("aria-expanded", open ? "true" : "false");
          if (box.classList) box.classList.toggle("kgraph-disclosed", open);
          geometryChanged(true);
        });
      });

    panelMoved = function (opened) {
      if (opened) concealOverTap();
      if (opened && holdForTap(function () { panelMoved(true); })) return;
      if (!opened) light(null);
      // Opened, the selection is kept clear by a pan alone (D12); closed,
      // room given back.
      revealSelection(true);
      showPanHint();
    };

    overlaysMoved = geometryChanged;
    // The shell header's height changes with the window's width.
    window.addEventListener("resize", function () {
      if (fullPage) measureHeader();
      geometryChanged(false);
    });
    // And whatever else resizes them — a panel's content, a wrapped
    // toolbar — once laid out: a frame later, so the variables measuring
    // sets are not a resize inside the observer's own callback.
    if (typeof window.ResizeObserver === "function" &&
        typeof window.requestAnimationFrame === "function") {
      let queued = false;
      const observer = new window.ResizeObserver(function () {
        if (queued) return;
        queued = true;
        window.requestAnimationFrame(function () {
          queued = false;
          geometryChanged(true);
        });
      });
      [stage, toolbar, key, host].forEach(function (elt) { if (elt) observer.observe(elt); });
    }

    // ── Expansion and collapse in place (D16) ───────────────────────────
    // One request at a time. `changeGraph` retires the panel request in
    // flight, disables what would change the graph or the presentation,
    // and fetches the page itself at the new address (no route of its
    // own). The answer is read and checked whole before anything on screen
    // changes: a 200 page whose payload parses as a drawn focus view, not
    // refused, with a rendered panel host. Then, in one synchronous step,
    // its text, panel and render id, payload, toolbar counts and picture
    // replace this page's, the address is pushed and the controls come
    // back. Anything else — offline, refused, the render cap's 503, a
    // network error — leaves the view and the address as they were, says
    // why under the toolbar's buttons, and links to the address as a page.
    const status = document.querySelector("[data-kgraph-graph-status]");

    function showStatus(reason, href) {
      if (!status) return;
      const link = element("a", "", "Load that view as a page");
      link.setAttribute("href", href);
      status.replaceChildren(element("span", "", "The graph was not changed: " + reason + " "), link);
      if (status.hidden) {
        status.hidden = false;
        overlaysMoved(true);
      }
    }

    function hideStatus() {
      if (!status || status.hidden) return;
      status.hidden = true;
      status.replaceChildren();
      overlaysMoved(true);
    }

    busyChanged = function () {
      const busy = !!graphRequest;
      container.dataset.canvasRequest = busy ? "busy" : "idle";
      if (stage && typeof stage.setAttribute === "function") {
        stage.setAttribute("aria-busy", busy ? "true" : "false");
      }
      renderControls();
    };

    function failure(reason) {
      const error = new Error(reason);
      error.kgraphReason = reason;
      return error;
    }

    // The answer, checked whole: anything short of a drawn, unrefused focus
    // view with its panel host's render id is a failure, read as nothing.
    function readView(text) {
      const doc = new window.DOMParser().parseFromString(text, "text/html");
      const source = doc.querySelector("[data-knowledge-graph-payload]");
      if (!source) throw failure("no graph came back (Lithos may be offline).");
      let next = null;
      try {
        next = JSON.parse(source.textContent);
      } catch (error) {
        next = null;
      }
      if (!next || typeof next !== "object") throw failure("the answer could not be read.");
      if (next.refusal) {
        throw failure("that view was refused. " + (next.refusal.message || ""));
      }
      if (next.mode !== "focus" || !Array.isArray(next.nodes) || !next.nodes.length) {
        throw failure("the answer drew no graph.");
      }
      if (!wholeView(next)) throw failure("the answer could not be read.");
      const nextHost = doc.querySelector("#kgraph-panel");
      const render = nextHost ? nextHost.getAttribute("data-kgraph-render") || "" : "";
      if (!render) throw failure("the answer could not be read.");
      return { doc: doc, payload: next, text: source.textContent, host: nextHost, render: render };
    }

    function fetchView(request) {
      return window.fetch(request.href, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
        signal: request.controller ? request.controller.signal : undefined
      }).then(function (response) {
        if (response.status === 503) throw failure("Lens is drawing too many views at once.");
        if (response.status !== 200) throw failure("the server answered " + response.status + ".");
        return response.text();
      }, function () {
        throw failure("the request did not complete.");
      }).then(readView);
    }

    function adopt(node) {
      return typeof document.importNode === "function" ? document.importNode(node, true) : node;
    }

    // `selector`'s element as the answer has it: replaced, taken out, or
    // put in by `place`. Answers what was put in.
    function swapPart(doc, selector, place) {
      const old = document.querySelector(selector);
      const next = doc.querySelector(selector);
      const node = next ? adopt(next) : null;
      if (old && node) old.replaceWith(node);
      else if (old) old.remove();
      else if (node) place(node);
      return node;
    }

    // The answer's text in place of this page's: the scope line where it
    // is (above the stage inline, below it in full page), the sections
    // between the stage and the payload as the answer has them, the payload,
    // the panel and its render id, and the toolbar's view-dependent parts.
    // Every server-written link in them is the answer's, so every panel
    // request after this one names the new render. htmx is told of each.
    function swapText(view) {
      const doc = view.doc;
      const inserted = [];
      const keep = function (node) { if (node) inserted.push(node); };
      keep(swapPart(doc, "[data-kgraph-scope]", function () {}));
      // The answer's own address for its view: the next double click's base.
      const page = document.querySelector("[data-knowledge-graph]");
      const nextPage = doc.querySelector("[data-knowledge-graph]");
      const address = nextPage ? nextPage.getAttribute("data-kgraph-address") : null;
      if (page && address) page.setAttribute("data-kgraph-address", address);
      const source = document.querySelector("[data-knowledge-graph-payload]");
      TEXT_SECTIONS.forEach(function (selector) {
        const old = document.querySelector(selector);
        if (old) old.remove();
      });
      TEXT_SECTIONS.forEach(function (selector) {
        const next = doc.querySelector(selector);
        if (!next || !source) return;
        const node = adopt(next);
        source.before(node);
        keep(node);
      });
      if (source) source.textContent = view.text;
      host.innerHTML = view.host.innerHTML;
      host.dataset.kgraphRender = view.render;
      if ("scrollTop" in host) host.scrollTop = 0;
      keep(host);
      const pill = document.querySelector("[data-kgraph-refresh-pill]");
      keep(swapPart(doc, "[data-kgraph-unresolved-count]", function (node) {
        if (pill) pill.before(node);
      }));
      keep(swapPart(doc, "[data-kgraph-provenance-group]", function (node) {
        if (hiddenCount) hiddenCount.after(node);
      }));
      if (window.htmx && typeof window.htmx.process === "function") {
        inserted.forEach(function (node) { window.htmx.process(node); });
      }
    }

    // An expansion's notes in view (D16): the note expanded and the notes
    // it brought, panned to in the usable area from where the view is (or
    // is going) — zoomed out only as far as they need and never below
    // MIN_READABLE_ZOOM, and in no further than that floor (an operator
    // zoomed out past it is brought back to it); centred on the note when
    // they still do not fit, the pan hint saying what is outside. Answers
    // whether it moved.
    function revealNodes(nodes, animate) {
      if (!nodes.length) return false;
      const base = inFlight ? inFlight.target : { zoom: cy.zoom(), pan: cy.pan() };
      const floor = Math.max(MIN_READABLE_ZOOM, cy.minZoom());
      const ceiling = Math.max(base.zoom, floor);
      makeRoom(ceiling);
      const box = nodes.boundingBox();
      const drawn = {
        x1: base.zoom * box.x1 + base.pan.x,
        x2: base.zoom * box.x2 + base.pan.x,
        y1: base.zoom * box.y1 + base.pan.y,
        y2: base.zoom * box.y2 + base.pan.y
      };
      if (base.zoom >= floor && drawn.x1 >= usable.x1 && drawn.x2 <= usable.x2 &&
          drawn.y1 >= usable.y1 && drawn.y2 <= usable.y2) return false;
      const roomW = Math.max(usable.x2 - usable.x1 - 2 * FIT_PADDING, 1);
      const roomH = Math.max(usable.y2 - usable.y1 - 2 * FIT_PADDING, 1);
      const zoom = Math.max(Math.min(ceiling, roomW / box.w, roomH / box.h), floor);
      let centre = centreOf(box);
      if (box.w * zoom > roomW || box.h * zoom > roomH) centre = centreOf(nodes[0].boundingBox());
      const middle = centreOf(usable);
      moveTo({
        zoom: zoom,
        pan: { x: middle.x - zoom * centre.x, y: middle.y - zoom * centre.y }
      }, animate);
      return true;
    }

    // The answer committed, in one synchronous step: text and panel, the
    // picture diffed by identity (survivors where they stand, the new
    // placed beside their anchors, the labels placed again), the controls,
    // key, search and counts — and only then the address pushed, the
    // selection lit from it and the view moved.
    function commit(view, request, built) {
      swapText(view);
      current = view.payload;
      panelInvoker = null;
      const added = applyElements(current, built);
      cy.nodes().addClass("widest");
      placeNew(current, added, cy.nodes().not(added));
      relabel();
      cy.nodes().removeClass("widest");
      bindProvenance();
      busyChanged();
      renderKey();
      applySearch();
      container.dataset.canvasNodes = String(cy.nodes().length);
      container.dataset.canvasEdges = String(cy.edges().length);
      // The whole view is in: only now is its address pushed.
      const target = new URL(request.href, window.location.href).href;
      if (withoutFragment(target) !== withoutFragment(window.location.href)) {
        window.history.pushState({ kgraph: true }, "", request.href);
      }
      syncRefresh();
      shownHref = window.location.href;
      lightFromUrl();
      const anchor = request.anchor ? byPid(cy.nodes(), request.anchor) : cy.collection();
      const brought = anchor.length
        ? anchor.union(added.filter(function (node) { return node.data("via") === request.anchor; }))
        : anchor;
      if (!revealNodes(brought, true)) revealSelection(true);
      showPanHint();
      // The link used is gone with the text it was in: the focus goes to
      // the panel the view came back with.
      const heading = within(host, "[data-kgraph-panel-heading]");
      if (heading && typeof heading.focus === "function") heading.focus({ preventScroll: true });
    }

    changeGraph = function (href, invoker, anchor) {
      if (!href) return false;
      // Inert while one is out, and once a Back or Forward is reloading.
      if (retired || graphRequest) return true;
      if (current.mode !== "focus" || typeof window.fetch !== "function" ||
          typeof window.DOMParser !== "function") return false;
      const target = new URL(href, window.location.href);
      const expands = invoker && typeof invoker.getAttribute === "function" &&
        invoker.getAttribute("data-kgraph-expand") !== null;
      const request = {
        href: target.pathname + target.search,
        controller: typeof window.AbortController === "function" ? new window.AbortController() : null,
        anchor: anchor || (expands ? queryValue(target.searchParams, "expand") : "")
      };
      abandonPending();
      graphRequest = request;
      hideStatus();
      busyChanged();
      fetchView(request).then(function (view) {
        if (graphRequest !== request) return;
        graphRequest = null;
        let built = null;
        try {
          built = elementsOf(view.payload);
        } catch (error) {
          built = null;
        }
        if (!built) {
          // Nothing on the page has changed: the view stays, with why.
          busyChanged();
          showStatus("the answer could not be read.", request.href);
          return;
        }
        try {
          commit(view, request, built);
        } catch (error) {
          // A commit that broke part way would leave a view no address
          // names: the address, not yet pushed, is drawn again whole.
          retired = true;
          window.location.reload();
        }
      }, function (error) {
        if (graphRequest !== request) return;
        graphRequest = null;
        busyChanged();
        showStatus((error && error.kgraphReason) || "the request did not complete.", request.href);
      });
      return true;
    };

    // An address spelling the presentation otherwise (` full `, a repeat,
    // `inline`) takes the server's spelling, so it and every link to it are
    // one address.
    const spelled = withCanvas(window.location.href, fullPage);
    if (spelled !== window.location.href && typeof window.history.replaceState === "function") {
      window.history.replaceState(window.history.state, "", spelled);
    }
    shownHref = window.location.href;
    container.dataset.canvasState = "ready";
    container.dataset.canvasNodes = String(cy.nodes().length);
    container.dataset.canvasEdges = String(cy.edges().length);

    container.dataset.canvasRequest = "idle";

    window.LithosLensKnowledgeGraph = {
      cy: cy,
      // The view on screen: each swap's (D16).
      get payload() { return current; },
      edgeColours: EDGE_COLOURS,
      resolvedColour: RESOLVED,
      nodePalette: NODE_PALETTE,
      nodeNeutral: NODE_NEUTRAL,
      edgeWidth: edgeWidth,
      blank: blank,
      minReadableZoom: MIN_READABLE_ZOOM,
      withCanvas: withCanvas,
      fullPage: function () { return fullPage; },
      busy: function () { return !!graphRequest; },
      usableArea: function () { return Object.assign({}, usable); }
    };
  }

  // Drawn once the label face is in: a label placed by a fallback's metrics
  // sits wrongly once the face arrives. Both weights the canvas draws — a
  // title, and a lit or matched one in bold. A face that cannot load still
  // draws, in the fallback.
  function drawOnceTheFaceIsIn() {
    const fonts = document.fonts;
    if (!fonts || typeof fonts.load !== "function") {
      draw();
      return;
    }
    const face = NODE_FONT + 'px "' + LABEL_FACE + '"';
    Promise.all([fonts.load(face), fonts.load("bold " + face)]).then(draw, draw);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", drawOnceTheFaceIsIn);
  } else {
    drawOnceTheFaceIsIn();
  }
  if (!window.LithosLensKnowledgeGraphBound) {
    window.LithosLensKnowledgeGraphBound = true;
    document.addEventListener("htmx:beforeRequest", trackPanel);
    document.addEventListener("htmx:beforeOnLoad", dropAbandoned);
    document.addEventListener("htmx:afterSwap", pushAfterSwap);
    document.addEventListener("keydown", onKeydown);
    document.addEventListener("click", onCloseClick);
    document.addEventListener("click", onGraphLinkClick);
    window.addEventListener("popstate", onTravel);
    listen();
  }
})();
