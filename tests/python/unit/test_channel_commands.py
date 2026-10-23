# Unit tests for prefix command parsing and the `status` reply.
# The parser decides which word a member typed after the build-time prefix;
# the renderer turns already collected agent facts into a grouped fenced
# code block that is sent as plain message content. No network and no
# inventory: every fact is supplied by the test.

from astral_remote.agent.channel_commands import format_status, parse_command

FACTS = {
    "hostname": "DESKTOP-9RDAR4H",
    "installationId": "50707c24a441c8de",
    "agentVersion": "0.1.0",
    "uptimeSeconds": 8160,
    "connected": True,
    "latencySeconds": 0.042,
    "reconnects": 0,
    "lastUpdated": "2026-10-21 18:30:00",
}

ROW_LABELS = (
    "Status",
    "Hostname",
    "Installation ID",
    "Version",
    "Uptime",
    "Discord",
    "Gateway Latency",
    "Reconnect Count",
)


def test_parse_command_requires_an_exact_prefix():
    assert parse_command("!status", "!") == "status"
    assert parse_command("  !STATUS  ", "!") == "status"
    assert parse_command("!status extra args", "!") == "status"
    assert parse_command("pc!status", "pc!") == "status"
    assert parse_command("status", "!") is None
    assert parse_command("?status", "!") is None
    assert parse_command("!", "!") is None
    assert parse_command("", "!") is None
    assert parse_command(None, "!") is None
    assert parse_command("status", "") is None
    assert parse_command("!status", None) is None


def test_format_status_uses_the_grouped_code_block_layout():
    text = format_status(FACTS)
    assert text.startswith("```\n")
    assert text.endswith("\n```")
    lines = text.splitlines()
    assert lines[1] == "ASTRAL REMOTE — STATUS"
    assert lines[2] == "━" * 28
    assert "[ AGENT ]" in lines
    assert "[ CONNECTION ]" in lines
    assert "[ LAST UPDATED ]" in lines
    assert lines.index("[ AGENT ]") < lines.index("[ CONNECTION ]") < lines.index("[ LAST UPDATED ]")
    assert "Status" + " " * 10 + ": ONLINE" in text
    assert "Hostname" + " " * 8 + ": DESKTOP-9RDAR4H" in text
    assert "Installation ID : 50707c24a441c8de" in text
    assert "Version" + " " * 9 + ": 0.1.0" in text
    assert "Uptime" + " " * 10 + ": 2h 16m" in text
    assert "Discord" + " " * 9 + ": Connected" in text
    assert "Gateway Latency : 42 ms" in text
    assert "Reconnect Count : 0" in text
    assert "2026-10-21 18:30:00" in text


def test_format_status_aligns_the_value_column():
    text = format_status(FACTS)
    rows = [line for line in text.splitlines() if line.startswith(ROW_LABELS)]
    assert len(rows) == len(ROW_LABELS)
    assert all(line.index(":") == 16 for line in rows)


def test_format_status_compacts_uptime():
    def uptime(seconds):
        return next(line for line in format_status({"uptimeSeconds": seconds}).splitlines() if line.startswith("Uptime"))

    assert uptime(59).endswith(": 0m")
    assert uptime(300).endswith(": 5m")
    assert uptime(8160).endswith(": 2h 16m")
    assert uptime(86400 + 7200).endswith(": 1d 2h 0m")
    assert uptime(None).endswith(": Unavailable")


def test_format_status_marks_missing_facts_unavailable():
    text = format_status({})
    assert "Hostname" + " " * 8 + ": Unavailable" in text
    assert "Installation ID : Unavailable" in text
    assert "Version" + " " * 9 + ": Unavailable" in text
    assert "Uptime" + " " * 10 + ": Unavailable" in text
    assert "Discord" + " " * 9 + ": Unavailable" in text
    assert "Gateway Latency : Unavailable" in text
    assert "Reconnect Count : Unavailable" in text
    assert "[ LAST UPDATED ]\nUnavailable" in text


def test_format_status_ignores_unusable_counters():
    text = format_status({"latencySeconds": float("nan"), "reconnects": "many"})
    assert "Gateway Latency : Unavailable" in text
    assert "Reconnect Count : Unavailable" in text
