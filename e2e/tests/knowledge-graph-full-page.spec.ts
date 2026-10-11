import { test, expect, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

/**
 * The knowledge graph's full-page canvas in a real browser (K2 S10, PRD D17).
 *
 * "Full page" / "Exit full page" switches in place: the address and every
 * graph link on the page take the mode, the canvas is resized and refitted
 * and no node moves; the panel is a drawer at 768px and wider and a bottom
 * sheet below, closed by Esc or its Close with the focus returned; fits and
 * pans keep the selection out from under the toolbar, the key and the panel,
 * never below the readable zoom, and respect reduced motion. Captures at
 * 1440, 768 and 390px go to `e2e/artifacts/knowledge-graph-full-*`. The Node
 * harness (`tests/test_knowledge_graph_js.py`) pins the URLs the toggle
 * writes; `tests/test_knowledge_graph_full_page.py` the server's half.
 *
 * S9 (expansion in place) has not landed: when it does, it owns the combined
 * expansion/full-page checks (PRD slice 9).
 *
 * Read-only against the default fake server: nothing here writes.
 */

const PLAN = "note-influx-plan";
const CAPACITY = "note-influx-capacity";
const LEGACY = "note-influx-legacy-ingest";
const ROLLBACK = "note-influx-rollback";
const FAINT_EDGE = "edge_15d0c3e8f972";
const SUPPORTS = "edge_4c1e9a7b20d3";

const ARTIFACTS_DIR = path.resolve(__dirname, "..", "artifacts");

test.use({ viewport: { width: 1440, height: 900 } });

// Every history push, and every XHR the page ends — the barrier a race is
// read behind (as `knowledge-graph-canvas.spec.ts` records them).
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const pushes: string[] = [];
    (window as any).__pushes = pushes;
    const push = history.pushState.bind(history);
    history.pushState = function (state: any, title: string, url?: string | URL | null) {
      pushes.push(String(url));
      return push(state, title, url);
    };
    // Every EventSource the page opens, so a test can deliver a frame.
    const sources: EventSource[] = [];
    (window as any).__sources = sources;
    const Native = window.EventSource;
    (window as any).EventSource = class extends Native {
      constructor(url: string | URL, init?: EventSourceInit) {
        super(url, init);
        sources.push(this);
      }
    };
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

async function canvasReady(page: Page) {
  await expect(page.locator('[data-kgraph-canvas][data-canvas-state="ready"]')).toBeVisible();
}

/** The viewport at rest: no refit or pan still moving. */
async function settled(page: Page) {
  await page.waitForFunction(() => {
    const graph = (window as any).LithosLensKnowledgeGraph;
    const canvas = document.querySelector("[data-kgraph-canvas]") as HTMLElement;
    return graph && !graph.cy.animated() && canvas.dataset.canvasMotion !== "moving";
  });
}

/**
 * The layout settled: two frames for the overlays' resize observation to
 * run its re-measure, then no refit or pan still moving.
 */
async function settledLayout(page: Page) {
  await page.evaluate(
    () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))),
  );
  await settled(page);
}

async function isFull(page: Page): Promise<boolean> {
  return page.evaluate(() => document.body.classList.contains("kgraph-full"));
}

function query(page: Page): Record<string, string> {
  return Object.fromEntries(new URL(page.url()).searchParams);
}

async function pushes(page: Page): Promise<string[]> {
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

async function positions(page: Page) {
  return page.evaluate(() =>
    (window as any).LithosLensKnowledgeGraph.cy
      .nodes()
      .map((n: any) => [n.data("pid"), n.position()]),
  );
}

async function toggle(page: Page) {
  await page.locator("[data-kgraph-full-toggle]").click();
  await settled(page);
}

/** Click a node where the canvas draws it: a real pointer event. */
async function clickNode(page: Page, id: string) {
  await page.locator("[data-kgraph-canvas]").scrollIntoViewIfNeeded();
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

async function nodePanel(page: Page, id: string) {
  await expect(
    page.locator(`#kgraph-panel [data-kgraph-panel="node"][data-kgraph-panel-id="${id}"]`),
  ).toBeVisible();
}

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

/** Every panel request the page makes, and whether its answer redirected. */
function panelTraffic(page: Page) {
  const requests: string[] = [];
  const redirects: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/knowledge/graph/panel") requests.push(request.url());
  });
  page.on("response", (response) => {
    if (response.headers()["hx-redirect"]) redirects.push(response.url());
  });
  return { requests, redirects };
}

type Box = { left: number; top: number; right: number; bottom: number };

/**
 * Where things are, in page pixels: the canvas, what lies over it, the
 * selection's drawn box (title included), and the script's usable area.
 */
