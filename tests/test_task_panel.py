"""T2 slice A6 — the side panel, one implementation for rows and nodes.

§5.5 puts a panel behind every row: clicking one opens the task's blockers,
its dependents, its parent and its claims WITHOUT leaving the board, and
`GET /tasks?selected=<id>` renders that panel server-side so a shared link (or
a browser with JavaScript off) lands on the same thing. The graph page reuses
this exact fragment from T2-A4 with `focus=` in place of `selected=`.

What these pin is the server half of that contract:

- `?selected=` renders the panel INTO the board, with the task's title, its
  blockers carrying live status, and its level-1 dependents;
- `?fragment=panel` answers with the partial and nothing else — no layout, no
  nav, no findings timeline — because a click swaps the response straight into
  the page;
- an unknown id renders the not-found PANEL at 200, never HTTP 500, on either
  route and beside a perfectly good board;
- closing preserves the board: the close link carries `?project=` and drops
  only the selection (the browser-side half of that is `test_tasks_js.py`);
- and the full detail page grows the same downstream half of the relationship
  under "Blocks" (§5.5.2's Blocks line).

The fixture helpers come from ``tests.test_task_detail``: same fake, same
edge-writing vocabulary, and the panel is the same reads that page already
makes.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from lithos_lens.task_links import LINK_PAGE_SIZE
from tests.test_task_detail import _client, _link, _task
from tests.test_tasks_mvp import TaskFakeLithosClient, _add_gate


def _related_fixture() -> TaskFakeLithosClient:
    """``open-unclaimed`` with a blocker above it and three dependents below.

    Both directions of the SAME edge types, which is the point of the slice:
    the chain answers "why can't this run?" and the Blocks line answers "what
    is waiting on it?". One dependent is deliberately COMPLETED — the two
    directions read opposite ways round, and a finished dependent is the case
    where a verdict written for predecessors would be wrong on this list.
    """
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("pred-open", title="Design schema"),
            _task("dep-ship", title="Ship the harness"),
            _task("dep-gate-waiter", title="Announce the harness"),
            _task("dep-done", title="Land the migration", status="completed"),
            _task("parent-epic", title="Ingest epic", task_type="epic"),
        ]
    )
    _link(fake, "pred-open", "open-unclaimed", "blocks")
    _link(fake, "open-unclaimed", "dep-ship", "blocks")
    _link(fake, "open-unclaimed", "dep-gate-waiter", "waits_on_gate")
    _link(fake, "open-unclaimed", "dep-done", "blocks")
    _link(fake, "parent-epic", "open-unclaimed", "parent_child")
    return fake


# --- Acceptance: `GET /tasks?selected=<id>` renders the panel open ----------


def test_selected_renders_the_panel_open_on_the_dashboard(
    lithos_lens_config_env: Path,
) -> None:
    """Headline acceptance: the no-JS baseline. One request, and the board
    comes back with the panel already open on the named task — its title, its
    blockers with the status Lithos reports for them NOW, and its level-1
    dependents."""
    fake = _related_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=open-unclaimed")

    assert response.status_code == 200
    text = response.text
    # The board is still there: the panel is beside the list, not instead of it.
    assert 'data-task-row data-task-id="open-claimed"' in text
    assert "data-task-panel" in text
    assert 'data-panel-task="open-unclaimed"' in text
    assert "Unclaimed open task" in text
    # Blockers, with live status.
    blockers = text.split("data-panel-blockers", 1)[1].split("data-panel-dependents")[0]
    assert 'data-link-target="pred-open"' in blockers
    assert "Design schema" in blockers
    assert 'class="badge badge-open">open</span>' in blockers
    # Dependents — the downstream half, each with the status it was READ with.
    dependents = text.split("data-panel-dependents", 1)[1].split("data-panel-claims")[0]
    assert "Blocks:" in dependents
    assert 'data-link-list="dependents"' in dependents
    assert 'data-link-target="dep-ship"' in dependents
    assert 'data-link-target="dep-gate-waiter"' in dependents
    assert "Ship the harness" in dependents
    ship = dependents.split('data-link-target="dep-ship"', 1)[1].split("</li>")[0]
    assert 'class="badge badge-open">open</span>' in ship
    done = dependents.split('data-link-target="dep-done"', 1)[1].split("</li>")[0]
    assert 'class="badge badge-completed">completed</span>' in done
    # The parent breadcrumb and the findings link complete §5.5.1's panel.
    assert "data-panel-parent" in text
    assert "Ingest epic" in text
    findings = text.split("data-panel-findings", 1)[1].split("</p>")[0]
    assert ">0 findings</a>" in findings
    # D10's downstream-impact line (T2-A7) is a count within ONE assembled
    # graph, and the dashboard assembles none: the slot is here and stands
    # empty rather than showing a number nothing behind this page supports.
    assert "data-panel-impact-slot" in text
    assert "data-panel-impact" not in text.replace("data-panel-impact-slot", "")


def test_the_panel_names_the_task_project_and_type(
    lithos_lens_config_env: Path,
) -> None:
    """The header is the identity §5.5.1 asks for: type badge, status and the
    project chip read under §5B.1's conventions, not guessed at in the
    template."""
    fake = _related_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=open-unclaimed")

    header = response.text.split("data-panel-task=", 1)[1].split("</header>")[0]
    assert 'data-panel-project="influx"' in header
    assert 'data-task-type="task"' in header
    assert 'class="badge badge-open">open</span>' in header
    # Expand leaves for the full page; close only clears the selection.
    assert "data-panel-expand" in header
    assert "data-panel-close" in header


def _panel_header(html: str) -> str:
    return html.split("data-panel-task=", 1)[1].split("</header>", 1)[0]


@pytest.mark.parametrize("posture", ["both", "tag", "metadata"])
def test_the_panel_names_a_metadata_only_project_whatever_the_posture(
    lithos_lens_config_env: Path, posture: str
) -> None:
    """The panel resolves its project chip itself (``TaskDetailData.projects``),
    so the retired ``project_convention`` (§4.4) used to reach it: under
    ``"tag"`` a task carrying ``metadata.project`` alone rendered
    ``(no project)`` beside a board that filtered it into view. Both
    conventions are read now, under every parsed value of the knob.
    """
    lithos_lens_config_env.write_text(
        lithos_lens_config_env.read_text()
        + f'\n[lithos-lens.tasks]\nproject_convention = "{posture}"\n'
    )
    fake = _related_fixture()
    fake.tasks.append(_task("mirrored", metadata={"project": "lithos-loom"}))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=mirrored")

    header = _panel_header(response.text)
    assert 'data-panel-project="lithos-loom"' in header, posture
    assert "data-panel-project-none" not in header, posture


def test_a_conflicting_rows_first_project_chip_is_the_metadata_one(
    lithos_lens_config_env: Path,
) -> None:
    """§5B.1's precedence, where a single value is needed: metadata WINS.

    The row claims two projects at once, so §5B.8 renders one chip per
    distinct project rather than silently dropping either — but the order is
    the precedence, metadata first, and it is what every single-value consumer
    reads (``projects[0]``: the mini-graph's focus link). A renderer that
    picked the tag, or an ordering that let the tag drift to the front, would
    state the wrong project for a row Lens has already warned about.
    """
    fake = _related_fixture()
    fake.tasks.append(
        replace(
            _task("conflicted", metadata={"project": "stamped"}),
            tags=("project:tagged",),
        )
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=conflicted")

    header = _panel_header(response.text)
    assert re.findall(r'data-panel-project="([^"]+)"', header) == [
        "stamped",
        "tagged",
    ]


def test_a_task_with_no_project_says_so_rather_than_rendering_nothing(
    lithos_lens_config_env: Path,
) -> None:
    """A task belonging to no project is a real domain case, not an error: a
    cross-project chore, or work nobody stamped. `task_projects` correctly
    answers with an empty tuple — and a header that then renders NOTHING where
    §5.5.1 asks for a project is indistinguishable from a panel that failed to
    fill the field in. It is said out loud, the same way the row's tag strip
    writes "(empty tag)" rather than showing a blank pill."""
    fake = _related_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=pred-open")

    header = response.text.split("data-panel-task=", 1)[1].split("</header>")[0]
    assert "data-panel-project=" not in header, "this fixture carries no project"
    assert "data-panel-project-none" in header
    assert "(no project)" in header
    # And the chip does not claim to NAME a project it has not got.
    assert "tag-chip-project" not in header


def test_a_selected_task_with_no_dependents_says_so(
    lithos_lens_config_env: Path,
) -> None:
    """The affirmative answer matters as much as the list — an empty Blocks
    section would read as a rendering failure."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=open-unclaimed")

    assert "Nothing depends on this task." in response.text


