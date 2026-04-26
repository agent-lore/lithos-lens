"""Structured logging setup for Lithos Lens."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from lithos_lens.config import LogLevel


class JsonFormatter(logging.Formatter):
    """Small JSON formatter that avoids a runtime dependency for M0."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(level: LogLevel) -> None:
    """Configure root logging to stdout with a structured JSON formatter."""

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
