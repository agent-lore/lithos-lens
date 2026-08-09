"""K1-S4 Related panel behavior tests."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import (
    ConfigError,
    KnowledgeConfig,
    LithosConfig,
    load_config,
)
from lithos_lens.knowledge import (
    RELATED_RENDER_CAP,
    RelatedNeighborhood,
    RelatedRef,
    SearchResult,
    load_related_panel,
    normalize_related,
)
from lithos_lens.lithos_client import LithosClient, LithosHealth, LithosToolError
from lithos_lens.task_graph import BlockedTaskRecord, EdgeRecord
from lithos_lens.tasks import (
    AgentRecord,
    FindingRecord,
    NoteRecord,
    NoteSummary,
    SectionState,
    TaskRecord,
    TaskStatusRecord,
)
from lithos_lens.web import create_app
from tests.conftest import load_contract


class KnowledgeFakeLithosClient:
    """Fake exercising only the note-view surface used by the related panel."""

    def __init__(
        self,
        *,
        neighborhood: RelatedNeighborhood | None = None,
        titles: dict[str, str] | None = None,
        note: NoteRecord | None = None,
        related_error: bool = False,
        health: LithosHealth = "ok",
    ) -> None:
        self.neighborhood = neighborhood or RelatedNeighborhood()
        self.titles = titles or {}
        self.note = note
        self.related_error = related_error
        self.health_value: LithosHealth = health
        self.read_calls: list[tuple[str, int | None]] = []
        self.related_calls: list[str] = []
        self.closed = False

    async def startup(self) -> None:
        return None

    async def health(self) -> LithosHealth:
        return self.health_value

    async def register_agent(self) -> bool:
        return True

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        self.read_calls.append((knowledge_id, max_length))
        if self.note is not None and knowledge_id == self.note.id:
            return self.note
        title = self.titles.get(knowledge_id)
        if title is None:
            return None
        return NoteRecord(id=knowledge_id, title=title, content="")

    async def read_note_by_path(self, path: str) -> NoteRecord | None:
        return None

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        self.related_calls.append(knowledge_id)
        if self.related_error:
            raise RuntimeError("related unavailable")
        return self.neighborhood

    async def list_notes(
        self,
        *,
        title_contains: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]:
        return []

    async def search_notes(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[SearchResult]:
        return []

    # ── unused task surface (present only to satisfy LithosClientProtocol) ──

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        with_claims: bool = False,
    ) -> list[TaskRecord]:
        return []

    async def task_status(self, task_id: str) -> TaskStatusRecord | None:
        return None

    async def task_ready(
        self,
        *,
        limit: int | None = None,
        with_claims: bool = False,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[TaskRecord]:
        return []

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]:
        return []

    async def task_get(self, task_id: str) -> TaskRecord:
        # Same not-found contract as the concrete client: coded error, not None.
        raise LithosToolError(f"Task '{task_id}' not found.", code="task_not_found")

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]:
        return []

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]:
        return []

    async def list_findings(
        self, task_id: str, *, since: str | None = None
    ) -> list[FindingRecord]:
        return []

    async def stats(self) -> dict[str, object]:
        return {}

    async def list_agents(self) -> list[AgentRecord]:
        return []

    async def close(self) -> None:
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


# ── normalizer ─────────────────────────────────────────────────────────
#
# REAL_RELATED_PAYLOAD is the canonical lithos_related response from the
# vendored contract (tests/contracts/lithos_related.json — the authoritative
# payload shapes; see tests/contracts/README.md): nested ``links``/``edges``
# with ``outgoing``/``incoming`` arrays and full edges.db rows, NOT an
# invented flat shape.


def _edge_row(**overrides: Any) -> dict[str, Any]:
    """A full edges.db row as lithos_related returns it (all 12 columns)."""
    row: dict[str, Any] = {
        "edge_id": "edge-row",
        "from_id": "root",
        "to_id": "other",
        "type": "supports",
        "weight": 0.75,
        "namespace": "default",
        "created_at": "2026-05-01T00:00:00+00:00",
        "updated_at": "2026-05-01T00:00:00+00:00",
        "provenance_actor": "agent-x",
        "provenance_type": "asserted",
        "evidence": "",
        "conflict_state": "",
    }
    row.update(overrides)
    return row


REAL_RELATED_PAYLOAD: dict[str, Any] = load_contract("lithos_related")["responses"][
    "success"
]


def test_normalize_related_parses_the_real_nested_payload() -> None:
    neighborhood = normalize_related(REAL_RELATED_PAYLOAD)

    assert neighborhood.links == (RelatedRef(id="out-1", title="Outgoing Note"),)
    assert neighborhood.backlinks == (RelatedRef(id="in-1", title="Incoming Note"),)
    assert neighborhood.sources == (RelatedRef(id="src-1", title="Source Note"),)
    assert neighborhood.derived == (RelatedRef(id="der-1", title="Derived Note"),)
    assert neighborhood.unresolved == ("drafts/missing.md",)


def test_normalize_related_outgoing_edge_selects_to_id_endpoint() -> None:
    neighborhood = normalize_related(
        {"edges": {"outgoing": [_edge_row(from_id="root", to_id="edge-out")]}}
    )

    assert neighborhood.edges == (
        RelatedRef(
            id="edge-out",
            edge_type="supports",
            weight=0.75,
            direction="outgoing",
            conflict_state="",
        ),
    )


def test_normalize_related_incoming_edge_selects_from_id_endpoint() -> None:
    neighborhood = normalize_related(
        {
            "edges": {
                "incoming": [
                    _edge_row(
                        from_id="edge-in",
                        to_id="root",
                        type="contradicts",
                        weight=0.9,
                        conflict_state="unresolved",
                    )
                ]
            }
        }
    )

    assert neighborhood.edges == (
        RelatedRef(
            id="edge-in",
            edge_type="contradicts",
            weight=0.9,
            direction="incoming",
            conflict_state="unresolved",
        ),
    )


def test_normalize_related_preserves_direction_type_weight_conflict_state() -> None:
    """REQUIREMENTS.md §6.5: edge records carry type, weight and conflict_state;
    direction distinguishes the two fan-out halves. All survive normalization."""
    neighborhood = normalize_related(REAL_RELATED_PAYLOAD)

    outgoing, incoming = neighborhood.edges
    assert (outgoing.direction, outgoing.edge_type, outgoing.weight) == (
        "outgoing",
        "supports",
        0.75,
    )
    assert incoming.conflict_state == "unresolved"
    assert incoming.direction == "incoming"


def test_normalize_related_omitted_sections_normalize_empty() -> None:
    """Sections not in ``include`` are omitted from the response entirely."""
    neighborhood = normalize_related(
        {
            "id": "root",
            "included": ["links"],
            "links": {"outgoing": [], "incoming": []},
        }
    )

    assert neighborhood.links == ()
    assert neighborhood.backlinks == ()
    assert neighborhood.sources == ()
    assert neighborhood.derived == ()
    assert neighborhood.unresolved == ()
    assert neighborhood.edges == ()


def test_normalize_related_tolerates_missing_and_malformed_fields() -> None:
    neighborhood = normalize_related(
        {
            "links": "nope",
            "provenance": [],
            "edges": {"outgoing": [42, {}], "incoming": "bad"},
        }
    )

    assert neighborhood.links == ()
    assert neighborhood.backlinks == ()
    assert neighborhood.edges == ()
    assert neighborhood.unresolved == ()


# ── load_related_panel ─────────────────────────────────────────────────


def test_related_panel_resolves_titles_and_lists_backlinks() -> None:
    neighborhood = RelatedNeighborhood(
        links=(RelatedRef(id="out-1"),),
        backlinks=(RelatedRef(id="in-1"), RelatedRef(id="in-2")),
        sources=(RelatedRef(id="src-1"),),
        edges=(RelatedRef(id="edge-1", edge_type="supports", weight=0.5),),
    )
    titles = {
        "out-1": "Outgoing Note",
        "in-1": "First Backlink",
        "in-2": "Second Backlink",
        "src-1": "Source Note",
        "edge-1": "Edge Note",
    }
    fake = KnowledgeFakeLithosClient(neighborhood=neighborhood, titles=titles)

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20, render_cap=50))

    assert panel.state == SectionState.OK
    assert [item.label for item in panel.backlinks.items] == [
        "First Backlink",
        "Second Backlink",
    ]
    assert panel.links.items[0].label == "Outgoing Note"
    assert panel.sources.items[0].label == "Source Note"
    assert panel.edges.items[0].edge_type == "supports"
    assert panel.edges.items[0].weight == 0.5
    # Title fan-out uses the cheap max_length=1 read.
    assert all(max_length == 1 for _, max_length in fake.read_calls)


def test_related_panel_caps_title_fanout_and_reports_overflow() -> None:
    edges = tuple(RelatedRef(id=f"edge-{i}", edge_type="supports") for i in range(25))
    titles = {f"edge-{i}": f"Edge {i}" for i in range(25)}
    fake = KnowledgeFakeLithosClient(
        neighborhood=RelatedNeighborhood(edges=edges), titles=titles
    )

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20, render_cap=50))

    assert len(panel.edges.items) == 20
    assert panel.edges.overflow == 5
    # Only the capped set of ids is looked up, not all 25.
    assert len(fake.read_calls) == 20


def test_related_panel_renders_bare_id_when_title_unresolved() -> None:
    fake = KnowledgeFakeLithosClient(
        neighborhood=RelatedNeighborhood(links=(RelatedRef(id="ghost"),)),
        titles={},
    )

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20, render_cap=50))

    # Within the cap but unresolvable -> rendered as a bare id, not overflow.
    assert panel.links.items[0].label == "ghost"
    assert panel.links.overflow == 0


def test_related_panel_degrades_when_related_call_fails() -> None:
    fake = KnowledgeFakeLithosClient(related_error=True)

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20, render_cap=50))

    assert panel.state == SectionState.ERROR
    assert panel.is_empty


def test_related_panel_uses_inline_title_beyond_fanout_cap() -> None:
    """Regression for f-001: an inline title renders even past the cap and does
    not spend fan-out budget."""
    links = tuple(RelatedRef(id=f"n-{i}", title=f"Title {i}") for i in range(25))
    fake = KnowledgeFakeLithosClient(neighborhood=RelatedNeighborhood(links=links))

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20, render_cap=50))

    assert len(panel.links.items) == 25
    assert panel.links.overflow == 0
    assert panel.links.items[24].label == "Title 24"
    # Inline titles need no lookup, so nothing is fanned out.
    assert fake.read_calls == []


def test_related_panel_render_cap_collapses_excess_inline_titles() -> None:
    """Regression for security/f-002: inline titles no longer bypass a lens-side
    render bound — excess collapses into overflow (+N more), re-bounding page
    size for a highly connected hub note."""
    links = tuple(RelatedRef(id=f"n-{i}", title=f"Title {i}") for i in range(30))
    fake = KnowledgeFakeLithosClient(neighborhood=RelatedNeighborhood(links=links))

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20, render_cap=10))

    assert len(panel.links.items) == 10
    assert panel.links.overflow == 20
    # Still no fan-out — the render cap is independent of backend calls.
    assert fake.read_calls == []


def test_related_panel_provenance_overflow_is_reported() -> None:
    """Regression for f-002: sources collapsed past the cap still surface as
    provenance overflow rather than vanishing."""
    links = tuple(RelatedRef(id=f"link-{i}") for i in range(20))
    sources = tuple(RelatedRef(id=f"src-{i}") for i in range(5))
    titles = {f"link-{i}": f"Link {i}" for i in range(20)}
    fake = KnowledgeFakeLithosClient(
        neighborhood=RelatedNeighborhood(links=links, sources=sources),
        titles=titles,
    )

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20, render_cap=50))

    assert panel.sources.items == ()
    assert panel.sources.overflow == 5
    assert panel.has_provenance


# ── note page integration ──────────────────────────────────────────────


def _client(config_path: Path, fake: KnowledgeFakeLithosClient) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def test_note_page_renders_related_panel_sections(
    lithos_lens_config_env: Path,
) -> None:
    note = NoteRecord(id="root", title="Root Note", content="Body text.")
    neighborhood = RelatedNeighborhood(
        backlinks=(RelatedRef(id="in-1"),),
        sources=(RelatedRef(id="src-1"),),
        edges=(RelatedRef(id="edge-1", edge_type="supports", weight=0.9),),
    )
    titles = {
        "in-1": "Incoming Note",
        "src-1": "Provenance Source",
        "edge-1": "Edge Target Note",
    }
    fake = KnowledgeFakeLithosClient(
        neighborhood=neighborhood, titles=titles, note=note
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/root")

    assert response.status_code == 200
    assert "Back-links" in response.text
    assert "Incoming Note" in response.text
    assert "Provenance" in response.text
    assert "Provenance Source" in response.text
    assert "Typed edges" in response.text
    assert "Edge Target Note" in response.text
    assert "supports" in response.text


def test_note_page_related_panel_shows_overflow_note(
    lithos_lens_config_env: Path,
) -> None:
    note = NoteRecord(id="root", title="Root Note", content="Body.")
    edges = tuple(RelatedRef(id=f"edge-{i}", edge_type="supports") for i in range(25))
    titles = {f"edge-{i}": f"Edge {i}" for i in range(25)}
    fake = KnowledgeFakeLithosClient(
        neighborhood=RelatedNeighborhood(edges=edges), titles=titles, note=note
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/root")

    assert response.status_code == 200
    assert "+5 more" in response.text


def test_note_page_survives_related_panel_failure(
    lithos_lens_config_env: Path,
) -> None:
    note = NoteRecord(id="root", title="Root Note", content="Still here.")
    fake = KnowledgeFakeLithosClient(note=note, related_error=True)

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/root")

    assert response.status_code == 200
    # Body still renders even though the related panel failed to load.
    assert "Root Note" in response.text
    assert "Still here." in response.text
    assert "The related panel could not be loaded." in response.text


def test_note_page_renders_provenance_overflow(
    lithos_lens_config_env: Path,
) -> None:
    """Regression for f-002: provenance sources collapsed past the fan-out cap
    still render the Provenance section with a "+N more" indicator."""
    note = NoteRecord(id="root", title="Root Note", content="Body.")
    links = tuple(RelatedRef(id=f"link-{i}") for i in range(20))
    sources = tuple(RelatedRef(id=f"src-{i}") for i in range(5))
    titles = {f"link-{i}": f"Link {i}" for i in range(20)}
    fake = KnowledgeFakeLithosClient(
        neighborhood=RelatedNeighborhood(links=links, sources=sources),
        titles=titles,
        note=note,
    )

    with _client(lithos_lens_config_env, fake) as client:
        response = client.get("/note/root")

    assert response.status_code == 200
    assert "Provenance" in response.text
    assert "Sources" in response.text
    assert "+5 more" in response.text


def test_config_rejects_oversized_related_fanout_cap(tmp_path: Path) -> None:
    """The title fan-out cap is bounded above so a misconfiguration can't
    amplify one request into an unbounded concurrent read burst."""
    config_path = tmp_path / "lithos-lens.toml"
    config_path.write_text(
        "[lithos-lens]\n"
        'environment = "test"\n'
        "[lithos-lens.knowledge]\n"
        "related_title_fanout_cap = 1000\n"
    )

    with pytest.raises(ConfigError, match="related_title_fanout_cap"):
        load_config(config_path)


def test_related_panel_uses_internal_render_cap_by_default() -> None:
    """related_render_cap is an internal constant, not public config (the PRD
    only specifies related_title_fanout_cap): the loader bounds each section at
    RELATED_RENDER_CAP when no explicit cap is passed."""
    links = tuple(RelatedRef(id=f"n-{i}", title=f"Title {i}") for i in range(60))
    fake = KnowledgeFakeLithosClient(neighborhood=RelatedNeighborhood(links=links))

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20))

    assert len(panel.links.items) == RELATED_RENDER_CAP
    assert panel.links.overflow == 60 - RELATED_RENDER_CAP


def test_config_has_no_public_related_render_cap() -> None:
    assert not hasattr(KnowledgeConfig(), "related_render_cap")


def test_env_override_sets_related_title_fanout_cap(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP", "5")

    config = load_config(lithos_lens_config_env)

    assert config.knowledge.related_title_fanout_cap == 5


@pytest.mark.parametrize("value", ["1000", "0", "-3", "nope"])
def test_env_override_related_title_fanout_cap_enforces_bounds(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The env override honors the same 1..MAX bounds as the TOML key, so a
    misconfigured environment can't amplify the per-request read fan-out."""
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP", value)

    with pytest.raises(
        ConfigError, match="LITHOS_LENS_KNOWLEDGE_RELATED_TITLE_FANOUT_CAP"
    ):
        load_config(lithos_lens_config_env)