async function geometry(page: Page) {
  return page.evaluate(() => {
    const graph = (window as any).LithosLensKnowledgeGraph;
    const cy = graph.cy;
    const rect = (selector: string): Box | null => {
      const el = document.querySelector(selector) as HTMLElement | null;
      if (!el || el.hidden || el.offsetParent === null) return null;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return null;
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
    };
    const canvas = document.querySelector("[data-kgraph-canvas]")!.getBoundingClientRect();
    const picked = cy.elements(".picked");
    let selection: Box | null = null;
    if (picked.length) {
      const b = picked.renderedBoundingBox();
      selection = {
        left: canvas.left + b.x1,
        top: canvas.top + b.y1,
        right: canvas.left + b.x2,
        bottom: canvas.top + b.y2,
      };
    }
    const drawn = cy.elements().renderedBoundingBox();
    const usable = graph.usableArea();
    const hint = document.querySelector("[data-kgraph-pan-hint]") as HTMLElement;
    return {
      zoom: cy.zoom(),
      minReadable: graph.minReadableZoom,
      canvas: { left: canvas.left, top: canvas.top, right: canvas.right, bottom: canvas.bottom },
      toolbar: rect("[data-kgraph-toolbar]"),
      key: rect("[data-kgraph-key]"),
      panel: rect("#kgraph-panel"),
      selection,
      usable,
      clipped:
        drawn.x1 < usable.x1 - 1 ||
        drawn.y1 < usable.y1 - 1 ||
        drawn.x2 > usable.x2 + 1 ||
        drawn.y2 > usable.y2 + 1,
      hinted: !!hint && !hint.hidden,
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    };
  });
}

function overlaps(a: Box, b: Box | null): boolean {
  if (!b) return false;
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

/** The selection is drawn on the canvas and under none of its overlays. */
function unobscured(g: Awaited<ReturnType<typeof geometry>>) {
  expect(g.selection).not.toBeNull();
  const s = g.selection!;
  expect(s.left).toBeGreaterThanOrEqual(g.canvas.left - 1);
  expect(s.right).toBeLessThanOrEqual(g.canvas.right + 1);
  expect(s.top).toBeGreaterThanOrEqual(g.canvas.top - 1);
  expect(s.bottom).toBeLessThanOrEqual(g.canvas.bottom + 1);
  expect(overlaps(s, g.toolbar)).toBe(false);
  expect(overlaps(s, g.key)).toBe(false);
  expect(overlaps(s, g.panel)).toBe(false);
}

async function capture(page: Page, name: string, fullPage: boolean) {
  await page.evaluate(() => (document as any).fonts.ready);
  await page.evaluate(
    () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))),
  );
  fs.mkdirSync(ARTIFACTS_DIR, { recursive: true });
  const file = path.join(ARTIFACTS_DIR, `${name}.png`);
  await page.screenshot({ path: file, fullPage });
  expect(fs.statSync(file).size).toBeGreaterThan(0);
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(page.viewportSize()!.width);
}

// ── The toggle ─────────────────────────────────────────────────────────

test("the toggle switches in place: canvas=full pushed, no node moved, the stage under the header", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2`);
  await canvasReady(page);
  await markDocument(page);
  const before = await positions(page);
  const inlineHeight = (await page.locator("[data-kgraph-canvas]").boundingBox())!.height;

  await toggle(page);

  expect(query(page)).toEqual({ focus: PLAN, depth: "2", canvas: "full" });
  expect((await pushes(page)).map((u) => new URL(u, page.url()).search)).toEqual([
    `?focus=${PLAN}&depth=2&canvas=full`,
  ]);
  expect(await isFull(page)).toBe(true);
  expect(await sameDocument(page)).toBe(true);
  expect(await positions(page)).toEqual(before);
  await expect(page.locator("[data-kgraph-full-toggle]")).toHaveText("Exit full page");
  await expect(page.locator("[data-kgraph-full-toggle]")).toHaveAttribute("aria-pressed", "true");
  const frame = await page.evaluate(() => {
    const header = document.querySelector("header.shell-header")!.getBoundingClientRect();
    const stage = document.querySelector("[data-kgraph-stage]")!.getBoundingClientRect();
    const order = Array.from(document.querySelector("[data-knowledge-graph]")!.children)
      .slice(0, 3)
      .map((el) => (el.matches("[data-kgraph-stage]") ? "stage" : el.matches("header") ? "heading" : el.matches("[data-kgraph-scope]") ? "scope" : el.tagName));
    return {
      headerBottom: header.bottom,
      stageTop: stage.top,
      stageHeight: stage.height,
      stageWidth: stage.width,
      innerHeight: window.innerHeight,
      clientWidth: document.documentElement.clientWidth,
      order,
    };
  });
  expect(Math.abs(frame.stageTop - frame.headerBottom)).toBeLessThan(1);
  expect(Math.abs(frame.stageHeight - (frame.innerHeight - frame.headerBottom))).toBeLessThan(1);
  expect(frame.stageWidth).toBe(frame.clientWidth);
  expect(frame.order).toEqual(["stage", "heading", "scope"]);
  const fullHeight = (await page.locator("[data-kgraph-canvas]").boundingBox())!.height;
  expect(fullHeight).toBeGreaterThan(inlineHeight);
  const g = await geometry(page);
  expect(g.zoom).toBeGreaterThanOrEqual(g.minReadable);
  expect(g.hinted).toBe(g.clipped);

  await toggle(page);

  expect(query(page)).toEqual({ focus: PLAN, depth: "2" });
  expect(await isFull(page)).toBe(false);
  expect(await positions(page)).toEqual(before);
  expect(await sameDocument(page)).toBe(true);
  const order = await page.evaluate(() =>
    Array.from(document.querySelector("[data-knowledge-graph]")!.children)
      .slice(0, 3)
      .map((el) => el.tagName + (el.matches("[data-kgraph-stage]") ? ".stage" : "")),
  );
  expect(order).toEqual(["HEADER", "SECTION", "DIV.stage"]);
  await expect(page.locator("[data-kgraph-canvas]")).toHaveCSS("height", `${inlineHeight}px`);
});

test("a canvas=full load lays out for the full-size canvas", async ({ page }) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&canvas=full`);
  await canvasReady(page);

  expect(await isFull(page)).toBe(true);
  const size = await page.evaluate(() => {
    const cy = (window as any).LithosLensKnowledgeGraph.cy;
    const stage = document.querySelector("[data-kgraph-stage]")!.getBoundingClientRect();
    return { width: cy.width(), height: cy.height(), stage: stage.height };
  });
  expect(size.width).toBe(1440);
  expect(size.height).toBeGreaterThanOrEqual(size.stage - 2);
  const g = await geometry(page);
  expect(g.zoom).toBeGreaterThanOrEqual(g.minReadable);
  expect(g.hinted).toBe(g.clipped);
});

