"""The Agent picker: last activity, its ordering, and the inactive window (§5.4).

``lithos_agent_list`` is a registration log that accumulates a row per probe,
per session and per host, so the datalist rendered verbatim buried the fifteen
live identities under forty-odd dead ones. These tests pin what Lens does about
it WITHOUT a Lithos change: activity derived from rows the board already holds,
newest first, the idle ones behind a toggle, and duplicate names told apart by
id — and, because the whole point is to stay cheap, that the picker still costs
exactly one ``lithos_agent_list`` call and no per-agent read at all.

The load-bearing property is that ONE timestamp drives both the label and the
order: the list is sorted by the times it shows. The fixture exercises every
signal that timestamp can come from — a live claim, the newest of several
created tasks, a task on a RESOLVED row, the registration stamp, and nothing at
all — so a derivation that silently narrowed to one of them fails here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lithos_lens.agent_picker import agent_options, show_all_agents
from lithos_lens.config import load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.tasks import AgentRecord, ClaimRecord, FindingRecord, TaskRecord
from lithos_lens.web import create_app

# The route path takes its clock from the REQUEST (``load_dashboard`` reads it),
# while these fixtures are stamped at import, so every age below is deliberately
# placed mid-bucket — 2h30 renders "2h" until the gap between import and request
# exceeds half an hour, and the day-scale ones tolerate twelve. A boundary-
# hugging fixture (exactly 3h) would fail on a slow or suspended worker with no
# product regression, which is the one way these assertions could lie.
_NOW = datetime.now(UTC).replace(microsecond=0)


def _ago(**delta: float) -> str:
    return (_NOW - timedelta(**delta)).isoformat()


def _ahead(**delta: float) -> str:
    return (_NOW + timedelta(**delta)).isoformat()


def _task(
    task_id: str,
    *,
    created_by: str,
    created_at: str,
    status: str = "open",
    resolved_at: str = "",
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title=f"Title {task_id}",
        status=status,  # type: ignore[arg-type]
        created_by=created_by,
        created_at=created_at,
        resolved_at=resolved_at,
        claims=(),
    )


# The fixture the acceptance asks for — one agent active today, one active 40
# days ago, two registrations sharing a name, one never-active registration —
# plus the three cases that pin the derivation itself: `claimer` (an OLD
# registration holding a live claim, which must lead the list and say so),
# `archivist` (whose only activity is on a RESOLVED row) and `steady` (inside
# the shipped 30-day window but well outside a smaller one).
_AGENTS = (
    AgentRecord(id="pilot", name="Pilot", last_seen_at=_ago(days=2)),
    AgentRecord(id="lens-a", name="Lithos Lens", last_seen_at=_ago(days=1)),
    AgentRecord(id="lens-b", name="Lithos Lens", last_seen_at=_ago(days=1)),
    AgentRecord(id="archivist", name="Archivist"),
    AgentRecord(id="steady", name="Steady", last_seen_at=_ago(days=25)),
    AgentRecord(id="claimer", name="Claimer", last_seen_at=_ago(days=40)),
    AgentRecord(id="dormant", name="Dormant", last_seen_at=_ago(days=40)),
    AgentRecord(id="probe", name="probe-agent"),
)
_OPEN_TASKS = (
    # Two tasks for one registration: the NEWEST dates it. Ordering alone would
    # not catch the wrong one, so the label assertion below states 2h.
    _task("t-pilot-old", created_by="pilot", created_at=_ago(days=9)),
    _task("t-pilot", created_by="pilot", created_at=_ago(hours=2, minutes=30)),
    _task("t-lens-a", created_by="lens-a", created_at=_ago(hours=5, minutes=30)),
    _task("t-lens-b", created_by="lens-b", created_at=_ago(hours=8, minutes=30)),
    _task("t-steady", created_by="steady", created_at=_ago(days=20, hours=12)),
    _task("t-dormant", created_by="dormant", created_at=_ago(days=40)),
    # Claimed by `claimer`, created by an identity that is not registered at
    # all: the picker lists REGISTRATIONS, so the creator must not appear.
    _task("t-claimed", created_by="retired-planner", created_at=_ago(days=90)),
)
_RESOLVED_TASKS = (
    _task(
        "t-archived",
        created_by="archivist",
        created_at=_ago(days=3, hours=12),
        status="completed",
        resolved_at=_ago(days=1),
    ),
)
_TASKS = _OPEN_TASKS + _RESOLVED_TASKS


def _dataset() -> FakeLithosDataset:
    return FakeLithosDataset(
        tasks=_TASKS,
        ready_ids=frozenset(task.id for task in _OPEN_TASKS if task.id != "t-claimed"),
        claims={
            "t-claimed": (
                ClaimRecord(
                    agent="claimer",
                    aspect="implementation",
                    expires_at=_ahead(hours=6),
                ),
            )
        },
        agents=_AGENTS,
        # Findings exist on a row the picker must NOT read: "last finding
        # posted" is deliberately out of scope (it is not in the snapshot).
        findings={
            "t-pilot": (
                FindingRecord(
                    id="f-1",
                    task_id="t-pilot",
                    agent="pilot",
                    summary="Nothing to see here",
                    created_at=_ago(minutes=5),
                ),
            )
        },
        stats={"open_claims": 1, "agents": len(_AGENTS)},
    )


# Every registration the picker offers by default, in rendered order.
_DEFAULT_ORDER = ["claimer", "pilot", "lens-a", "lens-b", "archivist", "steady"]


class _CountingFake(FakeLithosClient):
    """The shipped fake, plus the call log the cost assertions read.

    Subclassed rather than reimplemented so the picker is exercised against the
    same client the demo and the browser suite run on.
    """

    def __init__(self, dataset: FakeLithosDataset) -> None:
        super().__init__(None, dataset=dataset)
        self.calls: list[str] = []

    async def list_agents(self) -> list[AgentRecord]:
        self.calls.append("lithos_agent_list")
        return await super().list_agents()

    async def task_status(self, task_id: str):  # type: ignore[no-untyped-def]
        self.calls.append("lithos_task_status")
        return await super().task_status(task_id)

    async def list_findings(self, task_id: str, *, since: str | None = None):  # type: ignore[no-untyped-def]
        self.calls.append("lithos_finding_list")
        return await super().list_findings(task_id, since=since)


def _client(config_path: Path, fake: FakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


_DATALIST = re.compile(r'<datalist id="agents">(.*?)</datalist>', re.S)
_OPTION = re.compile(r'<option value="([^"]*)">([^<]*)</option>')


def _options(html: str) -> list[tuple[str, str]]:
    """The agent datalist as (value, label) pairs, in rendered order."""
    block = _DATALIST.search(html)
    assert block is not None, "the agents datalist is missing"
    return _OPTION.findall(block.group(1))


def _labels(html: str) -> dict[str, str]:
    return dict(_options(html))


def _toggle_href(html: str, state: str) -> str:
    """The show-all toggle's URL in the given state ("on" / "off")."""
    link = re.search(rf'href="([^"]*)" data-agent-show-all="{state}"', html)
    assert link is not None, f"the show-all toggle is missing in state {state}"
    return unescape(link.group(1))


