"""Every surface that names a task states the id the ecosystem names it by.

Everything AROUND Lens refers to a task by the first 8 characters of its id —
loom's gate lines (``raised needs-human gate ... for story 28105098``), the
findings, the ROADMAP, PR bodies. Lens rendered a title on nine surfaces and
kept the id in the ``href``, so relating a row to ``28105098`` meant hovering a
link.

The rule these pin (Dave, 2026-09-20): the id is METADATA, so it goes FIRST in
that surface's metadata group — the row's ``.task-row-meta``, the detail page's
and the panel's ``.detail-meta``, a graph entry's trailing badges. Position is
the claim, not presence: an id that lands after the status on one surface and
before it on the next is not the column the decision asks for, so the
assertions below are about WHERE it sits. Where a surface has no such group
(the children table, a blocker / Blocks / provenance line, a gate's waiters)
the id joins that surface's other facts about the task.

Two surfaces deliberately do NOT show it, and both are asserted here too: the
epic strip's progress chips (tooltip only — no room) and the Cytoscape canvas
(``test_tasks_js.py``, since a canvas label is not in the HTML).
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

from lithos_lens.tasks import SHORT_ID_CHARS, TaskRecord, short_id
from tests.test_graph_page import (
    GraphFakeClient,
    cycle_blocker,
    dataset,
    get,
    task,
)
from tests.test_task_detail import _client, _link, _task
from tests.test_tasks_mvp import TaskFakeLithosClient, _add_gate, _waits_on

#: A real Lithos id, not a fixture slug: the prefix has to be a SLICE of
#: something longer for "shows 8 of them" to mean anything.
FULL_ID = "28105098aa4c4d0fbb2f6b06d0e0b0aa"
PREFIX = "28105098"

CHIP = f'<code class="task-short-id" title="{FULL_ID}">{PREFIX}</code>'


def _group(html: str, opening: str) -> str:
    """The inner markup of the metadata group that starts at ``opening``."""
    start = html.index(opening) + len(opening)
    return html[start : html.index("</div>", start)]


def _leads(group: str, chip: str) -> bool:
    """Whether ``chip`` is the FIRST element in a metadata group."""
    return group.strip().startswith(chip)


# --- The filter itself ------------------------------------------------------


def test_the_filter_returns_the_eight_character_prefix() -> None:
    """The prefix loom types, and the one Lithos resolves (>= 6 unambiguous)."""
    assert short_id(FULL_ID) == PREFIX
    assert len(short_id(FULL_ID)) == SHORT_ID_CHARS


def test_an_id_shorter_than_the_prefix_is_returned_whole() -> None:
    """Nothing to elide, and a padded or truncated id would not resolve."""
    assert short_id("abc") == "abc"
    assert short_id("") == ""


# --- Dashboard rows and gate rows ------------------------------------------


def _row_fixture() -> TaskFakeLithosClient:
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task(FULL_ID, title="Ship the harness"))
    fake.ready_ids.add(FULL_ID)
    return fake


def test_a_dashboard_row_leads_its_metadata_with_the_short_id(
    lithos_lens_config_env: Path,
) -> None:
    """Headline acceptance: first in ``.task-row-meta``, ahead of the status.

    First is the whole point — the ids line up as a column down the board, so a
    row can be matched to a loom line at a glance without reading past a status
    badge to find it.
    """
    fake = _row_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    assert response.status_code == 200
    row = unescape(response.text).split(f'id="task-row-{FULL_ID}"', 1)[1]
    meta = _group(row, '<div class="task-row-meta">')
    assert _leads(meta, CHIP), meta
    assert meta.index(CHIP) < meta.index("badge-open")


def test_a_gate_row_leads_its_metadata_with_the_short_id(
    lithos_lens_config_env: Path,
) -> None:
    """A gate is the row loom names by id most often ("... gate for story X")."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, FULL_ID, title="Human review")
    _waits_on(fake, "open-unclaimed", FULL_ID)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    row = unescape(response.text).split(f'id="task-row-{FULL_ID}"', 1)[1]
    meta = _group(row, '<div class="task-row-meta">')
    assert _leads(meta, CHIP), meta
    assert meta.index(CHIP) < meta.index("data-gate-type-badge")


