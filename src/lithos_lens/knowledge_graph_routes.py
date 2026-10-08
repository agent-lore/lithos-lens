"""The `/knowledge/graph` page: picker, focus and scoped-global text (K2 S3).

Its own module for the reason `graph_routes.py` is: the page's assembly — the
edge-table snapshot, one ``lithos_related``, the gated facts fan-out — lives in
:mod:`lithos_lens.knowledge_graph` and shares nothing with the K1 routes in
`knowledge_routes.py`. What is here is the page's own vocabulary and wiring:

- **One query grammar** (:func:`parse_knowledge_graph_params`) and **one URL
  builder** (:func:`knowledge_graph_url`, a template global), so every link
  into the graph — this page's, and the panels, canvas and entry points that
  follow it — is spelled one way.
- **Three modes.** No ``focus``, ``type`` or ``namespace``: the scope picker,
  from the snapshot's facets alone. ``focus=``: the ego graph. ``type=`` and/or
  ``namespace=``: the scoped-global graph. A refusal (over a cap, the table over
  its bound, the table unreadable) renders the "narrow your scope" panel and
  nothing degraded.
- **The text is the page** (D12): the template renders the view in the PRD's
  order and embeds the same view as the JSON payload the canvas (S4) draws.
- **Telemetry**: ``lens.knowledge.graph.*`` attributes on the request span and
  one counter by mode and outcome; the scope itself is never a label.

Registered as a closure over app, state and templates, the shape `create_app`
already uses.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from lithos_lens import metrics
from lithos_lens.knowledge_edge_types import is_conflict_resolved
from lithos_lens.knowledge_edges import (
    EdgeFacets,
    EdgeTable,
    EdgeTableRefusal,
    EdgeTableSnapshot,
)
from lithos_lens.knowledge_facts import NoteFactsTally
from lithos_lens.knowledge_graph import assemble_focus_graph, assemble_global_graph
from lithos_lens.knowledge_graph_view import (
    MAX_DEPTH,
    PROVENANCE_GROUPS,
    HiddenEdgeCounts,
    KnowledgeGraphFilters,
    KnowledgeGraphView,
    edge_entry,
    edge_sections,
    graph_payload,
    named_edge,
)
from lithos_lens.knowledge_metadata import NoteMetadata
from lithos_lens.state import AppState
from lithos_lens.telemetry import get_current_span

KNOWLEDGE_GRAPH_PATH = "/knowledge/graph"

#: The picker's namespace table shows this many rows; the rest are behind a
#: disclosure (PRD D11).
PICKER_TOP_NAMESPACES = 20

GraphMode = Literal["picker", "focus", "global"]

#: The counter's outcome label: what the page did.
RenderOutcome = Literal["rendered", "refused", "unavailable", "offline"]

# The query keys the page reads, in the order the URL builder writes them.
_URL_KEYS = (
    "focus",
    "depth",
    "type",
    "namespace",
    "min_weight",
    "provenance",
    "selected",
    "edge",
)


@dataclass(frozen=True)
class KnowledgeGraphParams:
    """The page's query, parsed. ``None`` and ``""`` mean "not given".

    ``depth`` and ``min_weight`` stay ``None`` when absent or unreadable, so
    the configured defaults apply at render time and links do not pin them.
    ``provenance`` is empty when the request names no known group: all shown.
    """

    focus: str = ""
    type: str | None = None
    namespace: str | None = None
    depth: int | None = None
    min_weight: float | None = None
    provenance: tuple[str, ...] = ()
    edge: str = ""
    selected: str = ""

    @property
    def mode(self) -> GraphMode:
        if self.focus:
            return "focus"
        if self.type is not None or self.namespace is not None:
            return "global"
        return "picker"

    def depth_or(self, default: int) -> int:
        return self.depth if self.depth is not None else default

    def filters(self, default_min_weight: float) -> KnowledgeGraphFilters:
        weight = self.min_weight if self.min_weight is not None else default_min_weight
        groups = frozenset(self.provenance or PROVENANCE_GROUPS)
        return KnowledgeGraphFilters(min_weight=weight, provenance=groups)


def _value(query: Mapping[str, str], key: str) -> str:
    """A query value exactly as sent; ``""`` when absent or wholly blank.

    Not trimmed: a type or namespace is matched as stored upstream (a
    namespace ``" influx "`` is a different namespace from ``"influx"``, and
    the picker's own facet links send it so), and ``focus`` / ``edge`` /
    ``selected`` are ids carried unchanged. Only the numeric and provenance
    grammar below strips what it parses.
    """
    value = query.get(key) or ""
    return value if value.strip() else ""


_DEPTHS = {str(level): level for level in range(1, MAX_DEPTH + 1)}


def _depth(raw: str) -> int | None:
    return _DEPTHS.get(raw)


def _weight(raw: str) -> float | None:
    try:
        weight = float(raw)
    except ValueError:
        return None
    if not math.isfinite(weight):
        return None
    return min(max(weight, 0.0), 1.0)


def _provenance(raw: str) -> tuple[str, ...]:
    named = {part.strip() for part in raw.split(",")}
    return tuple(group for group in PROVENANCE_GROUPS if group in named)


def parse_knowledge_graph_params(query: Mapping[str, str]) -> KnowledgeGraphParams:
    """The page's query grammar (S3 D3), in one place.

    A blank value is absent. A non-blank ``focus`` is focus mode and the
    ``type``/``namespace`` scope is dropped; otherwise either of those (exact
    strings — a type or namespace is matched as stored) is global mode; with
    none, the picker. ``depth`` is 1 or 2, anything else the default;
    ``min_weight`` is clamped to [0, 1], unreadable is the default;
    ``provenance`` is a comma list of known groups, unknown names dropped.
    ``edge`` and ``selected`` are carried through links as given.
    """
    focus = _value(query, "focus")
    edge_type = _value(query, "type") or None
    namespace = _value(query, "namespace") or None
    if focus:
        edge_type = namespace = None
    return KnowledgeGraphParams(
        focus=focus,
        type=edge_type,
        namespace=namespace,
        depth=_depth(_value(query, "depth").strip()),
        min_weight=_weight(_value(query, "min_weight").strip()),
        provenance=_provenance(_value(query, "provenance")),
        edge=_value(query, "edge"),
        selected=_value(query, "selected"),
    )


def _url_value(key: str, params: KnowledgeGraphParams) -> str:
    if key == "depth":
        return "" if params.depth is None or not params.focus else str(params.depth)
    if key == "min_weight":
        # repr round-trips: a link must name the very threshold the page drew
        # with, or following it changes the scope (0.7000001 is not 0.7).
        return "" if params.min_weight is None else repr(params.min_weight)
    if key == "provenance":
        return ",".join(params.provenance)
    if key in ("type", "namespace") and params.focus:
        return ""
    value = getattr(params, key)
    return value or ""


def knowledge_graph_url(
    params: KnowledgeGraphParams | None = None, **changes: Any
) -> str:
    """A link into the knowledge graph: ``params`` with ``changes`` applied.

    The one URL builder (a template global): the picker's facet links,
    the refusal's remedies, every edge entry's ``edge=`` link, and the entry
    points S6 adds all come through here. Defaults are left out, so a link
    does not pin today's configured depth or weight; ``type``/``namespace``
    are dropped from a focus link, as the parser drops them.
    """
    merged = replace(params or KnowledgeGraphParams(), **changes)
    pairs = [(key, _url_value(key, merged)) for key in _URL_KEYS]
    query = urlencode([(key, value) for key, value in pairs if value])
    return f"{KNOWLEDGE_GRAPH_PATH}?{query}" if query else KNOWLEDGE_GRAPH_PATH


def utc_minute(moment: datetime) -> str:
    """``as_of`` as the scope line states it: UTC, to the minute."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# ── the picker ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KnowledgeGraphPicker:
    """What the unscoped page offers (PRD D11, story 17).

    ``facets`` is ``None`` when the snapshot gave none: the table over its
    bound (``refused``) or unreadable (``unavailable``). The page then offers
    only a typed-in ``type=`` / ``namespace=`` scope.
    """

    facets: EdgeFacets | None = None
    as_of: datetime | None = None
    stale: bool = False
    refused: EdgeTableRefusal | None = None
    unavailable: bool = False

    @property
    def top_namespaces(self) -> tuple[tuple[str, int], ...]:
        rows = tuple(self.facets.namespaces.items()) if self.facets else ()
        return rows[:PICKER_TOP_NAMESPACES]

    @property
    def more_namespaces(self) -> tuple[tuple[str, int], ...]:
        rows = tuple(self.facets.namespaces.items()) if self.facets else ()
        return rows[PICKER_TOP_NAMESPACES:]


async def load_picker(table: EdgeTable) -> KnowledgeGraphPicker:
    """The picker from the snapshot: its facets, or why there are none."""
    try:
        state = await table.read()
    except Exception:
        return KnowledgeGraphPicker(unavailable=True)
    if isinstance(state, EdgeTableSnapshot):
        return KnowledgeGraphPicker(state.facets, state.as_of, state.stale)
    return KnowledgeGraphPicker(refused=state, as_of=state.as_of)


# ── telemetry ──────────────────────────────────────────────────────────


def _outcome(
    view: KnowledgeGraphView | None, picker: KnowledgeGraphPicker | None
) -> tuple[RenderOutcome, str]:
    """The counter's outcome and the span's refusal reason ("" for none)."""
    if picker is not None:
        if picker.unavailable:
            return "unavailable", "unavailable"
        if picker.refused is not None:
            return "refused", "table_refused"
        return "rendered", ""
    if view is not None and view.refusal is not None:
        reason = view.refusal.reason
        return ("unavailable" if reason == "unavailable" else "refused"), reason
    return "rendered", ""


def _record(
    mode: GraphMode,
    outcome: RenderOutcome,
    *,
    snapshot_age_s: float,
    refusal: str = "",
    view: KnowledgeGraphView | None = None,
    depth: int | None = None,
) -> None:
    """``lens.knowledge.graph``: attributes on the request span, one counter.

    Counts and the refusal reason go on the span, where a per-request value
    costs no series; the counter carries only the two bounded enums. Every
    mode sets every count — zero where nothing was drawn (the picker, an
    offline page) — so a query over the span never mistakes absent for 0;
    ``depth`` is set in focus mode only (the drawn one, else the requested
    or configured one), ``refusal`` only on a refusal. The
    focus id, type and namespace are on neither: ``http.target`` already
    carries the query on the span, bounded (``telemetry.py``).
    """
    span = get_current_span()
    prefix = "lens.knowledge.graph"
    span.set_attribute(f"{prefix}.mode", mode)
    span.set_attribute(f"{prefix}.outcome", outcome)
    if refusal:
        span.set_attribute(f"{prefix}.refusal", refusal)
    span.set_attribute(f"{prefix}.snapshot_age_s", snapshot_age_s)
    if view is not None and view.mode == "focus":
        depth = view.depth
    if mode == "focus" and depth is not None:
        span.set_attribute(f"{prefix}.depth", depth)
    hidden = view.hidden if view is not None else HiddenEdgeCounts()
    tally = view.facts_tally if view is not None else NoteFactsTally()
    span.set_attribute(f"{prefix}.nodes", len(view.nodes) if view else 0)
    span.set_attribute(f"{prefix}.edges", len(view.edges) if view else 0)
    span.set_attribute(f"{prefix}.hidden_by_weight", hidden.by_weight)
    span.set_attribute(f"{prefix}.hidden_by_provenance", hidden.by_provenance)
    span.set_attribute(f"{prefix}.hidden_total", hidden.total)
    span.set_attribute(f"{prefix}.facts.hits", tally.hits)
    span.set_attribute(f"{prefix}.facts.reads", tally.reads)
    span.set_attribute(f"{prefix}.facts.missing", tally.missing)
    span.set_attribute(f"{prefix}.facts.capped", tally.capped)
    span.set_attribute(f"{prefix}.facts.failed", tally.failed)
    metrics.knowledge_graph_renders().add(1, {"mode": mode, "outcome": outcome})


def focus_metadata(view: KnowledgeGraphView) -> NoteMetadata | None:
    """The focus note's chips, through K1's ``NoteMetadata`` (S3 D7), so the
    status slug the chip's class is built from has one definition."""
    node = view.node(view.focus_id) if view.mode == "focus" else None
    if node is None or node.facts is None:
        return None
    facts = node.facts
    return NoteMetadata(
        note_type=facts.note_type,
        status=facts.status,
        namespace=facts.namespace,
        confidence=facts.confidence,
        lede=facts.lede,
    )


# ── the route ──────────────────────────────────────────────────────────


def register_knowledge_graph_routes(
    app: FastAPI, state: AppState, templates: Jinja2Templates
) -> None:
    """Attach `GET /knowledge/graph` (the picker, focus and scoped-global)."""

    templates.env.globals["knowledge_graph_url"] = knowledge_graph_url
    templates.env.globals["edge_entry"] = edge_entry
    templates.env.filters["utc_minute"] = utc_minute
    templates.env.filters["is_conflict_resolved"] = is_conflict_resolved

    @app.get(KNOWLEDGE_GRAPH_PATH, response_class=HTMLResponse)
    async def knowledge_graph(request: Request) -> HTMLResponse:
        """The knowledge graph as text, with its payload for the canvas.

        Offline, the page says so and reads nothing. The picker reads only
        the snapshot. Focus and global modes run the S2 orchestrators, which
        never raise for a table failure: they answer a refused view instead,
        and the page renders its message and remedy in place of the graph.
        """
        params = parse_knowledge_graph_params(request.query_params)
        mode = params.mode
        knowledge = state.config.knowledge
        health = await state.refresh_health()
        context: dict[str, Any] = {
            "config": state.config,
            "health": health,
            "active_view": "knowledge",
            "params": params,
            "mode": mode,
            "offline": health.lithos != "ok",
            "picker": None,
            "view": None,
        }
        table = state.edge_table
        if health.lithos != "ok":
            # The age is the holder's own clock: no read is started for it.
            _record(
                mode,
                "offline",
                snapshot_age_s=table.age_seconds(),
                depth=params.depth_or(knowledge.graph_default_depth),
            )
            return templates.TemplateResponse(request, "knowledge/graph.html", context)
        if mode == "picker":
            picker = await load_picker(table)
            context["picker"] = picker
            outcome, reason = _outcome(None, picker)
            _record(mode, outcome, refusal=reason, snapshot_age_s=table.age_seconds())
            return templates.TemplateResponse(request, "knowledge/graph.html", context)
        filters = params.filters(knowledge.graph_min_weight_default)
        if mode == "focus":
            view = await assemble_focus_graph(
                table,
                state.lithos_client.related,
                state.note_facts,
                params.focus,
                depth=params.depth_or(knowledge.graph_default_depth),
                filters=filters,
                max_nodes=knowledge.graph_focus_max_nodes,
                fanout_cap=knowledge.graph_title_fanout_cap,
            )
        else:
            view = await assemble_global_graph(
                table,
                state.note_facts,
                type=params.type,
                namespace=params.namespace,
                filters=filters,
                max_nodes=knowledge.graph_global_max_nodes,
                fanout_cap=knowledge.graph_title_fanout_cap,
            )
        context.update(
            view=view,
            sections=edge_sections(view),
            named_edge=named_edge(view, params.edge),
            focus_node=view.node(view.focus_id) if view.mode == "focus" else None,
            focus_meta=focus_metadata(view),
            payload=graph_payload(view),
        )
        outcome, reason = _outcome(view, None)
        _record(
            mode,
            outcome,
            refusal=reason,
            view=view,
            snapshot_age_s=table.age_seconds(),
        )
        return templates.TemplateResponse(request, "knowledge/graph.html", context)
