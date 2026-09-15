"""T2 slice A5 — the detail page's mini-graph fragment.

The acceptance surface is the same one A3's is (D3): what the SERVER decided,
read off the rendered fragment and its embedded payload. The canvas itself is
`graph.js`'s, verified by the JS harness in ``test_tasks_js.py`` and by the
e2e capture; what is asserted here is the membership rule (two hops up, one
down, the parent epic), the cap's deterministic fill order, the tail that
counts what the cap left out, and the focus link into the full graph.

The client fixture is borrowed from ``test_graph_page`` rather than rebuilt:
this fragment reads the same cache through the same fan-out, and a second fake
would let the two drift on exactly the reads both are about.
"""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from typing import Any

import pytest

from lithos_lens.config import DEFAULT_GRAPH_MINI_GRAPH_MAX_NODES
from lithos_lens.graph_cache import GraphCache
from lithos_lens.graph_mini import (
    DEFAULT_GRAPH_MINI_GRAPH_MAX_NODES as MODULE_DEFAULT_MAX_NODES,
)
from lithos_lens.graph_mini import (
    MiniGraphLimits,
    load_mini_graph,
)
from lithos_lens.tasks import TaskRecord, TaskStatusName
from tests.test_graph_page import GraphFakeClient, client_for, dataset

pytestmark = pytest.mark.anyio

PROJECT = "loom"
CAP = DEFAULT_GRAPH_MINI_GRAPH_MAX_NODES


def made(
    task_id: str,
    *,
    created_at: str,
    status: TaskStatusName = "open",
    task_type: str = "task",
    project: str | None = PROJECT,
) -> TaskRecord:
    """A fixture task with an explicit ``created_at`` — the tier tie-break."""
    return TaskRecord(
        id=task_id,
        title=task_id.replace("-", " ").title(),
        status=status,
        task_type=task_type,
        created_by="planner",
        created_at=created_at,
        tags=(f"project:{project}",) if project else (),
        resolved_at="2026-09-02T00:00:00+00:00" if status != "open" else "",
    )


def fragment(config_path: Path, fake: GraphFakeClient, task_id: str) -> str:
    """Render `/tasks/<id>/minigraph` and hand back its HTML, entities decoded."""
    with client_for(config_path, fake) as client:
        response = client.get(f"/tasks/{task_id}/minigraph")
    assert response.status_code == 200, response.text
    return unescape(response.text)


def payload(html: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" data-graph-payload>(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "no embedded payload"
    return json.loads(match.group(1))


def node_ids(html: str) -> set[str]:
    return {node["id"] for node in payload(html)["nodes"]}


def edge_pairs(html: str) -> set[tuple[str, str, str]]:
    return {(edge["from"], edge["to"], edge["type"]) for edge in payload(html)["edges"]}


def tail_of(html: str) -> tuple[int, str]:
    """The shared tail's remaining count and its sentence."""
    match = re.search(
        r'data-link-tail="minigraph" data-link-remaining="(\d+)">(.*?)</p>',
        html,
        re.DOTALL,
    )
    assert match, "no mini-graph tail"
    return int(match.group(1)), re.sub(
        r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(2))
    ).strip()


# ── Membership: two hops up, one down, the parent epic ──────────────────


