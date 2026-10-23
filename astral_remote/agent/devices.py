# Device registry records, heartbeat records, and the `device` reply layout.
# Devices are stored as machine-readable fenced blocks in bot-owned Discord
# messages: one registration record per installation in the server-wide
# CONTROL #main, and one reusable heartbeat record per installation in the
# computer's own host #main. Every Agent can read the shared CONTROL
# messages, so no local file is treated as the server-wide registry and no
# central database is introduced. This module has no Discord dependency:
# discord_setup performs the I/O and passes plain dictionaries in, which
# keeps parsing, status classification, and message packing testable.
# Timestamps are UTC ISO-8601 strings. Connection state is always derived
# from the age of a successfully written heartbeat, never from the bot's
# gateway link or from a stored success flag.

from __future__ import annotations

import datetime
import json
import math
import re

REGISTRY_LANGUAGE = "astral-device"
HEARTBEAT_LANGUAGE = "astral-heartbeat"
RECORD_VERSION = 1

# One minute matches the existing #info refresh loop. A device counts as
# connected while three intervals still contain a heartbeat, then becomes
# disconnected. Missing or malformed timestamps stay Unknown.
HEARTBEAT_INTERVAL_SECONDS = 60
HEARTBEAT_TIMEOUT_SECONDS = 180
FUTURE_TOLERANCE_SECONDS = 60

TITLE = "ASTRAL REMOTE — DEVICES"
DETAIL_TITLE = "ASTRAL REMOTE — DEVICE DETAILS"
RULE = "━" * 32
LABEL_WIDTH = 17

# Discord's hard limit is 2000 characters. Keep enough room for the code
# fences and a continuation heading added to the second and later parts.
MAX_MESSAGE_LENGTH = 2000
MAX_CODE_LENGTH = 1900
MAX_CHUNK_BODY = 1500
DEFAULT_PREFIX = "!"

STATUS_CONNECTED = "Connected"
STATUS_DISCONNECTED = "Disconnected"
STATUS_UNKNOWN = "Unknown"
STATUS_ORDER = (STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_UNKNOWN)
STATUS_GROUPS = {
    STATUS_CONNECTED: "CONNECTED",
    STATUS_DISCONNECTED: "DISCONNECTED",
    STATUS_UNKNOWN: "UNKNOWN",
}

_RECORD_PATTERN = re.compile(
    r"```" + REGISTRY_LANGUAGE + r"\s*\n(\{.*?\})\s*\n```",
    re.DOTALL,
)
_HEARTBEAT_PATTERN = re.compile(
    r"```" + HEARTBEAT_LANGUAGE + r"\s*\n(\{.*?\})\s*\n```",
    re.DOTALL,
)


def utc_now() -> datetime.datetime:
    """Return the current UTC time with second precision."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def format_timestamp(value) -> str | None:
    """Render a datetime as the UTC timestamp shared between Agent processes."""
    if not isinstance(value, datetime.datetime):
        return None
    moment = value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value) -> datetime.datetime | None:
    """Parse a registry timestamp, tolerating `Z`, offsets, and naive values."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    try:
        return moment.astimezone(datetime.timezone.utc)
    except (OverflowError, ValueError):
        return None


def _record_time(record: dict) -> datetime.datetime | None:
    """Return the freshest timestamp in a record for duplicate selection."""
    for key in ("updatedAt", "lastHeartbeat", "registeredAt"):
        moment = parse_timestamp(record.get(key))
        if moment is not None:
            return moment
    return None


def _clean(value, limit: int = 200) -> str:
    """Make a registry value safe for one code-block line."""
    text = "" if value is None else str(value)
    text = " ".join(text.replace("`", "").split())
    return text[:limit] or "Unavailable"


