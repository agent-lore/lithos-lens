import { test, expect, type Page } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import {
  GRAPH_BASE_URL,
  GRAPH_DEGRADED_SCOPE,
  GRAPH_REFUSED_SCOPE,
  TRUNCATED_BASE_URL,
  TRUNCATED_FRONTIER_LIMIT,
} from "../servers";

/**
 * Responsive screenshot capture — the artifacts-dir contract.
 *
 * Every covered page is captured full-page at each of the four standard
 * viewport widths and written to a deterministic path:
 *
 *   e2e/artifacts/<page>-<width>.png
 *
 * That path layout IS the downstream contract: loom's visual-review flow
 * (agent-lore/lithos-loom#283) picks the images up from `e2e/artifacts/` by
 * exactly this naming scheme. Change it only in lockstep with that consumer.
 * The directory is gitignored; files are overwritten on every run.
 *
 * The contract is exact: each PNG's pixel width equals its stated viewport
 * width, and the page must not overflow horizontally (scrollWidth <= viewport
 * width) — both are asserted per capture, alongside the file existing with
 * non-empty bytes, so a silently broken capture cannot pass.
 */

// Chromium's headless shell occasionally aborts a full-page capture with
// "Protocol error (Page.captureScreenshot)" under parallel load. Mitigate the
// load itself: this file opts out of fullyParallel (mode "default" runs its
// tests sequentially in one worker) and captures only after a settled wait
// (fonts loaded + a double rAF tick). One retry stays as backstop; the ready()
// waits below are deterministic, so a retry can't mask a real failure.
test.describe.configure({ mode: "default", retries: 1 });

const ARTIFACTS_DIR = path.resolve(__dirname, "..", "artifacts");

/**
 * A mini-graph is drawn at the model's own scale, never blown up to fill its
 * box (T2-A5, round-4 correctness f-006).
 *
 * The canvas is a fixed, viewport-relative height sized for the RULE's worst
 * case (two blocker ranks up, one of dependents down). `cy.fit()` has a
 * minimum readable zoom and had no maximum, so a neighbourhood with little in
 * it — most commonly the focal task alone — was scaled up until one node
 * filled the panel. Both halves of the fix are checked here: the automatic fit
 * is capped at the model scale, and the box gives back the height the small
 * picture does not need.
 */
async function miniGraphIsNotMagnified(page: Page) {
  const canvas = page.locator(
    '[data-mini-graph] [data-graph-canvas][data-canvas-state="ready"]',
  );
  await expect(canvas).toBeVisible();
  const drawn = await page.evaluate(() => {
    const graph = (window as any).LithosLensMiniGraph;
    const box = document.querySelector(
      "[data-mini-graph] [data-graph-canvas]",
    ) as HTMLElement;
    const nodes = graph.cy.nodes();
    return {
      zoom: graph.cy.zoom(),
      // The tallest node as DRAWN against its own model height: at the cap
      // these are equal, and magnification is exactly their ratio.
      scale: Math.max(
        ...nodes.map((node: any) => node.renderedHeight() / node.height()),
      ),
      fontPx: Math.max(
        ...nodes.map(
          (node: any) => parseFloat(node.style("font-size")) * graph.cy.zoom(),
        ),
      ),
      height: box.getBoundingClientRect().height,
    };
  });
  // Never magnified: at or below the model's own scale, which is what a
  // populated mini-graph renders at.
  expect(drawn.zoom).toBeLessThanOrEqual(1.001);
  expect(drawn.scale).toBeLessThanOrEqual(1.001);
  // …and still legible, which is the floor this ceiling must not undo.
  expect(drawn.fontPx).toBeGreaterThanOrEqual(10);
  // A one-node picture leaves no half-empty panel behind it either.
  expect(drawn.height).toBeLessThan(260);
}

/**
 * The canvas never scales the graph below legibility (T2-A4, round-6 review).
 *
 * Cytoscape scales text with the viewport, so a fit that shrinks the graph into
 * a narrow box shrinks the labels with it — at 320px the loom graph fitted to
 * zoom 0.26 and drew its labels at under three pixels, which is a picture that
 * communicates none of what the canvas was added to show. The automatic fit
 * therefore stops at a readable floor and the graph overflows to be panned;
 * this asserts the floor held and that a clipped view SAYS it is partial,
 * rather than letting a fragment read as the whole graph.
 *
 * Asserted inside `ready()`, so it is checked at every captured width.
 */
async function canvasIsLegible(page: Page) {
  const drawn = await page.evaluate(() => {
    const graph = (window as any).LithosLensGraph;
    const box = document.querySelector("[data-graph-canvas]") as HTMLElement;
    // The SMALLEST label on the canvas, not a sampled one: a cycle's box
    // carries a caption of its own, and a floor that held for the nodes while
    // that rendered at six pixels would be a floor in name only.
    const sizes = graph.cy
      .nodes()
      .filter((node: any) => node.style("display") !== "none")
      .map((node: any) => parseFloat(node.style("font-size")) * graph.cy.zoom());
    return {
      rendered: Math.min(...sizes),
      clipped: box.dataset.canvasClipped === "true",
    };
  });
  expect(drawn.rendered).toBeGreaterThanOrEqual(10);
  const hint = page.locator("[data-graph-pan-hint]");
  if (drawn.clipped) await expect(hint).toBeVisible();
  else await expect(hint).toBeHidden();
}

