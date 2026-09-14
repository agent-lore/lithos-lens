"""T2 slice A7 — the server half of exploration mode: the chain through a
focused task (D7/D8) and the panel's downstream impact (D10).

The text is the acceptance surface here for the same reason it is in
`test_graph_page.py` (D3): loom's review gate is headless, and "frees 3 in
this graph, 1 immediately" is a SENTENCE the page states. The canvas half of
A7 — which nodes light, what a search hit pushes — is pinned in
`test_tasks_js.py`, against the real Cytoscape.

The fixtures are the smallest graph each rule needs, and the fake's demo board
(`fake_graph_dataset`) carries the whole picture for the e2e capture.
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest

from lithos_lens.config import DEFAULT_TASKS_FRONTIER_LIMIT
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.graph_impact import parse_impact_scope
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord
from tests.test_graph_page import (
    PROJECT,
    GraphFakeClient,
    client_for,
    dataset,
    get,
    task,
)

pytestmark = pytest.mark.anyio

#: The chain the acceptance criteria name: `root` blocks `one`, which blocks
#: `two` and `three`. Completing `root` frees three tasks in this graph, and
#: exactly one of them — `one` — has `root` as its sole unsatisfied blocker.
IMPACT_TASKS = ("root", "one", "two", "three")
IMPACT_EDGES = (
    ("root", "one", "blocks"),
    ("one", "two", "blocks"),
    ("one", "three", "blocks"),
)


def blocker(task_id: str) -> BlockerRecord:
    """Lithos's own "this task is waiting on `task_id`" row entry."""
    return BlockerRecord(
        kind="task",
        task_id=task_id,
        type="blocks",
        status="open",
        message=f"Waiting on {task_id}.",
    )


def impact_dataset() -> FakeLithosDataset:
    """The acceptance fixture as one dataset, blockers and all."""
    return dataset(
        [task(name) for name in IMPACT_TASKS],
        IMPACT_EDGES,
        blocked={
            "one": (blocker("root"),),
            "two": (blocker("one"),),
            "three": (blocker("one"),),
        },
    )


def slot(html: str) -> str:
    """The panel's impact slot as one line of text, markup stripped.

    The claims under test are sentences — "frees 3 in this graph, 1
    immediately" — so they are read as text rather than as attributes, which
    is what an operator actually sees.
    """
    match = re.search(r"data-panel-impact-slot>(.*?)</div>", unescape(html), re.DOTALL)
    assert match, "no impact slot in the panel"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()


def attribute(html: str, name: str) -> str:
    """One `data-impact-*` value off the rendered line."""
    match = re.search(rf'{name}="([^"]*)"', unescape(html))
    return match.group(1) if match else ""


def chain_line(html: str) -> str:
    match = re.search(r"data-longest-chain.*?</section>", unescape(html), re.DOTALL)
    assert match, "no chain section"
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(0))).strip()


# ── The chain through the focused task (D7/D8) ──────────────────────────


def test_focusing_a_task_traces_the_chain_through_it(
    lithos_lens_config_env: Path,
) -> None:
    """In focus mode the chain THROUGH the focused task replaces the scope's
    (D7). The two differ whenever the focus sits off the longest chain, which
    is exactly when a line still naming the scope's would describe a sequence
    the lit picture is not about."""
    fake = GraphFakeClient(
        dataset(
            [task(name) for name in ("a", "b", "c", "d", "e", "side")],
            (
                ("a", "b", "blocks"),
                ("b", "c", "blocks"),
                ("c", "d", "blocks"),
                ("d", "e", "blocks"),
                ("side", "d", "blocks"),
            ),
        )
    )

    unfocused = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    middle = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=c"
    )
    off_chain = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=side"
    )

    # The scope's own longest, unchanged by the absence of a focus.
    assert "A → B → C → D → E" in chain_line(unfocused)
    assert "through" not in chain_line(unfocused)
    # The layer-2 node is ON the longest chain, so the chain through it is the
    # whole thing — and the line says whose chain it is.
    assert "Longest blocking chain through C" in chain_line(middle)
    assert "<span data-chain-length>5</span>" in middle
    assert "A → B → C → D → E" in chain_line(middle)
    # …and a focus beside it gets its own, shorter, chain instead.
    assert "Longest blocking chain through Side" in chain_line(off_chain)
    assert "<span data-chain-length>3</span>" in off_chain
    assert "Side → D → E" in chain_line(off_chain)
    assert 'data-chain-through="side"' in off_chain