def _number(value) -> float | None:
    """Return a finite non-negative number, or None for unusable values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def latency_milliseconds(seconds) -> int | None:
    """Convert Discord latency seconds to whole milliseconds, or None."""
    value = _number(seconds)
    if value is None:
        return None
    milliseconds = int(round(value * 1000))
    return milliseconds if 0 <= milliseconds <= 60000 else None


def connection_label(connected) -> str:
    """Render the gateway state stored during registration without guessing."""
    if connected is True:
        return STATUS_CONNECTED
    if connected is False:
        return STATUS_DISCONNECTED
    return STATUS_UNKNOWN


def is_valid_record(record) -> bool:
    """True when a decoded dictionary is a usable device registration."""
    if not isinstance(record, dict):
        return False
    install_id = record.get("installationId")
    return isinstance(install_id, str) and bool(install_id.strip())


def _decode(content, pattern) -> dict | None:
    """Decode the first machine-readable block of one message."""
    text = str(content or "")
    match = pattern.search(text)
    if match is None:
        return None
    try:
        record = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def encode_record(record: dict) -> str:
    """Encode one device registration as a Discord code block."""
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"```{REGISTRY_LANGUAGE}\n{payload}\n```"


def decode_record(content) -> dict | None:
    """Decode a device registration block, or None for ordinary messages."""
    record = _decode(content, _RECORD_PATTERN)
    return record if is_valid_record(record) else None


def encode_heartbeat(record: dict) -> str:
    """Encode one heartbeat as a reusable Discord code block."""
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"```{HEARTBEAT_LANGUAGE}\n{payload}\n```"


def decode_heartbeat(content) -> dict | None:
    """Decode a heartbeat block, or None for ordinary messages."""
    record = _decode(content, _HEARTBEAT_PATTERN)
    if not is_valid_record(record):
        return None
    return record


def classify_device(record: dict, now=None) -> str:
    """Classify one device from its last successful heartbeat.

    A heartbeat inside HEARTBEAT_TIMEOUT_SECONDS is Connected. An older one
    is Disconnected. A missing, malformed, or implausibly future timestamp
    is Unknown, because the registry does not contain enough evidence to
    claim the computer is alive.
    """
    if not is_valid_record(record):
        return STATUS_UNKNOWN
    last = parse_timestamp(record.get("lastHeartbeat"))
    if last is None:
        return STATUS_UNKNOWN
    moment = now if isinstance(now, datetime.datetime) else utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    age = (moment.astimezone(datetime.timezone.utc) - last).total_seconds()
    if age < -FUTURE_TOLERANCE_SECONDS:
        return STATUS_UNKNOWN
    if age <= HEARTBEAT_TIMEOUT_SECONDS:
        return STATUS_CONNECTED
    return STATUS_DISCONNECTED


def format_last_seen(record: dict) -> str:
    """Render the last successful heartbeat in UTC, or Unavailable."""
    moment = parse_timestamp(record.get("lastHeartbeat"))
    if moment is None:
        return "Unavailable"
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_latency(record: dict) -> str:
    """Render the recorded Discord latency, or Unavailable when unmeasured."""
    value = _number(record.get("latencyMs"))
    if value is None:
        return "Unavailable"
    return f"{int(round(value))} ms"


def dedupe_records(records) -> tuple[list[dict], int]:
    """Keep one record per installation, retaining the freshest.

    Duplicate messages can be left by a crash between sending and saving a
    message ID. The newest record wins, while its earliest registration
    time is preserved. Nothing is deleted. Returns the records and the
    number of duplicate entries that were collapsed.
    """
    chosen: dict[str, dict] = {}
    duplicates = 0
    for record in records or []:
        if not is_valid_record(record):
            continue
        install_id = record["installationId"].strip()
        previous = chosen.get(install_id)
        if previous is None:
            chosen[install_id] = dict(record)
            continue
        duplicates += 1
        previous_time = _record_time(previous) or datetime.datetime.min.replace(
            tzinfo=datetime.timezone.utc
        )
        record_time = _record_time(record) or datetime.datetime.min.replace(
            tzinfo=datetime.timezone.utc
        )
        newest = dict(record) if record_time >= previous_time else previous
        first = parse_timestamp(previous.get("registeredAt"))
        second = parse_timestamp(record.get("registeredAt"))
        candidates = [item for item in (first, second) if item is not None]
        if candidates:
            newest["registeredAt"] = format_timestamp(min(candidates))
        chosen[install_id] = newest
    return list(chosen.values()), duplicates


def _device_sort_key(record: dict, now) -> tuple:
    """Sort connected devices first, then by hostname and installation ID."""
    status = classify_device(record, now)
    try:
        rank = STATUS_ORDER.index(status)
    except ValueError:
        rank = len(STATUS_ORDER)
    hostname = _clean(record.get("hostname"), 100).casefold()
    install_id = _clean(record.get("installationId"), 100).casefold()
    return rank, hostname, install_id


def _row(label: str, value) -> str:
    """Render one aligned `Label : value` row."""
    return f"{label:<{LABEL_WIDTH}}: {_clean(value, 240)}"


def _device_item(index: int, record: dict, status: str) -> list[str]:
    """Render one device entry inside its status group."""
    lines = [f"{index:02d}. {_clean(record.get('hostname'), 100)}"]
    if status == STATUS_CONNECTED:
        lines.extend([
            _row("    ID", record.get("installationId")),
            _row("    OS", record.get("operatingSystem")),
            _row("    Version", record.get("agentVersion")),
            _row("    Latency", format_latency(record)),
        ])
    else:
        lines.append(_row("    ID", record.get("installationId")))
        lines.append(_row("    Last Seen", format_last_seen(record)))
    lines.append("")
    return lines


def _chunk_blocks(blocks: list[list[str]]) -> list[list[str]]:
    """Pack whole entries into code blocks without splitting a device."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        block_size = sum(len(line) + 1 for line in block)
        if current and size + block_size > MAX_CHUNK_BODY:
            chunks.append(current)
            current = []
            size = 0
        if not current and block_size > MAX_CHUNK_BODY:
            # A value is capped well below this limit, but keep the reply
            # valid even if a future field grows.
            for line in block:
                line_size = len(line) + 1
                if current and size + line_size > MAX_CHUNK_BODY:
                    chunks.append(current)
                    current = []
                    size = 0
                current.append(line)
                size += line_size
            continue
        current.extend(block)
        size += block_size
    if current:
        chunks.append(current)
    return chunks or [[]]