# --- Acceptance: the fragment route returns the partial only ---------------


def test_the_fragment_route_returns_the_panel_partial_only(
    lithos_lens_config_env: Path,
) -> None:
    """What a row click swaps into the page. No layout, no nav, no board — and
    none of the full page's sections either, because the panel is a summary
    with an Expand button, not a second copy of `/tasks/{id}`."""
    fake = _related_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed?fragment=panel")

    assert response.status_code == 200
    text = response.text
    assert text.lstrip().startswith("<aside")
    assert "<!doctype html>" not in text.lower()
    assert "shell-header" not in text
    assert "Search knowledge" not in text
    assert "data-task-row" not in text
    # The panel links the findings COUNT; the timeline itself stays on the page.
    assert "findings-timeline" not in text
    assert "children-table" not in text
    # It is nonetheless the whole panel.
    assert 'data-panel-task="open-unclaimed"' in text
    assert 'data-link-target="dep-ship"' in text


def test_the_rows_panel_url_is_the_fragment_route_with_the_board_filters(
    lithos_lens_config_env: Path,
) -> None:
    """The click handler fetches a URL the SERVER built, so the id encoding and
    the preserved filters have one definition. A panel fetched without the
    board's scope would come back with Expand and Close links that silently
    drop it."""
    fake = _related_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?project=influx")

    row = response.text.split('data-task-id="open-unclaimed"', 1)[1]
    panel_url = row.split('data-panel-url="', 1)[1].split('"')[0]
    assert panel_url.replace("&amp;", "&") == (
        "/tasks/open-unclaimed?project=influx&fragment=panel"
    )


