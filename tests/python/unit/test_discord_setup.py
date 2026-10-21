# Unit tests for per-computer category setup. Fakes only, no network and
# no real inventory: the manager receives a stub collector, so the whole
# render/pack/publish path still runs for real.
# Covers stable identity reuse, one category per guild, idempotent
# on_ready/reconnect behavior, concurrency, failure handling, duplicate
# reporting without automatic deletion, and the managed #info dashboard
# (Components V2 report, in-place refresh, shrink cleanup).

import asyncio
from types import SimpleNamespace

import astral_remote.agent.discord_setup as setup
from astral_remote.agent.discord_setup import (
    INFO_CHANNEL_NAME,
    CategoryManager,
    ensure_host_categories,
    host_category_name,
    install_marker,
    legacy_host_category_name,
)

BOT_USER = SimpleNamespace(id=9001, bot=True)


class FakeMessage:
    def __init__(self, message_id, author=BOT_USER):
        self.id = message_id
        self.author = author
        self.content = ""
        self.views = []
        self.deleted = False

    async def edit(self, content=None, view=None, **kwargs):
        # A Components V2 message must not carry plain content.
        assert content is None, "report messages must never carry content"
        self.views.append(view)
        return self

    async def delete(self):
        self.deleted = True


class FakeTextChannel:
    def __init__(self, channel_id, name):
        self.id = channel_id
        self.name = name
        self.guild = SimpleNamespace(me=BOT_USER)
        self.messages = {}
        self.sent = []
        self._next_id = 5000

    async def send(self, content=None, view=None, **kwargs):
        assert content is None, "report messages must never carry content"
        self._next_id += 1
        message = FakeMessage(self._next_id)
        if view is not None:
            message.views.append(view)
        self.messages[message.id] = message
        self.sent.append(view)
        return message

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise LookupError("unknown message")
        return self.messages[message_id]


