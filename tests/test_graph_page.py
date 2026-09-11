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

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lithos_lens import graph_scope
from lithos_lens.config import DEFAULT_TASKS_FRONTIER_LIMIT, load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_graph_dataset import edge_index
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.graph_cache import GraphCache
from lithos_lens.graph_cycles import CycleSignal
from lithos_lens.graph_page import build_graph_page, graph_url, parse_graph_params
from lithos_lens.graph_routes import GRAPH_SPAN
from lithos_lens.graph_scope import load_project_scope
from lithos_lens.lithos_client import (
    LithosClientProtocol,
    LithosHealth,
    LithosToolError,
)
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import (
    ClaimRecord,
    TaskRecord,
    TaskStatusName,
    task_detail_path,
)
from lithos_lens.web import create_app
from tests.conftest import metric_value

pytestmark = pytest.mark.anyio

PROJECT = "loom"
#: The `frontier_limit` every unconfigured fixture's blocked reads carry — the
#: call log is asserted against it rather than a literal, so a change to the
#: shipped default fails here instead of silently rewriting the contract.
LIMIT = DEFAULT_TASKS_FRONTIER_LIMIT


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


class StepClock:
    """A clock a test advances by hand — the only way two cache entries differ
    in age without a sleep."""

    def __init__(self, start: datetime = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)):
        self.start = start
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def metadata_task(
    task_id: str,
    *,
    project: str,
    status: TaskStatusName = "open",
    extra_tags: Sequence[str] = (),
) -> TaskRecord:
    """A task that claims its project the OTHER way — ``metadata.project``."""
    return replace(
        task(task_id, project=None, status=status, extra_tags=extra_tags),
        metadata={"project": project},
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
        list_failures: set[str] | None = None,
        children_failures: set[str] | None = None,
        edge_gate: asyncio.Event | None = None,
        health: LithosHealth = "ok",
    ) -> None:
        self._client = FakeLithosClient(dataset=data)
        self._edge_failures = edge_failures or set()
        self._get_failures = get_failures or set()
        # Keyed by the READ, not by the project: "loom" fails the metadata
        # half and "project:loom" the tag half, so a test can break exactly
        # one side of a pair — which is the only way to tell "both halves must
        # be complete" apart from "either half is enough".
        self._blocked_failures = blocked_failures or set()
        self._blocked_rows = blocked_rows or {}
        self._list_failures = list_failures or set()
        self._edge_gate = edge_gate
        self._children_failures = children_failures or set()
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
        if str(kwargs.get("status") or "") in self._list_failures:
            raise LithosToolError("task_list failed", code="internal_error")
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
            # Honour the limit the way the server does, so a test that injects
            # exactly `limit` rows is testing the page's truncation rule and
            # not the fake's willingness to overrun.
            rows = list(self._blocked_rows[key])
            return rows[:limit] if limit is not None else rows
        return await self._client.task_blocked(limit=limit, project=project, tags=tags)

    async def task_get(self, task_id: str) -> TaskRecord:
        self.get_calls.append(task_id)
        if task_id in self._get_failures:
            raise LithosToolError("task_get failed", code="internal_error")
        return await self._client.task_get(task_id)

    async def task_children(
        self, task_id: str, *, recursive: bool = False, include_closed: bool = False
    ) -> list[TaskRecord]:
        if task_id in self._children_failures:
            raise LithosToolError("task_children failed", code="internal_error")
        return await self._client.task_children(
            task_id, recursive=recursive, include_closed=include_closed
        )

    async def task_edge_list(
        self, task_id: str, *, direction: str = "both", types: list[str] | None = None
    ) -> list[EdgeRecord]:
        self.edge_calls.append(task_id)
        if self._edge_gate is not None:
            await self._edge_gate.wait()
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


def blocked_log(fake: GraphFakeClient) -> list[tuple[str, str, int | None]]:
    """The blocked-read call log, normalized to ``(by, value, limit)``.

    Normalized rather than reduced to a set of project names: D4's contract is
    one metadata call and one tag call PER covered project, each carrying
    ``frontier_limit`` and exactly one of the two filters. A set of project
    values hides a duplicate call, a missing tag half, and a malformed call
    that sends both filters at once.
    """
    log: list[tuple[str, str, int | None]] = []
    for call in fake.blocked_calls:
        project, tags = call["project"], call["tags"]
        assert not (project and tags), f"call sends both filters: {call}"
        if project:
            log.append(("project", project, call["limit"]))
        elif tags:
            assert len(tags) == 1, f"a read pair sends one tag: {call}"
            log.append(("tags", tags[0], call["limit"]))
        else:
            log.append(("unscoped", "", call["limit"]))
    return sorted(log)


def section_order(html: str) -> list[str]:
    """The page's own section markers, in the order they are rendered (D3)."""
    markers = (
        ("callout", "data-cycle-callout"),
        ("banner", "data-graph-banner"),
        ("legend", "data-graph-legend"),
        ("chain", "data-longest-chain"),
        ("layers", "data-graph-layers"),
        ("isolated", "data-isolated-disclosure"),
        # The SECTION, not its tree: D3 renders it whatever the scope holds,
        # and a graph with no `parent_child` edge still owes its empty state.
        ("hierarchy", "data-hierarchy"),
        ("payload", "data-graph-payload"),
    )
    found = [(html.index(hook), name) for name, hook in markers if hook in html]
    return [name for _, name in sorted(found)]