def test_a_gates_waiter_list_states_each_waiters_id_after_its_status(
    lithos_lens_config_env: Path,
) -> None:
    """A waiter line has no metadata group, so the id joins its other facts."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task(FULL_ID, title="Announce the harness"))
    _add_gate(fake, "gate-human", title="Human review")
    _waits_on(fake, FULL_ID, "gate-human")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    waiters = unescape(response.text).split("data-gate-waiters", 1)[1]
    waiters = waiters.split("</details>")[0]
    assert CHIP in waiters
    assert waiters.index("badge-open") < waiters.index(CHIP)


def test_a_rows_title_and_tooltip_are_untouched(
    lithos_lens_config_env: Path,
) -> None:
    """The id is added BESIDE the title, never into it (nor into the row's own
    ``title=``, which is the full title and stays that)."""
    fake = _row_fixture()

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    text = unescape(response.text)
    row = text.split(f'id="task-row-{FULL_ID}"', 1)[1].split("</article>")[0]
    assert '<a class="task-title"' in row
    assert ">Ship the harness</a>" in row
    assert 'title="Ship the harness"' in text
    # The identity every URL and every script reads is still the WHOLE id.
    assert f'data-task-id="{FULL_ID}"' in text
    assert f'href="/tasks/{FULL_ID}"' in row


# --- Detail page and side panel --------------------------------------------


def _detail_fixture() -> TaskFakeLithosClient:
    """The focal task, one blocker, one dependent and one child — every line
    the detail page draws about ANOTHER task, in one render."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task(FULL_ID, title="Ship the harness"),
            _task("pred-open", title="Design schema"),
            _task("dep-ship", title="Announce the harness"),
            _task("child-one", title="Write the docs"),
            _task("source-task", title="Spike the parser"),
        ]
    )
    _link(fake, "pred-open", FULL_ID, "blocks")
    _link(fake, FULL_ID, "dep-ship", "blocks")
    _link(fake, "source-task", FULL_ID, "discovered_from")
    _link(fake, FULL_ID, "child-one", "parent_child")
    fake.children[FULL_ID] = ["child-one"]
    return fake


def test_the_detail_header_leads_its_metadata_with_the_short_id(
    lithos_lens_config_env: Path,
) -> None:
    """Same position as a row's, one level up: first in ``.detail-meta``."""
    with _client(lithos_lens_config_env, _detail_fixture()) as client:
        response = client.get(f"/tasks/{FULL_ID}")

    assert response.status_code == 200
    html = unescape(response.text)
    meta = _group(html, '<div class="detail-meta">')
    assert _leads(meta, CHIP), meta
    assert meta.index(CHIP) < meta.index("badge-open")
    # Unchanged: the heading is the title, and the id is not in it.
    assert "<h1>Ship the harness</h1>" in html


def test_the_side_panel_header_leads_its_metadata_with_the_short_id(
    lithos_lens_config_env: Path,
) -> None:
    """The panel is what a Cytoscape node click opens, and the canvas labels
    deliberately carry no id — so the panel is where that click finds one."""
    with _client(lithos_lens_config_env, _detail_fixture()) as client:
        response = client.get(f"/tasks?selected={FULL_ID}&fragment=panel")

    assert response.status_code == 200
    html = unescape(response.text)
    meta = _group(html, '<div class="detail-meta">')
    assert _leads(meta, CHIP), meta
    assert meta.index(CHIP) < meta.index("badge-open")
    assert "<h2>Ship the harness</h2>" in html


