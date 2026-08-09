"""In-memory fake Lithos client for the e2e / demo "fake-Lithos app mode".

The real :class:`~lithos_lens.lithos_client.LithosClient` needs a live Lithos
MCP-over-SSE server to answer any tool call. That makes the shipped UI awkward
to drive from an end-to-end browser suite (or to demo offline): there is no
Lithos to point it at.

:class:`FakeLithosClient` satisfies the same
:class:`~lithos_lens.lithos_client.LithosClientProtocol` over an immutable
:class:`~lithos_lens.fake_dataset.FakeLithosDataset` — by default the shipped
demo set (:func:`~lithos_lens.fake_dataset.demo_dataset`), with enough tasks,
claims, findings, notes, and graph edges for every server-rendered surface to
light up. This module holds only the client *behavior* (filtering, scoping,
coded error envelopes); the fixture *data* is the dataset's job, so tests can
compose their own sets and the demo stays an editable artifact.

When the ``LITHOS_LENS_FAKE_LITHOS`` environment variable is truthy the
application factory (:func:`lithos_lens.web.create_app`) wires this client in
instead of the real one, so ``uv run lithos-lens`` boots a fully browsable app
with no Lithos behind it. The Playwright smoke suite under ``e2e/`` drives
exactly that mode.

The fake deliberately lives next to the shipped code (not under ``tests/``)
because this is a real, launchable application mode, not a test double that
only the unit suite can see. Contract fidelity with the real client is pinned
by the fake↔real matrix in ``tests/test_lithos_contract.py``.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from lithos_lens.config import LithosConfig
from lithos_lens.events import EventHub
from lithos_lens.fake_dataset import FakeLithosDataset, demo_dataset
from lithos_lens.knowledge import RelatedNeighborhood
from lithos_lens.lithos_client import LithosHealth, LithosToolError
from lithos_lens.task_graph import BlockedTaskRecord, EdgeRecord
from lithos_lens.tasks import (
    AgentRecord,
    ClaimRecord,
    FindingRecord,
    NoteRecord,
    NoteSummary,
    TaskRecord,
    TaskStatusRecord,
)

__all__ = ["FakeEventHub", "FakeLithosClient", "fake_lithos_enabled"]

_TRUTHY = {"1", "true", "yes", "on"}

_EDGE_DIRECTIONS = ("incoming", "outgoing", "both")


def _in_scope(task: TaskRecord, project: str | None, tags: list[str] | None) -> bool:
    """Whether ``task`` satisfies a scoped frontier read's project/tag filters.

    Mirrors how the real Lithos scopes ``lithos_task_ready`` /
    ``lithos_task_blocked``: ``project`` matches the app-wide ``project:<name>``
    tag convention, and every entry in ``tags`` must be present. ``None``/empty
    filters match everything.
    """
    if project and f"project:{project}" not in task.tags:
        return False
    return not tags or all(tag in task.tags for tag in tags)


def _parse_since(since: str) -> datetime:
    """Parse a findings ``since`` filter exactly like upstream
    ``lithos_finding_list``: ``datetime.fromisoformat``, a naive value treated
    as already-UTC, and a malformed value answered with the ``invalid_input``
    envelope."""
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError:
        raise LithosToolError(
            f"Invalid since datetime: {since}", code="invalid_input"
        ) from None
    return _as_utc(parsed)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _created_after(created_at: str, since_utc: datetime) -> bool:
    """Strict ``created_at > since``, mirroring the upstream SQL filter. A
    ``created_at`` that doesn't parse (e.g. a composed record leaving the
    default ``""``) is excluded rather than crashing the read."""
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    return _as_utc(created) > since_utc


def fake_lithos_enabled() -> bool:
    """Return whether the fake-Lithos app mode is switched on via the environment.

    Reads ``LITHOS_LENS_FAKE_LITHOS``. Any of ``1/true/yes/on`` (case- and
    whitespace-insensitive) enables it; anything else — including unset — leaves
    the app on the real :class:`~lithos_lens.lithos_client.LithosClient`.
    """
    return os.environ.get("LITHOS_LENS_FAKE_LITHOS", "").strip().lower() in _TRUTHY


class FakeEventHub(EventHub):
    """Hermetic in-process event hub for fake-Lithos app mode.

    The real :class:`~lithos_lens.events.EventHub` run loop dials the
    configured Lithos ``/events`` SSE endpoint — an outbound connection fake
    mode must never make. This subclass keeps the whole subscriber surface
    (``subscribe`` / ``publish`` — so the browser-facing ``/tasks/events``
    endpoint and its initial connected signal keep functioning) but its run
    loop just reports ``live`` and idles until stop: it genuinely serves the
    in-process stream, which is why the status is honest rather than
    "disabled".
    """

    async def start(self) -> None:
        await super().start()
        if self.config.enabled:
            # The in-process stream needs no connection phase: it is live the
            # moment the run task exists, not after a first loop tick.
            self.status = "live"

    async def _run(self) -> None:
        self.status = "live"
        await self._stop.wait()


class FakeLithosClient:
    """A protocol-complete Lithos client backed by an in-memory dataset.

    Every method is a pure lookup over ``dataset`` (the shipped
    :func:`~lithos_lens.fake_dataset.demo_dataset` unless a test composes its
    own); nothing touches the network. ``health()`` always reports ``"ok"`` so
    the whole dashboard renders. The constructor accepts a
    :class:`LithosConfig` purely so it is drop-in swappable for
    :class:`LithosClient` in the app factory — the config is otherwise unused.
    """

    def __init__(
        self,
        config: LithosConfig | None = None,
        *,
        dataset: FakeLithosDataset | None = None,
    ) -> None:
        self._config = config
        self.dataset = dataset if dataset is not None else demo_dataset()
        self.closed = False

    # ── lifecycle ──────────────────────────────────────────────────────

    async def startup(self) -> None:
        return None

    async def health(self) -> LithosHealth:
        return "ok"

    async def register_agent(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True

    # ── reads ──────────────────────────────────────────────────────────

    async def list_tasks(
        self,
        *,
        agent: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        since: str | None = None,
        with_claims: bool = False,
    ) -> list[TaskRecord]:
        rows = [
            task
            for task in self.dataset.tasks
            if status is None or task.status == status
        ]
        if agent:
            rows = [task for task in rows if task.created_by == agent]
        if tags:
            rows = [task for task in rows if all(tag in task.tags for tag in tags)]
        if since:
            # Upstream lithos_task_list filters `created_at >= ?` on the raw
            # ISO strings — inclusive, full precision (deliberately unlike
            # findings' strict parsed >), so the fake compares the same way.
            rows = [task for task in rows if task.created_at >= since]
        if with_claims:
            rows = [replace(task, claims=self._claims_for(task.id)) for task in rows]
        return rows

    async def task_ready(
        self,
        *,
        limit: int | None = None,
        with_claims: bool = False,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[TaskRecord]:
        rows = [
            task
            for task in self.dataset.tasks
            if task.id in self.dataset.ready_ids
            and task.status == "open"
            and _in_scope(task, project, tags)
        ]
        if with_claims:
            rows = [replace(task, claims=self._claims_for(task.id)) for task in rows]
        return rows[:limit] if limit is not None else rows

    async def task_blocked(
        self,
        *,
        limit: int | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
    ) -> list[BlockedTaskRecord]:
        rows = [
            BlockedTaskRecord(task=task, blockers=self.dataset.blocked[task.id])
            for task in self.dataset.tasks
            if task.id in self.dataset.blocked
            and task.status == "open"
            and _in_scope(task, project, tags)
        ]
        return rows[:limit] if limit is not None else rows

    async def task_get(self, task_id: str) -> TaskRecord:
        task = self._by_id(task_id)
        if task is None:
            # Mirror the concrete client: a missing task is an error envelope
            # (code=task_not_found), surfaced as a coded LithosToolError.
            raise LithosToolError(f"Task '{task_id}' not found.", code="task_not_found")
        return task

    async def task_children(
        self,
        task_id: str,
        *,
        recursive: bool = False,
        include_closed: bool = False,
    ) -> list[TaskRecord]:
        child_ids = list(self.dataset.children.get(task_id, ()))
        if recursive:
            queue = list(child_ids)
            while queue:
                for cid in self.dataset.children.get(queue.pop(), ()):
                    if cid not in child_ids:
                        child_ids.append(cid)
                        queue.append(cid)
        rows = [task for cid in child_ids if (task := self._by_id(cid)) is not None]
        if not include_closed:
            rows = [task for task in rows if task.status == "open"]
        return rows

    async def task_edge_list(
        self,
        task_id: str,
        *,
        direction: str = "both",
        types: list[str] | None = None,
    ) -> list[EdgeRecord]:
        if direction not in _EDGE_DIRECTIONS:
            # Upstream lithos_task_edge_list answers an unknown direction with
            # an invalid_input error envelope, not a silently-empty filter.
            raise LithosToolError(
                "direction must be 'incoming', 'outgoing', or 'both', "
                f"got {direction!r}.",
                code="invalid_input",
            )
        rows = list(self.dataset.edges.get(task_id, ()))
        if direction != "both":
            rows = [edge for edge in rows if edge.direction == direction]
        if types:
            rows = [edge for edge in rows if edge.type in types]
        return rows

    async def task_status(self, task_id: str) -> TaskStatusRecord | None:
        task = self._by_id(task_id)
        if task is None:
            return None
        return TaskStatusRecord(
            id=task.id,
            title=task.title,
            status=task.status,
            claims=self._claims_for(task_id),
        )

    async def list_findings(
        self, task_id: str, *, since: str | None = None
    ) -> list[FindingRecord]:
        rows = list(self.dataset.findings.get(task_id, ()))
        if since:
            since_utc = _parse_since(since)
            rows = [f for f in rows if _created_after(f.created_at, since_utc)]
        return rows

    async def stats(self) -> dict[str, Any]:
        return dict(self.dataset.stats)

    async def list_agents(self) -> list[AgentRecord]:
        return list(self.dataset.agents)

    async def read_note(
        self, knowledge_id: str, *, max_length: int | None = None
    ) -> NoteRecord | None:
        note = self.dataset.notes.get(knowledge_id)
        if note is None:
            # Parity with the concrete client: upstream lithos_read answers a
            # missing doc with an error envelope code "doc_not_found", which
            # LithosClient raises as a coded LithosToolError. The note route
            # maps that code to its not-found banner.
            raise LithosToolError(
                f"Document not found: {knowledge_id}", code="doc_not_found"
            )
        if max_length is not None:
            # Mirror upstream truncation (used by the related-panel title
            # fan-out's cheap max_length=1 reads) without mutating the fixture.
            note = replace(note, content=note.content[:max_length])
        return note

    async def read_note_by_path(self, path: str) -> NoteRecord | None:
        """Resolve a note by path for the wiki-link resolver's existence probe.

        Looks the exact path up in the dataset's explicit ``note_paths``
        mapping (path -> note id — deliberately DISTINCT values, so the
        path->UUID workflow is exercised for real). A miss returns ``None``
        (parity with the concrete client, which maps ``doc_not_found`` to
        ``None`` on this probe), never a raised error.
        """
        note_id = self.dataset.note_paths.get(path)
        if note_id is None:
            return None
        return self.dataset.notes.get(note_id)

    async def list_notes(
        self,
        *,
        title_contains: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[NoteSummary]:
        paths_by_id = {
            note_id: path for path, note_id in self.dataset.note_paths.items()
        }
        rows = [
            NoteSummary(
                id=note.id,
                title=note.title,
                path=paths_by_id.get(note.id, ""),
                tags=note.tags,
            )
            for note in self.dataset.notes.values()
        ]
        if title_contains:
            needle = title_contains.lower()
            rows = [row for row in rows if needle in row.title.lower()]
        if tags:
            rows = [row for row in rows if all(tag in row.tags for tag in tags)]
        return rows[:limit] if limit is not None else rows

    async def related(self, knowledge_id: str) -> RelatedNeighborhood:
        """Neighborhood lookup so the K1-S4 related panel lights up.

        An unknown id answers the production contract: upstream lithos_related
        returns the doc_not_found error envelope for a missing document, so the
        fake raises the same coded error. A KNOWN note with no fixture
        neighborhood still gets an empty read (a document with no relations is
        a success upstream).
        """
        if knowledge_id not in self.dataset.notes:
            raise LithosToolError(
                f"Document not found: {knowledge_id}", code="doc_not_found"
            )
        return self.dataset.related_neighborhoods.get(
            knowledge_id, RelatedNeighborhood()
        )

    # ── helpers ────────────────────────────────────────────────────────

    def _by_id(self, task_id: str) -> TaskRecord | None:
        return next((task for task in self.dataset.tasks if task.id == task_id), None)

    def _claims_for(self, task_id: str) -> tuple[ClaimRecord, ...]:
        return self.dataset.claims.get(task_id, ())
