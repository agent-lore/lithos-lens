"""The three knowledge routes and their instrumentation.

Extracted from :mod:`lithos_lens.web` when instrumenting them pushed that
module past the 800-line ceiling — extraction over budget-raising, the repo
convention (`request_filters.py`, `knowledge_produced_by.py` in #40,
`mcp_transport.py`). The seam is the natural one: these three are the whole
knowledge surface, they share no state with the task routes, and they are what
the K1 telemetry points describe.

Registered as a closure over the app, state and templates, which is the shape
`create_app` already uses — not an ``APIRouter``, which would need its own
dependency wiring for the same two objects.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lithos_lens import metrics
from lithos_lens.knowledge import RelatedPanel, load_related_panel
from lithos_lens.knowledge_edges import (
    EdgeTable,
    EdgeTableSnapshot,
    is_unresolved_contradiction,
)
from lithos_lens.knowledge_graph_picker import load_picker
from lithos_lens.knowledge_graph_routes import knowledge_graph_url
from lithos_lens.knowledge_landing import (
    SECTIONS,
    NamespaceFacet,
    RecentLanding,
    build_recent_landing,
    namespace_facets,
    namespace_path_prefix,
    normalize_namespace,
)
from lithos_lens.knowledge_metadata import (
    ListChips,
    build_note_metadata,
    load_list_chips,
)
from lithos_lens.knowledge_produced_by import load_produced_by
from lithos_lens.knowledge_resolver import ResolveOutcome, resolve_wiki_link
from lithos_lens.knowledge_tags import (
    TagBrowse,
    build_tag_browse,
    tag_count,
    tag_label,
)
from lithos_lens.lithos_client import LithosClientProtocol, LithosToolError
from lithos_lens.request_filters import (
    exact_query_param,
    form_encode,
    form_unstable,
    knowledge_landing_url,
    knowledge_note_url,
    knowledge_tags_url,
)
from lithos_lens.state import AppState
from lithos_lens.telemetry import get_current_span, get_tracer
from lithos_lens.template_vocabulary import format_tag
from lithos_lens.write_guards import safe_next

logger = logging.getLogger(__name__)

KNOWLEDGE_PATH = "/knowledge"


@dataclass(frozen=True)
class NoteBackLink:
    """Where a note page's back link goes, and what it calls that place."""

    href: str
    label: str


def note_back_link(next_url: str | None) -> NoteBackLink:
    """The note page's back link when it was not opened from a task.

    ``next_url`` is the untrusted ``next=`` the landing put on its result links
    (``request_filters.knowledge_note_url``); ``safe_next`` admits only a
    same-origin relative path, so a ``javascript:`` or ``//host`` value falls
    back to the landing rather than becoming an href. The label names the
    place the link actually goes: the results, the tag list, or the landing.
    A same-origin path that is not the landing gets a plain "Back" rather than
    a name it does not have. Note-to-note hops carry no ``next`` at all.
    """
    href = safe_next(next_url, default=KNOWLEDGE_PATH)
    parts = urlsplit(href)
    if parts.path != KNOWLEDGE_PATH:
        return NoteBackLink(href, "Back")
    # Blank values kept: ``tag=`` is the empty tag, not "no tag" (see
    # `filter_tag_label`).
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if params.get("q", "").strip():
        return NoteBackLink(href, "Back to search results")
    if "tag" in params:
        return NoteBackLink(
            href, f"Back to notes tagged {filter_tag_label(params['tag'])}"
        )
    return NoteBackLink(href, "Back to knowledge")


def filter_tag_label(tag: str) -> str:
    """The active tag as the landing names it: ``key: value``, as everywhere,
    unless that would hide which tag it is — the empty tag and a padded one
    take :func:`knowledge_tags.tag_label`'s spelling instead."""
    label = tag_label(tag)
    return format_tag(tag) if label == tag else label