# ── the datalist (route level) ─────────────────────────────────────────


def test_default_picker_is_recent_first_and_drops_the_dead_registrations(
    lithos_lens_config_env: Path,
) -> None:
    """One ordering over every signal: the live claim leads (it is the only
    thing observed NOW), then the newest created task — including one on a
    resolved row — and the 40-day and never-active registrations are not
    offered at all."""
    fake = _CountingFake(_dataset())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    assert response.status_code == 200
    assert [value for value, _ in _options(response.text)] == _DEFAULT_ORDER


def test_each_option_says_when_that_agent_was_last_active(
    lithos_lens_config_env: Path,
) -> None:
    """In the board's own age style (``humanize_age``), not a second format —
    and the time shown is the SAME signal the ordering used, which is what
    makes "most recently active first" a claim the operator can check."""
    fake = _CountingFake(_dataset())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    labels = _labels(response.text)
    # The claim holder leads with its own observed time, not the 40-day-old
    # registration stamp it would otherwise have been dated by.
    assert labels["claimer"] == "Claimer · 0m ago · claim held"
    # The NEWEST of pilot's two tasks (2h30), not the 9-day-old one.
    assert labels["pilot"] == "Pilot · 2h ago"
    assert labels["lens-a"] == "Lithos Lens (lens-a) · 5h ago"
    # Activity on a RESOLVED row counts: the picker reads every row the load
    # fetched, not just the open snapshot.
    assert labels["archivist"] == "Archivist · 3d ago"
    assert labels["steady"] == "Steady · 20d ago"


