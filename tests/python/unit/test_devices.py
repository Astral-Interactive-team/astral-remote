# Unit tests for the shared device registry helpers.
# Exercises record encoding/decoding, heartbeat-age status classification,
# duplicate collapsing, exact/prefix lookup, code-block layout, permission
# checks, and message splitting. No Discord connection or real registry is
# involved; every record is supplied by the test.

import datetime
from types import SimpleNamespace

from astral_remote.agent import devices
from astral_remote.agent.channel_commands import (
    device_query,
    has_manage_guild_permission,
    parse_argument,
    parse_command,
)

NOW = datetime.datetime(2026, 10, 23, 12, 0, 0, tzinfo=datetime.timezone.utc)


def record(install_id="a1b2c3d4e5f60708", hostname="DESKTOP-A", heartbeat="2026-10-23T11:59:00Z",
           **extra):
    data = {
        "version": 1,
        "installationId": install_id,
        "hostname": hostname,
        "agentVersion": "0.1.0",
        "operatingSystem": "Windows 11 Pro",
        "registeredAt": "2026-10-20T09:00:00Z",
        "lastHeartbeat": heartbeat,
        "latencyMs": 42,
        "categoryStatus": "Reused remembered category (id 12)",
        "updatedAt": heartbeat,
    }
    data.update(extra)
    return data


def test_record_round_trips_and_ignores_ordinary_messages():
    item = record()
    text = devices.encode_record(item)
    assert text.startswith("```astral-device\n")
    assert text.endswith("\n```")
    assert devices.decode_record(text) == item
    assert devices.decode_record("hello") is None
    assert devices.decode_record("```astral-device\nnot json\n```") is None
    assert devices.decode_record("```astral-device\n{}\n```") is None
    heartbeat = devices.encode_heartbeat({"installationId": "abc", "lastHeartbeat": None})
    assert devices.decode_heartbeat(heartbeat)["installationId"] == "abc"


def test_classify_uses_heartbeat_age_only():
    assert devices.classify_device(record(heartbeat="2026-10-23T11:59:00Z"), NOW) == "Connected"
    assert devices.classify_device(record(heartbeat="2026-10-23T11:57:00Z"), NOW) == "Connected"
    assert devices.classify_device(record(heartbeat="2026-10-23T11:56:59Z"), NOW) == "Disconnected"
    assert devices.classify_device(record(heartbeat=None), NOW) == "Unknown"
    assert devices.classify_device(record(heartbeat="not-a-time"), NOW) == "Unknown"
    assert devices.classify_device(record(heartbeat="2026-10-22T00:00:00Z"), NOW) == "Disconnected"
    # A timestamp far enough in the future is not evidence of a live link.
    assert devices.classify_device(record(heartbeat="2026-10-23T12:05:00Z"), NOW) == "Unknown"


def test_latency_and_timestamp_formatting_reject_unusable_values():
    assert devices.latency_milliseconds(0.042) == 42
    assert devices.latency_milliseconds(False) is None
    assert devices.latency_milliseconds(float("nan")) is None
    assert devices.latency_milliseconds(-1) is None
    assert devices.format_last_seen(record()) == "2026-10-23 11:59:00 UTC"
    assert devices.format_last_seen({"lastHeartbeat": None}) == "Unavailable"
    assert devices.format_latency({"latencyMs": None}) == "Unavailable"


def test_dedupe_keeps_the_freshest_record_and_earliest_registration():
    older = record(heartbeat="2026-10-23T10:00:00Z", registeredAt="2026-10-01T00:00:00Z")
    newer = record(heartbeat="2026-10-23T11:59:00Z", registeredAt="2026-10-02T00:00:00Z")
    other = record(install_id="ffffffffffffffff", hostname="OTHER")

    chosen, duplicates = devices.dedupe_records([older, newer, other])

    assert duplicates == 1
    assert len(chosen) == 2
    winner = next(item for item in chosen if item["installationId"] == older["installationId"])
    assert winner["lastHeartbeat"] == "2026-10-23T11:59:00Z"
    assert winner["registeredAt"] == "2026-10-01T00:00:00Z"


