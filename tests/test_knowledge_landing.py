"""/knowledge landing: your notes first, intake second, with a namespace filter.

§7.1: without a query the landing renders "Your notes" (non-intake) then
"Recent intake", each newest-first and cut to ``recent_limit``. A note is
intake when it carries an ``ingested-by:*`` tag or its path starts with one of
``[knowledge].intake_path_prefixes``. ``?namespace=`` (a path prefix) filters
both sections and search, and composes with ``?tag=``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import ConfigError, load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge_landing import (
    NamespaceFacet,
    build_recent_landing,
    is_intake,
    namespace_facets,
    path_namespace,
)
from lithos_lens.knowledge_search import SearchResult
from lithos_lens.tasks import NoteRecord, NoteSummary
from lithos_lens.web import create_app

DEFAULT_PREFIXES = ("articles/", "papers/", "digests/")

# (id, path, updated day in Aug 2026, tags). Insertion order is deliberately
# not recency order: the sections must be sorted, not merely partitioned.
_CORPUS: tuple[tuple[str, str, int, tuple[str, ...]], ...] = (
    ("own-plan", "plans/cutover.md", 3, ()),
    ("feed-1", "influx/rss/feed-1.md", 20, ("ingested-by:influx",)),
    ("own-review", "user/reviews/q3.md", 9, ("project:x",)),
    ("article", "articles/attention.md", 18, ()),
    ("own-context", "projects/lens/context.md", 7, ("project:x",)),
    ("both", "papers/compaction.md", 19, ("ingested-by:influx", "project:x")),
    ("own-research", "research/rrf.md", 5, ()),
    ("feed-2", "influx/rss/feed-2.md", 16, ("ingested-by:influx", "project:x")),
    ("root-note", "scratch.md", 1, ()),
)


def _updated(day: int) -> str:
    return f"2026-08-{day:02d}T10:00:00+00:00"


def _corpus_fake() -> FakeLithosClient:
    notes = {
        note_id: NoteRecord(
            id=note_id,
            title=f"Title {note_id}",
            content=f"shared body of {note_id}",
            tags=tags,
            metadata={"updated_at": _updated(day)},
        )
        for note_id, _, day, tags in _CORPUS
    }
    paths = {path: note_id for note_id, path, _, _ in _CORPUS}
    return FakeLithosClient(dataset=FakeLithosDataset(notes=notes, note_paths=paths))


def _client(config_path: Path, fake: FakeLithosClient) -> TestClient:
    app = create_app(load_config(config_path), lithos_client_factory=lambda _: fake)
    return TestClient(app)


def _set_knowledge(config_path: Path, body: str) -> None:
    with config_path.open("a") as handle:
        handle.write(f"\n[lithos-lens.knowledge]\n{body}\n")


def _section_ids(html: str, section_id: str) -> list[str] | None:
    """The note ids a section lists, in page order; ``None`` if not rendered."""
    start = html.find(f'id="{section_id}"')
    if start == -1:
        return None
    body = html[start : html.index("</section>", start)]
    return re.findall(r'href="/note/([^?"]+)\?next=', body)


def _facets(html: str) -> list[tuple[str, str | None, bool]]:
    """The namespace row as (label, count, active) per link, in order."""
    start = html.index('aria-label="Namespace filter"')
    body = html[start : html.index("</nav>", start)]
    return [
        (label.strip(), count, bool(current))
        for current, label, count in re.findall(
            r'<a href="[^"]*"( aria-current="true")?>([^<]+?)\s*'
            r'(?:<span class="knowledge-namespace-count">(\d+)</span>)?</a>',
            body,
        )
    ]


# ── the rule (pure) ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "tags", "expected"),
    [
        ("influx/rss/a.md", ("ingested-by:influx",), True),  # tag only
        ("articles/a.md", (), True),  # prefix only
        ("papers/a.md", ("ingested-by:arxiv",), True),  # both
        ("user/plans/a.md", ("project:x",), False),  # neither
        # Prefixes match from the start of the path, and a tag must be the
        # ingested-by key, not merely mention it.
        ("user/articles/a.md", ("source:ingested-by",), False),
        ("papers-old/a.md", (), False),
    ],
)
def test_intake_is_the_tag_or_the_path_prefix(
    path: str, tags: tuple[str, ...], expected: bool
) -> None:
    row = NoteSummary(id="n", path=path, tags=tags)
    assert is_intake(row, DEFAULT_PREFIXES) is expected


def test_no_intake_prefixes_leaves_only_the_tag() -> None:
    assert not is_intake(NoteSummary(id="n", path="articles/a.md"), ())
    assert is_intake(NoteSummary(id="n", path="x.md", tags=("ingested-by:influx",)), ())


def test_path_namespace_is_the_first_segment() -> None:
    assert path_namespace("projects/lens/context.md") == "projects"
    assert path_namespace("scratch.md") == ""
    assert path_namespace("") == ""


def test_namespace_facets_rank_by_count_and_list_only_present() -> None:
    paths = ["influx/a", "influx/b", "influx/c", "user/a", "plans/a", "user/b", "x"]
    assert namespace_facets(paths) == (
        NamespaceFacet("influx", 3),
        NamespaceFacet("user", 2),
        NamespaceFacet("plans", 1),
    )
    assert namespace_facets(paths, limit=1) == (NamespaceFacet("influx", 3),)
    assert namespace_facets([]) == ()


def test_build_recent_landing_partitions_and_cuts_each_class() -> None:
    rows = [NoteSummary(id=f"i{n}", path=f"articles/{n}.md") for n in range(5)] + [
        NoteSummary(id=f"o{n}", path=f"user/{n}.md") for n in range(5)
    ]

    landing = build_recent_landing(
        rows,
        intake_path_prefixes=DEFAULT_PREFIXES,
        namespace="",
        section="",
        limit=3,
    )

    # Five intake notes ahead of the user's in recency do not starve "Your
    # notes": each class keeps its own newest three, order preserved.
    assert [r.id for r in landing.notes or ()] == ["o0", "o1", "o2"]
    assert [r.id for r in landing.intake or ()] == ["i0", "i1", "i2"]
    assert [r.id for r in landing.rows][:3] == ["o0", "o1", "o2"]


# ── the landing (TestClient + fake dataset) ─────────────────────────────


def test_landing_renders_your_notes_then_recent_intake(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        html = client.get("/knowledge").text

    assert html.index("Your notes") < html.index("Recent intake")
    assert _section_ids(html, "your-notes") == [
        "own-review",
        "own-context",
        "own-research",
        "own-plan",
        "root-note",
    ]
    # Tagged (feed-*), prefixed (article) and both (both) — newest first. The
    # note that is tagged AND prefixed is listed once, under intake only.
    assert _section_ids(html, "recent-intake") == [
        "feed-1",
        "both",
        "article",
        "feed-2",
    ]
    assert html.count('href="/note/both?next=') == 1


def test_each_section_is_cut_to_recent_limit(lithos_lens_config_env: Path) -> None:
    _set_knowledge(lithos_lens_config_env, "recent_limit = 2")

    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        html = client.get("/knowledge").text

    assert _section_ids(html, "your-notes") == ["own-review", "own-context"]
    assert _section_ids(html, "recent-intake") == ["feed-1", "both"]


def test_intake_path_prefixes_config_moves_notes_between_sections(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, 'intake_path_prefixes = ["research/"]')

    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        html = client.get("/knowledge").text

    # research/ is intake now; articles/ is not (and is untagged).
    assert "own-research" in (_section_ids(html, "recent-intake") or [])
    assert "article" in (_section_ids(html, "your-notes") or [])


def test_namespace_filters_both_sections(lithos_lens_config_env: Path) -> None:
    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        influx = client.get("/knowledge?namespace=influx").text
        user = client.get("/knowledge", params={"namespace": "user/"}).text

    assert _section_ids(influx, "your-notes") == []
    assert _section_ids(influx, "recent-intake") == ["feed-1", "feed-2"]
    # A trailing slash names the same namespace; the prefix is matched whole.
    assert _section_ids(user, "your-notes") == ["own-review"]
    assert _section_ids(user, "recent-intake") == []
    assert "Filtered by namespace user" in user


def test_namespace_row_lists_only_present_namespaces_by_count(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        bare = client.get("/knowledge").text
        filtered = client.get("/knowledge?namespace=influx").text

    assert _facets(bare) == [
        ("all", "", True),
        # influx dominates and is shown, first; ties by name; the root note has
        # no namespace and is not a row entry.
        ("influx", "2", False),
        ("articles", "1", False),
        ("papers", "1", False),
        ("plans", "1", False),
        ("projects", "1", False),
        ("research", "1", False),
        ("user", "1", False),
    ]
    # Filtering does not shrink the row: it counts everything fetched, and
    # marks the active entry.
    assert [label for label, _, _ in _facets(filtered)] == [
        label for label, _, _ in _facets(bare)
    ]
    assert ("influx", "2", True) in _facets(filtered)
    assert ("all", "", False) in _facets(filtered)
    assert 'href="/knowledge?namespace=influx"' in bare


def test_tag_and_namespace_compose_on_the_landing(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        html = client.get(
            "/knowledge", params={"tag": "project:x", "namespace": "influx"}
        ).text

    assert _section_ids(html, "your-notes") == []
    assert _section_ids(html, "recent-intake") == ["feed-2"]
    assert "Filtered by project: x and namespace influx" in html
    # The search form carries both, so a search keeps the filters.
    assert '<input type="hidden" name="tag" value="project:x">' in html
    assert '<input type="hidden" name="namespace" value="influx">' in html
    # The row's entries keep the tag, and "all" drops only the namespace.
    assert 'href="/knowledge?tag=project%3Ax"' in html


def test_section_heading_links_to_that_section_alone(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        bare = client.get("/knowledge?namespace=influx").text
        intake = client.get("/knowledge?section=intake&namespace=influx").text
        notes = client.get("/knowledge?section=notes").text
        junk = client.get("/knowledge?section=bogus").text

    assert '<a href="/knowledge?namespace=influx&amp;section=intake">' in bare
    assert '<a href="/knowledge?namespace=influx&amp;section=notes">' in bare
    assert _section_ids(intake, "your-notes") is None
    assert _section_ids(intake, "recent-intake") == ["feed-1", "feed-2"]
    assert _section_ids(notes, "recent-intake") is None
    assert "own-plan" in (_section_ids(notes, "your-notes") or [])
    # An unknown section shows both.
    assert _section_ids(junk, "your-notes") and _section_ids(junk, "recent-intake")


def test_a_note_opened_from_a_filtered_landing_goes_back_to_it(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        html = client.get("/knowledge?namespace=influx&tag=project:x").text

    assert (
        'href="/note/feed-2?next=%2Fknowledge%3Ftag%3Dproject%253Ax'
        '%26namespace%3Dinflux"'
    ) in html


# ── search ──────────────────────────────────────────────────────────────


def _record_search(fake: FakeLithosClient) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    original = fake.search_notes

    async def record(query: str, **kwargs: Any) -> list[SearchResult]:
        calls.append({"query": query, **kwargs})
        return await original(query, **kwargs)

    fake.search_notes = record  # type: ignore[method-assign]
    return calls


def test_namespace_filters_search_via_path_prefix(
    lithos_lens_config_env: Path,
) -> None:
    fake = _corpus_fake()
    calls = _record_search(fake)

    with _client(lithos_lens_config_env, fake) as client:
        scoped = client.get("/knowledge?q=shared&namespace=influx").text
        unscoped = client.get("/knowledge?q=shared").text

    assert calls[0]["path_prefix"] == "influx/"
    assert calls[1]["path_prefix"] is None
    assert re.findall(r'href="/note/([^?"]+)\?next=', scoped) == ["feed-1", "feed-2"]
    assert "Filtered by namespace influx" in scoped
    # The search's own row lists the namespaces its results span.
    assert ("influx", "2", False) in _facets(unscoped)
    assert ("influx", "2", True) in _facets(scoped)


def test_tag_and_namespace_compose_on_search(lithos_lens_config_env: Path) -> None:
    fake = _corpus_fake()
    calls = _record_search(fake)

    with _client(lithos_lens_config_env, fake) as client:
        html = client.get(
            "/knowledge",
            params={"q": "shared", "tag": "project:x", "namespace": "influx"},
        ).text

    assert calls[0]["tags"] == ["project:x"]
    assert calls[0]["path_prefix"] == "influx/"
    assert re.findall(r'href="/note/([^?"]+)\?next=', html) == ["feed-2"]
    assert "Filtered by project: x and namespace influx" in html


# ── config: [knowledge].intake_path_prefixes ────────────────────────────


def test_intake_path_prefixes_default(lithos_lens_config_env: Path) -> None:
    config = load_config(lithos_lens_config_env)
    assert config.knowledge.intake_path_prefixes == DEFAULT_PREFIXES


@pytest.mark.parametrize(
    ("value", "expected"),
    [('["inbox/", "feeds/"]', ("inbox/", "feeds/")), ("[]", ())],
)
def test_intake_path_prefixes_read_from_toml(
    lithos_lens_config_env: Path, value: str, expected: tuple[str, ...]
) -> None:
    _set_knowledge(lithos_lens_config_env, f"intake_path_prefixes = {value}")
    config = load_config(lithos_lens_config_env)
    assert config.knowledge.intake_path_prefixes == expected


@pytest.mark.parametrize("bad", ['[""]', '"articles/"', "[1]"])
def test_intake_path_prefixes_rejects_junk(
    lithos_lens_config_env: Path, bad: str
) -> None:
    _set_knowledge(lithos_lens_config_env, f"intake_path_prefixes = {bad}")
    with pytest.raises(ConfigError, match="intake_path_prefixes"):
        load_config(lithos_lens_config_env)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("inbox/, feeds/", ("inbox/", "feeds/")),
        # Present-empty is the empty list: only the tag marks intake.
        ("", ()),
    ],
)
def test_env_override_sets_intake_path_prefixes(
    lithos_lens_config_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: str,
    expected: tuple[str, ...],
) -> None:
    # The env beats the file value.
    _set_knowledge(lithos_lens_config_env, 'intake_path_prefixes = ["toml/"]')
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_INTAKE_PATH_PREFIXES", env)
    config = load_config(lithos_lens_config_env)
    assert config.knowledge.intake_path_prefixes == expected


def test_env_override_intake_path_prefixes_rejects_a_blank_entry(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_INTAKE_PATH_PREFIXES", "articles/,,")
    with pytest.raises(ConfigError, match="LITHOS_LENS_KNOWLEDGE_INTAKE_PATH_PREFIXES"):
        load_config(lithos_lens_config_env)


def test_env_override_reaches_the_landing(
    lithos_lens_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITHOS_LENS_KNOWLEDGE_INTAKE_PATH_PREFIXES", "")

    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        html = client.get("/knowledge").text

    # No prefixes: the untagged article/ note is the user's; the tagged paper
    # stays intake by its tag.
    assert "article" in (_section_ids(html, "your-notes") or [])
    assert "both" in (_section_ids(html, "recent-intake") or [])
