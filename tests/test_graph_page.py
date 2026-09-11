"""T2 slice A3 — the `/tasks/graph` text baseline.

The text IS the acceptance surface (D3): loom's review gate is headless, so a
slice whose only output is a canvas has nothing a panel can assert. Every test
below therefore reads the rendered HTML — one `<ol>` per layer, the callout,
the chain line, the markers — or the call log behind it, because the scoped
blocked reads (D4) are a behaviour, not an implementation detail.

The fixtures are deliberately small and named after the rule they exercise; the
demo dataset (`fake_graph_dataset`) carries the full board and the e2e suite
renders that.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from html import unescape
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lithos_lens.config import load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_graph_dataset import edge_index
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.graph_page import graph_url, parse_graph_params
from lithos_lens.lithos_client import (
    LithosClientProtocol,
    LithosHealth,
    LithosToolError,
)
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import ClaimRecord, TaskRecord, TaskStatusName
from lithos_lens.web import create_app
from tests.conftest import metric_value

PROJECT = "loom"


def task(
    task_id: str,
    *,
    status: TaskStatusName = "open",
    project: str | None = PROJECT,
    title: str = "",
    created_at: str = "",
    task_type: str = "task",
    extra_tags: Sequence[str] = (),
) -> TaskRecord:
    """One fixture task, in ``project`` under the tag convention."""
    return TaskRecord(
        id=task_id,
        title=title or task_id.replace("-", " ").title(),
        status=status,
        task_type=task_type,
        created_by="planner",
        created_at=created_at or f"2026-09-01T00:00:{len(task_id):02d}+00:00",
        tags=((f"project:{project}",) if project else ()) + tuple(extra_tags),
        resolved_at="2026-09-02T00:00:00+00:00" if status != "open" else "",
    )


def cycle_blocker(other: str, message: str) -> tuple[BlockerRecord, ...]:
    return (
        BlockerRecord(
            kind="cycle", task_id=other, type="blocks", status="open", message=message
        ),
    )


def dataset(
    tasks: Sequence[TaskRecord],
    edges: Sequence[tuple[str, str, str]] = (),
    *,
    children: dict[str, tuple[str, ...]] | None = None,
    blocked: dict[str, tuple[BlockerRecord, ...]] | None = None,
) -> FakeLithosDataset:
    return FakeLithosDataset(
        tasks=tuple(tasks),
        edges=edge_index(tuple(edges)),
        children=children or {},
        blocked=blocked or {},
    )


class GraphFakeClient:
    """The demo client with a call log and injectable per-read failures.

    Wraps rather than subclasses ``FakeLithosClient`` so the delegation is
    explicit: the log is what several acceptance criteria are stated in ("a
    blocked read for that project too", "no read attempted"), and a silent
    passthrough would make those assertions untestable.
    """

    def __init__(
        self,
        data: FakeLithosDataset,
        *,
        edge_failures: set[str] | None = None,
        get_failures: set[str] | None = None,
        blocked_failures: set[str] | None = None,
        blocked_rows: dict[str, list[BlockedTaskRecord]] | None = None,
        health: LithosHealth = "ok",
    ) -> None:
        self._client = FakeLithosClient(dataset=data)
        self._edge_failures = edge_failures or set()
        self._get_failures = get_failures or set()
        self._blocked_failures = blocked_failures or set()
        self._blocked_rows = blocked_rows or {}
        self._health: LithosHealth = health
        self.blocked_calls: list[dict[str, Any]] = []
        self.edge_calls: list[str] = []
        self.get_calls: list[str] = []
        self.list_calls: list[dict[str, Any]] = []

    # ── lifecycle ──────────────────────────────────────────────────────

    async def startup(self) -> None:
        return None

    async def health(self) -> LithosHealth:
        return self._health

    async def register_agent(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    # ── reads ──────────────────────────────────────────────────────────

    async def list_tasks(self, **kwargs: Any) -> list[TaskRecord]:
        self.list_calls.append(kwargs)
        return await self._client.list_tasks(**kwargs)

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]:
        self.blocked_calls.append({"limit": limit, "project": project, "tags": tags})
        key = project or (tags[0] if tags else "")
        if project in self._blocked_failures or key in self._blocked_failures:
            raise LithosToolError("blocked read failed", code="internal_error")
        if key in self._blocked_rows:
            return list(self._blocked_rows[key])
        return await self._client.task_blocked(limit=limit, project=project, tags=tags)

    async def task_get(self, task_id: str) -> TaskRecord:
        self.get_calls.append(task_id)
        if task_id in self._get_failures:
            raise LithosToolError("task_get failed", code="internal_error")
        return await self._client.task_get(task_id)

    async def task_children(
        self, task_id: str, *, recursive: bool = False, include_closed: bool = False
    ) -> list[TaskRecord]:
        return await self._client.task_children(
            task_id, recursive=recursive, include_closed=include_closed
        )

    async def task_edge_list(
        self, task_id: str, *, direction: str = "both", types: list[str] | None = None
    ) -> list[EdgeRecord]:
        self.edge_calls.append(task_id)
        if task_id in self._edge_failures:
            raise LithosToolError("edge_list failed", code="internal_error")
        return await self._client.task_edge_list(
            task_id, direction=direction, types=types
        )

    def __getattr__(self, name: str) -> Any:
        # Everything the app touches that this page does not: health probes,
        # agent lists, findings. Delegated rather than reimplemented.
        return getattr(self._client, name)


def client_for(config_path: Path, fake: GraphFakeClient) -> TestClient:
    config = load_config(config_path)
    # Cast, not a hand-written passthrough for every unused method: the rest of
    # the protocol is served by ``__getattr__`` above, which a type checker
    # cannot see.
    client = cast("LithosClientProtocol", fake)
    return TestClient(create_app(config, lithos_client_factory=lambda _: client))


def get(config_path: Path, fake: GraphFakeClient, url: str) -> str:
    """Render a page and hand back its HTML with entities decoded.

    Decoded because the assertions below are about the SENTENCES the page
    states — "1 tasks' edges unreadable", an edge id "done->next" — and
    matching them against ``&#39;`` / ``&gt;`` would test Jinja's escaping
    instead of the page's claims.
    """
    with client_for(config_path, fake) as client:
        response = client.get(url)
    assert response.status_code == 200, response.text
    return unescape(response.text)


# ── HTML helpers: the text baseline is the contract, so read it as text ──


def layer_nodes(html: str, index: int) -> list[str]:
    """Node ids inside ``<ol data-graph-layer="index">``, in render order."""
    match = re.search(
        rf'<ol data-graph-layer="{index}">(.*?)</ol>\s*(?:<h3|</section>)',
        html,
        re.DOTALL,
    )
    assert match, f"no layer {index} in the render"
    return re.findall(r'data-graph-node="([^"]+)"', match.group(1))


def node_block(html: str, task_id: str) -> str:
    """The markup of one node row, from its own ``<li>`` to the next one."""
    anchor = html.index(f'data-graph-node="{task_id}"')
    start = html.rindex("<li", 0, anchor)
    following = html.find("data-graph-node=", anchor + 1)
    end = html.rindex("<li", 0, following) if following != -1 else len(html)
    return html[start:end]


def markers(html: str, task_id: str) -> set[str]:
    return set(re.findall(r'data-marker="([^"]+)"', node_block(html, task_id)))


def payload(html: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" data-graph-payload>(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match, "no embedded payload"
    return json.loads(match.group(1))


def only_group(pattern: str, html: str, group: int = 1) -> str:
    """The single capture of ``pattern``, asserted rather than Optional-chained."""
    match = re.search(pattern, html, re.DOTALL)
    assert match, f"no match for {pattern}"
    return match.group(group)


def rendered_layers(html: str) -> dict[str, int]:
    """task id -> the layer the TEXT put it in, across layers and disclosure."""
    return {
        node_id: int(layer)
        for node_id, layer in re.findall(
            r'data-graph-node="([^"]+)"\s*\n?\s*data-layer="(\d+)"', html
        )
    }


# ── Layers, ordering and the cycle callout ──────────────────────────────


def test_each_layer_is_an_ordered_list_carrying_every_node_s_status(
    lithos_lens_config_env: Path,
) -> None:
    """One `<ol>` per layer, status on every row — D3's first promise."""
    tasks = [task("a"), task("b"), task("c")]
    fake = GraphFakeClient(dataset(tasks, [("a", "b", "blocks"), ("b", "c", "blocks")]))

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert layer_nodes(html, 0) == ["a"]
    assert layer_nodes(html, 1) == ["b"]
    assert layer_nodes(html, 2) == ["c"]
    assert 'data-status="open"' in node_block(html, "a")


def test_a_cycle_names_its_members_in_order_with_one_representative_path(
    lithos_lens_config_env: Path,
) -> None:
    """The callout is deterministic: sorted members plus ONE path (D4)."""
    tasks = [
        task("cyc-b", created_at="2026-09-01T02:00:00+00:00"),
        task("cyc-a", created_at="2026-09-01T01:00:00+00:00"),
        task("downstream", created_at="2026-09-01T03:00:00+00:00"),
    ]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [
                ("cyc-a", "cyc-b", "blocks"),
                ("cyc-b", "cyc-a", "blocks"),
                ("cyc-b", "downstream", "blocks"),
            ],
            blocked={
                "cyc-a": cycle_blocker("cyc-b", "Dependency cycle: cyc-a -> cyc-b."),
                "cyc-b": cycle_blocker("cyc-a", "Dependency cycle: cyc-b -> cyc-a."),
            },
        )
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    callout = re.search(r"data-cycle-callout(.*?)</section>", html, re.DOTALL)
    assert callout
    members = re.search(r"data-cycle-members>(.*?)</span>", callout.group(1), re.DOTALL)
    assert members and members.group(1).strip() == "Cyc A, Cyc B"
    path = re.search(r"data-cycle-path>(.*?)</span>", callout.group(1), re.DOTALL)
    assert path
    assert path.group(1).split() == ["Cyc", "A", "→", "Cyc", "B", "→", "Cyc", "A"]
    # The whole reason cycle members are condensed rather than dropped: the
    # work below a cycle keeps a layer, and is marked unreachable.
    assert "blocked-via-cycle" in markers(html, "downstream")