def test_every_row_on_the_board_carries_the_panel_contract(
    lithos_lens_config_env: Path,
) -> None:
    """§5.5 puts a panel behind EVERY row, so every row the board renders has
    to carry the pair tasks.js opens one from: the id it selects and the
    server-built URL its panel comes from.

    Asserted over the rendered markup rather than per template, because the
    board has more than one kind of row and they are easy to forget. The Gates
    section was: its rows carry gate chrome instead of claim chrome, so they
    do not carry `data-task-row`, and a click handler keyed off that attribute
    left the whole section navigating away instead of opening a panel.
    """
    fake = _related_fixture()
    _add_gate(fake, "gate-human", title="Approve the cutover")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?since=2026-04-01")

    rows = re.findall(r"<article class=\"task-row[^\"]*\"[^>]*>", response.text)
    assert any("data-gate-row" in row for row in rows), (
        "the fixture is meant to put a gate row on the board"
    )
    inert = [
        row
        for row in rows
        if "data-task-id=" not in row or "data-panel-url=" not in row
    ]
    assert inert == [], f"rows with no panel to open: {inert}"


# --- Acceptance: an unknown id is a panel, never a 500 ----------------------


def test_an_unknown_id_renders_the_not_found_panel_on_the_fragment_route(
    lithos_lens_config_env: Path,
) -> None:
    """Lithos answers `task_not_found`, so the panel says so. A 500 here would
    be an error page swapped into a working board."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/no-such-task?fragment=panel")

    assert response.status_code == 200
    assert 'data-panel-state="not-found"' in response.text
    assert "Lithos has no task with this id." in response.text


def test_an_unknown_selected_id_does_not_cost_the_operator_the_board(
    lithos_lens_config_env: Path,
) -> None:
    """A stale id in a shared URL is a bad PANEL, not a bad page."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=no-such-task")

    assert response.status_code == 200
    assert 'data-panel-state="not-found"' in response.text
    assert 'data-task-row data-task-id="open-unclaimed"' in response.text


