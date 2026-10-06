"""/knowledge/tags: every tag with its note count, from ``lithos_tags``.

§5.7: the tags most notes first (ties by name), a ``?q=`` substring filter, a
``?prefix=`` facet over the ``key:`` families PRESENT in the data, each tag
linking to ``/knowledge?tag=<tag>``, the list cut to
``[knowledge].tags_page_limit``. The landing links here ("Browse tags") and
puts the active tag's count on its "Filtered by" line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lithos_lens.config import ConfigError, load_config
from lithos_lens.fake_dataset import FakeLithosDataset
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge_tags import (
    TagCount,
    TagFamily,
    build_tag_browse,
    normalize_tag_counts,
    tag_families,
    tag_family,
    tag_label,
)
from lithos_lens.lithos_client import LithosHealth, LithosToolError
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app

# (note id, tags). Counts: project:influx 3, ingested-by:influx 2, research 2,
# then four tags on one note each. Two `project:` tags, one of which extends
# the other — the landing's count must read the EXACT key.
_CORPUS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("n1", ("project:influx", "research")),
    ("n2", ("project:influx", "ingested-by:influx")),
    ("n3", ("project:influx", "area:data", "research")),
    ("n4", ("ingested-by:influx", "draft")),
    ("n5", ("project:influx-old", "topic:a&b c")),
)

_RANKED = [
    ("project:influx", 3),
    ("ingested-by:influx", 2),
    ("research", 2),
    ("area:data", 1),
    ("draft", 1),
    ("project:influx-old", 1),
    ("topic:a&b c", 1),
]


def _corpus_fake() -> FakeLithosClient:
    notes = {
        note_id: NoteRecord(
            id=note_id,
            title=f"Title {note_id}",
            content="body",
            tags=tags,
            metadata={"updated_at": "2026-08-01T10:00:00+00:00"},
        )
        for note_id, tags in _CORPUS
    }
    paths = {f"notes/{note_id}.md": note_id for note_id, _ in _CORPUS}
    return FakeLithosClient(dataset=FakeLithosDataset(notes=notes, note_paths=paths))


def _client(config_path: Path, fake: FakeLithosClient) -> TestClient:
    app = create_app(load_config(config_path), lithos_client_factory=lambda _: fake)
    return TestClient(app)


def _set_knowledge(config_path: Path, body: str) -> None:
    with config_path.open("a") as handle:
        handle.write(f"\n[lithos-lens.knowledge]\n{body}\n")


def _get(config_path: Path, url: str, fake: FakeLithosClient | None = None) -> str:
    with _client(config_path, fake or _corpus_fake()) as client:
        response = client.get(url)
    assert response.status_code == 200
    return response.text


def _tag_rows(html: str) -> list[tuple[str, str, int]]:
    """The rendered tag list as (href, label, count), in page order."""
    start = html.index('<ul class="knowledge-results">')
    body = html[start : html.index("</ul>", start)]
    return [
        (href, label.replace("&amp;", "&"), int(count))
        for href, label, count in re.findall(
            r'<a href="([^"]+)">([^<]+)</a>\s*'
            r'<span class="knowledge-namespace-count">(\d+)</span>',
            body,
        )
    ]


def _families(html: str) -> list[tuple[str, str | None, bool]]:
    """The family row as (label, count, active) per link, in order."""
    start = html.index('aria-label="Tag family filter"')
    body = html[start : html.index("</nav>", start)]
    return [
        (label.strip(), count or None, bool(current))
        for current, label, count in re.findall(
            r'<a href="[^"]*"( aria-current="true")?>([^<]+?)\s*'
            r'(?:<span class="knowledge-namespace-count">(\d+)</span>)?</a>',
            body,
        )
    ]


# ── the view model (pure) ───────────────────────────────────────────────


def test_normalize_ranks_by_count_then_name_and_skips_non_counts() -> None:
    rows = normalize_tag_counts(
        {
            "tags": {
                "b": 2,
                "a": 2,
                "c": 5,
                # Lithos stores tag names verbatim (no trim, no non-empty
                # check at d2c49bb): these are tags, kept as sent.
                "": 9,
                " a ": 1,
                "flag": True,  # a bool is not a count
                "text": "3",
            }
        }
    )
    assert rows == (
        TagCount("", 9),
        TagCount("c", 5),
        TagCount("a", 2),
        TagCount("b", 2),
        TagCount(" a ", 1),
    )
    assert normalize_tag_counts({"tags": ["a"]}) == ()
    assert normalize_tag_counts({}) == ()


@pytest.mark.parametrize(
    ("tag", "family"),
    [
        ("project:influx", "project:"),
        ("source:https://x.org/a", "source:"),  # the FIRST colon splits
        ("research", ""),
        (":orphan", ""),  # no key before the colon
    ],
)
def test_tag_family_is_the_key_before_the_first_colon(tag: str, family: str) -> None:
    assert tag_family(tag) == family


def test_tag_families_are_derived_from_the_tags_present() -> None:
    families = tag_families(["zeta:1", "alpha:1", "alpha:2", "plain", "zeta:2"])
    assert families == (TagFamily("alpha:", 2), TagFamily("zeta:", 2))


def test_families_differing_only_in_case_are_one_family() -> None:
    """The prefix filter is case-insensitive, so ``Project:`` and ``project:``
    select the same tags: one entry, counting both, in the spelling most of
    its tags use (ties by name)."""
    families = tag_families(["Project:a", "project:b", "project:c", "AREA:x", "area:y"])
    assert families == (TagFamily("project:", 3), TagFamily("AREA:", 2))


@pytest.mark.parametrize(
    ("tag", "label"),
    [
        ("project:influx", "project:influx"),
        ("", "(empty tag)"),
        (" project:influx ", "\u201c project:influx \u201d"),
        ("   ", "\u201c   \u201d"),
    ],
)
def test_tag_label_never_hides_which_tag_it_is(tag: str, label: str) -> None:
    assert tag_label(tag) == label


def test_build_tag_browse_filters_then_caps_and_facets_over_everything() -> None:
    rows = normalize_tag_counts({"tags": dict(_RANKED)})
    browse = build_tag_browse(rows, query="INFLUX", prefix="Project:", limit=1)
    assert browse.tags == (TagCount("project:influx", 3),)
    assert (browse.matched, browse.total, browse.hidden) == (2, 7, 1)
    # The facet row ignores both filters.
    assert {family.prefix for family in browse.families} == {
        "project:",
        "ingested-by:",
        "area:",
        "topic:",
    }


# ── the page ────────────────────────────────────────────────────────────


def test_tags_page_lists_every_tag_with_its_count_most_notes_first(
    lithos_lens_config_env: Path,
) -> None:
    fake = _corpus_fake()
    html = _get(lithos_lens_config_env, "/knowledge/tags", fake)

    assert [(label, count) for _, label, count in _tag_rows(html)] == _RANKED
    assert "7 tags, most notes first" in html
    # One call, no arguments: the whole map, filtered Lens-side.
    assert ("lithos_tags", {}) in fake.tool_calls
    assert "more &mdash; narrow the filter" not in html


def test_q_filters_tags_by_case_insensitive_substring(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, "/knowledge/tags?q=INFLUX")

    assert [label for _, label, _ in _tag_rows(html)] == [
        "project:influx",
        "ingested-by:influx",
        "project:influx-old",
    ]
    assert "3 of 7 tags" in html
    assert 'value="INFLUX"' in html


def test_a_filter_matching_nothing_says_so(lithos_lens_config_env: Path) -> None:
    html = _get(lithos_lens_config_env, "/knowledge/tags?q=nothing-like-it")

    assert "No matching tags." in html
    assert "0 of 7 tags" in html


def test_prefix_facet_lists_only_the_colon_families_present(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, "/knowledge/tags")

    assert _families(html) == [
        ("all", None, True),
        ("project:", "2", False),
        ("area:", "1", False),
        ("ingested-by:", "1", False),
        ("topic:", "1", False),
    ]
    # Families are read off the data, not a fixed list: none of these occur.
    for absent in ("profile:", "source:", "research", "draft"):
        assert all(label != absent for label, _, _ in _families(html))


def test_prefix_filters_and_composes_with_q(lithos_lens_config_env: Path) -> None:
    html = _get(lithos_lens_config_env, "/knowledge/tags?prefix=project%3A")

    assert [label for _, label, _ in _tag_rows(html)] == [
        "project:influx",
        "project:influx-old",
    ]
    assert ("project:", "2", True) in _families(html)
    assert ("all", None, False) in _families(html)
    # The GET form keeps the family when the substring is (re)submitted.
    assert '<input type="hidden" name="prefix" value="project:">' in html

    # The family matches case-insensitively, as lithos_tags's own prefix does,
    # and the facet it matches is the one marked current.
    typed = _get(lithos_lens_config_env, "/knowledge/tags?prefix=PROJECT%3A")
    assert len(_tag_rows(typed)) == 2
    assert ("project:", "2", True) in _families(typed)

    composed = _get(lithos_lens_config_env, "/knowledge/tags?prefix=project%3A&q=old")
    assert [label for _, label, _ in _tag_rows(composed)] == ["project:influx-old"]
    # Each family link keeps the substring.
    assert 'href="/knowledge/tags?q=old&amp;prefix=area%3A"' in composed


def test_the_list_is_cut_to_tags_page_limit_with_the_rest_counted(
    lithos_lens_config_env: Path,
) -> None:
    _set_knowledge(lithos_lens_config_env, "tags_page_limit = 3")

    html = _get(lithos_lens_config_env, "/knowledge/tags")

    assert [label for _, label, _ in _tag_rows(html)] == [
        "project:influx",
        "ingested-by:influx",
        "research",
    ]
    assert "4 more &mdash; narrow the filter" in html


def test_each_tag_links_to_the_landing_filtered_by_its_encoded_tag(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, "/knowledge/tags")

    hrefs = {label: href for href, label, _ in _tag_rows(html)}
    assert hrefs["project:influx"] == "/knowledge?tag=project%3Ainflux"
    assert hrefs["topic:a&b c"] == "/knowledge?tag=topic%3Aa%26b+c"

    # The link lands on the one note carrying that tag.
    landing = _get(lithos_lens_config_env, hrefs["topic:a&b c"])
    assert 'href="/note/n5?next=' in landing
    assert 'href="/note/n1?next=' not in landing


def test_offline_lithos_renders_the_banner_and_makes_no_call(
    lithos_lens_config_env: Path,
) -> None:
    class OfflineClient(FakeLithosClient):
        async def health(self) -> LithosHealth:
            return "unreachable"

    fake = OfflineClient()
    html = _get(lithos_lens_config_env, "/knowledge/tags", fake)

    assert "banner banner-warning" in html
    assert "Lithos is offline or degraded. Tags are unavailable." in html
    assert '<ul class="knowledge-results">' not in html
    assert not [call for call in fake.tool_calls if call[0] == "lithos_tags"]


def test_a_failed_lithos_tags_read_renders_the_banner(
    lithos_lens_config_env: Path,
) -> None:
    class FailingTags(FakeLithosClient):
        async def list_tags(self, *, prefix: str | None = None) -> tuple[TagCount, ...]:
            raise LithosToolError("boom", code="transport_error")

    html = _get(lithos_lens_config_env, "/knowledge/tags", FailingTags())

    assert "banner banner-warning" in html
    assert "Tags are currently unavailable." in html
    assert '<ul class="knowledge-results">' not in html


# ── the landing ─────────────────────────────────────────────────────────


def test_the_landing_links_to_tag_browse(lithos_lens_config_env: Path) -> None:
    html = _get(lithos_lens_config_env, "/knowledge")

    assert '<a href="/knowledge/tags">Browse tags</a>' in html


@pytest.mark.parametrize(
    ("tag", "line"),
    [
        # EXACT key: the prefix answer also holds project:influx-old (1).
        ("project:influx", "(3 notes)"),
        ("draft", "(1 note)"),
        ("nothing-carries-this", "(0 notes)"),
    ],
)
def test_the_filtered_by_line_carries_the_active_tags_count(
    lithos_lens_config_env: Path, tag: str, line: str
) -> None:
    fake = _corpus_fake()
    with _client(lithos_lens_config_env, fake) as client:
        html = client.get("/knowledge", params={"tag": tag}).text

    start = html.index('<p class="knowledge-active-filter">')
    filtered_by = html[start : html.index("</p>", start)]
    assert line in filtered_by
    assert ("lithos_tags", {"prefix": tag}) in fake.tool_calls


def test_a_failed_count_leaves_the_filtered_landing_intact(
    lithos_lens_config_env: Path,
) -> None:
    class FailingTags(FakeLithosClient):
        async def list_tags(self, *, prefix: str | None = None) -> tuple[TagCount, ...]:
            raise LithosToolError("boom", code="transport_error")

    fake = FailingTags(dataset=_corpus_fake().dataset)
    html = _get(lithos_lens_config_env, "/knowledge?tag=draft", fake)

    start = html.index('<p class="knowledge-active-filter">')
    filtered_by = html[start : html.index("</p>", start)]
    assert "draft" in filtered_by
    assert "note" not in filtered_by
    assert 'href="/note/n4?next=' in html
    assert "banner banner-warning" not in html


# ── config ──────────────────────────────────────────────────────────────


def test_tags_page_limit_defaults_to_500(lithos_lens_config_env: Path) -> None:
    assert load_config(lithos_lens_config_env).knowledge.tags_page_limit == 500


@pytest.mark.parametrize("value", ["0", "100000", '"ten"'])
def test_tags_page_limit_rejects_out_of_range(
    lithos_lens_config_env: Path, value: str
) -> None:
    _set_knowledge(lithos_lens_config_env, f"tags_page_limit = {value}")

    with pytest.raises(ConfigError, match="tags_page_limit"):
        load_config(lithos_lens_config_env)


# ── tag identity: names Lithos keeps verbatim ───────────────────────────

# Lithos stores tag strings as given — "" and surrounding whitespace included —
# and its tag index matches them exactly, so each of these is its own tag.
_ODD_CORPUS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("plain", ("project:influx",)),
    ("empty", ("",)),
    ("padded", (" project:influx ",)),
    ("blank", ("   ",)),
)


def _odd_fake() -> FakeLithosClient:
    notes = {
        note_id: NoteRecord(
            id=note_id,
            title=f"Title {note_id}",
            content="body",
            tags=tags,
            metadata={"updated_at": "2026-08-01T10:00:00+00:00"},
        )
        for note_id, tags in _ODD_CORPUS
    }
    paths = {f"notes/{note_id}.md": note_id for note_id, _ in _ODD_CORPUS}
    return FakeLithosClient(dataset=FakeLithosDataset(notes=notes, note_paths=paths))


def test_every_tag_name_is_listed_with_a_visible_label(
    lithos_lens_config_env: Path,
) -> None:
    html = _get(lithos_lens_config_env, "/knowledge/tags", _odd_fake())

    assert sorted(_tag_rows(html)) == sorted(
        [
            ("/knowledge?tag=project%3Ainflux", "project:influx", 1),
            ("/knowledge?tag=", "(empty tag)", 1),
            ("/knowledge?tag=+project%3Ainflux+", "\u201c project:influx \u201d", 1),
            ("/knowledge?tag=+++", "\u201c   \u201d", 1),
        ]
    )
    assert "4 tags, most notes first" in html


@pytest.mark.parametrize(
    ("tag", "note_id", "label"),
    [
        ("project:influx", "plain", "project: influx"),
        ("", "empty", "(empty tag)"),
        (" project:influx ", "padded", "\u201c project:influx \u201d"),
        ("   ", "blank", "\u201c   \u201d"),
    ],
)
def test_a_tag_link_selects_exactly_that_tag_on_the_landing(
    lithos_lens_config_env: Path, tag: str, note_id: str, label: str
) -> None:
    """The browse row's link must select the tag it names: no trimming between
    the link and the filter, the count, or the links the landing builds."""
    fake = _odd_fake()
    with _client(lithos_lens_config_env, fake) as client:
        tags_html = client.get("/knowledge/tags").text
        href = next(h for h, _, _ in _tag_rows(tags_html) if h == _tag_href(tag))
        html = client.get(href).text

    assert re.findall(r'href="/note/([^?"]+)\?next=', html) == [note_id]
    start = html.index('<p class="knowledge-active-filter">')
    filtered_by = html[start : html.index("</p>", start)]
    assert f"Filtered by {label}" in filtered_by
    assert "(1 note)" in filtered_by
    assert ("lithos_tags", {"prefix": tag}) in fake.tool_calls
    # A search from here keeps the same tag, verbatim.
    assert f'<input type="hidden" name="tag" value="{tag}">' in html


def _tag_href(tag: str) -> str:
    from urllib.parse import urlencode

    return f"/knowledge?{urlencode({'tag': tag})}"


def test_no_tag_param_is_no_filter(lithos_lens_config_env: Path) -> None:
    fake = _odd_fake()
    html = _get(lithos_lens_config_env, "/knowledge", fake)

    assert "knowledge-active-filter" not in html
    assert '<input type="hidden" name="tag"' not in html
    assert not [call for call in fake.tool_calls if call[0] == "lithos_tags"]
    assert sorted(re.findall(r'href="/note/([^?"]+)\?next=', html)) == sorted(
        note_id for note_id, _ in _ODD_CORPUS
    )


def test_a_note_opened_under_the_empty_tag_goes_back_to_it(
    lithos_lens_config_env: Path,
) -> None:
    with _client(lithos_lens_config_env, _odd_fake()) as client:
        landing = client.get("/knowledge?tag=").text
        href = re.findall(r'href="(/note/empty\?next=[^"]+)"', landing)[0]
        note = client.get(href.replace("&amp;", "&")).text

    assert 'href="/knowledge?tag="' in note
    assert "Back to notes tagged (empty tag)" in note


# ── the operator's own controls ─────────────────────────────────────────


def test_the_filter_form_submits_by_get_and_keeps_the_family(
    lithos_lens_config_env: Path,
) -> None:
    """Drive the RENDERED form: its method, action and named controls, as a
    browser would submit them, not a URL the test built."""
    with _client(lithos_lens_config_env, _corpus_fake()) as client:
        page = client.get("/knowledge/tags?prefix=project%3A").text
        start = page.index('<form class="knowledge-search"')
        form = page[start : page.index("</form>", start)]
        opening = form[: form.index(">")]
        assert re.search(r'method="get"', opening)
        action = re.search(r'action="([^"]+)"', opening)
        assert action is not None
        controls = dict(
            re.findall(r'<input[^>]*?name="([^"]+)"[^>]*?value="([^"]*)"', form)
        )
        assert set(controls) == {"q", "prefix"}
        controls["q"] = "old"
        submitted = client.get(action.group(1), params=controls)

    assert submitted.status_code == 200
    assert [label for _, label, _ in _tag_rows(submitted.text)] == [
        "project:influx-old"
    ]
    assert ("project:", "2", True) in _families(submitted.text)


def test_prefix_matches_the_start_of_a_tag_only(lithos_lens_config_env: Path) -> None:
    """``source:project:influx`` is in the ``source:`` family: ``prefix=project:``
    excludes it, while a ``q`` substring finds it."""
    rows = normalize_tag_counts(
        {"tags": {"project:influx": 2, "source:project:influx": 1}}
    )
    by_prefix = build_tag_browse(rows, query="", prefix="project:", limit=10)
    assert [row.tag for row in by_prefix.tags] == ["project:influx"]
    by_query = build_tag_browse(rows, query="project:", prefix="", limit=10)
    assert [row.tag for row in by_query.tags] == [
        "project:influx",
        "source:project:influx",
    ]

    notes = {
        "a": NoteRecord(id="a", title="A", content="", tags=("project:influx",)),
        "b": NoteRecord(id="b", title="B", content="", tags=("source:project:influx",)),
    }
    fake = FakeLithosClient(dataset=FakeLithosDataset(notes=notes))
    html = _get(lithos_lens_config_env, "/knowledge/tags?prefix=project%3A", fake)
    assert [label for _, label, _ in _tag_rows(html)] == ["project:influx"]
    html = _get(lithos_lens_config_env, "/knowledge/tags?q=project%3A", fake)
    assert [label for _, label, _ in _tag_rows(html)] == [
        "project:influx",
        "source:project:influx",
    ]


def test_case_variant_families_are_one_entry_and_one_is_current(
    lithos_lens_config_env: Path,
) -> None:
    notes = {
        "a": NoteRecord(id="a", title="A", content="", tags=("Project:a",)),
        "b": NoteRecord(id="b", title="B", content="", tags=("project:b", "project:c")),
    }
    fake = FakeLithosClient(dataset=FakeLithosDataset(notes=notes))
    html = _get(lithos_lens_config_env, "/knowledge/tags?prefix=PROJECT%3A", fake)

    assert _families(html) == [("all", None, False), ("project:", "3", True)]
    assert len(_tag_rows(html)) == 3
