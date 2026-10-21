# Unit tests for the Components V2 #info dashboard renderer and publisher.
# The renderer is pure, so these tests check the real packing arithmetic and
# the real discord.py LayoutView objects; the publisher runs against fake
# channels and a fake state store, with no network.

import asyncio
from types import SimpleNamespace

import pytest

from astral_remote.agent import info_report
from astral_remote.agent.info_report import Block, publish_report

BOT = SimpleNamespace(id=7, bot=True)
FOREIGN = SimpleNamespace(id=8, bot=False)


def sample(**sections):
    """Build a minimal inventory with the identity block every report needs."""
    inventory = {
        "identity": {
            "hostname": "DESKTOP-TEST",
            "installationId": "abc123",
            "agentStatus": "ONLINE",
            "agentVersion": "0.1.0",
            "lastUpdated": "2026-01-01 01:00:00",
        },
    }
    inventory.update(sections)
    return inventory


def big_inventory(items=400):
    """Build an inventory whose network adapter list spans several messages."""
    return sample(network={
        "internetConnectivity": "Reachable",
        "publicIpv4": "203.0.113.9",
        "adapters": [
            {
                "name": f"Adapter{index:04d}",
                "description": f"Contoso Adapter {index:04d}",
                "status": "Up",
                "virtual": "No",
                "ipv4": "192.168.1.20/24",
                "ipv6": "None",
                "subnet": "255.255.255.0",
                "gateway": "192.168.1.1",
                "dns": "192.168.1.1",
                "dhcpEnabled": "Yes",
                "dhcpServer": "192.168.1.1",
                "mac": "AA-BB-CC-DD-EE-FF",
                "linkSpeed": "1 Gbps",
                "received": "1.0 GB",
                "sent": "512.0 MB",
            }
            for index in range(items)
        ],
    })


def make_views(count):
    """Build `count` real LayoutViews for publisher tests."""
    return info_report.build_views([[Block(f"body {index}")] for index in range(count)])


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def groups_of(pages):
    """Return the message group of every packed page, in order."""
    return [page[0].group for page in pages]


def inventory_text(inventory):
    """Flatten every report block into one string for substring checks."""
    return "\n".join(block.text for block in info_report.build_blocks(inventory))


