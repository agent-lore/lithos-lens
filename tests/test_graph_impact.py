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
from collections.abc import Callable
from dataclasses import replace
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

import pytest

from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.graph_cycles import (
    READ_BY_PROJECT,
    READ_BY_TAG,
    CycleSignal,
    ProjectRead,
)
from lithos_lens.graph_impact import (
    downstream_impact,
    parse_impact_scope,
    reconciled_impact,
)
from lithos_lens.graph_scope import (
    COMPLETENESS_EDGES_UNKNOWN,
    EDGE_ACTIVE,
    EDGE_INACTIVE,
    GraphEdge,
    GraphNode,
    TaskGraphScope,
)
from lithos_lens.graph_snapshot import canvas_holds, impact_fingerprint
from lithos_lens.graph_view import DownstreamImpact
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import MAX_FILTER_QUERY_BYTES, TaskRecord, TaskStatusName
from tests.test_graph_page import (
    PROJECT,
    GraphFakeClient,
    client_for,
    dataset,
    get,
    payload,
    task,
)

# The A4 canvas harness, borrowed the way `test_graph_page` borrows it: the
# chain a focus transition traces is a claim with a PRODUCER here and a
# CONSUMER in `graph.js`, and only running both can show they agree.
from tests.test_tasks_js import NODE, _graph_run

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


def snapshot(html: str) -> str:
    """The fingerprint of the graph this render DREW, off its own config block.

    Read from the page rather than recomputed, because the claim under test is
    that the page and the panel agree about which graph is on screen — and a
    test that computed its own fingerprint would agree with neither.
    """
    match = re.search(r'panelSnapshot: "([^"]*)"', html)
    assert match, "the page handed the panel no snapshot"
    return match.group(1)


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


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_canvas_re_traces_the_chain_the_server_would_have_rendered(
    lithos_lens_config_env: Path,
) -> None:
    """The producer and the consumer in one test (round-1 correctness f-001).

    A focus transition is client-side — no reload, no fetch of the graph — so
    the canvas computes the chain through the newly focused node from the DP
    the payload ships (`active_chain`). That answer has to be the one the
    SERVER would have rendered for the same URL, or the picture and the text
    diverge the moment anybody clicks.
    """
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
    served = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=side"
    )

    # The canvas starts on the unfocused page and focuses `side` itself.
    drawn = _graph_run(
        ["tap:side"],
        href=f"http://lens.test/tasks/graph?project={PROJECT}",
        payload=payload(unfocused),
    )
    line = drawn["final"]["chainLine"]

    assert line["nodes"] == "Side → D → E"
    assert line["label"] == " through Side"
    assert line["length"] == "3"
    # …which is exactly the sentence the server renders for that same URL.
    assert f"Longest blocking chain{line['label']}" in chain_line(served)
    assert line["nodes"] in chain_line(served)
    assert f"<span data-chain-length>{line['length']}</span>" in served
    assert drawn["final"]["traced"] == ["d", "e", "side"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_canvas_names_a_focused_cycle_member_the_way_the_server_does(
    lithos_lens_config_env: Path,
) -> None:
    """The same producer/consumer check where the two vocabularies differ
    (round-2 correctness f-005): a chain is a walk over CONDENSATIONS and names
    each by its representative, while the sentence says whose chain it is — the
    task the operator focused. `ring-b` is a non-representative member of a
    live cycle, so a client that named the representative would print
    "through Ring A" under a URL that says `focus=ring-b`.
    """
    fake = GraphFakeClient(
        dataset(
            [task(name) for name in ("before", "ring-a", "ring-b", "after")],
            (
                ("before", "ring-a", "blocks"),
                ("ring-a", "ring-b", "blocks"),
                ("ring-b", "ring-a", "blocks"),
                ("ring-b", "after", "blocks"),
            ),
        )
    )
    unfocused = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    served = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=ring-b"
    )

    drawn = _graph_run(
        ["tap:ring-b"],
        href=f"http://lens.test/tasks/graph?project={PROJECT}",
        payload=payload(unfocused),
    )
    line = drawn["final"]["chainLine"]

    # The server names the FOCUSED member and condenses the walk …
    assert "Longest blocking chain through Ring B" in chain_line(served)
    assert "Before → Ring A → After" in chain_line(served)
    assert 'data-chain-through="ring-b"' in served
    # … and so does the canvas, from the same projection.
    assert line["through"] == "ring-b"
    assert f"Longest blocking chain{line['label']}" in chain_line(served)
    assert line["nodes"] in chain_line(served)
    assert f"<span data-chain-length>{line['length']}</span>" in served
    # Both cycle members are on the traced chain, which crosses the loop in one
    # move: the step enters at the member the edge actually lands on.
    assert set(drawn["final"]["traced"]) >= {"before", "ring-a", "ring-b", "after"}
    assert drawn["final"]["tracedEdges"] == ["before>ring-a", "ring-b>after"]


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