/**
 * The landing's compact chip rows (§5.7): one line each, in smaller type than
 * the note page's chips, and every chip REACHABLE. A row too wide for the
 * window scrolls inside itself; it once clipped (`overflow: hidden`), so at
 * 320px the namespace and confidence chips were in the markup and nowhere on
 * screen — while the search cards' auto grid track grew the page instead. The
 * page-width check in the loop below covers the second; this the first.
 */
async function compactChipRowsAreWhole(page: Page) {
  const rows = await page.evaluate(() => {
    // The full variant's chip size, measured rather than restated.
    const probe = document.createElement("span");
    probe.className = "chip";
    document.body.append(probe);
    const fullSize = parseFloat(getComputedStyle(probe).fontSize);
    probe.remove();
    return Array.from(document.querySelectorAll(".note-chips-compact")).map(
      (row) => {
        const chips = Array.from(row.querySelectorAll(".chip"));
        const tops = new Set(
          chips.map((chip) => Math.round(chip.getBoundingClientRect().top)),
        );
        row.scrollLeft = row.scrollWidth;
        const box = row.getBoundingClientRect();
        const last = chips[chips.length - 1].getBoundingClientRect();
        const reachable = last.width > 0 && last.right <= box.right + 1;
        row.scrollLeft = 0;
        return {
          lines: tops.size,
          reachable,
          smaller: parseFloat(getComputedStyle(chips[0]).fontSize) < fullSize,
        };
      },
    );
  });
  expect(rows.length).toBeGreaterThan(0);
  for (const row of rows) {
    expect(row).toEqual({ lines: 1, reachable: true, smaller: true });
  }
}

/**
 * The note page's related panel placement (§5.7). At and above the sidebar
 * breakpoint (`min-width: 701px`, the stylesheet's one two-column breakpoint)
 * the aside sits BESIDE the article and stays on screen while a long body
 * scrolls; below it, it follows the body. Either way the summary line under
 * the chips is on the first screen. The page is scrolled back to the top
 * before returning so the capture starts where a reader does.
 */
// The first width at which the note page's related panel is a sidebar: the
// complement of the stylesheet's `max-width: 700px` block, where the task
// pages' two-column rows collapse (§5.7).
const SIDEBAR_MIN_WIDTH = 701;

async function relatedPanelPlacement(page: Page) {
  const width = page.viewportSize()!.width;
  const placement = await page.evaluate(() => {
    const article = document.querySelector(".note-layout > article")!;
    const aside = document.querySelector(".note-layout > aside.related-panel")!;
    const summary = document.querySelector("[data-related-summary]")!;
    const a = article.getBoundingClientRect();
    const r = aside.getBoundingClientRect();
    // Far down the body, past the panel's own height.
    window.scrollTo(0, a.top + window.scrollY + a.height / 2);
    const scrolled = aside.getBoundingClientRect();
    window.scrollTo(0, 0);
    return {
      long: a.height > 2 * window.innerHeight,
      beside: r.left >= a.right && Math.abs(r.top - a.top) < 2,
      below: r.top >= a.bottom,
      stuck: scrolled.top >= 0 && scrolled.top < 40,
      summaryOnFirstScreen:
        summary.getBoundingClientRect().bottom <= window.innerHeight,
    };
  });
  expect(placement.long).toBe(true);
  expect(placement.summaryOnFirstScreen).toBe(true);
  if (width >= SIDEBAR_MIN_WIDTH) {
    expect(placement.beside).toBe(true);
    expect(placement.stuck).toBe(true);
  } else {
    expect(placement.below).toBe(true);
  }
}

const WIDTHS = [320, 768, 1024, 1440] as const;

