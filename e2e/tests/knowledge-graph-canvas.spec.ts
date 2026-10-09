import { test, expect, type Page } from "@playwright/test";

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
  });
});

async function canvasReady(page: Page) {
  await expect(
    page.locator('[data-kgraph-canvas][data-canvas-state="ready"]'),
  ).toBeVisible();
}

/** Click a node where the canvas draws it: a real pointer event. */
async function clickNode(page: Page, id: string) {
  await page.locator("[data-kgraph-canvas]").scrollIntoViewIfNeeded();
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

test("a later click wins over an earlier panel request still in flight", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);

  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(`**/knowledge/graph/panel?*selected=${CAPACITY}*`, async (route) => {
    await held;
    // The browser aborted it when the later click's request replaced it.
    await route.continue().catch(() => {});
  });

  await clickNode(page, CAPACITY);
  await clickNode(page, ROLLBACK);
  await nodePanel(page, ROLLBACK);
  release();
  // Give the held response every chance to land, then check it did not.
  await page.waitForTimeout(500);

  await nodePanel(page, ROLLBACK);
  expect(query(page)).toEqual({ focus: PLAN, selected: ROLLBACK });
  expect((await pushes(page)).map((p) => p.url)).toEqual([
    `/knowledge/graph?focus=${PLAN}&selected=${ROLLBACK}`,
  ]);
});

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

test("Back over a text panel link restores a live canvas and an honest search count", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await page.locator("[data-kgraph-search]").fill("capacity");
  await expect(page.locator("[data-kgraph-search-count]")).toHaveText("1 match");

  // A text edge link: htmx pushes it and snapshots this page first.
  await page.locator(`[data-kgraph-edge="edge_4c1e9a7b20d3"] .kgraph-edge-link`).click();
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