def test_a_failed_read_is_not_reported_as_a_missing_task(
    lithos_lens_config_env: Path,
) -> None:
    """The other half of the same distinction: a read that failed must not
    claim the task does not exist."""
    fake = TaskFakeLithosClient(visible_failures=True)

    async def failing_get(task_id: str):
        raise RuntimeError("boom")

    fake.task_get = failing_get  # type: ignore[method-assign]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed?fragment=panel")

    assert response.status_code == 200
    assert 'data-panel-state="error"' in response.text
    assert 'data-panel-state="not-found"' not in response.text


# --- Acceptance: closing preserves the board's state -----------------------


def test_closing_the_panel_keeps_the_boards_filters(
    lithos_lens_config_env: Path,
) -> None:
    """The no-JS half of "close preserves list state": the Close link is the
    board this request asked for, minus the selection. `selected` is not a
    preserved filter, so it falls out of every generated URL — including the
    row links and the Expand button, which must not carry one selection into
    the next page."""
    fake = _related_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?project=influx&selected=open-unclaimed")

    panel = response.text.split("data-task-panel", 1)[1]
    close_url = (
        panel.split("data-panel-close", 1)[0].rsplit('href="', 1)[1].split('"')[0]
    )
    assert close_url.replace("&amp;", "&") == "/tasks?project=influx"
    expand_url = (
        panel.split("data-panel-expand", 1)[0].rsplit('href="', 1)[1].split('"')[0]
    )
    assert expand_url.replace("&amp;", "&") == ("/tasks/open-unclaimed?project=influx")


# --- Acceptance: the detail page's own "Blocks" line (§5.5.2) --------------


def test_the_detail_page_lists_its_level_1_dependents_under_blocks(
    lithos_lens_config_env: Path,
) -> None:
    """The text baseline covers BOTH directions now: the blocker chain above,
    and one line per dependent — with live status — beneath it."""
    fake = _related_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert response.status_code == 200
    section = response.text.split("data-dependents", 1)[1]
    # The label §5.5.2 and the PRD both name, verbatim — colon included.
    assert "Blocks:" in section
    assert 'data-link-list="dependents"' in section
    assert 'data-link-target="dep-ship"' in section
    assert 'data-link-target="dep-gate-waiter"' in section
    assert "Ship the harness" in section
    # "one line per dependent WITH STATUS" — the status each was read with,
    # and no predecessor verdict on a downstream line.
    ship = section.split('data-link-target="dep-ship"', 1)[1].split("</li>")[0]
    assert 'class="badge badge-open">open</span>' in ship
    done = section.split('data-link-target="dep-done"', 1)[1].split("</li>")[0]
    assert 'class="badge badge-completed">completed</span>' in done
    assert "data-link-satisfied" not in done
    # Level 1 only: the downstream walk is the graph page's job, so no line
    # here offers the blocker chain's per-level expander.
    dependents = section.split('data-link-list="dependents"', 1)[1].split("</ul>")[0]
    assert "data-blocker-expand" not in dependents


