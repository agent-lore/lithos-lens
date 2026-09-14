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

  await expect(
    page.getByRole("heading", { level: 1, name: "Tasks" }),
  ).toBeVisible();

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
  // The graph cluster's "resolved predecessor inside the window" fixture is
  // deliberately inside this window too, so each terminal group carries two.
  await expect(
    completed.locator('[data-task-row][data-task-id="loom-design-done"]'),
  ).toHaveAttribute("data-task-status", "completed");
  expect(await completed.locator("[data-task-row]").count()).toBe(2);

  const cancelled = page.locator('[data-task-group="cancelled"]');
  await expect(
    cancelled.locator('[data-task-row][data-task-id="influx-spike"]'),
  ).toHaveAttribute("data-task-status", "cancelled");
  await expect(
    cancelled.getByRole("link", { name: "Spike Influx client options" }),
  ).toBeVisible();
  await expect(
    cancelled.locator('[data-task-row][data-task-id="loom-cancelled-pred"]'),
  ).toHaveAttribute("data-task-status", "cancelled");
  expect(await cancelled.locator("[data-task-row]").count()).toBe(2);
});

test("the gates section counts its timer gate down in the browser", async ({
  page,
  request,
}) => {
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

  const row = page.locator('[data-task-row][data-task-id="influx-dashboards"]');
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

test("nothing marked hidden is painted, anywhere on the page", async ({
  page,
}) => {
  // The generic complement to the test above, which names two elements. This
  // one sweeps EVERY `[hidden]` element, so a future component rule that sets
  // `display` on a class is caught wherever it lands rather than only on the
  // two the reset was written for. From T1-S6, which found the original bug.
  await page.goto("/tasks?since=2026-08-01");

  const painted = await page
    .locator("[hidden]")
    .evaluateAll((elements) =>
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
    {
      url: "/tasks?since=2026-08-01&tag=area%3Adata",
      marker: "[data-attention-scoped]",
    },
    // The "cannot assess" stripe, which only the truncated instance renders —
    // and only where the Needs-attention list is EMPTY while the tail still
    // has a row: an empty list is what the three stripe branches choose
    // between. The `area:archive` slice is that state, and honestly so, since
    // the row it would flag (unsatisfiable) is the one the capped blocked read
    // did not return.
    {
      url: `${TRUNCATED_BASE_URL}/tasks?since=2026-08-01&tag=area%3Aarchive`,
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

      expect(inset.left, `${marker} left inset at ${width}px`).toBe(
        inset.title,
      );
      expect(inset.right, `${marker} right inset at ${width}px`).toBe(
        inset.title,
      );
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

test("epic strip rolls the subtree up and scopes the board", async ({
  page,
}) => {
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

test("blocked row renders styled blocker chips with a visible label", async ({
  page,
}) => {
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

test("task.created event inserts a skeleton row on an unfiltered board", async ({
  page,
  request,
}) => {
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

  // …and the optimistic row takes part in the side panel like every other row
  // on the board (§5.5). It is the ONE row no template rendered, so its half
  // of the contract is written by tasks.js — through the query-alias route,
  // the only form the browser may build for an arbitrary id. Without it the
  // row is still visible and still clickable, and the click navigates away
  // instead of opening the panel.
  await expect(skeleton).toHaveAttribute(
    "data-panel-url",
    "/tasks/id?task_id=e2e-just-created&fragment=panel",
  );
  const fragment = page.waitForRequest(
    (request) =>
      new URL(request.url()).searchParams.get("fragment") === "panel",
  );

  await skeleton.locator("a.task-title").click();

  expect(new URL((await fragment).url()).searchParams.get("task_id")).toBe(
    "e2e-just-created",
  );
  // The board is still here — a navigation would have replaced it — and the
  // selection is on the URL. The panel itself is the NOT-FOUND one: the id
  // came off an event and no such task exists in the fixture, which is the
  // panel this route is specified to answer with and not an HTTP 500.
  await expect(page).toHaveURL(/selected=e2e-just-created/);
  await expect(page.locator(".task-board")).toBeVisible();
  await expect(page.locator("[data-task-panel]")).toBeVisible();
});

test("the optimistic skeleton is suppressed on a filtered board", async ({
  page,
  request,
}) => {
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
    page.locator(
      '[data-task-row][data-task-id="influx-ingest-cutover"] [data-claim-aspect="e2e-probe"]',
    ),
  ).toHaveCount(1);

  // Only now is this meaningful.
  await expect(
    page.locator('[data-task-row][data-task-id="e2e-out-of-scope"]'),
  ).toHaveCount(0);
});

test("clicking a task opens its panel, and Expand opens the full page", async ({
  page,
}) => {
  await page.goto("/tasks?since=2026-08-01");

  await page.getByRole("link", { name: "Cut over Influx ingest path" }).click();

  // §5.5 (T2-A6): a row click opens the side panel and pushes `selected` onto
  // the URL — the operator keeps their place in the list. The full page is
  // what Expand is for, and this is the transition the PRD's problem statement
  // is about ("every row click is a page navigation").
  await expect(
    page.locator('[data-panel-task="influx-ingest-cutover"]'),
  ).toBeVisible();
  await expect(page).toHaveURL(/selected=influx-ingest-cutover/);
  await expect(page.locator(".task-board")).toBeVisible();
  // The panel answers both directions of the relationship, not just upstream.
  await expect(
    page.locator(
      '[data-panel-dependents] [data-link-target="influx-backfill"]',
    ),
  ).toBeVisible();

  await page.locator("[data-panel-expand]").click();

  await expect(page).toHaveURL(/\/tasks\/influx-ingest-cutover/);
  await expect(
    page.locator('[data-task-detail="influx-ingest-cutover"]'),
  ).toBeVisible();
  // The claimed fixture surfaces its active claim on the detail page. The
  // agent appears in both the summary line and the claims list, so scope to
  // the first match rather than tripping strict mode.
  await expect(page.getByText("worker-a").first()).toBeVisible();
});

test("clicking a row away from its links opens the panel too", async ({
  page,
}) => {
  // §5.5's contract is "clicking a ROW opens a panel" — the title link is the
  // obvious target, not the whole of it. This clicks the row's meta line,
  // which carries badges and timestamps and no anchor at all.
  await page.goto("/tasks?since=2026-08-01");
  const row = page.locator('[data-task-row][data-task-id="influx-backfill"]');
  await expect(row).toBeVisible();

  await row.locator(".task-row-meta").click();

  await expect(
    page.locator('[data-panel-task="influx-backfill"]'),
  ).toBeVisible();
  await expect(page).toHaveURL(/selected=influx-backfill/);
});

test("clicking a gate row opens its panel, and the waiter list still opens", async ({
  page,
}) => {
  // §5.5's "clicking a row opens a panel" covers the Gates section too: a gate
  // is a task, and "what is this gate holding up?" is the Blocks list the
  // panel already answers. Gate rows carry gate chrome rather than claim
  // chrome, so they do not carry `data-task-row` — which is exactly how the
  // section fell out of the shared click contract.
  await page.goto("/tasks?since=2026-08-01");
  const gate = page.locator(
    '[data-gate-row][data-task-id="influx-read-swap-approval"]',
  );
  await expect(gate).toBeVisible();

  // The waiter disclosure is a <details> that works with no JS, so the row
  // handler must leave its <summary> alone. Toggled BEFORE the panel click:
  // a handler that swallowed it would both fail to expand the list and open
  // the panel early.
  const waiters = gate.locator("[data-gate-waiters]");
  await waiters.locator("summary").click();
  await expect(waiters.locator(".gate-waiter-list")).toBeVisible();
  await expect(page).not.toHaveURL(/selected=/);

  await gate.locator(".task-title").click();

  await expect(
    page.locator('[data-panel-task="influx-read-swap-approval"]'),
  ).toBeVisible();
  await expect(page).toHaveURL(/selected=influx-read-swap-approval/);
  // Still the board — a gate click used to be a full-page navigation.
  await expect(page.locator(".task-board")).toBeVisible();
});

test("a panel fetch that fails leaves the board exactly as it was", async ({
  page,
}) => {
  // The real failure path, in a real browser: the fragment request is aborted,
  // so `fetch` REJECTS rather than answering. A click pushes its URL only on
  // success, so nothing may move — and nothing may be left dangling either.
  await page.goto("/tasks?since=2026-08-01");
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  // Matched by predicate, not by glob: `fragment=panel` sits in the QUERY, and
  // a `**/…` pattern only matches after a path separator.
  const isPanelFragment = (url: URL) =>
    url.searchParams.get("fragment") === "panel";
  let aborted = 0;
  await page.route(isPanelFragment, (route) => {
    aborted += 1;
    return route.abort();
  });

  // Armed BEFORE the click, and awaited after it: the failing REQUEST is what
  // this test synchronises on. Every assertion below is also true in the
  // instant after the click and before the request fails, so a wall-clock
  // sleep would let the test pass on a run where the route never matched at
  // all — and would inspect `errors` too early on a slow worker. Waiting on
  // the event itself removes both.
  const requestFailed = page.waitForEvent("requestfailed", (request) =>
    isPanelFragment(new URL(request.url())),
  );

  await page.getByRole("link", { name: "Cut over Influx ingest path" }).click();
  await requestFailed;
  expect(aborted).toBe(1);
  // The rejection reaches the page's own handlers in the microtask checkpoint
  // that follows the failure, so one frame later it has either been caught or
  // been reported. A double rAF is how the rest of this suite waits for the
  // page to settle, and it costs a round trip, which is what orders the
  // `pageerror` delivery below against this assertion.
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );

  await expect(page.locator("[data-task-panel]")).toHaveCount(0);
  await expect(page).toHaveURL(/\/tasks\?since=2026-08-01$/);
  await expect(page.locator(".task-board")).toBeVisible();
  expect(errors).toEqual([]);
});

test("a board section anchor survives opening and closing the panel", async ({
  page,
}) => {
  // The summary cards link to a SECTION of the board, so `#task-group-blocked`
  // is generated dashboard state that says where the operator is. Driven from
  // the card rather than typed into `goto`, so the anchor under test is the
  // one the app actually emits.
  await page.goto("/tasks?since=2026-08-01");
  await page.locator('a.metric-card[href$="#task-group-blocked"]').click();
  await expect(page).toHaveURL(/#task-group-blocked$/);

  const row = page.locator('[data-task-row][data-task-id="influx-backfill"]');
  await row.locator(".task-title").click();

  await expect(
    page.locator('[data-panel-task="influx-backfill"]'),
  ).toBeVisible();
  await expect(page).toHaveURL(/selected=influx-backfill#task-group-blocked$/);

  await page.locator("[data-panel-close]").click();

  // Only the selection cleared. The anchor is list state like any filter.
  await expect(page.locator("[data-task-panel]")).toHaveCount(0);
  await expect(page).not.toHaveURL(/selected=/);
  await expect(page).toHaveURL(/#task-group-blocked$/);
});

test("closing the side panel keeps the board's filters", async ({ page }) => {
  // The no-JS baseline first: `?selected=` renders the panel open server-side.
  await page.goto("/tasks?project=influx&selected=influx-backfill");
  await expect(
    page.locator('[data-panel-task="influx-backfill"]'),
  ).toBeVisible();

  await page.locator("[data-panel-close]").click();

  // Closing clears the selection and NOTHING else: the project scope, and the
  // board under it, are exactly where they were.
  await expect(page.locator("[data-task-panel]")).toHaveCount(0);
  await expect(page).toHaveURL(/\/tasks\?project=influx$/);
  await expect(page.locator(".task-board")).toBeVisible();
});

test("PR reconciliation tones are visibly distinct colours (computed style)", async ({
  page,
}) => {
  // T2b: "one colour per state" is an acceptance criterion, and every other
  // test of it reads a Python tone token or a class name — all of which stay
  // green if the stylesheet is deleted, if danger and ok are swapped, or if
  // every tone is given the same declarations. This asserts browser truth:
  // what the page actually paints.
  await page.goto("/tasks?since=2026-08-01");

  const styleOf = (locator: ReturnType<typeof page.locator>) =>
    locator.evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        color: cs.color,
        background: cs.backgroundColor,
        borderColor: cs.borderTopColor,
        borderStyle: cs.borderTopStyle,
        weight: cs.fontWeight,
      };
    });

  // Channels of an "rgb(r, g, b)" / "rgba(...)" computed value.
  const rgb = (value: string) =>
    value.match(/\d+/g)!.slice(0, 3).map(Number) as [number, number, number];

  // The two states the demo board actually carries, in the two places they
  // render: the promoted attention row and the Gates section.
  const danger = page.locator('[data-reconciliation-state="needs_human"]');
  const ok = page.locator('[data-reconciliation-state="ready_to_merge"]');
  await expect(danger).toBeVisible();
  await expect(ok).toBeVisible();

  const dangerStyle = await styleOf(danger);
  const okStyle = await styleOf(ok);
  const [dr, dg, db] = rgb(dangerStyle.color);
  const [orr, og, ob] = rgb(okStyle.color);
  // Red reads red and green reads green — the two states an operator must not
  // confuse, asserted as hue rather than as a class name.
  expect(dr).toBeGreaterThan(dg);
  expect(dr).toBeGreaterThan(db);
  expect(og).toBeGreaterThan(orr);
  expect(og).toBeGreaterThan(ob);
  expect(dangerStyle.color).not.toBe(okStyle.color);

  // Every tone in the mapping, probed on this page so the assertions run
  // against the stylesheet the app actually served. The badge markup is the
  // template's, so a probe cannot pass a class the page could not produce.
  const tones = [
    "danger",
    "warn",
    "behind-is-warn-too",
    "info",
    "neutral",
    "ok",
    "unknown",
  ];
  const probes = await page.evaluate((toneList) => {
    const host = document.createElement("div");
    host.id = "tone-probes";
    document.body.appendChild(host);
    const read: Record<string, Record<string, string>> = {};
    for (const tone of toneList) {
      const span = document.createElement("span");
      span.className = `badge badge-reconciliation badge-reconciliation-${tone}`;
      span.textContent = tone;
      host.appendChild(span);
      const cs = getComputedStyle(span);
      read[tone] = {
        color: cs.color,
        background: cs.backgroundColor,
        borderColor: cs.borderTopColor,
        borderStyle: cs.borderTopStyle,
        weight: cs.fontWeight,
      };
    }
    return read;
  }, tones);

  // `behind-is-warn-too` is not a tone: it is the control. An undefined tone
  // suffix must fall through to the base badge, so if it matched any real
  // tone's declarations the "distinct per tone" assertion below would be
  // meaningless.
  const control = probes["behind-is-warn-too"];
  const real = ["danger", "warn", "info", "neutral", "ok", "unknown"];
  for (const tone of real) {
    expect(JSON.stringify(probes[tone])).not.toBe(JSON.stringify(control));
  }
  // …and no two tones paint the same. (neutral and unknown share the muted
  // text colour on purpose; the dashed border and the weight are what say
  // "this word is not one I know", which is why the whole declaration set is
  // compared rather than the colour alone.)
  const painted = real.map((tone) => JSON.stringify(probes[tone]));
  expect(new Set(painted).size).toBe(real.length);

  // The rendered badges are painted BY these tone rules, not by something else
  // that happens to look right.
  expect(JSON.stringify(dangerStyle)).toBe(JSON.stringify(probes.danger));
  expect(JSON.stringify(okStyle)).toBe(JSON.stringify(probes.ok));

  // Per-tone hue, where the tone carries one: amber warns on its background,
  // blue is blue, and grey is grey — an unknown state must never arrive
  // wearing a real state's colour.
  const [wr, wg, wb] = rgb(probes.warn.background);
  expect(wr).toBeGreaterThan(wb);
  expect(wg).toBeGreaterThan(wb);
  const [ir, , ib] = rgb(probes.info.color);
  expect(ib).toBeGreaterThan(ir);
  const grey = rgb(probes.unknown.color);
  expect(Math.max(...grey) - Math.min(...grey)).toBeLessThan(24);
  expect(probes.unknown.borderStyle).toBe("dashed");
});

test("knowledge note renders server-side markdown", async ({ page }) => {
  await page.goto("/note/note-influx-plan");

  // Since K1-S1 the body is rendered markdown, so the fixture's `# Influx
  // migration plan` yields a second h1 inside .markdown-body — scope the
  // page-title assertion to the article header instead of tripping strict
  // mode on the duplicate.
  await expect(
    page
      .locator("article header")
      .getByRole("heading", { name: "Influx migration plan" }),
  ).toBeVisible();
  // And the markdown really rendered (list items, not a plaintext <pre>).
  await expect(
    page.locator(".markdown-body").getByRole("listitem").first(),
  ).toContainText("Stage 1: dual-write");
});

test("knowledge note renders metadata chips, lede and authorship", async ({
  page,
}) => {
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

test("clicking a note tag opens the filtered knowledge landing", async ({
  page,
}) => {
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

test("quarantined note is visibly quarantined (computed style)", async ({
  page,
}) => {
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

test("knowledge note renders the related panel with edge badges", async ({
  page,
}) => {
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
  await expect(panel.locator(".edge-conflict")).toHaveText(
    "conflict: unresolved",
  );
});

test("missing knowledge note shows the not-found banner", async ({ page }) => {
  // The fixture finding "finding-orphan" links knowledge_id=missing-note;
  // opening it must hit the real doc_not_found path, not a generic failure.
  await page.goto("/note/missing-note");

  await expect(page.getByText("Document not found.")).toBeVisible();
});

test("live-updates status banner is present on the dashboard", async ({
  page,
}) => {
  await page.goto("/tasks?since=2026-08-01");
  await expect(page.locator("[data-live-status]")).toBeVisible();
});

test("a slow render does not let the next reconcile overlap it", async ({
  page,
  request,
}) => {
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

test("a skeleton link does not propagate a retired query param", async ({
  page,
  request,
}) => {
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

// ── The graph canvas (T2-A4), driven against the real Cytoscape ─────────────
//
// `tests/test_tasks_js.py` pins this behaviour against a stubbed library,
// which is where the interleavings and the "no fetch" assertions live. These
// drive the vendored 3.30.3 bundle itself, because "the toggle works" and "the
// toggle works with the library we actually ship" are different claims.

test("the canvas draws the graph and collapses the text behind a toggle", async ({
  page,
}) => {
  await page.goto("/tasks/graph?project=lithos-loom");
  const canvas = page.locator('[data-graph-canvas][data-canvas-state="ready"]');
  await expect(canvas).toBeVisible();

  // The text baseline is collapsed but present — D3's promise, and the reason
  // a screen reader and a PR screenshot still get the whole page.
  await expect(page.locator("[data-graph-layers]")).toBeHidden();
  await expect(page.locator('[data-graph-layer="4"]')).toBeAttached();
  await page.locator("[data-toggle-text]").click();
  await expect(page.locator("[data-graph-layers]")).toBeVisible();

  // The legend is persistent: it explains the arrowheads, so it never goes
  // away with the text.
  await expect(page.locator("[data-graph-legend]")).toBeVisible();
});

test("toggling the overlays adds their edges and remembers them in the URL", async ({
  page,
}) => {
  await page.goto("/tasks/graph?project=lithos-loom");
  await expect(
    page.locator('[data-graph-canvas][data-canvas-state="ready"]'),
  ).toBeVisible();

  const types = () =>
    page.evaluate(() =>
      (window as any).LithosLensGraph.shown().edges.map((edge: any) => edge.type),
    );

  // Default: dependency flow only, hierarchy and provenance switched off even
  // though both are already in the payload (D6/D8).
  expect(await types()).not.toContain("parent_child");
  expect(await types()).not.toContain("discovered_from");

  await page.locator('[data-toggle-overlay="hierarchy"]').click();
  await expect(page).toHaveURL(/overlays=hierarchy/);
  expect(await types()).toContain("parent_child");

  await page.locator('[data-toggle-overlay="provenance"]').click();
  expect(await types()).toContain("discovered_from");
  // The context ghost the provenance edge points from: a task resolved outside
  // this open-only scope, in the payload from the first render so the toggle
  // needs no fetch.
  const source = await page.evaluate(() =>
    (window as any).LithosLensGraph.node("loom-research-old").style("display"),
  );
  expect(source).not.toBe("none");

  // Back walks the exploration without a reload, re-applying the URL's
  // overlays from the same static payload.
  await page.goBack();
  expect(await types()).not.toContain("discovered_from");
  await page.goBack();
  expect(await types()).not.toContain("parent_child");
  await expect(page).not.toHaveURL(/overlays=/);
});

test("clicking a node opens that task's panel beside the canvas and pushes focus", async ({
  page,
}) => {
  await page.goto("/tasks/graph?project=lithos-loom");
  await expect(
    page.locator('[data-graph-canvas][data-canvas-state="ready"]'),
  ).toBeVisible();

  // The node is drawn on a canvas, so the click goes through Cytoscape's own
  // event surface rather than a DOM row — which is the whole reason the panel
  // is reachable as an API (D9: one implementation for rows and nodes).
  await page.evaluate(() =>
    (window as any).LithosLensGraph.node("loom-ship").emit("tap"),
  );

  await expect(
    page.locator('[data-panel-host] [data-panel-task="loom-ship"]'),
  ).toBeVisible();
  await expect(page).toHaveURL(/focus=loom-ship/);

  // Close clears the selection and nothing else: the scope survives, and the
  // node stops being lit. `pushState` fires no `popstate`, so the canvas only
  // learns of this because the panel announces it (round-1 correctness f-002).
  await page.locator("[data-panel-host] [data-panel-close]").click();
  await expect(page.locator("[data-panel-host] [data-task-panel]")).toHaveCount(0);
  await expect(page).not.toHaveURL(/focus=/);
  await expect(page).toHaveURL(/project=lithos-loom/);
  const lit = await page.evaluate(
    () => (window as any).LithosLensGraph.cy.nodes(".focused").length,
  );
  expect(lit).toBe(0);
});

test("the canvas ranks every node by the layer the text gives it", async ({
  page,
}) => {
  // D3, in the browser that ships it: the picture is printed above the text
  // layers, and the two may not disagree. Cytoscape's breadth-first ranks by
  // SHORTEST path while the server layers by longest, so this is the claim a
  // layout left to its own devices gets wrong (round-1 correctness f-001).
  await page.goto("/tasks/graph?project=lithos-loom");
  await expect(
    page.locator('[data-graph-canvas][data-canvas-state="ready"]'),
  ).toBeVisible();

  const bands = await page.evaluate(() => {
    const payload = JSON.parse(
      document.querySelector("[data-graph-payload]")!.textContent!,
    );
    const graph = (window as any).LithosLensGraph;
    const out: Record<string, { min: number; max: number }> = {};
    payload.nodes.forEach((node: any) => {
      const y = graph.node(node.id).position().y;
      const band = out[node.layer] || (out[node.layer] = { min: y, max: y });
      band.min = Math.min(band.min, y);
      band.max = Math.max(band.max, y);
    });
    return out;
  });

  // A cycle's members stack inside one slot, so a layer holding one spans a
  // band rather than a line — but the bands stay ordered and disjoint.
  const layers = Object.keys(bands)
    .map(Number)
    .sort((a, b) => a - b);
  expect(layers.length).toBeGreaterThan(2);
  for (let i = 1; i < layers.length; i += 1) {
    expect(bands[layers[i - 1]].max).toBeLessThan(bands[layers[i]].min);
  }
});

test("the focused panel is there with no JavaScript at all", async ({ browser }) => {
  // D9's baseline, and the only check that can prove it: with scripting off
  // there is no canvas to click, so a panel the CLIENT creates is no baseline
  // at all. The text page has to be complete on its own here too.
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  try {
    await page.goto("/tasks/graph?project=lithos-loom&focus=loom-ship");
    await expect(
      page.locator('[data-panel-host] [data-panel-task="loom-ship"]'),
    ).toBeVisible();
    // The canvas never appears, and the text baseline is not collapsed behind
    // a toggle only JavaScript can operate.
    await expect(page.locator("[data-graph-canvas]")).toBeHidden();
    await expect(page.locator("[data-graph-layers]")).toBeVisible();
    await expect(page.locator("[data-toggle-text]")).toBeHidden();
    // And Close is an ordinary link back to the unfocused graph.
    await expect(
      page.locator("[data-panel-host] [data-panel-close]"),
    ).toHaveAttribute("href", /project=lithos-loom/);
  } finally {
    await context.close();
  }
});

test("the event stream waits for the deferred scripts that subscribe to it", async ({
  page,
}) => {
  // Round-4 correctness f-008, in the browser it was found in. The graph page
  // loads `tasks.js`, then a ~400KB Cytoscape bundle, then `graph.js` — and
  // `graph.js` is what subscribes for the "graph changed" pill. A stream opened
  // at the end of `tasks.js` consumes (and deduplicates) a matching event while
  // the library is still in flight, with nothing to replay it to.
  //
  // So the claim under test is a SEQUENCE: `/tasks/events` must not be
  // requested until every deferred script has run.
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });

  // EventSource CONSTRUCTIONS, not network requests. Probed both ways: a page
  // that connects twice (`connect()` closes the first stream and opens a
  // second) shows two constructions and still only one request in Playwright's
  // request log, so the request count cannot tell the two apart and the thing
  // actually under test is the construction.
  await page.addInitScript(() => {
    const Real = window.EventSource;
    (window as any).__streams = [];
    class Counting extends Real {
      constructor(url: string | URL, init?: EventSourceInit) {
        super(url, init);
        (window as any).__streams.push(String(url));
      }
    }
    (window as any).EventSource = Counting;
  });
  const streams = () =>
    page.evaluate(() => ((window as any).__streams || []).length);

  await page.route("**/vendor/cytoscape.min.js", async (route) => {
    await held;
    await route.continue();
  });

  const navigation = page.goto("/tasks/graph?project=lithos-loom");
  // `tasks.js` publishes this at the very END of its own execution, after the
  // point where it used to open the stream — so once it exists, an early
  // connection would already have happened.
  await page.waitForFunction(() => (window as any).LithosLens !== undefined);
  expect(await streams()).toBe(0);

  release();
  await navigation;
  await expect(
    page.locator('[data-graph-canvas][data-canvas-state="ready"]'),
  ).toBeVisible();
  // And it connects — ONCE. A real page fires `DOMContentLoaded` and then
  // `load`, and both start the stream, so this is also what pins the
  // idempotence guard between them.
  await page.waitForLoadState("load");
  await expect.poll(streams, { timeout: 5000 }).toBe(1);
});

test("a narrow canvas keeps its labels readable and says the view is partial", async ({
  page,
}) => {
  // Round-6 review: Cytoscape scales text with the viewport, so fitting the
  // whole graph into a 320px column drew the labels at under three pixels —
  // the nodes, the arrowheads, the dimmed ghost and the compound cycle all
  // became unreadable, which is everything the canvas is for. The automatic
  // fit stops at a readable floor instead, and the overflow is panned.
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/tasks/graph?project=lithos-loom");
  await expect(
    page.locator('[data-graph-canvas][data-canvas-state="ready"]'),
  ).toBeVisible();

  const narrow = await page.evaluate(() => {
    const graph = (window as any).LithosLensGraph;
    const box = document.querySelector("[data-graph-canvas]") as HTMLElement;
    return {
      rendered: parseFloat(graph.node("loom-ship").style("font-size")) * graph.cy.zoom(),
      clipped: box.dataset.canvasClipped,
      scrollWidth: document.documentElement.scrollWidth,
    };
  });
  expect(narrow.rendered).toBeGreaterThanOrEqual(10);
  // Bigger than its box at that size, so the page says so …
  expect(narrow.clipped).toBe("true");
  await expect(page.locator("[data-graph-pan-hint]")).toBeVisible();
  // … and the overflow is clipped to the canvas rather than widening the page.
  expect(narrow.scrollWidth).toBeLessThanOrEqual(320);

  // Given room, the whole graph fits and the page stops claiming otherwise.
  await page.setViewportSize({ width: 1440, height: 800 });
  await expect(page.locator("[data-graph-pan-hint]")).toBeHidden();
  await expect(
    page.locator("[data-graph-canvas]"),
  ).toHaveAttribute("data-canvas-clipped", "false");
  const wide = await page.evaluate(() => {
    const graph = (window as any).LithosLensGraph;
    return parseFloat(graph.node("loom-ship").style("font-size")) * graph.cy.zoom();
  });
  expect(wide).toBeGreaterThanOrEqual(10);
});