def test_a_focus_the_scope_does_not_hold_keeps_the_scopes_own_chain(
    lithos_lens_config_env: Path,
) -> None:
    """A deep link to a task this graph never fetched must not silently empty
    the chain line: there is no chain through a node that is not here, so the
    page states the one it does have."""
    fake = GraphFakeClient(dataset([task("a"), task("b")], [("a", "b", "blocks")]))

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=nobody"
    )

    assert "<span data-chain-length>2</span>" in html
    assert "A → B" in chain_line(html)
    assert "through" not in chain_line(html)


def test_the_panel_says_where_the_focus_sits_on_the_longest_chain(
    lithos_lens_config_env: Path,
) -> None:
    """ "On the longest chain (k of n)" is a position on the SCOPE's chain (D7)
    — a task is trivially on the chain through itself, so saying that would
    state nothing at all. A task off the chain gets no line."""
    fake = GraphFakeClient(
        dataset(
            [task(name) for name in ("a", "b", "c", "alone")],
            (("a", "b", "blocks"), ("b", "c", "blocks")),
        )
    )

    middle = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=b"
    )
    aside = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=alone"
    )

    assert "On the longest chain (2 of 3)." in slot(middle)
    assert "longest chain" not in slot(aside)


# ── Downstream impact (D10) ─────────────────────────────────────────────


def test_the_panel_states_what_completing_the_focus_frees(
    lithos_lens_config_env: Path,
) -> None:
    """D10's two figures, from the two authorities they belong to: N is Lens's
    walk over the active projection (three transitive dependents), M is
    Lithos's sole-blocker fact (one of them names `root` and nothing else)."""
    fake = GraphFakeClient(impact_dataset())

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "frees 3 in this graph, 1 immediately" in slot(html)
    assert attribute(html, "data-impact-frees") == "3"
    assert attribute(html, "data-impact-immediate") == "1"
    assert attribute(html, "data-impact-bound") == "exact"


def test_a_satisfied_edge_frees_nobody_through_it(
    lithos_lens_config_env: Path,
) -> None:
    """N is over the ACTIVE projection (D6): a resolved dependent is not
    counted, and neither is anything reachable only through it — completing
    the focus cannot free work a finished task already unblocked."""
    fake = GraphFakeClient(
        dataset(
            [task("root"), task("done", status="completed"), task("beyond")],
            (("root", "done", "blocks"), ("done", "beyond", "blocks")),
        )
    )

    html = get(
        lithos_lens_config_env,
        fake,
        f"/tasks/graph?project={PROJECT}&focus=root&include_resolved=1",
    )

    assert "frees 0 in this graph" in slot(html)