def test_the_children_table_carries_a_leading_id_column(
    lithos_lens_config_env: Path,
) -> None:
    """No metadata group in a table row, so the id gets a column — leading, so
    it reads down the table the way it reads down the board."""
    fake = _detail_fixture()
    fake.tasks.append(_task("28105098bbbb", title="Write the docs"))
    fake.children[FULL_ID] = ["28105098bbbb"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks/{FULL_ID}")

    table = unescape(response.text).split('<table class="children-table">', 1)[1]
    table = table.split("</table>")[0]
    headers = re.findall(r"<th scope=\"col\">([^<]+)</th>", table)
    assert headers == ["Id", "Task", "Type", "Status"]
    row = table.split('data-child-id="28105098bbbb"', 1)[1].split("</tr>")[0]
    cells = re.findall(r"<td>(.*?)</td>", row, re.DOTALL)
    assert cells[0].strip() == (
        '<code class="task-short-id" title="28105098bbbb">28105098</code>'
    )
    assert "Write the docs" in cells[1]


def test_every_blocker_blocks_and_provenance_line_states_its_id(
    lithos_lens_config_env: Path,
) -> None:
    """One partial renders all three lists, so all three gain the id together —
    after the status, which is where those lines put their other facts."""
    fake = _detail_fixture()
    fake.tasks.append(_task(FULL_ID.replace("28", "77"), title="Design schema"))
    _link(fake, FULL_ID.replace("28", "77"), FULL_ID, "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks/{FULL_ID}")

    html = unescape(response.text)
    for label, target in (
        ("blockers", FULL_ID.replace("28", "77")),
        ("dependents", "dep-ship"),
        ("discovered-from", "source-task"),
    ):
        section = html.split(f'data-link-list="{label}"', 1)[1].split("</ul>")[0]
        line = section.split(f'data-link-target="{target}"', 1)[1].split("</li>")[0]
        chip = f'<code class="task-short-id" title="{target}">{short_id(target)}</code>'
        assert chip in line, f"{label} line states no short id"
        assert line.index("badge-") < line.index(chip), f"{label} id before status"


def test_an_unresolved_neighbour_still_states_its_id(
    lithos_lens_config_env: Path,
) -> None:
    """The case the id matters most: the edge names a task whose ``task_get``
    did not answer, so the id is the whole of what Lens knows about it."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("focus-task", title="Ship the harness"))
    _link(fake, FULL_ID, "focus-task", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/focus-task")

    html = unescape(response.text)
    line = html.split(f'data-link-target="{FULL_ID}"', 1)[1].split("</li>")[0]
    assert "data-link-unresolved" in line
    assert CHIP in line


def test_a_lazily_expanded_blocker_level_states_its_ids(
    lithos_lens_config_env: Path,
) -> None:
    """The chain walks one level per fetch (T1-S8), and every level is the same
    partial — so the id has to survive the HTMX fragment, not just the first
    render. This asks the expansion endpoint directly, which is what the
    expander button on the page below does."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("level-b", title="Design schema"),
            _task(FULL_ID, title="Spike the parser"),
        ]
    )
    _link(fake, "level-b", "open-unclaimed", "blocks")
    _link(fake, FULL_ID, "level-b", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks/level-b/blockers?chain=open-unclaimed&chain=level-b"
        )

    assert response.status_code == 200
    html = unescape(response.text)
    assert 'data-blocker-level="level-b"' in html
    line = html.split(f'data-link-target="{FULL_ID}"', 1)[1].split("</li>")[0]
    assert CHIP in line
    assert line.index("badge-") < line.index(CHIP)


# --- Graph page -------------------------------------------------------------


def _graph_html(config_path: Path, tasks: list[TaskRecord], edges: list) -> str:
    fake = GraphFakeClient(dataset(tasks, edges))
    return get(config_path, fake, "/tasks/graph?project=loom")


def test_a_graph_layer_entry_leads_its_badges_with_the_short_id(
    lithos_lens_config_env: Path,
) -> None:
    """Ahead of the status badge, ghosts included.

    On a graph the id is often ALL the operator has: a ghost is one hop outside
    the scope and carries no status of its own, and loom's lines name the
    endpoint by id rather than by title.
    """
    ghost_id = FULL_ID.replace("28", "77")
    html = _graph_html(
        lithos_lens_config_env,
        [
            task(FULL_ID, title="Ship the harness"),
            task(ghost_id, title="Design schema", project="other"),
        ],
        [(FULL_ID, ghost_id, "blocks")],
    )

    for task_id in (FULL_ID, ghost_id):
        entry = html.split(f'data-graph-node="{task_id}"', 1)[1].split("</li>")[0]
        after_link = entry.split("</a>", 1)[1]
        chip = (
            f'<code class="task-short-id" title="{task_id}">{short_id(task_id)}</code>'
        )
        assert _leads(after_link, chip), after_link
        assert after_link.index(chip) < after_link.index("data-node-status")


def test_a_graph_edge_line_states_the_predecessor_it_names(
    lithos_lens_config_env: Path,
) -> None:
    """The other place the graph page writes a task's title (§5.12)."""
    other = FULL_ID.replace("28", "77")
    html = _graph_html(
        lithos_lens_config_env,
        [task(other, title="Design schema"), task(FULL_ID, title="Ship the harness")],
        [(other, FULL_ID, "blocks")],
    )

    # `get` decodes entities, so the edge hook reads as the arrow it names.
    line = html.split(f'data-edge="{other}->{FULL_ID}"', 1)[1].split("</li>")[0]
    assert f'title="{other}">{short_id(other)}</code>' in line