test("open a panel, toggle, open another: the same render id, no reload, no read", async ({
  page,
}) => {
  const traffic = panelTraffic(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await markDocument(page);
  const render = await page.locator("#kgraph-panel").getAttribute("data-kgraph-render");

  await clickNode(page, CAPACITY);
  await nodePanel(page, CAPACITY);
  await toggle(page);
  expect(query(page)).toEqual({ focus: PLAN, selected: CAPACITY, canvas: "full" });
  // The drawer: the panel as it was, over the canvas's right side.
  await nodePanel(page, CAPACITY);
  await clickNode(page, ROLLBACK);
  await nodePanel(page, ROLLBACK);

  expect(query(page)).toEqual({ focus: PLAN, selected: ROLLBACK, canvas: "full" });
  expect(await sameDocument(page)).toBe(true);
  expect(traffic.redirects).toEqual([]);
  const asked = traffic.requests.map((url) => Object.fromEntries(new URL(url).searchParams));
  expect(asked).toEqual([
    { focus: PLAN, selected: CAPACITY, render },
    { focus: PLAN, selected: ROLLBACK, canvas: "full", render },
  ]);
});

test("after a toggle the page's own links, panels and forms keep the new mode", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await toggle(page);

  // Every graph address the server wrote on the page now carries it.
  const stale = await page.evaluate(() =>
    Array.from(
      document.querySelectorAll("[data-knowledge-graph] [href^='/knowledge/graph'], [data-knowledge-graph] [hx-get]"),
    )
      .flatMap((el) => ["href", "hx-get", "hx-push-url", "data-kgraph-push-url"].map((a) => el.getAttribute(a)))
      .filter((v): v is string => !!v && v.startsWith("/knowledge/graph") && !v.includes("canvas=full")),
  );
  expect(stale).toEqual([]);

  // A text edge link: its fragment and the URL pushed after it.
  await page.locator(`[data-kgraph-edge="${SUPPORTS}"] .kgraph-edge-link`).click();
  await expect(page.locator('#kgraph-panel [data-kgraph-panel="edge"]')).toBeVisible();
  expect(query(page)).toEqual({ focus: PLAN, edge: SUPPORTS, canvas: "full" });
  // A link inside that fragment: Node details, pinned.
  await page.locator(`#kgraph-panel [data-kgraph-card="${CAPACITY}"] [data-kgraph-node-details]`).click();
  await nodePanel(page, CAPACITY);
  expect(query(page)).toEqual({ focus: PLAN, selected: CAPACITY, pin: SUPPORTS, canvas: "full" });
  // The refresh pill follows the address.
  await expect(page.locator("[data-kgraph-refresh-pill]")).toHaveAttribute("href", page.url());
  // A toolbar control navigates with it, and the page it loads is full page.
  await page.locator('[data-kgraph-depth][value="2"]').check();
  await expect.poll(() => query(page).depth).toBe("2");
  expect(query(page).canvas).toBe("full");
  await canvasReady(page);
  expect(await isFull(page)).toBe(true);
  // So does a "Not shown" link, a plain navigation.
  await page.locator('[data-would-be="1"] a').click();
  await expect.poll(() => query(page).depth).toBe("1");
  expect(query(page).canvas).toBe("full");
  await canvasReady(page);
  expect(await isFull(page)).toBe(true);
});

test("Back and Forward over a toggle restore each entry's presentation", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await toggle(page);

  await page.goBack();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN });
  await canvasReady(page);
  expect(await isFull(page)).toBe(false);

  await page.goForward();
  await expect.poll(() => query(page)).toEqual({ focus: PLAN, canvas: "full" });
  await canvasReady(page);
  expect(await isFull(page)).toBe(true);
});

