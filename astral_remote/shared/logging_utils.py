# Minimal logger with secret redaction for CLI and agent.
# Every logged value passes through redact() so a bot token can never
# leak via terminal, logs, or audit records. Ported from src/shared/logger.js.

from __future__ import annotations

import datetime
import re
import sys
from typing import Any

_TOKEN_KEY = re.compile(r"(token|secret|password|credential)", re.IGNORECASE)
_LONG_SECRET = re.compile(r"[A-Za-z0-9._\-]{30,}")
_TOKEN_SHAPE = re.compile(r"[\w\-]{10,}\.[\w\-]{4,}\.[\w\-]{10,}")


def redact(value: Any) -> Any:
    """Replace secret-looking values with [REDACTED]. Handles str, list, dict."""
    if isinstance(value, str):
        return _LONG_SECRET.sub("[REDACTED]", _TOKEN_SHAPE.sub("[REDACTED]", value))
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if _TOKEN_KEY.search(str(key)) else redact(val)) for key, val in value.items()}
    return value


def _line(level: str, message: str, detail: Any, stream=None) -> None:
    out = stream if stream is not None else sys.stdout
    if out is None:
        return
    try:
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        safe = redact(detail if detail is not None else "")
        extra = "" if detail is None else " " + str(_json_safe(safe))
        out.write(f"[{stamp}] [{level}] {redact(str(message))}{extra}\n")
    except (OSError, ValueError):
        pass


def _json_safe(value: Any) -> str:
    import json

    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return '"<unserializable>"'


def info(message: str, detail: Any = None) -> None:
    """Log an informational line. Detail is redacted first."""
    _line("INFO", message, detail)


def warn(message: str, detail: Any = None) -> None:
    """Log a warning line. Detail is redacted first."""
    _line("WARN", message, detail)


def error(message: str, detail: Any = None) -> None:
    """Log an error line. Detail is redacted first."""
    _line("ERROR", message, detail)
