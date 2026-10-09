import { test, expect, type Page } from "@playwright/test";
import { knowledgeCanvasLabelsAreReadable } from "./knowledge-graph-checks";

/**
 * The knowledge graph's canvas in a real browser (K2 S4): a click on the
 * picture opens the S5 panel through htmx and only then pushes the page URL;
 * a later click wins over an earlier one still in flight; a view drawn for an
 * exempted edge keeps its pin across clicks; a stale render id sends the
 * browser to the full page; Back and Forward land on a live canvas; and the
 * slider shows the threshold the server applied. The Node harness
 * (`tests/test_knowledge_graph_js.py`) pins the URL each click builds; this is
 * the same lifecycle against htmx, the history and the server for real.
 *
 * Read-only against the default fake server: nothing here writes.
 */

const PLAN = "note-influx-plan";
const CAPACITY = "note-influx-capacity";
const LEGACY = "note-influx-legacy-ingest";
const ROLLBACK = "note-influx-rollback";
const FAINT_EDGE = "edge_15d0c3e8f972";
const RESOLVED = "edge_b6e0f27d4c18";
const SUPPORTS = "edge_4c1e9a7b20d3";

test.use({ viewport: { width: 1440, height: 1000 } });

// Every history push, with whether the panel host held a fragment at that
// moment — the order D10 requires is swap first, then push.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const pushes: Array<{ url: string; panel: string }> = [];
    (window as any).__pushes = pushes;
    const push = history.pushState.bind(history);
    history.pushState = function (state: any, title: string, url?: string | URL | null) {
      const host = document.querySelector("#kgraph-panel");
      pushes.push({
        url: String(url),
        panel: host ? host.innerHTML.trim() : "",
      });
      return push(state, title, url);
    };
    // Every XHR the page ENDS (`loadend`: after its load handler's swap and
    // push, or its abort) — the barrier a race is read behind, rather than
    // an elapsed time.
    const ended: string[] = [];
    (window as any).__panelXhrEnded = ended;
    const open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (
      this: XMLHttpRequest,
      method: string,
      url: string | URL,
      ...rest: any[]
    ) {
      this.addEventListener("loadend", () => ended.push(String(url)));
      return (open as any).call(this, method, url, ...rest);
    } as typeof XMLHttpRequest.prototype.open;
  });
});

/** Hold the next panel request whose query names `marker`, until released. */
async function hold(page: Page, marker: string) {
  let intercepted!: () => void;
  let release!: () => void;
  const seen = new Promise<void>((resolve) => (intercepted = resolve));
  const released = new Promise<void>((resolve) => (release = resolve));
  let handled: Promise<void> = Promise.resolve();
  await page.route(
    (url) => url.pathname === "/knowledge/graph/panel" && url.search.includes(marker),
    (route) => {
      handled = (async () => {
        intercepted();
        await released;
        // Aborted in the page meanwhile: it can no longer be continued.
        await route.continue().catch(() => undefined);
      })();
      return handled;
    },
    { times: 1 },
  );
  const ended = () =>
    page.waitForFunction(
      (m) => ((window as any).__panelXhrEnded as string[]).some((u) => u.includes(m)),
      marker,
    );
  return { seen, release: () => release(), ended, handled: () => handled };
}

/** A text baseline edge link (`panel_attrs`) on the page. */
function textEdge(page: Page, id: string) {
  return page.locator(`[data-kgraph-edge="${id}"] .kgraph-edge-link`);
}

async function canvasReady(page: Page) {
  await expect(
    page.locator('[data-kgraph-canvas][data-canvas-state="ready"]'),
  ).toBeVisible();
}