def chain_line(html: str) -> str:
    """The rendered chain sentence, tags removed and whitespace collapsed.

    Tags are dropped rather than replaced with a space: the sentence D3
    specifies is "Longest blocking chain (5): A → B", and a helper that spaced
    out every inline ``<span>`` would let "( 5 )" pass for it.
    """
    paragraph = only_group(r"data-longest-chain[^>]*>.*?(<p>.*?</p>)", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", paragraph)).strip()


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
    # Lithos's verdict travels with the row even when LENS can draw the shape
    # (D4): the message is Lithos's, and a page that dropped it for the cases
    # it can draw would be keeping it only where it has nothing else to say.
    assert "in-cycle" in markers(html, "cyc-a")
    assert "in-cycle" in markers(html, "cyc-b")
    # The message itself, read out of the row's own element rather than out of
    # the marker's tooltip — a page that kept only the tooltip would show it
    # to a mouse and to nobody else.
    for member, message in (
        ("cyc-a", "Dependency cycle: cyc-a -> cyc-b."),
        ("cyc-b", "Dependency cycle: cyc-b -> cyc-a."),
    ):
        row = node_block(html, member)
        assert only_group(r"data-cycle-message>(.*?)</span>", row).strip() == message


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


def test_a_flagged_cycle_with_an_in_scope_partner_is_shape_unavailable(
    lithos_lens_config_env: Path,
) -> None:
    """Missing SHAPE is not evidence of a cycle leaving the scope (D4).

    Reachable: two in-scope tasks with warm edge-empty cache entries, then an
    edge upsert — which emits no invalidating event — creates their cycle. The
    fresh blocked row names an IN-SCOPE partner while the stale edge cache has
    no component, so "through tasks outside this scope" would be a claim about
    where the loop runs that nothing supports.
    """
    fake = GraphFakeClient(
        dataset(
            [task("a"), task("b")],
            blocked={"a": cycle_blocker("b", "Dependency cycle: a -> b -> a.")},
        )
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    unavailable = only_group(r"data-cycle-shape-unavailable>(.*?)</div>", html)

    assert 'data-cycle="a"' in unavailable
    assert "Dependency cycle: a -> b -> a." in unavailable
    assert "Through tasks outside this scope" not in html
    # The category is NEUTRAL: the blocker names one immediate predecessor, so
    # an in-scope one leaves the rest of the path unknown. The page may not
    # claim the loop is inside the scope either.
    heading = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unavailable)).strip()
    assert heading.startswith(
        "Shape unavailable — Lithos reports these cycles, and this graph's "
        "edges do not show them"
    ), heading
    assert "inside this scope" not in heading
    # Still Lithos's verdict on the row itself, whatever Lens could draw.
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

    # The exact contract: one metadata call and one tag call, each carrying
    # frontier_limit — which is also what makes "len == limit" mean anything.
    assert blocked_log(fake) == [
        ("project", PROJECT, limit),
        ("tags", f"project:{PROJECT}", limit),
    ]
    assert 'data-graph-banner="cycle-truncated"' in html
    assert "cycle-unknown" in markers(html, "absent")
    # ABSENT tasks are unknown; a task the truncated response actually named is
    # answered for, and keeps Lithos's verdict without being contradicted.
    assert markers(html, "blocked-0") >= {"in-cycle"}
    assert "cycle-unknown" not in markers(html, "blocked-0")


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
    # …and the row does NOT claim membership. D4 makes membership Lithos's and
    # the shape Lens's: with both reads failed there is no verdict, so a row
    # reading "in a cycle" beside "cycle status unknown" would contradict
    # itself AND put Lens's own inference where the authority belongs.
    assert "in-cycle" not in markers(html, "cyc-a")
    assert "in-cycle" not in markers(html, "cyc-b")
    assert "data-cycle-message" not in node_block(html, "cyc-a")