def test_a_cross_scope_cycle_is_listed_as_external_and_draws_no_group(
    lithos_lens_config_env: Path,
) -> None:
    """A -> ghost -> ghost -> A is invisible to SCC, and still never dropped."""
    tasks = [
        task("a"),
        task("ghost-b", project="other"),
        task("ghost-c", project="other"),
    ]
    fake = GraphFakeClient(
        dataset(
            # Only ``a`` is in the loom project, so b and c are one-hop ghosts
            # and the edge closing the loop (c -> a) is never fetched from c.
            tasks,
            [("a", "ghost-b", "blocks"), ("ghost-c", "a", "blocks")],
            blocked={
                "a": cycle_blocker(
                    "ghost-c", "Dependency cycle: a -> ghost-b -> ghost-c -> a."
                )
            },
        )
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    external = re.search(r"data-cycle-external(.*?)</div>", html, re.DOTALL)
    assert external
    assert "Through tasks outside this scope" in html
    assert "Dependency cycle: a -> ghost-b -> ghost-c -> a." in external.group(1)
    # No SCC in the fetched topology, so no bracketed group is drawn for it.
    assert 'data-cycle-group="a"' not in html
    assert "in-cycle" in markers(html, "a")


# ── The scoped blocked reads (D4) ───────────────────────────────────────


def test_a_blocked_read_of_exactly_the_limit_marks_absent_tasks_unknown(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncation never renders as "no cycle" — it renders as "unknown"."""
    limit = 3
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", str(limit))
    filler = [task(f"blocked-{index}") for index in range(limit)]
    tasks = [task("absent"), *filler]
    fake = GraphFakeClient(
        dataset(tasks),
        blocked_rows={
            PROJECT: [
                BlockedTaskRecord(task=row, blockers=cycle_blocker("x", "cycle"))
                for row in filler
            ],
            f"project:{PROJECT}": [
                BlockedTaskRecord(task=row, blockers=cycle_blocker("x", "cycle"))
                for row in filler
            ],
        },
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert 'data-graph-banner="cycle-truncated"' in html
    assert "cycle-unknown" in markers(html, "absent")


def test_a_failed_blocked_read_banners_and_leaves_the_scc_cycle_rendered(
    lithos_lens_config_env: Path,
) -> None:
    """Lens's own shape survives Lithos going quiet: the claim degrades, not it."""
    tasks = [task("cyc-a"), task("cyc-b")]
    fake = GraphFakeClient(
        dataset(tasks, [("cyc-a", "cyc-b", "blocks"), ("cyc-b", "cyc-a", "blocks")]),
        blocked_failures={PROJECT, f"project:{PROJECT}"},
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert 'data-graph-banner="cycle-unavailable"' in html
    assert 'data-cycle-group="cyc-a"' in html
    assert "cycle-unknown" in markers(html, "cyc-a")


def test_an_epic_reads_every_project_its_children_span(
    lithos_lens_config_env: Path,
) -> None:
    """The coverage set is per §5B.1 project, not per scope (D4)."""
    tasks = [
        task("epic", task_type="epic"),
        task("child-loom"),
        task("child-other", project="other"),
    ]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [
                ("epic", "child-loom", "parent_child"),
                ("epic", "child-other", "parent_child"),
            ],
            children={"epic": ("child-loom", "child-other")},
        ),
        blocked_failures={"other", "project:other"},
    )

    html = get(lithos_lens_config_env, fake, "/tasks/graph?epic=epic")

    read_projects = {call["project"] for call in fake.blocked_calls if call["project"]}
    assert read_projects == {PROJECT, "other"}
    # Only the child in the failed project loses its cycle claim.
    assert "cycle-unknown" in markers(html, "child-other")
    assert "cycle-unknown" not in markers(html, "child-loom")


def test_a_projectless_child_is_unknown_with_no_read_attempted_for_it(
    lithos_lens_config_env: Path,
) -> None:
    """No scoped read can reach it, and Lens issues no unscoped one (D4)."""
    tasks = [
        task("epic", task_type="epic"),
        task("child-loom"),
        task("orphan", project=None),
    ]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [
                ("epic", "child-loom", "parent_child"),
                ("epic", "orphan", "parent_child"),
            ],
            children={"epic": ("child-loom", "orphan")},
        )
    )

    html = get(lithos_lens_config_env, fake, "/tasks/graph?epic=epic")

    assert all(call["project"] or call["tags"] for call in fake.blocked_calls), (
        "an unscoped blocked read was issued"
    )
    assert "cycle-unknown" in markers(html, "orphan")
    assert 'data-graph-banner="cycle-projectless"' in html