/** Click a node where the canvas draws it: a real pointer event. */
async function clickNode(page: Page, id: string) {
  await page.locator("[data-kgraph-canvas]").scrollIntoViewIfNeeded();
  // Cytoscape re-reads its container's position on the `scroll` event,
  // which the browser fires on the next frame: a click before it lands
  // where the canvas WAS (a person cannot click that fast).
  await page.evaluate(
    () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))),
  );
  const point = await page.evaluate((pid) => {
    const cy = (window as any).LithosLensKnowledgeGraph.cy;
    const node = cy.nodes().filter((n: any) => n.data("pid") === pid)[0];
    const box = document.querySelector("[data-kgraph-canvas]")!.getBoundingClientRect();
    const at = node.renderedPosition();
    return { x: box.left + at.x, y: box.top + at.y };
  }, id);
  await page.mouse.click(point.x, point.y);
}

/** Tap an edge: its rendered midpoint may sit under another edge's curve. */
async function tapEdge(page: Page, id: string) {
  await page.evaluate((pid) => {
    const cy = (window as any).LithosLensKnowledgeGraph.cy;
    cy.edges().filter((e: any) => e.data("pid") === pid).emit("tap");
  }, id);
}

async function nodePanel(page: Page, id: string) {
  await expect(
    page.locator(`#kgraph-panel [data-kgraph-panel="node"][data-kgraph-panel-id="${id}"]`),
  ).toBeVisible();
}

function query(page: Page): Record<string, string> {
  return Object.fromEntries(new URL(page.url()).searchParams);
}

async function pushes(page: Page): Promise<Array<{ url: string; panel: string }>> {
  return page.evaluate(() => (window as any).__pushes);
}

async function markDocument(page: Page) {
  await page.evaluate(() => {
    (window as any).__sameDocument = true;
  });
}

async function sameDocument(page: Page): Promise<boolean> {
  return page.evaluate(() => (window as any).__sameDocument === true);
}

test("a node click swaps its panel in, then pushes its URL, in the same document", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await markDocument(page);

  await clickNode(page, CAPACITY);

  await nodePanel(page, CAPACITY);
  await expect.poll(() => query(page)).toEqual({ focus: PLAN, selected: CAPACITY });
  const pushed = await pushes(page);
  expect(pushed).toHaveLength(1);
  expect(pushed[0].url).toBe(`/knowledge/graph?focus=${PLAN}&selected=${CAPACITY}`);
  // The fragment was already in the host when the URL moved.
  expect(pushed[0].panel).toContain(`data-kgraph-panel-id="${CAPACITY}"`);
  expect(await sameDocument(page)).toBe(true);
});

test("a typed edge click opens its edge panel", async ({ page }) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}&selected=${LEGACY}`);
  await canvasReady(page);

  await tapEdge(page, RESOLVED);

  await expect(
    page.locator(`#kgraph-panel [data-kgraph-panel="edge"][data-kgraph-panel-id="${RESOLVED}"]`),
  ).toBeVisible();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN, edge: RESOLVED });
});