def test_one_truncated_half_of_a_pair_still_leaves_an_absent_task_unknown(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5B.7 unions the pair, so coverage needs BOTH halves complete.

    A task claimed by tag only is absent from the ``project=`` response for a
    reason that has nothing to do with blocking, so a complete metadata read
    says nothing about what the truncated tag read did not return.
    """
    limit = 2
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", str(limit))
    filler = [task(f"blocked-{index}") for index in range(limit)]
    tasks = [task("absent"), *filler]
    fake = GraphFakeClient(
        dataset(tasks),
        blocked_rows={
            # Only the TAG half fills its limit; the metadata half answers in
            # full (the fake's default: no blocked rows in the dataset).
            f"project:{PROJECT}": [
                BlockedTaskRecord(task=row, blockers=()) for row in filler
            ]
        },
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert 'data-graph-banner="cycle-truncated"' in html
    assert "cycle-unknown" in markers(html, "absent")


def test_one_failed_half_of_a_pair_leaves_the_project_s_tasks_unknown(
    lithos_lens_config_env: Path,
) -> None:
    """Same rule, the other failure mode: half a pair is not coverage."""
    fake = GraphFakeClient(
        dataset([task("a")]), blocked_failures={f"project:{PROJECT}"}
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert blocked_log(fake) == [
        ("project", PROJECT, LIMIT),
        ("tags", f"project:{PROJECT}", LIMIT),
    ]
    assert 'data-graph-banner="cycle-unavailable"' in html
    assert "cycle-unknown" in markers(html, "a")


def test_a_cycle_blocker_from_either_half_of_the_pair_is_kept(
    lithos_lens_config_env: Path,
) -> None:
    """The pair is unioned per TASK, blockers included (§5B.7).

    The two calls are independent reads, not one snapshot: the tag-side
    response can carry the ``kind="cycle"`` blocker that the metadata-side
    response — read a moment earlier — did not have yet. Keeping whichever row
    landed first would drop Lithos's verdict while its message rendered beside
    it, and the callout would go missing for a task that cannot run.
    """
    rows = [task("a")]
    fake = GraphFakeClient(
        dataset(rows),
        blocked_rows={
            PROJECT: [BlockedTaskRecord(task=rows[0], blockers=())],
            f"project:{PROJECT}": [
                BlockedTaskRecord(
                    task=rows[0],
                    blockers=cycle_blocker("elsewhere", "Dependency cycle: a -> ?."),
                )
            ],
        },
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert "in-cycle" in markers(html, "a")
    assert "Dependency cycle: a -> ?." in html
    assert "data-cycle-external" in html


def test_a_partial_read_banner_claims_only_the_tasks_it_actually_left_unknown(
    lithos_lens_config_env: Path,
) -> None:
    """Coverage is per task, so the banner may not talk about whole projects.

    The metadata half answers in full and names A; the tag half fails. A is
    therefore KNOWN — and a banner saying "their tasks are marked cycle status
    unknown" would contradict the markers rendered beside it.
    """
    rows = [task("a")]
    fake = GraphFakeClient(
        dataset(rows),
        blocked_rows={
            PROJECT: [
                BlockedTaskRecord(
                    task=rows[0], blockers=cycle_blocker("elsewhere", "Cycle: a.")
                )
            ]
        },
        blocked_failures={f"project:{PROJECT}"},
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    banner = only_group(r'data-graph-banner="cycle-unavailable">(.*?)</section>', html)

    assert "the blocked read failed for loom" in banner
    # The banner states what the READ was, not which rows ended up marked: any
    # per-task rule here is false for some combination of outcomes.
    assert "A failed read is not evidence that a task is cycle-free." in banner
    assert "marked cycle status unknown" not in banner
    # Nothing was actually left unknown, so no count banner and no marker.
    assert 'data-graph-banner="cycle-unknown-count"' not in html
    assert "cycle-unknown" not in markers(html, "a")
    assert "in-cycle" in markers(html, "a")


def test_a_truncated_half_that_returned_a_task_does_not_contradict_its_marker(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The combination the per-task banner rules used to get wrong.

    The metadata half TRUNCATES after returning A (so no complete read covered
    A at all), the tag half fails, and B is absent from both. A is known —
    Lithos answered about it — while B is not, and the page must say exactly
    that rather than a rule that contradicts A's own row.
    """
    limit = 1
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", str(limit))
    rows = [task("a"), task("b")]
    fake = GraphFakeClient(
        dataset(rows),
        blocked_rows={
            PROJECT: [
                BlockedTaskRecord(
                    task=rows[0], blockers=cycle_blocker("elsewhere", "Cycle: a.")
                )
            ]
        },
        blocked_failures={f"project:{PROJECT}"},
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert 'data-graph-banner="cycle-truncated"' in html
    assert 'data-graph-banner="cycle-unavailable"' in html
    assert "in-cycle" in markers(html, "a")
    assert "cycle-unknown" not in markers(html, "a")
    assert "cycle-unknown" in markers(html, "b")
    # One task, and the sentence that names the rule it was marked under.
    count = only_group(r'data-graph-banner="cycle-unknown-count">(.*?)</section>', html)
    assert "1 tasks on this page are marked cycle status unknown" in count
    assert "no complete read that could have matched them was made" in count


def test_the_tasks_a_partial_read_did_leave_unknown_are_counted(
    lithos_lens_config_env: Path,
) -> None:
    """And when it DOES leave tasks unknown, the page states how many."""
    fake = GraphFakeClient(
        dataset([task("a"), task("b")]),
        blocked_failures={PROJECT, f"project:{PROJECT}"},
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert "2 tasks on this page are marked cycle status unknown" in html
    assert markers(html, "a") >= {"cycle-unknown"}
    assert markers(html, "b") >= {"cycle-unknown"}


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

    assert blocked_log(fake) == [
        ("project", PROJECT, LIMIT),
        ("project", "other", LIMIT),
        ("tags", f"project:{PROJECT}", LIMIT),
        ("tags", "project:other", LIMIT),
    ]
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

    assert blocked_log(fake) == [
        ("project", PROJECT, LIMIT),
        ("tags", f"project:{PROJECT}", LIMIT),
    ], "an unscoped or extra blocked read was issued"
    assert "cycle-unknown" in markers(html, "orphan")
    assert 'data-graph-banner="cycle-projectless"' in html


@pytest.mark.parametrize(
    ("posture", "child", "issued"),
    [
        # Only ``project=`` is issued, and it expresses the METADATA
        # convention (§5B.1) — so a child that claims its project by TAG could
        # never appear in the response, and an empty one is not coverage.
        ("metadata", "tag", [("project", "other", LIMIT)]),
        # The mirror image: only the tag read is issued, and a metadata-only
        # child is invisible to it.
        ("tag", "metadata", [("tags", "project:other", LIMIT)]),
    ],
)
def test_a_single_convention_posture_never_reads_coverage_it_did_not_get(
    lithos_lens_config_env: Path, posture: str, child: str, issued: list[Any]
) -> None:
    """An empty response from a filter that cannot match the task is not
    coverage — it is silence, and D4 says silence is `cycle status unknown`."""
    lithos_lens_config_env.write_text(
        lithos_lens_config_env.read_text()
        + f'\n[lithos-lens.tasks]\nproject_convention = "{posture}"\n'
    )
    rows = [
        task("epic", task_type="epic", project=None),
        task("child", project="other")
        if child == "tag"
        else metadata_task("child", project="other"),
    ]
    fake = GraphFakeClient(
        dataset(
            rows,
            [("epic", "child", "parent_child")],
            children={"epic": ("child",)},
        )
    )

    html = get(lithos_lens_config_env, fake, "/tasks/graph?epic=epic")

    assert blocked_log(fake) == issued
    assert "cycle-unknown" in markers(html, "child")
    assert 'data-graph-banner="cycle-unknown-count"' in html


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

    assert blocked_log(fake) == [
        ("project", PROJECT, LIMIT),
        ("tags", "proj:loom", LIMIT),
    ]


def test_a_metadata_only_scope_includes_its_tasks_and_reads_its_pair(
    lithos_lens_config_env: Path,
) -> None:
    """§5B.1 has TWO live conventions and the graph must honour both.

    Every other scoped fixture here claims its project through the
    ``project:<slug>`` tag, so a membership rule or a coverage set that read
    only tags would pass them all. This one carries `metadata.project` and
    nothing else, on the in-scope task AND on a downstream ghost.
    """
    rows = [
        metadata_task("a", project="meta-loom"),
        metadata_task("ghost", project="meta-other"),
    ]
    fake = GraphFakeClient(dataset(rows, [("a", "ghost", "blocks")]))

    html = get(lithos_lens_config_env, fake, "/tasks/graph?project=meta-loom")

    assert 'data-graph-node="a"' in html
    assert 'data-ghost-project="meta-other"' in html
    assert blocked_log(fake) == [
        ("project", "meta-loom", LIMIT),
        ("project", "meta-other", LIMIT),
        ("tags", "project:meta-loom", LIMIT),
        ("tags", "project:meta-other", LIMIT),
    ]


def test_a_task_whose_two_conventions_disagree_is_read_under_both(
    lithos_lens_config_env: Path,
) -> None:
    """§5B.1: the slugs are a UNION, so BOTH projects get a read pair.

    Reading only one would leave the task's cycle status resting on a project
    it may not even be blocked in, while the other project's graph shows it.
    """
    rows = [
        task("anchor"),
        metadata_task("split", project="meta-side", extra_tags=("project:tag-side",)),
    ]
    fake = GraphFakeClient(dataset(rows, [("anchor", "split", "blocks")]))

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    # The split task is a downstream ghost of the anchor: both its slugs are in
    # the coverage set, and neither is dropped for the other.
    assert 'data-ghost-project="meta-side"' in html
    assert 'data-ghost-project="tag-side"' in html
    assert blocked_log(fake) == [
        ("project", PROJECT, LIMIT),
        ("project", "meta-side", LIMIT),
        ("project", "tag-side", LIMIT),
        ("tags", f"project:{PROJECT}", LIMIT),
        ("tags", "project:meta-side", LIMIT),
        ("tags", "project:tag-side", LIMIT),
    ]


def test_a_downstream_ghost_s_project_joins_the_coverage_set(
    lithos_lens_config_env: Path,
) -> None:
    """Downstream ghosts are counted in impact, so their project must be readable."""
    tasks = [task("a"), task("ghost-downstream", project="other")]
    fake = GraphFakeClient(dataset(tasks, [("a", "ghost-downstream", "blocks")]))

    get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert blocked_log(fake) == [
        ("project", PROJECT, LIMIT),
        ("project", "other", LIMIT),
        ("tags", f"project:{PROJECT}", LIMIT),
        ("tags", "project:other", LIMIT),
    ]


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
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

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
    # The literal line D3 asks for, numerals and all — and the label that keeps
    # it a claim about THIS graph rather than a corpus-wide critical path.
    assert "Longest blocking chain within this graph" in text
    assert "Longest blocking chain ( 5 ): One → Two → Three → Four → Five" in text, text
    assert "≥" not in text


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
    assert chain_line(html) == (
        "Longest blocking chain (≥ 5, incomplete: 1 tasks' edges unreadable): "
        "One → Two → Three → Four → Five"
    )
    assert "within this graph" in re.sub(r"<[^>]+>", " ", html)
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


def test_isolated_1_and_0_flip_each_scope_s_default_and_the_toggle_says_so(
    lithos_lens_config_env: Path,
) -> None:
    """The toggle is a URL parameter (D8), so both values must actually work.

    Defaults that cannot be overridden are not defaults, and a toggle link
    that points at the value already in force is a dead control.
    """
    tasks = [task("epic", task_type="epic"), task("lonely-one"), task("lonely-two")]
    edges = [
        ("epic", "lonely-one", "parent_child"),
        ("epic", "lonely-two", "parent_child"),
    ]
    fake = GraphFakeClient(
        dataset(tasks, edges, children={"epic": ("lonely-one", "lonely-two")})
    )

    opened = get(
        lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}&isolated=1"
    )
    collapsed = get(lithos_lens_config_env, fake, "/tasks/graph?epic=epic&isolated=0")

    # Project scope defaults collapsed; isolated=1 opens it.
    assert re.search(r"data-isolated-disclosure\s+open", opened), "isolated=1 ignored"
    # Epic scope defaults open; isolated=0 collapses it.
    assert re.search(r"data-isolated-disclosure\s*>", collapsed), "isolated=0 ignored"
    # And each page's toggle points at the OTHER value, keeping its scope.
    opened_toggle = only_group(r'data-toggle-isolated href="([^"]+)"', opened)
    collapsed_toggle = only_group(r'data-toggle-isolated href="([^"]+)"', collapsed)
    assert f"project={PROJECT}" in opened_toggle and "isolated=0" in opened_toggle
    assert "epic=epic" in collapsed_toggle and "isolated=1" in collapsed_toggle
    assert "Hide isolated tasks" in opened
    assert "Show isolated tasks" in collapsed


@pytest.mark.parametrize("value", ["2", "garbage", "-1", "%20"])
def test_a_malformed_toggle_keeps_the_scope_s_default_rather_than_inverting_it(
    lithos_lens_config_env: Path, value: str
) -> None:
    """A value outside the documented `1|0` domain carries no request.

    The two toggles default by scope kind in OPPOSITE directions, so reading
    "anything I do not recognise is false" would silently hide an epic's closed
    children and collapse a disclosure the scope opens by default — behaviour
    with the opposite meaning to the one D6/D8 specify.
    """
    tasks = [
        task("epic", task_type="epic"),
        task("done", status="completed"),
        task("lonely"),
    ]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [("epic", "done", "parent_child"), ("epic", "lonely", "parent_child")],
            children={"epic": ("done", "lonely")},
        )
    )

    junk = get(
        lithos_lens_config_env,
        fake,
        f"/tasks/graph?epic=epic&include_resolved={value}&isolated={value}",
    )
    explicit = get(
        lithos_lens_config_env,
        fake,
        "/tasks/graph?epic=epic&include_resolved=0&isolated=0",
    )

    # An epic shows its closed children and opens the disclosure by DEFAULT,
    # and junk leaves both exactly there …
    assert 'data-graph-node="done"' in junk, "include_resolved default inverted"
    assert re.search(r"data-isolated-disclosure\s+open", junk), "isolated inverted"
    # … while a RECOGNISED false value still takes effect, so this is a parser
    # that reads its domain rather than one that ignores the parameter.
    assert 'data-graph-node="done"' not in explicit
    assert re.search(r"data-isolated-disclosure\s*>", explicit)


