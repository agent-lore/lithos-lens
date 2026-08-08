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

That installs the Node deps, downloads Chromium, and runs the suite. Or, from
this directory:

```sh
npm install
npm run install-browsers   # playwright install --with-deps chromium
npm test
```

Playwright's `webServer` (see [`playwright.config.ts`](./playwright.config.ts))
boots the app itself with:

```sh
LITHOS_LENS_FAKE_LITHOS=1 LITHOS_LENS_CONFIG=lithos-lens.example.toml \
  LENS_PORT=8123 uv run lithos-lens
```

so you do not need to start a server yourself.

## Fake-Lithos app mode

Set `LITHOS_LENS_FAKE_LITHOS=1` (any of `1/true/yes/on`) and the application
factory swaps the real MCP client for
[`FakeLithosClient`](../src/lithos_lens/fake_lithos.py). To drive it by hand:

```sh
make run-fake        # LITHOS_LENS_FAKE_LITHOS=1 ... uv run lithos-lens
```

then open <http://127.0.0.1:8000/tasks>. Override the port with `LENS_PORT`.
