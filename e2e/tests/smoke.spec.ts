import { test, expect } from "@playwright/test";
import { TRUNCATED_BASE_URL } from "../servers";

/**
 * Smoke suite: the app boots in fake-Lithos mode and every top-level
 * server-rendered surface renders from the in-memory fixtures. These are
 * deliberately shallow — they prove the pages come up and are wired, not the
 * detailed view logic (that is covered by the Python unit suite).
 */

test("health endpoint reports Lithos ok", async ({ request }) => {
  const response = await request.get("/health");
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  expect(body.lithos).toBe("ok");
});

test("dashboard renders the task board with fixture rows", async ({ page }) => {
  await page.goto("/tasks?since=2026-08-01");

  await expect(page.getByRole("heading", { level: 1, name: "Tasks" })).toBeVisible();

  // The workable board and its flagship fixture task are present. Open tasks
  // are partitioned into In progress / Ready / Blocked sections by the Lithos
  // frontier, so the row lives in one of those rather than a flat "open" group.
  await expect(page.locator(".task-board")).toBeVisible();
  await expect(
    page.locator('[data-task-row][data-task-id="influx-ingest-cutover"]'),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Cut over Influx ingest path" }),
  ).toBeVisible();

  // At least one task row rendered overall.
  expect(await page.locator("[data-task-row]").count()).toBeGreaterThan(0);

  // The terminal groups render their fixture rows (not just the always-present
  // section wrappers): ids, titles, counts, and terminal status metadata.
  const completed = page.locator('[data-task-group="completed"]');
  await expect(
    completed.locator('[data-task-row][data-task-id="lens-note-view"]'),
  ).toHaveAttribute("data-task-status", "completed");
  await expect(
    completed.getByRole("link", { name: "Land knowledge note view" }),
  ).toBeVisible();
  expect(await completed.locator("[data-task-row]").count()).toBe(1);

  const cancelled = page.locator('[data-task-group="cancelled"]');
  await expect(
    cancelled.locator('[data-task-row][data-task-id="influx-spike"]'),
  ).toHaveAttribute("data-task-status", "cancelled");
  await expect(
    cancelled.getByRole("link", { name: "Spike Influx client options" }),
  ).toBeVisible();
  expect(await cancelled.locator("[data-task-row]").count()).toBe(1);
});

test("the gates section counts its timer gate down in the browser", async ({ page, request }) => {
  // T1-S4 story 6: Lithos emits NO event when a timer gate lapses, so the
  // countdown and the one-shot self-refresh are browser-side and can only be
  // proved here. The server renders the absolute stamp as the no-JS baseline;
  // tasks.js replaces it with a live "ready in …" and publishes the instant it
  // will refresh at.
  await page.goto("/tasks?since=2026-08-01");

  const gates = page.locator('[data-task-group="gates"]');
  // Human gates lead, and the human one says what it holds up.
  const human = gates.locator(
    '[data-gate-row][data-task-id="influx-read-swap-approval"]',
  );
  await expect(human.locator("[data-gate-type-badge]")).toHaveText("human");
  await expect(human.locator("[data-gate-waiters] summary")).toHaveText(
    "blocks 1 task",
  );

  const countdown = gates.locator(".gate-countdown");
  await expect(countdown).toContainText(/^ready in /);
  // The board carries exactly one refresh instant, and it is the timer gate's.
  const readyAt = await countdown.getAttribute("data-gate-ready-at");
  await expect(page.locator(".task-board")).toHaveAttribute(
    "data-gates-next-ready-at",
    readyAt as string,
  );

  // The countdown is genuinely client-side: what the SERVER sent for this
  // element is the absolute "ready at …" baseline, so the "ready in …" above
  // can only have come from tasks.js rewriting it after load.
  const served = await (await request.get("/tasks?since=2026-08-01")).text();
  expect(served).toContain(`data-gate-ready-at="${readyAt}">ready at `);
  expect(served).not.toContain("ready in ");
});

test("elements marked hidden stay hidden", async ({ page }) => {
  // Browser truth for the [hidden] reset in lens.css: the chip rule sets
  // `display: inline-flex` on .finding-chip, which outranks the UA's
  // `[hidden] { display: none }` — without the reset every row advertised a
  // "0 new findings" badge, and the claim list on an unclaimed row rendered as
  // an empty box. tasks.js toggles both via the same attribute.
  await page.goto("/tasks?since=2026-08-01");

  const row = page.locator(
    '[data-task-row][data-task-id="influx-dashboards"]',
  );
  await expect(row).toBeVisible();
  await expect(row.locator("[data-finding-count]")).toBeHidden();
  await expect(row.locator("[data-claim-list]")).toBeHidden();
  // The claimed fixture proves the reset does not hide what should show.
  await expect(
    page
      .locator('[data-task-row][data-task-id="influx-ingest-cutover"]')
      .locator("[data-claim-list]"),
  ).toBeVisible();
});