def _wrap(parts: list[list[str]]) -> list[str]:
    """Wrap packed chunks in fences and add a part heading after the first."""
    total = len(parts)
    messages: list[str] = []
    for index, lines in enumerate(parts):
        body = list(lines)
        if index:
            body = [f"{TITLE} (Part {index + 1} of {total})", RULE, ""] + body
        text = "```\n" + "\n".join(body).rstrip() + "\n```"
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[: MAX_MESSAGE_LENGTH - 4].rstrip() + "\n```"
        messages.append(text)
    return messages


def format_devices(records, prefix: str = DEFAULT_PREFIX, now=None) -> list[str]:
    """Render the registered-device list as one or more fenced code blocks.

    Only supplied records are shown. Connected devices come first, then
    disconnected, then unknown. The list is split at device boundaries so
    every message stays inside Discord's content limit.
    """
    moment = now if isinstance(now, datetime.datetime) else utc_now()
    usable = [record for record in (records or []) if is_valid_record(record)]
    if not usable:
        return _wrap([[
            TITLE,
            RULE,
            "",
            "No registered devices",
            "",
            RULE,
        ]])

    ordered = sorted(usable, key=lambda record: _device_sort_key(record, moment))
    counts = {status: 0 for status in STATUS_ORDER}
    for record in ordered:
        counts[classify_device(record, moment)] += 1

    blocks: list[list[str]] = [[
        TITLE,
        RULE,
        "",
        f"Total Devices : {len(ordered)}",
        f"Connected     : {counts[STATUS_CONNECTED]}",
        f"Disconnected  : {counts[STATUS_DISCONNECTED]}",
        f"Unknown       : {counts[STATUS_UNKNOWN]}",
        "",
    ]]
    index = 1
    for status in STATUS_ORDER:
        group = [record for record in ordered if classify_device(record, moment) == status]
        if not group:
            continue
        blocks.append([f"[ {STATUS_GROUPS[status]} ]", ""])
        for record in group:
            blocks.append(_device_item(index, record, status))
            index += 1

    blocks.append([
        RULE,
        f"Use {_clean(prefix, 20)}device <id> for device details.",
    ])
    return _wrap(_chunk_blocks(blocks))


