# End-to-end smoke suite

A [Playwright](https://playwright.dev/) smoke suite that drives Lithos Lens in
**fake-Lithos app mode** — the real application, served against the in-memory
fixtures in [`src/lithos_lens/fake_lithos.py`](../src/lithos_lens/fake_lithos.py),
with no Lithos MCP server behind it.

This suite lives outside the Python test tree on purpose: it needs Node, the
`@playwright/test` runner, and a browser, none of which the `uv run pytest`
gate provides. The Python side of the harness (that the app boots in fake mode
and every surface renders) is covered fast and browserless by
[`tests/test_fake_lithos.py`](../tests/test_fake_lithos.py).

## Run it

From the repo root:

```sh
make e2e
```

That installs the locked Node deps (`npm ci` against the committed
`package-lock.json`), downloads Chromium, and runs the suite. CI runs the same
suite on every PR (`e2e` job) and uploads the screenshot artifacts. Or, from
this directory:

```sh
npm install
npm run install-browsers   # playwright install --with-deps chromium (falls back to a
                           # browser-only install when sudo can't run non-interactively)
npm test
```

## Three phases: read-only, then live events, then captures

The suite runs `fullyParallel`, and every tab it opens holds a live
`/tasks/events` subscription — so an event published through the fake-mode
`/tasks/events/publish` seam is fanned out to *all* of them, not just the page
that asked for it. A test that moves a **fixture** row therefore moves it under
every dashboard the suite happens to have open, and any tab that receives a
synthetic event photographs state the app never rendered from its own data.

So the config declares three projects, each sequenced behind the last with
`dependencies` — which is what actually orders them, since `fullyParallel` and
a file's own `mode` order tests only *within* a project:

| Project | Files | Runs |
|---|---|---|
| `app` | everything else (by exclusion, so a new spec file is never silently absent) | first, in parallel |
| `live-events` | `tests/live-events.spec.ts`, serial | after `app` |
| `screenshots` | `tests/screenshots.spec.ts` | after both |

An event test that only *adds* a row under a synthetic id — like
`task.created`'s skeleton — can stay in `smoke.spec.ts`; one that moves a
fixture row belongs in `live-events.spec.ts`.

Playwright's `webServer` (see [`playwright.config.ts`](./playwright.config.ts))
boots the app itself with:

```sh
LITHOS_LENS_FAKE_LITHOS=1 LITHOS_LENS_CONFIG=lithos-lens.example.toml \
  LENS_PORT=8123 LENS_HOST=127.0.0.1 uv run lithos-lens
```

so you do not need to start a server yourself.

`LENS_HOST` is not decoration. Lens binds every interface by default — the
accepted posture for the container it ships as — but fake mode registers
`POST /tasks/events/publish`, an unauthenticated write seam, and the suite runs
two such instances. Loopback keeps a run on a shared network from having a
foreign event fanned into the tabs being photographed. Use it for any fake-mode
instance you start by hand, too.

## Screenshot artifacts — the visual-review contract

Alongside the smoke assertions, `screenshots.spec.ts` captures every covered
page full-page at the four standard viewport widths and writes them to a
deterministic layout:

```
e2e/artifacts/<page>-<width>.png
```

with `<page>` one of `dashboard`, `task-detail`, `note`, `note-quarantined`,
`note-missing` and `<width>` one of `320`, `768`, `1024`, `1440` — 20 files
per run:

```
e2e/artifacts/dashboard-320.png
e2e/artifacts/dashboard-768.png
...
e2e/artifacts/note-missing-1440.png
```

**This path layout is a downstream contract**: loom's visual-review flow
([agent-lore/lithos-loom#283](https://github.com/agent-lore/lithos-loom/issues/283))
collects the images from `e2e/artifacts/` by exactly this naming scheme —
change either the directory or the `<page>-<width>.png` pattern only in
lockstep with that consumer. The contract is exact: **each PNG's pixel width
equals its stated width**, and the captured page must have **no horizontal
overflow** (`document.documentElement.scrollWidth <= width`) — both asserted
per capture, alongside the file existing with non-empty bytes, so a silently
broken capture fails the suite. The directory is gitignored; files are
overwritten on every run.

## Fake-Lithos app mode

Set `LITHOS_LENS_FAKE_LITHOS=1` (any of `1/true/yes/on`) and the application
factory swaps the real MCP client for
[`FakeLithosClient`](../src/lithos_lens/fake_lithos.py). To drive it by hand:

```sh
make run-fake        # LITHOS_LENS_FAKE_LITHOS=1 ... uv run lithos-lens
```

then open <http://127.0.0.1:8000/tasks>. Override the port with `LENS_PORT`,
and prefer `LENS_HOST=127.0.0.1` — see the note above on why fake mode in
particular should not listen on every interface.