def test_the_tag_side_read_uses_the_configured_project_tag_key(
    lithos_lens_config_env: Path,
) -> None:
    """`project_tag_key = "proj"` changes the TAG, never the metadata read."""
    lithos_lens_config_env.write_text(
        lithos_lens_config_env.read_text()
        + '\n[lithos-lens.tasks]\nproject_tag_key = "proj"\n'
    )
    tasks = [task("a", project=None, extra_tags=("proj:loom",))]
    fake = GraphFakeClient(dataset(tasks))

    get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert {"proj:loom"} == {
        tag for call in fake.blocked_calls for tag in (call["tags"] or [])
    }


def test_a_downstream_ghost_s_project_joins_the_coverage_set(
    lithos_lens_config_env: Path,
) -> None:
    """Downstream ghosts are counted in impact, so their project must be readable."""
    tasks = [task("a"), task("ghost-downstream", project="other")]
    fake = GraphFakeClient(dataset(tasks, [("a", "ghost-downstream", "blocks")]))

    get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert {call["project"] for call in fake.blocked_calls if call["project"]} == {
        PROJECT,
        "other",
    }


# ── Ghosts, completeness and the chain ──────────────────────────────────


def test_a_ghost_row_carries_its_project_chip_and_both_links(
    lithos_lens_config_env: Path,
) -> None:
    tasks = [task("a"), task("ghost", project="other")]
    fake = GraphFakeClient(dataset(tasks, [("ghost", "a", "blocks")]))

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    row = node_block(html, "ghost")

    assert 'data-ghost-project="other"' in row
    assert 'href="/tasks/ghost' in row
    assert 'href="/tasks/graph?project=other"' in row


