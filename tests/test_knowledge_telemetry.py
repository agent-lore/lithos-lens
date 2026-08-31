"""K1 telemetry: the three `lens.knowledge.*` instrumentation points (cdce170a).

Driven through the real routes against the fake client, so these cover what an
operator would actually see rather than the call sites' intent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lithos_lens.config import load_config
from lithos_lens.fake_lithos import FakeLithosClient
from lithos_lens.knowledge import RelatedNeighborhood
from lithos_lens.lithos_client import LithosToolError
from lithos_lens.tasks import NoteRecord
from lithos_lens.web import create_app
from tests.conftest import metric_value

DEMO_NOTE = "note-influx-plan"
DEMO_UUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
# A title the demo corpus resolves confidently, exercising the arm that
# actually evidences the wiki-link convention being used.
DEMO_TITLE = "Influx migration plan"


def _client(config_path: Path, client: Any = None) -> TestClient:
    return TestClient(
        create_app(
            load_config(config_path),
            lithos_client_factory=lambda _: client or FakeLithosClient(),
        )
    )


def _route_span(exporter: InMemorySpanExporter, route: str) -> ReadableSpan:
    """The server span for ``route``. Attributes live here rather than on a
    child span — see `telemetry.get_current_span` for why."""
    matching = [
        span
        for span in exporter.get_finished_spans()
        if (span.attributes or {}).get("http.route") == route
    ]
    assert len(matching) == 1, [
        (span.attributes or {}).get("http.route")
        for span in exporter.get_finished_spans()
    ]
    return matching[0]


def _attr(span: ReadableSpan, key: str) -> Any:
    return (span.attributes or {})[key]


# ── lens.knowledge.note ───────────────────────────────────────────────


def test_a_rendered_note_reports_its_related_panel_cost(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    """Render, related-panel timing and fan-out size — the three things the K1
    PRD names for this point.

    The panel is the expensive half of a note render (one `lithos_related` plus
    a bounded `lithos_read` fan-out), so without these a slow note page cannot
    be attributed to either half.
    """
    with _client(lithos_lens_config_env) as client:
        assert client.get(f"/note/{DEMO_NOTE}").status_code == 200

    span = _route_span(spans, "/note/{knowledge_id}")
    assert _attr(span, "lens.outcome") == "rendered"
    assert _attr(span, "lens.related.duration_ms") >= 0
    assert _attr(span, "lens.related.state") == "ok"
    assert _attr(span, "lens.related.fanout") >= 0

    assert (
        metric_value(
            metric_reader, "lens_knowledge_note_renders_total", outcome="rendered"
        ).value
        == 1
    )


def test_the_fanout_reported_is_the_backend_read_count(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    """Fan-out counts `lithos_read` calls, not neighbours.

    Refs whose title arrived inline need no lookup, and the count is capped, so
    "neighbours shown" and "backend reads spent" are different numbers. The
    second is the one that explains latency and load.
    """
    reads: list[str] = []

    class CountingClient(FakeLithosClient):
        async def read_note(
            self, knowledge_id: str, *, max_length: int | None = None
        ) -> NoteRecord | None:
            reads.append(knowledge_id)
            return await super().read_note(knowledge_id, max_length=max_length)

    with _client(lithos_lens_config_env, CountingClient()) as client:
        assert client.get(f"/note/{DEMO_NOTE}").status_code == 200

    span = _route_span(spans, "/note/{knowledge_id}")
    # The note body read is not fan-out; the title lookups are.
    assert _attr(span, "lens.related.fanout") == len(
        [note_id for note_id in reads if note_id != DEMO_NOTE]
    )


def test_a_missing_note_is_reported_apart_from_a_failure(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    """Both render a page and both answer 200, so the counter is the only place
    the difference survives: a dead wiki-link someone should fix versus Lithos
    failing to answer."""
    with _client(lithos_lens_config_env) as client:
        assert client.get("/note/no-such-note").status_code == 200

    assert _attr(_route_span(spans, "/note/{knowledge_id}"), "lens.outcome") == (
        "not_found"
    )
    assert (
        metric_value(
            metric_reader, "lens_knowledge_note_renders_total", outcome="not_found"
        ).value
        == 1
    )


def test_a_backend_failure_is_reported_as_an_error(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    class FailingClient(FakeLithosClient):
        async def read_note(
            self, knowledge_id: str, *, max_length: int | None = None
        ) -> NoteRecord | None:
            raise LithosToolError("upstream is unwell", code="tool_error")

    with _client(lithos_lens_config_env, FailingClient()) as client:
        assert client.get(f"/note/{DEMO_NOTE}").status_code == 200

    assert _attr(_route_span(spans, "/note/{knowledge_id}"), "lens.outcome") == "error"
    assert (
        metric_value(
            metric_reader, "lens_knowledge_note_renders_total", outcome="error"
        ).value
        == 1
    )


def test_a_degraded_related_panel_still_reports_the_render(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    """The note body renders even when the panel fails, so the render is a
    success with a degraded section — not an error. Conflating them would hide
    a failing panel behind a healthy render count."""

    class NoRelatedClient(FakeLithosClient):
        async def related(self, knowledge_id: str) -> RelatedNeighborhood:
            raise LithosToolError("related is unwell", code="tool_error")

    with _client(lithos_lens_config_env, NoRelatedClient()) as client:
        assert client.get(f"/note/{DEMO_NOTE}").status_code == 200

    span = _route_span(spans, "/note/{knowledge_id}")
    assert _attr(span, "lens.outcome") == "rendered"
    assert _attr(span, "lens.related.state") == "error"


def test_an_unloaded_panel_records_no_latency_sample(
    lithos_lens_config_env: Path, metric_reader: InMemoryMetricReader
) -> None:
    """A not-found page never loads a panel, so it must not contribute a zero.

    Zeros from pages that did no work would drag the latency distribution down
    and make a genuinely slow panel look fast on average — the metric reading
    healthiest as more requests fail.
    """
    from tests.conftest import metric_points

    with _client(lithos_lens_config_env) as client:
        assert client.get("/note/no-such-note").status_code == 200

    assert metric_points(metric_reader, "lens_knowledge_related_duration_seconds") == []


# ── lens.knowledge.search ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "mode"),
    [
        ("/knowledge?q=influx", "search"),
        ("/knowledge", "browse"),
        ("/knowledge?tag=project%3Ainflux", "browse"),
    ],
)
def test_each_landing_branch_reports_its_mode(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
    path: str,
    mode: str,
) -> None:
    """Hybrid search and the two recency-browse branches are different backend
    calls with different costs; one counter for "the landing page" would hide
    which is being used."""
    with _client(lithos_lens_config_env) as client:
        assert client.get(path).status_code == 200

    span = _route_span(spans, "/knowledge")
    assert _attr(span, "lens.mode") == mode
    assert _attr(span, "lens.result_count") >= 0
    assert (
        metric_value(metric_reader, "lens_knowledge_searches_total", mode=mode).value
        == 1
    )


def test_the_search_query_never_reaches_a_metric_label(
    lithos_lens_config_env: Path,
    metric_reader: InMemoryMetricReader,
) -> None:
    """Unbounded operator input must not become a Prometheus series.

    A query on a label would mint one series per distinct search — the exact
    cardinality failure the catalogue's rule exists to prevent, and reachable
    by anyone who can reach the route. Mode and count answer the operational
    question without it.
    """
    secret = "zzsecretquerystringzz"

    with _client(lithos_lens_config_env) as client:
        assert client.get(f"/knowledge?q={secret}").status_code == 200

    data = metric_reader.get_metrics_data()
    assert data is not None
    for resource_metric in data.resource_metrics or []:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                for point in metric.data.data_points:
                    assert secret not in str(dict(point.attributes or {}))


def test_an_oversized_query_string_is_bounded_on_the_span(
    lithos_lens_config_env: Path, spans: InMemorySpanExporter
) -> None:
    """The query DOES reach the trace, via the instrumentation's own
    `http.target`, and it is bounded there for the reason logs are.

    Found by writing the test above: `logging.py` bounds request-shaped values
    because a 47 KB query string on an unauthenticated route wrote a 47 KB log
    line — and the span attribute was copying the same input verbatim to the
    collector, a path that never inherited that ceiling.

    Bounded rather than stripped: unlike a metric label the query costs no
    series, and it is genuinely useful when reading a trace. What it must not
    do is carry unbounded volume.
    """
    from lithos_lens.logging import MAX_LOGGED_VALUE_CHARS

    with _client(lithos_lens_config_env) as client:
        assert client.get(f"/knowledge?q={'q' * 5000}").status_code == 200

    attributes = dict(_route_span(spans, "/knowledge").attributes or {})
    urls = [
        value
        for key, value in attributes.items()
        if key in {"http.target", "http.url", "url.full", "url.query"}
        and isinstance(value, str)
    ]
    assert urls, "no URL attribute was recorded; this test would prove nothing"
    for value in urls:
        assert len(value) <= MAX_LOGGED_VALUE_CHARS + 64, f"{value[:60]} unbounded"


def test_an_offline_lithos_is_reported_as_offline_not_as_an_empty_result(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    """ "Nothing matched" and "the backend is down" both render an empty page."""

    class OfflineClient(FakeLithosClient):
        async def health(self) -> Any:
            return "unreachable"

    with _client(lithos_lens_config_env, OfflineClient()) as client:
        assert client.get("/knowledge?q=influx").status_code == 200

    assert _attr(_route_span(spans, "/knowledge"), "lens.mode") == "offline"
    assert (
        metric_value(
            metric_reader, "lens_knowledge_searches_total", mode="offline"
        ).value
        == 1
    )


# ── lens.knowledge.resolve ────────────────────────────────────────────


def test_a_title_resolution_is_distinguishable_from_a_uuid_one(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    """The distinction `kind` cannot express, and the reason `via` exists.

    A uuid target, a path probe and a single title match all answer
    ``redirect``. A corpus resolving entirely by uuid is working for a
    different reason than one resolving by title, and only the second is
    evidence the wiki-link convention is being used as intended.
    """
    with _client(lithos_lens_config_env) as client:
        assert (
            client.get(
                f"/knowledge/resolve?target={quote(DEMO_TITLE)}&from={DEMO_NOTE}",
                follow_redirects=False,
            ).status_code
            == 302
        )
        assert (
            client.get(
                f"/knowledge/resolve?target={DEMO_UUID}&from={DEMO_NOTE}",
                follow_redirects=False,
            ).status_code
            == 302
        )

    for outcome in ("title", "uuid"):
        assert (
            metric_value(
                metric_reader, "lens_knowledge_resolves_total", outcome=outcome
            ).value
            == 1
        ), f"{outcome} resolutions were not counted under their own arm"


def test_a_redirect_is_counted_before_it_returns(
    lithos_lens_config_env: Path, metric_reader: InMemoryMetricReader
) -> None:
    """The confident resolutions leave the handler early.

    Recording after the redirect branch would count only the failures, so
    resolution would look broken exactly when it was working.
    """
    with _client(lithos_lens_config_env) as client:
        assert (
            client.get(
                f"/knowledge/resolve?target={DEMO_UUID}&from=x", follow_redirects=False
            ).status_code
            == 302
        )

    from tests.conftest import metric_points

    assert metric_points(metric_reader, "lens_knowledge_resolves_total"), (
        "a redirecting resolution recorded nothing"
    )


def test_an_unresolvable_link_is_counted_as_unresolved(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    """A rising unresolved share is the corpus growing dead links."""
    with _client(lithos_lens_config_env) as client:
        assert (
            client.get(
                "/knowledge/resolve?target=no-such-note-anywhere&from=x",
                follow_redirects=False,
            ).status_code
            == 200
        )

    span = _route_span(spans, "/knowledge/resolve")
    assert _attr(span, "lens.outcome") == "unresolved"
    assert _attr(span, "lens.candidate_count") == 0
    assert (
        metric_value(
            metric_reader, "lens_knowledge_resolves_total", outcome="unresolved"
        ).value
        == 1
    )


def test_an_offline_resolve_is_not_reported_as_a_dead_link(
    lithos_lens_config_env: Path,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
) -> None:
    """Both show the unresolved page, and confusing them would read as the
    corpus rotting when the backend is merely down."""

    class OfflineClient(FakeLithosClient):
        async def health(self) -> Any:
            return "unreachable"

    with _client(lithos_lens_config_env, OfflineClient()) as client:
        assert (
            client.get(
                f"/knowledge/resolve?target={DEMO_UUID}&from=x", follow_redirects=False
            ).status_code
            == 200
        )

    assert _attr(_route_span(spans, "/knowledge/resolve"), "lens.outcome") == "offline"
    assert (
        metric_value(
            metric_reader, "lens_knowledge_resolves_total", outcome="offline"
        ).value
        == 1
    )
