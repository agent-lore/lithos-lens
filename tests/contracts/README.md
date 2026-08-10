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
  client calls has a contract file and vice versa; files are well-formed; every
  canonical success payload round-trips through the real client method; every
  error envelope surfaces as its coded `LithosToolError`.
- `tests/test_lithos_contract.py` (`LITHOS_URL`-gated, host/CI): the vendored
  request shapes are validated against the live server's `tools/list` input
  schemas. Live response-shape verification needs seeded data — Lithos task
  `c144b363`.
- `_tools_snapshot.json` (refresh via `make contracts-snapshot`) is an advisory
  dump of ALL live tools' input schemas, so a hermetic agent can author a new
  client method against the real request schema. Underscore-prefixed files are
  not contracts.