test("on a view an exempted edge drew, node after node keeps the pin and the view", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}&edge=${FAINT_EDGE}`);
  await canvasReady(page);
  await markDocument(page);

  await clickNode(page, LEGACY);
  await nodePanel(page, LEGACY);
  await expect
    .poll(() => query(page))
    .toEqual({ focus: PLAN, selected: LEGACY, pin: FAINT_EDGE });

  await clickNode(page, CAPACITY);
  await nodePanel(page, CAPACITY);
  await expect
    .poll(() => query(page))
    .toEqual({ focus: PLAN, selected: CAPACITY, pin: FAINT_EDGE });
  // No HX-Redirect: both fragments came from the view on screen.
  expect(await sameDocument(page)).toBe(true);
  expect((await pushes(page)).map((p) => new URL(p.url, page.url()).search)).toEqual([
    `?focus=${PLAN}&selected=${LEGACY}&pin=${FAINT_EDGE}`,
    `?focus=${PLAN}&selected=${CAPACITY}&pin=${FAINT_EDGE}`,
  ]);
});

for (const race of [
  {
    name: "canvas, then canvas",
    held: `selected=${CAPACITY}`,
    first: (page: Page) => clickNode(page, CAPACITY),
    second: (page: Page) => clickNode(page, ROLLBACK),
    winner: { selected: ROLLBACK },
  },
  {
    name: "canvas, then a text link",
    held: `selected=${CAPACITY}`,
    first: (page: Page) => clickNode(page, CAPACITY),
    second: (page: Page) => textEdge(page, SUPPORTS).click(),
    winner: { edge: SUPPORTS },
  },
  {
    name: "a text link, then canvas",
    held: `edge=${SUPPORTS}`,
    first: (page: Page) => textEdge(page, SUPPORTS).click(),
    second: (page: Page) => clickNode(page, CAPACITY),
    winner: { selected: CAPACITY },
  },
]) {
  test(`a later click wins over an earlier panel request still in flight (${race.name})`, async ({
    page,
  }) => {
    await page.goto(`/knowledge/graph?focus=${PLAN}`);
    await canvasReady(page);
    const held = await hold(page, race.held);

    await race.first(page);
    await held.seen; // the earlier request is in flight, held
    await race.second(page);
    const [kind, id] = Object.entries(race.winner)[0];
    const panel = page.locator(`#kgraph-panel [data-kgraph-panel="${kind === "edge" ? "edge" : "node"}"]`);
    await expect(panel).toHaveAttribute("data-kgraph-panel-id", id);
    held.release();
    await held.ended(); // the earlier XHR is over: loaded and handled, or aborted
    await held.handled();

    await expect(panel).toHaveAttribute("data-kgraph-panel-id", id);
    expect(query(page)).toEqual({ focus: PLAN, ...race.winner });
    const winnerUrl = `/knowledge/graph?${new URLSearchParams({ focus: PLAN, ...race.winner })}`;
    expect((await pushes(page)).map((p) => p.url)).toEqual([winnerUrl]);
  });
}

test("a render id no longer held sends the browser to the full page", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await markDocument(page);
  await page.evaluate(() => {
    document.querySelector("#kgraph-panel")!.setAttribute("data-kgraph-render", "gone");
  });

  await clickNode(page, CAPACITY);

  await expect.poll(() => sameDocument(page)).toBe(false);
  await expect.poll(() => query(page)).toEqual({ focus: PLAN, selected: CAPACITY });
  await nodePanel(page, CAPACITY);
  await canvasReady(page);
});

test("Back and Forward over canvas clicks land on a live canvas with that URL's panel", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await clickNode(page, CAPACITY);
  await nodePanel(page, CAPACITY);
  await clickNode(page, ROLLBACK);
  await nodePanel(page, ROLLBACK);

  await page.goBack();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN, selected: CAPACITY });
  await nodePanel(page, CAPACITY);
  await canvasReady(page);

  await page.goBack();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN });
  await canvasReady(page);
  await expect(page.locator("#kgraph-panel [data-kgraph-panel]")).toHaveCount(0);

  await page.goForward();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN, selected: CAPACITY });
  await nodePanel(page, CAPACITY);
  await canvasReady(page);
});

test("Back over a text panel link reloads to a live canvas and an honest search count", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await page.locator("[data-kgraph-search]").fill("capacity");
  await expect(page.locator("[data-kgraph-search-count]")).toHaveText("1 match");

  // A text edge link: pushed after its swap, as a canvas click is.
  await textEdge(page, SUPPORTS).click();
  await expect(page.locator('#kgraph-panel [data-kgraph-panel="edge"]')).toBeVisible();

  await page.goBack();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN });
  await canvasReady(page);
  const live = await page.evaluate(() => {
    const graph = (window as any).LithosLensKnowledgeGraph;
    return {
      sameContainer: graph.cy.container() === document.querySelector("[data-kgraph-canvas]"),
      nodes: graph.cy.nodes().length,
      field: (document.querySelector("[data-kgraph-search]") as HTMLInputElement).value,
      count: document.querySelector("[data-kgraph-search-count]")!.textContent,
      matched: graph.cy.nodes(".match").length,
    };
  });
  expect(live.sameContainer).toBe(true);
  expect(live.nodes).toBeGreaterThan(0);
  // The count agrees with the field and the highlights, whatever was restored.
  expect(live.count).toBe(live.field ? `${live.matched} match${live.matched === 1 ? "" : "es"}` : "");
});

