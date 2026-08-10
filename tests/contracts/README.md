# Vendored Lithos tool contracts

One JSON file per Lithos MCP tool that `src/lithos_lens/lithos_client.py`
calls, named exactly after the tool. These are the **authoritative reference
for every payload shape in this repo** (issue #31): three PRs in a row
(#23/#26/#30) invented response shapes and passed green because the client,
fake, and tests were authored against each other. The contracts break that
self-reference — they are data consumed by tests, not documentation.

## The rule

**Never infer a Lithos payload shape.** Copy it from the contract file. If the
tool has no contract file yet, add one — transcribed from the Lithos source,
with the `source` citation — in the same PR as the new client method.
`tests/test_lithos_contracts.py` enforces the file's existence and shape and
round-trips every canonical payload through the real client; fakes and test
fixtures must reproduce the canonical payloads, not approximations of them.

## File format

```jsonc
{
  "tool": "lithos_task_get",              // == filename stem
  "lithos_version": "lithos 0.4.0 @ 917cb5d",  // source the shapes were transcribed from
  "source": ["lithos:src/lithos/tools/tasks.py::lithos_task_get"],
  "request": {
    "canonical": {"task_id": "..."},      // the args Lens actually sends
    "variants": {"name": {...}},           // optional alternate request shapes
    "notes": "semantics worth knowing (defaults, filter behavior, ...)"
  },
  "responses": {
    "success": {...},                      // verbatim canonical success payload
    "variants": {"name": {...}},           // legacy envelopes, with_claims shapes, ...
    "errors": [{"status": "error", "code": "...", "message": "..."}]
  },
  "observed_divergences": ""               // source-intended vs live-observed gaps
}
```

`observed_divergences` exists because the source and a running server can
disagree (live example: `lithos_finding_list`'s `invalid_input` envelope is
eaten by FastMCP output-schema validation — Lithos task `60b3e135`). Record
the divergence instead of letting the doc and the contract matrix fight.

## Verification layers

- `tests/test_lithos_contracts.py` (hermetic, every run): every tool name the
  client calls has a contract file and vice versa (dynamic tool names are
  rejected); files are well-formed; every canonical request is bound to a real
  client call whose recorded outbound arguments must equal it exactly; every
  canonical success payload round-trips through the real client into
  full-field expected records; every error envelope surfaces as its coded
  `LithosToolError`. Scope boundary: the round-trip covers the fields Lens
  records expose — payload fields Lens deliberately does not model (e.g.
  `lithos_read`'s `links`/`truncated`/`retrieval_count`) are documented here
  for authoring but carried by no Lens record.
- `tests/test_lithos_contract.py` (`LITHOS_URL`-gated): the vendored request
  shapes are validated against the live server's `tools/list` input schemas.
  **This is a manual host-side step today** — run `make contracts-verify`
  (defaults to `http://localhost:8765`) whenever contracts are added or
  changed; a scheduled CI run against a seeded instance (which will also
  verify response shapes live) is tracked as Lithos task `c144b363`.
- `_tools_snapshot.json` (refresh via `make contracts-snapshot`) is an advisory
  dump of ALL live tools' input schemas, so a hermetic agent can author a new
  client method against the real request schema. Underscore-prefixed files are
  not contracts.
