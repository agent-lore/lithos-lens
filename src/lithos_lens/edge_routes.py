"""Add a dependency, in sentences, with a confirm step (T3-W8, D11; §5C.2).

Direction is the mistake this action exists to prevent, so the operator never
picks an edge's ``from`` or ``to``: on an open task's detail page they pick a
sentence about it (``relation_sentences``) and name the other task, and the
relation is restated with both titles and its readiness meaning before
anything is written.

- ``GET /tasks/{task_id}/edges/new`` — the confirm step, a read. It resolves
  the other task by id or prefix (``lithos_task_get``; an ambiguous prefix
  lists its candidates under the input, as text, for the operator to retype),
  reads the focal task's edges FRESH, and either says the relation already
  exists — with no form — or restates it and offers the one form that writes
  it. That form carries the resolved full ids, the type, and the focal task's
  status as ``expected_status``.
- ``POST /tasks/{task_id}/edges`` — one ``lithos_task_edge_upsert`` through
  the write funnel, with no metadata (REQUIREMENTS: the edge's ``created_by``
  already records the operator). The upsert replaces an existing edge's
  metadata, and other agents' edge writes emit no event, so ``perform`` reads
  the focal task's edges fresh FIRST: a relation already there is the
  "already exists" outcome — a success that writes nothing (D5) — and a read
  that fails writes nothing either, since an upsert could then replace the
  metadata of an edge Lens could not see (D3). After the call both endpoints
  leave the edge cache: the page the redirect lands on reads them fresh, and
  other tabs converge on the cache's TTL or their next event (S3).

The residual, stated rather than closed: another agent inserting the same
relation between the fresh read and the upsert has its metadata replaced by
Lens's (empty) metadata. A strict guarantee needs an upstream ``created``
signal. There is no removal here (S1): a mis-drawn edge is removed outside
Lens with ``lithos_task_edge_delete``.

Registered by the write route group, which owns the funnel, like cancel and
create.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from functools import partial
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from lithos_lens.create_form import FieldError
from lithos_lens.lithos_client import LithosClientProtocol, LithosToolError
from lithos_lens.receipts import EdgeFacts, ReceiptTask
from lithos_lens.relation_sentences import (
    Relation,
    RelationSentence,
    existing_edge,
    readiness,
    sentence_named,
    sentence_of,
    sentences_for,
)
from lithos_lens.state import AppState, HealthSnapshot
from lithos_lens.task_graph import EdgeRecord
from lithos_lens.task_links import LINK_READ_TIMEOUT_S
from lithos_lens.tasks import TaskRecord, task_detail_path
from lithos_lens.write_errors import (
    CONFLICT,
    CONFLICT_STATUS,
    REFUSED,
    TaskRef,
    WriteProblem,
    funnel_problem,
    map_write_error,
)
from lithos_lens.write_funnel import TaskWrite, WriteDone, WriteForm, WriteFunnel
from lithos_lens.write_guards import safe_next

logger = logging.getLogger(__name__)

__all__ = [
    "ALREADY_EXISTS",
    "UNCHECKED_COPY",
    "edge_new_path",
    "edge_path",
    "offers_relation",
    "register_edge_routes",
]

#: What both steps say of a relation the fresh read found (D5); the banner
#: adds "(added by <agent>, <date>)" when the edge carries those stamps.
ALREADY_EXISTS = "This relation already exists"

#: The refusal when the fresh read before the upsert failed (D3).
UNCHECKED_COPY = (
    "Lens couldn't check whether this relation exists — nothing was written."
)

CONFIRM_PAGE = "writes/edge_confirm.html"
CONFLICT_PAGE = "writes/conflict.html"

_BAD_REQUEST = 400
_UNAVAILABLE = 503


def edge_new_path(task_id: str) -> str:
    """The confirm step's path, the id as ONE encoded segment."""
    return f"/tasks/{quote(task_id, safe='')}/edges/new"


def edge_path(task_id: str) -> str:
    """The write's path."""
    return f"/tasks/{quote(task_id, safe='')}/edges"


def offers_relation(task: TaskRecord | None) -> bool:
    """Whether a page showing ``task`` offers Add dependency: an OPEN task.

    Whether an identity resolves is the partial's other question.
    """
    return task is not None and task.status == "open"


def _named(task: TaskRecord) -> ReceiptTask:
    return ReceiptTask(task_id=task.id, title=task.title)


def _field_error(problem: WriteProblem, typed: str) -> FieldError:
    """An upstream refusal of the typed id, as the message under the input."""
    if problem.candidates:
        return FieldError(
            f"'{typed}' matches more than one task — type more of the id, or the "
            "whole id:",
            candidates=problem.candidates,
        )
    return FieldError(" ".join(filter(None, (problem.headline, problem.detail_text))))


