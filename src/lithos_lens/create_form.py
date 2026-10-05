"""The create form, as a model: what was typed → a validated create request.

One form creates a task, an epic or a gate (T3 D10, REQUIREMENTS §5C.2
"Create"). This module is everything about that form that can be decided
without a browser or a server:

- :class:`CreateInput` — the form's fields exactly as typed, so a refusal
  re-renders them unchanged (the operator never retypes a form to fix one
  field). Built from the posted form, or from the ``?project=`` / ``?parent=``
  pre-fill.
- :func:`validate` — Lens's own checks before any call: a title, a known type,
  a lowercase project slug, and for a gate a type a PERSON may create and, for
  a timer, a parseable ``ready_at``. Lithos stays the authority: what passes
  here can still be refused upstream, and :func:`place_problem` puts that
  refusal on the input it names.
- :class:`CreateRequest` — the one ``lithos_task_create`` the form asks for:
  the project under BOTH conventions (``metadata.project`` and the
  ``<project_tag_key>:<slug>`` tag, §5B.1), the request id as
  ``metadata.lens_request_id``, and the gate's fields. Nothing else reaches
  the task's metadata — the form has no input for advisory keys.

The gate types a person creates are a POLICY subset of the five Lithos knows
(:data:`CREATABLE_GATE_TYPES`): a hand-made ``ci`` or ``pr`` gate has nothing
watching it, and loom creates its own ``pr`` gates with the PR's metadata.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC
from typing import Any, Protocol

from lithos_lens.task_writes import TaskCreateResult
from lithos_lens.tasks import parse_timestamp
from lithos_lens.write_errors import TaskRef, WriteProblem

__all__ = [
    "CREATABLE_GATE_TYPES",
    "CREATABLE_TASK_TYPES",
    "CREATE_FIELDS",
    "CreateClient",
    "CreateInput",
    "CreateRequest",
    "FieldError",
    "PROJECT_SLUG_RULE",
    "Validated",
    "is_request_id",
    "new_request_id",
    "place_problem",
    "split_lines",
    "validate",
]

#: The task types the form offers, in the order its select lists them.
CREATABLE_TASK_TYPES: tuple[str, ...] = ("task", "epic", "gate")

#: The gate types a person may create, in the order the form lists them (S4).
#: A subset of ``gates.KNOWN_GATE_TYPES`` by policy, not a copy of it.
CREATABLE_GATE_TYPES: tuple[str, ...] = ("human", "external_task", "timer")

GATE_TASK_TYPE = "gate"
TIMER_GATE_TYPE = "timer"

#: The inputs of the form, by the name each posts under. A refusal is placed
#: on one of these, or at form level.
CREATE_FIELDS: tuple[str, ...] = (
    "title",
    "task_type",
    "description",
    "project",
    "tags",
    "parent",
    "predecessors",
    "gate_type",
    "ready_at",
)

#: Lithos's parameter names (as its refusal messages spell them, D8) mapped to
#: the input that carries each.
_PARAMETER_FIELDS: Mapping[str, str] = {
    "title": "title",
    "parent_task_id": "parent",
    "depends_on": "predecessors",
    "metadata.gate_type": "gate_type",
    "metadata.ready_at": "ready_at",
}

#: A request id as the server mints it: ``uuid4().hex`` (D6).
_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")

MAX_PROJECT_SLUG_LENGTH = 63
_PROJECT_SLUG = re.compile(rf"^[a-z0-9][a-z0-9._-]{{0,{MAX_PROJECT_SLUG_LENGTH - 1}}}$")
PROJECT_SLUG_RULE = (
    "lowercase letters, digits, '-', '_' and '.', starting with a letter or "
    f"digit, at most {MAX_PROJECT_SLUG_LENGTH} characters"
)


def new_request_id() -> str:
    """A fresh request id for a form being rendered (D6)."""
    return uuid.uuid4().hex


def is_request_id(value: str) -> bool:
    """Whether ``value`` is a request id this Lens could have minted."""
    return bool(_REQUEST_ID.fullmatch(value))


def split_lines(value: str) -> tuple[str, ...]:
    """One entry per line, stripped, blanks and repeats dropped, order kept.

    Tags and predecessors are typed one per line rather than comma-separated,
    because a comma can be part of a tag (D11).
    """
    entries = (line.strip() for line in value.splitlines())
    return tuple(dict.fromkeys(entry for entry in entries if entry))


def _field(form: Mapping[str, Any], name: str) -> str:
    value = form.get(name)
    return "" if value is None else str(value)


@dataclass(frozen=True)
class CreateInput:
    """The form's fields as typed, and the request id it carries.

    Kept raw — multi-line fields as their text — so a re-render puts back
    exactly what the operator typed, whatever validation made of it.
    """

    title: str = ""
    task_type: str = "task"
    description: str = ""
    project: str = ""
    tags: str = ""
    parent: str = ""
    predecessors: str = ""
    gate_type: str = ""
    ready_at: str = ""
    request_id: str = ""

    @classmethod
    def from_form(cls, form: Mapping[str, Any]) -> CreateInput:
        """The posted form. Browsers post CRLF line breaks; they are LF here."""
        values = {
            name: _field(form, name).replace("\r\n", "\n") for name in CREATE_FIELDS
        }
        return cls(**values, request_id=_field(form, "request_id"))

    @classmethod
    def prefilled(cls, *, project: str = "", parent: str = "") -> CreateInput:
        """A blank form with the context it was opened from, and a new id."""
        return cls(
            project=project.strip(), parent=parent.strip(), request_id=new_request_id()
        )

    def restarted(self) -> CreateInput:
        """The same input under a NEW request id — Start again (D7)."""
        return replace(self, request_id=new_request_id())

    @property
    def project_slug(self) -> str:
        return self.project.strip()


@dataclass(frozen=True)
class FieldError:
    """A refusal placed on one input: the sentence, and any candidates.

    ``candidates`` are the tasks an ambiguous prefix matched, listed as text
    under the field (S3) — the operator corrects the field; nothing is chosen
    for them.
    """

    message: str
    candidates: tuple[TaskRef, ...] = ()


class CreateClient(Protocol):
    """The one Lithos call a create makes, structurally (F6).

    Writes may not import the Lithos client, so the request is typed against
    this; ``LithosClient`` and the fake both satisfy it.
    """

    async def task_create(
        self,
        *,
        title: str,
        agent: str,
        description: str = "",
        tags: tuple[str, ...] | list[str] = (),
        metadata: dict[str, Any] | None = None,
        task_type: str = "task",
        depends_on: tuple[str, ...] | list[str] = (),
        parent_task_id: str = "",
    ) -> TaskCreateResult: ...


@dataclass(frozen=True)
class CreateRequest:
    """One validated ``lithos_task_create``, ready to send as an operator."""

    title: str
    task_type: str
    description: str
    project: str
    tags: tuple[str, ...]
    metadata: Mapping[str, Any]
    depends_on: tuple[str, ...]
    parent_task_id: str
    request_id: str

    @property
    def gate_type(self) -> str:
        return str(self.metadata.get("gate_type") or "")

    def arguments(self) -> dict[str, Any]:
        """The audit line's argument summary: ids, types and lengths only.

        Never the title or the description — the free text a person typed
        reaches Lithos and nothing else (§5C.6).
        """
        summary: dict[str, Any] = {
            "task_type": self.task_type,
            "title_chars": len(self.title),
            "description_chars": len(self.description),
            "project": self.project,
            "tag_count": len(self.tags),
            "parent_task_id": self.parent_task_id,
            "depends_on": list(self.depends_on),
            "request_id": self.request_id,
        }
        if self.gate_type:
            summary["gate_type"] = self.gate_type
        return summary

    async def send(self, client: CreateClient, *, agent: str) -> TaskCreateResult:
        """The single call, attributed to the operator."""
        return await client.task_create(
            title=self.title,
            agent=agent,
            description=self.description,
            tags=self.tags,
            metadata=dict(self.metadata),
            task_type=self.task_type,
            depends_on=self.depends_on,
            parent_task_id=self.parent_task_id,
        )


@dataclass(frozen=True)
class Validated:
    """What :func:`validate` made of a form: a request, or the errors."""

    request: CreateRequest | None = None
    errors: Mapping[str, FieldError] = field(default_factory=dict)


def validate(typed: CreateInput, *, project_tag_key: str) -> Validated:
    """Lens's checks before any call (D10), and the request they allow.

    A non-gate drops the gate fields: the no-JS form shows the fieldset to
    every type and posts it whatever was chosen.
    """
    errors: dict[str, FieldError] = {}
    title = typed.title.strip()
    if not title:
        errors["title"] = FieldError("Give it a title.")
    task_type = typed.task_type.strip()
    if task_type not in CREATABLE_TASK_TYPES:
        errors["task_type"] = FieldError("Choose task, epic or gate.")
    project = typed.project_slug
    if project and not _PROJECT_SLUG.fullmatch(project):
        errors["project"] = FieldError(f"A project is a slug: {PROJECT_SLUG_RULE}.")

    metadata: dict[str, Any] = {}
    if task_type == GATE_TASK_TYPE:
        gate_type = typed.gate_type.strip()
        if gate_type not in CREATABLE_GATE_TYPES:
            errors["gate_type"] = FieldError(
                "Choose human, external task or timer. CI and PR gates are "
                "created by what watches them, not by hand."
            )
        else:
            metadata["gate_type"] = gate_type
        if gate_type == TIMER_GATE_TYPE:
            ready_at = (
                parse_timestamp(typed.ready_at) if typed.ready_at.strip() else None
            )
            if ready_at is None:
                errors["ready_at"] = FieldError(
                    "A timer gate needs the date and time it becomes ready (UTC)."
                )
            else:
                metadata["ready_at"] = ready_at.astimezone(UTC).isoformat()

    if errors:
        return Validated(errors=errors)

    tags = split_lines(typed.tags)
    if project:
        # §5B.1: both conventions, the tag under the configured key.
        metadata = {"project": project, **metadata}
        tags = tuple(dict.fromkeys((*tags, f"{project_tag_key}:{project}")))
    metadata["lens_request_id"] = typed.request_id
    return Validated(
        request=CreateRequest(
            title=title,
            task_type=task_type,
            description=typed.description,
            project=project,
            tags=tags,
            metadata=metadata,
            depends_on=split_lines(typed.predecessors),
            parent_task_id=typed.parent.strip(),
            request_id=typed.request_id,
        )
    )


def place_problem(
    typed: CreateInput, problem: WriteProblem
) -> tuple[WriteProblem, dict[str, FieldError]]:
    """Put an upstream refusal on the input it names (D8).

    Lithos names the parameter in its MESSAGE (``write_errors`` reads it into
    :attr:`WriteProblem.field`), keyed by Lithos's parameter names, which are
    mapped to this form's inputs here. An ambiguous prefix names no field: the
    typed values are matched against the candidate ids by prefix, and the
    candidates are listed under the field the matching value was typed in, as
    text (S3). The returned problem drops its candidates, so the form-level
    notice does not also offer them as links away from the form.
    """
    errors: dict[str, FieldError] = {}
    detail = problem.detail_text
    if problem.candidates:
        typed_ids = (
            ("parent", typed.parent.strip()),
            *(("predecessors", value) for value in split_lines(typed.predecessors)),
        )
        for name, value in typed_ids:
            if value and all(
                candidate.task_id.startswith(value) for candidate in problem.candidates
            ):
                errors[name] = FieldError(
                    f"'{value}' matches more than one task — type more of the "
                    "id, or the whole id:",
                    candidates=problem.candidates,
                )
                break
        return replace(problem, candidates=()), errors
    name = _PARAMETER_FIELDS.get(problem.field, "")
    if name:
        errors[name] = FieldError(detail or problem.headline)
    return problem, errors