def test_the_chain_line_names_the_depth_five_chain(
    lithos_lens_config_env: Path,
) -> None:
    tasks = [task(name) for name in ("one", "two", "three", "four", "five")]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [
                ("one", "two", "blocks"),
                ("two", "three", "blocks"),
                ("three", "four", "blocks"),
                ("four", "five", "blocks"),
            ],
        )
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert 'data-chain-bound="exact"' in html
    chain = re.search(r"data-chain-nodes>(.*?)</span>", html, re.DOTALL)
    assert chain
    assert chain.group(1).split() == [
        "One",
        "→",
        "Two",
        "→",
        "Three",
        "→",
        "Four",
        "→",
        "Five",
    ]


def test_an_unreadable_edge_read_lowers_the_chain_and_marks_that_node(
    lithos_lens_config_env: Path,
) -> None:
    """A node Lens could not read is never isolated, and every claim it could
    affect says so (D2/D7/D8)."""
    tasks = [task(name) for name in ("one", "two", "three", "four", "five")]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [
                ("one", "two", "blocks"),
                ("two", "three", "blocks"),
                ("three", "four", "blocks"),
                ("four", "five", "blocks"),
            ],
        ),
        edge_failures={"three"},
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert 'data-chain-bound="lower_bound"' in html
    assert "≥" in html
    assert "1 tasks' edges unreadable" in html
    assert "edges-unknown" in markers(html, "three")
    assert 'data-graph-banner="edges-incomplete"' in html
    # In the layering, NOT folded into the disclosure: "no edges" is a claim
    # Lens has no evidence for here.
    isolated = re.search(r"data-isolated-list>(.*?)</ol>", html, re.DOTALL)
    assert isolated is None or 'data-graph-node="three"' not in isolated.group(1)


