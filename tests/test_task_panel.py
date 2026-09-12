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

from pathlib import Path

from lithos_lens.task_links import LINK_PAGE_SIZE
from tests.test_task_detail import _client, _link, _task
from tests.test_tasks_mvp import TaskFakeLithosClient


def _related_fixture() -> TaskFakeLithosClient:
    """``open-unclaimed`` with a blocker above it and two dependents below.

    Both directions of the SAME edge types, which is the point of the slice:
    the chain answers "why can't this run?" and the Blocks line answers "what
    is waiting on it?".
    """
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("pred-open", title="Design schema"),
            _task("dep-ship", title="Ship the harness"),
            _task("dep-gate-waiter", title="Announce the harness"),
            _task("parent-epic", title="Ingest epic", task_type="epic"),
        ]
    )
    _link(fake, "pred-open", "open-unclaimed", "blocks")
    _link(fake, "open-unclaimed", "dep-ship", "blocks")
    _link(fake, "open-unclaimed", "dep-gate-waiter", "waits_on_gate")
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
    # Dependents — the downstream half, on the same terms.
    dependents = text.split("data-panel-dependents", 1)[1].split("data-panel-claims")[0]
    assert 'data-link-list="dependents"' in dependents
    assert 'data-link-target="dep-ship"' in dependents
    assert 'data-link-target="dep-gate-waiter"' in dependents
    assert "Ship the harness" in dependents
    # The parent breadcrumb and the findings link complete §5.5.1's panel.
    assert "data-panel-parent" in text
    assert "Ingest epic" in text
    assert "data-panel-findings" in text


def test_the_panel_names_the_task_project_and_type(
    lithos_lens_config_env: Path,
) -> None:
    """The header is the identity §5.5.1 asks for: type badge, status and the
    project chip read under the configured convention (§5B.1), not guessed at
    in the template."""
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
    assert "<h2>Blocks</h2>" in section
    assert 'data-link-list="dependents"' in section
    assert 'data-link-target="dep-ship"' in section
    assert 'data-link-target="dep-gate-waiter"' in section
    assert "Ship the harness" in section
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
