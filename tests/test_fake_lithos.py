"""Fake-Lithos app mode: the launchable, server-backed fixture client.

These cover the seam the ``e2e/`` Playwright smoke suite depends on — that the
real application factory can boot a fully browsable app with no Lithos server
behind it — without needing a browser. The Playwright suite itself lives under
``e2e/`` and runs out of band (``make e2e``); here we prove the Python side.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from lithos_lens import main as main_module
from lithos_lens.config import EventsConfig, LithosConfig, load_config
from lithos_lens.errors import ConfigError
from lithos_lens.events import LensEvent
from lithos_lens.fake_dataset import FakeLithosDataset, demo_dataset
from lithos_lens.fake_lithos import (
    FakeEventHub,
    FakeLithosClient,
    fake_lithos_enabled,
)
from lithos_lens.lithos_client import LithosClient, LithosToolError
from lithos_lens.main import DEFAULT_HOST, DEFAULT_PORT, resolve_host, resolve_port
from lithos_lens.tasks import TaskRecord
from lithos_lens.web import create_app
from tests.conftest import load_contract


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        (" on ", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("maybe", False),
    ],
)
def test_fake_lithos_enabled_reads_env(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", value)
    assert fake_lithos_enabled() is expected


def test_fake_lithos_enabled_defaults_off_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITHOS_LENS_FAKE_LITHOS", raising=False)
    assert fake_lithos_enabled() is False


def test_create_app_uses_real_client_without_env(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITHOS_LENS_FAKE_LITHOS", raising=False)
    app = create_app(load_config(lithos_lens_config_env))
    # No lifespan/startup here so we never touch the network — just the wiring.
    assert isinstance(app.state.lens.lithos_client, LithosClient)


def test_create_app_uses_fake_client_when_env_enabled(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    assert isinstance(app.state.lens.lithos_client, FakeLithosClient)


def test_fake_mode_health_is_ok(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["lithos"] == "ok"


def test_fake_mode_dashboard_renders_fixture_tasks(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/tasks?since=2026-08-01")
    assert response.status_code == 200
    body = response.text
    assert "Cut over Influx ingest path" in body
    assert 'data-task-row data-task-id="influx-ingest-cutover"' in body
    # A completed and a cancelled fixture so all three groups have content.
    assert 'data-task-group="completed"' in body


def test_fake_mode_dashboard_renders_a_live_gates_section(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo fixture must exercise the Gates section, countdown included.

    A fixed ``ready_at`` drifts into the past, which is why the demo's timer
    gate is anchored RELATIVE to the process clock: without it the capture
    suites (and anyone browsing fake mode) would only ever see a gate section
    with no live timer in it — the one thing T1-S4 is about.
    """
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/tasks?since=2026-08-01")

    body = response.text
    assert 'data-task-group="gates"' in body
    # Two gate-type groups, human first: the demo shows the ordering rule.
    assert body.index('data-gate-group="human"') < body.index('data-gate-group="timer"')
    assert 'data-gate-row data-task-id="influx-read-swap-approval"' in body
    assert "blocks 1 task" in body
    # Advisory metadata is summarized on the row with the rest counted…
    assert "approval_required_from" in body
    assert "+1 more" in body

    # …and the timer gate publishes a STILL-FUTURE instant, which is what the
    # countdown ticks against and the one-shot self-refresh is scheduled for.
    match = re.search(r'data-gates-next-ready-at="([^"]+)"', body)
    assert match is not None
    assert datetime.fromisoformat(match.group(1)) > datetime.now(UTC)
    assert f'data-gate-ready-at="{match.group(1)}"' in body