def test_the_dependents_page_is_bounded_like_every_other_neighbour_list(
    lithos_lens_config_env: Path,
) -> None:
    """The outgoing edge count is agent-written too — a gate is exactly where a
    runaway one is expected — so the Blocks line renders one bounded page and
    COUNTS the rest through the shared tail, rather than resolving every
    dependent's status on every render."""
    extra = 9
    fake = TaskFakeLithosClient()
    for index in range(LINK_PAGE_SIZE + extra):
        dependent_id = f"dep-{index:03d}"
        fake.tasks.append(_task(dependent_id, title=f"Dependent {index:03d}"))
        _link(fake, "open-unclaimed", dependent_id, "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    text = response.text
    assert 'data-link-tail="dependents"' in text
    assert f"{extra} more dependents not shown." in text
    # One page of status reads for them, not one per edge.
    assert fake.get_calls[1:] == [f"dep-{index:03d}" for index in range(LINK_PAGE_SIZE)]


# --- §5.5.1's other required fields: claims, findings, the gate subtype -----


def test_the_panel_renders_the_tasks_active_claims(
    lithos_lens_config_env: Path,
) -> None:
    """§5.5.1's Active claims row, read from `lithos_task_status`. The claim
    is the answer to "is anyone on this?", so the panel names the aspect, the
    agent and the expiry rather than a count."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=open-claimed")

    claims = response.text.split("data-panel-claims", 1)[1].split("</section>")[0]
    assert 'data-panel-claim="implementation"' in claims
    assert "implementation / worker-a / expires" in claims
    assert "No active claims." not in claims


def test_the_panel_links_its_finding_count_to_the_timeline(
    lithos_lens_config_env: Path,
) -> None:
    """A COUNT plus a link, not the timeline (§5.5.1): the panel is a summary,
    and the full page carries §5.6. The count is the findings actually read,
    and the link opens the page AT them."""
    fake = TaskFakeLithosClient()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=open-claimed")

    findings = response.text.split("data-panel-findings", 1)[1].split("</p>")[0]
    # The fixture stages two findings on this task.
    assert ">2 findings</a>" in findings
    assert 'href="/tasks/open-claimed#findings"' in findings
    # The timeline itself stays on the full page.
    assert "findings-timeline" not in response.text.split("data-task-panel", 1)[1]


def test_the_panel_names_a_gates_subtype(lithos_lens_config_env: Path) -> None:
    """§5.5.1's type badge is "task / epic / gate + `gate_type`". A gate whose
    kind is not named is a gate the operator cannot judge — a human gate waits
    on a person, a timer gate on the clock."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        _task(
            "gate-review",
            title="Human review",
            task_type="gate",
            metadata={"gate_type": "human"},
        )
    )
    _link(fake, "gate-review", "open-unclaimed", "waits_on_gate")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=gate-review")

    header = response.text.split("data-panel-task=", 1)[1].split("</header>")[0]
    assert 'data-task-type="gate"' in header
    assert 'data-gate-type="human">human gate' in header
    # And its waiter is the Blocks list: what resolving this gate would free.
    dependents = response.text.split("data-panel-dependents", 1)[1]
    assert 'data-link-target="open-unclaimed"' in dependents