class FakeCategory:
    def __init__(self, category_id, name):
        self.id = category_id
        self.name = name
        self.edit_calls = []
        self.channels = []
        self._next_channel_id = 2000

    async def edit(self, name=None, **kwargs):
        self.edit_calls.append(name)
        if name is not None:
            self.name = name

    async def create_text_channel(self, name, reason=None):
        self._next_channel_id += 1
        channel = FakeTextChannel(self._next_channel_id, name)
        self.channels.append(channel)
        return channel

    async def delete(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("categories must never be deleted")


class FakeGuild:
    def __init__(self, guild_id=1, categories=None, fail=None):
        self.id = guild_id
        self.categories = list(categories or [])
        self.created = []
        self.attempts = 0
        self.fail = fail
        self._next_id = 1000

    async def create_category(self, name, reason=None):
        self.attempts += 1
        # Yield so concurrent callers would interleave without the lock.
        await asyncio.sleep(0)
        if self.fail is not None:
            raise self.fail
        self._next_id += 1
        category = FakeCategory(self._next_id, name)
        self.categories.append(category)
        self.created.append(name)
        return category


def stub_collector(full=False):
    """Build a collector stub shaped like the real inventory.

    `full` adds a long network adapter list so one inventory spans
    several report messages, letting tests exercise growth and shrink
    without running a real scan.
    """
    def collector(agent, env=None):
        inventory = {
            "identity": {
                "hostname": agent.get("hostname"),
                "installationId": agent.get("installationId"),
                "agentStatus": "ONLINE",
                "agentVersion": agent.get("agentVersion"),
                "lastUpdated": "2026-01-01 00:00:00",
            },
            "agent": {"version": agent.get("agentVersion"), "connection": "Connected"},
        }
        if full:
            inventory["network"] = {
                "internetConnectivity": "Reachable",
                "adapters": [
                    {
                        "name": f"Adapter{index:03d}",
                        "description": f"Contoso Adapter {index:03d}",
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
                    for index in range(400)
                ],
            }
        return inventory
    return collector


def view_text(view):
    """Flatten one LayoutView into its display text, for assertions."""
    parts = []
    for item in view.walk_children():
        content = getattr(item, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def make_manager(tmp_path, hostname="HOST", host_id="machineid", full=False):
    return CategoryManager(
        hostname, host_id, state_path=tmp_path / "identity.json",
        collect_fn=stub_collector(full),
    )


def _run(coro):
    return asyncio.run(coro)


def test_name_format_and_cap():
    assert host_category_name("DESKTOP-1", "abc123") == "DESKTOP-1 [abc123]"
    assert host_category_name("", "") == "unknown-host [unknown-id]"
    assert len(host_category_name("H" * 200, "abc123")) <= 100
    assert legacy_host_category_name("DESKTOP-1", "a1b2c3d4") == "pc DESKTOP-1 (a1b2c3d4)"


def test_first_startup_creates_one_category(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    category = _run(manager.ensure_guild(guild))
    expected = host_category_name("HOST", manager.installation_id)
    assert category.name == expected
    assert guild.created == [expected]
    assert guild.attempts == 1


def expected_parts(agent, full=False):
    """How many report messages one stubbed inventory should produce."""
    from astral_remote.agent.info_report import build_report

    _, pages = build_report(stub_collector(full)(agent))
    return len(pages)


def test_first_startup_creates_info_channel_with_system_report(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    agent = {"hostname": "HOST", "agentVersion": "9.9.9"}
    category = _run(manager.ensure_guild(guild, {"agentVersion": "9.9.9"}))
    assert [channel.name for channel in category.channels] == [INFO_CHANNEL_NAME]
    channel = category.channels[0]
    assert len(channel.sent) == expected_parts(agent)
    headline = view_text(channel.sent[0])
    assert "Astral Remote" in headline and "System Information" in headline
    assert "ONLINE" in headline
    assert "**Agent version:** 9.9.9" in headline
    assert "## Overview" in headline
    text = "\n".join(view_text(view) for view in channel.sent)
    assert "## Windows & Operating System" in text
    assert "## Performance & Agent" in text
    # Every part must stay inside the Discord message limits.
    assert all(view.content_length() <= 4000 for view in channel.sent)
    assert all(view.total_children_count <= 40 for view in channel.sent)
    assert manager.is_info_channel(channel.id) is True
    assert manager.is_info_channel(4242) is False


def test_info_channel_reused_and_report_refreshed_across_restarts(tmp_path):
    path = tmp_path / "identity.json"
    first = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    guild = FakeGuild()
    first_agent = {"hostname": "HOST", "agentVersion": "1.0.0"}
    category = _run(first.ensure_guild(guild, {"agentVersion": "1.0.0"}))
    channel = category.channels[0]
    parts = expected_parts(first_agent)
    assert len(channel.sent) == parts
    tracked_messages = list(channel.messages.values())
    assert len(tracked_messages) == parts

    restarted = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    second_guild = FakeGuild(categories=guild.categories)
    chosen = _run(restarted.ensure_guild(second_guild, {"agentVersion": "2.0.0"}))
    assert chosen is category
    assert second_guild.created == []
    assert len(category.channels) == 1  # no duplicate channel
    assert len(channel.sent) == parts  # no duplicate report message
    # Every part was refreshed in place, so each tracked message holds two views.
    assert all(len(message.views) == 2 for message in tracked_messages)
    assert "**Agent version:** 2.0.0" in view_text(tracked_messages[0].views[-1])
    assert restarted.is_info_channel(channel.id) is True


def test_report_is_split_and_rejoined_in_order(tmp_path):
    path = tmp_path / "identity.json"
    first = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector(full=True))
    guild = FakeGuild()
    agent = {"hostname": "HOST", "agentVersion": "1.0.0"}
    category = _run(first.ensure_guild(guild, {"agentVersion": "1.0.0"}))
    channel = category.channels[0]
    parts = expected_parts(agent, full=True)
    assert parts > 2  # a long report really does span several messages
    assert len(channel.sent) == parts
    combined = "\n".join(view_text(view) for view in channel.sent)
    # A topic that spans several messages repeats its heading on each one, and
    # is never marked as a continuation.
    from astral_remote.agent.info_report import build_report

    _, pages = build_report(stub_collector(full=True)(agent))
    network_pages = sum(1 for page in pages if page[0].group == "Network & Power")
    assert network_pages > 1
    assert combined.count("## Network & Power\n") == network_pages
    assert "(continued)" not in combined
    assert "### Network Adapters" in combined
    assert "Adapter000" in combined and "Adapter399" in combined
    # Every message names its part so the order stays readable.
    assert "(Part 1 of " in combined
    assert "(Part 2 of " in combined

    # A shorter report must remove the messages it no longer needs.
    restarted = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    _run(restarted.ensure_guild(FakeGuild(categories=guild.categories)))
    short_parts = expected_parts({"hostname": "HOST", "agentVersion": None})
    assert len(restarted._store.info_messages(guild.id)) == short_parts
    assert sum(1 for message in channel.messages.values() if message.deleted) == parts - short_parts


def test_existing_info_channel_is_adopted(tmp_path):
    manager = make_manager(tmp_path)
    category = FakeCategory(9, host_category_name("HOST", manager.installation_id))
    existing = FakeTextChannel(4444, INFO_CHANNEL_NAME)
    category.channels.append(existing)
    guild = FakeGuild(categories=[category])

    chosen = _run(manager.ensure_guild(guild))
    assert chosen is category
    assert category.channels == [existing]
    assert len(existing.sent) >= 1
    assert manager.is_info_channel(4444) is True


def test_deleted_info_message_is_reposted(tmp_path):
    path = tmp_path / "identity.json"
    first = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    guild = FakeGuild()
    category = _run(first.ensure_guild(guild))
    channel = category.channels[0]
    parts = len(channel.sent)
    channel.messages.clear()  # operator deleted the panel

    restarted = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    _run(restarted.ensure_guild(FakeGuild(categories=guild.categories)))
    assert len(channel.sent) == parts * 2


def test_info_channel_failure_still_returns_category(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)

    async def boom(guild, category):
        raise RuntimeError("Missing Permissions")

    monkeypatch.setattr(manager, "_ensure_info_channel", boom)
    guild = FakeGuild()
    category = _run(manager.ensure_guild(guild))
    assert category is not None
    assert guild.created == [host_category_name("HOST", manager.installation_id)]


def test_module_level_is_info_channel_matches_created_channel(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    monkeypatch.setattr(setup, "_default_manager", manager)
    guild = FakeGuild()
    _run(setup.default_manager("HOST", "machineid").ensure_guild(guild))
    channel = guild.categories[0].channels[0]
    assert setup.is_info_channel(channel.id) is True
    assert setup.is_info_channel(999999) is False


def test_repeated_startup_reuses_existing_category(tmp_path):
    path = tmp_path / "identity.json"
    first = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    guild = FakeGuild()
    _run(first.ensure_guild(guild))

    restarted = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    assert restarted.installation_id == first.installation_id
    second_guild = FakeGuild(categories=guild.categories)
    chosen = _run(restarted.ensure_guild(second_guild))
    assert chosen is guild.categories[0]
    assert second_guild.attempts == 0
    assert second_guild.created == []


def test_repeated_on_ready_events_do_not_duplicate(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    bot = SimpleNamespace(guilds=[guild])
    for _ in range(4):
        assert _run(ensure_host_categories(bot, "HOST", "machineid", manager=manager)) == 1
    assert guild.attempts == 1
    assert len(guild.created) == 1


def test_reconnection_reuses_remembered_category(tmp_path):
    path = tmp_path / "identity.json"
    first = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    original = FakeGuild(guild_id=7)
    category = _run(first.ensure_guild(original))

    # Reconnect: same installation, new process view of the same guild.
    reconnected = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    guild = FakeGuild(guild_id=7, categories=[category])
    chosen = _run(reconnected.ensure_guild(guild))
    assert chosen is category
    assert guild.attempts == 0


def test_concurrent_initialization_creates_at_most_one(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()

    async def burst():
        await asyncio.gather(*(manager.ensure_guild(guild) for _ in range(6)))

    _run(burst())
    assert guild.attempts == 1
    assert len(guild.created) == 1


def test_restart_preserves_installation_id(tmp_path):
    import json

    path = tmp_path / "identity.json"
    manager = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    first = manager.installation_id
    again = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    assert again.installation_id == first
    assert json.loads(path.read_text(encoding="utf-8"))["installationId"] == first


def test_hostname_change_reuses_category(tmp_path):
    path = tmp_path / "identity.json"
    first = CategoryManager("OLD-HOST", "machineid", state_path=path, collect_fn=stub_collector())
    original = FakeGuild()
    _run(first.ensure_guild(original))

    renamed_host = CategoryManager("NEW-HOST", "machineid", state_path=path, collect_fn=stub_collector())
    guild = FakeGuild(categories=original.categories)
    chosen = _run(renamed_host.ensure_guild(guild))
    assert chosen is original.categories[0]
    assert guild.attempts == 0
    # The managed category is renamed to the current display hostname.
    assert chosen.name == host_category_name("NEW-HOST", first.installation_id)


def test_permission_failure_does_not_loop(tmp_path):
    class Forbidden(Exception):
        pass

    manager = make_manager(tmp_path)
    guild = FakeGuild(fail=Forbidden("Missing Permissions"))
    assert _run(manager.ensure_guild(guild)) is None
    assert guild.attempts == 1
    # A later on_ready is skipped instead of hammering the API.
    assert _run(manager.ensure_guild(guild)) is None
    assert guild.attempts == 1
    assert guild.created == []


def test_api_failure_does_not_loop(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild(fail=RuntimeError("HTTP 500"))
    assert _run(manager.ensure_guild(guild)) is None
    assert guild.attempts == 1
    assert _run(manager.ensure_guild(guild)) is None
    assert guild.attempts == 1


def test_duplicates_reported_without_deletion(tmp_path, monkeypatch):
    reports = []
    monkeypatch.setattr(setup, "_report", lambda message, detail=None: reports.append((message, detail)))

    manager = make_manager(tmp_path)
    marker = install_marker(manager.installation_id)
    dup_one = FakeCategory(10, f"HOST {marker}")
    dup_two = FakeCategory(11, f"HOST {marker}")
    guild = FakeGuild(categories=[dup_one, dup_two])

    chosen = _run(manager.ensure_guild(guild))
    assert chosen in (dup_one, dup_two)
    assert guild.attempts == 0
    assert guild.created == []
    assert any("Multiple host categories" in message for message, _ in reports)


def test_legacy_category_adopted_without_creating(tmp_path):
    manager = make_manager(tmp_path, hostname="HOST", host_id="machineid")
    legacy = FakeCategory(5, legacy_host_category_name("HOST", "machineid"))
    guild = FakeGuild(categories=[legacy])

    chosen = _run(manager.ensure_guild(guild))
    assert chosen is legacy
    assert guild.attempts == 0
    assert legacy.name == host_category_name("HOST", manager.installation_id)


def test_ambiguous_legacy_categories_not_created(tmp_path, monkeypatch):
    reports = []
    monkeypatch.setattr(setup, "_report", lambda message, detail=None: reports.append(message))

    manager = make_manager(tmp_path, hostname="HOST", host_id="machineid")
    legacy_name = legacy_host_category_name("HOST", "machineid")
    guild = FakeGuild(categories=[FakeCategory(5, legacy_name), FakeCategory(6, legacy_name)])

    assert _run(manager.ensure_guild(guild)) is None
    assert guild.attempts == 0
    assert any("Ambiguous" in message for message in reports)


def test_report_writes_to_console_and_file(monkeypatch):
    from astral_remote.agent import error_log

    console = []
    files = []
    monkeypatch.setattr(setup, "warn", lambda message, detail=None: console.append(message))
    monkeypatch.setattr(error_log, "log_warn", lambda message, env=None: files.append(message))

    setup._report("Duplicate host categories.", {"guild": 1})
    assert console == ["Duplicate host categories."]
    assert any("Duplicate host categories." in line for line in files)


def test_covers_every_guild_and_counts(tmp_path):
    guilds = [FakeGuild(1), FakeGuild(2, fail=RuntimeError("gone"))]
    bot = SimpleNamespace(guilds=guilds)
    manager = make_manager(tmp_path)
    assert _run(ensure_host_categories(bot, "HOST", "machineid", manager=manager)) == 2
    assert guilds[0].attempts == 1
    assert guilds[1].attempts == 1


def test_missing_guilds_list_is_safe(tmp_path):
    manager = make_manager(tmp_path)
    assert _run(ensure_host_categories(SimpleNamespace(), "HOST", "machineid", manager=manager)) == 0
    assert _run(manager.ensure_guild(SimpleNamespace())) is None


def test_runtime_facts_ignore_unusable_latency():
    assert setup.runtime_facts(SimpleNamespace(latency=float("nan")))["latencySeconds"] is None
    assert setup.runtime_facts(SimpleNamespace(latency=float("inf")))["latencySeconds"] is None
    assert setup.runtime_facts(SimpleNamespace(latency=-1.0))["latencySeconds"] is None
    assert setup.runtime_facts(SimpleNamespace(latency="fast"))["latencySeconds"] is None
    assert setup.runtime_facts(SimpleNamespace())["latencySeconds"] is None
    assert setup.runtime_facts(SimpleNamespace(latency=0.05))["latencySeconds"] == 0.05


def test_runtime_facts_report_the_reconnect_count():
    facts = setup.runtime_facts(SimpleNamespace(latency=0.02, _astral_reconnects=3))
    assert facts == {"connected": True, "latencySeconds": 0.02, "reconnects": 3}
    assert setup.runtime_facts(SimpleNamespace())["reconnects"] is None