def test_a_dependent_with_a_second_blocker_is_not_freed_immediately(
    lithos_lens_config_env: Path,
) -> None:
    """M is the SOLE-unsatisfied-blocker fact, not "this row mentions the focal
    task" (D10). A dependent Lithos also reports waiting on something else is
    not freed by completing this one, and counting it would promise the
    operator an unblocking that will not happen."""
    fake = GraphFakeClient(
        dataset(
            [task("root"), task("one"), task("pair"), task("other-blocker")],
            (
                ("root", "one", "blocks"),
                ("root", "pair", "blocks"),
                ("other-blocker", "pair", "blocks"),
            ),
            blocked={
                "one": (blocker("root"),),
                # Named by BOTH — and still blocked after `root` finishes.
                "pair": (blocker("root"), blocker("other-blocker")),
            },
        )
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "frees 2 in this graph, 1 immediately" in slot(html)


def test_an_open_gate_states_what_clearing_it_would_free(
    lithos_lens_config_env: Path,
) -> None:
    """D10 counts an open GATE like an open task — completing a gate is the
    operator's own move — and its `waits_on_gate` edges are in the active
    projection exactly as `blocks` edges are (D6)."""
    fake = GraphFakeClient(
        dataset(
            [task("review", task_type="gate"), task("waiter"), task("after")],
            (("review", "waiter", "waits_on_gate"), ("waiter", "after", "blocks")),
            blocked={
                "waiter": (
                    BlockerRecord(
                        kind="gate",
                        task_id="review",
                        type="waits_on_gate",
                        status="open",
                        message="Waiting on the review gate.",
                    ),
                ),
                "after": (blocker("waiter"),),
            },
        )
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=review"
    )

    assert "frees 2 in this graph, 1 immediately" in slot(html)


def test_a_failed_read_for_a_dependents_project_withholds_the_figure(
    lithos_lens_config_env: Path,
) -> None:
    """A failed read is silence, like a truncated one (D4): M is withheld
    rather than reported low, and N — Lens's own arithmetic — stands."""
    fake = GraphFakeClient(
        dataset(
            [task("root"), task("far", project="other")],
            (("root", "far", "blocks"),),
            blocked={"far": (blocker("root"),)},
        ),
        blocked_failures={"other", "project:other"},
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "frees 1 in this graph" in slot(html)
    assert "how many immediately is withheld" in slot(html)
    assert attribute(html, "data-impact-withheld") == "true"


def test_a_projectless_dependent_withholds_the_figure_with_no_read_for_it(
    lithos_lens_config_env: Path,
) -> None:
    """No scoped read can reach a task carrying no project under either
    convention, and Lens does not issue the unscoped one (D4) — so its
    sole-blocker fact does not exist and M is withheld rather than guessed."""
    fake = GraphFakeClient(
        dataset(
            [task("root"), task("nowhere", project=None)],
            (("root", "nowhere", "blocks"),),
            blocked={"nowhere": (blocker("root"),)},
        )
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "frees 1 in this graph" in slot(html)
    assert "how many immediately is withheld" in slot(html)
    assert "covered 0 of 1 dependents" in slot(html)
    # And no read was invented to cover it: the coverage set is this project.
    assert {call["project"] for call in fake.blocked_calls if call["project"]} == {
        PROJECT
    }


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
    absorbing a relation Lens could not classify.

    It also bounds the LIT SET, which is D8's separate claim and the other half
    of `_relations_exact` (round-2 test-quality f-008): every edge list here was
    read in full, so the scope is complete and the unknown EDGE is the only
    thing that can be making either statement a lower bound."""
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

    # No edge read failed, so nothing here degrades on scope incompleteness.
    assert 'data-graph-banner="edges-incomplete"' not in html
    assert "frees ≥ 1 in this graph" in slot(html)
    assert "Not counted, relation unreadable: far" in slot(html)
    # …and the picture around the focus is a lower bound for the same reason.
    assert "data-panel-focus-bound" in html
    assert "lower bound of what surrounds it" in slot(html)


def test_an_incomplete_scope_makes_both_the_count_and_the_lit_set_bounds(
    lithos_lens_config_env: Path,
) -> None:
    """D10 and D8 degrade on the SCOPE being incomplete, not on where the gap
    happens to fall in the projection Lens can already see: an unread edge list
    is exactly the evidence that the known projection may not be all of it.
    Here the failed read is UPSTREAM of the focus, where a reachability-only
    rule would call the count exact (round-1 correctness f-002)."""
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

    assert "frees ≥ 1 in this graph, 1 immediately" in slot(html)
    assert attribute(html, "data-impact-bound") == "lower_bound"
    # …and the picture says what it could not see, in both directions.
    assert "data-panel-focus-bound" in html
    assert "lower bound of what surrounds it" in slot(html)


def test_an_unreadable_task_elsewhere_in_the_scope_still_bounds_the_claims(
    lithos_lens_config_env: Path,
) -> None:
    """The same rule at its edge: the failed read is on a task with no path to
    the focus at all. A rule narrowed to the KNOWN neighbourhood would report
    both claims exact — and the one thing an unread edge list means is that
    what Lens knows about the neighbourhood may be short."""
    fake = GraphFakeClient(
        dataset(
            [task("root"), task("one"), task("apart"), task("apart-next")],
            (("root", "one", "blocks"), ("apart", "apart-next", "blocks")),
            blocked={"one": (blocker("root"),)},
        ),
        edge_failures={"apart"},
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert "frees ≥ 1 in this graph, 1 immediately" in slot(html)
    assert attribute(html, "data-impact-bound") == "lower_bound"
    assert "data-panel-focus-bound" in html


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


def test_an_epic_carries_no_impact_figures_at_all(
    lithos_lens_config_env: Path,
) -> None:
    """An epic has no `blocks` edges, so a zero here would read as "finishing
    this frees nobody" rather than "this is not that kind of task" (D10).

    `a -> b` is this scope's blocking projection, so the epic is off the
    longest chain and off the degraded paths too: the slot is empty because
    D10 states nothing for it, not because some other claim happened to be
    absent. The two claims a focused epic DOES carry — D7's position on the
    chain when it is on one, and D8's lower-bound note — are next door."""
    fake = GraphFakeClient(
        dataset(
            [task("epic", task_type="epic"), task("child"), task("a"), task("b")],
            (("epic", "child", "parent_child"), ("a", "b", "blocks")),
        )
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=epic"
    )

    assert slot(html) == ""


def test_a_focused_epic_still_says_when_its_lit_set_is_a_lower_bound(
    lithos_lens_config_env: Path,
) -> None:
    """Two rules, and the second must not be suppressed with the first
    (round-1 correctness f-001). D10 gives an epic no FIGURES — it carries no
    `blocks` edges, and a zero would read as "finishing this frees nobody".
    D8's lower-bound note is a claim about the CANVAS: an unreadable edge list
    anywhere in the scope means the lit set around the focused node is a lower
    bound, whatever kind of node it is, and a dimmed node would otherwise read
    as "unrelated" when Lens only failed to look."""
    fake = GraphFakeClient(
        dataset(
            [task("epic", task_type="epic"), task("child"), task("apart")],
            (("epic", "child", "parent_child"),),
        ),
        edge_failures={"apart"},
    )

    with client_for(lithos_lens_config_env, fake) as client:
        html = unescape(client.get(f"/tasks/graph?project={PROJECT}&focus=epic").text)
        # The same panel a CLICK on that node fetches, which is the other path
        # to it and re-assembles the scope on its own.
        clicked = client.get(
            f"/tasks/epic?fragment=panel&scope=project:{PROJECT}"
            f"&snapshot={snapshot(html)}"
        ).text

    for rendered in (html, clicked):
        # No numbers, and no sentence standing in for the ones D10 withholds —
        # including the "this graph has changed" one, which would be a claim
        # about figures that were never counted.
        assert "frees" not in slot(rendered)
        assert attribute(rendered, "data-impact-state") == ""
        # …and the statement about the picture, which is not D10's to withhold.
        assert "data-panel-focus-bound" in rendered
        assert "lower bound of what surrounds it" in slot(rendered)


def test_a_focused_epic_on_the_longest_chain_still_states_its_position(
    lithos_lens_config_env: Path,
) -> None:
    """D7's line is not D10's, and suppressing the figures must not take it
    with them (round-2 correctness f-005). This scope's blocking projection is
    one node wide, so the epic IS the scope's longest chain — the page says
    "Longest blocking chain (1)" over it — and the panel owes the position.
    The lower-bound note is absent here for its own reason: every edge list was
    read in full, so it is a DEGRADATION rather than a new line for epics."""
    fake = GraphFakeClient(
        dataset(
            [task("epic", task_type="epic"), task("child")],
            (("epic", "child", "parent_child"),),
        )
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=epic"
    )

    assert "On the longest chain (1 of 1)." in slot(html)
    # …and still no figures, and nothing standing in for them.
    assert "frees" not in slot(html)
    assert attribute(html, "data-impact-state") == ""
    assert "data-panel-focus-bound" not in html


def test_a_focused_epic_over_a_moved_graph_says_so_without_the_frees_wording(
    lithos_lens_config_env: Path,
) -> None:
    """The fingerprint check runs BEFORE the focal node is classified, so an
    epic clicked after the graph moved was answered with the general stale
    sentence — "refresh to see what completing this frees" — which is a
    future-tense impact line, and the one D10 says an epic never carries
    (round-2 correctness f-004). The move is real and worth stating, so it is
    stated without the figures it has none of; the canvas notes go with them,
    being claims about a picture this assembly is no longer of."""
    tasks = [task("epic", task_type="epic"), task("child"), task("apart")]
    fake = GraphFakeClient(dataset(tasks, (("epic", "child", "parent_child"),)))

    with client_for(lithos_lens_config_env, fake) as client:
        page = unescape(client.get(f"/tasks/graph?project={PROJECT}").text)
        drawn = snapshot(page)
        # Lithos moves under the page: one more task, so the assembly the
        # panel makes for itself is not the graph on screen.
        fake.replace_dataset(
            dataset([*tasks, task("late")], (("epic", "child", "parent_child"),))
        )
        panel = client.get(
            f"/tasks/epic?fragment=panel&scope=project:{PROJECT}&snapshot={drawn}"
        ).text

    assert "This graph has changed" in slot(panel)
    assert "frees" not in slot(panel)
    assert "completing this" not in slot(panel).lower()
    assert attribute(panel, "data-impact-stale") == "true"
    # The notes are withheld with the figures: they describe the assembly this
    # panel just made, which is not the one the canvas is drawing.
    assert "data-panel-focus-bound" not in panel
    assert "longest chain" not in slot(panel)


def test_one_blocker_reported_twice_with_two_messages_stays_one_blocker(
    lithos_lens_config_env: Path,
) -> None:
    """D4's read pair is two independent samples, not one snapshot.

    A gate's blocker message carries its `ready_at`, so an update between the
    two calls comes back as different TEXT in the second response while the
    blocking fact — kind, predecessor, type, status — stands still. Merging on
    the whole record keeps both copies, and M, which reads the merged tuple's
    LENGTH, then sees two blockers where Lithos reported one and withholds
    "immediately" from a dependent this gate alone is blocking (round-1
    correctness f-002)."""
    waiting = task("waiting")

    def rows(message: str) -> list[BlockedTaskRecord]:
        return [
            BlockedTaskRecord(
                task=waiting,
                blockers=(
                    BlockerRecord(
                        kind="gate",
                        task_id="gate",
                        type="waits_on_gate",
                        status="open",
                        message=message,
                    ),
                ),
            )
        ]

    fake = GraphFakeClient(
        dataset(
            [task("gate", task_type="gate"), waiting],
            (("gate", "waiting", "waits_on_gate"),),
        ),
        # The metadata half answers first with the old `ready_at`; the tag half
        # answers with the new one, the same blocker either way.
        blocked_rows={
            PROJECT: rows("Waiting on gate: ready at 09:00."),
            f"project:{PROJECT}": rows("Waiting on gate: ready at 10:00."),
        },
    )

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=gate"
    )

    assert "frees 1 in this graph, 1 immediately" in slot(html)
    assert attribute(html, "data-impact-withheld") == ""


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


def test_the_fragment_route_counts_an_epic_scope_the_way_its_page_drew_it(
    lithos_lens_config_env: Path,
) -> None:
    """`scope=epic:<id>` is the other half of the panel's contract (D10), and
    it is a DIFFERENT assembly from a project's: membership comes from
    `task_children` rather than from §5B.1 projects, `include_resolved`
    defaults the opposite way — a finished child is half of an initiative's
    progress — and the coverage set spans every project those children sit in
    (D4). The claim is made against the PAGE's own snapshot, so the panel's
    independent assembly has to reproduce the graph the epic page drew, node
    for node and read for read, or the fingerprint check answers "this graph
    has changed" instead of a number.

    `child` blocks `mid`, which blocks `far` in another project; `done` is the
    closed child, a node here by the epic default and still not something
    completing `child` frees.
    """
    fake = GraphFakeClient(
        dataset(
            [
                task("epic", task_type="epic"),
                task("child"),
                task("mid"),
                task("far", project="other"),
                task("done", status="completed"),
            ],
            (
                ("epic", "child", "parent_child"),
                ("epic", "mid", "parent_child"),
                ("epic", "far", "parent_child"),
                ("epic", "done", "parent_child"),
                ("child", "mid", "blocks"),
                ("mid", "far", "blocks"),
                ("child", "done", "blocks"),
            ),
            children={"epic": ("child", "mid", "far", "done")},
            blocked={"mid": (blocker("child"),), "far": (blocker("mid"),)},
        )
    )

    with client_for(lithos_lens_config_env, fake) as client:
        page = unescape(client.get("/tasks/graph?epic=epic&focus=child").text)
        panel = client.get(
            f"/tasks/child?fragment=panel&scope=epic:epic&snapshot={snapshot(page)}"
        ).text

    # N is the two OPEN transitive dependents; the closed child is a node in
    # this scope (the epic default) and its edge is satisfied, not pending.
    # M is `mid`, the one whose blocked row names `child` as its sole blocker.
    assert 'data-graph-node="done"' in page
    assert "frees 2 in this graph, 1 immediately" in slot(page)
    # The panel assembled the epic scope on its own and answered the same —
    # which is what the page's snapshot pins.
    assert "frees 2 in this graph, 1 immediately" in slot(panel)
    assert "This graph has changed" not in slot(panel)
    # …including M's coverage across BOTH projects the children span: `far`'s
    # own project is read, or its absence from a `loom` response would be
    # silence rather than an answer.
    assert (None, ["project:other"]) in [
        (call["project"], call["tags"]) for call in fake.blocked_calls
    ]


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
    # A drawn scope loads the canvas half too — the gate that keeps Cytoscape
    # off an empty page must not keep it off a full one.
    assert "cytoscape.min.js" in html
    assert "graph.js" in html
    # And the identity of the graph it DREW, on both halves too: the scope name
    # fixes which tasks are asked for, not which ones came back.
    assert f"snapshot={snapshot(html)}" in unescape(html)
    assert snapshot(html)


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


def test_the_fragment_routes_reads_carry_the_configured_frontier_limit(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M rides on D4's coverage reads, so it inherits THEIR bound rather than
    a limit of its own — and the bound is what makes `len == limit` mean
    truncation at all. Asserted on the calls the panel's own route issued,
    because that route assembles the scope itself."""
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", "5")
    fake = GraphFakeClient(impact_dataset())

    with client_for(lithos_lens_config_env, fake) as client:
        text = client.get(f"/tasks/root?fragment=panel&scope=project:{PROJECT}").text

    assert "frees 3 in this graph, 1 immediately" in slot(text)
    assert fake.blocked_calls, "the panel made no coverage read at all"
    assert [call["limit"] for call in fake.blocked_calls] == [5, 5]
    assert [(call["project"], call["tags"]) for call in fake.blocked_calls] == [
        (PROJECT, None),
        (None, [f"project:{PROJECT}"]),
    ]


#: The scope both f-006 regressions start from: `above`'s edge list fails, so
#: the scope is incomplete and every claim about the focus's neighbourhood is a
#: lower bound; `root` sits in the middle of the three-node blocking chain.
def bounded_dataset(
    blocked: dict[str, tuple[BlockerRecord, ...]] | None = None,
    relink: bool = False,
) -> FakeLithosDataset:
    tasks = [task("above"), task("root"), task("one")]
    edges = [("above", "root", "blocks"), ("root", "one", "blocks")]
    if relink:
        # The PICTURE moving: a node and an edge the page never drew. (An edge
        # retype alone cannot be staged from HERE — the panel re-reads edges
        # through the warm per-task cache, so it would see the drawn ones and
        # agree. Which field belongs to which HALF is pinned directly, on the
        # fingerprint, by `MATERIAL_HALVES` below.)
        tasks.append(task("late"))
        edges.append(("root", "late", "blocks"))
    return dataset(
        tasks,
        edges,
        blocked=blocked or {"one": (blocker("root"),), "root": (blocker("above"),)},
    )


def test_a_blocked_row_that_moved_withholds_the_figures_and_keeps_the_notes(
    lithos_lens_config_env: Path,
) -> None:
    """The fingerprint has two halves because a panel has two kinds of claim
    (round-3 correctness f-006). Here only Lithos's blocked row moves — `one`
    picks up a second blocker, which is exactly the eventless change M is
    fingerprinted against — while the nodes, the edges, the unread edge list
    and the chain are identical. The FIGURES still go: they were counted over
    reads the drawn answer no longer matches. D8's lower bound and D7's
    position do not: they are claims about the picture on screen, and that
    picture has not moved."""
    fake = GraphFakeClient(bounded_dataset(), edge_failures={"above"})

    with client_for(lithos_lens_config_env, fake) as client:
        page = unescape(client.get(f"/tasks/graph?project={PROJECT}").text)
        drawn = snapshot(page)
        fake.replace_dataset(
            bounded_dataset(
                {
                    "one": (blocker("root"), blocker("other")),
                    "root": (blocker("above"),),
                }
            )
        )
        panel = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}&snapshot={drawn}"
        ).text

    assert "This graph has changed" in slot(panel)
    assert attribute(panel, "data-impact-frees") == ""
    # …and both statements about the canvas survive it.
    assert "data-panel-focus-bound" in panel
    assert "lower bound of what surrounds it" in slot(panel)
    assert "On the longest chain (2 of 3)." in slot(panel)


#: What the graph page's client says it is drawing around `root` in
#: `bounded_dataset()`: a lit set that is a lower bound, and the middle step of
#: the three-node chain. Read off the payload by `graph.js` and stated back on
#: every panel it fetches (`graph_snapshot.CanvasNotes`).
DRAWN_AROUND_ROOT = "&canvas_bound=lower&canvas_chain=2:3"


def test_a_picture_that_moved_still_states_what_the_canvas_is_showing(
    lithos_lens_config_env: Path,
) -> None:
    """The case the halves alone cannot answer (round-4 correctness f-006). A
    node and an edge appear, so the graph this panel just assembled is not the
    one being drawn — D8 leaves the canvas where it is — and the FIGURES go
    with the disagreement. D7's position and D8's lower bound do not: the
    operator is still looking at `root` lit in the middle of the same
    three-node chain, and the client says so when it asks. The panel states
    what the canvas shows rather than what its own rebuild found."""
    fake = GraphFakeClient(bounded_dataset(), edge_failures={"above"})

    with client_for(lithos_lens_config_env, fake) as client:
        drawn = snapshot(unescape(client.get(f"/tasks/graph?project={PROJECT}").text))
        fake.replace_dataset(bounded_dataset(relink=True))
        described = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}"
            f"&snapshot={drawn}{DRAWN_AROUND_ROOT}"
        ).text
        # The same fetch from something that is drawing NOTHING — no canvas to
        # describe, so no claim about one.
        undescribed = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}&snapshot={drawn}"
        ).text

    assert "This graph has changed" in slot(described)
    assert attribute(described, "data-impact-frees") == ""
    assert "lower bound of what surrounds it" in slot(described)
    assert "On the longest chain (2 of 3)." in slot(described)
    # Withheld, never invented: a caller that describes no canvas gets the
    # figures withheld AND no statement about a picture nobody named.
    assert "data-panel-focus-bound" not in undescribed
    assert "longest chain" not in slot(undescribed)


def test_a_scope_that_can_no_longer_answer_still_states_the_canvas(
    lithos_lens_config_env: Path,
) -> None:
    """Earlier than staleness, and the same rule (round-4 correctness f-006):
    the rebuilt scope no longer holds the focus at all — here `root` resolved
    out of an `include_resolved=0` project graph — so there are no figures to
    withhold and nothing D10 can say. The canvas is still drawing it, focused,
    on the same chain, so the notes are still owed."""
    fake = GraphFakeClient(bounded_dataset(), edge_failures={"above"})

    with client_for(lithos_lens_config_env, fake) as client:
        drawn = snapshot(unescape(client.get(f"/tasks/graph?project={PROJECT}").text))
        fake.replace_dataset(
            dataset(
                [task("above"), task("root", status="completed"), task("one")],
                [("above", "root", "blocks"), ("root", "one", "blocks")],
            )
        )
        panel = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}"
            f"&snapshot={drawn}{DRAWN_AROUND_ROOT}"
        ).text

    assert "frees" not in slot(panel)
    assert "lower bound of what surrounds it" in slot(panel)
    assert "On the longest chain (2 of 3)." in slot(panel)


def bounded_components_fake() -> GraphFakeClient:
    """Two disconnected, COMPLETE components, one of them degraded.

    `a1 -> a2 -> a3` is the scope's longest chain and `a3` also points at a
    ghost whose status could not be read, so that edge is `unknown` and every
    node in A's component sits beside a relation Lens cannot classify.
    `b1 -> b2` is its own component with nothing unreadable anywhere near it.
    No edge LIST failed, so the scope itself is complete — which is what makes
    this a test of D8's LOCAL caveat rather than of the scope-wide one.
    """
    return GraphFakeClient(
        dataset(
            [task(name) for name in ("a1", "a2", "a3", "b1", "b2")],
            (
                ("a1", "a2", "blocks"),
                ("a2", "a3", "blocks"),
                ("a3", "gone", "blocks"),
                ("b1", "b2", "blocks"),
            ),
        ),
        get_failures={"gone"},
    )


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_lower_bound_is_per_component_from_the_payload_to_the_request(
    lithos_lens_config_env: Path,
) -> None:
    """The producer and the consumer of D8's lower bound (round-5 test-quality
    f-004).

    With a complete scope the caveat is LOCAL: it belongs to the focused node's
    own neighbourhood, so an `unknown` edge hanging off one component says
    nothing about a task in another. The server answers that per node and puts
    it in the payload; the canvas states it back for whichever node the
    operator clicks. Both halves are checked against the same assembled graph,
    because a rule that degenerated to "any unknown edge bounds everything" —
    or a `bound` that never left the server — would still leave both sides
    self-consistent.
    """
    fake = bounded_components_fake()
    page = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    drawn = payload(page)

    # The PRODUCER: per node, per component.
    bound = {node["id"]: node["bound"] for node in drawn["nodes"]}
    assert bound["a1"] is True
    assert bound["a2"] is True
    assert bound["a3"] is True
    assert bound["b1"] is False
    assert bound["b2"] is False

    # …and the sentence the server itself renders for each focus, which is what
    # the panel below has to go on saying after a client-side transition.
    degraded = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=a2"
    )
    healthy = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=b1"
    )
    assert "lower bound of what surrounds it" in slot(degraded)
    assert "On the longest chain (2 of 3)." in slot(degraded)
    assert "lower bound of what surrounds it" not in slot(healthy)
    assert "longest chain" not in slot(healthy)

    # The CONSUMER: the same two facts, off the same payload, on the panel
    # request each click makes.
    inside = _graph_run(
        ["tap:a2"],
        href=f"http://lens.test/tasks/graph?project={PROJECT}",
        payload=drawn,
    )
    outside = _graph_run(
        ["tap:b1"],
        href=f"http://lens.test/tasks/graph?project={PROJECT}",
        payload=drawn,
    )

    assert inside["fetches"] == [
        "/tasks/id?task_id=a2&fragment=panel&canvas_bound=lower&canvas_chain=2%3A3"
    ]
    # `b1` is in the clean component AND off the scope's longest chain, so it
    # claims neither — a payload that carried one flag for the whole graph
    # would have it saying `lower` here.
    assert outside["fetches"] == [
        "/tasks/id?task_id=b1&fragment=panel&canvas_bound=exact"
    ]


def test_an_offline_panel_still_states_what_the_canvas_is_showing(
    lithos_lens_config_env: Path,
) -> None:
    """Lithos going quiet between the page load and the click costs the task
    detail and D10's figures — not the two statements about the picture
    (round-5 correctness f-008).

    Those describe the graph this page is still drawing and they arrive IN the
    request, so no live read is involved in either. The fragment is a 200 the
    client swaps in and pushes `focus=` behind, so a panel that dropped them
    would leave a focused, lower-bound canvas claiming an exact one — beside a
    panel that never said so.
    """
    healthy = GraphFakeClient(bounded_dataset(), edge_failures={"above"})
    # The same fixture behind a health probe that has gone red. Two clients
    # rather than one because the probe is cached for `health.refresh_interval_s`
    # (minimum 1s) — the ORDERING under test is the page's render, then a panel
    # request that finds Lithos unavailable.
    offline = GraphFakeClient(
        bounded_dataset(), edge_failures={"above"}, health="unreachable"
    )

    with client_for(lithos_lens_config_env, healthy) as client:
        drawn = snapshot(unescape(client.get(f"/tasks/graph?project={PROJECT}").text))
    with client_for(lithos_lens_config_env, offline) as client:
        panel = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}"
            f"&snapshot={drawn}{DRAWN_AROUND_ROOT}"
        ).text

    # The outage is still reported, and no task detail is invented for it …
    assert 'data-panel-state="offline"' in panel
    # … while the canvas beside the panel is described as it is.
    assert "lower bound of what surrounds it" in slot(panel)
    assert "On the longest chain (2 of 3)." in slot(panel)
    # No figures: D10's are counted over an assembly this request cannot make.
    assert "frees" not in slot(panel)
    assert attribute(panel, "data-impact-frees") == ""


def test_a_canvas_annotation_cannot_push_a_panel_past_the_filter_ceiling(
    lithos_lens_config_env: Path,
) -> None:
    """Lens's own annotations are not the operator's filters, and must not be
    measured as though they were (round-5 correctness f-009).

    The board's preserved filters ride on every panel URL the server builds, so
    a filter set the router ALREADY ACCEPTED can sit exactly at
    `MAX_FILTER_QUERY_BYTES` — and the panel of the node that page just drew
    has to be answerable. Under a key the budget measures (the blocker trail's
    `chain` is one) appending `2:3` would carry that request past the ceiling
    and the fragment would be refused at 400, which `tasks.js` answers by
    CLEARING the panel: Back would land on a focused node with no panel at all.
    """
    at_ceiling = "p" * (MAX_FILTER_QUERY_BYTES - len(urlencode([("project", "")])))
    fake = GraphFakeClient(bounded_dataset(), edge_failures={"above"})

    with client_for(lithos_lens_config_env, fake) as client:
        drawn = snapshot(unescape(client.get(f"/tasks/graph?project={PROJECT}").text))
        fragment = (
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}"
            f"&snapshot={drawn}{DRAWN_AROUND_ROOT}&project="
        )
        panel = client.get(fragment + at_ceiling)
        # One byte more of FILTER is over the ceiling and refused, which is what
        # makes the request above exactly at it rather than comfortably under.
        over = client.get(fragment + at_ceiling + "p")

    assert panel.status_code == 200
    assert "data-filter-rejected" not in panel.text
    assert "lower bound of what surrounds it" in slot(panel.text)
    assert "On the longest chain (2 of 3)." in slot(panel.text)
    assert over.status_code == 400


def test_a_panel_whose_task_read_failed_still_states_the_canvas(
    lithos_lens_config_env: Path,
) -> None:
    """D7's and D8's statements are about the PICTURE, so they cannot be
    conditional on the later `task_get` succeeding (round-4 correctness f-007).
    The canvas has `root` focused and its neighbourhood lit either way, and a
    panel that renders "Task unavailable" beside it while dropping both
    statements leaves a lower-bound picture reading as an exact one."""
    # The page reads no `task_get` for an in-scope node, so the failure lands
    # on the panel's own read and nothing else.
    fake = GraphFakeClient(
        bounded_dataset(), edge_failures={"above"}, get_failures={"root"}
    )

    with client_for(lithos_lens_config_env, fake) as client:
        drawn = snapshot(unescape(client.get(f"/tasks/graph?project={PROJECT}").text))
        panel = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}"
            f"&snapshot={drawn}{DRAWN_AROUND_ROOT}"
        ).text

    # The read failure is still reported — the panel does not pretend to have
    # the task …
    assert 'data-panel-state="error"' in panel
    # … and the canvas beside it is still described.
    assert "lower bound of what surrounds it" in slot(panel)
    assert "On the longest chain (2 of 3)." in slot(panel)
    # No figures: the focal status this panel could not read is what D10 counts
    # against, so nothing here is a count.
    assert "frees" not in slot(panel)


def test_a_focused_epic_is_not_stale_when_only_the_blocked_rows_moved(
    lithos_lens_config_env: Path,
) -> None:
    """An epic's whole line is those notes — D10 states no figures for it — so
    the answer half cannot make it wrong. With the picture unchanged there is
    nothing to refresh, and saying so would cost the operator the only thing
    the panel had to tell them."""
    tasks = [task("epic", task_type="epic"), task("child"), task("apart")]
    edges = (("epic", "child", "parent_child"),)
    fake = GraphFakeClient(dataset(tasks, edges), edge_failures={"apart"})

    with client_for(lithos_lens_config_env, fake) as client:
        drawn = snapshot(unescape(client.get(f"/tasks/graph?project={PROJECT}").text))
        fake.replace_dataset(
            dataset(tasks, edges, blocked={"child": (blocker("apart"),)})
        )
        panel = client.get(
            f"/tasks/epic?fragment=panel&scope=project:{PROJECT}&snapshot={drawn}"
        ).text

    assert "This graph has changed" not in slot(panel)
    assert "frees" not in slot(panel)
    # Its WHOLE note set, which is its whole line: D8's lower bound and D7's
    # position (round-4 test-quality f-003).
    assert "lower bound of what surrounds it" in slot(panel)
    assert "On the longest chain (1 of 1)." in slot(panel)


def test_a_panel_counting_over_a_moved_graph_says_so_instead_of_a_number(
    lithos_lens_config_env: Path,
) -> None:
    """The panel fetched on its own re-assembles the scope, and the scope NAME
    only fixes which tasks are asked for — not which ones come back. Between
    the page's read and the click, Lithos can move: here a fourth dependent
    appears, so a count taken now would say 4 while the canvas — which D8
    deliberately does not re-lay-out — is still drawing the three-node picture
    the page loaded with. "Frees N in this graph" would then name two different
    graphs at once, so the panel says the graph changed and withholds both
    figures."""
    fake = GraphFakeClient(impact_dataset())

    with client_for(lithos_lens_config_env, fake) as client:
        page = unescape(client.get(f"/tasks/graph?project={PROJECT}&focus=root").text)
        drawn = snapshot(page)
        # Lithos moves: `one` picks up a fourth task waiting on it.
        fake.replace_dataset(
            dataset(
                [task(name) for name in (*IMPACT_TASKS, "four")],
                (*IMPACT_EDGES, ("one", "four", "blocks")),
                blocked={
                    "one": (blocker("root"),),
                    "two": (blocker("one"),),
                    "three": (blocker("one"),),
                    "four": (blocker("one"),),
                },
            )
        )
        stale = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}&snapshot={drawn}"
        ).text
        # The SAME fetch without the page's snapshot is the number the moved
        # graph yields — which is exactly what must not be printed beside the
        # picture above.
        unpinned = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}"
        ).text

    assert "frees 3 in this graph, 1 immediately" in slot(page)
    assert "This graph has changed" in slot(stale)
    assert "in this graph" not in slot(stale)
    assert attribute(stale, "data-impact-frees") == ""
    assert attribute(stale, "data-impact-state") == "stale"
    assert "frees 4 in this graph" in slot(unpinned)


def test_a_panel_over_the_graph_still_on_screen_counts_it_as_before(
    lithos_lens_config_env: Path,
) -> None:
    """The other half of that rule, and the one that keeps it from withholding
    the line forever: a snapshot the re-assembled graph still matches is the
    page's own graph, warm from the per-task cache, so the click answers
    exactly what the server-rendered panel did — and a RELOAD after the change
    counts the new graph, because that is the picture on screen then."""
    fake = GraphFakeClient(impact_dataset())

    with client_for(lithos_lens_config_env, fake) as client:
        drawn = snapshot(
            unescape(client.get(f"/tasks/graph?project={PROJECT}&focus=root").text)
        )
        same = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}&snapshot={drawn}"
        ).text
        fake.replace_dataset(
            dataset(
                [task(name) for name in (*IMPACT_TASKS, "four")],
                (*IMPACT_EDGES, ("one", "four", "blocks")),
                blocked={
                    "one": (blocker("root"),),
                    "two": (blocker("one"),),
                    "three": (blocker("one"),),
                    "four": (blocker("one"),),
                },
            )
        )
        reloaded = unescape(
            client.get(f"/tasks/graph?project={PROJECT}&focus=root").text
        )
        after = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}"
            f"&snapshot={snapshot(reloaded)}"
        ).text

    assert "frees 3 in this graph, 1 immediately" in slot(same)
    assert "frees 4 in this graph, 1 immediately" in slot(reloaded)
    assert "frees 4 in this graph, 1 immediately" in slot(after)


def test_a_fingerprint_follows_the_graph_and_not_the_tasks_own_text(
    lithos_lens_config_env: Path,
) -> None:
    """What the fingerprint is OVER: the nodes and the edges D10's figures are
    derived from. A re-titled task moves no count and no lit set, so it must
    not withhold the line — a detector that fired on every edit would cost the
    impact line permanently rather than when it is actually wrong."""
    fake = GraphFakeClient(impact_dataset())

    with client_for(lithos_lens_config_env, fake) as client:
        before = snapshot(
            unescape(client.get(f"/tasks/graph?project={PROJECT}&focus=root").text)
        )
        fake.replace_dataset(
            dataset(
                [task(name, title=f"Renamed {name}") for name in IMPACT_TASKS],
                IMPACT_EDGES,
                blocked={
                    "one": (blocker("root"),),
                    "two": (blocker("one"),),
                    "three": (blocker("one"),),
                },
            )
        )
        renamed = unescape(
            client.get(f"/tasks/graph?project={PROJECT}&focus=root").text
        )
        panel = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}&snapshot={before}"
        ).text

    assert snapshot(renamed) == before
    assert "frees 3 in this graph, 1 immediately" in slot(panel)


def node_ids(html: str) -> list[str]:
    """The node membership of a rendered page, from its own payload."""
    return sorted(node["id"] for node in payload(html)["nodes"])


def moved_blocked_dataset() -> FakeLithosDataset:
    """The acceptance fixture after an EDGE UPSERT this page cannot see.

    `other -> one` lands in Lithos. Edge upserts emit no event (ROADMAP gap
    #1), `other` is in another project so this scope never reads it, and
    `one`'s own edge entry is warm — so every edge Lens fetches is the one it
    already had. Only Lithos's blocked row for `one` has moved, and M with it.
    """
    return dataset(
        [task(name) for name in IMPACT_TASKS] + [task("other", project="elsewhere")],
        (*IMPACT_EDGES, ("other", "one", "blocks")),
        blocked={
            "one": (blocker("root"), blocker("other")),
            "two": (blocker("one"),),
            "three": (blocker("one"),),
        },
    )


def resolved_focus_dataset() -> FakeLithosDataset:
    """The impact fixture after its FOCAL task completed — the same graph,
    minus the blocked row Lithos stops reporting for a resolved task."""
    return dataset(
        [task("root", status="completed"), task("one"), task("two"), task("three")],
        IMPACT_EDGES,
        blocked={"two": (blocker("one"),), "three": (blocker("one"),)},
    )


class CompletingClient(GraphFakeClient):
    """A fake whose focal task completes the moment the PANEL reads it.

    The window the graph route really has: `load_graph_page` returns, and only
    then does `_focused_panel` issue its own `task_get` for the focused task
    (the two are deliberately sequential — the scope assembly holds the fan-out
    gate for its whole duration). A task that resolves inside that window is an
    ORDINARY concurrent completion, and the swap here is exactly it: the graph
    was assembled against the open board, the panel's own read is not.
    """

    def __init__(self, before: FakeLithosDataset, after: FakeLithosDataset, focus: str):
        super().__init__(before)
        self._after = after
        self._focus = focus
        self.completed = False

    async def task_get(self, task_id: str) -> TaskRecord:
        if task_id == self._focus and not self.completed:
            self.completed = True
            self.replace_dataset(self._after)
        return await super().task_get(task_id)


def test_a_focus_that_completes_while_the_page_reads_it_says_it_completed(
    lithos_lens_config_env: Path,
) -> None:
    """A focused panel is TWO reads: the impact is this render's arithmetic
    over the graph it assembled, and the badge above it is a later `task_get`.
    A task that completes between them would put "Completing this frees 3 in
    this graph" under a `completed` badge — the one sentence D10 says a
    resolved task must never carry (round-2 correctness f-002).

    The figures go rather than the badge, and what replaces them is D10's own
    answer for a resolved task rather than a refresh notice: "completed; no
    pending impact" needs no arithmetic, and telling the operator to refresh —
    in the future tense D10 forbids a resolved task — would be the same defect
    in another sentence (round-3 correctness f-002)."""
    fake = CompletingClient(impact_dataset(), resolved_focus_dataset(), "root")

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=root"
    )

    assert fake.completed, "the panel never read the focal task"
    # The badge the operator sees is the panel's own read …
    assert 'class="badge badge-completed">completed</span>' in html
    # … and under it, the line D10 gives that state — no numbers, no future
    # tense, no refresh notice.
    assert slot(html).startswith("This task is completed; no pending impact.")
    assert attribute(html, "data-impact-state") == "completed"
    assert attribute(html, "data-impact-frees") == ""
    assert "in this graph" not in slot(html)
    assert "refresh" not in slot(html)
    # The rest of the line belongs to the GRAPH, not to the focal status, so it
    # survives: this task still sits where it did on the longest chain.
    assert "On the longest chain (1 of 3)." in slot(html)


def isolate_dataset(solo: TaskStatusName = "open") -> FakeLithosDataset:
    """One ISOLATED task beside a pair that is not, in the same project.

    An isolate is the shape that leaves the graph entirely when it resolves: no
    edge names it, so a project scope with `include_resolved=0` — the default —
    stops holding it the moment it is no longer on the open list.
    """
    return dataset(
        [task("solo", status=solo), task("root"), task("one")],
        (("root", "one", "blocks"),),
        blocked={"one": (blocker("root"),)},
    )


def test_a_focus_that_resolved_out_of_the_graph_still_says_it_completed(
    lithos_lens_config_env: Path,
) -> None:
    """The panel can lose its focus from the GRAPH and still owe D10's sentence.

    A completed task leaves an `include_resolved=0` project scope: the master
    lists open tasks only, and no edge names an isolate, so the rebuilt graph
    has no node to count over and the impact assembles to nothing at all. The
    panel's own read still says `completed`, and "completed; no pending impact"
    is a statement about the TASK rather than a count over a graph — so an
    empty slot there is the same D10 violation as a future-tense one (round-4
    correctness f-002)."""
    fake = GraphFakeClient(isolate_dataset())
    page_url = f"/tasks/graph?project={PROJECT}&focus=solo&isolated=1"

    with client_for(lithos_lens_config_env, fake) as client:
        page = unescape(client.get(page_url).text)
        drawn = snapshot(page)
        fake.replace_dataset(isolate_dataset(solo="completed"))
        panel = client.get(
            f"/tasks/solo?fragment=panel&scope=project:{PROJECT}&snapshot={drawn}"
        ).text

    # The task really did leave the graph the panel re-assembles …
    assert 'data-graph-node="solo"' in page
    assert 'class="badge badge-completed">completed</span>' in panel
    # … and the line is D10's, not an empty slot.
    assert slot(panel) == "This task is completed; no pending impact."
    assert attribute(panel, "data-impact-state") == "completed"


def test_a_focus_cancelled_out_of_the_graph_says_so_on_the_page_too(
    lithos_lens_config_env: Path,
) -> None:
    """The same rule on the SERVER-rendered panel, and for the cancelled
    wording — and here it needs no race at all: a deep link to an isolate that
    resolved BEFORE the page was asked for renders a graph that never held it,
    so this render computes no impact, while the panel read beside it names the
    state D10 has words for."""
    fake = GraphFakeClient(isolate_dataset(solo="cancelled"))

    html = get(
        lithos_lens_config_env,
        fake,
        f"/tasks/graph?project={PROJECT}&focus=solo&isolated=1",
    )

    # The graph really does not hold it …
    assert 'data-graph-node="solo"' not in html
    assert 'class="badge badge-cancelled">cancelled</span>' in html
    # … and the panel still carries D10's line for a cancelled focus.
    assert slot(html) == "This task is cancelled — its dependents are unsatisfiable."


def test_an_empty_scope_still_opens_the_panel_its_focus_names(
    lithos_lens_config_env: Path,
) -> None:
    """The boundary the node count used to swallow: a project whose only task
    has completed draws NOTHING under the default `include_resolved=0`, and the
    page said so and nothing else — no panel for the `focus=` it was given, and
    so no impact line either. `focus=` owes its panel whatever the canvas has
    to show (D9), and a resolved focus owes D10's sentence wherever it is
    rendered (round-5 correctness f-002). The empty graph itself is untouched:
    the "nothing to draw" banner is the honest answer to the SCOPE."""
    fake = GraphFakeClient(dataset([task("done", status="completed")]))

    html = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&focus=done"
    )

    assert "data-graph-empty" in html, "the scope was meant to draw nothing"
    assert 'data-panel-task="done"' in html
    assert 'class="badge badge-completed">completed</span>' in html
    assert slot(html) == "This task is completed; no pending impact."
    # And the panel CONTROLLER comes with it. A server-rendered panel with no
    # `tasks.js` behind it is a panel whose Close reloads the document and
    # whose Escape does nothing — D8 requires both to remove `focus` by
    # `pushState` (round-6 correctness f-004). The canvas half stays gated on
    # there being something to draw.
    assert f'panelScope: "project:{PROJECT}"' in html
    assert "tasks.js" in html
    assert "cytoscape.min.js" not in html
    assert "graph.js" not in html


def test_a_resolved_focus_keeps_its_line_when_the_scope_read_fails(
    lithos_lens_config_env: Path,
) -> None:
    """The degraded path through the same rule. D10's resolved wording needs no
    graph behind it, so a scope read that FAILS costs the figures — which a
    resolved task has none of anyway — and not the sentence. Asserted through
    the route rather than on the helper, because the promise is the panel's:
    an `except` that went back to answering `None` would empty this slot with
    every other regression still green (round-5 test-quality f-002)."""
    fake = GraphFakeClient(
        dataset([task("done", status="completed"), task("one")]),
        # The impact's own master read, and only that: the detail beside it
        # reads the task itself and is untouched.
        list_failures={"open"},
    )

    with client_for(lithos_lens_config_env, fake) as client:
        panel = client.get(f"/tasks/done?fragment=panel&scope=project:{PROJECT}").text

    assert any(call["status"] == "open" for call in fake.list_calls), (
        "the impact never attempted the read that was meant to fail"
    )
    assert 'class="badge badge-completed">completed</span>' in panel
    assert slot(panel) == "This task is completed; no pending impact."
    assert attribute(panel, "data-impact-frees") == ""


def test_a_panel_that_asked_for_no_impact_gains_none_from_a_resolved_task(
    lithos_lens_config_env: Path,
) -> None:
    """The guards on that rule, which are the reason it is not simply "state it
    whenever the task is resolved": a panel with no `scope=` asked for no
    impact line at all (the dashboard's), and an epic is given no FIGURES and
    no resolved wording by D10 whatever its state. Neither may gain one from
    the branch above. (The epic's chain position is a separate claim, D7's, and
    it is stated — see `test_a_focused_epic_on_the_longest_chain_...`.)"""
    fake = GraphFakeClient(
        dataset(
            [
                task("solo", status="completed"),
                task("epic", task_type="epic", status="completed"),
                task("child"),
            ],
            (("epic", "child", "parent_child"),),
        )
    )

    with client_for(lithos_lens_config_env, fake) as client:
        unscoped = client.get("/tasks/solo?fragment=panel").text
        epic = client.get(f"/tasks/epic?fragment=panel&scope=project:{PROJECT}").text

    assert slot(unscoped) == ""
    assert "no pending impact" not in slot(epic)
    assert "frees" not in slot(epic)
    assert attribute(epic, "data-impact-state") == ""


class CompletingMidPanelClient(GraphFakeClient):
    """Completes the focal task between the panel's DETAIL read and its count.

    The fragment route reads the detail first and assembles the impact after
    it (`web.task_detail`), and the impact's first call is the master task
    list — so swapping there is the completion that lands between one panel's
    two reads.
    """

    def __init__(self, before: FakeLithosDataset, after: FakeLithosDataset):
        super().__init__(before)
        self._after = after
        self.completed = False

    async def list_tasks(self, **kwargs: object) -> list[TaskRecord]:
        if not self.completed:
            self.completed = True
            self.replace_dataset(self._after)
        return await super().list_tasks(**kwargs)


def test_a_focus_that_completes_before_the_fragments_count_carries_none_either(
    lithos_lens_config_env: Path,
) -> None:
    """The same join on the CLICKED panel, where the two reads run the other
    way round: `_load_detail` first, the impact's own assembly after it. A task
    that completes in between leaves an `open` badge over "This task is
    completed; no pending impact" — contradictory in the other direction, and
    withheld for the same reason."""
    fake = CompletingMidPanelClient(impact_dataset(), resolved_focus_dataset())

    with client_for(lithos_lens_config_env, fake) as client:
        panel = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}&include_resolved=1"
        ).text

    assert fake.completed, "the panel never assembled an impact at all"
    # The badge is the panel's own read, taken before the completion …
    assert 'class="badge badge-open">open</span>' in panel
    # … and the line under it does not answer for a different task's state.
    assert "no pending impact" not in slot(panel)
    assert "This graph has changed" in slot(panel)


def resolving_dataset(second: TaskStatusName = "open") -> FakeLithosDataset:
    """`root` blocks `one` and `two`, and only `one` is a blocked ROW.

    So `two` resolving moves nothing Lithos said about this graph: it is the
    smallest fixture in which a status — and the state of the edge into it —
    is the whole of what changed.
    """
    return dataset(
        [task("root"), task("one"), task("two", status=second)],
        (("root", "one", "blocks"), ("root", "two", "blocks")),
        blocked={"one": (blocker("root"),)},
    )


def test_a_blocked_row_that_moved_under_a_warm_cache_withholds_both_figures(
    lithos_lens_config_env: Path,
) -> None:
    """M is LITHOS's fact, read fresh on every panel with no cache under it, so
    it can move while the graph does not: an eventless edge upsert on a task
    outside this scope adds a second blocker to `one`, and the panel that used
    to say "1 immediately" would say "0" beside a canvas whose every node and
    edge is unchanged (round-1 correctness f-001). Both figures are withheld
    instead — the picture agreeing is not the same as the ANSWER agreeing."""
    fake = GraphFakeClient(impact_dataset())
    page_url = f"/tasks/graph?project={PROJECT}&focus=root"

    with client_for(lithos_lens_config_env, fake) as client:
        page = unescape(client.get(page_url).text)
        drawn = snapshot(page)
        fake.replace_dataset(moved_blocked_dataset())
        stale = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}&snapshot={drawn}"
        ).text
        reloaded = unescape(client.get(page_url).text)

    assert "frees 3 in this graph, 1 immediately" in slot(page)
    # The GRAPH is byte-identical across the change — same nodes, same edges,
    # warm from the cache — which is exactly why a fingerprint over the picture
    # alone could not catch this.
    assert payload(reloaded)["nodes"] == payload(page)["nodes"]
    assert payload(reloaded)["edges"] == payload(page)["edges"]
    # And the ANSWER is not: a reload — the page the operator is told to fetch
    # — states the new M.
    assert "frees 3 in this graph, 0 immediately" in slot(reloaded)
    assert "This graph has changed" in slot(stale)
    assert attribute(stale, "data-impact-frees") == ""
    assert attribute(stale, "data-impact-state") == "stale"


def test_a_dependent_resolving_within_the_same_membership_withholds_them_too(
    lithos_lens_config_env: Path,
) -> None:
    """The scope half of the same rule, with no membership change to lean on.

    `two` carries no blocked row — nothing Lithos says about this graph moves
    when it completes — so under `include_resolved=1` the ONLY differences are
    its own status and the state of the edge into it (D6, satisfied now). N
    drops from 2 to 1 on that alone, and the panel must not answer either
    number beside the picture of the other."""
    fake = GraphFakeClient(resolving_dataset())
    page_url = f"/tasks/graph?project={PROJECT}&focus=root&include_resolved=1"

    with client_for(lithos_lens_config_env, fake) as client:
        page = unescape(client.get(page_url).text)
        drawn = snapshot(page)
        fake.replace_dataset(resolving_dataset(second="completed"))
        stale = client.get(
            f"/tasks/root?fragment=panel&scope=project:{PROJECT}"
            f"&include_resolved=1&snapshot={drawn}"
        ).text
        reloaded = unescape(client.get(page_url).text)

    assert "frees 2 in this graph, 1 immediately" in slot(page)
    assert node_ids(reloaded) == node_ids(page), "the membership was meant to hold"
    assert "frees 1 in this graph, 1 immediately" in slot(reloaded)
    assert "This graph has changed" in slot(stale)
    assert attribute(stale, "data-impact-frees") == ""


# ── What the fingerprint is OVER (round-2 test-quality f-001) ───────────
#
# The route tests above prove the detector FIRES; these prove it is watching
# the right material. Every field below can move a figure, the lit set or the
# chain without moving any other — so a fingerprint that dropped one would stay
# green on all of them while the panel went back to counting over a graph the
# canvas is not showing. Pure, one mutation per case, over the two authorities
# D10 reads (the scope, and Lithos's blocked rows).

FINGERPRINT_NODES = (
    GraphNode(task("root")),
    GraphNode(task("one")),
    GraphNode(task("far", project="elsewhere"), ghost=True, ghost_kind="dependency"),
)
FINGERPRINT_EDGES = (
    GraphEdge(
        EdgeRecord(from_task_id="root", to_task_id="one", type="blocks"),
        state=EDGE_ACTIVE,
    ),
    GraphEdge(
        EdgeRecord(from_task_id="one", to_task_id="far", type="blocks"),
        state=EDGE_ACTIVE,
    ),
)
FINGERPRINT_ROWS = (
    BlockedTaskRecord(task=task("one"), blockers=(blocker("root"),)),
    BlockedTaskRecord(
        task=task("far", project="elsewhere"), blockers=(blocker("one"),)
    ),
)


def fingerprint_scope(**changes: object) -> TaskGraphScope:
    """The baseline graph both authorities are fingerprinted over."""
    return replace(
        TaskGraphScope(
            kind="project",
            key=PROJECT,
            nodes=FINGERPRINT_NODES,
            edges=FINGERPRINT_EDGES,
        ),
        **changes,  # type: ignore[arg-type]
    )


def fingerprint_signal(**changes: object) -> CycleSignal:
    """The baseline blocked signal: one complete read, two rows."""
    return replace(
        CycleSignal(
            coverage=(PROJECT, "elsewhere"),
            reads=(
                ProjectRead(project=PROJECT, by=READ_BY_PROJECT, rows=FINGERPRINT_ROWS),
            ),
            blocked=FINGERPRINT_ROWS,
            verdicts=FINGERPRINT_ROWS[:1],
        ),
        **changes,  # type: ignore[arg-type]
    )


def baseline_fingerprint() -> str:
    return impact_fingerprint(fingerprint_scope(), fingerprint_signal())


def with_node(index: int, **changes: object) -> TaskGraphScope:
    nodes = list(FINGERPRINT_NODES)
    nodes[index] = replace(nodes[index], **changes)  # type: ignore[arg-type]
    return fingerprint_scope(nodes=tuple(nodes))


def with_edge(index: int, **changes: object) -> TaskGraphScope:
    edges = list(FINGERPRINT_EDGES)
    edges[index] = replace(edges[index], **changes)  # type: ignore[arg-type]
    return fingerprint_scope(edges=tuple(edges))


def with_edge_record(index: int, **changes: object) -> TaskGraphScope:
    edges = list(FINGERPRINT_EDGES)
    edges[index] = replace(edges[index], edge=replace(edges[index].edge, **changes))  # type: ignore[arg-type]
    return fingerprint_scope(edges=tuple(edges))


#: (name, what one mutation produces). Each moves a figure, a class on the
#: canvas, or whether M may be stated at all — so each must move the digest.
MATERIAL_CHANGES: tuple[
    tuple[str, Callable[[], tuple[TaskGraphScope, CycleSignal]]], ...
] = (
    # N counts OPEN dependents, so a status is a figure.
    (
        "node status",
        lambda: (
            with_node(1, task=replace(task("one"), status="completed")),
            fingerprint_signal(),
        ),
    ),
    # An unreadable edge list is what makes N a lower bound (D10).
    (
        "node completeness",
        lambda: (
            with_node(1, completeness=COMPLETENESS_EDGES_UNKNOWN),
            fingerprint_signal(),
        ),
    ),
    # A context ghost is not on the default canvas; a dependency one is.
    (
        "ghost kind",
        lambda: (with_node(2, ghost_kind="context"), fingerprint_signal()),
    ),
    # The project slugs a coverage read is matched against (D4/§5B.1).
    (
        "node projects",
        lambda: (
            with_node(1, task=replace(task("one"), tags=("project:elsewhere",))),
            fingerprint_signal(),
        ),
    ),
    # …and WHICH convention carries a slug, because coverage is decided per
    # READ: `project=` is matched against the metadata slug alone and `tags=`
    # against the tag ones (`graph_cycles.read_covers`). The same slug moved
    # between the two conventions leaves the union — and everything else here
    # — untouched while the coverage answer behind M changes.
    (
        "project convention",
        lambda: (
            with_node(
                1,
                task=replace(task("one"), tags=(), metadata={"project": PROJECT}),
            ),
            fingerprint_signal(),
        ),
    ),
    (
        "edge source",
        lambda: (with_edge_record(1, from_task_id="root"), fingerprint_signal()),
    ),
    (
        "edge target",
        lambda: (with_edge_record(1, to_task_id="root"), fingerprint_signal()),
    ),
    (
        "edge type",
        lambda: (with_edge_record(1, type="waits_on_gate"), fingerprint_signal()),
    ),
    # Only the ACTIVE projection is walked (D6).
    ("edge state", lambda: (with_edge(1, state=EDGE_INACTIVE), fingerprint_signal())),
    # M itself: a second blocker means completing the focus frees nobody now.
    (
        "blocker set",
        lambda: (
            fingerprint_scope(),
            fingerprint_signal(
                blocked=(
                    replace(
                        FINGERPRINT_ROWS[0],
                        blockers=(blocker("root"), blocker("other")),
                    ),
                    FINGERPRINT_ROWS[1],
                )
            ),
        ),
    ),
    # A row that appears or disappears for a task this graph holds.
    (
        "blocked rows",
        lambda: (fingerprint_scope(), fingerprint_signal(blocked=FINGERPRINT_ROWS[:1])),
    ),
    # Whether M may be stated at all: the read plan, and how it ended.
    (
        "coverage set",
        lambda: (fingerprint_scope(), fingerprint_signal(coverage=(PROJECT,))),
    ),
    (
        "read truncated",
        lambda: (
            fingerprint_scope(),
            fingerprint_signal(
                reads=(replace(fingerprint_signal().reads[0], truncated=True),)
            ),
        ),
    ),
    (
        "read failed",
        lambda: (
            fingerprint_scope(),
            fingerprint_signal(
                reads=(replace(fingerprint_signal().reads[0], error="internal_error"),)
            ),
        ),
    ),
    (
        "read unmade",
        lambda: (
            fingerprint_scope(),
            fingerprint_signal(
                reads=(replace(fingerprint_signal().reads[0], unmade=True),)
            ),
        ),
    ),
    (
        "projectless",
        lambda: (fingerprint_scope(), fingerprint_signal(projectless=("one",))),
    ),
)


#: And WHICH half of the fingerprint each of them moves (round-4 test-quality
#: f-003). "Moves the digest" is not the whole contract: the two halves answer
#: different questions, and a canvas field filed under the answer half would
#: let a panel keep D7's position and D8's lower bound — computed from a
#: topology the canvas is NOT showing — through an edge relink that changed the
#: picture. CANVAS is everything the drawing and N rest on; ANSWER is M's
#: material, which moves no line on screen.
CANVAS_HALF = "canvas"
ANSWER_HALF = "answer"
MATERIAL_HALVES: dict[str, str] = {
    "node status": CANVAS_HALF,
    "node completeness": CANVAS_HALF,
    "ghost kind": CANVAS_HALF,
    "edge source": CANVAS_HALF,
    "edge target": CANVAS_HALF,
    "edge type": CANVAS_HALF,
    "edge state": CANVAS_HALF,
    # A project slug decides which READ could have covered a task (D4/§5B.1)
    # and nothing that is drawn.
    "node projects": ANSWER_HALF,
    "project convention": ANSWER_HALF,
    "blocker set": ANSWER_HALF,
    "blocked rows": ANSWER_HALF,
    "coverage set": ANSWER_HALF,
    "read truncated": ANSWER_HALF,
    "read failed": ANSWER_HALF,
    "read unmade": ANSWER_HALF,
    "projectless": ANSWER_HALF,
}


def test_every_material_field_is_filed_in_a_half() -> None:
    """The table above and the halves below name the same fields — a mutation
    with no stated half would be tested for "moves the digest" alone, which is
    the weaker half of the contract."""
    assert MATERIAL_HALVES.keys() == {name for name, _ in MATERIAL_CHANGES}


@pytest.mark.parametrize(
    ("name", "mutate"), MATERIAL_CHANGES, ids=[name for name, _ in MATERIAL_CHANGES]
)
def test_every_material_field_moves_the_fingerprint(
    name: str, mutate: Callable[[], tuple[TaskGraphScope, CycleSignal]]
) -> None:
    """One field at a time, each of which can change N, M, the lit set or
    whether M may be stated — and none of which changes the node MEMBERSHIP, so
    a fingerprint over ids alone would be green on every case here. Each is
    also asserted into its HALF: the digest moving says the figures are
    withheld, the half says whether the notes about the canvas survive with
    them."""
    scope, signal = mutate()
    moved = impact_fingerprint(scope, signal)

    assert moved != baseline_fingerprint(), f"{name} left the fingerprint unchanged"
    assert canvas_holds(moved, baseline_fingerprint()) == (
        MATERIAL_HALVES[name] == ANSWER_HALF
    ), f"{name} is filed in the wrong half"


#: The other half of the contract: text and arrival order move no figure, so
#: they must not withhold the line. A detector that fired on these would cost
#: the impact permanently rather than when it is actually wrong.
IMMATERIAL_CHANGES: tuple[
    tuple[str, Callable[[], tuple[TaskGraphScope, CycleSignal]]], ...
] = (
    (
        "task title",
        lambda: (
            with_node(1, task=replace(task("one"), title="Renamed")),
            fingerprint_signal(),
        ),
    ),
    (
        "blocker message",
        lambda: (
            fingerprint_scope(),
            fingerprint_signal(
                blocked=(
                    replace(
                        FINGERPRINT_ROWS[0],
                        blockers=(replace(blocker("root"), message="Reworded."),),
                    ),
                    FINGERPRINT_ROWS[1],
                )
            ),
        ),
    ),
    # The fold that builds these orders them by whichever of two concurrent
    # reads answered first (`graph_cycles._signal`), so arrival order must not
    # be a difference — it would withhold the line at random.
    (
        "row order",
        lambda: (
            fingerprint_scope(
                nodes=tuple(reversed(FINGERPRINT_NODES)),
                edges=tuple(reversed(FINGERPRINT_EDGES)),
            ),
            fingerprint_signal(blocked=tuple(reversed(FINGERPRINT_ROWS))),
        ),
    ),
    # A scoped read legitimately names tasks this graph never fetched, and no
    # figure here is counted over them.
    (
        "rows for tasks outside the graph",
        lambda: (
            fingerprint_scope(),
            fingerprint_signal(
                blocked=(
                    *FINGERPRINT_ROWS,
                    BlockedTaskRecord(task=task("stranger"), blockers=(blocker("x"),)),
                )
            ),
        ),
    ),
)


@pytest.mark.parametrize(
    ("name", "mutate"),
    IMMATERIAL_CHANGES,
    ids=[name for name, _ in IMMATERIAL_CHANGES],
)
def test_no_immaterial_change_withholds_the_line(
    name: str, mutate: Callable[[], tuple[TaskGraphScope, CycleSignal]]
) -> None:
    scope, signal = mutate()

    assert impact_fingerprint(scope, signal) == baseline_fingerprint(), (
        f"{name} moved the fingerprint"
    )


def test_a_fingerprint_separates_the_picture_from_the_answer_over_it() -> None:
    """Which HALF moved is the question a stale panel has to answer before it
    knows what to withhold (round-3 correctness f-006): the figures belong to
    the whole answer, the notes beside them only to the picture. A blocked row
    that moved leaves the canvas half standing; a node's status — which changes
    what is drawn, what N counts and where the chain runs — does not."""
    baseline = baseline_fingerprint()
    rows_moved = impact_fingerprint(
        fingerprint_scope(),
        fingerprint_signal(
            blocked=(
                replace(
                    FINGERPRINT_ROWS[0], blockers=(blocker("root"), blocker("other"))
                ),
                FINGERPRINT_ROWS[1],
            )
        ),
    )
    picture_moved = impact_fingerprint(
        with_node(1, task=replace(task("one"), status="completed")),
        fingerprint_signal(),
    )

    # Both are changes — the whole fingerprint moves either way, which is what
    # withholds the figures …
    assert rows_moved != baseline
    assert picture_moved != baseline
    # … and only one of them is a change to what the operator is looking at.
    assert canvas_holds(rows_moved, baseline)
    assert not canvas_holds(picture_moved, baseline)
    assert canvas_holds(baseline, baseline)
    # A value Lens never emitted holds nothing: an invented or hand-truncated
    # snapshot withholds everything rather than half-answering from a guess.
    assert not canvas_holds(baseline, "garbage")
    assert not canvas_holds(baseline, baseline.split(".")[0])
    assert not canvas_holds(baseline, "")


def separator_scope(edge: tuple[str, str]) -> TaskGraphScope:
    """Four tasks whose ids CONTAIN the characters a joined digest would use.

    A task id is an arbitrary non-empty string (§5.1) and nothing normalises
    control characters out of one, so these are ids Lens can really be handed.
    """
    return TaskGraphScope(
        kind="project",
        key=PROJECT,
        nodes=tuple(
            GraphNode(task(task_id)) for task_id in ("a", "b\x1fc", "a\x1fb", "c")
        ),
        edges=(
            GraphEdge(
                EdgeRecord(from_task_id=edge[0], to_task_id=edge[1], type="blocks"),
                state=EDGE_ACTIVE,
            ),
        ),
    )


def test_two_graphs_that_differ_only_across_a_separator_are_not_the_same() -> None:
    """`a -> b\x1fc` and `a\x1fb -> c` are different graphs — focusing `a`
    frees one task in the first and nobody in the second — over the same four
    nodes. A digest that JOINED its fields on a separator would serialise the
    two edges identically and let a moved graph pass the equality check with
    the wrong count behind it (round-2 correctness f-003)."""
    blocks = separator_scope(("a", "b\x1fc"))
    strands = separator_scope(("a\x1fb", "c"))
    signal = CycleSignal()

    assert impact_fingerprint(blocks, signal) != impact_fingerprint(strands, signal)
    # …and the two really do answer differently, which is what makes a shared
    # digest a wrong count rather than a harmless one.
    blocking = downstream_impact(blocks, signal, focus="a")
    stranded = downstream_impact(strands, signal, focus="a")
    assert blocking is not None and blocking.frees == 1
    assert stranded is not None and stranded.frees == 0


def convention_scope(*, tagged: bool) -> TaskGraphScope:
    """`root` blocks one open dependent in `loom`, under ONE convention.

    The two graphs differ in nothing a reader of the canvas could see: same
    ids, statuses, edges, and the same project slug on the same node — only
    the place the slug is written moves (§5B.1's two conventions).
    """
    dependent = (
        task("dep")
        if tagged
        else replace(task("dep"), tags=(), metadata={"project": PROJECT})
    )
    return TaskGraphScope(
        kind="project",
        key=PROJECT,
        nodes=(GraphNode(task("root")), GraphNode(dependent)),
        edges=(
            GraphEdge(
                EdgeRecord(from_task_id="root", to_task_id="dep", type="blocks"),
                state=EDGE_ACTIVE,
            ),
        ),
    )


def test_a_slug_that_moves_between_conventions_is_a_different_answer() -> None:
    """Coverage belongs to the READ, not to the project (`read_covers`): a
    complete `project=loom` response establishes the absence of a task whose
    slug is in `metadata.project` and nothing about one carrying the tag, and
    the truncated `tags=` half establishes nothing either way. So the same
    dependent, the same slug, written the other way round is the difference
    between stating M and withholding it — and a fingerprint over the UNION of
    the two conventions would call the two graphs the same and print the stale
    answer beside the picture (external f-001)."""
    signal = CycleSignal(
        coverage=(PROJECT,),
        reads=(
            ProjectRead(project=PROJECT, by=READ_BY_PROJECT),
            ProjectRead(project=PROJECT, by=READ_BY_TAG, truncated=True),
        ),
    )
    metadata_side = convention_scope(tagged=False)
    tagged_side = convention_scope(tagged=True)

    covered = downstream_impact(metadata_side, signal, focus="root")
    withheld = downstream_impact(tagged_side, signal, focus="root")

    # The complete `project=` read answers for the metadata-stamped dependent…
    assert covered is not None and covered.frees == 1
    assert covered.immediately == 0 and covered.covered == 1
    # …and for the tagged one only the truncated `tags=` read could have.
    assert withheld is not None and withheld.frees == 1
    assert withheld.immediately is None and withheld.covered == 0
    assert impact_fingerprint(metadata_side, signal) != impact_fingerprint(
        tagged_side, signal
    )


def test_the_fingerprint_ignores_the_order_two_reads_merged_in() -> None:
    """A row's blockers are merged across the pair of reads D4 issues per
    project, in whichever order they answered (`graph_cycles._signal`). The
    same two blockers arriving the other way round are the same fact, and a
    digest that disagreed would withhold the impact line at random."""
    both = (blocker("root"), blocker("other"))
    forward = fingerprint_signal(
        blocked=(replace(FINGERPRINT_ROWS[0], blockers=both), FINGERPRINT_ROWS[1])
    )
    backward = fingerprint_signal(
        blocked=(
            FINGERPRINT_ROWS[1],
            replace(FINGERPRINT_ROWS[0], blockers=tuple(reversed(both))),
        )
    )

    assert impact_fingerprint(fingerprint_scope(), forward) == impact_fingerprint(
        fingerprint_scope(), backward
    )
    # …and it is not simply blind to the blockers: one MORE of them is M's own
    # difference between "freed by this" and "still waiting on something else".
    assert impact_fingerprint(fingerprint_scope(), forward) != baseline_fingerprint()


def test_an_impact_is_kept_only_while_the_panel_agrees_about_the_focus() -> None:
    """The join itself, at every answer it has to give. Agreement passes the
    line through untouched; a badge that has RESOLVED states D10's own wording
    for that state; and everything else — figures counted for a resolved task
    under an open badge, a status Lens cannot name, a task the panel could not
    read at all — has no fact to state and degrades to the neutral line."""
    open_impact = DownstreamImpact(
        focus="root",
        state="open",
        frees=3,
        immediately=1,
        relations_exact=False,
        chain_position=1,
        chain_length=3,
    )
    done = DownstreamImpact(focus="root", state="completed")

    # Agreement: the same object, not a rebuilt one.
    assert reconciled_impact(open_impact, task("root")) is open_impact
    assert reconciled_impact(done, task("root", status="completed")) is done
    assert reconciled_impact(None, task("root")) is None

    # No impact at all — the rebuilt scope lost the focus, or the assembly
    # failed — and a SCOPED panel still owes D10's words for a resolved task …
    for status in ("completed", "cancelled"):
        lost = reconciled_impact(None, task("root", status=status), scoped=True)
        assert lost is not None
        assert (lost.state, lost.focus, lost.frees) == (status, "root", 0)
    # … while nothing that asked for no line, or that D10 gives none, gains one.
    assert reconciled_impact(None, task("root", status="completed")) is None
    assert reconciled_impact(None, task("root"), scoped=True) is None
    assert reconciled_impact(None, None, scoped=True) is None
    assert (
        reconciled_impact(
            None, task("epic", task_type="epic", status="completed"), scoped=True
        )
        is None
    )

    # A resolved badge is answered in D10's words for it …
    for status in ("completed", "cancelled"):
        resolved = reconciled_impact(open_impact, task("root", status=status))
        assert resolved is not None
        assert resolved.state == status
        assert resolved.frees == 0
        # … and the parts of the line that belong to the GRAPH survive, which
        # is exactly what `downstream_impact` builds for a resolved node.
        assert resolved.relations_exact is False
        assert (resolved.chain_position, resolved.chain_length) == (1, 3)

    # … and every other disagreement has nothing to state.
    for mismatch, record in (
        (done, task("root")),
        (open_impact, replace(task("root"), status="archived")),  # type: ignore[arg-type]
    ):
        answer = reconciled_impact(mismatch, record)
        assert answer is not None
        assert answer.state == "stale"
        assert answer.frees == 0
        # The FIGURES are what the badge disagreed with. The notes beside them
        # describe the canvas this render drew, which is the picture on screen
        # either way, so they carry over here exactly as into the resolved
        # wording above (round-3 correctness f-006).
        assert answer.relations_exact is mismatch.relations_exact
        assert (answer.chain_position, answer.chain_length) == (
            mismatch.chain_position,
            mismatch.chain_length,
        )

    # A panel that could not read its focal task at all is NOT that: there is
    # no badge to disagree with and no refresh that resolves a failed read, so
    # it keeps the notes with no figures and no sentence, and its own markup
    # carries the failure (round-4 correctness f-007).
    unread = reconciled_impact(open_impact, None, scoped=True)
    assert unread is not None
    assert (unread.state, unread.frees) == ("none", 0)
    assert unread.relations_exact is False
    assert (unread.chain_position, unread.chain_length) == (1, 3)

    # An EPIC's line is the exception to all of it: it makes no claim about the
    # focal status, so no badge can disagree with it and nothing here may turn
    # it into "refresh to see what completing this frees" — the one sentence
    # D10 forbids an epic, moved graph or not (round-2 correctness f-004).
    for moved in (False, True):
        epic_line = DownstreamImpact(
            focus="epic", state="none", stale=moved, chain_position=1, chain_length=1
        )
        for record in (
            task("epic", task_type="epic"),
            task("epic", task_type="epic", status="completed"),
            None,
        ):
            assert reconciled_impact(epic_line, record, scoped=True) is epic_line
