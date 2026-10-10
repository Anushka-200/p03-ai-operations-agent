"""Structured JSON logs: one JSON object per line, phone numbers replaced by their keyed hash."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from app.privacy import redact_phones
from app.settings import get_settings

# Attributes every LogRecord has; anything else was passed via `extra=` and goes into the JSON.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        # Redact over the whole serialized body (extras, tracebacks), then add the timestamp,
        # which is never personal data and must not be touched by the phone pattern.
        body = redact_phones(json.dumps(entry, default=str))
        ts = datetime.fromtimestamp(record.created, UTC).isoformat()
        return '{"ts": "' + ts + '", ' + body[1:]


def setup_logging(stream: TextIO | None = None) -> None:
    """Log JSON lines to stdout. The MCP server passes stderr: its stdout is the protocol channel."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(get_settings().log_level)
