"""BUG a38a0f32 — a note opened from the knowledge view says where it came from.

The note page's back link used to hard-code "Back to tasks" whenever it had no
``?task=``, so every note reached from ``/knowledge`` (and the resolver's
pages) pointed the operator at the task board. Now: ``?task=`` keeps priority,
a landing result's ``next=`` returns to the results it came from, and anything
else goes back to ``/knowledge``.
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge import RelatedNeighborhood, RelatedRef
from lithos_lens.lithos_client import LithosClientProtocol
from lithos_lens.request_filters import knowledge_landing_url, knowledge_note_url
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app
from tests.test_knowledge_related import KnowledgeFakeLithosClient
from tests.test_tasks_mvp import TaskFakeLithosClient

# An id that addresses something else entirely if interpolated raw. (No `/`:
# the router decodes `%2F` before matching, so no such id is reachable at all.)
AWKWARD_ID = "doc x?y#z"
AWKWARD_PATH = "/note/doc%20x%3Fy%23z"


def _client(config_path: Path, fake: LithosClientProtocol) -> TestClient:
    config = load_config(config_path)
    app = create_app(config, lithos_client_factory=lambda _: fake)
    return TestClient(app)


def _notes_fake() -> FakeLithosClient:
    notes = {
        "plan": NoteRecord(
            id="plan",
            title="Influx migration plan",
            content="Cut over the ingest path first.",
            tags=("project:x",),
            metadata={"updated_at": "2026-08-01T10:00:00+00:00"},
        ),
        AWKWARD_ID: NoteRecord(
            id=AWKWARD_ID,
            title="Awkward ingest note",
            content="The ingest id needs encoding.",
            tags=("project:x",),
            metadata={"updated_at": "2026-08-02T10:00:00+00:00"},
        ),
    }
    return FakeLithosClient(dataset=FakeLithosDataset(notes=notes))


def _back_link(html: str) -> tuple[str, str]:
    """The page's back link: (href, text) of the first ``← …`` anchor."""
    match = re.search(r'<a href="([^"]*)">\s*&larr;\s*([^<]*)</a>', html)
    assert match, html
    return unescape(match.group(1)), unescape(match.group(2)).strip()


def _note_hrefs(html: str) -> list[str]:
    return [unescape(h) for h in re.findall(r'href="(/note/[^"]*)"', html)]


# ── URL helpers (pure) ────────────────────────────────────────────────


def test_knowledge_landing_url_keeps_query_and_tag() -> None:
    assert knowledge_landing_url() == "/knowledge"
    assert knowledge_landing_url("ingest") == "/knowledge?q=ingest"
    assert knowledge_landing_url("", "project:x") == "/knowledge?tag=project%3Ax"
    assert (
        knowledge_landing_url("a b&c", "project:x")
        == "/knowledge?q=a+b%26c&tag=project%3Ax"
    )


def test_knowledge_note_url_encodes_the_id_and_carries_next() -> None:
    url = knowledge_note_url(AWKWARD_ID, "/knowledge?q=a&tag=t")
    parts = urlsplit(url)
    assert parts.path == AWKWARD_PATH
    assert parts.fragment == ""
    assert parse_qs(parts.query) == {"next": ["/knowledge?q=a&tag=t"]}


# ── /note/{id}: the back link ────────────────────────────────────────


def test_note_without_params_goes_back_to_knowledge(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, TaskFakeLithosClient()) as client:
        response = client.get("/note/note-1")

    assert response.status_code == 200
    assert _back_link(response.text) == ("/knowledge", "Back to knowledge")
    assert "Back to tasks" not in response.text
    assert 'href="/tasks">' not in response.text.split("<main", 1)[-1]


def test_note_with_search_next_goes_back_to_search_results(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, TaskFakeLithosClient()) as client:
        response = client.get("/note/note-1", params={"next": "/knowledge?q=x"})

    assert _back_link(response.text) == ("/knowledge?q=x", "Back to search results")


def test_note_with_search_and_tag_next_is_still_search_results(
    lithos_lens_config_env: Path,
) -> None:
    landing = "/knowledge?q=x&tag=project%3Ax"
    with _client(lithos_lens_config_env, TaskFakeLithosClient()) as client:
        response = client.get("/note/note-1", params={"next": landing})

    assert _back_link(response.text) == (landing, "Back to search results")