def test_the_graph_scope_picker_states_each_epics_id(
    lithos_lens_config_env: Path,
) -> None:
    """The unscoped route offers open epics by title; the id is how the
    operator recognises the one a loom line named."""
    epic = task(FULL_ID, title="Ingest epic", task_type="epic")
    fake = GraphFakeClient(dataset([epic, task("a")]))
    html = get(lithos_lens_config_env, fake, "/tasks/graph")

    entry = html.split(f'data-picker-epic="{FULL_ID}"', 1)[1].split("</li>")[0]
    assert ">Ingest epic</a>" in entry
    # Position, not presence: the chip is the FIRST thing after the title, the
    # same place it holds in a layer entry's badges.
    assert _leads(entry.split("</a>", 1)[1], CHIP), entry


def test_the_graph_hierarchy_rows_lead_their_badges_with_the_short_id(
    lithos_lens_config_env: Path,
) -> None:
    """The parent/child tree is a second list of task rows on the same page, and
    it has the same metadata group: the id leads it, ahead of the status."""
    child = FULL_ID.replace("28", "77")
    fake = GraphFakeClient(
        dataset(
            [
                task(FULL_ID, title="Ingest epic", task_type="epic"),
                task(child, title="Ship the harness"),
            ],
            [(FULL_ID, child, "parent_child")],
        )
    )
    html = get(lithos_lens_config_env, fake, "/tasks/graph?project=loom")

    for task_id in (FULL_ID, child):
        row = html.split(f'data-hierarchy-node="{task_id}"', 1)[1].split("</li>")[0]
        after_link = row.split("</a>", 1)[1]
        chip = (
            f'<code class="task-short-id" title="{task_id}">{short_id(task_id)}</code>'
        )
        assert _leads(after_link, chip), after_link
        assert after_link.index(chip) < after_link.index("badge-")


def test_a_cycle_callout_names_every_member_with_its_id(
    lithos_lens_config_env: Path,
) -> None:
    """The cycle roster is a list of tasks, so it states their ids.

    The arrow PATH beside it stays labels — it is the walk's shape, and the
    roster on the same line has already said which tasks those are.
    """
    other = FULL_ID.replace("28", "77")
    fake = GraphFakeClient(
        dataset(
            [task(FULL_ID, title="Cyc A"), task(other, title="Cyc B")],
            [(FULL_ID, other, "blocks"), (other, FULL_ID, "blocks")],
            blocked={
                FULL_ID: cycle_blocker(other, "Dependency cycle."),
                other: cycle_blocker(FULL_ID, "Dependency cycle."),
            },
        )
    )
    html = get(lithos_lens_config_env, fake, "/tasks/graph?project=loom")

    members = html.split("data-cycle-members>", 1)[1].split("</span>")[0]
    for task_id, title in ((FULL_ID, "Cyc A"), (other, "Cyc B")):
        chip = (
            f'<code class="task-short-id" title="{task_id}">{short_id(task_id)}</code>'
        )
        assert f"{title} {chip}" in members, members


# --- Breadcrumbs ------------------------------------------------------------


