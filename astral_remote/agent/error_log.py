# Sanitized file diagnostics for silent production runs.
# The windowed EXE shows no console, so fatal startup errors and
# operator-reviewable warnings (for example duplicate host categories)
# land here instead of a visible terminal. Only caller-supplied safe
# strings are written; values pass through the shared redactor. Writes
# are best-effort and never throw. Ported from src/agent/error-log.js.

from __future__ import annotations

import datetime
from pathlib import Path

from astral_remote.shared.logging_utils import redact

LOG_FILENAME = "agent-error.log"


def log_dir(env=None) -> Path | None:
    """Resolve the log directory: LocalAppData on Windows, temp fallback."""
    from astral_remote.shared.paths import state_dir

    return state_dir(env)


def log_line(level: str, message: str, env=None) -> None:
    """Append one timestamped sanitized line. Never throws, never writes secrets."""
    try:
        directory = log_dir(env)
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        safe = str(redact(str(message))).replace("\n", " ")
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(directory / LOG_FILENAME, "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] [{level}] {safe}\n")
    except OSError:
        pass


def summarize_log(env=None) -> dict:
    """Summarize the diagnostic log without returning any message text.

    Only counts and the newest timestamp are exposed, so the dashboard
    can report that diagnostics exist without repeating Windows error
    strings that may quote private local paths. Never throws.
    """
    summary = {"present": False, "entries": 0, "warnings": 0, "fatals": 0, "lastTimestamp": None}
    try:
        directory = log_dir(env)
        if directory is None:
            return summary
        path = directory / LOG_FILENAME
        if not path.is_file():
            return summary
        summary["present"] = True
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.rstrip("\n")
                if not text:
                    continue
                summary["entries"] += 1
                if "[WARN]" in text:
                    summary["warnings"] += 1
                elif "[FATAL]" in text:
                    summary["fatals"] += 1
                if text.startswith("[") and "]" in text:
                    summary["lastTimestamp"] = text[1:text.index("]")]
    except OSError:
        return summary
    return summary


def log_fatal(message: str, env=None) -> None:
    """Append a fatal line. Retained for the startup failure path."""
    log_line("FATAL", message, env)


def log_warn(message: str, env=None) -> None:
    """Append a warning line for operator review in silent production runs."""
    log_line("WARN", message, env)
