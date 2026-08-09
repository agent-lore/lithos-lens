"""Fake-Lithos app mode: the launchable, server-backed fixture client.

These cover the seam the ``e2e/`` Playwright smoke suite depends on — that the
real application factory can boot a fully browsable app with no Lithos server
behind it — without needing a browser. The Playwright suite itself lives under
``e2e/`` and runs out of band (``make e2e``); here we prove the Python side.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

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
from lithos_lens.main import DEFAULT_PORT, resolve_port
from lithos_lens.tasks import TaskRecord
from lithos_lens.web import create_app


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
    # Exactly one row in each terminal group.
    assert body.count('data-task-status="completed"') == 1
    assert body.count('data-task-status="cancelled"') == 1


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
    assert all_ids == {"influx-ingest-cutover", "influx-dashboards", "lens-graph-view"}

    influx = {t.id for t in await client.task_ready(project="influx")}
    assert influx == {"influx-ingest-cutover", "influx-dashboards"}

    observability = {t.id for t in await client.task_ready(tags=["area:observability"])}
    assert observability == {"influx-dashboards"}

    assert await client.task_ready(project="missing") == []


@pytest.mark.anyio
async def test_fake_client_task_blocked_scoped_by_project_and_tags() -> None:
    client = FakeLithosClient()

    assert [b.task.id for b in await client.task_blocked()] == ["influx-backfill"]
    assert [b.task.id for b in await client.task_blocked(tags=["area:data"])] == [
        "influx-backfill"
    ]
    # influx-backfill lacks these, so a scoped read must exclude it.
    assert await client.task_blocked(tags=["area:observability"]) == []
    assert await client.task_blocked(project="lithos-lens") == []


def test_fake_client_defaults_to_the_demo_dataset() -> None:
    """The app-factory path (FakeLithosClient(config)) must keep serving the
    shipped demo set — the fixture/behavior split may not change fake mode."""
    assert FakeLithosClient().dataset == demo_dataset()


def test_demo_dataset_is_deterministic() -> None:
    assert demo_dataset() == demo_dataset()


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
    task = "influx-ingest-cutover"  # demo findings at 11:30Z and 12:15Z

    async def ids(since: str) -> list[str]:
        return [f.id for f in await client.list_findings(task, since=since)]

    # Same-day, between the two findings: only the later one passes.
    assert await ids("2026-08-06T12:00:00+00:00") == ["finding-orphan"]
    # Exactly equal to the earlier finding: strict >, so it is excluded.
    assert await ids("2026-08-06T11:30:00+00:00") == ["finding-orphan"]
    # Before both: both pass.
    assert await ids("2026-08-06T10:00:00+00:00") == ["finding-plan", "finding-orphan"]
    # Offset-aware: 13:15+02:00 is 11:15Z, before both.
    assert await ids("2026-08-06T13:15:00+02:00") == ["finding-plan", "finding-orphan"]
    # Naive values are treated as already-UTC (upstream normalize_datetime).
    assert await ids("2026-08-06T12:00:00") == ["finding-orphan"]
    # After both: nothing.
    assert await ids("2026-08-07T00:00:00+00:00") == []


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
    # influx-dashboards was created 2026-08-04T09:00:00+00:00.
    after = {t.id for t in await client.list_tasks(since="2026-08-04T10:00:00+00:00")}
    assert "influx-dashboards" not in after
    boundary = {
        t.id for t in await client.list_tasks(since="2026-08-04T09:00:00+00:00")
    }
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
