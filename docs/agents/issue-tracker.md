# Issue tracker: Lithos tasks

Planned work for this repo is tracked as **Lithos tasks**, not GitHub issues.
Lens dogfoods the task graph it visualizes. PRDs live in `docs/prd/`; the
milestone sequence lives in `docs/ROADMAP.md`; each PRD's tracer-bullet
slices become Lithos tasks when the milestone starts.

GitHub issues remain only for inbound/external reports (bugs filed by people
who don't have Lithos access). Triage those into Lithos tasks.

## Conventions

Use the Lithos MCP tools (see the `lithos` skill for the full workflow:
register, search-before-work, claim, findings, complete).

- **Tags**: every task carries `project:lithos-lens` plus `milestone:<id>`
  (e.g. `milestone:t1`). Set `metadata.project = "lithos-lens"` as well —
  both conventions are live in the corpus.
- **Create**: `lithos_task_create` with a title naming the PRD slice (e.g.
  "T1-S2: frontier join + workable sections"), the slice's acceptance
  criteria in the description, and `depends_on` edges mirroring the PRD's
  slice-dependency notes. Milestone-level containers are `task_type="epic"`
  with slices as `parent_child` children.
- **Find work**: `lithos_task_ready(tags=["project:lithos-lens"])`.
- **Claim before working**: `lithos_task_claim` (aspect `implementation`
  unless the task says otherwise); renew long work with `lithos_task_renew`.
- **Progress**: post `lithos_finding_post` findings at meaningful checkpoints,
  linking knowledge notes where they exist.
- **Finish**: `lithos_task_complete` with an outcome summarizing what shipped
  (PR link included). Discovered follow-on work: `lithos_task_spawn` from the
  task you were working, not a floating new task.

## When a skill says "publish to the issue tracker"

Create a Lithos task per the conventions above.

## When a skill says "fetch the relevant ticket"

Look up the task with `lithos_task_get` (or `lithos_task_list` filtered by
the tags above) and read its findings via `lithos_finding_list`.

## GitHub (external reports only)

`gh issue list --state open` to review inbound reports; triage by creating a
Lithos task and closing the issue with a comment naming the task id.