def test_an_unknown_ghost_predecessor_renders_unknown_and_marks_its_dependent(
    lithos_lens_config_env: Path,
) -> None:
    """A ghost Lens asked about and got no answer for: shown, not hidden (D2/D6)."""
    tasks = [task("a")]
    fake = GraphFakeClient(
        # ``ghost`` is absent from the master open list, so it needs a task_get
        # — and that read fails.
        dataset(
            [*tasks, task("ghost", status="completed", project="other")],
            [("ghost", "a", "blocks")],
        ),
        get_failures={"ghost"},
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert 'data-status="unknown"' in node_block(html, "ghost")
    assert "graph-node-unknown" in node_block(html, "ghost")
    assert "unresolvable" in markers(html, "a")
    assert 'data-chain-bound="lower_bound"' in html
    assert "1 edges unresolvable" in html


# ── Scope membership, isolation and the hierarchy tree ──────────────────


def test_an_epic_graph_fades_and_labels_a_completed_child_s_satisfied_edge(
    lithos_lens_config_env: Path,
) -> None:
    tasks = [
        task("epic", task_type="epic"),
        task("done", status="completed"),
        task("next"),
    ]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [
                ("epic", "done", "parent_child"),
                ("epic", "next", "parent_child"),
                ("done", "next", "blocks"),
            ],
            children={"epic": ("done", "next")},
        )
    )

    html = get(lithos_lens_config_env, fake, "/tasks/graph?epic=epic")
    edge = re.search(
        r'<li\s+class="([^"]*)"\s+data-edge="done->next"([^>]*)>', html, re.DOTALL
    )

    assert edge, "the satisfied edge is not rendered under its dependent"
    assert 'data-edge-state="inactive"' in edge.group(2)
    assert 'data-edge-reason="satisfied"' in edge.group(2)
    # Faded AND labelled: the class alone would say "history" without saying
    # which kind, and `satisfied` is a different fact from `dependent_resolved`.
    assert "graph-edge-inactive" in edge.group(1)
    assert "inactive — satisfied" in html


def test_include_resolved_zero_hides_an_epic_s_closed_children_everywhere(
    lithos_lens_config_env: Path,
) -> None:
    """Including the hierarchy tree: what the toggle removed stays removed."""
    tasks = [
        task("epic", task_type="epic"),
        task("done", status="completed"),
        task("next"),
    ]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [("epic", "done", "parent_child"), ("epic", "next", "parent_child")],
            children={"epic": ("done", "next")},
        )
    )

    html = get(
        lithos_lens_config_env, fake, "/tasks/graph?epic=epic&include_resolved=0"
    )

    assert 'data-graph-node="done"' not in html
    assert 'data-hierarchy-node="done"' not in html
    assert 'data-hierarchy-node="next"' in html


