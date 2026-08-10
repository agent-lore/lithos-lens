"""Vendored Lithos tool contracts — the single source of truth for tool shapes.

Every Lens tool call has three moving parts that must match the Lithos server
exactly: the tool *name*, the *arguments* it accepts (FastMCP rejects an
unexpected argument outright), and the response *envelope* key that holds the
rows. Historically each was re-derived by hand at every call site, encoded as a
bare string literal plus a "verified against the source" comment — and three
times running an invented shape slipped past review:

* **#23** — ``lithos_task_ready`` / ``lithos_task_list`` ``with_claims`` was
  omitted when False, silently inverting Lens's default against upstream's.
* **#26** — the ``lithos_related`` call sent invented arguments and read an
  invented flat payload instead of the real nested one.
* **#30** — ``lithos_list`` rows were read from invented ``notes`` /
  ``documents`` / ``results`` aliases; the real (and only) container key is
  ``items``.

This module vendors those contracts as data so a shape can be *stated once,
with provenance*, and enforced rather than re-guessed. :class:`LithosClient`
routes every call through :func:`check_arguments` (an out-of-contract argument
is a Lens bug and fails loudly) and reads list payloads through
:func:`envelope_rows` (the container key comes from here, never a literal). A
new tool, argument, or envelope key is therefore a deliberate, reviewed edit to
:data:`CONTRACTS` — with a ``source`` citation — instead of an inline invention.

Provenance: the shapes below are pinned to the Lithos 0.4 MCP tool source —
``tools/tasks.py`` (task_* + finding_list), ``tools/read_search.py``
(read / related / list), ``tools/agents.py`` (agent_register / agent_list),
``tools/stats.py`` — the same sources the fake↔real contract matrix
(``tests/test_lithos_contract.py``) and the graph-read fixtures cite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class LithosContractError(RuntimeError):
    """Raised when a tool call violates the vendored contract.

    Distinct from :class:`~lithos_lens.lithos_client.LithosToolError` (a runtime
    error *from* Lithos): this signals a Lens-side bug — an unregistered tool or
    an argument the tool does not accept — caught before it ever hits the wire.
    """


@dataclass(frozen=True)
class ToolContract:
    """The vendored shape of one Lithos MCP tool.

    ``arguments`` is the set of argument names Lens may send (a subset of, or
    equal to, the tool's accepted surface); sending anything outside it is a
    contract violation. ``required`` is the subset Lens must send on *every*
    call — the default-sensitive flags and mandatory keys where omission is the
    bug, not a valid default (``with_claims`` is the #23 escape: upstream
    ``lithos_task_ready`` defaults it to true, so dropping it silently inverts
    Lens's false default). ``rows_key`` is the response envelope key holding the
    row list for a list-returning tool, or ``""`` for tools that return a single
    object or a whole-payload dict. ``error_codes`` documents the coded error
    envelopes the tool can surface. ``source`` cites the upstream tool source.
    """

    name: str
    arguments: frozenset[str] = field(default_factory=frozenset)
    required: frozenset[str] = field(default_factory=frozenset)
    rows_key: str = ""
    error_codes: frozenset[str] = field(default_factory=frozenset)
    source: str = ""

    def __post_init__(self) -> None:
        # A required argument must also be an accepted one — otherwise the
        # registry contradicts itself and every call would be unsatisfiable.
        stray = self.required - self.arguments
        if stray:
            raise LithosContractError(
                f"{self.name}: required arguments {sorted(stray)} are not in "
                "the accepted set"
            )


def _contract(
    name: str,
    *,
    arguments: tuple[str, ...] = (),
    required: tuple[str, ...] = (),
    rows_key: str = "",
    error_codes: tuple[str, ...] = (),
    source: str,
) -> ToolContract:
    return ToolContract(
        name=name,
        arguments=frozenset(arguments),
        required=frozenset(required),
        rows_key=rows_key,
        error_codes=frozenset(error_codes),
        source=source,
    )


CONTRACTS: dict[str, ToolContract] = {
    contract.name: contract
    for contract in (
        _contract(
            "lithos_agent_register",
            arguments=("id", "name", "type"),
            required=("id", "name", "type"),
            source="tools/agents.py::lithos_agent_register",
        ),
        _contract(
            "lithos_agent_list",
            rows_key="agents",
            source="tools/agents.py::lithos_agent_list",
        ),
        _contract(
            "lithos_stats",
            source="tools/stats.py::lithos_stats (whole-payload dict)",
        ),
        # ── tasks surface ───────────────────────────────────────────────
        _contract(
            "lithos_task_list",
            arguments=("with_claims", "agent", "status", "tags", "since"),
            # with_claims is always sent explicitly so an upstream default flip
            # can't silently invert Lens's default (the #23 escape class).
            required=("with_claims",),
            rows_key="tasks",
            source="tools/tasks.py::lithos_task_list",
        ),
        _contract(
            "lithos_task_ready",
            arguments=("with_claims", "limit", "project", "tags"),
            # Upstream defaults with_claims to true; Lens's false default is only
            # honored if the flag is always sent — the #23 escape.
            required=("with_claims",),
            rows_key="tasks",
            source="tools/tasks.py::lithos_task_ready",
        ),
        _contract(
            "lithos_task_blocked",
            arguments=("limit", "project", "tags"),
            rows_key="tasks",
            source="tools/tasks.py::lithos_task_blocked",
        ),
        _contract(
            "lithos_task_get",
            arguments=("task_id",),
            required=("task_id",),
            # Primary success is a single {"task": {...}} object (bespoke in the
            # client); "tasks" is the accepted legacy list envelope.
            rows_key="tasks",
            error_codes=("task_not_found",),
            source="tools/tasks.py::lithos_task_get",
        ),
        _contract(
            "lithos_task_children",
            arguments=("task_id", "recursive", "include_closed"),
            required=("task_id",),
            rows_key="tasks",
            source="tools/tasks.py::lithos_task_children",
        ),
        _contract(
            "lithos_task_edge_list",
            arguments=("task_id", "direction", "types"),
            # direction is always sent (defaulting to "both") so its scoping is
            # never left to an upstream default.
            required=("task_id", "direction"),
            rows_key="edges",
            error_codes=("invalid_input",),
            source="tools/tasks.py::lithos_task_edge_list",
        ),
        _contract(
            "lithos_task_status",
            arguments=("task_id",),
            required=("task_id",),
            rows_key="tasks",
            source="tools/tasks.py::lithos_task_status",
        ),
        _contract(
            "lithos_finding_list",
            arguments=("task_id", "since"),
            required=("task_id",),
            rows_key="findings",
            error_codes=("invalid_input",),
            source="tools/tasks.py::lithos_finding_list",
        ),
        # ── knowledge surface ───────────────────────────────────────────
        _contract(
            "lithos_read",
            arguments=("id", "agent_id", "max_length", "path"),
            # agent_id is sent on every read (by-id and by-path alike); the
            # lookup key (id vs path) varies by call shape, so it is not required.
            required=("agent_id",),
            # Whole-payload note dict (frontmatter + content); no list envelope.
            error_codes=("doc_not_found",),
            source="tools/read_search.py::lithos_read",
        ),
        _contract(
            "lithos_related",
            arguments=("id", "include", "depth", "namespace"),
            # depth is always pinned to 1 (§6.5); id identifies the note.
            required=("id", "depth"),
            # Nested payload (links / provenance / edges); no flat list envelope.
            error_codes=("doc_not_found",),
            source="tools/read_search.py::lithos_related",
        ),
        _contract(
            "lithos_list",
            arguments=("title_contains", "tags", "limit"),
            # "items" is the one and only container key — never notes/documents/
            # results (results is lithos_search's key).
            rows_key="items",
            source="tools/read_search.py::lithos_list",
        ),
    )
}


def contract(name: str) -> ToolContract:
    """Return the vendored contract for ``name`` or raise if it is unregistered.

    An unregistered tool name is a Lens bug: a call site invented a tool, or a
    real tool was added to the client without vendoring its contract here.
    """
    try:
        return CONTRACTS[name]
    except KeyError:
        raise LithosContractError(
            f"no vendored contract for tool {name!r}; add it to CONTRACTS"
        ) from None


def check_arguments(name: str, arguments: dict[str, Any]) -> None:
    """Validate ``arguments`` against the tool's vendored accepted/required sets.

    Guards both invented-argument escape classes, before the call, as a
    :class:`LithosContractError`:

    * an argument the tool does not accept (FastMCP rejects it upstream) — #26;
    * a *missing required* argument — the #23 class, where dropping a
      default-sensitive flag (``with_claims``) silently inverts Lens's default
      against upstream's. A subset that omits a required key is a violation, not
      a valid default.
    """
    tool = contract(name)
    keys = set(arguments)
    missing = sorted(tool.required - keys)
    if missing:
        raise LithosContractError(
            f"{name} requires argument(s) {missing} on every call "
            "(omitting a default-sensitive flag is the #23 escape)"
        )
    unexpected = sorted(keys - tool.arguments)
    if unexpected:
        raise LithosContractError(
            f"{name} does not accept argument(s) {unexpected}; "
            f"accepted: {sorted(tool.arguments)}"
        )


def envelope_rows(name: str, payload: dict[str, Any]) -> list[Any]:
    """Return the row list from ``payload`` using the tool's vendored envelope key.

    Guards the #30 class of escape: the container key is read from the vendored
    contract, never a literal a call site guessed. A payload missing the key (or
    holding a non-list there) yields ``[]``, matching the ``payload.get(key, [])``
    the call sites used before.
    """
    tool = contract(name)
    if not tool.rows_key:
        raise LithosContractError(
            f"{name} has no list envelope; read its payload directly"
        )
    rows = payload.get(tool.rows_key)
    return rows if isinstance(rows, list) else []