def test_note_with_tag_next_goes_back_to_the_tag_list(
    lithos_lens_config_env: Path,
) -> None:
    landing = "/knowledge?tag=project%3Ax"
    with _client(lithos_lens_config_env, TaskFakeLithosClient()) as client:
        response = client.get("/note/note-1", params={"next": landing})

    assert _back_link(response.text) == (landing, "Back to notes tagged project: x")


def test_note_with_bare_landing_next_says_knowledge(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, TaskFakeLithosClient()) as client:
        response = client.get("/note/note-1", params={"next": "/knowledge"})

    assert _back_link(response.text) == ("/knowledge", "Back to knowledge")


def test_note_with_other_same_origin_next_does_not_misname_it(
    lithos_lens_config_env: Path,
) -> None:
    """A same-origin path that is not the landing is honoured, but not called
    "knowledge" or "search results" — a label naming a place the link does not
    go is the bug this fixes."""
    with _client(lithos_lens_config_env, TaskFakeLithosClient()) as client:
        response = client.get("/note/note-1", params={"next": "/tasks/graph"})

    assert _back_link(response.text) == ("/tasks/graph", "Back")


@pytest.mark.parametrize(
    "hostile",
    [
        "//evil.example/knowledge?q=x",
        "javascript:alert(1)",
        "http://evil.example/knowledge?q=x",
        "/\\evil.example",
        "/knowledge?q=x\r\nSet-Cookie: a=b",
        "",
    ],
)
def test_note_with_unsafe_next_falls_back_to_knowledge(
    lithos_lens_config_env: Path, hostile: str
) -> None:
    with _client(lithos_lens_config_env, TaskFakeLithosClient()) as client:
        response = client.get("/note/note-1", params={"next": hostile})

    assert response.status_code == 200
    assert _back_link(response.text) == ("/knowledge", "Back to knowledge")
    # No anchor goes off this Lens. (The header's operator link echoes the
    # page URL percent-encoded as its own `next`; that is not a destination.)
    hrefs = [unescape(h) for h in re.findall(r'href="([^"]*)"', response.text)]
    offsite = ("//", "/\\", "javascript:", "http://evil")
    assert not [h for h in hrefs if h.startswith(offsite)]


def test_note_task_back_link_wins_over_next(lithos_lens_config_env: Path) -> None:
    with _client(lithos_lens_config_env, TaskFakeLithosClient()) as client:
        response = client.get(
            "/note/note-1",
            params={"task": "open-claimed", "next": "/knowledge?q=x"},
        )

    assert _back_link(response.text) == (
        "/tasks/open-claimed",
        "Back to Claimed open task",
    )
    assert "Back to search results" not in response.text


def test_note_with_a_dead_task_link_falls_back_to_next(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, TaskFakeLithosClient()) as client:
        response = client.get(
            "/note/note-1",
            params={"task": "no-such-task", "next": "/knowledge?q=x"},
        )

    assert _back_link(response.text) == ("/knowledge?q=x", "Back to search results")


# ── /knowledge: result links carry the landing as next= ───────────────


@pytest.mark.parametrize(
    ("landing_query", "landing"),
    [
        ({"q": "ingest"}, "/knowledge?q=ingest"),
        ({"q": "ingest", "tag": "project:x"}, "/knowledge?q=ingest&tag=project%3Ax"),
        ({"tag": "project:x"}, "/knowledge?tag=project%3Ax"),
        ({}, "/knowledge"),
    ],
    ids=["search", "search-with-tag", "tag-list", "recent-list"],
)
def test_landing_note_links_are_encoded_and_carry_next(
    lithos_lens_config_env: Path, landing_query: dict[str, str], landing: str
) -> None:
    with _client(lithos_lens_config_env, _notes_fake()) as client:
        response = client.get("/knowledge", params=landing_query)

    hrefs = _note_hrefs(response.text)
    assert sorted(urlsplit(h).path for h in hrefs) == [
        AWKWARD_PATH,
        "/note/plan",
    ]
    for href in hrefs:
        assert parse_qs(urlsplit(href).query) == {"next": [landing]}


def test_following_a_result_and_its_back_link_returns_to_the_results(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, _notes_fake()) as client:
        landing = client.get("/knowledge", params={"q": "ingest", "tag": "project:x"})
        awkward = next(
            h for h in _note_hrefs(landing.text) if h.startswith(AWKWARD_PATH)
        )
        note = client.get(awkward)
        assert "Awkward ingest note" in note.text
        href, label = _back_link(note.text)
        back = client.get(href)

    assert label == "Back to search results"
    assert href == "/knowledge?q=ingest&tag=project%3Ax"
    assert "Results for &ldquo;ingest&rdquo;" in back.text
    assert "Filtered by project: x" in back.text


