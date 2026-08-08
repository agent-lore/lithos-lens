"""K1-S4 Related panel behavior tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.knowledge import (
    RelatedNeighborhood,
    RelatedRef,
    load_related_panel,
    normalize_related,
)
from lithos_lens.tasks import NoteRecord, SectionState
from lithos_lens.web import create_app


class KnowledgeFakeLithosClient:
    """Fake exercising only the note-view surface used by the related panel."""

    def __init__(
        self,
        *,
        neighborhood: RelatedNeighborhood | None = None,
        titles: dict[str, str] | None = None,
        note: NoteRecord | None = None,
        related_error: bool = False,
        health: str = "ok",
    ) -> None:
        self.neighborhood = neighborhood or RelatedNeighborhood()
        self.titles = titles or {}
        self.note = note
        self.related_error = related_error
        self.health_value = health
        self.read_calls: list[tuple[str, int | None]] = []
        self.related_calls: list[str] = []
        self.closed = False

    async def startup(self) -> None:
        return None

    async def health(self) -> str:
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

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        self.related_calls.append(knowledge_id)
        if self.related_error:
            raise RuntimeError("related unavailable")
        return self.neighborhood

    async def close(self) -> None:
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


# ── normalizer ─────────────────────────────────────────────────────────


def test_normalize_related_extracts_all_sections() -> None:
    payload = {
        "status": "ok",
        "links": [{"id": "out-1"}, {"target": "out-2"}],
        "backlinks": [{"id": "in-1"}],
        "provenance": {
            "sources": [{"id": "src-1"}],
            "derived": [{"id": "der-1"}],
            "unresolved": ["draft/missing", {"target": "other/missing"}],
        },
        "edges": [
            {"target": "edge-1", "type": "supports", "weight": 0.75},
            {"id": "edge-2", "edge_type": "refutes"},
        ],
    }

    neighborhood = normalize_related(payload)

    assert [ref.id for ref in neighborhood.links] == ["out-1", "out-2"]
    assert [ref.id for ref in neighborhood.backlinks] == ["in-1"]
    assert [ref.id for ref in neighborhood.sources] == ["src-1"]
    assert [ref.id for ref in neighborhood.derived] == ["der-1"]
    assert neighborhood.unresolved == ("draft/missing", "other/missing")
    assert neighborhood.edges[0] == RelatedRef(
        id="edge-1", edge_type="supports", weight=0.75
    )
    assert neighborhood.edges[1] == RelatedRef(id="edge-2", edge_type="refutes")


def test_normalize_related_tolerates_missing_and_malformed_fields() -> None:
    neighborhood = normalize_related({"links": "nope", "edges": [42, {}]})

    assert neighborhood.links == ()
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

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20))

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

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20))

    assert len(panel.edges.items) == 20
    assert panel.edges.overflow == 5
    # Only the capped set of ids is looked up, not all 25.
    assert len(fake.read_calls) == 20


def test_related_panel_renders_bare_id_when_title_unresolved() -> None:
    fake = KnowledgeFakeLithosClient(
        neighborhood=RelatedNeighborhood(links=(RelatedRef(id="ghost"),)),
        titles={},
    )

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20))

    # Within the cap but unresolvable -> rendered as a bare id, not overflow.
    assert panel.links.items[0].label == "ghost"
    assert panel.links.overflow == 0


def test_related_panel_degrades_when_related_call_fails() -> None:
    fake = KnowledgeFakeLithosClient(related_error=True)

    panel = _run(load_related_panel(fake, "root", title_fanout_cap=20))

    assert panel.state == SectionState.ERROR
    assert panel.is_empty


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