def view_text(view):
    """Flatten one LayoutView into its display text, for assertions."""
    parts = []
    for item in view.walk_children():
        content = getattr(item, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def high_usage_inventory():
    """An inventory whose CPU, memory, and system drive all run high."""
    return sample(
        cpu={"utilization": "92.0%"},
        memory={"usagePercent": "95.0%", "usedPhysical": "15.2 GB", "totalPhysical": "16.0 GB"},
        storage={
            "logical": [{
                "device": "C:", "type": "Local disk", "fixed": True,
                "usedPercent": "94.0%", "free": "27.9 GB", "total": "465.8 GB",
                "percentValue": 94.0,
            }],
            "physical": [],
        },
    )


def test_group_order_is_fixed_and_overview_comes_first():
    assert info_report.GROUPS == (
        "Overview",
        "Windows & Operating System",
        "Processor & Memory",
        "Graphics & Firmware",
        "Storage",
        "Network & Power",
        "Security & Updates",
        "Performance & Agent",
    )
    _, pages = info_report.build_report(sample())
    assert groups_of(pages) == list(info_report.GROUPS)


def test_overview_is_the_first_message_and_carries_headline_numbers():
    inventory = sample(
        identity={
            "hostname": "DESKTOP-TEST",
            "installationId": "abc123",
            "agentStatus": "ONLINE",
            "agentVersion": "0.1.0",
            "localIpv4": "192.168.1.20",
            "publicIpv4": "203.0.113.9",
            "osName": "Microsoft Windows 11 Pro",
            "osVersion": "10.0.22631",
            "lastUpdated": "2026-01-01 01:00:00",
        },
        cpu={"utilization": "12.5%"},
        memory={"usagePercent": "45.0%", "usedPhysical": "8.0 GB", "totalPhysical": "16.0 GB"},
        storage={
            "logical": [{
                "device": "C:", "type": "Local disk", "fixed": True,
                "usedPercent": "50.0%", "free": "232.9 GB", "total": "465.8 GB",
                "percentValue": 50.0,
            }],
            "physical": [
                {"model": "Contoso NVMe SSD", "health": "Healthy", "operational": "OK"},
            ],
        },
    )
    _, pages = info_report.build_report(inventory)
    assert pages[0][0].group == "Overview"
    text = "\n".join(block.text for block in pages[0])
    for line in (
        "**Agent status:** ONLINE",
        "**Hostname:** DESKTOP-TEST",
        "**Installation ID:** abc123",
        "**Local IPv4:** 192.168.1.20",
        "**Public IPv4:** 203.0.113.9",
        "**Operating system:** Microsoft Windows 11 Pro",
        "**OS version / build:** 10.0.22631",
        "**Agent version:** 0.1.0",
        "**Last updated:** 2026-01-01 01:00:00",
        "**CPU utilization:** 12.5%",
        "**Memory usage:** 45.0% (8.0 GB of 16.0 GB)",
        "- **C:** 50.0% used | 232.9 GB free of 465.8 GB",
        "- **Physical disk:** Contoso NVMe SSD | health Healthy | state OK",
    ):
        assert line in text
    # The Overview is a dashboard, not a duplicate of the Storage message, so
    # it shows usage, free space, and disk health only.
    assert "File system" not in text


def test_overview_groups_removable_volumes_instead_of_repeating_them():
    inventory = sample(storage={
        "logical": [
            {"device": "C:", "type": "Local disk", "fixed": True, "usedPercent": "50.0%",
             "free": "232.9 GB", "total": "465.8 GB", "percentValue": 50.0},
            {"device": "D:", "type": "Removable", "fixed": False, "usedPercent": "0.0%",
             "free": "0 B", "total": "0 B", "percentValue": 0.0},
            {"device": "E:", "type": "CD-ROM", "fixed": False, "usedPercent": "0.0%",
             "free": "0 B", "total": "0 B", "percentValue": 0.0},
        ],
    })
    text = inventory_text(inventory)
    assert "- **C:** 50.0% used | 232.9 GB free of 465.8 GB" in text
    assert "- **Other volumes:** D: (Removable), E: (CD-ROM)" in text
    _, pages = info_report.build_report(inventory)
    overview = "\n".join(block.text for block in pages[0])
    storage = "\n".join(
        block.text for page in pages if page[0].group == "Storage" for block in page
    )
    # The removable volumes are named once, in one grouped line, in each of the
    # two places that lists them.
    assert overview.count("**Other volumes:**") == 1
    assert storage.count("**Other volumes:**") == 1
    assert "E:" not in overview.replace("- **Other volumes:** D: (Removable), E: (CD-ROM)", "")


def test_high_usage_is_marked_in_words_and_with_the_accent_colour():
    inventory = high_usage_inventory()
    text = inventory_text(inventory)
    assert "**CPU utilization:** 92.0% - Critical" in text
    assert "**Memory usage:** 95.0% (15.2 GB of 16.0 GB) - Critical" in text
    assert "- **C:** 94.0% used | 27.9 GB free of 465.8 GB - Critical" in text
    _, pages = info_report.build_report(inventory)
    view = info_report.build_views([pages[0]])[0]
    container = view.to_components()[0]
    assert container["type"] == 17
    assert container["accent_color"] == info_report.LEVEL_COLOURS["critical"]


def test_a_quiet_report_uses_the_default_accent_colour():
    _, pages = info_report.build_report(sample())
    view = info_report.build_views([pages[0]])[0]
    assert view.to_components()[0]["accent_color"] == info_report.ACCENT_COLOUR


def test_first_section_of_each_group_opens_with_a_heading():
    _, pages = info_report.build_report(sample())
    for page in pages:
        assert page[0].heading is True
        assert page[0].text.startswith("### ")
        assert page[0].section is not None


def test_unknown_values_render_as_unavailable_not_placeholders():
    text = inventory_text(sample())
    # Missing inputs become the explicit marker; no sample values are invented.
    assert "**Graphics adapters:** Unavailable" in text
    assert "**Installed modules:** Unavailable" in text
    assert "**Pending updates:** Unavailable" in text
    assert "**Public IPv4:** Unavailable" in text
    assert "**CPU utilization:** Unavailable" in text
    assert "**Network adapters:** Unavailable" in text
    # A value the collector marked unsupported is passed through unchanged.
    support_text = inventory_text(sample(security={"pendingUpdates": "Not supported"}))
    assert "**Pending updates:** Not supported" in support_text


def test_gpu_monitoring_gaps_are_stated_once_per_adapter():
    inventory = sample(graphics=[{
        "name": "NVIDIA GeForce RTX 3070",
        "manufacturer": "NVIDIA",
        "driverVersion": "31.0.15.3699",
        "driverDate": "2024-01-01",
        "dedicatedMemory": "8.0 GB",
        "sharedMemory": info_report.NOT_SUPPORTED,
        "utilization": info_report.NOT_SUPPORTED,
        "temperature": info_report.NOT_SUPPORTED,
        "resolution": "2560 x 1440",
        "refreshRate": "144 Hz",
        "hdr": info_report.NOT_SUPPORTED,
        "status": "OK",
    }])
    text = inventory_text(inventory)
    assert "**GPU 1: NVIDIA GeForce RTX 3070**" in text
    assert ("**Monitoring:** Not supported "
            "(utilization, temperature, shared memory)") in text
    # One grouped line, not three separate Not supported fields.
    assert text.count("**Monitoring:**") == 1
    assert "**Utilization:**" not in text
    assert "**Shared memory:**" not in text


def test_os_directories_are_reported_once_in_the_os_group():
    inventory = sample(os={
        "osName": "Microsoft Windows 11 Pro",
        "windowsDirectory": "C:\\Windows",
        "systemDirectory": "C:\\Windows\\System32",
        "bootDevice": "\\Device\\HarddiskVolume3",
        "firmwareArchitecture": "x64-based PC",
    })
    os_blocks = [
        block for block in info_report.build_blocks(inventory)
        if block.group == "Windows & Operating System"
    ]
    os_text = "\n".join(block.text for block in os_blocks)
    assert "**Windows directory:** C:\\Windows" in os_text
    assert "**System directory:** C:\\Windows\\System32" in os_text
    assert "**Boot device:** \\Device\\HarddiskVolume3" in os_text
    assert "**Firmware architecture:** x64-based PC" in os_text
    environment = [
        block for block in info_report.build_blocks(inventory)
        if block.group == "Performance & Agent" and block.section == "Agent Environment"
    ]
    environment_text = "\n".join(block.text for block in environment)
    assert "Windows directory" not in environment_text
    assert "System directory" not in environment_text
    assert "Boot device" not in environment_text


def test_inferred_firmware_mode_is_labelled_as_inferred():
    text = inventory_text(sample(board={"biosMode": "UEFI"}))
    assert "**Firmware mode:** UEFI (inferred)" in text


def test_performance_and_agent_share_the_last_message():
    inventory = sample(
        performance={"cpuUtilization": "12.5%"},
        agent={"version": "0.1.0"},
        environment={"environmentStatus": "Collected (values withheld)"},
    )
    _, pages = info_report.build_report(inventory)
    agent_page = [page for page in pages if page[0].group == "Performance & Agent"][0]
    sections = [block.section for block in agent_page if block.heading]
    assert sections == ["Performance", "Astral Remote Agent", "Agent Environment"]
    text = "\n".join(block.text for block in agent_page)
    assert "**CPU utilization:** 12.5%" in text
    assert "**Agent version:** 0.1.0" in text


def test_processes_services_and_installed_software_never_reach_the_report():
    inventory = sample(
        processes={"count": 321, "items": [{"name": "explorer", "pid": "100"}]},
        services={"total": 2, "running": 1, "stopped": 1,
                  "items": [{"name": "Spooler", "display": "Print Spooler",
                             "status": "Running", "startType": "Automatic"}]},
        software={"count": "42", "items": [{"name": "Secret App", "version": "9.9"}]},
        agent={"pid": "4321", "version": "0.1.0"},
    )
    text = inventory_text(inventory)
    for leaked in ("Secret App", "explorer", "pid", "Process ID", "Installed Software",
                   "Software inventory", "Running processes", "Application 0",
                   "Spooler", "Print Spooler", "Service list", "Total services",
                   "Windows Services"):
        assert leaked not in text
    # The agent section still reports its own version.
    assert "**Agent version:** 0.1.0" in text


def test_long_section_yields_one_heading_and_plain_continuation_bodies():
    blocks = info_report.build_blocks(big_inventory())
    adapter_blocks = [block for block in blocks if block.section == "Network Adapters"]
    assert len(adapter_blocks) > 1
    first, rest = adapter_blocks[0], adapter_blocks[1:]
    assert first.heading is True
    assert first.text.startswith("### Network Adapters\n")
    assert all(block.continuation for block in rest)
    # Later chunks carry no heading at all, so the title appears exactly once.
    assert all(not block.heading and not block.text.startswith("### ") for block in rest)


def test_splitting_preserves_every_item_exactly_once_and_in_order():
    inventory = big_inventory(items=400)
    blocks, pages = info_report.build_report(inventory)
    assert [block.text for page in pages for block in page] == [block.text for block in blocks]
    combined = "\n".join(block.text for block in blocks)
    for index in (0, 199, 399):
        header = f"**Adapter {index + 1}: Adapter{index:04d} - Up, physical**"
        assert combined.count(header) == 1
    assert combined.index("**Adapter 1: Adapter0000") < combined.index("**Adapter 400: Adapter0399")


def test_oversized_single_line_is_wrapped_without_losing_characters():
    giant = "y" * (info_report.SECTION_CHUNK * 2 + 5)
    topic = info_report.Topic("Windows & Operating System", "Huge", [giant])
    blocks = info_report._topic_blocks(topic)
    assert all(len(block.text) <= info_report.MAX_TEXT_LENGTH for block in blocks)
    rebuilt = blocks[0].text.removeprefix("### Huge\n") + "".join(block.text for block in blocks[1:])
    assert rebuilt == giant


def test_packed_pages_stay_inside_both_discord_limits():
    for items in (400, 1200, 3000):
        _, pages = info_report.build_report(big_inventory(items=items))
        assert len(pages) > 3
        views = info_report.build_views(pages)
        reports = info_report.validate_views(views)
        assert all(report["textOk"] for report in reports)
        assert all(report["componentsOk"] for report in reports)
        assert all(report["textLength"] <= info_report.MAX_TEXT_LENGTH for report in reports)
        assert all(report["components"] <= info_report.MAX_COMPONENTS for report in reports)
        assert all(len(page) > 0 for page in pages)


def test_every_group_starts_its_own_message():
    _, pages = info_report.build_report(big_inventory(items=400))
    groups = groups_of(pages)
    # A group is contiguous: no group ever resumes after a later group started.
    seen = []
    for group in groups:
        if not seen or seen[-1] != group:
            assert group not in seen, f"{group} resumed after another group"
            seen.append(group)
    assert seen == list(info_report.GROUPS)


def test_build_views_uses_only_components_v2_containers():
    _, pages = info_report.build_report(sample())
    views = info_report.build_views(pages)
    for view in views:
        components = view.to_components()
        assert len(components) == 1
        container = components[0]
        assert container["type"] == 17  # Container
        types = [child["type"] for child in container["components"]]
        assert types[0] == 10  # TextDisplay carrying the message title
        assert set(types) <= {10, 14}  # only TextDisplay and Separator
        assert all("content" not in child or child["type"] == 10
                   for child in container["components"])


def test_no_page_starts_with_a_separator():
    _, pages = info_report.build_report(big_inventory(items=400))
    assert all(page for page in pages)
    views = info_report.build_views(pages)
    for view in views:
        container = view.to_components()[0]
        assert container["components"][0]["type"] == 10


def test_every_message_carries_its_part_number_and_group():
    _, pages = info_report.build_report(big_inventory(items=1200))
    views = info_report.build_views(pages)
    assert len(views) > 2
    for index, view in enumerate(views, start=1):
        children = view.to_components()[0]["components"]
        assert children[0]["content"] == f"# {info_report.TITLE} (Part {index} of {len(views)})"
        assert children[1]["type"] == 10
        assert children[1]["content"] == f"## {pages[index - 1][0].group}"
    # A topic that spans several messages repeats its heading, but never marks
    # the continuation: the part number already carries the order.
    headings = [view.to_components()[0]["components"][1]["content"] for view in views]
    assert headings.count("## Network & Power") > 1
    assert all("(continued)" not in heading for heading in headings)
    for view in views:
        text = view_text(view)
        assert "(continued)" not in text
        # No heading is printed twice inside the same message.
        section_headings = [
            child["content"]
            for child in view.to_components()[0]["components"]
            if child["type"] == 10 and child["content"].startswith("### ")
        ]
        assert len(section_headings) == len(set(section_headings))


def test_validate_views_reports_an_oversized_part(monkeypatch):
    warnings = []
    monkeypatch.setattr(info_report, "warn", lambda message, detail=None: warnings.append(message))
    view = info_report.build_views([[Block("x" * (info_report.MAX_TEXT_LENGTH + 1))]])[0]
    reports = info_report.validate_views([view])
    assert reports[0]["textOk"] is False
    assert warnings and "exceeds Discord limits" in warnings[0]



# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


class FakeMessage:
    def __init__(self, message_id, author=BOT):
        self.id = message_id
        self.author = author
        self.views = []
        self.deleted = False

    async def edit(self, content=None, view=None, **kwargs):
        assert content is None, "a Components V2 message must not carry content"
        self.views.append(view)
        return self

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, messages=None, fail_at=None):
        self.guild = SimpleNamespace(me=BOT)
        self.messages = {message.id: message for message in (messages or [])}
        self.sent = []
        self.fail_at = fail_at
        self._next_id = 100

    async def send(self, content=None, view=None, **kwargs):
        assert content is None, "a Components V2 message must not carry content"
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise RuntimeError("HTTP 500")
        self._next_id += 1
        message = FakeMessage(self._next_id)
        message.views.append(view)
        self.messages[message.id] = message
        self.sent.append(message)
        return message

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise LookupError("unknown message")
        return self.messages[message_id]


