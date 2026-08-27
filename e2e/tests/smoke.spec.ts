import { test, expect } from "@playwright/test";

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

test("task.created event inserts a skeleton row in the pending strip", async ({ page, request }) => {
  // Drives the REAL SSE path via the fake-mode publish seam: publish ->
  // in-process hub -> /tasks/events -> EventSource -> tasks.js skeleton.
  await page.goto("/tasks?since=2026-08-01");
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

test("a finding event reconciles the whole detail page, not just its timeline", async ({
  page,
  request,
}) => {
  // The browser half of the reopened-marker lifecycle (T1-S7 review round 2,
  // correctness/f-001). A reopen reaches an open page ONLY as finding.posted —
  // `lithos_task_reopen` leaves no other trace and `task.reopened` is not a
  // type Lens subscribes to yet — but the marker it produces renders in the
  // header, outside the findings fragment. When the finding event refreshed
  // that fragment ALONE, no request for the page itself ever followed, so the
  // header kept a stale status, Resolution block and missing marker for as
  // long as the tab stayed open. This asserts the requests the marker depends
  // on actually happen: the fast fragment path AND the floored whole-page
  // reconcile.
  const detailPath = "/tasks/influx-ingest-cutover";
  const reconciles: string[] = [];
  const fragments: string[] = [];
  page.on("request", (req) => {
    // resourceType filters out the navigation itself: the reconcile refetches
    // the same URL, but as a fetch.
    if (req.resourceType() !== "fetch") return;
    const path = new URL(req.url()).pathname;
    if (path === detailPath) reconciles.push(path);
    if (path === `${detailPath}/findings`) fragments.push(path);
  });

  await page.goto(detailPath);
  await expect(page.locator(`[data-task-detail="influx-ingest-cutover"]`)).toBeVisible();

  // The detail page carries no live-status chip, so there is nothing to wait
  // on for the EventSource handshake: publish until one lands. Each publish
  // re-arms the debounce, so the interval is wider than it (~800ms).
  let published = 0;
  await expect
    .poll(
      async () => {
        const response = await request.post("/tasks/events/publish", {
          data: {
            id: `evt-e2e-finding-${Date.now()}-${published++}`,
            type: "finding.posted",
            task_id: "influx-ingest-cutover",
            payload: {},
          },
        });
        expect(response.status()).toBe(202);
        return reconciles.length;
      },
      { timeout: 25000, intervals: [1500] },
    )
    .toBeGreaterThan(0);

  // ...and the cheap path ran too: both, each on its own floor.
  expect(fragments.length).toBeGreaterThan(0);
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