test("a toggle retires the panel request still in flight", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  const held = await hold(page, `selected=${CAPACITY}`);

  await clickNode(page, CAPACITY);
  await held.seen;
  await toggle(page);
  held.release();
  await held.ended();
  await held.handled();

  expect(query(page)).toEqual({ focus: PLAN, canvas: "full" });
  await expect(page.locator("#kgraph-panel [data-kgraph-panel]")).toHaveCount(0);
  expect((await pushes(page)).map((u) => new URL(u, page.url()).search)).toEqual([
    `?focus=${PLAN}&canvas=full`,
  ]);
  const picked = await page.evaluate(() =>
    (window as any).LithosLensKnowledgeGraph.cy.elements(".picked").length,
  );
  expect(picked).toBe(0);
});

// ── The panel: drawer, Esc, focus, scroll ──────────────────────────────

test("Esc closes the drawer, not full page; the pin stays and the next click keeps the view", async ({
  page,
}) => {
  const traffic = panelTraffic(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}&edge=${FAINT_EDGE}&canvas=full`);
  await canvasReady(page);
  await markDocument(page);
  const render = await page.locator("#kgraph-panel").getAttribute("data-kgraph-render");

  // Keyboard into the panel's Node details: an explicit open.
  const details = page.locator(`#kgraph-panel [data-kgraph-card="${LEGACY}"] [data-kgraph-node-details]`);
  await details.focus();
  await page.keyboard.press("Enter");
  await nodePanel(page, LEGACY);
  await expect(page.locator("#kgraph-panel [data-kgraph-panel-heading]")).toBeFocused();
  expect(query(page)).toEqual({ focus: PLAN, selected: LEGACY, pin: FAINT_EDGE, canvas: "full" });

  await page.keyboard.press("Escape");

  await expect(page.locator("#kgraph-panel")).toBeEmpty();
  await expect(page.locator("#kgraph-panel")).toBeHidden();
  expect(query(page)).toEqual({ focus: PLAN, pin: FAINT_EDGE, canvas: "full" });
  expect(await isFull(page)).toBe(true);
  // The link that opened it went with its panel: the focus goes to a
  // canvas control.
  await expect(page.locator("[data-kgraph-full-toggle]")).toBeFocused();
  // The exempted edge is still drawn: the drawing did not change.
  const drawn = await page.evaluate(
    (id) => (window as any).LithosLensKnowledgeGraph.cy.edges().some((e: any) => e.data("pid") === id),
    FAINT_EDGE,
  );
  expect(drawn).toBe(true);

  await clickNode(page, CAPACITY);
  await nodePanel(page, CAPACITY);
  expect(query(page)).toEqual({ focus: PLAN, selected: CAPACITY, pin: FAINT_EDGE, canvas: "full" });
  expect(await sameDocument(page)).toBe(true);
  expect(traffic.redirects).toEqual([]);
  expect(traffic.requests.every((url) => new URL(url).searchParams.get("render") === render)).toBe(true);
});

test("focus enters a panel opened from the text and returns to that link on close", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  const link = page.locator(`[data-kgraph-edge="${SUPPORTS}"] .kgraph-edge-link`);

  await link.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator('#kgraph-panel [data-kgraph-panel="edge"]')).toBeVisible();
  await expect(page.locator("#kgraph-panel [data-kgraph-panel-heading]")).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(page.locator("#kgraph-panel")).toBeEmpty();
  expect(query(page)).toEqual({ focus: PLAN });
  await expect(link).toBeFocused();

  // The Close link does the same, in place.
  await link.click();
  await expect(page.locator('#kgraph-panel [data-kgraph-panel="edge"]')).toBeVisible();
  await markDocument(page);
  await page.locator("#kgraph-panel [data-kgraph-panel-close]").click();
  await expect(page.locator("#kgraph-panel")).toBeEmpty();
  expect(query(page)).toEqual({ focus: PLAN });
  expect(await sameDocument(page)).toBe(true);
  await expect(link).toBeFocused();
});

test("scrolling the drawer scrolls the drawer and never zooms the canvas; the wheel over the canvas zooms", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 560 });
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&selected=${PLAN}&canvas=full`);
  await canvasReady(page);
  const drawer = page.locator("#kgraph-panel");
  const box = (await drawer.boundingBox())!;
  const scrollable = await drawer.evaluate((el) => el.scrollHeight > el.clientHeight);
  expect(scrollable).toBe(true);
  const zoom = () => page.evaluate(() => (window as any).LithosLensKnowledgeGraph.cy.zoom());
  const before = await zoom();

  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, 300);
  await expect.poll(() => drawer.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  expect(await zoom()).toBe(before);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);

  const canvas = (await page.locator("[data-kgraph-canvas]").boundingBox())!;
  await page.mouse.move(canvas.x + 200, canvas.y + canvas.height - 120);
  // Cytoscape ignores a wheel for a moment after any scroll (its guard
  // against zooming while the page scrolls): wheel again until it zooms,
  // as a hand would.
  await expect
    .poll(async () => {
      await page.mouse.wheel(0, -300);
      return zoom();
    })
    .not.toBe(before);
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});

test("Text view reaches the text with the keyboard, and the page scrolls to it", async ({
  page,
}) => {
  await page.goto(`/knowledge/graph?focus=${PLAN}&canvas=full`);
  await canvasReady(page);
  await markDocument(page);

  await page.locator("[data-kgraph-full-toggle]").focus();
  await page.keyboard.press("Tab");
  const textView = page.locator("[data-kgraph-text-link]");
  await expect(textView).toBeFocused();
  await expect(textView).toHaveText("Text view");
  await page.keyboard.press("Enter");

  await expect.poll(() => new URL(page.url()).hash).toBe("#kgraph-text");
  const scope = await page.locator("#kgraph-text").boundingBox();
  expect(scope!.y).toBeGreaterThanOrEqual(0);
  expect(scope!.y).toBeLessThan(page.viewportSize()!.height);
  expect(await sameDocument(page)).toBe(true);
  expect(await isFull(page)).toBe(true);
  // The text is the page below the canvas: the scrollbar reaches the end.
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect(page.locator("[data-kgraph-hidden]")).toBeInViewport();
});

test("a panel opened over the drawer's side pans its note into view, at the same zoom", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&canvas=full`);
  await canvasReady(page);
  // The rightmost note: where the drawer will lie.
  const rightmost = await page.evaluate(() => {
    const cy = (window as any).LithosLensKnowledgeGraph.cy;
    return cy.nodes().max((n: any) => n.renderedPosition().x).ele.data("pid");
  });
  const before = await geometry(page);

  await clickNode(page, rightmost);
  await nodePanel(page, rightmost);
  await settled(page);

  const after = await geometry(page);
  expect(after.zoom).toBe(before.zoom);
  expect(after.panel).not.toBeNull();
  unobscured(after);
});