def test_the_panel_and_the_detail_page_show_the_same_pr_state_badge(
    lithos_lens_config_env: Path,
) -> None:
    """§5.5.4 gate context, T2b: a `pr` gate's reconciliation state is part of
    judging the gate, so it rides the same one badge template the board's Gates
    section uses — asserted on BOTH host pages from one fixture, because "the
    same badge" is the claim and three copies of the markup would be how it
    stops being true. The detail line is TEXT beside it, not only a tooltip: a
    title attribute is invisible to a keyboard and to a screen reader."""
    fake = TaskFakeLithosClient()
    _add_gate(
        fake,
        "gate-pr",
        gate_type="pr",
        title="Land the migration PR",
        metadata={
            "pr_url": "https://example.invalid/pull/84",
            "reconciliation_pr_url": "https://example.invalid/pull/84",
            "reconciliation_state": "needs_human",
            "reconciliation_detail": "Reviewer requested changes.",
            "reconciliation_since": "2026-08-01T00:00:00+00:00",
        },
    )

    with _client(lithos_lens_config_env, fake) as client:
        panel = client.get("/tasks/gate-pr?fragment=panel").text
        page = client.get("/tasks/gate-pr").text

    badge = re.compile(
        r'<span class="badge badge-reconciliation badge-reconciliation-danger"'
        r' data-reconciliation-state="needs_human"[^>]*'
        r'title="Reviewer requested changes\."'
        r">needs human · \d+[dhm]</span>"
    )
    assert badge.search(panel), panel
    assert badge.search(page), page
    for body in (panel, page):
        assert (
            '<p class="reconciliation-detail" data-reconciliation-detail>'
            "Reviewer requested changes.</p>" in body
        )


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_a_resolved_pr_gate_shows_no_live_state_on_either_surface(
    lithos_lens_config_env: Path, status: str
) -> None:
    """loom refreshes the four keys on still-OPEN PR gates only, so once a gate
    is completed or cancelled they are the last snapshot before it closed.

    The board never shows one — it collects gates off the open list — but the
    panel and the detail page address a task by id, so they are where a
    resolved gate would go on flying a red `needs human` (or claiming
    `ready to merge` about a PR that merged weeks ago) for ever. Asserted on
    both surfaces, and for both terminal statuses, because the rule is about
    the task's lifecycle rather than about either page.
    """
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        _task(
            "gate-pr-done",
            title="Land the migration PR",
            status=status,
            task_type="gate",
            resolved_at="2026-08-20T09:00:00+00:00",
            metadata={
                "gate_type": "pr",
                "pr_url": "https://example.invalid/pull/84",
                "reconciliation_pr_url": "https://example.invalid/pull/84",
                "reconciliation_state": "needs_human",
                "reconciliation_detail": "Reviewer requested changes.",
                "reconciliation_since": "2026-08-01T00:00:00+00:00",
            },
        )
    )

    with _client(lithos_lens_config_env, fake) as client:
        panel = client.get("/tasks/gate-pr-done?fragment=panel").text
        page = client.get("/tasks/gate-pr-done").text

    # Positive control FIRST, on each surface: the promise is "render the
    # resolved gate, minus its stale live-state badge", and an absence
    # assertion is satisfied just as well by a panel that rendered nothing —
    # not-found, unavailable, or a read error — which would hide a regression
    # behind a passing test. So each body must be the real thing: this task,
    # as a `pr` gate, at its terminal status, and through none of the panel's
    # degraded branches (all of which mark themselves with `data-panel-state`).
    assert 'data-panel-task="gate-pr-done"' in panel
    assert "data-panel-state=" not in panel
    assert 'data-task-detail="gate-pr-done"' in page
    for body in (panel, page):
        assert "Land the migration PR" in body
        assert 'data-task-type="gate"' in body
        assert 'data-gate-type="pr">pr gate' in body
        assert f'<span class="badge badge-{status}">{status}</span>' in body
        # …and only the present-tense claim is missing from it.
        assert "badge-reconciliation" not in body
        assert "data-reconciliation-state" not in body
        assert "data-reconciliation-detail" not in body
    # The keys themselves survive as HISTORY in the metadata table.
    assert "<dt>reconciliation_state</dt>" in page
    assert "<dd>needs_human</dd>" in page