test("nothing marked hidden is painted, anywhere on the page", async ({ page }) => {
  // The generic complement to the test above, which names two elements. This
  // one sweeps EVERY `[hidden]` element, so a future component rule that sets
  // `display` on a class is caught wherever it lands rather than only on the
  // two the reset was written for. From T1-S6, which found the original bug.
  await page.goto("/tasks?since=2026-08-01");

  const painted = await page.locator("[hidden]").evaluateAll((elements) =>
    elements
      .filter((element) => getComputedStyle(element).display !== "none")
      .map((element) => element.outerHTML.slice(0, 80)),
  );

  expect(painted).toEqual([]);
});

test("the needs-attention stripe is inset like its siblings, not welded to the card", async ({
  page,
}) => {
  // dafa6221. The stripe carries a border and a radius of its own, so it is a
  // BAND INSIDE the card rather than the card's own footer. With `margin: 0` it
  // had 1px of inset — the card's border and nothing else — so the two borders
  // landed on each other and `.task-group`'s `overflow: hidden` cropped its
  // corners against the card's radius. It read as a bar wedged in.
  //
  // Geometry, not a stylesheet substring, because this is the defect class the
  // coverage guard cannot see: the rule was present the whole time, its value
  // was wrong. Asserted against the section title's inset rather than a literal
  // pixel count, so the two stay aligned if the card's padding ever changes.
  //
  // Both reachable variants, because they are separate template branches that
  // share one rule — a per-variant margin would otherwise slip through. The
  // third ("All systems healthy") needs an unfiltered board with an empty
  // Needs-attention section, which the fixtures deliberately do not produce.
  const boards = [
    // The scoped stripe, on a narrowed board of the ordinary instance.
    { url: "/tasks?since=2026-08-01&tag=area%3Adata", marker: "[data-attention-scoped]" },
    // The "cannot assess" stripe, which only the truncated instance renders.
    {
      url: `${TRUNCATED_BASE_URL}/tasks?since=2026-08-01`,
      marker: "[data-attention-unknown]",
    },
  ];

  for (const { url, marker } of boards) {
    for (const width of [320, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto(url);

      const stripe = page.locator(marker);
      await expect(stripe).toBeVisible();

      const inset = await stripe.evaluate((node) => {
        const card = node.closest(".task-group")!;
        const s = node.getBoundingClientRect();
        const c = card.getBoundingClientRect();
        const title = card.querySelector("h2, h3")!.getBoundingClientRect();
        return {
          left: Math.round(s.left - c.left),
          right: Math.round(c.right - s.right),
          title: Math.round(title.left - c.left),
        };
      });

      expect(inset.left, `${marker} left inset at ${width}px`).toBe(inset.title);
      expect(inset.right, `${marker} right inset at ${width}px`).toBe(inset.title);
      // Belt and braces: the title itself must be inset, or the assertions
      // above would hold for a stripe welded to a card that has no padding.
      expect(inset.title).toBeGreaterThan(4);
    }
  }
});

test("a page shorter than the viewport still fills it", async ({ page }) => {
  // Browser truth for the body background fix (#44/#46, deduplicated in #54),
  // which until now had no guard but a reviewer's eye on a screenshot: the
  // background propagates to the canvas while staying sized to the BODY box,
  // so a page shorter than the viewport tiled the gradient into hard bands.
  // Assert the box covers the viewport rather than that the rule is present.
  await page.setViewportSize({ width: 1440, height: 800 });
  await page.goto("/note/missing-note");
  await expect(page.getByText("Document not found.")).toBeVisible();

  const { body, viewport } = await page.evaluate(() => ({
    body: document.body.getBoundingClientRect().height,
    viewport: window.innerHeight,
  }));

  expect(body).toBeGreaterThanOrEqual(viewport);
});

test("epic strip rolls the subtree up and scopes the board", async ({ page }) => {
  // T1-S5 item: the demo epic covers six subtree tasks — one completed, one
  // cancelled (cancelled work leaves the denominator), so the chip reads 1/5.
  await page.goto("/tasks?since=2026-08-01");

  const chip = page.locator('[data-epic-strip] [data-epic-chip="influx-epic"]');
  await expect(chip).toBeVisible();
  await expect(chip.locator("[data-epic-progress]")).toHaveText("1/5");

  await chip.click();
  await expect(page).toHaveURL(/epic=influx-epic/);
  // Only the epic's descendants survive the scope.
  await expect(
    page.locator('[data-task-row][data-task-id="influx-backfill"]'),
  ).toBeVisible();
  await expect(
    page.locator('[data-task-row][data-task-id="lens-graph-view"]'),
  ).toHaveCount(0);
});

test("blocked row renders styled blocker chips with a visible label", async ({ page }) => {
  // T1-S2 item: the blocked fixture (influx-backfill, waiting on the cutover)
  // must show a labelled, STYLED chip strip — browser truth via computed style,
  // consistent with the chip system.
  await page.goto("/tasks?since=2026-08-01");

  const blockedRow = page.locator(
    '[data-task-group="blocked"] [data-task-row][data-task-id="influx-backfill"]',
  );
  await expect(blockedRow).toBeVisible();
  const strip = blockedRow.locator("[data-blocker-list]");
  await expect(strip.locator(".blocker-label")).toHaveText("Blocked by");
  const chip = strip.locator(".blocker-chip").first();
  await expect(chip).toContainText("Cut over Influx ingest path");
  const style = await chip.evaluate((el) => {
    const cs = getComputedStyle(el);
    return {
      radius: cs.borderRadius,
      border: cs.borderStyle,
      background: cs.backgroundColor,
    };
  });
  expect(style.radius).toBe("999px");
  expect(style.border).toBe("solid");
  expect(style.background).not.toBe("rgba(0, 0, 0, 0)");
});

test("task.created event inserts a skeleton row on an unfiltered board", async ({ page, request }) => {
  // Drives the REAL SSE path via the fake-mode publish seam: publish ->
  // in-process hub -> /tasks/events -> EventSource -> tasks.js skeleton.
  //
  // Deliberately an UNFILTERED board. The optimistic row is suppressed on a
  // narrowed one (see the next test), so `/tasks` is now the case where it is
  // supposed to appear. This test doubles as the positive control for that
  // one: it proves the publish seam and the client's event path work, so the
  // absence asserted there is a real suppression and not a dead pipeline.
  await page.goto("/tasks");
  await expect(page.locator('[data-live-state="live"]')).toBeVisible();

  const publish = await request.post("/tasks/events/publish", {
    data: {
      id: `evt-e2e-${Date.now()}`,
      type: "task.created",
      task_id: "e2e-just-created",
      payload: { title: "Freshly created task" },
      // Hold reconciliation off so the skeleton is deterministically
      // observable; the reconcile path is covered by its own ~800ms flow.
      requires_refresh: false,
    },
  });
  expect(publish.status()).toBe(202);

  const skeleton = page.locator(
    '[data-task-list="pending"] [data-task-row][data-task-id="e2e-just-created"]',
  );
  await expect(skeleton).toBeVisible();
  await expect(skeleton).toContainText("Freshly created task");
  // The link carries the board's query string, the way every other detail
  // link on the page does.
  await expect(skeleton.locator("a.task-title")).toHaveAttribute(
    "href",
    "/tasks/e2e-just-created",
  );
});

test("the optimistic skeleton is suppressed on a filtered board", async ({ page, request }) => {
  // a3fd5f01: `task.created` carries no tags, no project and no creator, so
  // the client cannot evaluate the new task against the active scope. It used
  // to insert the row anyway — asserting membership on a board that never
  // checked it, and persisting if the ~800ms reconcile failed.
  await page.goto("/tasks?tag=area%3Adata");
  await expect(page.locator('[data-live-state="live"]')).toBeVisible();

  const stamp = Date.now();
  const created = await request.post("/tasks/events/publish", {
    data: {
      id: `evt-e2e-created-${stamp}`,
      type: "task.created",
      task_id: "e2e-out-of-scope",
      payload: { title: "Out of scope task" },
      requires_refresh: false,
    },
  });
  expect(created.status()).toBe(202);

  // POSITIVE CONTROL, and the reason this absence assertion means something.
  // A second event, published AFTER the first, whose effect IS visible on this
  // board. One EventSource delivers both in order through one queue, so once
  // the claim chip appears the `task.created` above has demonstrably been
  // processed and declined — rather than still being in flight, or dropped by
  // a broken pipeline, which is how an absence assertion passes for the wrong
  // reason.
  const claimed = await request.post("/tasks/events/publish", {
    data: {
      id: `evt-e2e-claimed-${stamp}`,
      type: "task.claimed",
      task_id: "influx-ingest-cutover",
      payload: { aspect: "e2e-probe", agent: "e2e-runner" },
      requires_refresh: false,
    },
  });
  expect(claimed.status()).toBe(202);

  await expect(
    page.locator('[data-task-row][data-task-id="influx-ingest-cutover"] [data-claim-aspect="e2e-probe"]'),
  ).toHaveCount(1);

  // Only now is this meaningful.
  await expect(
    page.locator('[data-task-row][data-task-id="e2e-out-of-scope"]'),
  ).toHaveCount(0);
});

test("clicking a task opens its detail page", async ({ page }) => {
  await page.goto("/tasks?since=2026-08-01");

  await page.getByRole("link", { name: "Cut over Influx ingest path" }).click();

  await expect(page).toHaveURL(/\/tasks\/influx-ingest-cutover/);
  await expect(
    page.locator('[data-task-detail="influx-ingest-cutover"]'),
  ).toBeVisible();
  // The claimed fixture surfaces its active claim on the detail page. The
  // agent appears in both the summary line and the claims list, so scope to
  // the first match rather than tripping strict mode.
  await expect(page.getByText("worker-a").first()).toBeVisible();
});

test("knowledge note renders server-side markdown", async ({ page }) => {
  await page.goto("/note/note-influx-plan");

  // Since K1-S1 the body is rendered markdown, so the fixture's `# Influx
  // migration plan` yields a second h1 inside .markdown-body — scope the
  // page-title assertion to the article header instead of tripping strict
  // mode on the duplicate.
  await expect(
    page.locator("article header").getByRole("heading", { name: "Influx migration plan" }),
  ).toBeVisible();
  // And the markdown really rendered (list items, not a plaintext <pre>).
  await expect(
    page.locator(".markdown-body").getByRole("listitem").first(),
  ).toContainText("Stage 1: dual-write");
});

test("knowledge note renders metadata chips, lede and authorship", async ({ page }) => {
  // K1-S3: frontmatter drives the chip row, lede and authorship line.
  await page.goto("/note/note-influx-plan");

  const chips = page.locator(".note-chips");
  await expect(chips).toBeVisible();
  await expect(chips.locator(".note-type")).toHaveText("summary");
  await expect(chips.locator(".note-status")).toHaveText("active");
  // Scope shows because the fixture is NOT "shared" (shared renders no chip).
  await expect(chips.locator(".note-scope")).toHaveText("task");
  await expect(chips.locator(".note-namespace")).toHaveText("plans");
  await expect(chips.locator(".note-confidence")).toHaveText("confidence 90%");
  await expect(chips.locator(".note-supersedes a")).toHaveAttribute(
    "href",
    "/note/note-influx-legacy-ingest",
  );

  await expect(page.locator(".note-lede")).toContainText(
    "Cut ingest over first, backfill after",
  );
  await expect(page.locator(".note-authorship")).toContainText("By worker-a");
});

test("clicking a note tag opens the filtered knowledge landing", async ({ page }) => {
  await page.goto("/note/note-influx-plan");

  await page
    .locator("article .tag-list a", { hasText: "kind: plan" })
    .first()
    .click();

  await expect(page).toHaveURL(/\/knowledge\?tag=kind%3Aplan/);
  // The filtered landing renders and lists the tagged fixture notes.
  await expect(
    page.getByRole("link", { name: "Influx migration plan" }),
  ).toBeVisible();
});

test("quarantined note is visibly quarantined (computed style)", async ({ page }) => {
  await page.goto("/note/note-influx-legacy-ingest");

  const chip = page.locator(".note-status-quarantined");
  await expect(chip).toBeVisible();
  await expect(chip).toHaveText("quarantined");
  // Browser truth, not stylesheet substrings: the rule actually applies.
  const style = await chip.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { weight: cs.fontWeight, background: cs.backgroundColor };
  });
  expect(style.weight).toBe("700");
  expect(style.background).not.toBe("rgba(0, 0, 0, 0)");
});