def _breadcrumb_fixture() -> TaskFakeLithosClient:
    """A child under a parent epic, so both surfaces render a trail."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("child-task", title="Ship the harness"),
            _task(FULL_ID, title="Ingest epic", task_type="epic"),
        ]
    )
    _link(fake, FULL_ID, "child-task", "parent_child")
    return fake


def test_the_detail_breadcrumb_states_each_ancestors_id(
    lithos_lens_config_env: Path,
) -> None:
    """The trail names OTHER tasks, and those are the names an operator relates
    to a loom line. The trail's own last entry is this page's task, whose id
    leads the metadata group directly below it."""
    with _client(lithos_lens_config_env, _breadcrumb_fixture()) as client:
        response = client.get("/tasks/child-task")

    html = unescape(response.text)
    trail = html.split("data-parent-breadcrumb", 1)[1].split("</nav>")[0]
    assert ">Ingest epic</a>" in trail
    assert _leads(trail.split("</a>", 1)[1], CHIP), trail


def test_the_panel_breadcrumb_states_each_ancestors_id(
    lithos_lens_config_env: Path,
) -> None:
    """The panel renders the same trail from the same reads, so it says the
    same thing."""
    with _client(lithos_lens_config_env, _breadcrumb_fixture()) as client:
        response = client.get("/tasks?selected=child-task&fragment=panel")

    html = unescape(response.text)
    trail = html.split("data-panel-parent", 1)[1].split("</nav>")[0]
    assert ">Ingest epic</a>" in trail
    assert _leads(trail.split("</a>", 1)[1], CHIP), trail


# --- Scoped-epic banners ----------------------------------------------------


def test_every_scoped_epic_banner_names_the_epic_with_its_id(
    lithos_lens_config_env: Path,
) -> None:
    """Three banners state, in prose, which epic the empty board is scoped to.

    Each is the only place that epic is named on the page it appears on — the
    strip's chip is gone in two of the three — so each states the id.
    """
    empty = TaskFakeLithosClient()
    empty.tasks.append(_task(FULL_ID, title="Ingest epic", task_type="epic"))

    rolled_up = TaskFakeLithosClient()
    rolled_up.tasks.extend(
        [
            _task(FULL_ID, title="Ingest epic", task_type="epic"),
            _task("sub-epic", title="Sub epic", task_type="epic"),
        ]
    )
    rolled_up.children[FULL_ID] = ["sub-epic"]

    unmatched = TaskFakeLithosClient()
    unmatched.tasks.append(_task(FULL_ID, title="Ingest epic", task_type="epic"))
    unmatched.children[FULL_ID] = ["open-unclaimed"]

    scope = f"/tasks?epic={FULL_ID}"
    cases = (
        (empty, scope, "data-epic-scope-empty"),
        (rolled_up, scope, "data-epic-scope-rolled-up"),
        (unmatched, f"{scope}&tag=no-such-tag", "data-epic-scope-unmatched"),
    )
    for fake, url, hook in cases:
        with _client(lithos_lens_config_env, fake) as client:
            response = client.get(url)
        html = unescape(response.text)
        assert hook in html, hook
        banner = html.split(hook, 1)[1].split("</section>")[0]
        assert "Ingest epic" in banner
        assert CHIP in banner, hook
        assert banner.index("Ingest epic") < banner.index(CHIP)


# --- Note page --------------------------------------------------------------


def test_the_produced_by_chip_states_the_tasks_id(
    lithos_lens_config_env: Path,
) -> None:
    """The note's one reference to a task, so it names it the way loom does."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task(FULL_ID, title="Ship the harness"))
    fake.notes["note-produced"] = fake.notes["note-1"].__class__(
        id="note-produced",
        title="Harness notes",
        content="# Harness notes\n\nBody.",
        metadata={"source": FULL_ID},
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/note-produced")

    assert response.status_code == 200
    html = unescape(response.text)
    produced = html.split('<p class="produced-by">', 1)[1].split("</p>")[0]
    assert "Ship the harness" in produced
    assert CHIP in produced
    assert produced.index("produced-by-chip") < produced.index(CHIP)


def test_the_note_back_link_states_the_tasks_id(
    lithos_lens_config_env: Path,
) -> None:
    """`/note/<id>?task=<id>` heads the page with a link back to the task.

    It is the only place that task is named on the note page (a note need not
    have been produced by the task it was opened from), so it states the id.
    """
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task(FULL_ID, title="Ship the harness"))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/note/note-1?task={FULL_ID}")

    assert response.status_code == 200
    html = unescape(response.text)
    back = html.split(f'<a href="/tasks/{FULL_ID}">', 1)[1].split("</p>")[0]
    assert "Back to Ship the harness" in back
    assert _leads(back.split("</a>", 1)[1], CHIP), back


# --- The one surface that shows it only on hover ---------------------------


def test_an_epic_chip_keeps_its_text_and_carries_the_id_in_its_tooltip(
    lithos_lens_config_env: Path,
) -> None:
    """A progress chip has no room for another visible token, so the id rides
    in the tooltip alone — and the chip's own text is unchanged."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task(FULL_ID, title="Ingest epic", task_type="epic"))
    fake.children[FULL_ID] = ["open-unclaimed"]

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    html = unescape(response.text)
    # The WHOLE anchor, opening tag included, so a chip smuggled in ahead of
    # `.epic-chip-title` could not slip past the assertions below.
    start = html.rindex(
        '<a class="epic-chip', 0, html.index(f'data-epic-chip="{FULL_ID}"')
    )
    anchor = html[start : html.index("</a>", start)]

    # The tooltip is the chip's only identity fallback, so it carries the id
    # WHOLE — a truncated one would say no more than `data-epic-chip` already does.
    title = re.search(r'title="([^"]*)"', anchor)
    assert title is not None
    assert title.group(1) == f"Ingest epic ({FULL_ID}) — 0 of 1 done"
    assert FULL_ID in title.group(1)

    # Visible text unchanged: the title, the bar and the fraction, no id — and
    # the shared element appears nowhere inside the chip at all.
    visible = re.sub(r"<[^>]*>", " ", anchor.split(">", 1)[1])
    assert visible.split() == ["Ingest", "epic", "0/1"], visible
    assert "task-short-id" not in anchor