def format_device_details(record: dict, now=None) -> str:
    """Render one device's detail card as a fenced code block."""
    status = classify_device(record, now)
    registered = "Yes" if is_valid_record(record) else "No"
    lines = [
        DETAIL_TITLE,
        RULE,
        "",
        _row("Hostname", record.get("hostname")),
        _row("Installation ID", record.get("installationId")),
        _row("Status", status),
        _row("Agent Version", record.get("agentVersion")),
        _row("Operating System", record.get("operatingSystem")),
        "",
        _row("Last Seen", format_last_seen(record)),
        _row("Discord Latency", format_latency(record)),
        _row("Category", record.get("categoryStatus")),
        _row("Registered", registered),
        _row("Registered At", format_last_seen({"lastHeartbeat": record.get("registeredAt")})),
        "",
        RULE,
    ]
    return "```\n" + "\n".join(lines) + "\n```"


def format_device_not_found() -> str:
    """Render the not-found reply without echoing the user's input."""
    return "```\n" + "\n".join([
        DETAIL_TITLE,
        RULE,
        "",
        "Device not found",
        "",
        RULE,
    ]) + "\n```"


def format_ambiguous(records, now=None) -> str:
    """Render an ambiguous-prefix reply with the matching full IDs."""
    moment = now if isinstance(now, datetime.datetime) else utc_now()
    ordered = sorted(
        [record for record in (records or []) if is_valid_record(record)],
        key=lambda record: _clean(record.get("installationId"), 100).casefold(),
    )
    lines = [
        DETAIL_TITLE,
        RULE,
        "",
        "Ambiguous installation ID prefix.",
        "Matches:",
    ]
    for record in ordered:
        lines.append(
            f"- {_clean(record.get('installationId'), 100)}"
            f" ({_clean(record.get('hostname'), 100)}, {classify_device(record, moment)})"
        )
    lines.extend(["", "Use the full Installation ID.", "", RULE])
    return "```\n" + "\n".join(lines) + "\n```"


def format_permission_denied() -> str:
    """Render a permission denial without exposing registry data."""
    return "```\n" + "\n".join([
        DETAIL_TITLE,
        RULE,
        "",
        "Permission denied.",
        "Manage Server is required to view registered devices.",
        "",
        RULE,
    ]) + "\n```"


def format_registry_unavailable() -> str:
    """Render a read failure without treating it as an empty registry."""
    return "```\n" + "\n".join([
        DETAIL_TITLE,
        RULE,
        "",
        "Device registry unavailable.",
        "",
        RULE,
    ]) + "\n```"


def select_device(records, query: str, now=None) -> tuple[str, list[dict]]:
    """Resolve an exact ID or unique prefix.

    Returns `(kind, matches)` where kind is `exact`, `prefix`, `ambiguous`,
    or `missing`. Exact matching always wins over prefix matching.
    """
    text = str(query or "").strip().lower()
    usable = [record for record in (records or []) if is_valid_record(record)]
    if not text:
        return "missing", []
    exact = [
        record for record in usable
        if str(record.get("installationId", "")).strip().lower() == text
    ]
    if exact:
        return "exact", exact[:1]
    matches = [
        record for record in usable
        if str(record.get("installationId", "")).strip().lower().startswith(text)
    ]
    if len(matches) == 1:
        return "prefix", matches
    if len(matches) > 1:
        return "ambiguous", matches
    return "missing", []


def code_block_fits(text: str) -> bool:
    """True when a rendered reply fits one Discord message."""
    return len(str(text or "")) <= MAX_MESSAGE_LENGTH