test("the slider shows a threshold off its grid as the server applied it", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}&min_weight=0.123`);
  await canvasReady(page);

  await expect(page.locator("[data-kgraph-min-weight]")).toHaveValue("0.123");
  await expect(page.locator("[data-kgraph-min-weight-value]")).toHaveText("0.123");
  await expect(page.locator("[data-kgraph-weight-hidden]")).toContainText("below 0.123 hidden");

  // Moved, it is back on the 0.05 grid, and its release navigates there.
  await page.locator("[data-kgraph-min-weight]").focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => query(page).min_weight).not.toBe("0.123");
  const moved = Number(query(page).min_weight);
  expect(Math.abs(moved * 20 - Math.round(moved * 20))).toBeLessThan(1e-9);
  await canvasReady(page);
  await expect(page.locator("[data-kgraph-min-weight]")).toHaveValue(String(moved));
});

test("Back over text, canvas and text entries shows each URL's own panel", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  const edgePanel = (id: string) =>
    expect(
      page.locator(`#kgraph-panel [data-kgraph-panel="edge"][data-kgraph-panel-id="${id}"]`),
    ).toBeVisible();

  await textEdge(page, SUPPORTS).click();
  await edgePanel(SUPPORTS);
  await clickNode(page, CAPACITY);
  await nodePanel(page, CAPACITY);
  await expect
    .poll(() => query(page))
    .toEqual({ focus: PLAN, selected: CAPACITY, pin: SUPPORTS });
  await textEdge(page, RESOLVED).click();
  await edgePanel(RESOLVED);

  await page.goBack();
  await expect
    .poll(() => query(page))
    .toEqual({ focus: PLAN, selected: CAPACITY, pin: SUPPORTS });
  await nodePanel(page, CAPACITY);
  await canvasReady(page);

  await page.goBack();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN, edge: SUPPORTS });
  await edgePanel(SUPPORTS);
  await expect(page.locator('#kgraph-panel [data-kgraph-panel="node"]')).toHaveCount(0);
  await canvasReady(page);
});

test("Back while a canvas panel request is in flight is not undone by its response", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await textEdge(page, SUPPORTS).click();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN, edge: SUPPORTS });
  const held = await hold(page, `selected=${CAPACITY}`);

  await clickNode(page, CAPACITY);
  await held.seen;
  await page.goBack();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN });
  await canvasReady(page);
  held.release();
  await held.handled();

  // The Back stands: the address, the empty host, and no push since the reload.
  await expect.poll(() => query(page)).toEqual({ focus: PLAN });
  await expect(page.locator("#kgraph-panel [data-kgraph-panel]")).toHaveCount(0);
  expect(await pushes(page)).toEqual([]);
});

for (const weight of ["0.10000000001", "0.09999999999", "0.99999999999"]) {
  test(`the slider keeps ${weight} as applied, not its grid neighbour`, async ({ page }) => {
    await page.goto(`/knowledge/graph?focus=${PLAN}&min_weight=${weight}`);
    await canvasReady(page);

    const slider = page.locator("[data-kgraph-min-weight]");
    expect(Number(await slider.inputValue())).toBe(Number(weight));
    await expect(page.locator("[data-kgraph-min-weight-value]")).toHaveText(weight);
    await expect(page.locator("[data-kgraph-weight-hidden]")).toContainText(
      `below ${weight} hidden`,
    );
  });
}