def legend_lines(html: str) -> list[tuple[str, str]]:
    """Every legend row as ``(type, sentence)``, in order and NOT deduplicated.

    A dict would hide the failure D8's "one line per visible edge type" is
    about: two lines for the same type, or a repeated convention, reads as a
    legend that cannot be trusted to enumerate anything.
    """
    return [
        (edge_type, re.sub(r"\s+", " ", line).strip())
        for edge_type, line in re.findall(
            r'data-legend-edge="([^"]+)">(.*?)</li>', html, re.DOTALL
        )
    ]


def legend_conventions(html: str) -> list[tuple[str, str]]:
    return [
        (name, re.sub(r"\s+", " ", line).strip())
        for name, line in re.findall(
            r'data-legend-convention="([^"]+)">(.*?)</li>', html, re.DOTALL
        )
    ]


def test_the_legend_explains_exactly_the_visible_edge_types_in_words(
    lithos_lens_config_env: Path,
) -> None:
    """A legend hook with no sentence in it is not a legend (D3/D8).

    Direction is what the text baseline cannot show, so each visible type gets
    the plain-language line that says which way the arrow runs — once, in
    legend order, and only for the types this graph actually contains.
    """
    tasks = [
        task("epic", task_type="epic"),
        task("gate", task_type="gate"),
        task("a"),
        task("b"),
    ]
    fake = GraphFakeClient(
        dataset(
            tasks,
            [
                ("a", "b", "blocks"),
                ("gate", "a", "waits_on_gate"),
                ("epic", "a", "parent_child"),
                ("epic", "gate", "discovered_from"),
            ],
        )
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    # All four types at once, so the DEPENDENCY lines — the default view, and
    # the ones an operator reads direction off — are covered too.
    assert legend_lines(html) == [
        ("blocks", "A → B means A blocks B: B cannot start until A is done."),
        ("waits_on_gate", "A ⇢ B means B waits on gate A."),
        (
            "parent_child",
            "A ▸ B means A is B's parent (hierarchy, never satisfied).",
        ),
        ("discovered_from", "A ⋯ B means B was discovered from A."),
    ]
    assert legend_conventions(html) == [
        (
            "ghost",
            "A ghost is a task outside this scope, shown once with its "
            "project; its own edges are never fetched.",
        ),
        (
            "cycle",
            "A cycle is bracketed in its layer; everything below it is marked "
            '"blocked via cycle".',
        ),
    ]


def test_the_legend_omits_the_edge_types_a_graph_does_not_contain(
    lithos_lens_config_env: Path,
) -> None:
    """ "Exactly the visible edge types" — the omission half of the rule."""
    fake = GraphFakeClient(dataset([task("a"), task("b")], [("a", "b", "blocks")]))

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert [edge_type for edge_type, _ in legend_lines(html)] == ["blocks"]
    assert [name for name, _ in legend_conventions(html)] == ["ghost", "cycle"]


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


def test_the_payload_carries_D3_s_whole_schema(
    lithos_lens_config_env: Path,
) -> None:
    """A4 renders from these fields alone, and the scope/layout suites test the
    objects BEFORE serialisation — so without this a field can be renamed,
    dropped or filled wrong while every other test stays green.

    One fixture with every branch in it: an SCC cycle, a downstream ghost whose
    `task_get` failed (an `unknown` edge and a `status_unknown` node), a node
    whose edge read failed (`edges_unknown`, and the chain's lower bound), a
    satisfied edge, and a hierarchy edge.
    """
    rows = [
        task("epic", task_type="epic"),
        task("cyc-a"),
        task("cyc-b"),
        task("broken"),
        task("open-task"),
        task("done", status="completed"),
        task("ghost", project="other", status="completed"),
    ]
    fake = GraphFakeClient(
        dataset(
            rows,
            [
                ("cyc-a", "cyc-b", "blocks"),
                ("cyc-b", "cyc-a", "blocks"),
                ("cyc-b", "ghost", "blocks"),
                ("done", "open-task", "blocks"),
                ("epic", "open-task", "parent_child"),
            ],
        ),
        edge_failures={"broken"},
        get_failures={"ghost"},
    )

    html = get(
        lithos_lens_config_env,
        fake,
        f"/tasks/graph?project={PROJECT}&include_resolved=1",
    )
    data = payload(html)

    assert set(data) == {
        "scope",
        "nodes",
        "edges",
        "layers",
        "cycles",
        "ghosts",
        "longest_chain",
        "roots",
        "isolated",
        "incomplete",
        "as_of",
    }
    # Shape and verdict are separate fields because they are separate facts:
    # the canvas groups on `cycle` and marks on `flagged`.
    verdicts = {node["id"]: (node["cycle"], node["flagged"]) for node in data["nodes"]}
    assert verdicts["cyc-a"] == ("cyc-a", False), "no blocked row flagged it"
    assert verdicts["open-task"] == ("", False)
    completeness = {node["id"]: node["completeness"] for node in data["nodes"]}
    assert completeness["broken"] == "edges_unknown"
    assert completeness["ghost"] == "status_unknown"
    assert completeness["cyc-a"] == "ok"
    assert data["ghosts"] == ["ghost"]
    assert data["incomplete"] == {"broken": "internal_error"}
    # Every dependency edge with its state, and a reason on the inactive one.
    states = {
        (edge["from"], edge["to"]): (edge["type"], edge["state"], edge["reason"])
        for edge in data["edges"]
    }
    assert states[("cyc-a", "cyc-b")] == ("blocks", "active", "")
    assert states[("done", "open-task")] == ("blocks", "inactive", "satisfied")
    assert states[("cyc-b", "ghost")] == ("blocks", "unknown", "")
    assert states[("epic", "open-task")] == ("parent_child", "", "")
    # The cycle Lens can draw, with the members and the walk the text names.
    assert [cycle["id"] for cycle in data["cycles"]] == ["cyc-a"]
    assert data["cycles"][0]["members"] == ["cyc-a", "cyc-b"]
    assert data["cycles"][0]["scc"] is True
    assert data["cycles"][0]["path"][0] == "cyc-a"
    # The chain, its bound, and the roots the canvas lays out from.
    assert data["longest_chain"]["bound"] == "lower_bound"
    assert data["longest_chain"]["length"] == len(data["longest_chain"]["nodes"])
    assert "cyc-a" in data["roots"]
    assert set(data["roots"]) <= {node["id"] for node in data["nodes"]}
    # And it still agrees with the text about where every node is rendered.
    assert {node["id"]: node["layer"] for node in data["nodes"]} == rendered_layers(
        html
    )


# ── The route's own shape: picker and refusal ───────────────────────────


def test_the_unscoped_route_offers_projects_and_open_epics(
    lithos_lens_config_env: Path,
) -> None:
    """§5B.1's universe: BOTH conventions, and the resolved window too.

    The picker is the page's only way in without a bookmark, so a project it
    omits has no graph at all. Three ways a project can be observed are in this
    fixture — a tag, ``metadata.project``, and a task that is neither open nor
    named by any open row — plus a task whose two conventions disagree, which
    §5B.1 says names BOTH projects rather than picking one.
    """
    tasks = [
        task("a"),
        task("b", project="other"),
        metadata_task("c", project="meta-only"),
        # Both conventions, disagreeing: the universe is the union.
        metadata_task("d", project="meta-side", extra_tags=("project:tag-side",)),
        # Observed ONLY through the resolved window.
        task("e", project="finished-last-week", status="completed"),
        task("epic", task_type="epic"),
        task("closed-epic", status="completed", task_type="epic"),
    ]
    fake = GraphFakeClient(dataset(tasks))

    html = get(lithos_lens_config_env, fake, "/tasks/graph")

    assert "data-graph-picker" in html
    assert re.findall(r'data-picker-project="([^"]+)"', html) == [
        "finished-last-week",
        "loom",
        "meta-only",
        "meta-side",
        "other",
        "tag-side",
    ]
    # The epic column stays OPEN epics only — a finished initiative is not a
    # scope anyone picks next.
    assert re.findall(r'data-picker-epic="([^"]+)"', html) == ["epic"]
    assert "data-graph-layers" not in html
    # And the resolved rows are read once each, through the bounded window.
    assert [call.get("status") for call in fake.list_calls] == [
        "open",
        "completed",
        "cancelled",
    ]


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
    assert 'data-refusal-reason="tasks"' in html
    assert "over the 3 this page will render" in re.sub(r"<[^>]+>", "", html)
    assert "data-graph-layers" not in html
    # Refused BEFORE the fan-out: the guard exists to prevent exactly that cost.
    assert fake.edge_calls == []


def test_a_classification_refusal_counts_reads_not_tasks(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """That refusal's count is out-of-set ENDPOINTS, so the panel must not
    present it as a node count over the node limit — at the default guard it
    would read "1 tasks, over the 300", which is false twice over."""
    monkeypatch.setattr(graph_scope, "MAX_GHOST_RESOLUTION_READS", 0)
    tasks = [task("a")]
    fake = GraphFakeClient(
        dataset(
            [*tasks, task("ghost", project="other", status="completed")],
            [("ghost", "a", "blocks")],
        )
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    text = re.sub(r"<[^>]+>", " ", html)

    assert 'data-refusal-reason="classification"' in html
    assert only_group(r"data-refusal-count>(\d+)<", html) == "1"
    assert "reads of tasks outside it" in text
    assert "tasks, over the" not in text
    assert "this page will render" not in text


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


def test_an_epic_child_keeps_the_claim_the_master_list_knows_about(
    lithos_lens_config_env: Path,
) -> None:
    """`task_children` carries no claims, and a silent blank is a wrong answer.

    The vendored `lithos_task_children` contract has no `claims` field, so an
    epic scope assembled from it alone renders a genuinely claimed child as
    unclaimed — the §5.7 row anatomy (status, type, claim) filled with a lie.
    The master open list, read `with_claims=True`, is the authority.
    """
    rows = [task("epic", task_type="epic"), task("child")]
    data = FakeLithosDataset(
        tasks=tuple(rows),
        edges=edge_index((("epic", "child", "parent_child"),)),
        children={"epic": ("child",)},
        claims={"child": (ClaimRecord(agent="worker-b", aspect="implementation"),)},
    )
    fake = GraphFakeClient(data)

    html = get(lithos_lens_config_env, fake, "/tasks/graph?epic=epic")

    assert "claimed by worker-b" in node_block(html, "child")


def test_a_flagged_cycle_member_with_no_fetched_edge_is_layered_not_folded(
    lithos_lens_config_env: Path,
) -> None:
    """D4 beats D8 when they collide: the authority is not folded away.

    Reachable, not hypothetical: an edge upsert emits no event (ledger gap #1),
    so a warm, successful, edge-EMPTY cache entry can be served for a task the
    uncached blocked read is calling cyclic. Folding it into a disclosure that
    is collapsed by default would hide a task Lithos says cannot run.
    """
    rows = [task("flagged"), task("plain")]
    fake = GraphFakeClient(
        dataset(
            rows,
            blocked={
                "flagged": cycle_blocker("elsewhere", "Dependency cycle: flagged -> ?.")
            },
        )
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    data = payload(html)

    assert "flagged" in layer_nodes(html, 0)
    assert "in-cycle" in markers(html, "flagged")
    # The genuinely edge-less task still folds; the flagged one no longer does,
    # and the payload says the same thing the text does.
    assert data["isolated"] == ["plain"]
    isolated = only_group(r"data-isolated-list>(.*?)</ol>", html)
    assert 'data-graph-node="flagged"' not in isolated


def test_a_downstream_unknown_ghost_is_not_blamed_on_its_predecessor(
    lithos_lens_config_env: Path,
) -> None:
    """An edge is unknown when EITHER endpoint is unreadable (D6).

    Here the predecessor is a known open in-scope task and the DEPENDENT is the
    ghost whose `task_get` failed, so "blocked by unresolvable predecessor"
    would invent a cause that does not exist.
    """
    fake = GraphFakeClient(
        dataset(
            [task("a"), task("ghost", project="other", status="completed")],
            [("a", "ghost", "blocks")],
        ),
        get_failures={"ghost"},
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    ghost_row = node_block(html, "ghost")

    assert 'data-status="unknown"' in ghost_row
    assert "unresolvable" not in markers(html, "ghost")
    assert "this predecessor's status could not be read" not in ghost_row
    assert "this task's own status could not be read" in ghost_row
    # The claim it DOES degrade is the chain, which is a lower bound either way.
    assert 'data-chain-bound="lower_bound"' in html


def test_a_task_whose_id_collides_with_the_graph_route_stays_reachable(
    lithos_lens_config_env: Path,
) -> None:
    """Task ids are arbitrary strings, so one can be called "graph".

    Starlette matches the static route first, so `/tasks/graph` is the graph
    page — and without the alias every link to that task would silently open
    the graph instead of its detail page, which is worse than a 404.
    """
    rows = [task("graph", title="Task named graph"), task("other")]
    fake = GraphFakeClient(dataset(rows))

    with client_for(lithos_lens_config_env, fake) as client:
        graph_page = client.get(f"/tasks/graph?project={PROJECT}")
        detail = client.get("/tasks/id?task_id=graph")

    assert "data-graph-layers" in graph_page.text
    assert 'data-node-detail href="/tasks/id?task_id=graph' in unescape(graph_page.text)
    assert detail.status_code == 200
    assert "Task named graph" in detail.text


@pytest.mark.parametrize(
    ("task_id", "expected"),
    [
        # A page word under /tasks/ …
        ("graph", "/tasks/id?task_id=graph"),
        # … and the alias route's own segment, which needs the alias too.
        ("id", "/tasks/id?task_id=id"),
        # Ordinary ids keep the documented path, reserved characters encoded.
        ("plain", "/tasks/plain"),
        ("task?42#x", "/tasks/task%3F42%23x"),
    ],
)
def test_a_detail_link_addresses_the_task_it_names(
    lithos_lens_config_env: Path, task_id: str, expected: str
) -> None:
    """One rule for every surface, and it must reach the task it NAMES."""
    assert task_detail_path(task_id) == expected

    rows = [
        replace(task("placeholder"), id=task_id, title=f"Task {task_id}"),
        task("graph", title="Task named graph"),
    ]
    fake = GraphFakeClient(dataset(rows))

    with client_for(lithos_lens_config_env, fake) as client:
        response = client.get(expected)

    assert response.status_code == 200
    assert f"Task {task_id}" in unescape(response.text)


def test_an_id_shaped_like_the_alias_is_never_served_as_a_DIFFERENT_task(
    lithos_lens_config_env: Path,
) -> None:
    """The alias must not become a second way to misroute.

    A path-shaped alias (``/tasks/id/<id>``) would be MATCHED by an id like
    ``id/graph``: ASGI decodes ``%2F`` into a separator before routing, so the
    link would serve the task called ``graph`` at HTTP 200 — a silently wrong
    entity. The alias is therefore a single static segment carrying the id in
    the query, and a slash-bearing id keeps the documented path, where it stays
    unroutable exactly as `tests/test_blocker_chain.py` records (Lithos
    b1a65c6d, closed won't-fix): this change does not overturn that decision as
    a side effect of fixing the page-word collision.
    """
    task_id = "id/graph"
    assert task_detail_path(task_id) == f"/tasks/{quote(task_id, safe='')}"

    fake = GraphFakeClient(dataset([task("graph", title="Task named graph")]))

    with client_for(lithos_lens_config_env, fake) as client:
        response = client.get(task_detail_path(task_id))

    assert response.status_code == 404
    assert "Task named graph" not in response.text


# ── D3's order, and the row anatomy it promises ─────────────────────────


def test_the_page_renders_D3_s_sections_in_order(
    lithos_lens_config_env: Path,
) -> None:
    """The order IS the requirement: a cycle that makes work unreachable — and
    any banner saying the cycle signal is partial — is read before the layers
    that work sits in, and the payload comes last.

    The fixture produces every section at once: an SCC callout, a cycle-signal
    banner (a downstream ghost in a second project whose reads fail), isolates,
    and a hierarchy.
    """
    rows = [
        task("epic", task_type="epic"),
        task("cyc-a"),
        task("cyc-b"),
        task("lonely"),
        task("ghost", project="other"),
    ]
    fake = GraphFakeClient(
        dataset(
            rows,
            [
                ("cyc-a", "cyc-b", "blocks"),
                ("cyc-b", "cyc-a", "blocks"),
                ("cyc-b", "ghost", "blocks"),
                ("epic", "cyc-a", "parent_child"),
            ],
            blocked={"cyc-a": cycle_blocker("cyc-b", "Dependency cycle.")},
        ),
        blocked_failures={"other", "project:other"},
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    assert 'data-graph-banner="cycle-unavailable"' in html, "no banner to order"
    assert section_order(html) == [
        "callout",
        "banner",
        "legend",
        "chain",
        "layers",
        "isolated",
        "hierarchy",
        "payload",
    ]


def test_the_hierarchy_section_is_rendered_even_with_no_parent_child_edges(
    lithos_lens_config_env: Path,
) -> None:
    """ "Always rendered" is the requirement, so the empty case is the test."""
    fake = GraphFakeClient(dataset([task("a"), task("b")], [("a", "b", "blocks")]))

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")
    empty = get(lithos_lens_config_env, fake, "/tasks/graph?project=nothing-here")

    # No hierarchy edges: a forest of roots, every node still listed.
    assert "data-hierarchy" in html
    assert re.findall(r'data-hierarchy-node="([^"]+)"', html) == ["a", "b"]
    assert set(re.findall(r'data-hierarchy-node="[^"]+" data-depth="(\d+)"', html)) == {
        "0"
    }
    # And with nothing in the scope at all, the section states that rather
    # than disappearing.
    assert "data-hierarchy" in empty
    assert "data-hierarchy-empty" in empty
    assert "No parent/child edges in this scope." in empty
    assert section_order(empty)[-2:] == ["hierarchy", "payload"]


def test_every_rendered_row_carries_its_status_and_type(
    lithos_lens_config_env: Path,
) -> None:
    """The row anatomy §5.7 requires, checked on EVERY row rather than one."""
    rows = [
        task("epic", task_type="epic"),
        task("gate", task_type="gate"),
        task("open-task"),
        task("done", status="completed"),
        task("lonely"),
    ]
    fake = GraphFakeClient(
        dataset(
            rows,
            [
                ("gate", "open-task", "waits_on_gate"),
                ("done", "open-task", "blocks"),
                ("epic", "open-task", "parent_child"),
            ],
        )
    )

    html = get(
        lithos_lens_config_env,
        fake,
        f"/tasks/graph?project={PROJECT}&include_resolved=1",
    )

    expected = {row.id: (row.status, row.task_type) for row in rows}
    for node in payload(html)["nodes"]:
        block = node_block(html, node["id"])
        status, task_type = expected[node["id"]]
        assert f'data-status="{status}"' in block, node["id"]
        assert f'data-task-type="{task_type}"' in block, node["id"]
        assert f">{status}</span>" in block, node["id"]
        assert f">{task_type}</span>" in block, node["id"]


def test_as_of_is_the_fetch_time_and_the_visible_line_agrees_with_the_payload(
    lithos_lens_config_env: Path,
) -> None:
    """`as_of` is the staleness bound, so it must be a FETCH time.

    A render-time clock would read "now" on every reload of a cache that has
    not been re-read, which is the reassurance the TTL cannot give: edge
    upserts emit no event, so the page's honesty rests on this line.
    """
    fake = GraphFakeClient(dataset([task("a"), task("b")], [("a", "b", "blocks")]))

    with client_for(lithos_lens_config_env, fake) as client:
        first = unescape(client.get(f"/tasks/graph?project={PROJECT}").text)
        second = unescape(client.get(f"/tasks/graph?project={PROJECT}").text)

    rendered = only_group(r'data-graph-as-of>as of <time datetime="([^"]+)"', first)
    assert rendered == payload(first)["as_of"]
    # Second render, warm cache, no new reads — so the same fetch time.
    assert payload(second)["as_of"] == payload(first)["as_of"]
    assert fake.edge_calls == ["a", "b"]


async def test_as_of_is_the_oldest_contributing_fetch_not_the_newest() -> None:
    """With a mixed-age cache the page reports the OLDEST entry (D2).

    Driven through the cache with an injectable clock rather than the route,
    because "oldest" only means something once two entries differ in age and a
    render cannot make that happen on its own.
    """
    clock = StepClock()
    cache = GraphCache(clock=clock)
    rows = [task("a"), task("b")]
    fake = GraphFakeClient(dataset(rows, [("a", "b", "blocks")]))
    warm = await load_project_scope(
        fake, project=PROJECT, master=[rows[0]], cache=cache
    )
    clock.advance(minutes=5)
    scope = await load_project_scope(fake, project=PROJECT, master=rows, cache=cache)

    view = build_graph_page(scope, CycleSignal(), params=parse_graph_params({}))

    assert warm.as_of == clock.start
    assert view.as_of == clock.start, "a newer entry moved the staleness bound"
    assert scope.cache_hits == 1 and scope.cache_misses == 1


async def test_two_overlapping_scopes_each_report_their_own_fan_out() -> None:
    """The counts are per RENDER, not a slice of the shared cache's totals.

    The cache is process-wide and its counters are cumulative, so subtracting
    them around a call folds a concurrent page's reads into this one's — two
    overlapping graph pages would each claim the other's fan-out and the
    ledger-gap-#3 evidence would be junk. The invariant that catches it: every
    scope accounts for exactly its own node count, once each.
    """
    rows = [task("a"), task("b"), task("c")]
    gate = asyncio.Event()
    fake = GraphFakeClient(
        dataset(rows, [("a", "b", "blocks"), ("b", "c", "blocks")]), edge_gate=gate
    )
    cache = GraphCache(clock=StepClock())

    small = asyncio.create_task(
        load_project_scope(fake, project=PROJECT, master=rows[:2], cache=cache)
    )
    whole = asyncio.create_task(
        load_project_scope(fake, project=PROJECT, master=rows, cache=cache)
    )
    await asyncio.sleep(0)
    gate.set()
    left, right = await asyncio.gather(small, whole)

    in_scope = [sum(1 for node in s.nodes if not node.ghost) for s in (left, right)]
    assert in_scope == [2, 3], "the fixture's two scopes overlap on a and b"
    assert left.cache_hits + left.cache_misses == 2
    assert right.cache_hits + right.cache_misses == 3
    # Single-flight still holds: three tasks, three upstream reads between them.
    assert left.cache_misses + right.cache_misses == 3
    assert sorted(fake.edge_calls) == ["a", "b", "c"]


# ── Empty and failing scopes ────────────────────────────────────────────


def test_a_scope_with_no_tasks_renders_an_empty_page_not_an_error(
    lithos_lens_config_env: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    """A real answer: the scope exists and holds nothing to draw."""
    fake = GraphFakeClient(dataset([task("a")]))

    html = get(lithos_lens_config_env, fake, "/tasks/graph?project=empty-project")

    assert "data-graph-empty" in html
    assert "Nothing to draw in this scope." in html
    assert 'data-graph-node="a"' not in html
    assert fake.blocked_calls == []
    assert (
        metric_value(
            metric_reader,
            "lens_tasks_graph_renders_total",
            scope="project",
            outcome="rendered",
        ).value
        == 1
    )


def test_a_failed_master_read_renders_the_error_panel(
    lithos_lens_config_env: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    """The read before the scope fails too, and it is not a 500."""
    fake = GraphFakeClient(dataset([task("a")]), list_failures={"open"})

    with client_for(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks/graph?project={PROJECT}")

    assert response.status_code == 200
    assert "data-graph-error" in response.text
    assert "Task data is unavailable" in response.text
    assert "data-graph-layers" not in response.text
    assert (
        metric_value(
            metric_reader,
            "lens_tasks_graph_renders_total",
            scope="project",
            outcome="error",
        ).value
        == 1
    )


def test_a_failed_scope_assembly_renders_the_error_panel(
    lithos_lens_config_env: Path,
) -> None:
    """An epic scope reads its anchor and children up front; either can fail."""
    fake = GraphFakeClient(
        dataset([task("epic", task_type="epic")], children={"epic": ()}),
        children_failures={"epic"},
    )

    with client_for(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/graph?epic=epic")

    assert response.status_code == 200
    assert "This scope could not be loaded from Lithos." in response.text
    assert "data-graph-layers" not in response.text


# ── Telemetry (`lens.tasks.graph`) ──────────────────────────────────────


def graph_span(spans: InMemorySpanExporter) -> Any:
    """The LAST `lens.tasks.graph` span — one per graph render, by name."""
    matching = [span for span in spans.get_finished_spans() if span.name == GRAPH_SPAN]
    assert matching, [span.name for span in spans.get_finished_spans()]
    return matching[-1]


def test_the_render_records_its_whole_shape_on_the_assembly_span(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    """The PRD's telemetry point, in full: a named span carrying this page's
    own counts, and a counter whose labels are a bounded set."""
    rows = [
        task("epic", task_type="epic"),
        task("cyc-a"),
        task("cyc-b"),
        task("lonely"),
        task("ghost", project="other"),
    ]
    fake = GraphFakeClient(
        dataset(
            rows,
            [
                ("cyc-a", "cyc-b", "blocks"),
                ("cyc-b", "cyc-a", "blocks"),
                ("cyc-b", "ghost", "blocks"),
                ("epic", "cyc-a", "parent_child"),
            ],
            blocked={"cyc-a": cycle_blocker("cyc-b", "Dependency cycle.")},
        )
    )

    get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    attributes = dict(graph_span(spans).attributes or {})
    assert attributes == {
        "lens.graph.scope_kind": "project",
        "lens.graph.scope_key": PROJECT,
        "lens.graph.outcome": "rendered",
        "lens.graph.include_resolved": False,
        "lens.graph.nodes": 5,
        "lens.graph.edges": 4,
        "lens.graph.ghosts": 1,
        "lens.graph.cycles": 1,
        # The epic and `lonely` both hang off hierarchy edges only, which is
        # what the disclosure folds away.
        "lens.graph.isolated": 2,
        "lens.graph.chain_length": 2,
        "lens.graph.chain_exact": True,
        # Four in-scope tasks read cold, no ghost read (an OPEN far endpoint is
        # already on the master list), nothing warm.
        "lens.graph.cache_hits": 0,
        "lens.graph.cache_misses": 4,
        "lens.graph.ghost_reads": 0,
        "lens.graph.fanout": 4,
        "lens.graph.cycle_signal_incomplete": False,
    }
    assert (
        metric_value(
            metric_reader,
            "lens_tasks_graph_renders_total",
            scope="project",
            outcome="rendered",
        ).value
        == 1
    )
    # Two projects in the coverage set (the ghost's counts), two reads each.
    assert (
        metric_value(
            metric_reader, "lens_tasks_graph_cycle_reads_total", outcome="ok"
        ).value
        == 4
    )


def test_a_partial_cycle_signal_is_visible_in_the_telemetry(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three read outcomes on one page, and the span says the cycle claim
    is incomplete — which is the signal an operator needs before believing an
    absent marker."""
    limit = 2
    monkeypatch.setenv("LITHOS_LENS_TASKS_FRONTIER_LIMIT", str(limit))
    fake = GraphFakeClient(
        # The downstream ghost puts a SECOND project in the coverage set, and
        # both of its reads fail.
        dataset(
            [task("a"), task("ghost", project="other")],
            [("a", "ghost", "blocks")],
        ),
        blocked_rows={
            PROJECT: [
                BlockedTaskRecord(task=task(f"filler-{index}"), blockers=())
                for index in range(limit)
            ]
        },
        blocked_failures={"other", "project:other"},
    )

    html = get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    attributes = dict(graph_span(spans).attributes or {})
    assert attributes["lens.graph.cycle_signal_incomplete"] is True
    assert attributes["lens.graph.outcome"] == "rendered"
    assert 'data-graph-banner="cycle-truncated"' in html
    assert 'data-graph-banner="cycle-unavailable"' in html
    for outcome, expected in (("ok", 1), ("truncated", 1), ("failed", 2)):
        assert (
            metric_value(
                metric_reader, "lens_tasks_graph_cycle_reads_total", outcome=outcome
            ).value
            == expected
        ), outcome


def test_a_refusal_records_its_reason_and_count(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused is an outcome, not an error: how often the guard bites is how
    operators find out it is set wrong."""
    monkeypatch.setenv("LITHOS_LENS_GRAPH_MAX_TASKS", "2")
    fake = GraphFakeClient(dataset([task(f"t{index}") for index in range(3)]))

    get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    attributes = dict(graph_span(spans).attributes or {})
    assert attributes["lens.graph.outcome"] == "refused"
    assert attributes["lens.graph.refusal_reason"] == "tasks"
    assert attributes["lens.graph.refusal_count"] == 3
    assert (
        metric_value(
            metric_reader,
            "lens_tasks_graph_renders_total",
            scope="project",
            outcome="refused",
        ).value
        == 1
    )


def test_a_refusal_after_the_fan_out_reports_what_it_spent(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three of the four refusals happen AFTER the edge reads.

    Reporting zero there would tell the operator that a page which spent a
    hundred reads was free — backwards for the guard whose own telemetry is
    the evidence for tuning it, and for the ledger-gap-#3 fan-out figure.
    """
    monkeypatch.setattr(graph_scope, "MAX_GHOST_RESOLUTION_READS", 0)
    fake = GraphFakeClient(
        dataset(
            [task("a"), task("ghost", project="other", status="completed")],
            [("ghost", "a", "blocks")],
        )
    )

    get(lithos_lens_config_env, fake, f"/tasks/graph?project={PROJECT}")

    attributes = dict(graph_span(spans).attributes or {})
    assert attributes["lens.graph.outcome"] == "refused"
    assert attributes["lens.graph.refusal_reason"] == "classification"
    # One in-scope node was read cold before the refusal was even decidable.
    assert fake.edge_calls == ["a"]
    assert attributes["lens.graph.cache_misses"] == 1
    assert attributes["lens.graph.fanout"] == 1
    # And the ghost read the budget refused was never issued.
    assert attributes["lens.graph.ghost_reads"] == 0
    assert fake.get_calls == []


def test_the_picker_and_an_offline_page_carry_their_own_outcomes(
    lithos_lens_config_env: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    fake = GraphFakeClient(dataset([task("a")]))
    offline = GraphFakeClient(dataset([task("a")]), health="unreachable")

    get(lithos_lens_config_env, fake, "/tasks/graph")
    get(lithos_lens_config_env, offline, f"/tasks/graph?project={PROJECT}")

    for scope, outcome in (("none", "picker"), ("project", "offline")):
        assert (
            metric_value(
                metric_reader,
                "lens_tasks_graph_renders_total",
                scope=scope,
                outcome=outcome,
            ).value
            == 1
        ), outcome
