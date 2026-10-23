# Prefix command parsing and the read-only `status` reply.
# Managed host #main channels accept `status`; the answer is scoped to the
# computer whose managed category owns that channel, so a guild with
# several managed categories gets a different status from each #main.
# The server-wide CONTROL #main accepts the administrator-only `device`
# command, whose read-only reply is built by devices.py from shared Discord
# registry messages. General command replies are plain channel messages
# whose content is one fenced code block, so the labels and values line up
# in Discord's monospace rendering with no Components V2 wrapper. Replies
# are built from already collected facts: they change no system state and
# carry no token or other secret. #info accepts no commands.

from __future__ import annotations

from astral_remote.agent.windows.inventory import UNAVAILABLE

TITLE = "ASTRAL REMOTE — STATUS"
RULE = "━" * 28

# Widest label is "Installation ID" (15), so the colon sits at column 17.
LABEL_WIDTH = 16


def parse_invocation(content, prefix) -> tuple[str | None, str]:
    """Return the lowercased command word and its unparsed argument text.

    The prefix must match exactly; the command word itself is
    case-insensitive. Only the first whitespace-separated argument is
    meaningful to the `device` command, but the whole remainder is
    returned so callers can decide what to do with extra words.
    """
    text = str(content or "").strip()
    prefix = str(prefix or "")
    if not text or not prefix or not text.startswith(prefix):
        return None, ""
    rest = text[len(prefix):].strip()
    if not rest:
        return None, ""
    pieces = rest.split(None, 1)
    command = pieces[0].lower()
    argument = pieces[1].strip() if len(pieces) > 1 else ""
    return command, argument


def parse_command(content, prefix) -> str | None:
    """Return the lowercased command word after an exact prefix, or None.

    The prefix must match exactly; the command word itself is
    case-insensitive and any arguments after it are ignored.
    """
    command, _ = parse_invocation(content, prefix)
    return command


def parse_argument(content, prefix) -> str:
    """Return the argument text after the command word, or an empty string."""
    _, argument = parse_invocation(content, prefix)
    return argument


def device_query(argument) -> str:
    """Return the first argument accepted by `!device <id>`."""
    pieces = str(argument or "").split()
    return pieces[0] if pieces else ""


def has_manage_guild_permission(message) -> bool:
    """True when the member may use the administrator-only device command.

    Discord grants Manage Server to administrators, and the guild owner
    always has it. A DM or an author object without guild permissions is
    rejected rather than guessed as trusted.
    """
    author = getattr(message, "author", None)
    guild = getattr(message, "guild", None)
    author_id = getattr(author, "id", None)
    owner_id = getattr(guild, "owner_id", None)
    if author_id is not None and owner_id is not None and author_id == owner_id:
        return True
    permissions = getattr(author, "guild_permissions", None)
    if permissions is None:
        return False
    return bool(
        getattr(permissions, "manage_guild", False)
        or getattr(permissions, "administrator", False)
    )


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