def test_a_sole_blocked_downstream_ghost_is_counted_immediately(
    lithos_lens_config_env: Path,
) -> None:
    """A downstream ghost is a leaf of this graph but a real dependent, and
    D4 puts its project in the coverage set precisely so its sole-blocker fact
    is readable here (D10)."""
    fake = GraphFakeClient(
        dataset(
            [task("root"), task("far", project="other")],
            (("root", "far", "blocks"),),
            blocked={"far": (blocker("root"),)},
        )
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "frees 1 in this graph, 1 immediately" in slot(html)
    # The read that made it countable: the ghost's OWN project, not this one.
    read_projects = {call["project"] for call in fake.blocked_calls if call["project"]}
    assert "other" in read_projects


def test_a_truncated_read_for_a_dependents_project_withholds_the_figure(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M is Lithos's fact, and a truncated response is silence about the rows
    it did not return (D4). So M is WITHHELD rather than reported low: a
    partial count reads exactly like a whole one, and the operator is choosing
    what to work on next from it. N stands — it is Lens's own arithmetic."""
    limit = 3
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", str(limit))
    filler = [task(f"other-{index}", project="other") for index in range(limit)]
    fake = GraphFakeClient(
        dataset(
            [task("root"), task("far", project="other"), *filler],
            (("root", "far", "blocks"),),
            blocked={"far": (blocker("root"),)},
        ),
        blocked_rows={
            # The ghost's project answers with a full page that does not name
            # it: the fact Lens needs is exactly what the cap cut off.
            "other": [BlockedTaskRecord(task=row) for row in filler],
            "project:other": [BlockedTaskRecord(task=row) for row in filler],
        },
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "frees 1 in this graph" in slot(html)
    assert "how many immediately is withheld" in slot(html)
    assert "covered 0 of 1 dependents" in slot(html)
    assert attribute(html, "data-impact-withheld") == "true"
    assert "data-impact-immediate" not in html


def test_a_dependent_whose_edges_failed_makes_the_count_a_lower_bound(
    lithos_lens_config_env: Path,
) -> None:
    """A dependent whose own `edge_list` read failed hides whatever IT blocks,
    so N could only be larger and says so (D2/D10)."""
    fake = GraphFakeClient(impact_dataset(), edge_failures={"one"})

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "frees ≥ 3 in this graph, 1 immediately" in slot(html)
    assert attribute(html, "data-impact-bound") == "lower_bound"


def test_an_unreadable_ghost_dependent_is_listed_and_not_counted(
    lithos_lens_config_env: Path,
) -> None:
    """An `unknown` edge is counted in NEITHER direction (D6): the far end is
    named so the gap is visible, and N becomes a lower bound rather than
    absorbing a relation Lens could not classify."""
    fake = GraphFakeClient(
        dataset(
            [task("root"), task("one")],
            (("root", "one", "blocks"), ("root", "far", "blocks")),
            blocked={"one": (blocker("root"),)},
        ),
        # The far endpoint is out of scope and its `task_get` fails, so Lens
        # cannot tell a completed endpoint from a live one (D2).
        get_failures={"far"},
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "frees ≥ 1 in this graph" in slot(html)
    assert "Not counted, relation unreadable: far" in slot(html)


def test_an_unreadable_neighbour_makes_the_lit_set_a_lower_bound(
    lithos_lens_config_env: Path,
) -> None:
    """Focus mode lights ancestors AND descendants (D8), so the honesty of the
    lit set is a symmetric question where N's is not: an UPSTREAM task whose
    edges could not be read leaves N exact and the picture a lower bound, and
    a dimmed node must not read as "unrelated" when Lens only failed to look."""
    fake = GraphFakeClient(
        dataset(
            [task("above"), task("root"), task("one")],
            (("above", "root", "blocks"), ("root", "one", "blocks")),
            blocked={"one": (blocker("root"),), "root": (blocker("above"),)},
        ),
        edge_failures={"above"},
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    # Downstream is whole, so the COUNT stays exact …
    assert "frees 1 in this graph, 1 immediately" in slot(html)
    assert attribute(html, "data-impact-bound") == "exact"
    # … and the picture still says what it could not see.
    assert "data-panel-focus-bound" in html
    assert "lower bound of what surrounds it" in slot(html)


def test_a_whole_neighbourhood_claims_no_lower_bound(
    lithos_lens_config_env: Path,
) -> None:
    """The other side of it: a graph Lens read in full makes no such note, or
    the caveat would be this page's normal state and say nothing."""
    fake = GraphFakeClient(impact_dataset())

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "data-panel-focus-bound" not in html


def test_a_resolved_focal_task_states_no_pending_impact(
    lithos_lens_config_env: Path,
) -> None:
    """A completed task's edges are satisfied and a cancelled one's are
    unsatisfiable — neither is pending, so neither carries a future-tense
    number (D10)."""
    fake = GraphFakeClient(
        dataset(
            [
                task("done", status="completed"),
                task("stopped", status="cancelled"),
                task("next"),
            ],
            (("done", "next", "blocks"), ("stopped", "next", "blocks")),
        )
    )

    url = f"/tasks/graph?project={PROJECT}&include_resolved=1&focus="
    completed = get(lithos_lens_config_env, fake, url + "done")
    cancelled = get(lithos_lens_config_env, fake, url + "stopped")

    assert "completed; no pending impact" in slot(completed)
    assert "frees" not in slot(completed)
    assert "cancelled — its dependents are unsatisfiable" in slot(cancelled)


def test_an_epic_carries_no_impact_line_at_all(
    lithos_lens_config_env: Path,
) -> None:
    """An epic has no `blocks` edges, so a zero here would read as "finishing
    this frees nobody" rather than "this is not that kind of task" (D10)."""
    fake = GraphFakeClient(
        dataset(
            [task("epic", task_type="epic"), task("child")],
            (("epic", "child", "parent_child"),),
        )
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=epic"
    )

    assert slot(html) == ""


# ── The panel fetched on its own (the `scope=` fragment route) ──────────


def test_the_fragment_route_counts_the_impact_over_the_scope_it_is_given(
    lithos_lens_config_env: Path,
) -> None:
    """A node click fetches the panel on its own, so the scope travels in the
    URL (D10). Without it there is no graph to count within — and the panel
    renders no number rather than one from a scope nobody named."""
    fake = GraphFakeClient(impact_dataset())

    with client_for(lithos_lens_config_env, fake) as client:
        scoped = client.get(f"/tasks/root?fragment=panel&scope=project:{PROJECT}").text
        unscoped = client.get("/tasks/root?fragment=panel").text
        nonsense = client.get("/tasks/root?fragment=panel&scope=galaxy:root").text

    assert "frees 3 in this graph, 1 immediately" in slot(scoped)
    assert slot(unscoped) == ""
    assert slot(nonsense) == ""


def test_the_graph_page_hands_the_panel_its_own_scope(
    lithos_lens_config_env: Path,
) -> None:
    """Both halves of the panel's fetch URL — the one the server writes onto
    the host, and the one `tasks.js` builds for a node with no row — carry the
    scope AND its membership, or a clicked panel would count over a different
    graph from the one on screen."""
    fake = GraphFakeClient(impact_dataset())

    html = get(
        lithos_lens_config_env,
        fake,
        f"/tasks/graph?project={PROJECT}&focus=root&include_resolved=1",
    )

    assert f"scope=project%3A{PROJECT}" in html
    assert "include_resolved=1" in unescape(html)
    assert f'panelScope: "project:{PROJECT}"' in html
    assert "panelScopeResolved: true" in html


def test_an_impact_scope_is_parsed_with_the_pages_own_defaults() -> None:
    """The panel and its page must resolve one URL the same way: the scope
    kinds are the graph's, and `include_resolved` keeps the by-kind default a
    malformed value cannot flip."""
    assert parse_impact_scope("project:loom").kind == "project"
    assert parse_impact_scope("epic:e-1").key == "e-1"
    # An id may itself contain the separator, so only the FIRST one splits.
    assert parse_impact_scope("epic:a:b").key == "a:b"
    assert not parse_impact_scope("project:").scoped
    assert not parse_impact_scope("galaxy:loom").scoped
    assert not parse_impact_scope(None).scoped
    # Opposite defaults by kind, and a value in neither flag set carries no
    # request at all.
    assert parse_impact_scope("project:loom").include_resolved is False
    assert parse_impact_scope("epic:e-1").include_resolved is True
    assert parse_impact_scope("epic:e-1", "garbage").include_resolved is True
    assert parse_impact_scope("project:loom", "1").include_resolved is True


def test_the_shipped_frontier_limit_bounds_the_coverage_reads() -> None:
    """The impact's M rides on D4's coverage reads, so it inherits their
    bound rather than a limit of its own."""
    assert DEFAULT_TASKS_FRONTIER_LIMIT > 0