def register_edge_routes(
    app: FastAPI,
    state: AppState,
    templates: Jinja2Templates,
    funnel: WriteFunnel,
) -> None:
    """Attach the add-dependency routes and their template globals."""

    templates.env.globals["offers_relation"] = offers_relation
    templates.env.globals["relation_sentences"] = sentences_for
    templates.env.globals["edge_new_path"] = edge_new_path
    templates.env.globals["edge_path"] = edge_path
    templates.env.globals["relation_already_exists"] = ALREADY_EXISTS

    async def fresh_edges(
        client: LithosClientProtocol, task_id: str
    ) -> Sequence[EdgeRecord]:
        """The task's edges as Lithos has them NOW: its cache entry evicted
        and read again, under the link-read deadline (F4)."""

        async def fetch(fetched_id: str) -> list[EdgeRecord]:
            return await asyncio.wait_for(
                client.task_edge_list(fetched_id, direction="both"),
                LINK_READ_TIMEOUT_S,
            )

        state.graph_cache.evict(task_id)
        return (await state.graph_cache.edges_for(task_id, fetch)).edges

    async def titled(client: LithosClientProtocol, task_id: str) -> ReceiptTask:
        """A receipt name for ``task_id``; its short id alone if unreadable."""
        try:
            task = await asyncio.wait_for(client.task_get(task_id), LINK_READ_TIMEOUT_S)
        except Exception:
            return ReceiptTask(task_id=task_id)
        return _named(task)

    def add_edge(relation: Relation) -> TaskWrite:
        """The add-dependency action, as the funnel drives it (D3)."""

        async def perform(
            client: LithosClientProtocol, task: TaskRecord, operator: str
        ) -> WriteDone | WriteProblem:
            subject = TaskRef(task_id=task.id, title=task.title)
            try:
                edges = await fresh_edges(client, task.id)
            except Exception:
                logger.warning("edge write pre-read failed", extra={"task_id": task.id})
                return funnel_problem(
                    "edge_upsert",
                    REFUSED,
                    UNCHECKED_COPY,
                    code="precheck_failed",
                    status_code=_UNAVAILABLE,
                    subject=subject,
                    hint="Reload once Lithos is reachable.",
                )
            other_id = relation.other(task.id)
            existing = existing_edge(edges, relation)
            if existing is not None:
                # No call (D5). The other end's entry may predate the
                # relation — another agent's edge write evicted nothing.
                state.graph_cache.evict(other_id)
                ends = {task.id: _named(task), other_id: await titled(client, other_id)}
                return WriteDone(
                    task=_named(task),
                    edge=EdgeFacts(
                        source=ends[relation.from_task_id],
                        target=ends[relation.to_task_id],
                        edge_type=relation.type,
                        already_exists=True,
                        created_by=existing.created_by,
                        created_at=existing.created_at,
                    ),
                )
            try:
                result = await client.task_edge_upsert(
                    from_task_id=relation.from_task_id,
                    to_task_id=relation.to_task_id,
                    edge_type=relation.type,
                    agent=operator,
                    metadata=None,
                )
            finally:
                # Refused, applied or unanswered — an unanswered upsert may
                # still have landed — neither end's cached edges are trusted.
                state.graph_cache.evict(relation.from_task_id)
                state.graph_cache.evict(relation.to_task_id)
            # Lithos names both ends, resolved and titled, in its answer.
            return WriteDone(
                task=_named(task),
                answer=asdict(result),
                edge=EdgeFacts(
                    source=ReceiptTask(
                        task_id=result.from_task_id or relation.from_task_id,
                        title=result.from_title,
                    ),
                    target=ReceiptTask(
                        task_id=result.to_task_id or relation.to_task_id,
                        title=result.to_title,
                    ),
                    edge_type=relation.type,
                ),
            )

        def admits(task: TaskRecord) -> tuple[str, str] | None:
            if not offers_relation(task):
                return (
                    "not_open",
                    "A dependency is added from an open task's page; this one is "
                    f"{task.status}.",
                )
            if sentence_of(relation, task) is None:
                # Not a relation this task's page offers: a form Lens did
                # not render (the confirm step only ever posts one it did).
                return (
                    "bad_relation",
                    "The form didn't describe a relation this task's page "
                    "offers — go back to the task and start again.",
                )
            return None

        return TaskWrite(action="edge_upsert", perform=perform, admits=admits)

    def render(
        request: Request,
        *,
        next_url: str,
        health: HealthSnapshot,
        task: TaskRecord | None = None,
        sentence: RelationSentence | None = None,
        typed: str = "",
        errors: Mapping[str, FieldError] | None = None,
        notice: str = "",
        status_code: int = 200,
        **confirm: Any,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            CONFIRM_PAGE,
            {
                "config": state.config,
                "health": health,
                "active_view": "tasks",
                "task": task,
                "sentence": sentence,
                "typed": typed,
                "errors": dict(errors or {}),
                "notice": notice,
                "next_url": next_url,
                **confirm,
            },
            status_code=status_code,
        )

    def render_conflict(
        request: Request, problem: WriteProblem, health: HealthSnapshot
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            CONFLICT_PAGE,
            {
                "config": state.config,
                "health": health,
                "active_view": "tasks",
                "problem": problem,
            },
            status_code=problem.status_code,
        )

    @app.get("/tasks/{task_id}/edges/new", response_class=HTMLResponse)
    async def relation_confirm(request: Request, task_id: str) -> Response:
        """The confirm step (D2): resolve, read fresh, restate. A read."""
        params = request.query_params
        back_to = safe_next(params.get("next"), default=task_detail_path(task_id))
        typed = str(params.get("other") or "").strip()
        snapshot = await state.refresh_health()
        page = partial(render, request, next_url=back_to, health=snapshot, typed=typed)
        if snapshot.lithos != "ok":
            return page(
                notice="Lithos is unreachable, so Lens can't check this relation.",
                status_code=_UNAVAILABLE,
            )
        client = state.lithos_client
        try:
            task = await client.task_get(task_id)
        except Exception as exc:
            if isinstance(exc, LithosToolError) and exc.code == "task_not_found":
                problem = funnel_problem(
                    "edge_upsert",
                    CONFLICT,
                    "This task no longer exists.",
                    code="task_not_found",
                    status_code=404,
                    subject=TaskRef(task_id=task_id),
                    hint="It may have been removed since you loaded the page.",
                )
                return render_conflict(request, problem, snapshot)
            logger.warning("relation confirm read failed", extra={"task_id": task_id})
            return page(
                notice="Lens couldn't read this task. Reload once Lithos is reachable.",
                status_code=_UNAVAILABLE,
            )
        if not offers_relation(task):
            problem = funnel_problem(
                "edge_upsert",
                CONFLICT,
                f"This task is now {task.status}.",
                code="stale_status",
                status_code=CONFLICT_STATUS,
                subject=TaskRef(task_id=task.id, title=task.title),
                hint="Reload to see the task as it is now.",
            )
            return render_conflict(request, problem, snapshot)
        page = partial(page, task=task)
        sentence = sentence_named(str(params.get("relation") or ""), task)
        if sentence is None:
            return page(
                errors={"relation": FieldError("Choose one of the relations offered.")},
                status_code=_BAD_REQUEST,
            )
        page = partial(page, sentence=sentence)
        if not typed:
            return page(
                errors={
                    "other": FieldError(
                        "Name the other task: its full id, or at least six "
                        "characters of it."
                    )
                },
                status_code=_BAD_REQUEST,
            )
        try:
            other = await client.task_get(typed)
        except Exception as exc:
            if isinstance(exc, LithosToolError) and exc.envelope:
                problem = map_write_error(
                    "edge_upsert", exc.envelope, subject=TaskRef(task_id=task.id)
                )
                return page(
                    errors={"other": _field_error(problem, typed)},
                    status_code=problem.status_code,
                )
            logger.warning("relation confirm read failed", extra={"task_id": typed})
            return page(
                notice="Lens couldn't read the other task. Reload once Lithos is "
                "reachable.",
                status_code=_UNAVAILABLE,
            )
        if other.id == task.id:
            return page(
                errors={"other": FieldError("A task can't depend on itself.")},
                status_code=_BAD_REQUEST,
            )
        relation = sentence.relation(task.id, other.id)
        try:
            edges = await fresh_edges(client, task.id)
        except Exception:
            logger.warning(
                "relation confirm edge read failed", extra={"task_id": task.id}
            )
            return page(
                notice="Lens couldn't check whether this relation already exists. "
                "Reload once Lithos is reachable.",
                status_code=_UNAVAILABLE,
            )
        source, target = (
            (task, other) if relation.from_task_id == task.id else (other, task)
        )
        return page(
            other=other,
            relation=relation,
            terms=sentence.terms(task, other),
            existing=existing_edge(edges, relation),
            readiness=readiness(relation, source, target),
        )

    @app.post("/tasks/{task_id}/edges")
    async def add_dependency(request: Request, task_id: str) -> Response:
        """Add one dependency edge (D3): one upsert, via the funnel — or none."""
        form = await request.form()
        relation = Relation(
            from_task_id=str(form.get("from_task_id") or ""),
            to_task_id=str(form.get("to_task_id") or ""),
            type=str(form.get("type") or ""),
        )
        return await funnel.submit(
            request,
            WriteForm(
                task_id=task_id,
                expected_status=str(form.get("expected_status") or ""),
                next_url=str(form.get("next") or ""),
                arguments={
                    "from_task_id": relation.from_task_id,
                    "to_task_id": relation.to_task_id,
                    "type": relation.type,
                },
            ),
            add_edge(relation),
        )