test("fits and pans move smoothly unless reduced motion is preferred", async ({ page }) => {
  const toggleNow = () =>
    page.evaluate(() => {
      (document.querySelector("[data-kgraph-full-toggle]") as HTMLElement).click();
      const canvas = document.querySelector("[data-kgraph-canvas]") as HTMLElement;
      return {
        animated: (window as any).LithosLensKnowledgeGraph.cy.animated(),
        motion: canvas.dataset.canvasMotion,
      };
    });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2`);
  await canvasReady(page);
  expect(await toggleNow()).toEqual({ animated: false, motion: "still" });

  await page.emulateMedia({ reducedMotion: "no-preference" });
  expect(await toggleNow()).toEqual({ animated: true, motion: "moving" });
  await settled(page);
  const g = await geometry(page);
  expect(g.zoom).toBeGreaterThanOrEqual(g.minReadable);
});

test("without JavaScript canvas=full changes nothing visible", async ({ browser }) => {
  const context = await browser.newContext({
    javaScriptEnabled: false,
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();
  const shot = async (url: string) => {
    await page.goto(url);
    await page.evaluate(() => (document as any).fonts.ready).catch(() => undefined);
    return page.screenshot({ fullPage: true });
  };
  const url = `/knowledge/graph?focus=${PLAN}&selected=${CAPACITY}`;
  const inline = await shot(url);
  await expect(page.locator("[data-kgraph-canvas]")).toBeHidden();
  await expect(page.locator('#kgraph-panel [data-kgraph-panel="node"]')).toBeVisible();
  const full = await shot(`${url}&canvas=full`);
  await expect(page.locator("[data-kgraph-canvas]")).toBeHidden();
  await expect(page.locator("[data-kgraph-toolbar]")).toBeHidden();
  await expect(page.locator("[data-kgraph-edges]")).toBeVisible();
  expect(full.equals(inline)).toBe(true);
  await context.close();
});

// ── At 1440, 768 and 390px ─────────────────────────────────────────────

for (const [width, height] of [
  [1440, 900],
  [768, 1024],
  [390, 844],
] as const) {
  test(`at ${width}px: inline, then full page with its ${width >= 768 ? "drawer" : "bottom sheet"}, then the controls and key disclosed`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&selected=${LEGACY}`);
    await canvasReady(page);
    await nodePanel(page, LEGACY);
    await capture(page, `knowledge-graph-full-inline-${width}`, true);

    await toggle(page);
    expect(await isFull(page)).toBe(true);
    const sheet = await page.locator("#kgraph-panel").boundingBox();
    const stage = (await page.locator("[data-kgraph-stage]").boundingBox())!;
    if (width >= 768) {
      // A drawer: the stage's right side, below the toolbar.
      expect(Math.abs(sheet!.x + sheet!.width - (stage.x + stage.width))).toBeLessThan(1);
      expect(sheet!.width).toBeLessThan(stage.width / 2);
      expect(Math.abs(sheet!.y + sheet!.height - (stage.y + stage.height))).toBeLessThan(1);
    } else {
      // A bottom sheet: the stage's full width, along its bottom.
      expect(Math.abs(sheet!.width - stage.width)).toBeLessThan(1);
      expect(Math.abs(sheet!.y + sheet!.height - (stage.y + stage.height))).toBeLessThan(1);
    }
    let g = await geometry(page);
    unobscured(g);
    expect(g.zoom).toBeGreaterThanOrEqual(g.minReadable);
    expect(g.hinted).toBe(g.clipped);
    expect(g.scrollWidth).toBeLessThanOrEqual(g.innerWidth);
    await capture(page, `knowledge-graph-full-panel-${width}`, false);


    // With the panel still open: the controls fold behind "Controls" below
    // 768px (the pill stays out), and disclosing them — or the key — keeps
    // the selected note clear of every overlay by a pan alone.
    const controls = page.locator("[data-kgraph-controls]");
    const controlsToggle = page.locator("[data-kgraph-controls-toggle]");
    await page.evaluate(() => {
      (document.querySelector("[data-kgraph-refresh-pill]") as HTMLElement).hidden = false;
    });
    await expect(page.locator("[data-kgraph-refresh-pill]")).toBeVisible();
    await settledLayout(page);
    unobscured(await geometry(page));
    const model = await positions(page);
    const zoom = (await geometry(page)).zoom;
    if (width < 768) {
      await expect(controls).toBeHidden();
      await expect(controlsToggle).toBeVisible();
      await controlsToggle.click();
      await expect(controlsToggle).toHaveAttribute("aria-expanded", "true");
      await settledLayout(page);
      g = await geometry(page);
      unobscured(g);
      expect(g.zoom).toBe(zoom);
    } else {
      await expect(controlsToggle).toBeHidden();
    }
    await expect(page.locator("[data-kgraph-min-weight]")).toBeVisible();
    await expect(page.locator("[data-kgraph-refresh-pill]")).toBeVisible();

    // The key, bottom-left (above a sheet), behind its own disclosure.
    const keyBody = page.locator("#kgraph-key-body");
    await expect(keyBody).toBeHidden();
    await page.locator("[data-kgraph-key-toggle]").click();
    await expect(keyBody).toBeVisible();
    await settledLayout(page);
    const key = (await page.locator("[data-kgraph-key]").boundingBox())!;
    const above = width < 768 ? (await page.locator("#kgraph-panel").boundingBox())!.y : stage.y + stage.height;
    expect(key.x - stage.x).toBeLessThan(20);
    expect(above - (key.y + key.height)).toBeLessThan(20);
    g = await geometry(page);
    unobscured(g);
    expect(g.zoom).toBe(zoom);
    expect(await positions(page)).toEqual(model);
    expect(g.scrollWidth).toBeLessThanOrEqual(g.innerWidth);
    expect(g.hinted).toBe(g.clipped);
    // The usable area is clear of every overlay.
    const usable = {
      left: g.canvas.left + g.usable.x1,
      top: g.canvas.top + g.usable.y1,
      right: g.canvas.left + g.usable.x2,
      bottom: g.canvas.top + g.usable.y2,
    };
    expect(overlaps(usable, g.toolbar)).toBe(false);
    expect(overlaps(usable, g.key)).toBe(false);
    expect(overlaps(usable, g.panel)).toBe(false);
    await capture(page, `knowledge-graph-full-controls-${width}`, false);
    await page.keyboard.press("Escape");
    await expect(page.locator("#kgraph-panel")).toBeEmpty();

    // Refit to what is left: everything, or the focus at the readable zoom.
    await toggle(page);
    await toggle(page);
    g = await geometry(page);
    expect(g.zoom).toBeGreaterThanOrEqual(g.minReadable);
    expect(g.hinted).toBe(g.clipped);
  });
}