async def active_tag_count(client: LithosClientProtocol, tag: str) -> int | None:
    """How many notes carry ``tag``, for the landing's "Filtered by" line.

    ``lithos_tags`` with the tag as its ``prefix`` (one small answer rather
    than the whole tag map), read back by EXACT key: the prefix also matches
    longer tags. A tag the index does not hold has no notes, so 0. A failed
    read is ``None`` and the line names the tag without a count — the count
    is garnish on a page that otherwise rendered.
    """
    try:
        rows = await client.list_tags(prefix=tag)
    except Exception:
        logger.info("active tag count unavailable", exc_info=True)
        return None
    count = tag_count(rows, tag)
    return 0 if count is None else count


@dataclass(frozen=True)
class NoteContradiction:
    """One line of the note page's unresolved-contradiction banner."""

    edge_id: str
    other_id: str
    other_label: str
    #: The graph focused on the note with this edge's panel open.
    edge_url: str


def _related_titles(related: RelatedPanel | None) -> dict[str, str]:
    """Titles the related panel already resolved: typed edges, then links."""
    titles: dict[str, str] = {}
    if related is not None:
        for section in (related.edges, related.links, related.backlinks):
            for item in section.items:
                if item.title:
                    titles.setdefault(item.id, item.title)
    return titles


async def load_contradictions(
    table: EdgeTable,
    knowledge_id: str,
    related: RelatedPanel | None,
) -> tuple[NoteContradiction, ...]:
    """The unresolved ``contradicts`` rows ``knowledge_id`` is an endpoint of.

    Read from the edge-table snapshot alone (PRD K2 D13) — no call per note:
    within its TTL ``read()`` answers from memory, and a stale snapshot still
    serves. Unreadable or over its bound, there is no banner (§6.5). The
    other note is named by the title the related panel already holds, else
    its id; nothing is read to name it.
    """
    try:
        snapshot = await table.read()
    except Exception:
        logger.info(
            "edge table unavailable for the contradiction banner", exc_info=True
        )
        return ()
    if not isinstance(snapshot, EdgeTableSnapshot):
        return ()
    titles = _related_titles(related)
    lines: list[NoteContradiction] = []
    for edge in snapshot.edges_of(knowledge_id):
        if not is_unresolved_contradiction(edge):
            continue
        other = edge.to_id if edge.from_id == knowledge_id else edge.from_id
        # The page draws the edge it is linked to whatever the filters say.
        url = knowledge_graph_url(focus=knowledge_id, edge=edge.edge_id)
        lines.append(
            NoteContradiction(edge.edge_id, other, titles.get(other, other), url)
        )
    return tuple(lines)


async def _traced_related_panel(
    client: LithosClientProtocol, knowledge_id: str, *, cap: int
) -> RelatedPanel:
    """Load a note's related panel inside its own span.

    The one knowledge point that keeps a named span. It is a PHASE within the
    note render rather than the render itself — its own backend calls, its own
    failure mode — so it does not nest 1:1 with the request the way the three
    route-level points do, and "which half of the note page was slow" is a
    question the server span alone cannot answer. See
    `telemetry.get_current_span` for the rule this is the exception to.
    """
    with get_tracer().start_as_current_span("lens.knowledge.related") as span:
        panel = await load_related_panel(client, knowledge_id, title_fanout_cap=cap)
        span.set_attribute("lens.related.fanout", panel.fanout)
        span.set_attribute("lens.related.state", panel.state.value)
        return panel