def test_related_failure_logs_note_id_and_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = KnowledgeFakeLithosClient(related_error=True)

    with caplog.at_level(logging.WARNING, logger="lithos_lens.knowledge"):
        panel = _run(load_related_panel(fake, "note-xyz", title_fanout_cap=20))

    assert panel.state == SectionState.ERROR
    records = [r for r in caplog.records if "related panel" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "note-xyz" in message
    assert "RuntimeError" in message


def test_title_lookup_failures_log_one_aggregate_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per-item logs would let a hub note spam the log; failures aggregate to
    one count line."""

    class FailingReadsClient(KnowledgeFakeLithosClient):
        async def read_note(
            self, knowledge_id: str, *, max_length: int | None = None
        ) -> NoteRecord | None:
            self.read_calls.append((knowledge_id, max_length))
            raise RuntimeError("read unavailable")

    links = tuple(RelatedRef(id=f"n-{i}") for i in range(4))
    fake = FailingReadsClient(neighborhood=RelatedNeighborhood(links=links))

    with caplog.at_level(logging.WARNING, logger="lithos_lens.knowledge"):
        panel = _run(load_related_panel(fake, "note-xyz", title_fanout_cap=20))

    # Unresolvable titles degrade to bare-id items; the panel still renders.
    assert panel.state == SectionState.OK
    records = [r for r in caplog.records if "title lookup" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "4 of 4" in message
    assert "note-xyz" in message


# ── concrete client (transport contract) ───────────────────────────────


class _StubLithosClient(LithosClient):
    """LithosClient with the MCP transport stubbed out.

    Records each ``(tool, arguments)`` pair; ``lithos_related`` returns the
    production-shaped payload, ``lithos_read`` answers from a per-id note map.
    The lifecycle methods are neutralized so the stub can also back a full
    ``create_app`` page render (raw payload -> client -> loader -> HTML).
    """

    def __init__(
        self,
        *,
        related_payload: dict[str, Any] | None = None,
        notes: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(LithosConfig())
        self.related_payload = related_payload or {"id": "root", "included": []}
        self.notes = notes or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def startup(self) -> None:
        return None

    async def health(self) -> LithosHealth:
        return "ok"

    async def register_agent(self) -> bool:
        return True

    async def _call_tool(  # type: ignore[override]
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if name == "lithos_related":
            return self.related_payload
        if name == "lithos_read":
            note = self.notes.get(str(arguments.get("id")))
            if note is None:
                return {"status": "error", "code": "not_found", "message": "missing"}
            return note
        return {}

    def read_ids(self) -> list[str]:
        return [
            str(arguments.get("id"))
            for name, arguments in self.calls
            if name == "lithos_read"
        ]


def _run_client(client: LithosClient, coro: Any) -> Any:
    async def _driver() -> Any:
        try:
            return await coro
        finally:
            await client.close()

    return asyncio.run(_driver())


def test_client_related_sends_only_the_real_tool_arguments() -> None:
    """lithos_related accepts only id/include/depth/namespace — FastMCP rejects
    unexpected arguments, so an invented one (agent_id) would fail every call."""
    client = _StubLithosClient(related_payload=REAL_RELATED_PAYLOAD)

    neighborhood = _run_client(client, client.related("root"))

    assert client.calls == [("lithos_related", {"id": "root", "depth": 1})]
    assert neighborhood.links == (RelatedRef(id="out-1", title="Outgoing Note"),)


def test_note_page_flows_raw_related_payload_to_html(
    lithos_lens_config_env: Path,
) -> None:
    """End-to-end: raw lithos_related payload -> concrete client -> loader ->
    rendered HTML. Exactly one lithos_related call per page; edge endpoints
    that appear in several edge rows are title-resolved once."""
    payload = dict(REAL_RELATED_PAYLOAD)
    payload["edges"] = {
        "outgoing": [
            _edge_row(edge_id="e-1", from_id="root", to_id="dup-1", type="supports"),
            _edge_row(edge_id="e-2", from_id="root", to_id="dup-1", type="contradicts"),
        ],
        "incoming": [
            _edge_row(
                edge_id="e-3",
                from_id="challenger",
                to_id="root",
                type="contradicts",
                weight=0.9,
                conflict_state="unresolved",
            )
        ],
    }
    stub = _StubLithosClient(
        related_payload=payload,
        notes={
            "root": {"id": "root", "title": "Root Note", "content": "Body text."},
            "dup-1": {"id": "dup-1", "title": "Dup Note", "content": ""},
            "challenger": {"id": "challenger", "title": "Challenger Note"},
        },
    )

    config = load_config(lithos_lens_config_env)
    app = create_app(config, lithos_client_factory=lambda _: stub)
    with TestClient(app) as client:
        response = client.get("/note/root")

    assert response.status_code == 200
    # Inline-titled links and provenance flow through untouched.
    assert "Outgoing Note" in response.text
    assert "Incoming Note" in response.text
    assert "Source Note" in response.text
    assert "Derived Note" in response.text
    assert "drafts/missing.md" in response.text
    # Edge endpoints resolve to titles via the capped lithos_read fan-out.
    assert "Dup Note" in response.text
    assert "supports" in response.text
    # Direction is indicated per item ("A supports B" must not read the same
    # as "B supports A"), and a non-empty conflict_state renders explicitly
    # (REQUIREMENTS.md §6.5).
    assert "Challenger Note" in response.text
    assert 'edge-direction">outgoing' in response.text
    assert 'edge-direction">incoming' in response.text
    assert "conflict: unresolved" in response.text
    # Exactly one related call per page render.
    related_calls = [c for c in stub.calls if c[0] == "lithos_related"]
    assert related_calls == [("lithos_related", {"id": "root", "depth": 1})]
    # The duplicated edge endpoint is looked up once, not per edge row.
    assert stub.read_ids().count("dup-1") == 1