const PAGES: ReadonlyArray<{
  slug: string;
  url: string;
  ready: (page: Page) => Promise<void>;
}> = [
  {
    slug: "dashboard",
    url: "/tasks?since=2026-08-01",
    ready: async (page) => {
      await expect(page.locator(".task-board")).toBeVisible();
      // The artifact must show the blocked fixture row WITH its styled
      // blocker chips (loom's visual review looks at this page).
      await expect(
        page.locator('[data-task-group="blocked"] .blocker-chip').first(),
      ).toBeVisible();
      // …and the Gates section with a LIVE countdown. The server renders the
      // timer gate's absolute stamp ("ready at …") as the no-JS baseline and
      // tasks.js rewrites it to "ready in …", so waiting on that wording is
      // what makes the artifact show the countdown rather than the fallback.
      await expect(
        page.locator('[data-task-group="gates"] [data-gate-type-badge]').first(),
      ).toBeVisible();
      await expect(page.locator(".gate-countdown")).toContainText(/ready in /);
      // T2b: loom's PR reconciliation state, both ends of the vocabulary on
      // one board — the escalated PR (red, promoted into Needs attention) and
      // the one that is ready to merge (green, still in Gates). Waited on
      // here, not just captured, because this sandbox cannot look at the PNGs:
      // a board that lost a badge must fail the run rather than produce a
      // healthy-looking artifact.
      await expect(
        page.locator('[data-reconciliation-state="needs_human"]'),
      ).toHaveClass(/badge-reconciliation-danger/);
      await expect(
        page.locator('[data-reconciliation-state="ready_to_merge"]'),
      ).toHaveClass(/badge-reconciliation-ok/);
      // The chip the artifact must show reads as English, not as the slug the
      // markup hooks are built from.
      await expect(
        page.locator('[data-attention-rule="pr-needs-decision"]'),
      ).toHaveText("PR needs a decision");
      // §5.3: the board's ONE long, markdown-written description — the shape
      // loom and Claude Code actually file. The capture must show it as
      // MARKUP (the numbered list below its bold lead-in) and cut at a block
      // boundary, with the "see more" link to the full page. Waited on rather
      // than merely captured, for the reason every other clause here is: this
      // sandbox cannot look at the PNG, so a row that silently went back to
      // one run-on paragraph has to fail the run.
      const described = page.locator(
        '[data-task-row][data-task-id="lens-graph-view"]',
      );
      await expect(
        described.locator("[data-task-description] ol li").first(),
      ).toBeVisible();
      await expect(
        described.locator("[data-description-toggle]"),
      ).toContainText("see more");
    },
  },
  {
    // The project quick-switch strip (§5.3) on the board it exists for: a
    // monthly roadmap tag scoping three projects at once. The strip is what
    // the visual review is asked to look at, so each clause it must SHOW is
    // waited on here — this sandbox cannot read the PNG, and a capture that
    // silently lost the counts would otherwise pass as proof it had them.
    slug: "dashboard-projects",
    url: "/tasks?tag=roadmap-2026-09&since=2026-08-01",
    ready: async (page) => {
      await expect(page.locator(".task-board")).toBeVisible();
      const strip = page.locator("[data-project-strip]");
      await expect(strip).toBeVisible();
      // Every project in the scope, ordered by open count then slug, each
      // stating its own count.
      await expect(strip.locator("[data-project-chip]")).toHaveText([
        /lithos-loom\s*3/,
        /influx\s*2/,
        /lithos-lens\s*1/,
      ]);
      // Nothing selected yet, so there is nothing to clear.
      await expect(page.locator("[data-project-clear]")).toHaveCount(0);
    },
  },
  {
    // One project selected: the board narrows, the strip does not, and the
    // live chip is marked. The counts beside the unselected chips are the
    // point of the artifact — they are what makes the next move one click.
    slug: "dashboard-projects-one",
    url: "/tasks?tag=roadmap-2026-09&project=lithos-loom&since=2026-08-01",
    ready: async (page) => {
      await expect(page.locator(".task-board")).toBeVisible();
      const strip = page.locator("[data-project-strip]");
      await expect(strip.locator("[data-project-chip]")).toHaveCount(3);
      await expect(
        strip.locator('[data-project-chip="lithos-loom"]'),
      ).toHaveAttribute("aria-current", "true");
      await expect(page.locator("[data-project-clear]")).toBeVisible();
      // The board really is scoped to it.
      await expect(
        page.locator('[data-task-row][data-task-id="influx-backfill"]'),
      ).toHaveCount(0);
    },
  },
  {
    // Two projects selected (``project=a,b``): projects OR, so both chips are
    // live and both projects' rows are on the board.
    slug: "dashboard-projects-two",
    url: "/tasks?tag=roadmap-2026-09&project=lithos-loom,influx&since=2026-08-01",
    ready: async (page) => {
      await expect(page.locator(".task-board")).toBeVisible();
      const strip = page.locator("[data-project-strip]");
      await expect(strip.locator('[aria-current="true"]')).toHaveCount(2);
      await expect(
        page.locator('[data-task-row][data-task-id="influx-backfill"]'),
      ).toBeVisible();
      await expect(
        page.locator('[data-task-row][data-task-id="loom-schema"]'),
      ).toBeVisible();
      await expect(page.locator("[data-project-clear]")).toBeVisible();
    },
  },
  {
    // The truncation half of T1-S11, which no reviewer had ever seen rendered:
    // the Not-classified tail, the accuracy banner, and the PER-COUNTER
    // "at least this many" marking. Served by the second webServer, whose
    // `frontier_limit` is low enough for the demo fixtures to overflow — an
    // absolute URL, so it is unambiguous which instance this shot came from.
    //
    // The `ready()` below waits on EVERY marker the picture exists to prove,
    // because this sandbox cannot look at the PNGs: a capture that silently
    // lost one must FAIL here rather than produce a healthy-looking screenshot
    // a later reviewer reads as proof that it did not.
    slug: "dashboard-truncated",
    url: `${TRUNCATED_BASE_URL}/tasks?since=2026-08-01`,
    ready: async (page) => {
      await expect(page.locator(".task-board")).toBeVisible();
      // 1. The overflow tail, with a row actually in it.
      await expect(
        page.locator('[data-task-group="unclassified"] .task-row').first(),
      ).toBeVisible();
      // 2. The accuracy banner, naming the one side that truncated. Which
      //    side that is follows the fixture counts, not the slice: since the
      //    graph cluster landed, Blocked is the larger frontier (see
      //    `servers.ts`), so the limit separates them the other way round.
      const banner = page.locator("[data-truncation-banner]");
      await expect(banner).toBeVisible();
      await expect(banner).toContainText(
        `blocked frontier truncated at ${TRUNCATED_FRONTIER_LIMIT}`,
      );
      // 3. The per-counter marking on both counters the blocked read feeds.
      await expect(
        page.locator('[data-approximate-count="blocked"]'),
      ).toBeVisible();
      await expect(
        page.locator('[data-approximate-count="attention"]'),
      ).toBeVisible();
      // 4. The honesty claim itself, which is the whole slice: at this limit
      //    the ready read answered IN FULL, so the Ready card carries an
      //    exact count and must show no marking at all. A regression to the
      //    board-wide banner fails right here.
      await expect(
        page.locator('[data-approximate-count="ready"]'),
      ).toHaveCount(0);
      await expect(
        page.locator('[data-task-group="ready"] .task-row').first(),
      ).toBeVisible();
    },
  },
  {
    // The side panel (T2-A6), server-rendered open by `?selected=` — the
    // no-JS baseline, and the only artifact that shows the panel OVER the
    // board. `loom-worker` sits mid-chain in the demo's loom cluster, so it
    // is the one fixture carrying every section at once: a blocker above, two
    // dependents below, and a parent epic.
    slug: "dashboard-panel",
    url: "/tasks?since=2026-08-01&selected=loom-worker",
    ready: async (page) => {
      await expect(page.locator(".task-board")).toBeVisible();
      await expect(page.locator('[data-panel-task="loom-worker"]')).toBeVisible();
      // Both directions of the relationship, which is the slice: the chain
      // upstream and the "Blocks" list downstream.
      await expect(
        page.locator('[data-panel-blockers] [data-link-list="blockers"] li').first(),
      ).toBeVisible();
      await expect(
        page
          .locator('[data-panel-dependents] [data-link-list="dependents"] li')
          .first(),
      ).toBeVisible();
      // The parent breadcrumb and the two buttons §5.5 promises.
      await expect(page.locator("[data-panel-parent]")).toBeVisible();
      await expect(page.locator("[data-panel-expand]")).toBeVisible();
      await expect(page.locator("[data-panel-close]")).toBeVisible();
    },
  },
  {
    slug: "task-detail",
    url: "/tasks/influx-ingest-cutover",
    ready: async (page) => {
      await expect(
        page.locator('[data-task-detail="influx-ingest-cutover"]'),
      ).toBeVisible();
    },
  },
  {
    // The story's two headline surfaces in one page: the level-1 blocker chain
    // with live status, and the parent breadcrumb. `influx-ingest-cutover`
    // (captured above) has only OUTGOING edges, so it can never render either.
    slug: "task-detail-blocked",
    url: "/tasks/influx-backfill",
    ready: async (page) => {
      await expect(
        page.locator('[data-link-list="blockers"] li').first(),
      ).toBeVisible();
      await expect(page.locator("[data-parent-breadcrumb]")).toBeVisible();
    },
  },
  {
    // The detail mini-graph on a BLOCKED task (T2-A5): the artifact D11 names.
    // `loom-ship` is the one fixture that carries every tier of the rule at
    // once — blocked by `loom-worker` (itself blocked by `loom-transport`, so
    // two hops up), blocking `loom-announce` and the cross-project
    // `lens-graph-page` one hop down, and parented by `loom-epic`.
    //
    // This sandbox cannot look at the PNG, so each clause is waited on
    // separately: a capture that quietly lost the canvas, an edge or the text
    // chain beneath it must FAIL here rather than produce a healthy-looking
    // image a reviewer reads as proof it did not.
    slug: "task-detail-minigraph",
    url: "/tasks/loom-ship",
    ready: async (page) => {
      const canvas = page.locator(
        '[data-mini-graph] [data-graph-canvas][data-canvas-state="ready"]',
      );
      await expect(canvas).toBeVisible();
      // 1. Two up, one down, plus the parent epic — and NOT `loom-schema`
      //    (three hops up) or anything `loom-announce` blocks.
      const drawn = await page.evaluate(() =>
        (window as any).LithosLensMiniGraph.shown(),
      );
      expect(drawn.nodes.sort()).toEqual([
        "lens-graph-page",
        "loom-announce",
        "loom-epic",
        "loom-ship",
        "loom-transport",
        "loom-worker",
      ]);
      // 2. The parent epic is a LABELLED node — D11's words, and a claim
      //    about the text actually drawn rather than about the id behind it.
      const epicLabel = await page.evaluate(
        () => (window as any).LithosLensMiniGraph.node("loom-epic").data("label"),
      );
      expect(epicLabel).toBe("Loom run harness");
      // 3. ARROWHEADS ON EVERY EDGE, the same claim the project graph's
      //    artifact makes and the same styling vocabulary behind it (D11).
      expect(drawn.edges.length).toBeGreaterThan(0);
      expect(
        drawn.edges.filter((edge: any) => edge.arrow !== "triangle"),
      ).toEqual([]);
      // 4. The legend that says which way an arrow reads, and the focus link
      //    into the full project graph.
      await expect(page.locator("[data-mini-graph-legend] li").first()).toBeVisible();
      await expect(page.locator("[data-mini-graph-focus]")).toHaveAttribute(
        "href",
        /project=lithos-loom.*focus=loom-ship/,
      );
      // 5. And the text baseline is untouched BELOW it: the blocker chain the
      //    mini-graph illustrates, and the "Blocks:" line for the dependents
      //    it draws downstream.
      await expect(
        page.locator('[data-blocker-chain] [data-link-list="blockers"] li').first(),
      ).toBeVisible();
      await expect(
        page.locator('[data-dependents] [data-link-list="dependents"] li').first(),
      ).toBeVisible();
    },
  },
  {
    // The children table and the `epic` type badge.
    slug: "task-detail-children",
    url: "/tasks/influx-epic",
    ready: async (page) => {
      await expect(
        page.locator(".children-table tbody tr").first(),
      ).toBeVisible();
      // …and the FOCAL-ONLY mini-graph, which this fixture also is: an epic
      // whose only edge points down to a child, so two-up-one-down leaves it
      // alone on the canvas. The automatic fit has no ceiling of its own, so
      // that one node was magnified to fill a 22–32rem box — hundreds of
      // pixels wide, its label bigger than the page's own title (round-4
      // correctness f-006). Asserted at every captured width, because the box
      // is a viewport-relative height and the magnification followed it.
      await miniGraphIsNotMagnified(page);
    },
  },
  {
    // The overflow tail (T1-S7). The operator requirement is that an
    // agent-sized set degrade VISIBLY rather than truncate silently, which is
    // a claim about appearance and so cannot be settled by the HTML-substring
    // assertions in the pytest suite. `influx-shard-epic` is the demo's only
    // fixture that overflows a page (30 children against 25); the tail markup
    // is shared with the blocker chain and both provenance lists, so reviewing
    // it here reviews it everywhere.
    slug: "task-detail-overflow",
    url: "/tasks/influx-shard-epic",
    ready: async (page) => {
      await expect(page.locator('[data-link-tail="children"]')).toBeVisible();
      // The second focal-only mini-graph in these captures, and the one under
      // the longest page: the same magnification showed up here too.
      await miniGraphIsNotMagnified(page);
    },
  },
  {
    // The graph page with nothing to render yet (T2-A3): the scope picker is
    // what `/tasks/graph` IS until an operator chooses, so it is the first
    // thing anyone sees and the only state with no scope behind it.
    slug: "graph-picker",
    url: "/tasks/graph",
    ready: async (page) => {
      await expect(page.locator("[data-graph-picker]")).toBeVisible();
      // Both columns populated — an empty one is a legitimate render, and an
      // artifact of it would show nothing this page is for.
      await expect(
        page.locator("[data-picker-project]").first(),
      ).toBeVisible();
      await expect(page.locator("[data-picker-epic]").first()).toBeVisible();
    },
  },
  {
    // THE artifact the PRD names ("project graph with cycle + ghost +
    // isolated disclosure + legend"). The demo's loom cluster carries every
    // branch of graph assembly at once, and the `ready()` below waits on each
    // one separately: this sandbox cannot look at the PNG, so a capture that
    // silently lost a section must FAIL here rather than produce a
    // healthy-looking image a later reviewer reads as proof it did not.
    slug: "graph-project",
    url: "/tasks/graph?project=lithos-loom",
    ready: async (page) => {
      // Since T2-A4 this is the CANVAS artifact: Cytoscape draws from the same
      // payload and collapses the text behind "show as text" (D3), so what a
      // reviewer looks at here is the picture — arrowheads, the cycle as a
      // compound node, the ghost dimmed — with the legend that explains it.
      const canvas = page.locator('[data-graph-canvas][data-canvas-state="ready"]');
      await expect(canvas).toBeVisible();
      // 1. The cycle callout, and the SCC Lens can actually SHAPE — drawn as a
      //    compound parent, which is the convention the legend explains.
      await expect(page.locator("[data-cycle-callout]")).toBeVisible();
      await expect(canvas).toHaveAttribute("data-canvas-cycles", "1");
      // The members are INSIDE the box, which is the whole convention: a
      // parent node drawn beside them would bracket nothing.
      const inBox = await page.evaluate(() => {
        const graph = (window as any).LithosLensGraph;
        // Cytoscape's element ids are opaque here (a task id may be
        // `__proto__`, which the library itself cannot hold), so the box's
        // members are compared as elements rather than as strings.
        const children = graph.cycle("loom-cycle-b").children();
        const expected = ["loom-cycle-a", "loom-cycle-b"].map((id) =>
          graph.node(id).id(),
        );
        return {
          children: children.map((node: any) => node.id()).sort(),
          expected: expected.sort(),
        };
      });
      expect(inBox.children).toEqual(inBox.expected);
      expect(inBox.children.length).toBe(2);
      // 2. The ghost: drawn, dimmed, and carrying its project on the label —
      //    the cross-project `blocks` edge, the one node here that belongs to
      //    another scope.
      const ghost = await page.evaluate(() => {
        const node = (window as any).LithosLensGraph.node("lens-graph-page");
        return { opacity: Number(node.style("opacity")), label: node.data("label") };
      });
      expect(ghost.opacity).toBeLessThan(1);
      expect(ghost.label).toContain("lithos-lens");
      // 3. ARROWHEADS ON EVERY EDGE (D8) — the claim this whole artifact is
      //    for, since direction is the one thing a graph must not be readable
      //    two ways.
      const drawn = await page.evaluate(() =>
        (window as any).LithosLensGraph.shown(),
      );
      expect(drawn.edges.length).toBeGreaterThan(0);
      expect(
        drawn.edges.filter((edge: any) => edge.arrow !== "triangle"),
      ).toEqual([]);
      // 4. The legend and the chain line, both persistent beside the canvas.
      await expect(page.locator("[data-graph-legend]")).toBeVisible();
      await expect(
        page.locator('[data-longest-chain][data-chain-bound="exact"]'),
      ).toBeVisible();
      // 5. The text is COLLAPSED, not gone: depth (the demo's chain is five
      //    deep), the bracketed cycle, the ghost chip and the hierarchy tree
      //    are all still in the DOM behind the "show as text" toggle.
      await expect(page.locator("[data-graph-layers]")).toBeHidden();
      await expect(page.locator('[data-graph-layer="4"]')).toBeAttached();
      await expect(page.locator('[data-cycle-group="loom-cycle-b"]')).toBeAttached();
      await expect(page.locator('[data-ghost-project="lithos-lens"]')).toBeAttached();
      await expect(
        page.locator("[data-hierarchy-tree] [data-hierarchy-node]").first(),
      ).toBeAttached();
      await expect(page.locator("[data-toggle-text]")).toBeVisible();
      // 6. The disclosure, CLOSED: collapsed on a project scope is the
      //    acceptance criterion, and `open` is the other artifact below.
      await expect(page.locator("[data-isolated-disclosure]")).toHaveJSProperty(
        "open",
        false,
      );
      // 7. And it is the HEALTHY picture: no cycle-signal banner belongs on
      //    the artifact the PRD promises, or "signal incomplete" reads as
      //    this page's normal state.
      await expect(page.locator("[data-graph-banner]")).toHaveCount(0);
      await expect(page.locator("[data-graph-refusal]")).toHaveCount(0);
      // 8. And it is READABLE at this width, whichever width that is.
      await canvasIsLegible(page);
    },
  },
  {
    // The other half of A4, which no still of the default view can show: both
    // overlays switched on from the URL, and the side panel open BESIDE the
    // canvas (D9) rather than overlaying it the way the dashboard's does —
    // and, from A7, EXPLORATION MODE on a mid-chain node: `loom-ship` sits at
    // step 4 of the depth-5 chain, so its ancestors and descendants light and
    // the rest of the board dims around it.
    slug: "graph-focus",
    url: "/tasks/graph?project=lithos-loom&overlays=hierarchy,provenance&focus=loom-ship",
    ready: async (page) => {
      await expect(
        page.locator('[data-graph-canvas][data-canvas-state="ready"]'),
      ).toBeVisible();
      // The panel the URL's `focus` opened, showing THAT task.
      await expect(
        page.locator('[data-panel-host] [data-panel-task="loom-ship"]'),
      ).toBeVisible();
      // Both overlays drawn from the payload — including the provenance edge
      // whose source is a completed task outside this open-only scope, which
      // is the context ghost D6 resolves on every request so that toggling
      // costs no fetch.
      const types = await page.evaluate(() =>
        (window as any).LithosLensGraph.shown().edges.map((edge: any) => edge.type),
      );
      expect(types).toContain("parent_child");
      expect(types).toContain("discovered_from");
      const source = await page.evaluate(() =>
        (window as any).LithosLensGraph.node("loom-research-old").style("display"),
      );
      expect(source).not.toBe("none");
      // The canvas gave the panel its room: Cytoscape sizes its drawing
      // surface to the container once and does not watch it, so without the
      // resize the graph would be painted straight across the panel that just
      // opened — a picture no reviewer could read and no assertion on the
      // markup would catch.
      const sized = await page.evaluate(() => {
        const node = document.querySelector("[data-graph-canvas]") as HTMLElement;
        return {
          container: node.clientWidth,
          drawn: (window as any).LithosLensGraph.cy.width(),
        };
      });
      expect(sized.drawn).toBe(sized.container);
      // Focus mode (D8), which is the whole reason this capture is worth a
      // reviewer's eye: the classes are what the dimming is drawn FROM, so a
      // still that looks right for the wrong reason is caught here.
      const lighting = await page.evaluate(() => {
        const graph = (window as any).LithosLensGraph;
        const of = (name: string) =>
          graph.cy
            .nodes()
            .filter((node: any) => node.hasClass(name))
            .map((node: any) => node.data("label"))
            .length;
        return { lit: of("focus-lit"), dimmed: of("focus-dimmed") };
      });
      // The chain through it — schema → transport → worker → ship → announce —
      // plus the cross-project ghost it blocks.
      expect(lighting.lit).toBeGreaterThanOrEqual(5);
      expect(lighting.dimmed).toBeGreaterThan(0);
      // D10's line, from the demo board's own arithmetic: `loom-ship` blocks
      // `loom-announce` and the lens ghost, and Lithos names it as the sole
      // unsatisfied blocker of both — the cross-project half of the count is
      // the one no single-project read could have answered.
      await expect(page.locator("[data-panel-impact]")).toHaveText(
        /frees 2 in this graph, 2 immediately/,
      );
      // The chain line follows the focus (D7).
      await expect(page.locator("[data-longest-chain]")).toHaveAttribute(
        "data-chain-through",
        "loom-ship",
      );
      await canvasIsLegible(page);
    },
  },
  {
    // D4's honesty, which is a claim about APPEARANCE and so cannot be settled
    // by the pytest suite's HTML substrings: when the scoped blocked read came
    // back capped, the page has to SAY so and mark the rows it cannot answer
    // for — never an implied "no cycle". Served by the third instance, whose
    // `frontier_limit` caps every read (see `servers.ts`).
    slug: "graph-degraded",
    url: `${GRAPH_BASE_URL}/tasks/graph?epic=${GRAPH_DEGRADED_SCOPE}`,
    ready: async (page) => {
      // THE TEXT artifact, deliberately: the markers below are sentences, not
      // shapes, so this capture takes the canvas's "show as text" toggle back
      // to the baseline A3 renders — which also proves the toggle restores it
      // rather than merely hiding it (D3: the text stays in the DOM).
      await expect(
        page.locator('[data-graph-canvas][data-canvas-state="ready"]'),
      ).toBeVisible();
      await page.locator("[data-toggle-text]").click();
      // Still a graph, not an error page: degraded means partial, not absent.
      await expect(page.locator("[data-graph-layers]")).toBeVisible();
      // 1. The banner naming what the read did …
      await expect(
        page.locator('[data-graph-banner="cycle-truncated"]'),
      ).toBeVisible();
      // 2. … and the one stating the rule with its real count.
      await expect(
        page.locator('[data-graph-banner="cycle-unknown-count"]'),
      ).toBeVisible();
      // 3. The per-row marking those banners are about. A page that showed
      //    the banner and left the rows unmarked would read as "no cycle".
      await expect(
        page.locator('[data-marker="cycle-unknown"]').first(),
      ).toBeVisible();
      // 4. An epic scope, so the disclosure starts OPEN — the other half of
      //    the criterion `graph-project` captures closed.
      await expect(page.locator("[data-isolated-disclosure]")).toHaveJSProperty(
        "open",
        true,
      );
    },
  },
  {
    // The refusal (§5.7): a scope over the node guard is turned away rather
    // than drawn, and the panel has to be readable at every width — it is the
    // only thing on the page. Same instance, a scope one side of the guard.
    slug: "graph-refused",
    url: `${GRAPH_BASE_URL}/tasks/graph?project=${GRAPH_REFUSED_SCOPE}`,
    ready: async (page) => {
      const refusal = page.locator("[data-graph-refusal]");
      await expect(refusal).toBeVisible();
      await expect(refusal).toContainText("Narrow your scope");
      // The count is the guard's own evidence, so the artifact must carry it.
      await expect(page.locator("[data-refusal-count]")).toBeVisible();
      // And nothing was drawn: a refusal that still rendered layers would be
      // the guard failing open.
      await expect(page.locator("[data-graph-layers]")).toHaveCount(0);
    },
  },
  {
    // The knowledge landing: the search row and the namespace filter row
    // above "Your notes" and then "Recent intake" (§7.1). Its input once
    // collapsed to two characters beside a full-row Search button (the
    // shared `width: 100%` on the button in a flex row), and no capture
    // covered the page, so nobody saw it. The proportion is asserted here,
    // at every width, because this sandbox cannot read the PNG. The header
    // carries no nav search box here: the landing's form is its one search.
    slug: "knowledge",
    url: "/knowledge",
    ready: async (page) => {
      const yours = page.getByRole("heading", { level: 2, name: "Your notes" });
      const intake = page.getByRole("heading", {
        level: 2,
        name: "Recent intake",
      });
      await expect(yours).toBeVisible();
      await expect(intake).toBeVisible();
      // Your notes first, intake second — the order is the point of the page.
      expect((await yours.boundingBox())!.y).toBeLessThan(
        (await intake.boundingBox())!.y,
      );
      const namespaces = page.getByRole("navigation", {
        name: "Namespace filter",
      });
      await expect(namespaces.getByRole("link", { name: "all" })).toBeVisible();
      // The row wraps: it never widens the page past a narrow viewport.
      const row = await namespaces.boundingBox();
      expect(row!.x + row!.width).toBeLessThanOrEqual(
        page.viewportSize()!.width,
      );
      // One search box on the landing: the page's own form, not the
      // chrome's nav box beside it (§5.7). With no query it has the focus.
      const search = page.getByRole("search");
      await expect(search).toHaveCount(1);
      await expect(search).toHaveClass("knowledge-search");
      await expect(search.getByRole("searchbox")).toBeFocused();
      const form = await search.boundingBox();
      const input = await search.getByRole("searchbox").boundingBox();
      const button = await search
        .getByRole("button", { name: "Search" })
        .boundingBox();
      expect(input!.width).toBeGreaterThanOrEqual(form!.width * 0.6);
      // The button is content-width: it sits beside the input on one row.
      expect(button!.width).toBeLessThan(form!.width * 0.4);
      expect(button!.y).toBe(input!.y);
      // The quarantined fixture's row is the widest chip row in the corpus.
      await expect(page.locator(".note-status-quarantined")).toBeVisible();
      await compactChipRowsAreWhole(page);
    },
  },
  {
    // The search branch's result cards: a separate template branch and a
    // separate layout (a grid of cards), whose auto track once let one chip
    // row widen the page past a 320px viewport.
    slug: "knowledge-search",
    url: "/knowledge?q=influx",
    ready: async (page) => {
      await expect(
        page.getByRole("heading", { level: 2, name: "Results for “influx”" }),
      ).toBeVisible();
      // Still one search form; with a query the input does not take focus.
      const search = page.getByRole("search");
      await expect(search).toHaveCount(1);
      await expect(search.getByRole("searchbox")).not.toBeFocused();
      await compactChipRowsAreWhole(page);
    },
  },
  {
    slug: "note",
    url: "/note/note-influx-plan",
    ready: async (page) => {
      // The note page is only "ready" once the K1-S4 related aside is up too.
      await expect(
        page.getByRole("complementary", { name: "Related notes" }),
      ).toBeVisible();
    },
  },
  {
    // A full-text note several screens long: the related panel is a sticky
    // sidebar at the wide widths and follows the body at the narrow ones, and
    // the summary line under the chips states its groups on the first screen.
    slug: "note-long",
    url: "/note/note-influx-capacity",
    ready: async (page) => {
      await expect(
        page.getByRole("complementary", { name: "Related notes" }),
      ).toBeVisible();
      await expect(page.locator("[data-related-summary]")).toHaveText(
        /Related:\s*2 outgoing links · 1 source · 3 typed edges/,
      );
      await relatedPanelPlacement(page);
    },
  },
  {
    slug: "note-quarantined",
    url: "/note/note-influx-legacy-ingest",
    ready: async (page) => {
      await expect(page.locator(".note-status-quarantined")).toBeVisible();
    },
  },
  {
    slug: "note-missing",
    url: "/note/missing-note",
    ready: async (page) => {
      await expect(page.getByText("Document not found.")).toBeVisible();
    },
  },
  {
    // The operator page (§5C.5) and the identity chip in the chrome, which is
    // what the visual review is asked to look at. The fixture config sets no
    // `default_operator`, so the identity is CHOSEN here: the cookie is set
    // and the page reloaded, which is also the only way one capture can show
    // both the page's "acting as / where it came from" line and the chip.
    //
    // Every clause the artifact must show is waited on, for the reason the
    // other entries do it: this sandbox cannot read the PNG, so a page that
    // silently lost the boundary statement has to fail the run rather than
    // produce a healthy-looking image.
    slug: "operator",
    url: "/operator",
    ready: async (page) => {
      await page.context().addCookies([
        {
          name: "lens_operator",
          value: "dave",
          url: page.url(),
        },
      ]);
      await page.reload();
      // The chip in the chrome, naming the identity and offering the switch.
      const chip = page.locator("[data-operator-chip]");
      await expect(chip).toHaveAttribute("data-operator-resolved", "yes");
      await expect(chip.locator("[data-operator-id]")).toHaveText("dave");
      // The page itself: the identity, its SOURCE, and the form to switch.
      await expect(
        page.locator("[data-operator-current] [data-operator-source]"),
      ).toHaveAttribute("data-operator-source", "cookie");
      await expect(page.locator("#operator")).toHaveValue("dave");
      // The boundary statement REQUIREMENTS §5C.1 requires to be here.
      await expect(page.locator("[data-operator-boundary]")).toContainText(
        "Anyone who can reach this port can perform these actions",
      );
      await expect(page.locator("[data-operator-boundary]")).toContainText(
        "hygiene, not security",
      );
    },
  },
];