class FakeStore:
    def __init__(self, ids=None, broken=False):
        self.ids = list(ids or [])
        self.broken = broken
        self.writes = []

    def info_messages(self, guild_id):
        if self.broken:
            raise RuntimeError("unreadable state")
        return list(self.ids)

    def remember_info_messages(self, guild_id, message_ids):
        self.ids = list(message_ids)
        self.writes.append(list(message_ids))


def test_publisher_refreshes_tracked_messages_in_place():
    tracked = [FakeMessage(1), FakeMessage(2)]
    channel = FakeChannel(tracked)
    store = FakeStore([1, 2])
    views = make_views(2)

    result = asyncio.run(publish_report(channel, views, store, 5))

    assert result == {"published": [1, 2], "failedAt": None, "deleted": [], "reused": 2}
    assert [message.views for message in tracked] == [[views[0]], [views[1]]]
    assert channel.sent == []
    assert store.ids == [1, 2]


def test_publisher_reposts_a_deleted_message_and_clears_the_rest():
    stale = [FakeMessage(1), FakeMessage(2)]
    channel = FakeChannel(stale)
    channel.messages.pop(1)  # operator deleted the first part
    store = FakeStore([1, 2])

    result = asyncio.run(publish_report(channel, make_views(2), store, 5))

    assert result["failedAt"] is None
    assert result["reused"] == 0
    assert result["deleted"] == [2]
    assert stale[1].deleted is True
    assert len(channel.sent) == 2
    assert store.ids == result["published"]