def test_duplicate_names_carry_their_id_and_singletons_do_not(
    lithos_lens_config_env: Path,
) -> None:
    """Three rows called "Lithos Lens" are the picker's other failure: the
    operator cannot tell which registration a name means, and guessing wrong
    filters the board to nothing."""
    fake = _CountingFake(_dataset())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    labels = _labels(response.text)
    assert labels["lens-a"].startswith("Lithos Lens (lens-a)")
    assert labels["lens-b"].startswith("Lithos Lens (lens-b)")
    assert "(pilot)" not in labels["pilot"]


def test_show_all_toggle_brings_the_hidden_registrations_back(
    lithos_lens_config_env: Path,
) -> None:
    """A query parameter, so the choice survives a reload and needs no JS."""
    fake = _CountingFake(_dataset())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?all_agents=1")

    values = [value for value, _ in _options(response.text)]
    # The two hidden ones join the end: 40 days old, then the registration
    # nothing ever dated.
    assert values == [*_DEFAULT_ORDER, "dormant", "probe"]
    assert _labels(response.text)["probe"] == "probe-agent · no activity"
    # And the form carries the choice through an Apply.
    assert '<input type="hidden" name="all_agents" value="1">' in response.text


def test_the_toggle_is_a_two_way_control(lithos_lens_config_env: Path) -> None:
    """The widened state must offer the way back, with the live filters intact
    — otherwise "show all" is a one-way door and the board's own filters are
    the price of closing it."""
    fake = _CountingFake(_dataset())

    with _client(lithos_lens_config_env, fake) as client:
        widened = client.get("/tasks?status=open&all_agents=1")
        back = _toggle_href(widened.text, "on")
        narrowed = client.get(back)

    assert back == "/tasks?status=open"
    assert [value for value, _ in _options(narrowed.text)] == _DEFAULT_ORDER


def test_the_toggle_link_keeps_the_live_filters(
    lithos_lens_config_env: Path,
) -> None:
    """Showing the dead registrations is not a reason to reset the board."""
    fake = _CountingFake(_dataset())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?status=open&agent=pilot")

    assert _toggle_href(response.text, "off") == (
        "/tasks?status=open&agent=pilot&all_agents=1"
    )