for (const { slug, url, ready } of PAGES) {
  for (const width of WIDTHS) {
    test(`screenshot: ${slug} at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      await page.goto(url);
      await ready(page);

      // Nothing here may have come from another spec's synthetic event. One
      // `webServer` serves the whole run and `EventHub.publish` fans to every
      // connected browser, so before the projects were sequenced (see
      // playwright.config.ts) smoke's `task.created` publish landed in THIS
      // tab and got photographed — a row the app never rendered from its own
      // data, inside a required visual gate. The config keeps the two files
      // apart; this asserts the result, so a later config change cannot
      // quietly reopen the hole. Deliberately matched on the skeleton CLASS
      // rather than smoke's task id: the fixtures never produce a skeleton, so
      // any skeleton here is a leak, whatever published it.
      await expect(page.locator(".task-row-skeleton")).toHaveCount(0);

      // No horizontal overflow: a page wider than the viewport would yield a
      // wider-than-stated PNG and a broken responsive layout.
      const scrollWidth = await page.evaluate(
        () => document.documentElement.scrollWidth,
      );
      expect(scrollWidth).toBeLessThanOrEqual(width);

      // No row runs past its card. The page-level check above cannot see
      // this: `.task-group` clips with `overflow: hidden`, so a row held wide
      // by one long unbroken token in its description (a path in a code
      // block) lost its right edge — title, prose and list items cut off
      // mid-sentence — while the document stayed exactly the viewport wide.
      const overflowingRows = await page.evaluate(() =>
        Array.from(document.querySelectorAll(".task-group .task-row"))
          .filter((row) => {
            const card = row.closest(".task-group")!.getBoundingClientRect();
            return row.getBoundingClientRect().right > card.right + 1;
          })
          .map((row) => (row as HTMLElement).dataset.taskId),
      );
      expect(overflowingRows).toEqual([]);

      // Settle before capturing: fonts loaded plus a double rAF tick. This is
      // the real mitigation for Chromium's intermittent full-page
      // Page.captureScreenshot abort; the retry above is only a backstop.
      await page.evaluate(async () => {
        await document.fonts.ready;
        await new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)),
        );
      });

      const file = path.join(ARTIFACTS_DIR, `${slug}-${width}.png`);
      await page.screenshot({ path: file, fullPage: true });

      // A silently broken capture must not pass: the file has to exist,
      // carry actual image bytes, and be exactly the stated width (PNG IHDR
      // width, bytes 16-19 big-endian; deviceScaleFactor is 1).
      const stat = fs.statSync(file);
      expect(stat.size).toBeGreaterThan(0);
      const header = fs.readFileSync(file).subarray(0, 24);
      expect(header.readUInt32BE(16)).toBe(width);
    });
  }
}

/**
 * The sidebar breakpoint IS the task pages' two-column breakpoint, checked on
 * both sides of the boundary rather than only at the capture widths (which
 * would pass with the threshold anywhere in 321–768px). At 700px a task row
 * has collapsed to one column and the related panel follows the note body;
 * at 701px the row has two columns and the panel is a sticky sidebar.
 */
test("the related sidebar starts where the task rows' two columns do", async ({
  page,
}) => {
  const rowColumns = () =>
    page.evaluate(
      () =>
        getComputedStyle(document.querySelector(".task-row")!)
          .gridTemplateColumns.split(" ").length,
    );
  for (const [width, columns] of [
    [SIDEBAR_MIN_WIDTH - 1, 1],
    [SIDEBAR_MIN_WIDTH, 2],
  ] as const) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto("/tasks?since=2026-08-01");
    await expect(page.locator(".task-row").first()).toBeVisible();
    expect(await rowColumns()).toBe(columns);

    await page.goto("/note/note-influx-capacity");
    await expect(
      page.getByRole("complementary", { name: "Related notes" }),
    ).toBeVisible();
    await relatedPanelPlacement(page);
  }
});
