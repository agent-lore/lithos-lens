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
 * `frontier_limit = 2` against the demo fixtures (four ready ids, one blocked)
 * is what makes the shot worth taking: the ready read comes back full and the
 * blocked read does not, so the picture shows the PER-SIDE marking this slice
 * delivers — Ready and Needs attention marked, Blocked left as the exact count
 * Lithos answered completely. A limit of 1 caps both sides and photographs the
 * board-wide banner that was already there.
 */
export const PORT = Number(process.env.LENS_E2E_PORT ?? 8123);
export const BASE_URL = `http://127.0.0.1:${PORT}`;

export const TRUNCATED_PORT = Number(
  process.env.LENS_E2E_TRUNCATED_PORT ?? 8124,
);
export const TRUNCATED_BASE_URL = `http://127.0.0.1:${TRUNCATED_PORT}`;
export const TRUNCATED_FRONTIER_LIMIT = "2";
