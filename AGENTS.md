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
  domain-model scanning, and the hard metric budgets. Adding a new module,
  component, or model? The guardrail orphan/completeness checks fail until you map
  it there.
- Directional import rules (Entrypoints → Core → Foundation) are enforced by
  import-linter (`pyproject.toml [tool.importlinter]`).
- This is the portable "diagrams as tests" kit; `tests/guardrail/AGENTS.md` has the
  generator contracts. The kit's optional tool-catalog and container adapters are
  not enabled here (lithos-lens is an MCP client with no store surface).

## Agent skills

### Issue tracker

Planned work is tracked as Lithos tasks (tags `project:lithos-lens`, `milestone:<id>`); PRDs live in `docs/prd/` and the milestone sequence in `docs/ROADMAP.md`. GitHub Issues are used only for inbound external reports. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default mattpocock/skills triage label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo. See `docs/agents/domain.md`.
