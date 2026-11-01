## Architecture guardrails & generated docs

`docs/generated/` holds generated views of the code — component diagram, domain
model, architecture metrics, and per-component drill-down pages (indexed by
`docs/generated/README.md`) — produced by `tests/guardrail/` and drift-checked in
CI:

- `make diagrams` regenerates everything (it just runs `pytest tests/guardrail/ -q`).
  Note `make test` runs the same tests, so a test run rewrites `docs/generated/`
  as a side effect — commit the result if it changed.
- The CI job `diagrams` (Diagram drift) fails when the committed files disagree
  with what the code generates. Fix: `make diagrams`, commit.
- `docs/architecture.toml` is the source of truth for components, tiers,
  domain-model scanning, and the accepted metric budgets. Adding a new module,
  component, or model? The guardrail orphan/completeness checks fail until you map
  it there.
- Directional import rules (Entrypoints → Core → Foundation) are enforced by
  import-linter (`pyproject.toml [tool.importlinter]`).
- This is the portable "diagrams as tests" kit; `tests/guardrail/AGENTS.md` has the
  generator contracts. The kit's optional tool-catalog and container adapters are
  not enabled here (lithos-lens is an MCP client with no store surface).

## Lithos tool contracts

**Never infer a Lithos payload shape.** Every request/response shape for the
Lithos MCP tools comes from the vendored contracts in `tests/contracts/`
(`<tool>.json`; format and discipline in `tests/contracts/README.md`):

- Writing or changing a client method, fake behavior, or test fixture? Copy the
  shape from the contract file. Fakes and fixtures reproduce the canonical
  payloads — no approximations.
- Calling a Lithos tool that has no contract file yet? Add the contract —
  transcribed from the Lithos source, with the `source` citation — **in the
  same PR**. `tests/test_lithos_contracts.py` fails otherwise, and also
  round-trips every canonical payload through the real client.
- `tests/contracts/_tools_snapshot.json` has the live input schemas of ALL
  server tools (refresh: `make contracts-snapshot`) for authoring new methods.
- `make contracts-verify` (host-side, manual — CI does not run it; scheduled
  seeded run tracked as Lithos task `c144b363`) verifies the vendored
  contracts against a live server. Run it when adding or changing a contract.

## Agent skills

### Issue tracker

Planned work is tracked as Lithos tasks (tags `project:lithos-lens`, `milestone:<id>`); PRDs live in `docs/prd/` and the milestone sequence in `docs/ROADMAP.md`. GitHub Issues are used only for inbound external reports. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default mattpocock/skills triage label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo. See `docs/agents/domain.md`.

## Guardrail policy

Guardrails support maintainability; passing a metric alone is not evidence of
good design. Dependency restrictions, cycle limits, and private-access contracts
are architectural invariants. Size, complexity, and dependency counts are review
thresholds: do not split cohesive code or add forwarding layers just to meet a
number. Refactor when a clean seam improves clarity, cohesion, or testability.

`[policies]` classifies the accepted budgets; `[review]` holds per-entity thresholds
and optional new/worsened-code gates. If keeping code together is clearer, record
a proportionate, bounded `[[exceptions]]` entry with a reason and review reference,
or explain a threshold adjustment in the same PR. Never automatically raise
budgets or bypass an invariant. Lower thresholds when that protects useful gains.
Changed-code gates remain disabled until the first 1.6 baseline is merged.
See `docs/quality-guide.md` for the migration and evidence contracts.