test("re-selecting a node then Back with another request in flight keeps the Back", async ({
  page,
}) => {
  // The reviewer's sequence (round 3, f-007): capacity twice — one entry,
  // not two — a rollback request held in flight, Back, then its release.
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await clickNode(page, CAPACITY);
  await nodePanel(page, CAPACITY);
  await clickNode(page, CAPACITY);
  await nodePanel(page, CAPACITY);
  expect((await pushes(page)).map((p) => p.url)).toEqual([
    `/knowledge/graph?focus=${PLAN}&selected=${CAPACITY}`,
  ]);
  const held = await hold(page, `selected=${ROLLBACK}`);

  await clickNode(page, ROLLBACK);
  await held.seen;
  await page.goBack();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN });
  await canvasReady(page);
  held.release();
  await held.handled();

  await expect.poll(() => query(page)).toEqual({ focus: PLAN });
  await expect(page.locator("#kgraph-panel [data-kgraph-panel]")).toHaveCount(0);
  expect(await pushes(page)).toEqual([]);
});

test("Back onto an entry with the same address still abandons the request in flight", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await clickNode(page, CAPACITY);
  await nodePanel(page, CAPACITY);
  await markDocument(page);
  // A same-document `#` entry: Back from it lands on the capacity address.
  await page.evaluate(() => {
    location.hash = "top";
  });
  await expect.poll(() => new URL(page.url()).hash).toBe("#top");
  const held = await hold(page, `selected=${ROLLBACK}`);

  await clickNode(page, ROLLBACK);
  await held.seen;
  await page.goBack();
  await expect.poll(() => new URL(page.url()).hash).toBe("");
  held.release();
  await held.ended(); // the abandoned XHR is over: aborted, or landed and dropped
  await held.handled();

  expect(query(page)).toEqual({ focus: PLAN, selected: CAPACITY });
  await nodePanel(page, CAPACITY);
  expect((await pushes(page)).map((p) => p.url)).toEqual([
    `/knowledge/graph?focus=${PLAN}&selected=${CAPACITY}`,
  ]);
  // No reload was needed: the capacity panel already is this entry's.
  expect(await sameDocument(page)).toBe(true);
  const lit = await page.evaluate(() =>
    (window as any).LithosLensKnowledgeGraph.cy
      .nodes(".picked")
      .map((n: any) => n.data("pid")),
  );
  expect(lit).toEqual([CAPACITY]);
});

test("Back onto the same address drops an abandoned request's HX-Redirect", async ({
  page,
}) => {
  // The reviewer's sequence (round 4, f-007): on the view an exempted edge
  // drew, capacity selected (pinned); a `#` entry; a supports-edge request
  // held in flight — the server answers it with HX-Redirect, since the
  // edge's own view draws without the pin — then Back, then its release.
  await page.goto(`/knowledge/graph?focus=${PLAN}&edge=${FAINT_EDGE}`);
  await canvasReady(page);
  await clickNode(page, CAPACITY);
  await nodePanel(page, CAPACITY);
  await markDocument(page);
  await page.evaluate(() => {
    location.hash = "top";
  });
  await expect.poll(() => new URL(page.url()).hash).toBe("#top");
  const held = await hold(page, `edge=${SUPPORTS}`);

  await tapEdge(page, SUPPORTS);
  await held.seen;
  await page.goBack();
  await expect.poll(() => new URL(page.url()).hash).toBe("");
  held.release();
  await held.ended(); // aborted, or landed and stopped before htmx read it
  await held.handled();

  expect(await sameDocument(page)).toBe(true);
  expect(query(page)).toEqual({ focus: PLAN, selected: CAPACITY, pin: FAINT_EDGE });
  await nodePanel(page, CAPACITY);
  expect((await pushes(page)).map((p) => new URL(p.url, page.url()).search)).toEqual([
    `?focus=${PLAN}&selected=${CAPACITY}&pin=${FAINT_EDGE}`,
  ]);
});

test("the canvas lights the selection the server reads from a repeated or blank key", async ({
  page,
}) => {
  await page.goto(
    `/knowledge/graph?focus=${PLAN}&selected=${CAPACITY}&selected=${ROLLBACK}&edge=%20`,
  );
  await canvasReady(page);
  await nodePanel(page, ROLLBACK);
  const picked = await page.evaluate(() =>
    (window as any).LithosLensKnowledgeGraph.cy
      .elements(".picked")
      .map((e: any) => e.data("pid")),
  );
  expect(picked).toEqual([ROLLBACK]);
});