def test_a_blocked_task_draws_two_hops_up_one_down_and_its_parent(
    lithos_lens_config_env: Path,
) -> None:
    """The slice's first acceptance criterion, verbatim.

    ``task`` is blocked by B, B is blocked by C, D depends on ``task``, and an
    epic parents it: the mini-graph is exactly those five nodes. C is depth 2
    upstream (reached through B's own edge list), D is depth 1 downstream, and
    E — a dependent of D — is depth 2 DOWNSTREAM, which the rule stops before.
    """
    fake = GraphFakeClient(
        dataset(
            [
                made("epic", created_at="2026-09-01T00:00:00+00:00", task_type="epic"),
                made("task", created_at="2026-09-01T00:00:04+00:00"),
                made("b", created_at="2026-09-01T00:00:02+00:00"),
                made("c", created_at="2026-09-01T00:00:01+00:00"),
                made("d", created_at="2026-09-01T00:00:05+00:00"),
                made("e", created_at="2026-09-01T00:00:06+00:00"),
            ],
            [
                ("c", "b", "blocks"),
                ("b", "task", "blocks"),
                ("task", "d", "blocks"),
                ("d", "e", "blocks"),
                ("epic", "task", "parent_child"),
            ],
        )
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    assert node_ids(html) == {"c", "b", "task", "d", "epic"}
    assert edge_pairs(html) == {
        ("c", "b", "blocks"),
        ("b", "task", "blocks"),
        ("task", "d", "blocks"),
        ("epic", "task", "parent_child"),
    }


def test_provenance_is_never_drawn_in_a_mini_graph(
    lithos_lens_config_env: Path,
) -> None:
    """D11 excludes ``discovered_from``, in both directions.

    The detail page renders provenance as its own text section; repeating it
    on a picture whose subject is "what blocks this" would put a non-blocking
    relation in a graph read as blocking.
    """
    fake = GraphFakeClient(
        dataset(
            [
                made("task", created_at="2026-09-01T00:00:02+00:00"),
                made("source", created_at="2026-09-01T00:00:01+00:00"),
                made("spawn", created_at="2026-09-01T00:00:03+00:00"),
            ],
            [
                ("source", "task", "discovered_from"),
                ("task", "spawn", "discovered_from"),
            ],
        )
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    assert node_ids(html) == {"task"}
    assert edge_pairs(html) == set()


def test_a_waits_on_gate_blocker_is_drawn_like_any_other(
    lithos_lens_config_env: Path,
) -> None:
    """Both blocker edge types are in scope upstream AND downstream (D11)."""
    fake = GraphFakeClient(
        dataset(
            [
                made("gate", created_at="2026-09-01T00:00:01+00:00", task_type="gate"),
                made("task", created_at="2026-09-01T00:00:02+00:00"),
            ],
            [("gate", "task", "waits_on_gate")],
        )
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    assert node_ids(html) == {"gate", "task"}
    assert edge_pairs(html) == {("gate", "task", "waits_on_gate")}
    assert [
        node["type"] for node in payload(html)["nodes"] if node["id"] == "gate"
    ] == ["gate"]


def test_a_completed_blocker_is_drawn_on_an_inactive_edge(
    lithos_lens_config_env: Path,
) -> None:
    """The picture agrees with the chain below it, which keeps a satisfied one.

    The detail page's blocker section shows a completed predecessor under
    "Dependencies" rather than dropping it, so the mini-graph draws the same
    edge — carrying the state that says it no longer blocks (D6).
    """
    fake = GraphFakeClient(
        dataset(
            [
                made(
                    "done", created_at="2026-09-01T00:00:01+00:00", status="completed"
                ),
                made("task", created_at="2026-09-01T00:00:02+00:00"),
            ],
            [("done", "task", "blocks")],
        )
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    edges = payload(html)["edges"]
    assert [(edge["state"], edge["reason"]) for edge in edges] == [
        ("inactive", "satisfied")
    ]


# ── The cap, its fill order, and the tail ───────────────────────────────


def runaway(dependents: int, *, blockers: int = 0, parent: bool = False) -> tuple:
    """One focal task with ``dependents`` dependents and ``blockers`` blockers."""
    tasks = [made("task", created_at="2026-09-01T00:00:00+00:00")]
    edges: list[tuple[str, str, str]] = []
    if parent:
        tasks.append(
            made("epic", created_at="2026-09-01T00:00:00+00:00", task_type="epic")
        )
        edges.append(("epic", "task", "parent_child"))
    for index in range(blockers):
        tasks.append(
            made(f"b{index:02d}", created_at=f"2026-09-02T00:{index:02d}:00+00:00")
        )
        edges.append((f"b{index:02d}", "task", "blocks"))
    for index in range(dependents):
        tasks.append(
            made(f"d{index:02d}", created_at=f"2026-09-03T00:{index:02d}:00+00:00")
        )
        edges.append(("task", f"d{index:02d}", "blocks"))
    return tuple(tasks), tuple(edges)


def test_sixty_dependents_render_the_focal_task_plus_thirty_nine_and_a_tail(
    lithos_lens_config_env: Path,
) -> None:
    """The cap counts the FOCAL TASK (D11), so 40 nodes is 1 + 39 dependents.

    The 21 it left out are counted, not dropped: a "what does finishing this
    free" picture that silently showed two thirds of the answer would be worse
    than one that shows less and says so.
    """
    tasks, edges = runaway(60)
    html = fragment(
        lithos_lens_config_env, GraphFakeClient(dataset(tasks, edges)), "task"
    )

    drawn = node_ids(html)
    assert len(drawn) == CAP
    assert "task" in drawn
    assert len(drawn - {"task"}) == CAP - 1
    remaining, sentence = tail_of(html)
    assert remaining == 21
    assert "21 more related tasks not shown." in sentence
    # The copy is about the NEIGHBOURS — 60 of them, 39 drawn beside the focal
    # task — and the size it names is the room this cap leaves them, not the
    # detail page's 25-row neighbour page.
    assert "This task has 60 related tasks in all" in sentence
    assert f"the first {CAP - 1} are listed above" in sentence


def test_the_drawn_dependents_are_the_oldest_ones_in_the_tier(
    lithos_lens_config_env: Path,
) -> None:
    """(``created_at``, ``id``) within a tier — so the 39 shown are stable.

    Asserted against a deliberately SHUFFLED edge order: without the rule the
    picture would be whichever dependents Lithos happened to list first, and
    two renders of an unchanged task could disagree about which.
    """
    tasks, edges = runaway(60)
    shuffled = tuple(sorted(edges, key=lambda edge: edge[1], reverse=True))
    html = fragment(
        lithos_lens_config_env, GraphFakeClient(dataset(tasks, shuffled)), "task"
    )

    assert node_ids(html) == {"task"} | {f"d{index:02d}" for index in range(CAP - 1)}


def test_with_a_parent_and_two_blockers_the_cap_binds_on_dependents_first(
    lithos_lens_config_env: Path,
) -> None:
    """The fill order is the point: parent and blockers survive, dependents are cut.

    Both blockers and the epic are drawn — they are tiers 2 and 3 — and the
    remaining room goes to dependents, so what the tail counts is the
    dependents the cap could not fit.
    """
    tasks, edges = runaway(60, blockers=2, parent=True)
    html = fragment(
        lithos_lens_config_env, GraphFakeClient(dataset(tasks, edges)), "task"
    )

    drawn = node_ids(html)
    assert len(drawn) == CAP
    assert {"task", "epic", "b00", "b01"} <= drawn
    dependents = {node for node in drawn if node.startswith("d")}
    assert len(dependents) == CAP - 4
    # 60 dependents named, 36 drawn: the other 24 are what the tail counts.
    assert tail_of(html)[0] == 24


async def test_the_cap_is_the_configured_knob_not_a_constant() -> None:
    """`[graph].mini_graph_max_nodes` is what bounds the picture."""
    tasks, edges = runaway(12)
    fake = GraphFakeClient(dataset(tasks, edges))
    view = await load_mini_graph(
        fake, "task", master=[], cache=GraphCache(), limits=MiniGraphLimits(max_nodes=5)
    )

    assert len(view.nodes) == 5
    assert view.capped
    assert view.tail.remaining == 8
    # Four of the twelve neighbours fit beside the focal task under a cap of 5.
    assert view.tail.shown == 4
    assert view.tail.total == 12
    assert view.tail.page_size == 4


def test_the_module_default_matches_the_shipped_config_default() -> None:
    """The mirrored default and the config's are one figure, as `graph_scope`'s are."""
    assert MODULE_DEFAULT_MAX_NODES == DEFAULT_GRAPH_MINI_GRAPH_MAX_NODES


def test_an_uncapped_neighbourhood_renders_no_tail(
    lithos_lens_config_env: Path,
) -> None:
    tasks, edges = runaway(3)
    html = fragment(
        lithos_lens_config_env, GraphFakeClient(dataset(tasks, edges)), "task"
    )

    assert 'data-link-tail="minigraph"' not in html


def test_depth_two_blockers_fill_last_and_are_counted_when_they_do_not_fit(
    lithos_lens_config_env: Path,
) -> None:
    """The last tier: drawn when there is room, counted in the tail when not."""
    tasks, edges = runaway(CAP - 2, blockers=1)
    tasks = (*tasks, made("deep", created_at="2026-09-01T00:00:01+00:00"))
    edges = (*edges, ("deep", "b00", "blocks"))
    html = fragment(
        lithos_lens_config_env, GraphFakeClient(dataset(tasks, edges)), "task"
    )

    drawn = node_ids(html)
    assert len(drawn) == CAP
    # focal + 1 blocker + 38 dependents fills the cap exactly, so the depth-2
    # blocker is the one node left out — and the tail says so rather than
    # leaving the picture to imply `b00` has none.
    assert "deep" not in drawn
    assert tail_of(html)[0] == 1


# ── The focus link, and the fragment's own chrome ───────────────────────


def test_the_focus_link_carries_the_project_slug_and_the_task_id(
    lithos_lens_config_env: Path,
) -> None:
    fake = GraphFakeClient(
        dataset([made("task", created_at="2026-09-01T00:00:00+00:00")], [])
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    href = re.search(r'data-mini-graph-focus href="([^"]+)"', html)
    assert href, "no focus link"
    assert href.group(1).startswith("/tasks/graph?")
    assert f"project={PROJECT}" in href.group(1)
    assert "focus=task" in href.group(1)


def test_a_projectless_task_gets_no_focus_link_and_says_why(
    lithos_lens_config_env: Path,
) -> None:
    """A graph renders one scope, and a task in no project names none."""
    fake = GraphFakeClient(
        dataset(
            [made("task", created_at="2026-09-01T00:00:00+00:00", project=None)], []
        )
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    assert "data-mini-graph-focus" not in html
    assert "data-mini-graph-unscoped" in html


def test_the_fragment_carries_the_legend_for_the_edges_it_drew(
    lithos_lens_config_env: Path,
) -> None:
    """Arrow direction is the one thing a graph must not be readable two ways."""
    fake = GraphFakeClient(
        dataset(
            [
                made("b", created_at="2026-09-01T00:00:01+00:00"),
                made("task", created_at="2026-09-01T00:00:02+00:00"),
            ],
            [("b", "task", "blocks")],
        )
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    assert 'data-legend-edge="blocks"' in html
    assert "A → B means A blocks B" in html
    assert 'data-legend-edge="parent_child"' not in html


def test_the_payload_turns_the_hierarchy_overlay_on(
    lithos_lens_config_env: Path,
) -> None:
    """The parent epic hangs on a `parent_child` edge the graph page hides.

    It is a member of THIS scope by decision (D11), so the overlay that draws
    its edge is on by decision too — otherwise the node the PRD asks for would
    be drawn with nothing connecting it to the task it parents.
    """
    fake = GraphFakeClient(
        dataset(
            [
                made("epic", created_at="2026-09-01T00:00:00+00:00", task_type="epic"),
                made("task", created_at="2026-09-01T00:00:01+00:00"),
            ],
            [("epic", "task", "parent_child")],
        )
    )
    scope = payload(fragment(lithos_lens_config_env, fake, "task"))["scope"]

    assert scope["overlays"] == ["hierarchy"]
    assert scope["focus"] == "task"
    assert scope["isolated"] is True


# ── Degradation ─────────────────────────────────────────────────────────


def test_a_neighbour_whose_status_could_not_be_read_is_drawn_as_unknown(
    lithos_lens_config_env: Path,
) -> None:
    """Never dropped: hiding a possibly-live blocker is the wrong way to err.

    The blocker is off the open snapshot (it is resolved), so it needs a
    ``task_get`` of its own — which is the read that fails here. An OPEN
    neighbour is on the master list and cannot reach this state at all, which
    is the point of consulting the list first.
    """
    fake = GraphFakeClient(
        dataset(
            [
                made("b", created_at="2026-09-01T00:00:01+00:00", status="completed"),
                made("task", created_at="2026-09-01T00:00:02+00:00"),
            ],
            [("b", "task", "blocks")],
        ),
        get_failures={"b"},
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    blocker = next(node for node in payload(html)["nodes"] if node["id"] == "b")
    assert blocker["status"] == "unknown"
    assert blocker["completeness"] == "status_unknown"
    assert [edge["state"] for edge in payload(html)["edges"]] == ["unknown"]


def test_a_blocker_whose_edges_could_not_be_read_says_so_rather_than_looking_leaf(
    lithos_lens_config_env: Path,
) -> None:
    """A failed `edge_list` is "Lens does not know", not "nothing deeper"."""
    fake = GraphFakeClient(
        dataset(
            [
                made("b", created_at="2026-09-01T00:00:01+00:00"),
                made("task", created_at="2026-09-01T00:00:02+00:00"),
            ],
            [("b", "task", "blocks")],
        ),
        edge_failures={"b"},
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    blocker = next(node for node in payload(html)["nodes"] if node["id"] == "b")
    assert blocker["completeness"] == "edges_unknown"
    assert "data-mini-graph-incomplete" in html


def test_a_focal_read_that_fails_answers_with_the_fragment_s_own_error(
    lithos_lens_config_env: Path,
) -> None:
    """The chain below is unaffected, and the fragment says which half failed."""
    fake = GraphFakeClient(
        dataset([made("task", created_at="2026-09-01T00:00:00+00:00")], []),
        get_failures={"task"},
        list_failures={"open"},
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    assert "data-mini-graph-error" in html
    assert "data-graph-payload" not in html


def test_an_offline_lithos_renders_the_fragment_without_a_canvas(
    lithos_lens_config_env: Path,
) -> None:
    fake = GraphFakeClient(
        dataset([made("task", created_at="2026-09-01T00:00:00+00:00")], []),
        health="unreachable",
    )
    html = fragment(lithos_lens_config_env, fake, "task")

    assert "offline" in html
    assert "data-graph-payload" not in html


# ── The reads behind it ─────────────────────────────────────────────────


def test_an_open_neighbour_on_the_master_list_costs_no_task_get(
    lithos_lens_config_env: Path,
) -> None:
    """The same rule D5 applies to a ghost, for the same reason.

    The focal task's own read is the only ``task_get`` a fully-open
    neighbourhood needs — and even that one is served from the snapshot.
    """
    fake = GraphFakeClient(
        dataset(
            [
                made("b", created_at="2026-09-01T00:00:01+00:00"),
                made("task", created_at="2026-09-01T00:00:02+00:00"),
                made("d", created_at="2026-09-01T00:00:03+00:00"),
            ],
            [("b", "task", "blocks"), ("task", "d", "blocks")],
        )
    )
    fragment(lithos_lens_config_env, fake, "task")

    assert fake.get_calls == []
    # One edge read for the focal task and one for its drawn blocker — the
    # depth-2 tier, and nothing per dependent (they are leaves).
    assert sorted(fake.edge_calls) == ["b", "task"]


def test_the_mini_graph_reuses_the_edge_cache_the_graph_page_warmed(
    lithos_lens_config_env: Path,
) -> None:
    """D2's whole point: one cache per TASK, shared by every scope over it."""
    fake = GraphFakeClient(
        dataset(
            [
                made("b", created_at="2026-09-01T00:00:01+00:00"),
                made("task", created_at="2026-09-01T00:00:02+00:00"),
            ],
            [("b", "task", "blocks")],
        )
    )
    with client_for(lithos_lens_config_env, fake) as client:
        assert client.get(f"/tasks/graph?project={PROJECT}").status_code == 200
        warm = len(fake.edge_calls)
        assert client.get("/tasks/task/minigraph").status_code == 200

    assert len(fake.edge_calls) == warm, "the mini-graph re-read a warm entry"


def test_the_detail_page_hosts_the_fragment_above_its_blocker_chain(
    lithos_lens_config_env: Path,
) -> None:
    """D11's placement, and A6's text baseline underneath it.

    The picture is fetched rather than inlined, so what the page carries is the
    host and its URL; the chain and the "Blocks:" line below it are the
    accessible baseline that renders whether or not it ever arrives.
    """
    fake = GraphFakeClient(
        dataset(
            [
                made("b", created_at="2026-09-01T00:00:01+00:00"),
                made("task", created_at="2026-09-01T00:00:02+00:00"),
                made("d", created_at="2026-09-01T00:00:03+00:00"),
            ],
            [("b", "task", "blocks"), ("task", "d", "blocks")],
        )
    )
    with client_for(lithos_lens_config_env, fake) as client:
        html = unescape(client.get("/tasks/task").text)

    assert 'hx-get="/tasks/task/minigraph"' in html
    assert html.index("data-mini-graph-host") < html.index("data-blocker-chain")
    # The text baseline the mini-graph renders no layers of its own for: the
    # chain above, and the level-1 dependents under "Blocks:".
    blocks = html[html.index("data-dependents") :]
    assert "<h2>Blocks:</h2>" in blocks
    assert "Title D" in blocks or "D</a>" in blocks
