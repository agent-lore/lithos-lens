"""A scoped board shows the gates its in-scope tasks wait on (``gate_scope``).

Observed live (2026-10-09): ``/tasks?tag=roadmap-2026-10`` read "No open gates
match these filters" while tasks in scope were blocked by open gates, because
gates do not carry story tags — loom's ``pr`` gates carry none, its ``human``
gates only ``project:<slug>`` and ``needs-human``. The rule (Dave, 2026-10-09):
an open gate is in scope when it passes the filters itself OR a task in scope
waits on it — on every gate surface of the board (the Gates section, the
Needs-attention promotion, the gates tile, the project strip), under every
scope filter, with no extra Lithos call on a healthy render.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.frontier import load_dashboard
from lithos_lens.gate_scope import GATES_INCOMPLETE_ERROR
from lithos_lens.gates import GATE_WAITER_FANOUT_CAP
from lithos_lens.task_graph import BlockedTaskRecord, BlockerRecord, EdgeRecord
from lithos_lens.tasks import TaskRecord
from lithos_lens.web import create_app
from tests.test_frontier import (
    _FILTERS,
    _NOW,
    _blocked,
    _FrontierFake,
    _gate_row,
    _gate_task,
    _section_ids,
    _task,
)
from tests.test_tasks_mvp import (
    TaskFakeLithosClient,
    _add_gate,
    _ago,
    _client,
    _waits_on,
)

SINCE = "since=2026-04-01"

# One healthy board render's Lithos reads: the master open list and the two
# resolved windows, the two frontiers, stats and the agent list. Nothing else.
HEALTHY_RENDER_CALLS = Counter(
    {
        "list_tasks": 3,
        "task_ready": 1,
        "task_blocked": 1,
        "stats": 1,
        "list_agents": 1,
    }
)


def _story(
    fake: TaskFakeLithosClient,
    task_id: str,
    *tags: str,
    created_by: str = "planner",
) -> None:
    fake.tasks.append(
        TaskRecord(
            id=task_id,
            title=f"Story {task_id}",
            status="open",
            created_by=created_by,
            created_at=_ago(minutes=30),
            tags=tags,
        )
    )


def _group(body: str, name: str) -> str:
    """One board section's markup, up to the next section."""
    start = body.index(f'data-task-group="{name}"')
    end = body.find("data-task-group=", start + 1)
    return body[start : end if end != -1 else len(body)]


def _gate_ids(body: str) -> list[str]:
    return re.findall(r'data-gate-row data-task-id="([^"]+)"', _group(body, "gates"))


def _project_count(body: str, slug: str) -> int | None:
    match = re.search(
        rf'data-project-chip="{re.escape(slug)}".*?data-project-open-count>(\d+)<',
        body,
        flags=re.S,
    )
    return int(match.group(1)) if match else None