for (const [blank, picked] of [
  ["%C2%85", [CAPACITY]], // blank to the server: `selected` stands
  ["%EF%BB%BF", []], // not blank to the server: an `edge=` it does not draw
] as const) {
  test(`a selection beside edge=${blank} lights what the server selects`, async ({ page }) => {
    await page.goto(`/knowledge/graph?focus=${PLAN}&selected=${CAPACITY}&edge=${blank}`);
    await canvasReady(page);
    const lit = await page.evaluate(() =>
      (window as any).LithosLensKnowledgeGraph.cy
        .elements(".picked")
        .map((e: any) => e.data("pid")),
    );
    expect(lit).toEqual([...picked]);
    await expect(
      page.locator('#kgraph-panel [data-kgraph-panel="node"]'),
    ).toHaveCount(picked.length);
  });
}


/**
 * Serve `/knowledge/graph?focus=…&depth=2` with two more `assesses`-like
 * rows beside the fixture's capacity → plan `assesses`: distinct unknown
 * types (`measures`, `estimates`), so three parallel edges each carry a raw
 * type label — ordinary data under Lithos's UNIQUE(from, to, type,
 * namespace). The canvas draws from the embedded payload, which is what is
 * extended here; the rows are copies of the fixture's own canonical one.
 */
async function withParallelLabels(page: Page) {
  await page.route(
    (url) => url.pathname === "/knowledge/graph" && url.search.includes("depth=2"),
    async (route) => {
      const response = await route.fetch();
      const html = await response.text();
      const marker = /(<script type="application\/json" data-knowledge-graph-payload>)(.*?)(<\/script>)/s;
      const found = html.match(marker)!;
      const payload = JSON.parse(found[2]);
      const assesses = payload.edges.find((edge: any) => edge.id === "edge_f29d84a6130c");
      ["measures", "estimates"].forEach((type, index) => {
        payload.edges.push({
          ...assesses,
          id: `edge_00000000000${index}`,
          type,
          style: { ...assesses.style, label: type },
        });
      });
      const body = html.replace(
        marker,
        (_, open, _old, close) => open + JSON.stringify(payload).replace(/</g, "\\u003c") + close,
      );
      await route.fulfill({ response, body });
    },
  );
}

for (const width of [320, 768, 1440]) {
  test(`parallel edges' raw-type labels each get their own place at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 800 });
    await withParallelLabels(page);
    await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2`);
    await canvasReady(page);
    const labels = await page.evaluate(() =>
      (window as any).LithosLensKnowledgeGraph.cy
        .edges()
        .filter((edge: any) => edge.data("label"))
        .map((edge: any) => edge.data("label"))
        .sort(),
    );
    expect(labels).toEqual(["assesses", "estimates", "measures", "superseded"]);
    await knowledgeCanvasLabelsAreReadable(page);
  });
}

test("the pan hint follows the view as the operator zooms", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2`);
  await canvasReady(page);
  const hint = page.locator("[data-kgraph-pan-hint]");
  await expect(hint).toBeVisible(); // opened at a readable zoom: part is outside

  // Zoomed out until the whole graph fits: the hint goes…
  await page.evaluate(() => (window as any).LithosLensKnowledgeGraph.cy.fit(undefined, 10));
  await expect(hint).toBeHidden();
  // …and zoomed back in past the edges, it returns.
  await page.evaluate(() => {
    const cy = (window as any).LithosLensKnowledgeGraph.cy;
    cy.zoom({ level: 3, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  });
  await expect(hint).toBeVisible();
  // A pan that brings it all back into view (after fitting again) hides it.
  await page.evaluate(() => (window as any).LithosLensKnowledgeGraph.cy.fit(undefined, 10));
  await expect(hint).toBeHidden();
});