def test_the_hierarchy_tree_shows_a_completed_parent_of_an_open_child(
    lithos_lens_config_env: Path,
) -> None:
    """Context is added upstream only (D6) — and the tree is always rendered."""
    tasks = [task("child"), task("parent-epic", status="completed", task_type="epic")]
    fake = GraphFakeClient(dataset(tasks, [("parent-epic", "child", "parent_child")]))

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert 'data-hierarchy-node="parent-epic"' in html
    assert 'data-hierarchy-node="child"' in html


def test_the_isolated_disclosure_is_collapsed_on_a_project_and_open_on_an_epic(
    lithos_lens_config_env: Path,
) -> None:
    tasks = [
        task("epic", task_type="epic"),
        task("lonely-one"),
        task("lonely-two"),
    ]
    edges = [
        ("epic", "lonely-one", "parent_child"),
        ("epic", "lonely-two", "parent_child"),
    ]
    fake = GraphFakeClient(
        dataset(tasks, edges, children={"epic": ("lonely-one", "lonely-two")})
    )

    project_html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    epic_html = get(lithos_lens_config_env, fake, "/tasks/graph?epic=epic")

    assert re.search(r"data-isolated-disclosure\s*>", project_html), "project opened it"
    assert re.search(r"data-isolated-disclosure\s+open", epic_html), "epic collapsed it"
    assert only_group(r"data-isolated-count>(\d+)<", epic_html) == "3"


def test_the_legend_lists_exactly_the_visible_edge_types(
    lithos_lens_config_env: Path,
) -> None:
    tasks = [task("epic", task_type="epic"), task("a"), task("b")]
    fake = GraphFakeClient(
        dataset(tasks, [("a", "b", "blocks"), ("epic", "a", "parent_child")])
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert re.findall(r'data-legend-edge="([^"]+)"', html) == ["blocks", "parent_child"]
    assert 'data-legend-convention="ghost"' in html
    assert 'data-legend-convention="cycle"' in html


def test_the_payload_node_set_and_layers_match_the_text(
    lithos_lens_config_env: Path,
) -> None:
    """A4 draws from the payload, so a disagreement here is a lying picture."""
    tasks = [task("epic", task_type="epic"), task("a"), task("b"), task("lonely")]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [
                ("a", "b", "blocks"),
                ("epic", "a", "parent_child"),
                ("epic", "lonely", "parent_child"),
            ],
        )
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    data = payload(html)
    text = rendered_layers(html)

    assert {node["id"] for node in data["nodes"]} == set(text)
    assert {node["id"]: node["layer"] for node in data["nodes"]} == text
    assert data["layers"][0] == layer_nodes(html, 0)
    # The epic hangs off nothing but its own hierarchy edges, which is exactly
    # what the disclosure folds away (D8): hierarchy never rescues a task from
    # isolation.
    assert data["isolated"] == ["epic", "lonely"]
    assert data["as_of"]


# ── The route's own shape: picker and refusal ───────────────────────────


def test_the_unscoped_route_offers_projects_and_open_epics(
    lithos_lens_config_env: Path,
) -> None:
    tasks = [
        task("a"),
        task("b", project="other"),
        task("epic", task_type="epic"),
        task("closed-epic", status="completed", task_type="epic"),
    ]
    fake = GraphFakeClient(dataset(tasks))

    html = get(lithos_lens_config_env, fake, "/tasks/graph")

    assert "data-graph-picker" in html
    assert re.findall(r'data-picker-project="([^"]+)"', html) == ["loom", "other"]
    assert re.findall(r'data-picker-epic="([^"]+)"', html) == ["epic"]
    assert "data-graph-layers" not in html