def register_knowledge_routes(
    app: FastAPI, state: AppState, templates: Jinja2Templates
) -> None:
    """Attach the knowledge landing, wiki-link resolver and note routes."""

    templates.env.globals["knowledge_note_url"] = knowledge_note_url
    templates.env.globals["knowledge_landing_url"] = knowledge_landing_url
    templates.env.globals["knowledge_tags_url"] = knowledge_tags_url
    templates.env.filters["tag_label"] = tag_label
    templates.env.filters["filter_tag_label"] = filter_tag_label
    templates.env.filters["form_unstable"] = form_unstable
    templates.env.filters["form_encode"] = form_encode

    @app.get("/knowledge", response_class=HTMLResponse)
    async def knowledge(request: Request) -> HTMLResponse:
        """Knowledge landing: hybrid search, or your notes and recent intake.

        Two branches (§7.1): a ``?q=`` query runs ``lithos_search`` and
        renders hybrid-search result cards (title, escaped snippet, updated);
        without one, ``recent_notes`` walks ``lithos_list`` newest-first and
        the walk is split into "Your notes" and "Recent intake"
        (``knowledge_landing``), each cut to ``recent_limit`` — ``?section=``
        shows one of them alone. ``?tag=`` and ``?namespace=`` (a path
        prefix) narrow both branches and compose. The rendered lists are
        capped from config so a broad ``?q=a`` / ``?tag=`` cannot render an
        unbounded result set (the resolver caps candidates for the same
        reason). Each card and row carries the note page's metadata chips,
        compact, for the first ``list_chip_fanout_cap`` notes (one capped
        ``lithos_read`` each — see ``load_list_chips``).
        """
        query = request.query_params.get("q", "").strip()
        # Verbatim, and None only when absent: Lithos keeps tag names as given
        # ("" and surrounding whitespace included) and matches them exactly,
        # so a trimmed or dropped value would select a different tag than the
        # link or form that sent it (a /knowledge/tags row, a note chip, this
        # page's own search form — whose carrier `exact_query_param` decodes).
        tag = exact_query_param(request, "tag")
        namespace = normalize_namespace(request.query_params.get("namespace", ""))
        section = request.query_params.get("section", "").strip()
        if query or section not in SECTIONS:
            section = ""
        knowledge_config = state.config.knowledge
        snapshot = await state.refresh_health()
        search_results = None
        browse: RecentLanding | None = None
        graph_picker = None
        facets: tuple[NamespaceFacet, ...] = ()
        error = ""
        mode = "search" if query else "browse"
        if snapshot.lithos != "ok":
            mode = "offline"
            error = "Lithos is offline or degraded. Knowledge search is unavailable."
        else:
            try:
                if query:
                    search_results = await state.lithos_client.search_notes(
                        query,
                        tags=[tag] if tag is not None else None,
                        path_prefix=namespace_path_prefix(namespace) or None,
                        limit=knowledge_config.search_limit,
                    )
                    facets = namespace_facets(row.path for row in search_results)
                else:
                    # The whole (tagged) corpus, newest-first: recent_notes
                    # owns the ordering lithos_list cannot provide (upstream
                    # task e0e31654), and the intake split and namespace counts
                    # are over every row — no limit, so no section can be
                    # starved by the other's notes (bound: the client's
                    # runaway guard). The namespace filter is applied here, on
                    # path, so the filter row still counts every namespace.
                    rows = await state.lithos_client.recent_notes(
                        tags=[tag] if tag is not None else None
                    )
                    browse = build_recent_landing(
                        rows,
                        intake_path_prefixes=knowledge_config.intake_path_prefixes,
                        namespace=namespace,
                        section=section,
                        limit=knowledge_config.recent_limit,
                    )
                    facets = browse.namespaces
                    # The "Browse the graph" line's unresolved count (K2 D13).
                    graph_picker = await load_picker(state.edge_table)
            except Exception:
                mode = "error"
                error = "Knowledge search is currently unavailable."
        tag_notes = None
        if tag is not None and mode != "offline":
            tag_notes = await active_tag_count(state.lithos_client, tag)
        if search_results is not None:
            rendered_ids = [row.id for row in search_results]
        else:
            rendered_ids = [row.id for row in browse.rows] if browse else []
        chips = ListChips()
        if rendered_ids:
            # Every section's ids in one call, so the per-request dedupe holds
            # across them: an id is read once however many rows show it.
            chips = await load_list_chips(
                state.lithos_client,
                rendered_ids,
                cap=knowledge_config.list_chip_fanout_cap,
            )
        span = get_current_span()
        # The query is deliberately absent from the metric LABEL: one series
        # per distinct search is unbounded cardinality from unauthenticated
        # input. It does reach the SPAN, via the instrumentation's own
        # `http.target`, where it costs no series and helps read a trace -- and
        # is bounded there by `MAX_LOGGED_VALUE_CHARS` (telemetry.py).
        span.set_attribute("lens.mode", mode)
        span.set_attribute("lens.result_count", len(rendered_ids))
        span.set_attribute("lens.has_tag", tag is not None)
        span.set_attribute("lens.has_namespace", bool(namespace))
        # lithos_read calls spent on the rows' chips: the landing's backend
        # cost beyond its one list call, bounded by list_chip_fanout_cap.
        span.set_attribute("lens.chips.fanout", chips.fanout)
        metrics.knowledge_searches().add(1, {"mode": mode})
        return templates.TemplateResponse(
            request,
            "knowledge/landing.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "knowledge",
                "query": query,
                "tag": tag,
                "tag_notes": tag_notes,
                "namespace": namespace,
                "section": section,
                # The return address every result link carries as `next=`.
                "landing_url": knowledge_landing_url(
                    query, tag, namespace=namespace, section=section
                ),
                "search_results": search_results,
                "browse": browse,
                "namespace_facets": facets,
                "graph_picker": graph_picker,
                "chips": chips,
                "error": error,
            },
        )

    @app.get("/knowledge/tags", response_class=HTMLResponse)
    async def knowledge_tags(request: Request) -> HTMLResponse:
        """Tag browse: every tag with its note count, from ``lithos_tags``.

        One call with no arguments answers the whole tag map; ``?q=`` (a
        substring) and ``?prefix=`` (a ``key:`` family) filter it Lens-side,
        because the family row is derived from every tag, and the list is cut
        to ``tags_page_limit`` (``knowledge_tags``). Each tag links to the
        landing filtered by it. Offline, or on a failed read, the landing's
        banner stands in for the list.
        """
        query = request.query_params.get("q", "").strip()
        # Verbatim like the landing's tag: a family is read off stored names,
        # so one with surrounding whitespace is matched as such.
        prefix = exact_query_param(request, "prefix") or ""
        snapshot = await state.refresh_health()
        browse: TagBrowse | None = None
        error = ""
        mode = "browse"
        if snapshot.lithos != "ok":
            mode = "offline"
            error = "Lithos is offline or degraded. Tags are unavailable."
        else:
            try:
                rows = await state.lithos_client.list_tags()
                browse = build_tag_browse(
                    rows,
                    query=query,
                    prefix=prefix,
                    limit=state.config.knowledge.tags_page_limit,
                )
            except Exception:
                mode = "error"
                error = "Tags are currently unavailable."
        span = get_current_span()
        span.set_attribute("lens.mode", mode)
        span.set_attribute("lens.result_count", len(browse.tags) if browse else 0)
        return templates.TemplateResponse(
            request,
            "knowledge/tags.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "knowledge",
                "query": query,
                "prefix": prefix,
                "browse": browse,
                "error": error,
            },
        )

    @app.get("/knowledge/resolve")
    async def knowledge_resolve(request: Request):
        """Resolve a clicked ``[[wiki-link]]`` per §6.3 and redirect or explain.

        A confident resolution 302-redirects to the note page; an ambiguous one
        renders a disambiguation page listing candidates; an unresolvable one
        renders an unresolved page offering a search. When Lithos is offline the
        link can't be resolved, so the unresolved page is shown directly.
        """
        target = request.query_params.get("target", "").strip()
        from_id = request.query_params.get("from", "").strip()
        snapshot = await state.refresh_health()
        offline = snapshot.lithos != "ok"
        if offline:
            outcome = ResolveOutcome(
                kind="unresolved", via="offline", target=target, search_query=target
            )
        else:
            outcome = await resolve_wiki_link(state.lithos_client, target, from_id)
        # Recorded BEFORE the redirect returns: the confident resolutions are
        # the ones that leave early, so measuring after the branch would count
        # only the failures and make resolution look broken. `via`, not `kind`
        # — the latter answers "redirect" for the uuid, path and single-title
        # arms alike (see ResolveOutcome).
        span = get_current_span()
        span.set_attribute("lens.outcome", outcome.via)
        span.set_attribute("lens.candidate_count", outcome.candidate_count)
        metrics.knowledge_resolves().add(1, {"outcome": outcome.via})
        if outcome.kind == "redirect":
            return RedirectResponse(
                f"/note/{quote(outcome.target_id)}", status_code=302
            )
        return templates.TemplateResponse(
            request,
            "knowledge/resolve.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "knowledge",
                "outcome": outcome,
                "offline": offline,
            },
        )

    @app.get("/note/{knowledge_id}", response_class=HTMLResponse)
    async def note(request: Request, knowledge_id: str) -> HTMLResponse:
        snapshot = await state.refresh_health()
        note_record = None
        note_meta = None
        task = None
        related = None
        produced_by = None
        contradictions: tuple[NoteContradiction, ...] = ()
        error = ""
        outcome = "rendered"
        related_seconds = 0.0
        if snapshot.lithos != "ok":
            outcome = "offline"
            error = "Lithos is offline or degraded. The note cannot be loaded."
        else:
            not_found = False
            try:
                note_record = await state.lithos_client.read_note(knowledge_id)
            except LithosToolError as exc:
                # Lithos answers a missing document with a coded error envelope
                # (doc_not_found) rather than an empty success, so this — not
                # the None fallback below — is the production not-found path.
                if exc.code == "doc_not_found":
                    not_found = True
                else:
                    error = "Could not load this document from Lithos."
            except Exception:
                error = "Could not load this document from Lithos."
            if not_found or (note_record is None and not error):
                error = "Document not found."
                outcome = "not_found"
            elif error:
                outcome = "error"
            if note_record is not None:
                note_meta = build_note_metadata(note_record)
                started = time.perf_counter()
                related = await _traced_related_panel(
                    state.lithos_client,
                    knowledge_id,
                    cap=state.config.knowledge.related_title_fanout_cap,
                )
                related_seconds = time.perf_counter() - started
                produced_by = await load_produced_by(state.lithos_client, note_record)
                contradictions = await load_contradictions(
                    state.edge_table, knowledge_id, related
                )
            task_id = request.query_params.get("task", "")
            if task_id:
                try:
                    # Addressed directly, like the detail page since T1-S7:
                    # the three-list scan `find_task` did is gone. A dead
                    # ?task= link answers task_not_found and drops the
                    # back-link rather than failing the document render.
                    task = await state.lithos_client.task_get(task_id)
                except Exception:
                    task = None
        span = get_current_span()
        span.set_attribute("lens.outcome", outcome)
        metrics.knowledge_note_renders().add(1, {"outcome": outcome})
        if related is not None:
            # Only when the panel was actually loaded. Recording a zero for the
            # offline and not-found paths would drag the latency distribution
            # toward nothing and make a slow panel look fast on average.
            span.set_attribute("lens.related.duration_ms", related_seconds * 1000)
            span.set_attribute("lens.related.fanout", related.fanout)
            span.set_attribute("lens.related.state", related.state.value)
            metrics.knowledge_related_duration().record(related_seconds)
            metrics.knowledge_related_fanout().record(related.fanout)
        return templates.TemplateResponse(
            request,
            "note.html",
            {
                "config": state.config,
                "health": snapshot,
                "active_view": "knowledge",
                "note": note_record,
                "note_meta": note_meta,
                "task": task,
                "back_link": note_back_link(request.query_params.get("next")),
                "related": related,
                "produced_by": produced_by,
                "contradictions": contradictions,
                "error": error,
            },
        )