def test_the_window_is_read_from_config(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``agent_inactive_days=60`` brings the 40-day agent back into the default
    list — proving the knob is wired, not a literal in the picker."""
    monkeypatch.setenv("LITHOS_LENS_TASKS_AGENT_INACTIVE_DAYS", "60")
    fake = _CountingFake(_dataset())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks")

    values = [value for value, _ in _options(response.text)]
    assert values == [*_DEFAULT_ORDER, "dormant"]


def test_the_picker_costs_one_agent_read_and_no_per_agent_call(
    lithos_lens_config_env: Path,
) -> None:
    """The whole slice is derived from rows the board already loaded. A fan-out
    over 59 registrations would cost more than the crowded datalist did."""
    fake = _CountingFake(_dataset())

    with _client(lithos_lens_config_env, fake) as client:
        client.get("/tasks?all_agents=1")

    assert fake.calls == ["lithos_agent_list"]


def test_the_agent_filter_itself_is_unchanged(
    lithos_lens_config_env: Path,
) -> None:
    """The option VALUE is still the bare id, so typing a name or an id submits
    exactly as before — the labels are decoration on the same vocabulary."""
    fake = _CountingFake(_dataset())

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/tasks?agent=lens-a")

    assert '<input list="agents" name="agent" value="lens-a"' in response.text
    assert "Title t-lens-a" in response.text
    assert "Title t-pilot" not in response.text


# ── the assembly (unit level) ──────────────────────────────────────────


def test_a_held_claim_is_dated_by_the_load_that_observed_it() -> None:
    """A claim is live state with no start time upstream. It is stamped with
    the load's own evaluation time — the instant Lens saw it held — so the
    holder leads the list AND says why, instead of sorting first while showing
    a 40-day-old registration stamp."""
    claimed = TaskRecord(
        id="old",
        title="Old work",
        status="open",
        created_by="someone-else",
        created_at=_ago(days=90),
        claims=(ClaimRecord(agent="worker", aspect="implementation"),),
    )
    agents = [
        AgentRecord(id="recent", name="Recent"),
        AgentRecord(id="worker", name="Worker", last_seen_at=_ago(days=40)),
    ]
    errors: list[str] = []

    options = agent_options(
        agents,
        [claimed, _task("fresh", created_by="recent", created_at=_ago(minutes=1))],
        errors,
        now=_NOW,
    )

    assert [option.id for option in options] == ["worker", "recent"]
    assert options[0].label == "Worker · 0m ago · claim held"
    assert options[0].active
    assert options[1].label == "Recent · 1m ago"
    assert errors == []


def test_the_newest_task_dates_an_agent() -> None:
    """An agent is described by its LAST activity, so the newest of its rows
    wins — an oldest-wins derivation would report a live agent as dormant."""
    errors: list[str] = []

    options = agent_options(
        [AgentRecord(id="w", name="W")],
        [
            _task("old", created_by="w", created_at=_ago(days=9)),
            _task("new", created_by="w", created_at=_ago(hours=2)),
            _task("middle", created_by="w", created_at=_ago(days=1)),
        ],
        errors,
        now=_NOW,
    )

    assert options[0].label == "W · 2h ago"


def test_work_outranks_the_registration_stamp() -> None:
    """``last_seen_at`` is the FALLBACK. An agent that did something is
    described by that, even when Lithos touched the registration later."""
    errors: list[str] = []

    options = agent_options(
        [AgentRecord(id="w", name="W", last_seen_at=_ago(minutes=1))],
        [_task("t", created_by="w", created_at=_ago(hours=4))],
        errors,
        now=_NOW,
    )

    assert options[0].label == "W · 4h ago"
    assert not options[0].registered_only


def test_a_registration_only_agent_says_so() -> None:
    """ "Registered 2h ago" and "created something 2h ago" are different answers
    to "is this identity live?", so the label does not conflate them."""
    errors: list[str] = []

    options = agent_options(
        [AgentRecord(id="fresh", name="Fresh", last_seen_at=_ago(hours=2))],
        [],
        errors,
        now=_NOW,
    )

    assert options[0].label == "Fresh · registered 2h ago"
    assert options[0].active


def test_an_agent_exactly_on_the_window_is_still_offered() -> None:
    """The boundary belongs to the window it is measured against — one day
    later the same agent drops out."""
    errors: list[str] = []
    agents = [AgentRecord(id="edge", name="Edge", last_seen_at=_ago(days=30))]

    on_window = agent_options(agents, [], errors, days=30, now=_NOW)
    past_window = agent_options(agents, [], errors, days=29, now=_NOW)

    assert on_window[0].active
    assert not past_window[0].active


def test_an_unreadable_timestamp_is_not_guessed_at() -> None:
    """A row Lens cannot date is no evidence of activity: the agent falls back
    to its registration, and with neither it reads as never active."""
    errors: list[str] = []

    options = agent_options(
        [AgentRecord(id="w", name="W")],
        [_task("t", created_by="w", created_at="not-a-timestamp")],
        errors,
        now=_NOW,
    )

    assert options[0].label == "W · no activity"
    assert not options[0].active


def test_a_failed_agent_read_reports_the_error_and_offers_nothing() -> None:
    """The picker is one input on a board the rest of which is fine."""
    errors: list[str] = []

    options = agent_options(RuntimeError("agents unavailable"), [], errors)

    assert options == ()
    assert errors == ["Could not load agent list."]


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_show_all_agents_accepts_the_shipped_truthy_spellings(value: str) -> None:
    assert show_all_agents(value)


@pytest.mark.parametrize("value", ["", "0", "off", "no"])
def test_show_all_agents_defaults_to_the_windowed_list(value: str) -> None:
    assert not show_all_agents(value)