def test_the_panel_shows_no_pr_badge_for_a_state_about_another_pr(
    lithos_lens_config_env: Path,
) -> None:
    """The staleness rule is the server's, so every surface inherits it — the
    panel cannot re-decide it, because it is handed nothing to render."""
    fake = TaskFakeLithosClient()
    _add_gate(
        fake,
        "gate-pr-replaced",
        gate_type="pr",
        metadata={
            "pr_url": "https://example.invalid/pull/84",
            "reconciliation_pr_url": "https://example.invalid/pull/12",
            "reconciliation_state": "needs_human",
            "reconciliation_detail": "About the replaced PR.",
        },
    )

    with _client(lithos_lens_config_env, fake) as client:
        panel = client.get("/tasks/gate-pr-replaced?fragment=panel").text

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/gate-pr-replaced").text

    # Both surfaces, because the requirement is about the state being asserted
    # anywhere — a detail page that badged it while the panel did not would be
    # the same wrong claim on a bigger canvas.
    for body in (panel, page):
        assert "badge-reconciliation" not in body
        assert "data-reconciliation-state" not in body
        assert "data-reconciliation-detail" not in body
    # …and the raw keys are still on the page's metadata table, where the gate's
    # full metadata has always been, so the stale state stays inspectable — it
    # is the CLAIM that is withheld, not the data.
    assert "<dt>reconciliation_state</dt>" in page
    assert "<dd>About the replaced PR.</dd>" in page


# --- A dependent is not a blocker: the verdicts read one way round ----------


def test_a_finished_dependent_is_not_labelled_as_a_satisfied_dependency(
    lithos_lens_config_env: Path,
) -> None:
    """The two blocker edge types render BOTH lists, and the verdicts written
    for predecessors say the opposite thing downstream: "satisfied" means this
    task's own dependency was met, and a completed DEPENDENT met nothing of the
    sort. It carries its status and no verdict."""
    fake = _related_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=open-unclaimed")

    dependents = response.text.split("data-panel-dependents", 1)[1].split(
        "data-panel-claims"
    )[0]
    done = dependents.split('data-link-target="dep-done"', 1)[1].split("</li>")[0]
    assert 'class="badge badge-completed">completed</span>' in done
    assert "data-link-satisfied" not in done
    # The blocker list above is untouched by that rule — a completed
    # PREDECESSOR is still marked satisfied there.
    assert "Blocked by" in response.text


