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

/**
 * A THIRD instance, for the graph page's two degraded states (T2-A3).
 *
 * Both are config-driven — a scoped blocked read that truncates, and a scope
 * over the node guard — so neither can be reached by a query parameter, and
 * neither may be produced on the healthy instance: `graph-project-*.png` is the
 * PRD's promised artifact (§"Visual (e2e…)"), and a board whose NORMAL state
 * carries "Cycle signal incomplete" is the false picture this gate exists to
 * prevent. The truncation instance cannot serve them either: its limit is
 * pinned at the value separating the two dashboard frontiers, and at `8` no
 * project's blocked read caps, so its graph page renders healthy.
 *
 * One instance covers both because the two states live at different SCOPES:
 * `GRAPH_REFUSAL_MAX_TASKS` sits strictly between the `loom-epic` subtree and
 * the `lithos-loom` project, so the epic renders (degraded, at a limit of 1
 * every read caps) while the project is turned away. That pairing is the whole
 * subject of both captures, and like the truncation limit above it is coupled
 * to fixture SIZES that grow silently —
 * `tests/test_fake_lithos.py::test_the_graph_instance_separates_the_degraded_and_refused_scopes`
 * pins it on every `make check`, because `make e2e` does not run there.
 */
export const GRAPH_PORT = Number(process.env.LENS_E2E_GRAPH_PORT ?? 8125);
export const GRAPH_BASE_URL = `http://127.0.0.1:${GRAPH_PORT}`;
// Every scoped blocked read comes back capped, so the cycle signal is partial
// on any scope this instance renders. `1` is the env path's floor.
export const GRAPH_DEGRADED_FRONTIER_LIMIT = "1";
// Between the two scopes the captures use — see the block above.
export const GRAPH_REFUSAL_MAX_TASKS = "9";
// The scope each capture drives, named here so the parity test and the
// capture cannot drift apart about which one is which.
export const GRAPH_DEGRADED_SCOPE = "loom-epic";
export const GRAPH_REFUSED_SCOPE = "lithos-loom";