// ── Keeping the selection clear as things change (round 2) ────────────

for (const [width, height] of [
  [1440, 900],
  [768, 1024],
  [390, 844],
] as const) {
  for (const near of ["toolbar", "key"] as const) {
    test(`at ${width}px a note selected by the ${near} stays clear as Controls and Key open`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height });
      await page.emulateMedia({ reducedMotion: "reduce" });
      await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&canvas=full`);
      await canvasReady(page);
      // The note nearest that overlay: the topmost, or the bottom-left one.
      const id = await page.evaluate((side) => {
        const cy = (window as any).LithosLensKnowledgeGraph.cy;
        const score = (n: any) => {
          const at = n.renderedPosition();
          return side === "toolbar" ? at.y : at.x - at.y;
        };
        return cy.nodes().min(score).ele.data("pid");
      }, near);
      await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&selected=${encodeURIComponent(id)}&canvas=full`);
      await canvasReady(page);
      await expect(page.locator("#kgraph-panel [data-kgraph-panel]")).toBeVisible();
      await settledLayout(page);
      unobscured(await geometry(page));
      const model = await positions(page);
      const zoom = (await geometry(page)).zoom;

      if (width < 768) {
        await page.locator("[data-kgraph-controls-toggle]").click();
        await settledLayout(page);
        unobscured(await geometry(page));
      }
      await page.locator("[data-kgraph-key-toggle]").click();
      await settledLayout(page);
      const g = await geometry(page);
      unobscured(g);
      expect(g.zoom).toBe(zoom);
      expect(await positions(page)).toEqual(model);
      expect(g.zoom).toBeGreaterThanOrEqual(g.minReadable);
    });
  }
}