def test_a_scope_one_task_over_the_guard_is_refused_with_its_count(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITHOS_LENS_GRAPH_MAX_TASKS", "3")
    tasks = [task(f"t{index}") for index in range(4)]
    fake = GraphFakeClient(dataset(tasks))

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert "data-graph-refusal" in html
    assert "Narrow your scope" in html
    assert only_group(r"data-refusal-count>(\d+)<", html) == "4"
    assert "data-graph-layers" not in html
    # Refused BEFORE the fan-out: the guard exists to prevent exactly that cost.
    assert fake.edge_calls == []


def test_the_nav_offers_the_graph_page(lithos_lens_config_env: Path) -> None:
    fake = GraphFakeClient(dataset([task("a")]))

    html = get(lithos_lens_config_env, fake, "/tasks/graph")

    assert '<a class="active" aria-current="page" href="/tasks/graph">Graph</a>' in html


def test_an_offline_lithos_renders_the_degraded_page_not_a_graph(
    lithos_lens_config_env: Path,
) -> None:
    fake = GraphFakeClient(dataset([task("a")]), health="unreachable")

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert "Lithos is offline or degraded." in html
    assert "data-graph-layers" not in html


# ── URL state (D8) ──────────────────────────────────────────────────────


def test_selected_is_canonicalised_to_focus_and_defaults_flip_by_scope_kind() -> None:
    """One selection parameter per host page; two defaults, opposite ways round."""
    assert parse_graph_params({"project": "loom", "selected": "abc"}).focus == "abc"
    assert parse_graph_params({"project": "loom"}).include_resolved is False
    assert parse_graph_params({"epic": "e1"}).include_resolved is True
    assert parse_graph_params({"project": "loom"}).show_isolated is False
    assert parse_graph_params({"epic": "e1"}).show_isolated is True
    overlays = parse_graph_params(
        {"epic": "e1", "overlays": "hierarchy,bogus"}
    ).overlays
    assert overlays == ("hierarchy",)


def test_a_new_scope_url_drops_the_current_page_s_focus() -> None:
    """A ghost's project-graph link must not point at a node that scope lacks."""
    params = parse_graph_params({"project": "loom", "focus": "abc", "isolated": "1"})

    assert graph_url(project="other") == "/tasks/graph?project=other"
    assert "focus=abc" in graph_url(params)
    assert "isolated=0" in graph_url(params, isolated=False)


def test_a_claimed_task_shows_its_claim_on_the_row(
    lithos_lens_config_env: Path,
) -> None:
    rows = [task("a")]
    data = FakeLithosDataset(
        tasks=tuple(rows), claims={"a": (ClaimRecord(agent="worker-a", aspect="impl"),)}
    )
    fake = GraphFakeClient(data)

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert "claimed by worker-a" in node_block(html, "a")


# ── Telemetry (`lens.tasks.graph`) ──────────────────────────────────────


def test_the_render_records_its_shape_on_the_span_and_counts_the_outcome(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    """The PRD's telemetry point: shape on the span, bounded labels on the counter."""
    tasks = [task("a"), task("b"), task("ghost", project="other")]
    fake = GraphFakeClient(
        dataset(tasks, [("a", "b", "blocks"), ("b", "ghost", "blocks")])
    )

    get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    span = next(
        span
        for span in spans.get_finished_spans()
        if (span.attributes or {}).get("http.route") == "/tasks/graph"
    )
    attributes = span.attributes or {}
    assert attributes["lens.graph.scope_kind"] == "project"
    assert attributes["lens.graph.scope_key"] == PROJECT
    assert attributes["lens.graph.outcome"] == "rendered"
    assert attributes["lens.graph.nodes"] == 3
    assert attributes["lens.graph.ghosts"] == 1
    assert attributes["lens.graph.chain_length"] == 3
    # A cold page fetches every in-scope node's edges once: the fan-out figure
    # is the evidence behind the bulk-fetch ask (ROADMAP ledger #3).
    assert attributes["lens.graph.cache_misses"] == 2
    assert (
        metric_value(
            metric_reader,
            "lens_tasks_graph_renders_total",
            scope="project",
            outcome="rendered",
        ).value
        == 1
    )
    assert (
        metric_value(
            metric_reader, "lens_tasks_graph_cycle_reads_total", outcome="ok"
        ).value
        == 4
    )


def test_a_failed_cycle_read_is_counted_as_such(
    lithos_lens_config_env: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    """ "How often is the cycle signal partial here?" without reading banners."""
    fake = GraphFakeClient(
        dataset([task("a")]), blocked_failures={PROJECT, f"project:{PROJECT}"}
    )

    get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert (
        metric_value(
            metric_reader, "lens_tasks_graph_cycle_reads_total", outcome="failed"
        ).value
        == 2
    )
