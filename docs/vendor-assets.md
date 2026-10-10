# Vendor Assets

Lithos Lens serves production frontend dependencies from local static files
rather than public CDNs.

| Asset | Path | Version | Source URL | SHA256 |
|-------|------|---------|------------|--------|
| HTMX | `src/lithos_lens/static/vendor/htmx.min.js` | 2.0.4 | `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| HTMX SSE extension | `src/lithos_lens/static/vendor/htmx-sse.js` | 2.2.2 | `https://unpkg.com/htmx-ext-sse@2.2.2/sse.js` | `83eca6fa0611fe2b0bf1700b424b88b5eced38ef448ef9760a2ea08fbc875611` |
| Cytoscape.js | `src/lithos_lens/static/vendor/cytoscape.min.js` | 3.30.3 | `https://unpkg.com/cytoscape@3.30.3/dist/cytoscape.min.js` | `14f26e42750a194e5f827e9498c628efc7d411e2a1274126b9ee4e6daffa7694` |
| Inter (variable, Latin) | `src/lithos_lens/static/vendor/inter-latin-wght-normal.woff2` | 5.2.8 | `https://registry.npmjs.org/@fontsource-variable/inter/-/inter-5.2.8.tgz` (`package/files/inter-latin-wght-normal.woff2`) | `3100e775e8616cd2611beecfa23a4263d7037586789b43f035236a2e6fbd4c62` |

Inter is the knowledge graph canvas's label face (`"Lens Inter"` in
`lens.css`). The canvas places labels by their measured boxes, so it draws
them in a face Lens serves itself rather than whatever sans the browser's
machine has. It is the `@fontsource-variable/inter` package's Latin subset of
Inter's variable `wght` axis, one file for every weight the canvas draws. The
package tarball's SHA-1 matched the npm registry's `dist.shasum`
(`29b11476f5149f6a443b4df6516e26002d87941a`). Inter is licensed under the SIL
Open Font License 1.1, whose text ships beside it as
`src/lithos_lens/static/vendor/inter-OFL.txt`.