def test_list_layout_orders_statuses_and_uses_code_blocks():
    items = [
        record(install_id="cccccccccccccccc", hostname="PC-C",
               heartbeat="2026-10-22T00:00:00Z"),
        record(install_id="bbbbbbbbbbbbbbbb", hostname="PC-B", heartbeat=None),
        record(install_id="aaaaaaaaaaaaaaaa", hostname="PC-A"),
        record(install_id="dddddddddddddddd", hostname="PC-D"),
    ]
    replies = devices.format_devices(items, "pc!", now=NOW)

    assert len(replies) == 1
    text = replies[0]
    assert text.startswith("```\n") and text.endswith("\n```")
    assert "ASTRAL REMOTE — DEVICES" in text
    assert "Total Devices : 4" in text
    assert "Connected     : 2" in text
    assert "Disconnected  : 1" in text
    assert "Unknown       : 1" in text
    assert text.index("[ CONNECTED ]") < text.index("[ DISCONNECTED ]") < text.index("[ UNKNOWN ]")
    assert text.index("PC-A") < text.index("PC-D") < text.index("PC-C") < text.index("PC-B")
    assert "Use pc!device <id> for device details." in text
    assert all(item["installationId"] in text for item in items)


def test_empty_registry_is_explicit():
    text = devices.format_devices([], now=NOW)[0]
    assert "No registered devices" in text
    assert text.startswith("```\n") and text.endswith("\n```")


def test_details_and_lookup_prefer_exact_ids_then_unique_prefixes():
    first = record(install_id="abc1111111111111", hostname="PC-A")
    second = record(install_id="abc2222222222222", hostname="PC-B")
    third = record(install_id="def3333333333333", hostname="PC-C")
    records = [first, second, third]

    kind, matches = devices.select_device(records, "abc1111111111111")
    assert kind == "exact" and matches == [first]
    kind, matches = devices.select_device(records, "def3")
    assert kind == "prefix" and matches == [third]
    kind, matches = devices.select_device(records, "abc")
    assert kind == "ambiguous" and len(matches) == 2
    assert devices.select_device(records, "missing") == ("missing", [])
    assert devices.select_device(records, "") == ("missing", [])

    text = devices.format_device_details(first, now=NOW)
    assert text.startswith("```\n") and text.endswith("\n```")
    assert "ASTRAL REMOTE — DEVICE DETAILS" in text
    assert "Hostname" + " " * 9 + ": PC-A" in text
    assert "Status" + " " * 11 + ": Connected" in text
    assert "Category" + " " * 9 + ": Reused remembered category" in text
    assert "Registered" + " " * 7 + ": Yes" in text
    assert "MAC" not in text and "Public IP" not in text

    ambiguous = devices.format_ambiguous(matches, now=NOW)
    assert "Ambiguous installation ID prefix." in ambiguous
    assert "abc1111111111111" in ambiguous and "abc2222222222222" in ambiguous
    assert "Device not found" in devices.format_device_not_found()
    assert "Device registry unavailable." in devices.format_registry_unavailable()
    assert "Manage Server is required" in devices.format_permission_denied()


def test_large_registry_is_split_at_device_boundaries():
    items = [
        record(install_id=f"{index:016x}", hostname=f"DESKTOP-{index:03d}")
        for index in range(200)
    ]
    replies = devices.format_devices(items, now=NOW)
    assert len(replies) > 1
    assert all(len(reply) <= devices.MAX_MESSAGE_LENGTH for reply in replies)
    assert all(reply.startswith("```\n") and reply.endswith("\n```") for reply in replies)
    combined = "\n".join(replies)
    assert all(item["installationId"] in combined for item in items)
    assert "(Part 2 of " in combined


def test_device_query_parsing_and_permission_policy():
    assert parse_command("!device abc", "!") == "device"
    assert parse_argument("!device abc extra", "!") == "abc extra"
    assert device_query("abc extra") == "abc"
    assert device_query(None) == ""

    denied = SimpleNamespace(author=SimpleNamespace(id=1, guild_permissions=None), guild=None)
    assert has_manage_guild_permission(denied) is False
    member = SimpleNamespace(
        author=SimpleNamespace(id=2, guild_permissions=SimpleNamespace(
            manage_guild=False, administrator=False)),
        guild=SimpleNamespace(owner_id=1),
    )
    assert has_manage_guild_permission(member) is False
    member.author.guild_permissions.manage_guild = True
    assert has_manage_guild_permission(member) is True
    owner = SimpleNamespace(
        author=SimpleNamespace(id=1, guild_permissions=None),
        guild=SimpleNamespace(owner_id=1),
    )
    assert has_manage_guild_permission(owner) is True
    assert has_manage_guild_permission(SimpleNamespace()) is False