test("resizing the window keeps the selected note clear, by a pan alone", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&selected=${CAPACITY}&canvas=full`);
  await canvasReady(page);
  await settledLayout(page);
  unobscured(await geometry(page));
  const model = await positions(page);
  const zoom = (await geometry(page)).zoom;

  await page.setViewportSize({ width: 390, height: 844 });
  await settledLayout(page);

  const g = await geometry(page);
  unobscured(g);
  expect(g.zoom).toBe(zoom);
  expect(await positions(page)).toEqual(model);
  expect(g.hinted).toBe(g.clipped);
  // The stage follows the taller, stacked header.
  const frame = await page.evaluate(() => ({
    header: document.querySelector("header.shell-header")!.getBoundingClientRect().bottom,
    stage: document.querySelector("[data-kgraph-stage]")!.getBoundingClientRect(),
    innerHeight: window.innerHeight,
  }));
  expect(Math.abs(frame.stage.top - frame.header)).toBeLessThan(1);
  expect(Math.abs(frame.stage.bottom - frame.innerHeight)).toBeLessThan(1);
});

test("the refresh pill appearing moves the drawer and the usable area below the toolbar", async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&selected=${PLAN}&canvas=full`);
  await canvasReady(page);
  await settledLayout(page);
  const before = (await page.locator("[data-kgraph-toolbar]").boundingBox())!;

  // A missed-frames refresh on the knowledge stream raises the pill.
  await page.evaluate(() =>
    ((window as any).__sources as EventSource[]).forEach((source) =>
      source.dispatchEvent(new MessageEvent("lens.refresh", { data: "{}" })),
    ),
  );
  await expect(page.locator("[data-kgraph-refresh-pill]")).toBeVisible();
  await settledLayout(page);

  const toolbar = (await page.locator("[data-kgraph-toolbar]").boundingBox())!;
  expect(toolbar.height).toBeGreaterThan(before.height); // the pill wrapped it
  const drawer = (await page.locator("#kgraph-panel").boundingBox())!;
  expect(Math.abs(drawer.y - (toolbar.y + toolbar.height))).toBeLessThan(1);
  const g = await geometry(page);
  expect(Math.abs(g.canvas.top + g.usable.y1 - (toolbar.y + toolbar.height))).toBeLessThan(1);
  unobscured(g);
  expect(g.hinted).toBe(g.clipped);
  // The hint, under the toolbar, moved down with it.
  const hint = await page.locator("[data-kgraph-pan-hint]").boundingBox();
  if (hint) expect(hint.y).toBeGreaterThanOrEqual(toolbar.y + toolbar.height);
});

test("a short landscape window: the stage and its sheet end at the window's bottom", async ({
  page,
}) => {
  await page.setViewportSize({ width: 667, height: 375 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}&selected=${PLAN}&canvas=full`);
  await canvasReady(page);

  const frame = await page.evaluate(() => ({
    header: document.querySelector("header.shell-header")!.getBoundingClientRect().bottom,
    stage: document.querySelector("[data-kgraph-stage]")!.getBoundingClientRect(),
    sheet: document.querySelector("#kgraph-panel")!.getBoundingClientRect(),
    innerHeight: window.innerHeight,
  }));
  expect(Math.abs(frame.stage.top - frame.header)).toBeLessThan(1);
  expect(Math.abs(frame.stage.bottom - frame.innerHeight)).toBeLessThan(1);
  expect(frame.sheet.bottom).toBeLessThanOrEqual(frame.innerHeight + 1);
  const g = await geometry(page);
  expect(g.canvas.bottom).toBeLessThanOrEqual(frame.innerHeight + 1);
});

test("a text link used before a toggle fetches and pushes the toggled mode, both ways", async ({
  page,
}) => {
  const traffic = panelTraffic(page);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}`);
  await canvasReady(page);
  await markDocument(page);
  const render = await page.locator("#kgraph-panel").getAttribute("data-kgraph-render");
  const link = page.locator(`[data-kgraph-edge="${SUPPORTS}"] .kgraph-edge-link`);
  const edgePanel = page.locator('#kgraph-panel [data-kgraph-panel="edge"]');
  const use = async (expected: Record<string, string>) => {
    await link.click();
    await expect(edgePanel).toBeVisible();
    await expect.poll(() => query(page)).toEqual(expected);
  };

  await use({ focus: PLAN, edge: SUPPORTS });
  await expect(link).toHaveAttribute("hx-push-url", "false");
  await toggle(page);
  await page.keyboard.press("Escape");
  await expect(page.locator("#kgraph-panel")).toBeEmpty();
  await expect(link).toHaveAttribute(
    "data-kgraph-push-url",
    `/knowledge/graph?focus=${PLAN}&edge=${SUPPORTS}&canvas=full`,
  );
  await use({ focus: PLAN, edge: SUPPORTS, canvas: "full" });
  expect(await isFull(page)).toBe(true);

  await toggle(page);
  await page.keyboard.press("Escape");
  await expect(page.locator("#kgraph-panel")).toBeEmpty();
  await expect(link).toHaveAttribute(
    "data-kgraph-push-url",
    `/knowledge/graph?focus=${PLAN}&edge=${SUPPORTS}`,
  );
  await use({ focus: PLAN, edge: SUPPORTS });
  expect(await isFull(page)).toBe(false);

  expect(await sameDocument(page)).toBe(true);
  expect(traffic.redirects).toEqual([]);
  expect(traffic.requests.map((url) => Object.fromEntries(new URL(url).searchParams))).toEqual([
    { focus: PLAN, edge: SUPPORTS, render },
    { focus: PLAN, edge: SUPPORTS, canvas: "full", render },
    { focus: PLAN, edge: SUPPORTS, render },
  ]);
  expect((await pushes(page)).map((u) => new URL(u, page.url()).search)).toEqual([
    `?focus=${PLAN}&edge=${SUPPORTS}`,
    `?focus=${PLAN}&edge=${SUPPORTS}&canvas=full`,
    `?focus=${PLAN}&canvas=full`,
    `?focus=${PLAN}&edge=${SUPPORTS}&canvas=full`,
    `?focus=${PLAN}&edge=${SUPPORTS}`,
    `?focus=${PLAN}`,
    `?focus=${PLAN}&edge=${SUPPORTS}`,
  ]);
});

