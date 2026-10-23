# Prefix command parsing and the read-only `status` reply.
# Managed #main channels accept `status`; the answer is scoped to the
# computer whose managed category owns that channel, so a guild with
# several managed categories gets a different status from each #main.
# General command replies are plain channel messages whose content is one
# fenced code block, so the labels and values line up in Discord's
# monospace rendering with no Components V2 wrapper. The reply is built
# from already collected agent facts: it changes no system state, echoes no
# user input, and carries no token or other secret. #info accepts no
# commands.

from __future__ import annotations

from astral_remote.agent.windows.inventory import UNAVAILABLE

TITLE = "ASTRAL REMOTE — STATUS"
RULE = "━" * 28

# Widest label is "Installation ID" (15), so the colon sits at column 17.
LABEL_WIDTH = 16


def parse_command(content, prefix) -> str | None:
    """Return the lowercased command word after an exact prefix, or None.

    The prefix must match exactly; the command word itself is
    case-insensitive and any arguments after it are ignored.
    """
    text = str(content or "").strip()
    prefix = str(prefix or "")
    if not text or not prefix or not text.startswith(prefix):
        return None
    rest = text[len(prefix):].strip()
    if not rest:
        return None
    return rest.split()[0].lower()


def _text(value) -> str:
    """Render a fact for the reply, or Unavailable when it is missing."""
    text = "" if value is None else str(value).strip()
    return text or UNAVAILABLE


def _row(label: str, value) -> str:
    """Render one aligned `Label : value` row."""
    return f"{label:<{LABEL_WIDTH}}: {value}"


def _uptime(seconds) -> str:
    """Render uptime as `2h 16m`, or Unavailable when unknown."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return UNAVAILABLE
    if total < 0:
        return UNAVAILABLE
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, _ = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _latency(seconds) -> str:
    """Render the gateway latency in milliseconds, or Unavailable."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return UNAVAILABLE
    if seconds != seconds or seconds < 0:
        return UNAVAILABLE
    return f"{seconds * 1000:.0f} ms"


def _count(value) -> str:
    """Render an integer counter, or Unavailable when unknown."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return UNAVAILABLE


def status_lines(facts: dict) -> list[str]:
    """Render the status facts as the grouped code-block lines."""
    connection = "Connected" if facts.get("connected") else UNAVAILABLE
    return [
        TITLE,
        RULE,
        "",
        "[ AGENT ]",
        _row("Status", "ONLINE"),
        _row("Hostname", _text(facts.get("hostname"))),
        _row("Installation ID", _text(facts.get("installationId"))),
        _row("Version", _text(facts.get("agentVersion"))),
        _row("Uptime", _uptime(facts.get("uptimeSeconds"))),
        "",
        "[ CONNECTION ]",
        _row("Discord", connection),
        _row("Gateway Latency", _latency(facts.get("latencySeconds"))),
        _row("Reconnect Count", _count(facts.get("reconnects"))),
        "",
        "[ LAST UPDATED ]",
        _text(facts.get("lastUpdated")),
        "",
        RULE,
    ]


def format_status(facts: dict) -> str:
    """Render the status reply as one fenced code block.

    Missing facts become Unavailable. Code-block rendering keeps the
    label column aligned and makes the reply read as command output. The
    result is sent as plain message content, not a Components V2 view.
    """
    return "```\n" + "\n".join(status_lines(facts)) + "\n```"