def test_fake_mode_task_detail_renders(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/tasks/influx-ingest-cutover")
    assert response.status_code == 200
    assert 'data-task-detail="influx-ingest-cutover"' in response.text
    # The claimed fixture surfaces an active claim on the detail page.
    assert "worker-a" in response.text
    # T1-S7 graph surfaces: the demo dataset carries a discovered_from edge, so
    # the spawn provenance section lights up in fake mode too.
    assert 'data-link-list="spawned"' in response.text


def test_fake_mode_task_detail_renders_the_blocker_chain_and_breadcrumb(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo's blocked fixture exercises the other two T1-S7 graph surfaces:
    a level-1 blocker with live status, and the parent breadcrumb."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/tasks/influx-backfill")
    assert response.status_code == 200
    assert 'data-link-target="influx-ingest-cutover"' in response.text
    assert "data-parent-breadcrumb" in response.text


def test_fake_mode_note_renders(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/note/note-influx-plan")
    assert response.status_code == 200
    assert "Influx migration plan" in response.text


def test_fake_mode_startup_makes_no_outbound_requests(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake mode must be hermetic: swapping only LithosClient still left the
    real EventHub dialing the configured Lithos /events URL. Spy on the
    transport seam — any httpx.AsyncClient construction IS an outbound
    attempt (the SSE stream builds one before connecting)."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    constructed: list[str] = []
    real_async_client = httpx.AsyncClient

    class RecordingAsyncClient(real_async_client):
        def __init__(self, *args: object, **kwargs: object) -> None:
            constructed.append("httpx.AsyncClient")
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", RecordingAsyncClient)

    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        assert client.get("/tasks?since=2026-08-01").status_code == 200
        health = client.get("/health").json()

    assert isinstance(app.state.lens.events, FakeEventHub)
    # The live-updates surface stays honest: the hub is genuinely serving the
    # in-process stream, so it reports live rather than pretending "disabled".
    assert health["events"] == "live"
    assert constructed == []


@pytest.mark.anyio
async def test_fake_event_hub_serves_in_process_stream() -> None:
    """The hermetic hub genuinely serves subscribers (so the browser-facing
    /tasks/events endpoint and the live-updates banner keep functioning) —
    it just never dials upstream Lithos."""
    hub = FakeEventHub(EventsConfig(enabled=True), LithosConfig())
    await hub.start()
    try:
        assert hub.status == "live"
        queue = hub.subscribe()
        event = LensEvent(id="evt-1", type="task.created", task_id="t-1")
        await hub.publish(event)
        assert await asyncio.wait_for(queue.get(), timeout=0.1) == event
    finally:
        await hub.stop()
    assert hub.status == "disabled"


def test_fake_mode_dashboard_renders_terminal_groups(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The completed/cancelled fixtures must actually fall inside the suite's
    static since=2026-08-01 window and render as rows — not just the (always
    present) empty section wrappers."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/tasks?since=2026-08-01")
    assert response.status_code == 200
    body = response.text
    # Completed fixture row: id, title, terminal status metadata.
    assert 'data-task-id="lens-note-view" data-task-status="completed"' in body
    assert "Land knowledge note view" in body
    assert 'class="badge badge-completed"' in body
    # Cancelled fixture row.
    assert 'data-task-id="influx-spike" data-task-status="cancelled"' in body
    assert "Spike Influx client options" in body
    assert 'class="badge badge-cancelled"' in body
    # The graph cluster's resolved predecessors are inside the window too —
    # deliberately, since "a resolved predecessor inside the window" is one of
    # the graph fixtures — so each terminal group carries exactly two rows.
    assert 'data-task-id="loom-design-done" data-task-status="completed"' in body
    assert 'data-task-id="loom-cancelled-pred" data-task-status="cancelled"' in body
    assert body.count('data-task-status="completed"') == 2
    assert body.count('data-task-status="cancelled"') == 2


def test_fake_mode_missing_note_renders_not_found_banner(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixture finding 'finding-orphan' links knowledge_id='missing-note';
    opening it must exercise the intended not-found path (the 'Document not
    found.' banner), not the generic load-failure banner."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/note/missing-note")
    assert response.status_code == 200
    assert "Document not found." in response.text
    assert "Could not load this document from Lithos." not in response.text


def test_fake_mode_note_renders_related_panel(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K1-S4 note pages carry the related <aside>; the fake supplies a small
    neighborhood so the panel (and its direction/conflict badges) light up in
    fake mode / e2e instead of rendering empty."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/note/note-influx-plan")
    assert response.status_code == 200
    assert 'aria-label="Related notes"' in response.text
    assert "Influx rollback route" in response.text
    assert 'edge-direction">incoming' in response.text
    assert "conflict: unresolved" in response.text


@pytest.mark.anyio
async def test_fake_client_read_note_missing_raises_doc_not_found() -> None:
    """Parity with the concrete client: upstream lithos_read answers a missing
    doc with an error envelope code 'doc_not_found', which LithosClient raises
    as a coded LithosToolError — the fake must speak the same contract."""
    client = FakeLithosClient()
    with pytest.raises(LithosToolError) as excinfo:
        await client.read_note("missing-note")
    assert excinfo.value.code == "doc_not_found"


@pytest.mark.anyio
async def test_fake_client_read_note_honors_max_length() -> None:
    client = FakeLithosClient()
    note = await client.read_note("note-influx-plan", max_length=1)
    assert note is not None
    assert len(note.content) == 1
    # Truncation must not mutate the stored fixture.
    full = await client.read_note("note-influx-plan")
    assert full is not None and len(full.content) > 1


@pytest.mark.anyio
async def test_fake_client_related_returns_neighborhood_for_fixture_note() -> None:
    client = FakeLithosClient()
    neighborhood = await client.related("note-influx-plan")
    assert any(ref.id == "note-influx-rollback" for ref in neighborhood.links)
    assert any(
        ref.conflict_state == "unresolved" and ref.direction == "incoming"
        for ref in neighborhood.edges
    )
    # Unknown ids: production lithos_related answers doc_not_found — the
    # fake speaks the same coded contract, not an invented empty read.
    with pytest.raises(LithosToolError) as excinfo:
        await client.related("missing-note")
    assert excinfo.value.code == "doc_not_found"


@pytest.mark.anyio
async def test_fake_read_note_by_path_maps_distinct_path_to_note_id() -> None:
    """The fake dataset carries EXPLICIT note paths distinct from note ids, so
    the dominant path->UUID wiki workflow is testable realistically (a fake
    that equates path stem with id makes every path-probe test tautological)."""
    client = FakeLithosClient()

    note = await client.read_note_by_path("runbooks/influx-rollback.md")

    assert note is not None
    assert note.id == "note-influx-rollback"
    # The mapping is genuinely path->id, not an identity: the path stem is NOT
    # the note id.
    assert note.id != "runbooks/influx-rollback"


@pytest.mark.anyio
async def test_fake_read_note_by_path_miss_returns_none() -> None:
    """Probe-miss parity with the concrete client, which maps the
    doc_not_found envelope to None on this path-probe read."""
    client = FakeLithosClient()

    assert await client.read_note_by_path("no/such/path.md") is None
    # A note ID is not a path: the old stem==id shortcut must be gone.
    assert await client.read_note_by_path("note-influx-rollback.md") is None


@pytest.mark.anyio
async def test_fake_list_notes_rows_carry_their_explicit_paths() -> None:
    client = FakeLithosClient()

    rows = {row.id: row.path for row in await client.list_notes()}

    assert rows["note-influx-plan"] == "plans/influx-migration.md"
    assert rows["note-influx-rollback"] == "runbooks/influx-rollback.md"


def test_resolve_port_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LENS_PORT", raising=False)
    assert resolve_port() == DEFAULT_PORT


def test_resolve_port_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LENS_PORT", "8123")
    assert resolve_port() == 8123


@pytest.mark.parametrize("bad", ["0", "70000", "-1", "notaport"])
def test_resolve_port_rejects_invalid(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("LENS_PORT", bad)
    with pytest.raises(ConfigError):
        resolve_port()


def test_resolve_host_defaults_to_every_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container posture is the default and must stay it.

    Lens is published on a port and reached across the trusted-network boundary
    (REQUIREMENTS §5C.1). A loopback default would leave a correctly configured
    container answering nothing, so narrowing the bind is opt-IN.
    """
    monkeypatch.delenv("LENS_HOST", raising=False)
    assert resolve_host() == DEFAULT_HOST == "0.0.0.0"  # nosec B104


@pytest.mark.parametrize("value", ["127.0.0.1", "  127.0.0.1  ", "::1"])
def test_resolve_host_honors_env(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LENS_HOST", value)
    assert resolve_host() == value.strip()


@pytest.mark.parametrize("blank", ["", "   "])
def test_resolve_host_treats_blank_as_unset(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # Same shape as LENS_PORT: an env var set to nothing is not an instruction.
    monkeypatch.setenv("LENS_HOST", blank)
    assert resolve_host() == DEFAULT_HOST


def test_main_binds_the_host_and_port_it_resolved(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver nothing calls would close nothing.

    The bind is one line in ``main`` and no other test reaches it, so a change
    that resolved the host and then passed a literal anyway would leave every
    other test here green while the listener went back on every interface.
    """
    monkeypatch.setenv("LENS_HOST", "127.0.0.1")
    monkeypatch.setenv("LENS_PORT", "8123")
    monkeypatch.setattr(main_module, "configure_logging", lambda level: None)
    recorded: dict[str, object] = {}
    monkeypatch.setattr(
        main_module.uvicorn,
        "run",
        lambda *args, **kwargs: recorded.update(kwargs),
    )

    main_module.main()

    assert recorded["host"] == "127.0.0.1"
    assert recorded["port"] == 8123


def _web_server_entries(source: str) -> list[str]:
    """The text of each ``webServer`` entry in the Playwright config.

    Sliced between ``command:`` keys — one per entry — so an entry that carries
    no ``env`` block at all is an entry with nothing in it, rather than one this
    scan never looks at.
    """
    block = source[source.index("webServer:") :]
    starts = [match.start() for match in re.finditer(r"^\s*command:", block, re.M)]
    bounds = [*starts, len(block)]
    return [block[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def test_the_e2e_harness_binds_every_instance_to_loopback() -> None:
    """``make e2e`` must not put an unauthenticated write seam on the network.

    Fake mode registers ``POST /tasks/events/publish`` — no auth, no Origin
    check — and the suite runs two instances of it. On every interface, anyone
    on the segment could fan an event into the tabs being photographed, and
    those artifacts are read as evidence by loom's visual review.

    Asserted against the config file rather than the running servers because
    that is where the decision lives, and because a Python test runs on every
    ``make check`` while the harness itself does not.
    """
    config = Path(__file__).resolve().parents[1] / "e2e/playwright.config.ts"
    entries = _web_server_entries(config.read_text())

    assert len(entries) >= 2, "the truncation instance is a second server"
    for index, entry in enumerate(entries):
        pinned = re.search(r'LENS_HOST:\s*"([^"]+)"', entry)
        assert pinned, f"webServer entry {index} does not pin LENS_HOST"
        assert pinned.group(1) in {"127.0.0.1", "localhost", "::1"}, (
            f"webServer entry {index} binds {pinned.group(1)!r}, not loopback"
        )


def test_the_truncation_instance_limit_still_separates_the_two_frontiers() -> None:
    """The truncated-board capture only proves anything at the right limit.

    ``e2e/servers.ts`` picks a ``frontier_limit`` that sits BETWEEN the demo's
    two frontier sizes, so one read comes back capped and the other complete —
    that pairing is the whole subject of the per-side marking the capture
    photographs, and at a limit below both sides it degrades to the board-wide
    banner that was already there.

    The coupling is invisible from either file alone: growing the fixtures
    (T2-A1's graph cluster did) silently moves which side overflows. Checked
    here rather than only in Playwright because `make check` runs on every
    change and `make e2e` does not — this exact drift shipped once already.
    """
    repo_root = Path(__file__).resolve().parents[1]
    servers = (repo_root / "e2e/servers.ts").read_text()
    match = re.search(r'TRUNCATED_FRONTIER_LIMIT = "(\d+)"', servers)
    assert match, "e2e/servers.ts no longer declares TRUNCATED_FRONTIER_LIMIT"
    limit = int(match.group(1))

    dataset = demo_dataset()
    open_ids = {task.id for task in dataset.tasks if task.status == "open"}
    ready = len(open_ids & set(dataset.ready_ids))
    blocked = len(open_ids & set(dataset.blocked))

    # A read is truncated exactly when it comes back holding `limit` rows.
    capped = {"ready": ready >= limit, "blocked": blocked >= limit}
    truncating = [side for side, is_capped in capped.items() if is_capped]
    assert len(truncating) == 1, (
        f"frontier_limit {limit} against {ready} ready / {blocked} blocked "
        f"caps {truncating or 'neither side'}; the capture needs exactly one. "
        "Pick a limit strictly between the two counts and update "
        "e2e/tests/screenshots.spec.ts to name that side."
    )

    # ...and the capture must assert the side that actually caps.
    spec = (repo_root / "e2e/tests/screenshots.spec.ts").read_text()
    assert f"{truncating[0]} frontier truncated at" in spec, (
        f"the {truncating[0]} frontier is the one that truncates, but "
        "screenshots.spec.ts asserts a different side's banner"
    )


@pytest.mark.anyio
async def test_fake_client_task_get_missing_raises_coded_error() -> None:
    client = FakeLithosClient()
    with pytest.raises(LithosToolError) as excinfo:
        await client.task_get("does-not-exist")
    assert excinfo.value.code == "task_not_found"


@pytest.mark.anyio
async def test_fake_client_ready_frontier_includes_the_claimed_task() -> None:
    """The advertised 'ready, claimed, in-flight' fixture must be on the frontier."""
    client = FakeLithosClient()
    ready = await client.task_ready(with_claims=True)
    by_id = {task.id: task for task in ready}
    assert "influx-ingest-cutover" in by_id
    # It is the one carrying claims, so with_claims must surface them.
    claimed = by_id["influx-ingest-cutover"]
    assert claimed.claims is not None and len(claimed.claims) == 1
    assert claimed.claims[0].agent == "worker-a"


@pytest.mark.anyio
async def test_fake_client_task_ready_scoped_by_project_and_tags() -> None:
    client = FakeLithosClient()

    all_ids = {t.id for t in await client.task_ready()}
    # Both demo clusters: the influx (dashboard) one and the loom (graph) one.
    assert all_ids == {
        "influx-ingest-cutover",
        "influx-dashboards",
        "lens-graph-view",
        "influx-ingest-old",
        "loom-schema",
        "loom-docs-tidy",
        "loom-metrics-note",
    }

    influx = {t.id for t in await client.task_ready(project="influx")}
    assert influx == {
        "influx-ingest-cutover",
        "influx-dashboards",
        "influx-ingest-old",
    }

    observability = {t.id for t in await client.task_ready(tags=["area:observability"])}
    assert observability == {"influx-dashboards"}

    assert await client.task_ready(project="missing") == []


@pytest.mark.anyio
async def test_fake_client_task_blocked_scoped_by_project_and_tags() -> None:
    client = FakeLithosClient()

    assert [b.task.id for b in await client.task_blocked(project="influx")] == [
        "influx-backfill"
    ]
    assert [b.task.id for b in await client.task_blocked(tags=["area:data"])] == [
        "influx-backfill"
    ]
    # influx-backfill lacks these, so a scoped read must exclude it.
    assert await client.task_blocked(tags=["area:observability"]) == []
    # The graph cluster's cross-project dependent is the one lens-tagged
    # blocked row: a loom task blocks it, which is what makes each project's
    # graph render the other endpoint as a ghost.
    assert [b.task.id for b in await client.task_blocked(project="lithos-lens")] == [
        "lens-graph-page"
    ]


def test_fake_client_defaults_to_the_demo_dataset() -> None:
    """The app-factory path (FakeLithosClient(config)) must keep serving the
    shipped demo set — the fixture/behavior split may not change fake mode."""
    assert FakeLithosClient().dataset == demo_dataset()


def test_demo_timestamps_keep_the_contracts_second_precision() -> None:
    """Fixtures reproduce the canonical payloads, not approximations of them
    (AGENTS.md): every vendored contract stamps whole seconds, so the demo's
    now-relative timestamps must too. ``datetime.now(UTC)`` carries
    microseconds, and the surfaces that render a timestamp verbatim — the task
    detail page — would show a six-digit fraction no Lithos record can produce
    (and wrap it mid-value at 320px)."""
    dataset = demo_dataset()
    stamps = [task.created_at for task in dataset.tasks]
    stamps += [task.resolved_at for task in dataset.tasks if task.resolved_at]
    stamps += [
        claim.expires_at for claims in dataset.claims.values() for claim in claims
    ]
    stamps += [
        finding.created_at
        for findings in dataset.findings.values()
        for finding in findings
    ]
    assert stamps, "no fixture timestamps found — the sweep would pass vacuously"

    # Shape taken from the contract itself, so the pattern cannot drift from
    # what the server actually emits.
    canonical = load_contract("lithos_task_list")["responses"]["success"]["tasks"][0]
    pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")
    assert pattern.fullmatch(canonical["created_at"])
    for value in stamps:
        assert pattern.fullmatch(value), f"not contract-shaped: {value!r}"


def test_demo_dataset_is_deterministic() -> None:
    assert demo_dataset() == demo_dataset()


def test_demo_dataset_classifies_every_open_workable_task() -> None:
    """Regression (f-003): every open workable (task-typed) row in the demo set
    must sit on exactly one frontier. A workable open task in neither ready_ids
    nor blocked would land in the Not-classified tail and make the rendered
    dashboard claim a false "frontier truncated at 500" on a five-task corpus."""
    dataset = demo_dataset()
    workable_open = {
        task.id
        for task in dataset.tasks
        if task.status == "open" and task.task_type == "task"
    }
    classified = set(dataset.ready_ids) | set(dataset.blocked)
    assert workable_open - classified == set()


@pytest.mark.anyio
async def test_fake_client_serves_a_composed_dataset() -> None:
    """The point of the fixture/behavior split: a test can hand the client its
    own minimal dataset instead of inheriting (and filtering around) the demo
    set, while the behavior half keeps the coded error contracts."""
    task = TaskRecord(
        id="only-task",
        title="The only task",
        status="open",
        created_by="composer",
        created_at="2026-08-01T00:00:00+00:00",
        tags=("project:compose",),
    )
    client = FakeLithosClient(
        dataset=FakeLithosDataset(tasks=(task,), ready_ids=frozenset({"only-task"}))
    )

    assert [t.id for t in await client.list_tasks()] == ["only-task"]
    assert [t.id for t in await client.task_ready()] == ["only-task"]
    assert await client.task_blocked() == []
    assert (await client.task_get("only-task")).title == "The only task"
    # Claims were not composed, so with_claims surfaces an empty tuple.
    (ready,) = await client.task_ready(with_claims=True)
    assert ready.claims == ()
    # The behavior half still speaks the coded contracts over any dataset.
    with pytest.raises(LithosToolError) as excinfo:
        await client.read_note("not-composed")
    assert excinfo.value.code == "doc_not_found"


@pytest.mark.anyio
async def test_fake_list_findings_since_uses_full_timestamp_strict_greater() -> None:
    """Parity with upstream lithos_finding_list: `since` is parsed as a full
    ISO datetime (naive == UTC, offsets normalized) and filters strictly
    created_at > since — not an inclusive calendar-date compare."""
    client = FakeLithosClient()
    task = "influx-ingest-cutover"
    # Derived from the fixture, not restated: the demo timestamps are relative
    # to the run (so the fixtures stay "recent" for the attention rules), and a
    # hard-coded date here would pin them back into the past.
    plan, orphan = demo_dataset().findings[task]
    plan_at = datetime.fromisoformat(plan.created_at)

    async def ids(since: str) -> list[str]:
        return [f.id for f in await client.list_findings(task, since=since)]

    # Between the two findings: only the later one passes.
    assert await ids((plan_at + timedelta(minutes=1)).isoformat()) == ["finding-orphan"]
    # Exactly equal to the earlier finding: strict >, so it is excluded.
    assert await ids(plan.created_at) == ["finding-orphan"]
    # Before both: both pass.
    assert await ids((plan_at - timedelta(minutes=1)).isoformat()) == [
        "finding-plan",
        "finding-orphan",
    ]
    # Offset-aware: the same instant expressed in +02:00 must compare equal
    # (strict >, so the earlier finding is still excluded).
    assert await ids(plan_at.astimezone(timezone(timedelta(hours=2))).isoformat()) == [
        "finding-orphan"
    ]
    # Naive values are treated as already-UTC (upstream normalize_datetime).
    assert await ids(plan_at.replace(tzinfo=None).isoformat()) == ["finding-orphan"]
    # After both: nothing.
    assert (
        await ids(
            (
                datetime.fromisoformat(orphan.created_at) + timedelta(minutes=1)
            ).isoformat()
        )
        == []
    )


@pytest.mark.anyio
async def test_fake_list_findings_malformed_since_raises_invalid_input() -> None:
    """Upstream parses `since` with datetime.fromisoformat and answers a
    ValueError with the invalid_input envelope; the fake must match."""
    client = FakeLithosClient()
    with pytest.raises(LithosToolError) as excinfo:
        await client.list_findings("influx-ingest-cutover", since="not-a-timestamp")
    assert excinfo.value.code == "invalid_input"


@pytest.mark.anyio
async def test_fake_list_tasks_since_compares_full_strings_inclusive() -> None:
    """Upstream lithos_task_list filters `created_at >= since` on the raw ISO
    strings (inclusive, full precision — deliberately unlike findings' strict
    parsed >). A same-day-but-earlier task must therefore be excluded."""
    client = FakeLithosClient()
    dashboards = next(
        task for task in demo_dataset().tasks if task.id == "influx-dashboards"
    )
    created_at = datetime.fromisoformat(dashboards.created_at)
    after = {
        t.id
        for t in await client.list_tasks(
            since=(created_at + timedelta(seconds=1)).isoformat()
        )
    }
    assert "influx-dashboards" not in after
    # Exactly equal: the compare is inclusive, so the row survives.
    boundary = {t.id for t in await client.list_tasks(since=dashboards.created_at)}
    assert "influx-dashboards" in boundary


def test_dataset_mappings_are_read_only_views() -> None:
    """FakeLithosDataset promises an immutable bundle: its mapping fields are
    read-only at runtime, not just unassignable dataclass attributes."""
    dataset = FakeLithosDataset()
    with pytest.raises(TypeError):
        dataset.stats["mutated"] = 1  # type: ignore[index]


def test_dataset_copies_constructor_mappings() -> None:
    """A caller-held dict must not remain an alias into the dataset."""
    source = {"open_claims": 2}
    dataset = FakeLithosDataset(stats=source)
    source["open_claims"] = 99
    assert dataset.stats["open_claims"] == 2


def test_fake_mode_logs_loud_warning_when_engaged(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    with caplog.at_level("WARNING", logger="lithos_lens.web"):
        create_app(load_config(lithos_lens_config_env))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("fake-Lithos app mode is ENABLED" in r.message for r in warnings)


def test_no_warning_when_fake_mode_off(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("LITHOS_LENS_FAKE_LITHOS", raising=False)
    with caplog.at_level("WARNING", logger="lithos_lens.web"):
        create_app(load_config(lithos_lens_config_env))
    assert not [
        r for r in caplog.records if "fake-Lithos app mode is ENABLED" in r.getMessage()
    ]


def test_fake_mode_event_publish_seam_reaches_subscribers(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /tasks/events/publish (fake-mode-only harness seam) pushes a real
    LensEvent through the in-process hub to /tasks/events subscribers."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        hub = app.state.lens.events
        queue = hub.subscribe()
        response = client.post(
            "/tasks/events/publish",
            json={
                "id": "evt-77",
                "type": "task.created",
                "task_id": "brand-new",
                "payload": {"title": "Brand new"},
            },
        )
        event = queue.get_nowait()
    assert response.status_code == 202
    assert event.id == "evt-77"
    assert event.type == "task.created"
    assert event.task_id == "brand-new"


@pytest.mark.parametrize("forged", ["lens.refresh", "task.exploded"])
def test_fake_mode_event_publish_seam_refuses_types_lithos_never_sends(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch, forged: str
) -> None:
    """The seam is unauthenticated and CSRF-reachable, and since T2 it drives
    server state: the hub invalidates the graph cache before fanning out, so a
    forged ``lens.refresh`` would flush the whole cache (and a forged task type
    would evict an entry) on a cross-origin POST. Only the types Lithos itself
    sends are accepted — `lens.*` is the hub's own synthetic namespace."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        hub = app.state.lens.events
        queue = hub.subscribe()
        response = client.post(
            "/tasks/events/publish", json={"type": forged, "task_id": "x"}
        )

    assert response.status_code == 400
    assert queue.empty()


def test_event_publish_seam_absent_outside_fake_mode(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITHOS_LENS_FAKE_LITHOS", raising=False)
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.post("/tasks/events/publish", json={"task_id": "x"})
    assert response.status_code in (404, 405)


def test_fake_mode_dashboard_has_the_pending_strip(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tasks.js targets [data-task-list="pending"] for task.created skeletons
    (the old data-task-list="open" target died with the sectioned board)."""
    monkeypatch.setenv("LITHOS_LENS_FAKE_LITHOS", "1")
    app = create_app(load_config(lithos_lens_config_env))
    with TestClient(app) as client:
        response = client.get("/tasks?since=2026-08-01")
    assert response.status_code == 200
    assert 'data-task-list="pending"' in response.text
