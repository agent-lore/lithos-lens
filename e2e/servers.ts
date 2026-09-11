/**
 * The application instances the suite drives, shared by `playwright.config.ts`
 * (which boots them) and `tests/screenshots.spec.ts` (which navigates to them).
 *
 * There are TWO, on their own ports, because the truncation capture needs a
 * `frontier_limit` the healthy board must not have. Lowering the limit on the
 * one server would degrade every other artifact — `dashboard-*.png` is loom's
 * visual-review page (agent-lore/lithos-loom#283) and would start showing a
 * "Section counts are approximate" board as its NORMAL state, which is exactly
 * the false picture the capture gate exists to prevent. A second instance keeps
 * both truths on screen: one honest healthy board, one honest truncated one.
 *
 * The limit is chosen against the demo fixtures' two frontier SIZES, and it has
 * to sit between them: the larger side truncates, the smaller one answers in
 * full, and the picture shows the PER-SIDE marking this slice delivers rather
 * than the board-wide banner that was already there (which is what any limit
 * below both sides photographs).
 *
 * Since T2-A1 added the graph cluster the sizes are seven ready and nine
 * blocked — the dependency chain, the cycle pair and the unsatisfiable fixture
 * are all blocked — so BLOCKED is now the side that overflows, where it used to
 * be Ready. `8` is the only value that separates them, and the capture asserts
 * that pairing explicitly: Blocked and Needs attention marked "at least this
 * many", Ready left as the exact count Lithos answered completely. Change the
 * fixture counts and this number moves with them; `tests/screenshots.spec.ts`
 * fails loudly if it stops separating the two sides.
 */
export const PORT = Number(process.env.LENS_E2E_PORT ?? 8123);
export const BASE_URL = `http://127.0.0.1:${PORT}`;

export const TRUNCATED_PORT = Number(
  process.env.LENS_E2E_TRUNCATED_PORT ?? 8124,
);
export const TRUNCATED_BASE_URL = `http://127.0.0.1:${TRUNCATED_PORT}`;
export const TRUNCATED_FRONTIER_LIMIT = "8";
