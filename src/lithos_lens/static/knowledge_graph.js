/*
  The knowledge graph's canvas (K2 S4) — progressive enhancement over the
  text baseline `/knowledge/graph` renders, drawn from the SAME embedded
  payload so the picture and the text cannot disagree (D12).

  Not `graph.js`: the task graph's canvas is task semantics (layers, chains,
  cycles, claims) and is not generalised (PRD D15). The two share Cytoscape,
  the stylesheet's conventions and the "text first, canvas from the same
  payload" rule, and no code.

  Four rules shape everything below.

  1. THE CANVAS DRAWS EXACTLY THE PAYLOAD. Every node and every edge in it,
     nothing else. The server applied the weight and provenance filters
     before its cap (an edge they hide is counted, not shipped), so the canvas
     cannot lower a threshold on its own — and does not raise one either.

  2. EVERY CONTROL NAVIGATES. The min-weight slider (on `change`, not while
     dragging), the provenance toggles, the depth and the colour mode each
     rewrite one key of the page's own query and load it: the server
     re-renders text, payload and canvas together, so a reload reproduces
     the view and the text below always agrees with the picture.

  3. NO STYLE CLAIM IS DERIVED HERE. Direction, symmetry and resolution are
     the server's: each edge carries its `style` (class tokens, stroke,
     arrowhead, label) from `knowledge_edge_types.edge_style`, and a layer
     pair is told from a typed row by `kind`, never by `type`. What this file
     owns is the palette and the sizes: a colour per edge class, a fixed slot
     palette for the node colour mode that shares no colour with it, width
     from weight and size from degree in view.

  4. THE LAYOUT RUNS ONCE. Concentric by hop around the focus, or a
     force-directed `cose` for a scoped graph — once per page load. A click
     opens the S5 panel through htmx and lights the selection; it never moves
     a node.
*/
(function () {
  "use strict";

  const PANEL_PATH = "/knowledge/graph/panel";
  const RENDER_KEY = "render";
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

  // The values present take slots by node count, most first, ties by name
  // (S4 D7): the same graph colours the same way on every load.
  function colourSlots(nodes, mode) {
    const counts = dict();
    nodes.forEach(function (node) {
      const value = colourValue(node, mode);
      if (value !== null) counts[value] = (counts[value] || 0) + 1;
    });
    const values = Object.keys(counts).sort(function (a, b) {
      return counts[b] - counts[a] || (a < b ? -1 : a > b ? 1 : 0);
    });
    const slots = dict();
    values.slice(0, NODE_PALETTE.length).forEach(function (value, index) {
      slots[value] = NODE_PALETTE[index];
    });
    return { slots: slots, values: values, counts: counts };
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
    return params.toString();
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
  // - Any Back or Forward first ABANDONS the panel request in flight,
  //   whatever entry it lands on: the request is aborted, and should its
  //   response land anyway its swap is cancelled, so it can neither replace
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
    if (retired || !panelHost || detail.target !== panelHost) return;
    const page = pushUrlOf(detail.elt);
    pendingPush = page ? { page: page, xhr: detail.xhr, elt: detail.elt } : null;
  }

  function pushAfterSwap(event) {
    const detail = event.detail || {};
    if (retired || !pendingPush || detail.xhr !== pendingPush.xhr) return;
    if (detail.target !== panelHost || panelHost.isConnected === false) return;
    const page = pendingPush.page;
    pendingPush = null;
    const target = new URL(page, window.location.href).href;
    if (withoutFragment(target) !== withoutFragment(window.location.href)) {
      window.history.pushState({ kgraph: true }, "", page);
    }
    shownHref = window.location.href;
  }

  function dropAbandoned(event) {
    const detail = event.detail || {};
    if (detail.xhr && abandoned.indexOf(detail.xhr) !== -1) detail.shouldSwap = false;
  }

  function onTravel() {
    if (!panelHost) return;
    if (pendingPush) {
      abandoned.push(pendingPush.xhr);
      if (window.htmx && pendingPush.elt) {
        window.htmx.trigger(pendingPush.elt, "htmx:abort");
      }
      pendingPush = null;
    }
    if (withoutFragment(window.location.href) === withoutFragment(shownHref)) {
      // The entry's panel is the one on screen; the canvas lights it again.
      lightFromUrl();
      return;
    }
    retired = true;
    window.location.reload();
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
    const sizes = nodeSizes(payload.nodes);
    const colours = colourSlots(payload.nodes, mode);
    // Cytoscape's own element ids are opaque: a note id is any string, and
    // the bundle misbehaves on ids such as `__proto__`. The payload's id
    // travels as `pid`.
    const elementOf = dict();
    const nodeById = dict();
    const elements = [];
    payload.nodes.forEach(function (node, index) {
      const id = "n" + index;
      elementOf[node.id] = id;
      nodeById[node.id] = node;
      const value = colourValue(node, mode);
      const classes = [];
      if (node.focus) classes.push("focus");
      if (node.ghost) classes.push("ghost");
      if (node.facts_state === "pending") classes.push("pending");
      if (node.status === "archived") classes.push("archived");
      if (node.status === "quarantined") classes.push("quarantined");
      elements.push({
        group: "nodes",
        data: {
          id: id,
          pid: node.id,
          label: node.label,
          size: sizes[node.id],
          colour: (value !== null && colours.slots[value]) || NODE_NEUTRAL,
          hop: node.hop || 0
        },
        classes: classes.join(" ")
      });
    });
    const edgeById = dict();
    (payload.edges || []).forEach(function (edge, index) {
      const source = elementOf[edge.from];
      const target = elementOf[edge.to];
      if (!source || !target) return;
      edgeById[edge.id] = edge;
      const style = edge.style || {};
      const classes = String(style["class"] || "kedge-unknown").split(/\s+/).filter(Boolean);
      classes.push("stroke-" + (style.stroke || "solid"));
      if (style.arrowhead) classes.push("arrow");
      if (edge.partial) classes.push("partial");
      classes.push(edge.kind === "typed" ? "typed" : "layer");
      const unresolved = classes.indexOf("kedge-unresolved") !== -1;
      elements.push({
        group: "edges",
        data: {
          id: "e" + index,
          pid: edge.id,
          source: source,
          target: target,
          width: edgeWidth(edge.weight) + (unresolved ? EMPHASIS : 0),
          label: style.label || ""
        },
        classes: classes.join(" ")
      });
    });

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
          "font-size": 12,
          "font-family": "ui-sans-serif, system-ui, sans-serif",
          "text-valign": "bottom",
          "text-margin-y": 4,
          "text-wrap": "wrap",
          "text-max-width": 140
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
      {
        selector: "edge",
        style: {
          "curve-style": "bezier",
          width: "data(width)",
          "line-color": EDGE_COLOURS["kedge-unknown"],
          "target-arrow-color": EDGE_COLOURS["kedge-unknown"],
          "target-arrow-shape": "none",
          label: "data(label)",
          "font-size": 10,
          color: MUTED,
          "text-rotation": "autorotate",
          "text-background-color": PANEL,
          "text-background-opacity": 0.85,
          "text-background-padding": 2
        }
      },
      { selector: "edge.arrow", style: { "target-arrow-shape": "triangle" } },
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
      { selector: ".picked", style: { "overlay-color": INK, "overlay-opacity": 0.12, "overlay-padding": 5 } }
    );

    // Revealed before Cytoscape is built: it measures its container.
    container.hidden = false;
    if (container.replaceChildren) container.replaceChildren();
    const cy = window.cytoscape({
      container: container,
      elements: elements,
      style: style,
      // A drag pans; nothing moves once placed, and taps do not "select" in
      // Cytoscape's sense — the selection is the URL's.
      autoungrabify: true,
      autounselectify: true,
      boxSelectionEnabled: false,
      minZoom: 0.2,
      maxZoom: 3
    });

    // ── Layout: once ────────────────────────────────────────────────────
    if (payload.mode === "focus") {
      const maxHop = payload.nodes.reduce(function (high, node) {
        return Math.max(high, node.hop || 0);
      }, 0);
      cy.layout({
        name: "concentric",
        concentric: function (element) { return maxHop + 1 - element.data("hop"); },
        levelWidth: function () { return 1; },
        minNodeSpacing: 36,
        padding: FIT_PADDING,
        animate: false
      }).run();
    } else {
      // A circle first, so the force-directed pass starts from the same
      // place on every load and draws the same picture.
      cy.layout({ name: "circle", animate: false }).run();
      cy.layout({
        name: "cose",
        animate: false,
        randomize: false,
        padding: FIT_PADDING,
        nodeRepulsion: function () { return 9000; },
        idealEdgeLength: function () { return 90; }
      }).run();
    }
    cy.fit(undefined, FIT_PADDING);
    if (cy.zoom() > MAX_FIT_ZOOM) {
      cy.zoom(MAX_FIT_ZOOM);
      cy.center();
    }

    // ── Lit and dimmed: the selection and its neighbours ────────────────
    function light(selection) {
      cy.elements().removeClass("lit dimmed picked");
      if (!selection || !selection.length) return;
      const lit = selection.isNode()
        ? selection.closedNeighborhood()
        : selection.union(selection.connectedNodes());
      lit.addClass("lit");
      selection.addClass("picked");
      cy.elements().not(lit).addClass("dimmed");
    }

    function byPid(collection, pid) {
      return collection.filter(function (element) { return element.data("pid") === pid; });
    }

    // From the URL, on load and after a Back onto the same address: `edge=`
    // wins over `selected=`, as the parser reads them; `pin=` is never a
    // selection. An id the payload does not draw lights nothing.
    lightFromUrl = function () {
      const query = new URLSearchParams(window.location.search);
      const selectedEdge = query.get("edge") || "";
      const selectedNode = selectedEdge ? "" : query.get("selected") || "";
      if (selectedEdge) {
        light(byPid(cy.edges(".typed"), selectedEdge));
      } else if (selectedNode) {
        light(byPid(cy.nodes(), selectedNode));
      } else {
        light(null);
      }
    };
    lightFromUrl();

    // ── Clicks open the S5 panel ────────────────────────────────────────
    const host = document.querySelector("#kgraph-panel");
    const panelSource = document.querySelector("[data-kgraph-panel-source]");
    panelHost = host;
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

    cy.on("tap", "node", function (event) {
      const pid = event.target.data("pid");
      const now = new URLSearchParams(window.location.search);
      // A node opened from a view drawn for an edge pins that edge, as the
      // text's node links do (`knowledge_graph_url`), so the panel is drawn
      // from the very view on screen.
      const pin = now.get("edge") || now.get("pin") || null;
      light(event.target);
      openPanel({ selected: pid, edge: null, pin: pin });
    });
    cy.on("tap", "edge", function (event) {
      // Only a typed row has an edge panel; a layer pair is not one.
      if (!event.target.hasClass("typed")) return;
      light(event.target);
      openPanel({ edge: event.target.data("pid"), selected: null, pin: null });
    });
    cy.on("tap", function (event) {
      if (event.target === cy) light(null);
    });

    // ── The toolbar ─────────────────────────────────────────────────────
    const filters = payload.filters || {};
    const minWeight = typeof filters.min_weight === "number" ? filters.min_weight : WEIGHT_FLOOR;
    const groups = Array.isArray(filters.provenance) ? filters.provenance : PROVENANCE_GROUPS;

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
    if (hiddenCount) {
      const hidden = (payload.hidden && payload.hidden.by_weight) || 0;
      hiddenCount.textContent = plural(hidden, "edge", "edges") + " below " + weightText(minWeight) + " hidden";
    }

    const toggles = Array.prototype.slice.call(
      document.querySelectorAll("[data-kgraph-provenance]")
    );
    toggles.forEach(function (toggle) {
      toggle.checked = groups.indexOf(toggle.dataset.kgraphProvenance) !== -1;
    });
    const checked = toggles.filter(function (toggle) { return toggle.checked; });
    toggles.forEach(function (toggle) {
      // The last group on cannot be turned off: an empty list means "all".
      toggle.disabled = checked.length === 1 && toggle.checked;
      toggle.addEventListener("change", function () {
        const group = toggle.dataset.kgraphProvenance;
        const next = groups.filter(function (name) { return name !== group; });
        if (toggle.checked) next.push(group);
        navigate({ provenance: provenanceValue(next) });
      });
    });

    const wouldBe = payload.would_be_nodes || {};
    document.querySelectorAll("[data-kgraph-depth]").forEach(function (radio) {
      radio.checked = Number(radio.value) === payload.depth;
      radio.addEventListener("change", function () {
        if (radio.checked) navigate({ depth: radio.value });
      });
    });
    document.querySelectorAll("[data-kgraph-depth-count]").forEach(function (count) {
      const level = count.dataset.kgraphDepthCount;
      const notes = wouldBe[level];
      count.textContent = Number(level) !== payload.depth && typeof notes === "number"
        ? ": " + plural(notes, "note", "notes")
        : "";
    });

    document.querySelectorAll("[data-kgraph-colour]").forEach(function (radio) {
      radio.checked = radio.value === mode;
      radio.addEventListener("change", function () {
        if (radio.checked) navigate({ colour: radio.value === "type" ? "type" : null });
      });
    });

    const search = document.querySelector("[data-kgraph-search]");
    const searchCount = document.querySelector("[data-kgraph-search-count]");
    function applySearch() {
      const needle = String((search && search.value) || "").trim().toLowerCase();
      cy.nodes().removeClass("match");
      if (!needle) {
        if (searchCount) searchCount.textContent = "";
        return;
      }
      const hits = cy.nodes().filter(function (element) {
        return String(element.data("label") || "").toLowerCase().indexOf(needle) !== -1;
      });
      hits.addClass("match");
      if (searchCount) searchCount.textContent = plural(hits.length, "match", "matches");
    }
    if (search) search.addEventListener("input", applySearch);
    // From the field as it stands: a page the browser restored (a reload
    // after Back) may bring the field's value back with it.
    applySearch();

    // ── The canvas's key: the text legend's lines, the colours present ──
    const edgeKey = document.querySelector("[data-kgraph-key-edges]");
    if (edgeKey) {
      edgeKey.replaceChildren.apply(edgeKey, (payload.legend || []).map(function (line) {
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
      colours.values.forEach(function (value) {
        items.push(keyItem(swatch(colours.slots[value] || NODE_NEUTRAL), value, { keyValue: value }));
      });
      const unvalued = payload.nodes.some(function (node) {
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
      markKey.replaceChildren.apply(markKey, marks.map(function (mark) {
        return keyItem(null, mark[1], { keyMark: mark[0] });
      }));
    }

    const toolbar = document.querySelector("[data-kgraph-toolbar]");
    if (toolbar) toolbar.hidden = false;
    const key = document.querySelector("[data-kgraph-key]");
    if (key) key.hidden = false;

    shownHref = window.location.href;
    container.dataset.canvasState = "ready";
    container.dataset.canvasNodes = String(cy.nodes().length);
    container.dataset.canvasEdges = String(cy.edges().length);

    window.LithosLensKnowledgeGraph = {
      cy: cy,
      payload: payload,
      edgeColours: EDGE_COLOURS,
      resolvedColour: RESOLVED,
      nodePalette: NODE_PALETTE,
      nodeNeutral: NODE_NEUTRAL,
      edgeWidth: edgeWidth
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", draw);
  } else {
    draw();
  }
  if (!window.LithosLensKnowledgeGraphBound) {
    window.LithosLensKnowledgeGraphBound = true;
    document.addEventListener("htmx:beforeRequest", trackPanel);
    document.addEventListener("htmx:beforeSwap", dropAbandoned);
    document.addEventListener("htmx:afterSwap", pushAfterSwap);
    window.addEventListener("popstate", onTravel);
  }
})();
