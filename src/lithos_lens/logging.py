"""Structured logging setup for Lithos Lens."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from lithos_lens.config import LogLevel

# Ceiling on any single logged string, applied centrally in the formatter.
#
# Request-shaped data reaches the log from more places than Lens itself writes:
# ``uvicorn.access`` logs the whole request line, query string included, and it
# propagates to the root handler ``configure_logging`` installs. A 47 KB query
# string therefore wrote a 47 KB log line at the SHIPPED default level, and no
# per-field fix in this package could have covered it.
#
# That matters because the container log is size-capped and rotated
# (docker/docker-compose.yml): unbounded lines turn into cheap eviction of the
# log history, and on a service with no authentication that history is the only
# forensic record there is. Truncating here bounds every logger — Lens's own,
# uvicorn's, and whatever is added next — rather than relying on each call site
# to remember.
#
# 2 KB leaves normal lines untouched: the longest Lens record is a few hundred
# bytes, and a real request line is well under this.
MAX_LOGGED_VALUE_CHARS = 2048


def _truncated(value: str) -> str:
    """``value`` bounded to ``MAX_LOGGED_VALUE_CHARS``, marked when cut.

    The marker carries the original length, so a truncated line still says how
    much was dropped — silently shortening evidence is its own problem.
    """
    if len(value) <= MAX_LOGGED_VALUE_CHARS:
        return value
    return f"{value[:MAX_LOGGED_VALUE_CHARS]}…[truncated, {len(value)} chars]"


_STANDARD_LOG_RECORD_FIELDS = set(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
)
_STANDARD_LOG_RECORD_FIELDS.update({"message", "asctime"})


class JsonFormatter(logging.Formatter):
    """Small JSON formatter that avoids a runtime dependency for M0."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": _truncated(record.getMessage()),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = _json_safe(value)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(level: LogLevel) -> None:
    """Configure root logging to stdout with a structured JSON formatter."""

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def _json_safe(value: Any) -> Any:
    """A log ``extra`` value, JSON-encodable and length-bounded.

    Strings are truncated for the same reason messages are; containers are
    walked so a list or dict of request-shaped values cannot smuggle the same
    volume past the ceiling one element at a time.
    """
    if isinstance(value, str):
        return _truncated(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value)
    except TypeError:
        return _truncated(str(value))
    return value