def test_publisher_removes_surplus_messages_when_the_report_shrinks():
    tracked = [FakeMessage(1), FakeMessage(2), FakeMessage(3)]
    channel = FakeChannel(tracked)
    store = FakeStore([1, 2, 3])

    result = asyncio.run(publish_report(channel, make_views(2), store, 5))

    assert result["deleted"] == [3]
    assert tracked[2].deleted is True
    assert store.ids == [1, 2]


def test_publisher_stops_at_a_failed_part_and_records_only_sent_parts(monkeypatch):
    warnings = []
    monkeypatch.setattr(info_report, "warn", lambda message, detail=None: warnings.append(message))
    channel = FakeChannel(fail_at=1)
    store = FakeStore([])

    result = asyncio.run(publish_report(channel, make_views(3), store, 5))

    assert result["failedAt"] == 1
    assert len(result["published"]) == 1
    assert store.ids == result["published"]
    assert len(channel.sent) == 1
    assert warnings and "failed to send" in warnings[0]


def test_publisher_never_edits_or_deletes_foreign_messages():
    foreign = FakeMessage(9, author=FOREIGN)
    channel = FakeChannel([foreign])
    store = FakeStore([9])

    result = asyncio.run(publish_report(channel, make_views(1), store, 5))

    assert foreign.views == []
    assert foreign.deleted is False
    assert result["deleted"] == []
    assert len(channel.sent) == 1