test("knowledge note renders the related panel with edge badges", async ({ page }) => {
  // K1-S4: the note page carries a related <aside> fed by one lithos_related
  // call; the fixtures give the plan note a link, an unresolved contradicts
  // edge, and an unresolved provenance stub.
  await page.goto("/note/note-influx-plan");

  const panel = page.getByRole("complementary", { name: "Related notes" });
  await expect(panel).toBeVisible();
  await expect(
    panel.getByRole("link", { name: "Influx rollback route" }).first(),
  ).toBeVisible();
  await expect(panel.locator(".edge-direction")).toHaveText("incoming");
  await expect(panel.locator(".edge-conflict")).toHaveText("conflict: unresolved");
});

test("missing knowledge note shows the not-found banner", async ({ page }) => {
  // The fixture finding "finding-orphan" links knowledge_id=missing-note;
  // opening it must hit the real doc_not_found path, not a generic failure.
  await page.goto("/note/missing-note");

  await expect(page.getByText("Document not found.")).toBeVisible();
});

test("live-updates status banner is present on the dashboard", async ({ page }) => {
  await page.goto("/tasks?since=2026-08-01");
  await expect(page.locator("[data-live-status]")).toBeVisible();
});

test("a slow render does not let the next reconcile overlap it", async ({ page, request }) => {
  // 7e2a1ed1: `refreshFragments` had a `latestRefreshToken` guard that
  // discarded a stale RESULT — after the server had already rendered it. Every
  // per-request bound on the server is per-INVOCATION, so two overlapping
  // reconciles each get their own full fan-out allowance, and LithosClient
  // holds ONE MCP session for the whole process, so that contention degrades
  // every surface rather than just this page.
  //
  // Asserted on REQUESTS ISSUED, which is the thing that costs a render.
  // Determinism comes from HOLDING the first refresh open for the whole test,
  // not from racing it: while it is held, a second request either was issued
  // or was not. The waits below only have to exceed the 800ms reconcile
  // debounce, so they are a bound, not a race.
  await page.goto("/tasks");
  await expect(page.locator('[data-live-state="live"]')).toBeVisible();

  let issued = 0;
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });

  await page.route("**/tasks**", async (route) => {
    if (route.request().headers()["x-lithos-lens-refresh"] !== "tasks") {
      await route.continue();
      return;
    }
    issued += 1;
    if (issued === 1) {
      // Held open for the first half of the test, then FAILED — a rejected
      // render is the case where coalescing is easiest to get wrong.
      await held;
      await route.abort();
      return;
    }
    await route.continue();
  });

  const nudge = async (n: number) => {
    const response = await request.post("/tasks/events/publish", {
      data: {
        id: `evt-e2e-reconcile-${Date.now()}-${n}`,
        type: "finding.posted",
        task_id: "influx-ingest-cutover",
        payload: {},
        requires_refresh: true,
      },
    });
    expect(response.status()).toBe(202);
  };

  try {
    await nudge(1);
    await page.waitForTimeout(1600);
    // The first render is still held open here. Without the in-flight guard
    // this second reconcile fires its own fetch alongside it.
    await nudge(2);
    await page.waitForTimeout(1600);

    expect(issued).toBe(1);

    // Now fail the held render. The reconcile that was queued behind it must
    // still happen: coalescing that only survives SUCCESS is half a guarantee,
    // and the board would otherwise sit stale until the 30s poll.
    release();
    await expect.poll(() => issued, { timeout: 5000 }).toBe(2);
  } finally {
    release();
  }
});

test("a skeleton link does not propagate a retired query param", async ({ page, request }) => {
  // `claimed_state` is parsed away and never read, so it is NOT a preserved
  // filter — the board is unfiltered and the optimistic row is allowed. Its
  // link must still come out bare: every other detail link re-emits filters
  // through an allowlist, so a retired param stops at the link rather than
  // propagating (test_legacy_claimed_state_bookmark_does_not_propagate_through_navigation).
  // This row must not be the one exception.
  await page.goto("/tasks?claimed_state=legacy");
  await expect(page.locator('[data-live-state="live"]')).toBeVisible();

  const publish = await request.post("/tasks/events/publish", {
    data: {
      id: `evt-e2e-retired-${Date.now()}`,
      type: "task.created",
      task_id: "e2e-retired-param",
      payload: { title: "Created under a legacy bookmark" },
      requires_refresh: false,
    },
  });
  expect(publish.status()).toBe(202);

  const skeleton = page.locator(
    '[data-task-list="pending"] [data-task-row][data-task-id="e2e-retired-param"]',
  );
  await expect(skeleton).toBeVisible();
  await expect(skeleton.locator("a.task-title")).toHaveAttribute(
    "href",
    "/tasks/e2e-retired-param",
  );
});