# ── note-to-note hops carry no next= ──────────────────────────────────

SEARCH_LANDING = "/knowledge?q=ingest"


def test_related_and_replaces_links_drop_next_and_go_back_to_knowledge(
    lithos_lens_config_env: Path,
) -> None:
    """A note opened from search results returns there; a note reached FROM it
    (related panel, ``replaces:`` chip) does not inherit that return address —
    its back link is the landing, not the first note's results."""
    root = NoteRecord(
        id="root",
        title="Root Note",
        content="Body.",
        metadata={"supersedes": "prior"},
    )
    neighborhood = RelatedNeighborhood(
        links=(RelatedRef(id="out-1"),),
        backlinks=(RelatedRef(id="in-1"),),
        sources=(RelatedRef(id="src-1"),),
        edges=(RelatedRef(id="edge-1", edge_type="supports"),),
    )
    titles = {
        "out-1": "Outgoing Note",
        "in-1": "Incoming Note",
        "src-1": "Source Note",
        "edge-1": "Edge Note",
        "prior": "Prior Note",
    }
    fake = KnowledgeFakeLithosClient(
        neighborhood=neighborhood, titles=titles, note=root
    )

    with _client(lithos_lens_config_env, fake) as client:
        first = client.get("/note/root", params={"next": SEARCH_LANDING})
        assert _back_link(first.text) == (SEARCH_LANDING, "Back to search results")

        panel = first.text.split('<aside class="related-panel"', 1)[1]
        chip = first.text.split('class="chip note-supersedes"', 1)[1]
        chip = chip.split("</span>", 1)[0]
        hops = _note_hrefs(panel) + _note_hrefs(chip)
        assert sorted(hops) == [
            "/note/edge-1",
            "/note/in-1",
            "/note/out-1",
            "/note/prior",
            "/note/src-1",
        ]
        for href in hops:
            second = client.get(href)
            assert second.status_code == 200
            assert _back_link(second.text) == ("/knowledge", "Back to knowledge"), href


def test_wiki_link_through_the_resolver_drops_next(
    lithos_lens_config_env: Path,
) -> None:
    target_id = "33333333-3333-4333-8333-333333333333"
    notes = {
        "root": NoteRecord(id="root", title="Root", content="See [[guides/target]]."),
        target_id: NoteRecord(id=target_id, title="Target Note", content="Body."),
    }
    fake = FakeLithosClient(
        dataset=FakeLithosDataset(
            notes=notes, note_paths={"guides/target.md": target_id}
        )
    )

    with _client(lithos_lens_config_env, fake) as client:
        first = client.get("/note/root", params={"next": SEARCH_LANDING})
        resolve_hrefs = re.findall(r'href="(/knowledge/resolve\?[^"]*)"', first.text)
        assert len(resolve_hrefs) == 1, first.text
        wiki = unescape(resolve_hrefs[0])
        assert "next" not in parse_qs(urlsplit(wiki).query)
        redirect = client.get(wiki, follow_redirects=False)
        assert redirect.status_code == 302
        assert redirect.headers["location"] == f"/note/{target_id}"
        second = client.get(redirect.headers["location"])

    assert "Target Note" in second.text
    assert _back_link(second.text) == ("/knowledge", "Back to knowledge")


# ── /knowledge/resolve: no more "Back to tasks" ────────────────────────


@pytest.mark.parametrize("target", ["Shared", "nothing-here"])
def test_resolve_pages_go_back_to_knowledge(
    lithos_lens_config_env: Path, target: str
) -> None:
    notes = {
        "shared-one": NoteRecord(id="shared-one", title="Shared design", content=""),
        "shared-two": NoteRecord(id="shared-two", title="Shared rollout", content=""),
    }
    fake = FakeLithosClient(dataset=FakeLithosDataset(notes=notes))
    with _client(lithos_lens_config_env, fake) as client:
        response = client.get(
            "/knowledge/resolve",
            params={"target": target, "from": "src"},
            follow_redirects=False,
        )

    assert response.status_code == 200
    assert _back_link(response.text) == ("/knowledge", "Back to knowledge")
    assert "Back to tasks" not in response.text
