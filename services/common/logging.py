"""Structured JSON logging (NFR-09).

Purpose:    make every log line a single JSON object (time, level, logger,
            message plus any `extra={...}` fields such as a session id).
Why needed: Prometheus/Grafana and any log search need machine-readable lines;
            plain text logs cannot be filtered by session or candidate.
Contains:
    RESERVED          - names every LogRecord already has, so only caller-added
                        `extra` fields are copied into the JSON.
    JsonFormatter     - turns one record into one JSON line (with traceback).
    configure_logging - installs the formatter on the root logger at startup.
Used by:    common/events, common/storage, main (called once at app start).
Depends on: nothing inside the project (stdlib only).
"""

import json  # serialise each log record as one JSON line
import logging  # stdlib logging framework we extend
import sys  # stdout is the log destination
from datetime import UTC, datetime  # timestamp each record in UTC

# Attribute names every LogRecord already has; anything else came from `extra={...}`.
RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON, carrying any `extra` fields through."""

    def format(self, record: logging.LogRecord) -> str:
        """Build the JSON payload for one record (time, level, logger, message, extras)."""
        # Core fields every line has, with the timestamp fixed to UTC.
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Pick up caller-supplied fields such as extra={"session_id": ...}.
        extras = {k: v for k, v in record.__dict__.items() if k not in RESERVED}
        if extras:
            payload.update(extras)
        # Include the traceback so errors stay on one line and stay searchable.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str keeps odd values (UUIDs, datetimes) from breaking logging.
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Route all logging to stdout as JSON at the given level (called once at startup)."""
    # One stdout handler with the JSON formatter replaces any default handlers.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