class _CallLoggingFake(TaskFakeLithosClient):
    """Logs EVERY coroutine method the app awaits on the client, by name.

    Not the per-tool logs the base fake happens to keep: a read nobody thought
    to instrument (a second ``task_blocked``, say) must show up here too.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def __getattribute__(self, name: str) -> Any:
        attr = super().__getattribute__(name)
        if name.startswith("_") or not inspect.iscoroutinefunction(attr):
            return attr
        calls = super().__getattribute__("calls")

        async def logged(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return await attr(*args, **kwargs)

        return logged


def _roadmap_gate_fake() -> _CallLoggingFake:
    """An untagged ``pr`` gate (``metadata.project`` only, as loom writes it)
    holding a ``roadmap-x`` story, beside a gate that holds only an
    out-of-scope task."""
    fake = _CallLoggingFake()
    _add_gate(fake, "gate-pr", gate_type="pr", metadata={"project": "lithos-lens"})
    _story(fake, "story-1", "roadmap-x", "project:lithos-lens")
    _waits_on(fake, "story-1", "gate-pr")
    # A second project in scope, so the strip renders at all.
    _story(fake, "story-loom", "roadmap-x", "project:lithos-loom")
    fake.ready_ids.add("story-loom")
    _add_gate(fake, "gate-elsewhere", gate_type="ci")
    _story(fake, "story-off", "roadmap-y", "project:lithos-lens")
    _waits_on(fake, "story-off", "gate-elsewhere")
    return fake


def test_untagged_gate_an_in_scope_task_waits_on_is_on_the_tag_scoped_board(
    lithos_lens_config_env: Path,
) -> None:
    """The live repro: under ``?tag=roadmap-x`` the gate the story waits on is
    in the Gates section, on the gates tile and in its project's strip count —
    and the render reads exactly what the unfiltered one does."""
    fake = _roadmap_gate_fake()

    with _client(lithos_lens_config_env, fake) as client:
        before = len(fake.calls)
        client.get(f"/tasks?status=open&{SINCE}")
        unfiltered_cost = Counter(fake.calls[before:])
        before = len(fake.calls)
        response = client.get(f"/tasks?status=open&tag=roadmap-x&{SINCE}")
        filtered_cost = Counter(fake.calls[before:])

    assert response.status_code == 200
    body = response.text
    assert _gate_ids(body) == ["gate-pr"]
    assert "<strong data-gate-count>1</strong>" in body
    assert "No open gates match these filters." not in body
    # The story and its gate: both are open rows of lithos-lens on this board.
    assert _project_count(body, "lithos-lens") == 2
    assert _project_count(body, "lithos-loom") == 1
    # No extra Lithos call: the blocked frontier already named the gate. The
    # board's whole read budget, every awaited client method counted — and the
    # scoped render spends exactly what the unscoped one does.
    assert filtered_cost == HEALTHY_RENDER_CALLS
    assert unfiltered_cost == HEALTHY_RENDER_CALLS
    assert fake.edge_list_calls == []
    assert "data-gates-incomplete" not in body


def test_a_gate_holding_only_out_of_scope_tasks_stays_off_the_scoped_board(
    lithos_lens_config_env: Path,
) -> None:
    fake = _roadmap_gate_fake()

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get(f"/tasks?status=open&tag=roadmap-x&{SINCE}").text
        wide = client.get(f"/tasks?status=open&{SINCE}").text

    assert "gate-elsewhere" not in _gate_ids(body)
    assert sorted(_gate_ids(wide)) == ["gate-elsewhere", "gate-pr"]


def test_a_gate_carrying_the_tag_itself_still_appears(
    lithos_lens_config_env: Path,
) -> None:
    """Today's rule is untouched: a gate that matches on its own shows even
    when nothing in scope waits on it."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-tagged", tags=("roadmap-x",))

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get(f"/tasks?status=open&tag=roadmap-x&{SINCE}").text

    assert _gate_ids(body) == ["gate-tagged"]


def test_needs_attention_promotes_a_waited_on_gate_under_the_filter(
    lithos_lens_config_env: Path,
) -> None:
    """A human gate past its threshold is promoted unfiltered; under the tag it
    must be promoted too — not dropped from both surfaces."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-late", created_at=_ago(days=3), tags=("needs-human",))
    _story(fake, "story-1", "roadmap-x")
    _waits_on(fake, "story-1", "gate-late")

    with _client(lithos_lens_config_env, fake) as client:
        wide = client.get(f"/tasks?status=open&{SINCE}").text
        body = client.get(f"/tasks?status=open&tag=roadmap-x&{SINCE}").text

    for page in (wide, body):
        attention = _group(page, "attention")
        assert 'data-task-id="gate-late"' in attention
        assert 'data-attention-rule="gate-waiting"' in attention
        # Single placement: promoted, so not also in Gates.
        assert _gate_ids(page) == []


def test_the_rule_holds_under_project_agent_and_epic_scope(
    lithos_lens_config_env: Path,
) -> None:
    """Every filter that narrows the visible set, not just ``tag=``: the
    waiter is in scope and the gate is not."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-1")
    _story(fake, "story-1", "project:lithos-lens", created_by="worker-z")
    _waits_on(fake, "story-1", "gate-1")
    fake.tasks.append(
        TaskRecord(
            id="epic-1",
            title="Roadmap epic",
            status="open",
            created_by="planner",
            created_at=_ago(hours=2),
            task_type="epic",
        )
    )
    fake.children["epic-1"] = ["story-1"]

    with _client(lithos_lens_config_env, fake) as client:
        pages = {
            query: client.get(f"/tasks?status=open&{query}&{SINCE}").text
            for query in ("project=lithos-lens", "agent=worker-z", "epic=epic-1")
        }

    for query, body in pages.items():
        assert 'data-task-id="story-1"' in body, query
        assert _gate_ids(body) == ["gate-1"], query
        assert "<strong data-gate-count>1</strong>" in body, query


