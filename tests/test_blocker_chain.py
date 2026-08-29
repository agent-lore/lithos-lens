"""T1 slice 8 — lazy per-level expansion of the blocker chain.

The detail page renders level 1 eagerly (T1-S7). These tests pin what happens
BELOW it: an unfinished blocker line carries an HTMX expander that loads its
own blockers one level deeper, the walk stops at ``BLOCKER_MAX_DEPTH``, and a
cycle renders §5.5.2's callout instead of recursing.

They also pin the bound that depth alone does NOT give. Each expansion is a
fresh ``lithos_task_get`` fan-out over an agent-written edge set, so a level
resolved without a count bound is T1-S7's finding (f-007) reintroduced one
level down. Every level here goes through ``task_links.load_link_page`` — the
same function, not merely the same number — so the assertions below are on the
CALL COUNT, the tail copy and the deadline behaviour, all at depth >= 2.

The fixture helpers come from ``tests.test_task_detail``: the same fake, the
same edge-writing vocabulary, and the same page under test one level down.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from lithos_lens import web
from lithos_lens.blocker_chain import (
    BLOCKER_MAX_DEPTH,
    BlockerExpansion,
    blocker_expansion,
    load_blocker_level,
)
from lithos_lens.config import load_config
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import LINK_PAGE_SIZE, LinkedTask
from lithos_lens.tasks import MAX_FILTER_QUERY_BYTES, TaskRecord
from lithos_lens.web import create_app
from tests.test_task_detail import _client, _link, _task
from tests.test_tasks_mvp import TaskFakeLithosClient


def _expander_urls(text: str) -> dict[str, str]:
    """Every expander the rendered markup offers, keyed by the blocker it walks.

    Read off the markup rather than constructed by the test: the URL is the
    contract between the level that renders it and the fragment route that
    answers it, so a test that built its own would pass over a broken link.
    """
    urls: dict[str, str] = {}
    for chunk in text.split('data-blocker-expand="')[1:]:
        task_id, _, rest = chunk.partition('"')
        url = rest.split('hx-get="', 1)[1].split('"', 1)[0]
        urls[task_id] = url.replace("&amp;", "&")
    return urls


def _chain_fixture(fake: TaskFakeLithosClient, ids: list[str]) -> None:
    """Wire ``ids`` into a blocker chain: each entry blocks the one before it."""
    for index, task_id in enumerate(ids):
        if task_id != "open-unclaimed":
            fake.tasks.append(_task(task_id, title=f"Level {index}"))
    for blocker, blocked in zip(ids[1:], ids[:-1], strict=True):
        _link(fake, blocker, blocked, "blocks")


# --- Criterion 1: a chain expands two levels, one level per interaction ----


def test_a_blocker_chain_expands_two_levels_one_interaction_at_a_time(
    lithos_lens_config_env: Path,
) -> None:
    """Headline acceptance: A <- B <- C. The detail page shows B; asking for
    B's blockers shows C with ITS live status; asking for C's shows D.

    Each step is one fetch of one level — the operator walks the chain without
    Lens loading a graph — and every level is rendered by the same partial, so
    C's line looks like B's."""
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b", "level-c", "level-d"])

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        # Level 1 names B and offers to walk it; C is NOT on the page yet.
        assert 'data-link-target="level-b"' in page.text
        assert 'data-link-target="level-c"' not in page.text

        first = client.get(_expander_urls(page.text)["level-b"])
        assert first.status_code == 200
        # Level 2: C, with the status Lithos reports for it now.
        assert 'data-blocker-depth="2"' in first.text
        assert 'data-link-target="level-c"' in first.text
        assert 'class="badge badge-open">open</span>' in first.text
        assert 'data-link-target="level-d"' not in first.text

        second = client.get(_expander_urls(first.text)["level-c"])
        assert second.status_code == 200
        assert 'data-blocker-depth="3"' in second.text
        assert 'data-link-target="level-d"' in second.text


def test_an_expanded_level_that_has_no_blockers_says_so(
    lithos_lens_config_env: Path,
) -> None:
    """The affirmative answer matters one level down too: a walk that ends
    must say the chain ends, not render blank."""
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b"])

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        level = client.get(_expander_urls(page.text)["level-b"])

    assert level.status_code == 200
    assert "Nothing is blocking this task." in level.text


# --- Criterion 2: the walk stops at BLOCKER_MAX_DEPTH ----------------------


def test_the_walk_stops_at_the_depth_bound_and_never_fetches_one_more_level(
    lithos_lens_config_env: Path,
) -> None:
    """A chain deeper than the bound renders no expansion control AT the bound,
    so the depth-6 level is never requested — not merely refused.

    Walked the way an operator walks it: follow whatever expander the markup
    actually offers, and stop when it offers none."""
    ids = ["open-unclaimed"] + [f"deep-{index}" for index in range(1, 8)]
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ids)

    with _client(lithos_lens_config_env, fake) as client:
        text = client.get("/tasks/open-unclaimed").text
        depth = 1
        while urls := _expander_urls(text):
            assert depth < BLOCKER_MAX_DEPTH, "expanded past the bound"
            text = client.get(urls[ids[depth]]).text
            depth += 1

    assert depth == BLOCKER_MAX_DEPTH
    # The last level rendered its lines and SAID why it stops there.
    assert f'data-link-target="{ids[BLOCKER_MAX_DEPTH]}"' in text
    assert "data-blocker-depth-limit" in text
    assert f'data-blocker-max-depth="{BLOCKER_MAX_DEPTH}"' in text
    # And the level below it was never read: no task_get for the depth-6 node.
    assert ids[BLOCKER_MAX_DEPTH + 1] not in fake.get_calls


def test_the_depth_notice_is_absent_when_the_bound_held_nothing_back(
    lithos_lens_config_env: Path,
) -> None:
    """The notice is a claim that there is more below, so it must not fire on a
    level that simply ends. A chain whose last level is entirely satisfied has
    nothing the bound is hiding — the same care the overflow tail takes, in the
    other direction."""
    ids = ["open-unclaimed"] + [f"deep-{index}" for index in range(1, 6)]
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ids)
    # The line the walk ends on is a COMPLETED predecessor: satisfied, so it
    # would carry no expander at any depth. The bound is not what stops here.
    fake.tasks = [
        replace(task, status="completed") if task.id == ids[-1] else task
        for task in fake.tasks
    ]

    with _client(lithos_lens_config_env, fake) as client:
        text = client.get("/tasks/open-unclaimed").text
        depth = 1
        while urls := _expander_urls(text):
            text = client.get(urls[ids[depth]]).text
            depth += 1

    assert depth == BLOCKER_MAX_DEPTH
    assert 'data-link-target="deep-5"' in text
    assert "data-link-satisfied" in text
    assert "data-blocker-depth-limit" not in text


def test_an_over_deep_chain_is_refused_before_any_lithos_read(
    lithos_lens_config_env: Path,
) -> None:
    """The rendered levels stop offering expanders at the bound, so this is
    only reachable by hand-editing the URL. It must still cost nothing: a
    forged chain is answered from the depth bound alone, with no edge read and
    no fan-out behind it."""
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b"])
    chain = "&".join(f"chain=forged-{index}" for index in range(BLOCKER_MAX_DEPTH + 2))

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(f"/tasks/level-b/blockers?{chain}")

    assert response.status_code == 200
    assert "data-blocker-depth-limit" in response.text
    assert fake.get_calls == []
    assert fake.edge_list_calls == []


def test_a_chain_that_does_not_end_at_this_task_is_refused(
    lithos_lens_config_env: Path,
) -> None:
    """correctness/f-001: everything downstream reads the trail's LAST entry as
    "the task this level is about" — the depth count, the cycle test and the
    deeper expander URLs all proceed from it.

    A trail naming this task earlier and something else last describes no walk
    this level can speak from: ``?chain=A&chain=B&chain=X`` on B's level used
    to report a cycle running through X, asserting that X had been walked and
    was part of the loop. It is refused instead, before any read."""
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b"])

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/tasks/open-unclaimed/blockers"
            "?chain=level-b&chain=open-unclaimed&chain=forged-x"
        )

    assert response.status_code == 200
    assert "data-blocker-bad-chain" in response.text
    # No forged loop, and the entry that was never walked is not named at all.
    assert "data-blocker-cycle" not in response.text
    assert "forged-x" not in response.text
    # Refused from the request alone: no edge read, no fan-out.
    assert fake.get_calls == []
    assert fake.edge_list_calls == []


def test_a_chain_that_omits_this_task_still_walks_it(
    lithos_lens_config_env: Path,
) -> None:
    """The other half of the rule. A trail naming only the ancestors is a
    coherent walk — this level appends itself, so it still counts against the
    depth bound and still sees a blocker pointing back at itself."""
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b", "level-c"])

    level = asyncio.run(load_blocker_level(fake, "level-b", ["open-unclaimed"]))

    assert level.chain == ("open-unclaimed", "level-b")
    assert not level.bad_chain
    assert [link.task_id for link in level.page.links] == ["level-c"]


# --- Criterion 3: a cycle renders the callout instead of recursing ---------


def test_a_cycle_renders_the_callout_instead_of_recursing(
    lithos_lens_config_env: Path,
) -> None:
    """``blocks`` edges are agent-written and Lithos does not forbid a cycle.

    Expanding into one must terminate the walk — the offending line carries the
    cycle callout and, crucially, NO expander, which is what stops it rather
    than merely labelling it."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("loop-b", title="Loop partner"))
    _link(fake, "loop-b", "open-unclaimed", "blocks")
    _link(fake, "open-unclaimed", "loop-b", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        level = client.get(_expander_urls(page.text)["loop-b"])

    assert level.status_code == 200
    text = level.text
    # The line the chain has been through before is called out, on that line.
    assert 'data-blocker-cycle="open-unclaimed"' in text
    assert "cycle: already on this chain" in text
    # The walk terminates: nothing on this level offers to go deeper.
    assert _expander_urls(text) == {}


def test_a_cycle_stops_only_the_line_that_closes_it(
    lithos_lens_config_env: Path,
) -> None:
    """A cycle is a property of one path, not of the level. A sibling blocker
    on the same level is still walkable, or one bad edge would truncate an
    otherwise sound chain."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("loop-b", title="Loop partner"),
            _task("side-c", title="Sound sibling"),
            _task("side-d", title="Deeper still"),
        ]
    )
    _link(fake, "loop-b", "open-unclaimed", "blocks")
    _link(fake, "open-unclaimed", "loop-b", "blocks")
    _link(fake, "side-c", "loop-b", "blocks")
    _link(fake, "side-d", "side-c", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        level = client.get(_expander_urls(page.text)["loop-b"])

    assert 'data-blocker-cycle="open-unclaimed"' in level.text
    assert list(_expander_urls(level.text)) == ["side-c"]


def test_a_deeper_cycle_still_terminates_the_walk(
    lithos_lens_config_env: Path,
) -> None:
    """The three-task loop, walked the way an operator walks it. The callout
    fires where the chain comes back round, and the walk stops there rather
    than running on to the depth bound."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend([_task("loop-b", title="Second"), _task("loop-c", title="Third")])
    _link(fake, "loop-b", "open-unclaimed", "blocks")
    _link(fake, "loop-c", "loop-b", "blocks")
    _link(fake, "open-unclaimed", "loop-c", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        second = client.get(_expander_urls(page.text)["loop-b"])
        third = client.get(_expander_urls(second.text)["loop-c"])

    assert 'data-link-target="open-unclaimed"' in third.text
    assert 'data-blocker-cycle="open-unclaimed"' in third.text
    assert _expander_urls(third.text) == {}
    # Stopped by the loop, not by running out of depth.
    assert "data-blocker-depth-limit" not in third.text


# --- correctness/f-002, security/f-002: the callout claims only what it read


def test_the_callout_never_asserts_an_edge_this_render_did_not_read(
    lithos_lens_config_env: Path,
) -> None:
    """The defect both reviewers landed on. §5.5.2's ``cycle: A -> B -> A``
    asserts TWO edges; a level reads one — its edge list proves the line blocks
    the task above. That the chain arrived here FROM that line is the URL's
    claim, and the URL is anonymous client input.

    So over a graph with no cycle anywhere, a hand-built ``?chain=`` used to
    render that arrow form in its most credible shape (no elision marker),
    telling an operator that two real tasks deadlock — on the page they opened
    to find out why work is stuck — and suppressing the expander so they could
    not walk it to check.

    The callout now reports the CHAIN, which is the thing this render can see.
    """
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [_task("level-b", title="Level B"), _task("other-c", title="Other C")]
    )
    # other-c -> level-b -> open-unclaimed. No edge closes any loop.
    _link(fake, "level-b", "open-unclaimed", "blocks")
    _link(fake, "other-c", "level-b", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        honest = client.get("/tasks/open-unclaimed")
        forged = client.get(
            "/tasks/open-unclaimed/blockers?chain=level-b&chain=open-unclaimed"
        )

    # The honest page never claimed a cycle, and still does not.
    assert "blocker-chip-cycle" not in honest.text
    # The crafted link cannot make the page assert one either: no arrow form,
    # and no pair of task ids presented as blocking each other.
    chip = forged.text.split('class="blocker-chip blocker-chip-cycle"', 1)[1]
    chip = chip.split("</span>", 1)[0]
    assert "→" not in chip
    assert "level-b" not in chip.split(">", 1)[1]
    assert "open-unclaimed" not in chip.split(">", 1)[1]
    # What it says instead is true of the chain it was handed.
    assert "cycle: already on this chain" in chip


def test_the_callout_carries_no_request_derived_text_at_all() -> None:
    """Stated without a page around it: the verdict is a flag, so there is no
    field for a crafted chain to write into. Whatever the trail holds and
    however long it is, the answer is the same shape."""
    link = LinkedTask(task_id="alpha", edge_type="blocks")

    assert blocker_expansion(link, ("alpha", "beta")) == BlockerExpansion(
        revisits_chain=True
    )
    assert blocker_expansion(link, ("alpha", "middle", "beta")) == BlockerExpansion(
        revisits_chain=True
    )
    # The degenerate loop — a task blocking itself — is the same answer.
    assert blocker_expansion(link, ("alpha",)) == BlockerExpansion(revisits_chain=True)
    # And a line the chain has not been through is offered a walk, not a verdict.
    assert blocker_expansion(link, ("beta",)) == BlockerExpansion(expandable=True)


def test_a_forged_chain_cannot_put_its_own_text_into_the_cycle_callout(
    lithos_lens_config_env: Path,
) -> None:
    """security/f-002's first half. ``chain`` is anonymous client input and the
    fragment is served as HTML from Lens's own origin, so a trail entry spliced
    into the callout was attacker text presented in Lens's vocabulary as a task
    id — content spoofing today, and one hand-built attribute or ``|safe``
    away from live injection.

    The fixture holds only ``level-b -> open-unclaimed``: the request below is
    hand-built and the chain it describes was never walked, which is exactly
    why the callout may not speak for the graph. Nothing from the query string
    reaches the body at all — the entry appears NOWHERE in it, escaped or
    otherwise."""
    forged = "<img src=x onerror=alert(1)>"
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b"])

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            f"/tasks/open-unclaimed/blockers?chain=level-b&chain={forged}"
        )

    assert response.status_code == 200
    # The chain it was handed has been through level-b, and that is all it says.
    assert 'data-blocker-cycle="level-b"' in response.text
    assert "cycle: already on this chain" in response.text
    # The forged entry is absent from the body in every form.
    assert "onerror" not in response.text
    assert "img src" not in response.text
    assert "alert(1)" not in response.text


# --- Criterion 4 + 5: every level pages its breadth, and says so -----------


def test_an_expanded_level_resolves_only_one_page_of_blockers(
    lithos_lens_config_env: Path,
) -> None:
    """The defect this slice must not replicate. Depth bounds how many levels
    a walk chains; it says nothing about how WIDE any one of them is, and a
    level whose node has a runaway blocker set would otherwise issue one
    ``lithos_task_get`` per blocker with no count bound — T1-S7's f-007, one
    level down.

    Asserted on the recorded calls for the FRAGMENT request alone, because
    what is bounded is the round trips on the shared MCP session."""
    extra = 13
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "wide-b"])
    for index in range(LINK_PAGE_SIZE + extra):
        blocker_id = f"wide-pred-{index:03d}"
        fake.tasks.append(_task(blocker_id, title=f"Predecessor {index:03d}"))
        _link(fake, blocker_id, "wide-b", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        url = _expander_urls(page.text)["wide-b"]
        fake.get_calls.clear()
        level = client.get(url)

    assert level.status_code == 200
    # Exactly one page of lookups for this expansion, and no more.
    assert len(fake.get_calls) == LINK_PAGE_SIZE
    assert fake.get_calls == [
        f"wide-pred-{index:03d}" for index in range(LINK_PAGE_SIZE)
    ]
    # The page really is rendered with live status, not just counted.
    assert 'data-link-target="wide-pred-000"' in level.text
    assert f'data-link-target="wide-pred-{LINK_PAGE_SIZE:03d}"' not in level.text


def test_a_deeper_level_tail_says_how_many_more_blockers_exist(
    lithos_lens_config_env: Path,
) -> None:
    """The tail is VISIBLE at every level, not just level 1. A silently
    truncated blocker list on a page whose job is "why can't this run?" is a
    defect, not a bound — so the operator sees that more exist and how many,
    in the same words the level-1 tail uses."""
    extra = 13
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "wide-b"])
    for index in range(LINK_PAGE_SIZE + extra):
        blocker_id = f"wide-pred-{index:03d}"
        fake.tasks.append(_task(blocker_id))
        _link(fake, blocker_id, "wide-b", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        level = client.get(_expander_urls(page.text)["wide-b"])

    text = level.text
    assert 'data-link-tail="blockers"' in text
    assert f'data-link-remaining="{extra}"' in text
    assert f"{extra} more blockers not shown." in text
    assert f"This task has {LINK_PAGE_SIZE + extra} blockers in all" in text


# --- Criterion 6: the pagination is REUSED, not reimplemented --------------


def test_every_level_is_paged_by_the_one_shared_page_size(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence that this level goes through T1-S7's helper rather than a
    lookalike: moving ``task_links.LINK_PAGE_SIZE`` — the single constant, which
    ``load_link_page`` reads and the tail copy quotes — moves the deeper
    level's page, its lookup count and its tail together.

    A second page size, or a second tail path, would leave one of the three
    behind."""
    monkeypatch.setattr("lithos_lens.task_links.LINK_PAGE_SIZE", 2)
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "wide-b"])
    for index in range(5):
        blocker_id = f"wide-pred-{index}"
        fake.tasks.append(_task(blocker_id))
        _link(fake, blocker_id, "wide-b", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        url = _expander_urls(page.text)["wide-b"]
        fake.get_calls.clear()
        level = client.get(url)

    assert len(fake.get_calls) == 2
    assert level.text.count("data-link-target=") == 2
    assert 'data-link-remaining="3"' in level.text
    assert "3 more blockers not shown." in level.text
    assert "the first 2 are listed above" in level.text


# --- Criterion 8: expandability comes from the verdicts, not from status ---


@pytest.mark.parametrize(
    ("status", "expandable", "why"),
    [
        ("open", True, "a live blocker: its own blockers are the next answer"),
        ("completed", False, "satisfied — the dependency is met"),
        ("cancelled", False, "unsatisfiable — a dead end, walking it changes nothing"),
    ],
)
def test_which_blocker_states_carry_an_expander(
    lithos_lens_config_env: Path, status: str, expandable: bool, why: str
) -> None:
    """Decided from the verdicts ``LinkedTask`` already exposes, not from a
    fresh ``status != "completed"`` test."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("pred-under-test", title="Predecessor", status=status))
    _link(fake, "pred-under-test", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert ("pred-under-test" in _expander_urls(response.text)) is expandable, why


def test_a_blocker_whose_status_never_arrived_carries_no_expander(
    lithos_lens_config_env: Path,
) -> None:
    """The fourth state, and the one a re-derivation gets wrong. An unresolved
    row has ``status == ""``, so a naive ``!= "completed"`` marks it expandable
    on the strength of a read that never answered — an offer to walk under a
    line the page has no state for at all."""
    fake = TaskFakeLithosClient()
    # No task record, so the blocker's task_get answers task_not_found.
    _link(fake, "ghost-pred", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert "data-link-unresolved" in response.text
    assert _expander_urls(response.text) == {}


def test_provenance_links_never_carry_an_expander(
    lithos_lens_config_env: Path,
) -> None:
    """One partial renders the blocker chain AND both provenance directions, so
    the gate on ``blocking`` is what keeps a walk off a section that records no
    dependency."""
    fake = TaskFakeLithosClient()
    fake.tasks.extend(
        [
            _task("source-task", title="Source"),
            _task("follow-on", title="Follow-on"),
        ]
    )
    _link(fake, "source-task", "open-unclaimed", "discovered_from")
    _link(fake, "open-unclaimed", "follow-on", "discovered_from")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/open-unclaimed")

    assert "Discovered while working on" in response.text
    assert _expander_urls(response.text) == {}


def test_a_gate_blocker_is_walkable_like_any_other_blocker(
    lithos_lens_config_env: Path,
) -> None:
    """``waits_on_gate`` stops a task exactly as ``blocks`` does, so a live
    gate is a live blocker — what the gate itself waits on is the next answer.
    """
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
        response = client.get("/tasks/open-unclaimed")

    assert "gate-review" in _expander_urls(response.text)


def test_the_expansion_verdict_is_derived_from_the_link_verdicts() -> None:
    """The rule stated directly, without a page around it: the four states and
    what each decides."""
    chain = ("root",)
    live = LinkedTask(task_id="pred", edge_type="blocks", status="open")
    satisfied = LinkedTask(task_id="pred", edge_type="blocks", status="completed")
    unsatisfiable = LinkedTask(task_id="pred", edge_type="blocks", status="cancelled")
    unresolved = LinkedTask(task_id="pred", edge_type="blocks", unresolved=True)

    assert blocker_expansion(live, chain) == BlockerExpansion(expandable=True)
    assert blocker_expansion(satisfied, chain) == BlockerExpansion()
    assert blocker_expansion(unsatisfiable, chain) == BlockerExpansion()
    assert blocker_expansion(unresolved, chain) == BlockerExpansion()
    # And with no chain there is no walk at all: the provenance lists.
    assert blocker_expansion(live) == BlockerExpansion()


# --- Criterion 9: every read on a level is the deadlined, bounded one ------


def test_a_stalled_neighbour_read_degrades_that_line_not_the_fragment(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing under the fragment imposes a deadline — ``session.call_tool``
    takes no timeout, ``SESSION_WAIT_TIMEOUT_S`` covers only session
    establishment, and uvicorn sets no request deadline — so a route that read
    a neighbour itself would hang for as long as the session stayed half-open.

    Going through ``load_link_page`` at every level is what gives the deeper
    levels the deadline the eager one has: the stalled line renders with no
    status claim and the rest of the level renders around it."""
    monkeypatch.setattr("lithos_lens.task_links.LINK_READ_TIMEOUT_S", 0.05)

    class StalledNeighbour(TaskFakeLithosClient):
        async def task_get(self, task_id: str) -> TaskRecord:
            if task_id == "stalled-c":
                await asyncio.Event().wait()  # never answers
            return await super().task_get(task_id)

    fake = StalledNeighbour()
    _chain_fixture(fake, ["open-unclaimed", "level-b"])
    fake.tasks.extend(
        [_task("stalled-c", title="Stalled"), _task("live-c", title="Live")]
    )
    _link(fake, "stalled-c", "level-b", "blocks")
    _link(fake, "live-c", "level-b", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        level = client.get(_expander_urls(page.text)["level-b"])

    assert level.status_code == 200
    text = level.text
    # The stalled line renders, claiming nothing; its sibling is unaffected.
    assert 'data-link-target="stalled-c"' in text
    assert "data-link-unresolved" in text
    assert 'data-link-target="live-c"' in text
    assert "Live" in text


def test_a_stalled_edge_read_degrades_the_level_rather_than_hanging_it(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The level's own gating read is deadlined for the same reason, and a
    level that could not be read must not come back as "nothing is blocking
    this task"."""
    monkeypatch.setattr("lithos_lens.blocker_chain.LINK_READ_TIMEOUT_S", 0.05)

    class StalledEdges(TaskFakeLithosClient):
        async def task_edge_list(
            self, task_id: str, **kwargs: object
        ) -> list[EdgeRecord]:
            if task_id == "level-b":
                await asyncio.Event().wait()  # never answers
            return await super().task_edge_list(task_id, **kwargs)  # type: ignore[arg-type]

    fake = StalledEdges()
    _chain_fixture(fake, ["open-unclaimed", "level-b"])

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")
        level = client.get(_expander_urls(page.text)["level-b"])

    assert level.status_code == 200
    assert "Blockers unavailable. Refresh this task to retry." in level.text
    assert "Nothing is blocking this task." not in level.text


def test_a_level_reads_its_own_task_only_through_the_bounded_page() -> None:
    """The fragment resolves neighbours, never the node it was asked about:
    that record is already on the page the expander was clicked from, and a
    second read of it would be a round trip outside the bounded fan-out."""
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b", "level-c"])

    level = asyncio.run(
        load_blocker_level(fake, "level-b", ["open-unclaimed", "level-b"])
    )

    assert [link.task_id for link in level.page.links] == ["level-c"]
    assert fake.get_calls == ["level-c"]


# --- Criterion 10: the fragment is an ordinary, metered render -------------


def test_the_fragment_refuses_an_oversized_filter_query_before_reading(
    lithos_lens_config_env: Path,
) -> None:
    """Refused FIRST, like every other route that re-emits preserved filters
    into generated URLs: the amplification lives in the shared URL builder, and
    a fragment that renders one link per blocker multiplies it per level."""
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b"])
    oversized = "x" * (MAX_FILTER_QUERY_BYTES + 1)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            f"/tasks/level-b/blockers?chain=open-unclaimed&chain=level-b"
            f"&project={oversized}"
        )

    assert response.status_code == 400
    assert "data-filter-rejected" in response.text
    assert oversized not in response.text
    assert fake.get_calls == []
    assert fake.edge_list_calls == []


def test_an_oversized_chain_is_measured_by_the_same_query_budget(
    lithos_lens_config_env: Path,
) -> None:
    """security/f-001: ``chain`` is re-emitted into the ``hx-get`` of every
    expandable line, so one fragment copies it up to ``LINK_PAGE_SIZE`` times —
    the same multiplication ``MAX_FILTER_QUERY_BYTES`` exists to bound. It was
    exempt from that budget because the budget measured only the PRESERVED
    keys, so an anonymous client picked both the input size and how many times
    the render copied it out: a 39 KB chain rendered a 992 KB fragment (~25x).

    Now measured with the rest, so it takes the existing refusal path before
    any Lithos read."""
    fake = TaskFakeLithosClient()
    for index in range(30):
        blocker_id = f"pred-{index:03d}"
        fake.tasks.append(_task(blocker_id))
        _link(fake, blocker_id, "open-unclaimed", "blocks")
    oversized = "A" * MAX_FILTER_QUERY_BYTES

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            f"/tasks/open-unclaimed/blockers?chain={oversized}&chain=open-unclaimed"
        )

    assert response.status_code == 400
    assert "data-filter-rejected" in response.text
    # Refused, not rendered: no reads, and the response cannot be inflated by
    # the request — it is smaller than the query that asked for it.
    assert fake.get_calls == []
    assert fake.edge_list_calls == []
    assert len(response.text) < len(oversized)


def test_a_chain_inside_the_budget_still_expands(
    lithos_lens_config_env: Path,
) -> None:
    """The bound must not cost the ordinary case: real ids are short, and a
    full-depth trail of them is nowhere near the budget."""
    ids = ["open-unclaimed"] + [f"deep-{index}" for index in range(1, 6)]
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ids)

    with _client(lithos_lens_config_env, fake) as client:
        text = client.get("/tasks/open-unclaimed").text
        depth = 1
        while urls := _expander_urls(text):
            response = client.get(urls[ids[depth]])
            assert response.status_code == 200
            text = response.text
            depth += 1

    assert depth == BLOCKER_MAX_DEPTH


def test_the_chain_never_rides_along_on_ordinary_navigation(
    lithos_lens_config_env: Path,
) -> None:
    """Measured is not the same as preserved. ``chain`` describes one expansion
    walk, so the board, tag and detail links a level renders must not carry it
    — only the one builder that owns it emits it."""
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b", "level-c"])

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed?project=influx")
        level = client.get(_expander_urls(page.text)["level-b"])

    # The level's own detail links carry the filter and NOT the trail.
    assert "/tasks/level-c?project=influx" in level.text
    assert "/tasks/level-c?chain" not in level.text
    assert "/tasks/level-c?project=influx&amp;chain" not in level.text


def test_the_fragment_stays_metered_by_admission_control(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_is_metered`` is default-metered, and this route must not be added to
    the unmetered lists — those exist for the health probe, the SSE stream and
    static assets, none of which do Lithos work. A fragment that fans out one
    ``task_get`` per blocker is exactly what the cap is for."""
    monkeypatch.setattr(web, "MAX_CONCURRENT_RENDERS", 0)
    app = create_app(load_config(lithos_lens_config_env))

    with TestClient(app) as client:
        response = client.get("/tasks/level-b/blockers?chain=level-b")

    assert response.status_code == 503
    assert "capacity" in response.text.lower()


def test_the_fragment_is_unavailable_rather_than_wrong_while_lithos_is_offline(
    lithos_lens_config_env: Path,
) -> None:
    """Same call the detail page makes: with no backend the level says so, and
    never renders as an empty (and therefore reassuring) chain."""
    fake = TaskFakeLithosClient(health="unreachable")

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks/level-b/blockers?chain=level-b")

    assert response.status_code == 200
    assert "data-blocker-level-offline" in response.text
    assert "Nothing is blocking this task." not in response.text


def test_the_expander_and_the_links_it_renders_preserve_the_boards_filters(
    lithos_lens_config_env: Path,
) -> None:
    """A fragment fetched without the board's query string emits unfiltered
    links, so the expander URL carries the preserved filters and the level it
    returns puts them back into every link and every deeper expander.

    Through the same allowlist as the rest of navigation: a retired param
    (``claimed_state``) stops at the first link rather than riding the walk
    down."""
    fake = TaskFakeLithosClient()
    _chain_fixture(fake, ["open-unclaimed", "level-b", "level-c"])

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get(
            "/tasks/open-unclaimed?project=influx&claimed_state=unclaimed"
        )
        expander = _expander_urls(page.text)["level-b"]
        level = client.get(expander)

    # The expander carries the scope and the trail, and not the retired param.
    query = parse_qs(urlsplit(expander).query, keep_blank_values=True)
    assert urlsplit(expander).path == "/tasks/level-b/blockers"
    assert query["project"] == ["influx"]
    assert query["chain"] == ["open-unclaimed", "level-b"]
    assert "claimed_state" not in query
    # And the level it returns keeps them on its links and its own expander.
    assert "/tasks/level-c?project=influx" in level.text
    deeper = _expander_urls(level.text)["level-c"]
    assert parse_qs(urlsplit(deeper).query)["project"] == ["influx"]
    assert "claimed_state" not in level.text


def test_an_id_with_reserved_characters_stays_one_path_segment(
    lithos_lens_config_env: Path,
) -> None:
    """Task ids are arbitrary non-empty strings off agent-written payloads, so
    the fragment URL encodes the id for the same reason ``task_detail_url``
    does: a ``/`` would invent a path segment and address something else, and a
    ``?`` would truncate the path and lose the chain behind it.

    Encoded to the same shape as the row's own detail link, which is the
    property that matters — the two URLs address the same task."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("team/pred", title="Awkward predecessor"))
    _link(fake, "team/pred", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        page = client.get("/tasks/open-unclaimed")

    expander = _expander_urls(page.text)["team/pred"]
    assert urlsplit(expander).path == f"/tasks/{quote('team/pred', safe='')}/blockers"
    assert f'href="/tasks/{quote("team/pred", safe="")}"' in page.text
    assert "/tasks/team/pred" not in page.text


# --- The T1-S7 contracts this slice must not disturb ----------------------


def test_the_level_1_heading_still_follows_still_blocking(
    lithos_lens_config_env: Path,
) -> None:
    """``LinkPage.still_blocking`` decides the section heading and is
    deliberately conservative. Adding expanders to the same rows must not have
    touched it — a satisfied row is still not a reason the task cannot run, and
    it still carries no expander."""
    fake = TaskFakeLithosClient()
    fake.tasks.append(_task("pred-done", title="Finished", status="completed"))
    _link(fake, "pred-done", "open-unclaimed", "blocks")

    with _client(lithos_lens_config_env, fake) as client:
        text = client.get("/tasks/open-unclaimed").text

    assert "<h2>Dependencies</h2>" in text
    assert "data-link-satisfied" in text
    assert _expander_urls(text) == {}
