"""The audit record of one curated write attempt (§5C.6).

Exactly one structured log line, one span's attributes and one counter
increment per attempt, refusals included — whatever the attempt was and however
it ended. The write funnel opens the span, fills a :class:`WriteLedger` as it
learns each field, and hands both to :func:`record_attempt` in a ``finally``.
An action may fill the fields only it knows (a note write's version); nothing
else writes the record.

Split from :mod:`lithos_lens.write_funnel` on its own seam: WHAT is recorded
about an attempt is one definition, independent of the steps that decide the
attempt's outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from opentelemetry.trace import Span

from lithos_lens import metrics
from lithos_lens.write_errors import WriteAction

# The funnel's logger, by name: the audit line is the funnel's record, and its
# logger name is what an operator's log filter already keys on.
logger = logging.getLogger("lithos_lens.write_funnel")

__all__ = ["AUDIT_EVENT", "WriteLedger", "WriteResult", "record_attempt"]

#: The ``lens_event`` marker on the one structured audit line per attempt. The
#: warnings the Origin check and the operator registry log beside it are
#: diagnostics, not audit lines, and carry no such marker.
AUDIT_EVENT = "lens.writes.audit"

#: How each attempt ended, as the span and the counter spell it (§5C.6).
WriteResult = Literal[
    "ok", "conflict", "rejected", "unknown", "refused_origin", "no_operator"
]


@dataclass
class WriteLedger:
    """One attempt's audit fields, filled in as the funnel learns them.

    ``task_id`` is the subject's id — a task's, or for a note write the
    note's. ``expected_status`` / ``observed_status`` are what the operator
    saw and what the pre-check read; a note write records the version it read
    as observed, and expects none.
    """

    action: WriteAction
    task_id: str
    expected_status: str
    arguments: Mapping[str, Any]
    result: WriteResult = "unknown"
    code: str = ""
    operator: str = ""
    observed_status: str = ""
    envelope: Mapping[str, Any] = field(default_factory=dict)
    attributes: dict[str, str | bool] = field(default_factory=dict)


def record_attempt(span: Span, ledger: WriteLedger) -> None:
    """One attempt's span attributes, counter increment and audit line.

    The operator, the task and a refusal's code are span attributes and audit
    fields, never metric labels (``metrics.writes``).
    """
    span.set_attribute("lens.write.action", ledger.action)
    span.set_attribute("lens.write.result", ledger.result)
    span.set_attribute("lens.write.task_id", ledger.task_id)
    span.set_attribute("lens.write.operator", ledger.operator)
    if ledger.code:
        span.set_attribute("lens.write.code", ledger.code)
    for key, value in ledger.attributes.items():
        span.set_attribute(f"lens.write.{key}", value)
    metrics.writes().add(1, {"action": ledger.action, "result": ledger.result})
    logger.info(
        "write attempt",
        extra={
            "lens_event": AUDIT_EVENT,
            "operator": ledger.operator,
            "action": ledger.action,
            "task_id": ledger.task_id,
            "arguments": dict(ledger.arguments),
            "expected_status": ledger.expected_status,
            "observed_status": ledger.observed_status,
            "result": ledger.result,
            "code": ledger.code,
            "envelope": dict(ledger.envelope),
            **{f"write_{key}": value for key, value in ledger.attributes.items()},
        },
    )