def test_publisher_publishes_fresh_when_tracking_is_unreadable():
    channel = FakeChannel()
    store = FakeStore([1], broken=True)

    result = asyncio.run(publish_report(channel, make_views(2), store, 5))

    assert result["reused"] == 0
    assert len(result["published"]) == 2
    assert len(channel.sent) == 2
    assert store.writes[-1] == result["published"]


def test_publisher_survives_a_channel_without_fetch_support():
    class SendOnlyChannel(FakeChannel):
        fetch_message = None

    channel = SendOnlyChannel()
    store = FakeStore([1, 2])

    result = asyncio.run(publish_report(channel, make_views(1), store, 5))

    assert result["reused"] == 0
    assert len(channel.sent) == 1
    assert store.ids == result["published"]


def test_report_is_never_an_embed():
    views = info_report.build_views(info_report.pack_blocks(info_report.build_blocks(big_inventory(20))))
    for view in views:
        components = view.to_components()
        assert len(components) == 1
        assert components[0]["type"] == 17
        assert all(child["type"] in (10, 14) for child in components[0]["components"])
        assert "embed" not in str(components).lower()


@pytest.mark.parametrize("items", [0, 1, 40, 400])
def test_packing_is_never_empty_for_a_valid_inventory(items):
    blocks, pages = info_report.build_report(big_inventory(items=items))
    assert blocks and pages
    assert sum(len(page) for page in pages) == len(blocks)