def test_the_waited_on_gates_waiter_count_is_not_narrowed(
    lithos_lens_config_env: Path,
) -> None:
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-1")
    _story(fake, "story-in", "roadmap-x")
    _story(fake, "story-out-1", "roadmap-y")
    _story(fake, "story-out-2")
    for waiter in ("story-in", "story-out-1", "story-out-2"):
        _waits_on(fake, waiter, "gate-1")

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get(f"/tasks?status=open&tag=roadmap-x&{SINCE}").text

    assert _gate_ids(body) == ["gate-1"]
    assert "blocks 3 tasks" in _group(body, "gates")


def test_blocked_by_chips_link_to_the_gate_and_the_task_they_name(
    lithos_lens_config_env: Path,
) -> None:
    """The chip keeps its label and short id and becomes a plain link to the
    blocker's detail page — no JavaScript needed to follow it."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-1", title="Approve rollout")
    _story(fake, "story-1", "roadmap-x")
    _waits_on(fake, "story-1", "gate-1")
    fake.blocked["story-1"] += (
        BlockerRecord(
            kind="task",
            task_id="open-claimed",
            type="blocks",
            status="open",
            message="Waiting on predecessor open-claimed to complete.",
        ),
    )

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get(f"/tasks?status=open&{SINCE}").text

    chips = _group(body, "blocked")
    gate_chip = re.search(
        r'<a class="blocker-chip blocker-chip-gate" data-blocker-kind="gate" '
        r'href="([^"]+)">Approve rollout (<code[^>]*>[^<]*</code>)</a>',
        chips,
    )
    assert gate_chip is not None
    assert gate_chip.group(1).startswith("/tasks/gate-1")
    assert 'title="gate-1"' in gate_chip.group(2)
    task_chip = re.search(
        r'<a class="blocker-chip blocker-chip-task" data-blocker-kind="task" '
        r'href="([^"]+)">Claimed open task <code',
        chips,
    )
    assert task_chip is not None
    assert task_chip.group(1).startswith("/tasks/open-claimed")
    # The same URL the board uses for that task's own row.
    assert f'<a class="task-title" href="{task_chip.group(1)}">' in body


class _OutageFake(TaskFakeLithosClient):
    """The blocked frontier and every gate edge read fail."""

    async def task_blocked(self, **_: Any) -> list[BlockedTaskRecord]:
        raise RuntimeError("blocked frontier unavailable")

    async def task_edge_list(self, task_id: str, **kwargs: Any) -> list[EdgeRecord]:
        await super().task_edge_list(task_id, **kwargs)
        raise RuntimeError("edge read failed")


def test_a_board_that_cannot_verify_its_gates_says_so(
    lithos_lens_config_env: Path,
) -> None:
    """Neither source answered which gates the story waits on: the scoped board
    states the Gates list may be incomplete instead of a confident empty line."""
    fake = _OutageFake()
    _add_gate(fake, "gate-1")
    _story(fake, "story-1", "roadmap-x")

    with _client(lithos_lens_config_env, fake) as client:
        body = client.get(f"/tasks?status=open&tag=roadmap-x&{SINCE}").text

    assert "data-gates-incomplete" in _group(body, "gates")
    assert "No open gates match these filters." not in body
    assert GATES_INCOMPLETE_ERROR in body
    assert [call["task_id"] for call in fake.edge_list_calls] == ["gate-1"]


def test_truncated_blocked_read_finds_the_waiter_through_the_gate_edges(
    lithos_lens_config_env: Path,
) -> None:
    """The waiter's row fell past ``frontier_limit``: the gate's own edge read
    brings the gate into scope, once — the section reuses the same answer."""
    fake = TaskFakeLithosClient()
    _add_gate(fake, "gate-1")
    _story(fake, "filler")
    _story(fake, "story-1", "roadmap-x")
    fake.blocked["filler"] = (BlockerRecord(kind="task", task_id="open-old"),)
    _waits_on(fake, "story-1", "gate-1")
    fake.edges["gate-1"] = [
        EdgeRecord(
            from_task_id="gate-1",
            to_task_id="story-1",
            type="waits_on_gate",
            direction="outgoing",
        )
    ]
    config = load_config(lithos_lens_config_env)
    config = replace(config, tasks=replace(config.tasks, frontier_limit=1))
    app = create_app(config, lithos_client_factory=lambda _: fake)

    with TestClient(app) as client:
        body = client.get(f"/tasks?status=open&tag=roadmap-x&{SINCE}").text

    assert _gate_ids(body) == ["gate-1"]
    assert "blocks 1 task (unverified)" in _group(body, "gates")
    assert "data-gates-incomplete" not in body
    assert [call["task_id"] for call in fake.edge_list_calls] == ["gate-1"]


_ROADMAP = replace(_FILTERS, statuses=("open",), tags=("roadmap-x",))


def test_failed_blocked_read_finds_the_waiter_through_the_gate_edges() -> None:
    gate = _gate_task("gate-1")
    story = _task("story-1", claims=(), tags=("roadmap-x",))
    fake = _FrontierFake(
        open_tasks=[gate, story],
        ready=[],
        blocked=[],
        blocked_error=RuntimeError("blocked frontier unavailable"),
        edges={
            "gate-1": [
                EdgeRecord(
                    from_task_id="gate-1", to_task_id="story-1", type="waits_on_gate"
                )
            ]
        },
    )
    data = asyncio.run(
        load_dashboard(fake, filters=_ROADMAP, frontier_limit=500, now=_NOW)
    )

    assert data.open_flat is True
    assert [row.task.id for row in data.gates] == ["gate-1"]
    assert data.summary.gates == 1
    assert data.gates_incomplete is False
    assert len(fake.edge_list_calls) == 1


def test_degraded_gate_reads_stay_under_one_cap_and_the_gap_is_stated() -> None:
    """More candidate gates than ``GATE_WAITER_FANOUT_CAP``: the render reads
    no more than the cap in total (scope and section together), and the gates
    it could not read make the board say its list may be incomplete."""
    gates = [_gate_task(f"gate-{index:03d}") for index in range(40)]
    story = _task("story-1", claims=(), tags=("roadmap-x",))
    filler = _task("filler", claims=())
    fake = _FrontierFake(
        open_tasks=[*gates, story, filler],
        ready=[],
        blocked=[_blocked(filler, BlockerRecord(kind="task", task_id="elsewhere"))],
        edges={
            "gate-000": [
                EdgeRecord(
                    from_task_id="gate-000", to_task_id="story-1", type="waits_on_gate"
                )
            ]
        },
    )
    data = asyncio.run(
        load_dashboard(fake, filters=_ROADMAP, frontier_limit=1, now=_NOW)
    )

    assert len(fake.edge_list_calls) == GATE_WAITER_FANOUT_CAP
    assert [row.task.id for row in data.gates] == ["gate-000"]
    # Reused, not read twice.
    assert [call["task_id"] for call in fake.edge_list_calls].count("gate-000") == 1
    assert data.gates_incomplete is True
    assert GATES_INCOMPLETE_ERROR in data.errors


def test_a_truncated_read_that_still_names_the_waiter_needs_no_scope_read() -> None:
    """The waiter's row survived the truncated response, so the gate is in
    scope already; the only edge read is the section's own waiter count."""
    gate = _gate_task("gate-1")
    story = _task("story-1", claims=(), tags=("roadmap-x",))
    fake = _FrontierFake(
        open_tasks=[gate, story],
        ready=[],
        blocked=[
            _blocked(
                story,
                BlockerRecord(kind="gate", task_id="gate-1", type="waits_on_gate"),
            )
        ],
    )
    data = asyncio.run(
        load_dashboard(fake, filters=_ROADMAP, frontier_limit=1, now=_NOW)
    )

    assert [row.task.id for row in data.gates] == ["gate-1"]
    assert [call["task_id"] for call in fake.edge_list_calls] == ["gate-1"]
    assert data.gates_incomplete is False