def test_a_cancelled_dependent_is_not_called_unsatisfiable(
    lithos_lens_config_env: Path,
) -> None:
    """The loudest version of the same error: "unsatisfiable" says THIS task
    can never run. A cancelled task waiting on it says nothing about whether it
    can run — only that one of the things that wanted it went away."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("dep-cancelled", title="Dropped work", status="cancelled"))
    _link(fake, "open-unclaimed", "dep-cancelled", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=open-unclaimed")

    dependents = response.text.split("data-panel-dependents", 1)[1].split(
        "data-panel-claims"
    )[0]
    assert 'class="badge badge-cancelled">cancelled</span>' in dependents
    assert "data-link-unsatisfiable" not in dependents
    # …and the section heading still reports the task as unblocked.
    assert "Nothing is blocking this task." in response.text


# --- Reserved and URL-reserved ids reach their own panel --------------------


def test_a_page_word_id_is_addressed_through_the_alias_route(
    lithos_lens_config_env: Path,
) -> None:
    """A task really can be called `graph` (ids are arbitrary strings), and
    `/tasks/graph` is the graph PAGE. The row's panel URL therefore goes
    through the query alias — and fetching it returns that task's panel, not a
    page swapped into the panel host."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(
        replace(_task("graph", title="Draw the graph"), tags=("project:influx",))
    )
    _link(fake, "graph", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get("/tasks?project=influx")
        row = board.text.split('data-task-id="graph"', 1)[1]
        panel_url = row.split('data-panel-url="', 1)[1].split('"')[0]
        panel = client.get(panel_url.replace("&amp;", "&"))

    resolved = panel_url.replace("&amp;", "&")
    assert resolved.startswith("/tasks/id?task_id=graph")
    assert "fragment=panel" in resolved
    # …and the board's scope rides along, like every other generated tasks URL.
    assert "project=influx" in resolved
    assert panel.status_code == 200
    assert panel.text.lstrip().startswith("<aside")
    assert 'data-panel-task="graph"' in panel.text
    assert "Draw the graph" in panel.text
    # The graph page would have come back with these; the panel must not.
    assert "data-graph-picker" not in panel.text
    assert "<!doctype html>" not in panel.text.lower()


def test_a_url_reserved_id_is_encoded_into_one_path_segment(
    lithos_lens_config_env: Path,
) -> None:
    """Ids are arbitrary non-empty strings, so `?` and `#` in one would
    truncate the path and address something else entirely. The server builds
    the URL, so the encoding is the same decision every task link makes."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("od?d#id", title="Odd id task"))

    with _client(lithos_lens_config_env, fake) as client:
        board = client.get("/tasks")
        row = board.text.split('data-task-id="od?d#id"', 1)[1]
        panel_url = row.split('data-panel-url="', 1)[1].split('"')[0]
        panel = client.get(panel_url.replace("&amp;", "&"))

    assert panel_url.startswith("/tasks/od%3Fd%23id?")
    assert panel.status_code == 200
    assert "Odd id task" in panel.text


def test_the_panel_host_carries_the_selections_own_fetch_url(
    lithos_lens_config_env: Path,
) -> None:
    """A deep-linked task need not have a row: a filter can exclude it, or it
    resolved outside the window. Back and Forward still have to reopen it, so
    the HOST carries the URL the server built for the selection — including for
    an id no row could ever supply, and for one that does not exist at all."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("graph", title="Draw the graph"))

    with _client(lithos_lens_config_env, fake) as client:
        # `graph` carries no `project:influx` tag, so this board excludes it.
        scoped = client.get("/tasks?project=influx&selected=graph")
        unknown = client.get("/tasks?selected=no-such-task")

    host = scoped.text.split("data-panel-host", 1)[1].split(">")[0]
    assert 'data-panel-selected="graph"' in host
    assert "/tasks/id?task_id=graph" in host.replace("&amp;", "&")
    assert 'data-task-id="graph"' not in scoped.text
    # Even an unknown id: the host's job is to be able to re-ask the question.
    unknown_host = unknown.text.split("data-panel-host", 1)[1].split(">")[0]
    assert 'data-panel-selected="no-such-task"' in unknown_host


def test_the_panel_task_hook_names_exactly_one_element(
    lithos_lens_config_env: Path,
) -> None:
    """The host and the panel header carry DIFFERENT hooks on purpose, and this
    is why: a board with the panel open is addressed by `[data-panel-task]` in
    the browser suite, and two elements answering to it is a strict-mode
    failure there and an ambiguous selector everywhere else. The host names the
    id the REQUEST carried (an unknown task has one); the header names the task
    that actually resolved."""
    fake = _related_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?selected=open-unclaimed")

    assert response.text.count("data-panel-task=") == 1
    assert response.text.count("data-panel-selected=") == 1


def test_the_close_link_keeps_every_part_of_the_boards_state(
    lithos_lens_config_env: Path,
) -> None:
    """ "Close preserves list state" is the whole board, not one filter: the
    epic scope the build criterion names, a repeated multi-select, a tag whose
    value carries a colon, the agent and the resolved-since window all survive,
    and only `selected` goes. The no-JS half of the browser test in
    `test_tasks_js.py` — the link an operator without JavaScript clicks."""
    from urllib.parse import parse_qsl, urlsplit

    fake = _related_fixture()
    query = (
        "status=open&project=influx&project=loom&tag=area%3Adata&tag=ops"
        "&agent=planner&epic=parent-epic&since=2026-08-01"
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks?{query}&selected=open-unclaimed")

    panel = response.text.split("data-task-panel", 1)[1]
    close_url = (
        panel.split("data-panel-close", 1)[0].rsplit('href="', 1)[1].split('"')[0]
    )
    closed = sorted(parse_qsl(urlsplit(close_url.replace("&amp;", "&")).query))
    assert closed == sorted(parse_qsl(query))