// ── Room for the selection when the overlays would meet (round 3) ─────

for (const scenario of [
  {
    name: "a short landscape window with the sheet, Controls and Key open",
    load: [667, 375] as const,
    resize: null,
    selected: LEGACY,
  },
  {
    name: "a desktop page resized to 390px, then Controls and Key opened, at its own zoom",
    load: [1440, 900] as const,
    resize: [390, 844] as const,
    selected: PLAN,
  },
]) {
  test(`the overlays give way for the selected note: ${scenario.name}`, async ({ page }) => {
    await page.setViewportSize({ width: scenario.load[0], height: scenario.load[1] });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(
      `/knowledge/graph?focus=${PLAN}&depth=2&selected=${scenario.selected}&canvas=full`,
    );
    await canvasReady(page);
    await nodePanel(page, scenario.selected);
    if (scenario.resize) {
      await page.setViewportSize({ width: scenario.resize[0], height: scenario.resize[1] });
      await settledLayout(page);
    }
    const zoom = (await geometry(page)).zoom;
    const model = await positions(page);

    await page.locator("[data-kgraph-controls-toggle]").click();
    await settledLayout(page);
    unobscured(await geometry(page));
    await page.locator("[data-kgraph-key-toggle]").click();
    await settledLayout(page);

    const g = await geometry(page);
    unobscured(g);
    // Room was made by the overlays, not by zooming out or moving notes.
    expect(g.zoom).toBe(zoom);
    expect(await positions(page)).toEqual(model);
    // Each kept its own controls: the toggles and the disclosures still shown.
    for (const control of [
      "[data-kgraph-full-toggle]",
      "[data-kgraph-controls-toggle]",
      "[data-kgraph-key-toggle]",
      "#kgraph-panel [data-kgraph-panel-close]",
    ]) {
      await expect(page.locator(control)).toBeInViewport();
    }

    // Closed, the room is given back: the toolbar's controls show again.
    await page.keyboard.press("Escape");
    await expect(page.locator("#kgraph-panel")).toBeEmpty();
    await settledLayout(page);
    const restored = await page.evaluate(
      () => getComputedStyle(document.querySelector("[data-kgraph-stage]")!).getPropertyValue("--kgraph-toolbar-cap"),
    );
    expect(restored).toBe("");
  });
}

// ── The operator's zoom, and a fit in flight (round 4) ────────────────

test("opening a panel on a note zoomed in to the maximum only pans: the drawer makes room", async ({
  page,
}) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&canvas=full`);
  await canvasReady(page);
  const focusAt = () =>
    page.evaluate(() => {
      const cy = (window as any).LithosLensKnowledgeGraph.cy;
      const box = document.querySelector("[data-kgraph-canvas]")!.getBoundingClientRect();
      const at = cy.nodes(".focus").renderedPosition();
      return { x: box.left + at.x, y: box.top + at.y };
    });
  const zoom = () => page.evaluate(() => (window as any).LithosLensKnowledgeGraph.cy.zoom());
  // The operator's own wheel, over the focus, up to Cytoscape's maximum.
  const start = await focusAt();
  await page.mouse.move(start.x, start.y);
  await expect
    .poll(async () => {
      await page.mouse.wheel(0, -400);
      return zoom();
    })
    .toBe(3);
  const model = await positions(page);
  const at = await focusAt();

  await page.mouse.click(at.x, at.y);
  await nodePanel(page, PLAN);
  await settledLayout(page);

  const g = await geometry(page);
  expect(g.zoom).toBe(3);
  expect(await positions(page)).toEqual(model);
  unobscured(g);
});

test("a toggle's animated fit is not cut short by the resize it causes", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto(`/knowledge/graph?focus=${PLAN}&depth=2&selected=${PLAN}`);
  await canvasReady(page);

  await page.locator("[data-kgraph-full-toggle]").click();
  await settledLayout(page);
  await settledLayout(page);

  const g = await geometry(page);
  // The graph does not fit at 768px: the fit opens at the readable floor,
  // centred on the selected note in the usable area — as reduced motion
  // reaches it at once.
  expect(g.zoom).toBeCloseTo(g.minReadable, 6);
  const selection = {
    x: (g.selection!.left + g.selection!.right) / 2 - g.canvas.left,
    y: (g.selection!.top + g.selection!.bottom) / 2 - g.canvas.top,
  };
  expect(Math.abs(selection.x - (g.usable.x1 + g.usable.x2) / 2)).toBeLessThan(1);
  expect(Math.abs(selection.y - (g.usable.y1 + g.usable.y2) / 2)).toBeLessThan(1);
  unobscured(g);
});