def test_project_strip_counts_a_waited_on_gate_under_its_waiters_projects() -> None:
    """A chip's count is the open rows following it would show. Following
    ``lithos-lens`` keeps BOTH gates — its waiter is lens work, whatever project
    each gate names — so both count there; ``?project=lithos-loom`` would keep
    neither (no loom waiter), so there is no loom chip at all."""
    lens_gate = _gate_task("gate-lens", metadata={"project": "lithos-lens"})
    loom_gate = _gate_task("gate-loom", metadata={"project": "lithos-loom"})
    story = _task("story-1", claims=(), tags=("roadmap-x", "project:lithos-lens"))
    fake = _FrontierFake(
        open_tasks=[lens_gate, loom_gate, story],
        ready=[],
        blocked=[
            _blocked(
                story,
                BlockerRecord(kind="gate", task_id="gate-lens", type="waits_on_gate"),
                BlockerRecord(kind="gate", task_id="gate-loom", type="waits_on_gate"),
            )
        ],
    )
    data = asyncio.run(
        load_dashboard(fake, filters=_ROADMAP, frontier_limit=500, now=_NOW)
    )

    assert sorted(row.task.id for row in data.gates) == ["gate-lens", "gate-loom"]
    assert [(chip.slug, chip.open_count) for chip in data.project_chips] == [
        ("lithos-lens", 3)
    ]
    followed = asyncio.run(
        load_dashboard(
            fake,
            filters=replace(_ROADMAP, projects=("lithos-lens",)),
            frontier_limit=500,
            now=_NOW,
        )
    )
    assert sorted(row.task.id for row in followed.gates) == ["gate-lens", "gate-loom"]
    assert _section_ids(followed.sections, "blocked") == ["story-1"]
    assert _gate_row(followed, "gate-lens").waiters_label == "blocks 1 task"
    # The count the chip promised is what following it shows: story + 2 gates.
    assert len(followed.gates) + len(followed.sections["blocked"]) == 3
    assert [(chip.slug, chip.open_count) for chip in followed.project_chips] == [
        ("lithos-lens", 3)
    ]


def test_a_truncated_read_feeds_the_strip_the_same_waiters_as_the_section() -> None:
    """Reviewer repro (correctness f-002): the truncated blocked response names
    only A as G's waiter; G's edge read names A and B. The strip must count G
    under B's project too — on both ``?project=a`` and ``?project=b``, since
    its scope excludes ``project`` — from the SAME read the Gates row uses,
    which is made once."""
    gate = _gate_task("gate-g", tags=("project:a", "project:b"))
    a = _task("task-a", claims=(), tags=("roadmap-x", "project:a"))
    b = _task("task-b", claims=(), tags=("roadmap-x", "project:b"))
    waits = BlockerRecord(kind="gate", task_id="gate-g", type="waits_on_gate")
    for project in ("a", "b"):
        fake = _FrontierFake(
            open_tasks=[gate, a, b],
            ready=[],
            # frontier_limit=1 cuts this to A's row: truncated.
            blocked=[_blocked(a, waits), _blocked(b, waits)],
            edges={
                "gate-g": [
                    EdgeRecord(
                        from_task_id="gate-g", to_task_id=waiter, type="waits_on_gate"
                    )
                    for waiter in ("task-a", "task-b")
                ]
            },
        )
        data = asyncio.run(
            load_dashboard(
                fake,
                filters=replace(_ROADMAP, projects=(project,)),
                frontier_limit=1,
                now=_NOW,
            )
        )

        assert [row.task.id for row in data.gates] == ["gate-g"], project
        assert [w.id for w in _gate_row(data, "gate-g").waiters] == [
            "task-a",
            "task-b",
        ]
        assert [(chip.slug, chip.open_count) for chip in data.project_chips] == [
            ("a", 2),
            ("b", 2),
        ], project
        assert data.gates_incomplete is False
        assert [call["task_id"] for call in fake.edge_list_calls] == ["gate-g"]


def test_a_gate_matching_on_its_own_also_counts_under_its_waiters_projects() -> None:
    """Reviewer repro (round 2, correctness f-001): G carries ``roadmap-x``
    itself, so it is on the board by its own match — but ``?project=lens`` keeps
    it too, through its lens waiter. The lens chip must count it (story + gate
    = 2), as well as G's own project; a gate naming no project counts under its
    waiter's alone. Each project once, however many routes reach it."""
    story = _task("story-1", claims=(), tags=("roadmap-x", "project:lens"))
    waits = BlockerRecord(kind="gate", task_id="gate-g", type="waits_on_gate")
    for metadata, expected in (
        ({"project": "other"}, [("lens", 2), ("other", 1)]),
        ({}, [("lens", 2)]),
        ({"project": "lens"}, [("lens", 2)]),
    ):
        gate = _gate_task("gate-g", tags=("roadmap-x",), metadata=metadata)
        fake = _FrontierFake(
            open_tasks=[gate, story], ready=[], blocked=[_blocked(story, waits)]
        )
        for projects in ((), ("lens",)):
            data = asyncio.run(
                load_dashboard(
                    fake,
                    filters=replace(_ROADMAP, projects=projects),
                    frontier_limit=500,
                    now=_NOW,
                )
            )
            assert [row.task.id for row in data.gates] == ["gate-g"]
            counts = [(chip.slug, chip.open_count) for chip in data.project_chips]
            assert counts == expected, (metadata, projects)
