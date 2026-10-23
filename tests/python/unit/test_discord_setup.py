# Unit tests for per-computer category setup. Fakes only, no network and
# no real inventory: the manager receives a stub collector, so the whole
# render/pack/publish path still runs for real.
# Covers stable identity reuse, one category per guild, idempotent
# on_ready/reconnect behavior, concurrency, failure handling, duplicate
# reporting without automatic deletion, the managed #info dashboard
# (Components V2 report, in-place refresh, shrink cleanup), the managed
# #main command channel below it, the read-only `status` reply that is
# scoped to each #main channel's own category, and the server-wide control
# category created in parallel with host setup.

import asyncio
import datetime
from types import SimpleNamespace

import astral_remote.agent.discord_setup as setup
import astral_remote.agent.info_report as info_report
from astral_remote.agent import devices
from astral_remote.agent.discord_setup import (
    CONTROL_CATEGORY_NAME,
    INFO_CHANNEL_NAME,
    LOGS_CHANNEL_NAME,
    MAIN_CHANNEL_NAME,
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
        if view is not None:
            assert content is None, "report messages must never carry content"
            self.views.append(view)
        if content is not None:
            self.content = content
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
        self.contents = []
        self._next_id = 5000

    async def send(self, content=None, view=None, **kwargs):
        # A message carries either a report view or plain text, never both.
        assert content is None or view is None
        self._next_id += 1
        message = FakeMessage(self._next_id)
        if view is not None:
            message.views.append(view)
            self.sent.append(view)
        if content is not None:
            message.content = content
            self.contents.append(content)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise LookupError("unknown message")
        return self.messages[message_id]

    async def history(self, limit=None):
        messages = list(self.messages.values())
        if isinstance(limit, int) and limit >= 0:
            messages = messages[-limit:]
        for message in messages:
            yield message


class FakeCategory:
    def __init__(self, category_id, name):
        self.id = category_id
        self.name = name
        self.edit_calls = []
        self.channels = []
        # Channel IDs are unique per category, so two categories in one guild
        # never share a managed channel ID.
        self._next_channel_id = category_id * 10

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
                "connection": "Connected",
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
    # #main is created after #info, so it sits directly below it.
    assert [channel.name for channel in category.channels] == [INFO_CHANNEL_NAME, MAIN_CHANNEL_NAME]
    channel = category.channels[0]
    assert len(channel.sent) == expected_parts(agent)
    headline = view_text(channel.sent[0])
    assert "Astral Remote" in headline and "System Information" in headline
    assert f"{info_report.CONNECTION_ICONS['Connected']} **Connection: Connected**" in headline
    assert "**Agent Version:** `9.9.9`" in headline
    assert "## Overview" in headline
    text = "\n".join(view_text(view) for view in channel.sent)
    assert "## Windows & Operating System" in text
    assert "## Performance & Agent" in text
    # Every part must stay inside the Discord message limits.
    assert all(view.content_length() <= 4000 for view in channel.sent)
    assert all(view.total_children_count <= 40 for view in channel.sent)
    assert manager.is_info_channel(channel.id) is True
    assert manager.is_info_channel(4242) is False
    # #main is the operator command channel: no report is posted there and
    # its messages are never treated as #info chatter.
    main_channel = category.channels[1]
    assert main_channel.sent == []
    assert manager.is_info_channel(main_channel.id) is False
    assert manager._channel_counts[guild.id] == 2
    assert manager._store.main_channel_id(guild.id) == main_channel.id


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
    assert len(category.channels) == 2  # no duplicate channel
    assert len(channel.sent) == parts  # no duplicate report message
    # Every part was refreshed in place, so each tracked message holds two views.
    assert all(len(message.views) == 2 for message in tracked_messages)
    assert "**Agent Version:** `2.0.0`" in view_text(tracked_messages[0].views[-1])
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
    assert category.channels[0] is existing
    assert [channel.name for channel in category.channels] == [INFO_CHANNEL_NAME, MAIN_CHANNEL_NAME]
    assert len(existing.sent) >= 1
    assert manager.is_info_channel(4444) is True


def test_existing_main_channel_is_adopted(tmp_path):
    manager = make_manager(tmp_path)
    category = FakeCategory(9, host_category_name("HOST", manager.installation_id))
    existing_info = FakeTextChannel(4444, INFO_CHANNEL_NAME)
    existing_main = FakeTextChannel(5555, MAIN_CHANNEL_NAME)
    category.channels.extend([existing_info, existing_main])
    guild = FakeGuild(categories=[category])

    _run(manager.ensure_guild(guild))
    assert category.channels == [existing_info, existing_main]  # no duplicate
    assert manager._store.main_channel_id(guild.id) == 5555


def test_main_channel_is_recreated_when_deleted(tmp_path):
    path = tmp_path / "identity.json"
    first = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    guild = FakeGuild()
    category = _run(first.ensure_guild(guild))
    deleted = category.channels[1]
    assert deleted.name == MAIN_CHANNEL_NAME
    category.channels.remove(deleted)  # operator deleted #main

    restarted = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    _run(restarted.ensure_guild(FakeGuild(categories=guild.categories)))
    assert [channel.name for channel in category.channels] == [INFO_CHANNEL_NAME, MAIN_CHANNEL_NAME]
    assert category.channels[1] is not deleted
    assert restarted._store.main_channel_id(guild.id) == category.channels[1].id


def test_main_channel_failure_still_publishes_report(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)

    async def boom(guild, category):
        raise RuntimeError("Missing Permissions")

    monkeypatch.setattr(manager, "_ensure_main_channel", boom)
    guild = FakeGuild()
    category = _run(manager.ensure_guild(guild))
    assert [channel.name for channel in category.channels] == [INFO_CHANNEL_NAME]
    assert category.channels[0].sent  # the report still went out
    assert manager._channel_counts[guild.id] == 1


def test_control_category_is_created_with_only_main(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    control = _run(manager.ensure_control(guild))

    assert control is guild.categories[0]
    assert control.name == CONTROL_CATEGORY_NAME
    assert [channel.name for channel in control.channels] == [MAIN_CHANNEL_NAME]
    main_channel = control.channels[0]
    assert manager._store.control_category_id(guild.id) == control.id
    assert manager._store.control_main_channel_id(guild.id) == main_channel.id
    assert manager._store.control_logs_channel_id(guild.id) is None
    # CONTROL #main is the shared device command channel, but it is not
    # one of this computer's host #main channels.
    assert manager.is_main_channel(main_channel.id) is False
    assert manager.is_control_main_channel(main_channel.id) is True
    assert manager.commands_for(main_channel.id) == ("device",)
    assert manager.status_facts(main_channel.id) is None


def test_control_category_is_reused_across_restarts(tmp_path):
    path = tmp_path / "identity.json"
    first = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    guild = FakeGuild()
    category = _run(first.ensure_control(guild))

    restarted = CategoryManager("HOST", "machineid", state_path=path, collect_fn=stub_collector())
    reused = _run(restarted.ensure_control(FakeGuild(categories=guild.categories)))
    assert reused is category
    assert len(guild.categories) == 1
    assert [channel.name for channel in category.channels] == [MAIN_CHANNEL_NAME]


def test_existing_control_channels_are_adopted_without_deleting_logs(tmp_path):
    manager = make_manager(tmp_path)
    category = FakeCategory(77, CONTROL_CATEGORY_NAME)
    main_channel = FakeTextChannel(701, MAIN_CHANNEL_NAME)
    logs_channel = FakeTextChannel(702, LOGS_CHANNEL_NAME)
    category.channels.extend([main_channel, logs_channel])
    guild = FakeGuild(categories=[category])

    assert _run(manager.ensure_control(guild)) is category
    # The legacy #logs channel stays untouched; this build creates only #main.
    assert len(category.channels) == 2
    assert manager._store.control_main_channel_id(guild.id) == 701
    assert manager._store.control_logs_channel_id(guild.id) is None
    assert category.channels[1] is logs_channel


def test_control_category_is_created_once_by_startup(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    bot = SimpleNamespace(guilds=[guild])
    host_name = host_category_name("HOST", manager.installation_id)

    assert _run(ensure_host_categories(bot, "HOST", "machineid", manager=manager)) == 1
    assert [category.name for category in guild.categories] == [CONTROL_CATEGORY_NAME, host_name]
    assert guild.created == [CONTROL_CATEGORY_NAME, host_name]

    # Re-running startup (on_ready after a re-identify) reuses both categories.
    assert _run(ensure_host_categories(bot, "HOST", "machineid", manager=manager)) == 1
    assert len(guild.categories) == 2
    assert guild.created == [CONTROL_CATEGORY_NAME, host_name]


def test_control_failure_warns_and_keeps_host_setup(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    host = _run(manager.ensure_guild(guild))

    warnings = []
    monkeypatch.setattr(setup, "warn", lambda message, detail=None: warnings.append(message))

    async def boom(guild):
        raise RuntimeError("Missing Permissions")

    monkeypatch.setattr(manager, "_ensure_control_category", boom)
    assert _run(manager.ensure_control(guild)) is None
    assert warnings == ["Control category unavailable."]
    assert guild.categories == [host]


def test_control_setup_does_not_wait_for_the_host_lock(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()

    async def scenario():
        async with manager._lock(guild.id):
            return await asyncio.wait_for(manager.ensure_control(guild), timeout=1)

    assert _run(scenario()) is not None
    assert guild.categories[0].name == CONTROL_CATEGORY_NAME


def test_control_category_is_created_while_the_report_is_publishing(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    report_started = asyncio.Event()
    release_report = asyncio.Event()

    async def slow_publish(guild, channel, config, runtime, announce=True):
        report_started.set()
        await release_report.wait()

    monkeypatch.setattr(manager, "_publish_report", slow_publish)
    bot = SimpleNamespace(guilds=[guild])

    async def scenario():
        task = asyncio.create_task(
            ensure_host_categories(bot, "HOST", "machineid", manager=manager))
        try:
            await report_started.wait()
            for _ in range(100):
                if any(category.name == CONTROL_CATEGORY_NAME for category in guild.categories):
                    break
                await asyncio.sleep(0)
            # The report is still blocked, but control setup already finished.
            assert any(category.name == CONTROL_CATEGORY_NAME for category in guild.categories)
        finally:
            release_report.set()
            await task

    _run(scenario())


def test_refresh_republishes_in_place_without_new_channels(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    category = _run(manager.ensure_guild(guild, {"agentVersion": "1.0.0"}))
    info_channel = category.channels[0]
    parts = len(info_channel.sent)
    tracked = list(info_channel.messages.values())

    assert _run(manager.refresh(guild, {"agentVersion": "2.0.0"})) is True
    assert len(info_channel.sent) == parts  # edited, not re-sent
    assert len(category.channels) == 2  # no channel duplicated
    assert all(len(message.views) == 2 for message in tracked)
    assert "**Agent Version:** `2.0.0`" in view_text(tracked[0].views[-1])


def test_refresh_skips_guilds_that_never_resolved(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    assert _run(manager.refresh(guild)) is False
    assert guild.attempts == 0
    assert guild.categories == []


def test_refresh_does_not_log_every_pass(tmp_path, monkeypatch):
    lines = []
    monkeypatch.setattr(setup, "info", lambda message, detail=None: lines.append(message))
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    _run(manager.ensure_guild(guild))
    assert "Host info report published." in lines
    lines.clear()
    _run(manager.refresh(guild))
    assert lines == []  # a minute-by-minute refresh must not flood the log


def test_refresh_host_categories_updates_resolved_guilds(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    _run(manager.ensure_guild(guild))
    info_channel = guild.categories[0].channels[0]
    parts = len(info_channel.sent)
    tracked = list(info_channel.messages.values())
    bot = SimpleNamespace(guilds=[guild], latency=0.02, _astral_reconnects=1)

    assert _run(setup.refresh_host_categories(bot, config={}, manager=manager)) == 1
    assert len(info_channel.sent) == parts
    assert all(len(message.views) == 2 for message in tracked)


def test_refresh_host_categories_without_a_manager_is_safe(monkeypatch):
    monkeypatch.setattr(setup, "_default_manager", None)
    assert _run(setup.refresh_host_categories(SimpleNamespace(guilds=[]))) == 0


def test_refresh_loop_repeats_until_the_client_closes(monkeypatch):
    from astral_remote.agent import discord_bot

    calls = []
    checks = {"count": 0}

    class FakeClient:
        def is_closed(self):
            checks["count"] += 1
            return checks["count"] > 2

    async def fake_refresh(client, config=None):
        calls.append(config)
        return 1

    monkeypatch.setattr(setup, "refresh_host_categories", fake_refresh)
    monkeypatch.setattr(discord_bot, "REFRESH_INTERVAL_SECONDS", 0)
    _run(discord_bot._refresh_loop(FakeClient(), {"agentVersion": "1"}))
    assert calls == [{"agentVersion": "1"}, {"agentVersion": "1"}]


def test_start_refresh_reuses_the_running_task():
    from astral_remote.agent import discord_bot

    class RunningTask:
        def done(self):
            return False

    client = SimpleNamespace(_astral_refresh_task=RunningTask())
    discord_bot._start_refresh(client, {})
    assert isinstance(client._astral_refresh_task, RunningTask)


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
    # #main is still ensured even when #info cannot be created.
    assert [channel.name for channel in category.channels] == [MAIN_CHANNEL_NAME]
    assert manager._channel_counts[guild.id] == 1


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
    # One host category and one control category are created in total.
    assert guild.attempts == 2
    assert len(guild.created) == 2
    assert guild.created[0] == CONTROL_CATEGORY_NAME
    assert [channel.name for channel in guild.categories[1].channels] == [
        INFO_CHANNEL_NAME, MAIN_CHANNEL_NAME
    ]  # #info and #main, once each
    assert [channel.name for channel in guild.categories[0].channels] == [
        MAIN_CHANNEL_NAME
    ]


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
    # Each guild attempts the host category and then the control category;
    # the failing guild raises on both attempts.
    assert guilds[0].attempts == 2
    assert guilds[1].attempts == 2
    assert guilds[0].created[0] == CONTROL_CATEGORY_NAME


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
    assert facts == {"connected": None, "latencySeconds": 0.02, "reconnects": 3}
    assert setup.runtime_facts(SimpleNamespace())["reconnects"] is None


def test_runtime_facts_confirm_only_a_live_gateway_link():
    live = SimpleNamespace(
        ws=SimpleNamespace(socket=SimpleNamespace(closed=False)),
        is_ready=lambda: True,
        is_closed=lambda: False,
        latency=0.02,
    )
    assert setup.runtime_facts(live)["connected"] is True
    assert setup.runtime_facts(SimpleNamespace(
        ws=SimpleNamespace(socket=SimpleNamespace(closed=True)),
        is_ready=lambda: True,
        is_closed=lambda: False,
    ))["connected"] is False
    assert setup.runtime_facts(SimpleNamespace(
        is_ready=lambda: False,
        is_closed=lambda: True,
    ))["connected"] is False
    assert setup.runtime_facts(SimpleNamespace())["connected"] is None


def test_status_command_is_scoped_to_main_channels(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    category = _run(manager.ensure_guild(guild, {"agentVersion": "9.9.9"}))
    info_channel, main_channel = category.channels

    assert manager.commands_for(main_channel.id) == ("status",)
    assert manager.commands_for(info_channel.id) == ()
    assert manager.commands_for(4242) == ()
    assert manager.is_main_channel(main_channel.id) is True
    assert manager.is_main_channel(info_channel.id) is False

    facts = manager.status_facts(
        main_channel.id,
        {"agentVersion": "9.9.9"},
        {"connected": True, "latencySeconds": 0.042, "reconnects": 1},
    )
    assert facts["hostname"] == "HOST"
    assert facts["installationId"] == manager.installation_id
    assert facts["agentVersion"] == "9.9.9"
    assert facts["connected"] is True
    assert facts["latencySeconds"] == 0.042
    assert facts["reconnects"] == 1
    assert isinstance(facts["uptimeSeconds"], int)
    assert len(facts["lastUpdated"]) == 19
    assert manager.status_facts(info_channel.id) is None


def test_status_answer_differs_per_managed_category(tmp_path):
    guild = FakeGuild()
    alpha = CategoryManager(
        "PC-ALPHA", "id-a", state_path=tmp_path / "a.json", collect_fn=stub_collector()
    )
    beta = CategoryManager(
        "PC-BETA", "id-b", state_path=tmp_path / "b.json", collect_fn=stub_collector()
    )
    alpha_category = _run(alpha.ensure_guild(guild))
    beta_category = _run(beta.ensure_guild(guild))
    alpha_main = alpha_category.channels[1]
    beta_main = beta_category.channels[1]
    assert alpha_main.id != beta_main.id

    alpha_facts = alpha.status_facts(alpha_main.id, {"agentVersion": "1.0.0"})
    beta_facts = beta.status_facts(beta_main.id, {"agentVersion": "2.0.0"})
    assert alpha_facts["hostname"] == "PC-ALPHA"
    assert beta_facts["hostname"] == "PC-BETA"
    assert alpha_facts["installationId"] != beta_facts["installationId"]
    assert alpha_facts["agentVersion"] == "1.0.0"
    assert beta_facts["agentVersion"] == "2.0.0"
    # Each process answers only in its own category's #main.
    assert alpha.status_facts(beta_main.id) is None
    assert beta.status_facts(alpha_main.id) is None
    assert alpha.commands_for(beta_main.id) == ()


def test_module_level_command_helpers_follow_the_default_manager(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    category = _run(manager.ensure_guild(guild))
    info_channel, main_channel = category.channels
    monkeypatch.setattr(setup, "_default_manager", manager)

    assert setup.is_main_channel(main_channel.id) is True
    assert setup.is_main_channel(info_channel.id) is False
    assert setup.commands_for_channel(main_channel.id) == ("status",)
    assert setup.commands_for_channel(info_channel.id) == ()
    assert setup.status_for_channel(main_channel.id)["hostname"] == "HOST"
    assert setup.status_for_channel(info_channel.id) is None

    monkeypatch.setattr(setup, "_default_manager", None)
    assert setup.is_main_channel(main_channel.id) is False
    assert setup.commands_for_channel(main_channel.id) == ()
    assert setup.status_for_channel(main_channel.id) is None


def test_on_message_answers_status_only_in_managed_main(tmp_path, monkeypatch):
    from astral_remote.agent import discord_bot

    manager = make_manager(tmp_path)
    guild = FakeGuild()
    category = _run(manager.ensure_guild(guild, {"agentVersion": "9.9.9"}))
    info_channel, main_channel = category.channels
    monkeypatch.setattr(setup, "_default_manager", manager)
    handler = discord_bot.build_bot({"prefix": "!", "agentVersion": "9.9.9"}).on_message

    class MemberMessage:
        def __init__(self, channel, content, bot=False):
            self.author = SimpleNamespace(bot=bot)
            self.channel = channel
            self.content = content
            self.deleted = False

        async def delete(self):
            self.deleted = True

    # A command in #main answers with plain code-block content for this
    # category, not a Components V2 view.
    _run(handler(MemberMessage(main_channel, "!status")))
    assert main_channel.sent == []
    assert len(main_channel.contents) == 1
    card = main_channel.contents[0]
    assert card.startswith("```\n")
    assert "ASTRAL REMOTE — STATUS" in card
    assert "[ AGENT ]" in card
    assert "Hostname" + " " * 8 + ": HOST" in card

    # The same command in #info is deleted and never answered.
    report_count = len(info_channel.sent)
    spammed = MemberMessage(info_channel, "!status")
    _run(handler(spammed))
    assert spammed.deleted is True
    assert len(info_channel.sent) == report_count

    # Bang-less chatter, unknown commands, and the bot's own messages are
    # ignored in #main. Unmanaged channels are ignored even with a valid
    # command.
    _run(handler(MemberMessage(main_channel, "status")))
    _run(handler(MemberMessage(main_channel, "!open")))
    _run(handler(MemberMessage(main_channel, "!status", bot=True)))
    other = FakeTextChannel(4242, "other")
    _run(handler(MemberMessage(other, "!status")))
    assert other.sent == []
    assert other.contents == []
    assert len(main_channel.contents) == 1


def test_on_message_honours_the_configured_prefix(tmp_path, monkeypatch):
    from astral_remote.agent import discord_bot

    manager = make_manager(tmp_path)
    guild = FakeGuild()
    category = _run(manager.ensure_guild(guild))
    main_channel = category.channels[1]
    monkeypatch.setattr(setup, "_default_manager", manager)
    handler = discord_bot.build_bot({"prefix": "pc!", "agentVersion": "9.9.9"}).on_message

    class MemberMessage:
        def __init__(self, channel, content):
            self.author = SimpleNamespace(bot=False)
            self.channel = channel
            self.content = content

        async def delete(self):
            pass

    _run(handler(MemberMessage(main_channel, "!status")))
    assert main_channel.contents == []
    _run(handler(MemberMessage(main_channel, "pc!status")))
    assert len(main_channel.contents) == 1


class DeviceMemberMessage:
    """Member message used by the CONTROL #main device tests."""

    def __init__(self, channel, content, manage_guild=True, author_id=42, owner=False,
                 bot=False):
        self.author = SimpleNamespace(
            id=author_id,
            bot=bot,
            guild_permissions=SimpleNamespace(
                manage_guild=manage_guild,
                administrator=False,
            ),
        )
        self.channel = channel
        self.content = content
        self.guild = SimpleNamespace(owner_id=42 if owner else 7)
        self.deleted = False

    async def delete(self):
        self.deleted = True


def device_messages(channel):
    """Return decoded device records posted in a CONTROL channel."""
    found = []
    for message in channel.messages.values():
        record = devices.decode_record(message.content)
        if record is not None:
            found.append((message, record))
    return found


def heartbeat_messages(channel):
    """Return decoded heartbeat records posted in a host #main channel."""
    found = []
    for message in channel.messages.values():
        record = devices.decode_heartbeat(message.content)
        if record is not None:
            found.append((message, record))
    return found


def start_agents(tmp_path, guild, count=1):
    """Start one or more isolated Agent installations in the same guild."""
    managers = []
    bot = SimpleNamespace(guilds=[guild])
    for index in range(count):
        manager = CategoryManager(
            f"HOST-{index}", f"machineid-{index}",
            state_path=tmp_path / f"identity-{index}.json",
            collect_fn=stub_collector(),
        )
        assert _run(ensure_host_categories(
            bot, f"HOST-{index}", f"machineid-{index}", manager=manager,
            config={"agentVersion": "0.1.0"},
        )) == 1
        managers.append(manager)
    return managers


def test_startup_registers_the_device_and_writes_one_heartbeat(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    assert _run(ensure_host_categories(
        SimpleNamespace(guilds=[guild]), "HOST", "machineid", manager=manager,
        config={"agentVersion": "0.1.0"},
    )) == 1
    control = guild.categories[0]
    host = guild.categories[1]
    control_main = control.channels[0]
    host_main = host.channels[1]

    entries = device_messages(control_main)
    assert len(entries) == 1
    _, record = entries[0]
    assert record["installationId"] == manager.installation_id
    assert record["hostname"] == "HOST"
    assert record["agentVersion"] == "0.1.0"
    assert record["operatingSystem"]
    assert record["categoryId"] == host.id
    assert "Created new category" in record["categoryStatus"]
    assert devices.parse_timestamp(record["lastHeartbeat"]) is not None
    assert devices.classify_device(record) == "Connected"
    assert manager._store.device_message_id(guild.id) == entries[0][0].id

    heartbeats = heartbeat_messages(host_main)
    assert len(heartbeats) == 1
    heartbeat = heartbeats[0][1]
    assert heartbeat["installationId"] == manager.installation_id
    assert heartbeat["lastHeartbeat"] == record["lastHeartbeat"]
    assert manager._store.heartbeat_message_id(guild.id) == heartbeats[0][0].id
    # The registry entry is plain content, not a Components V2 report.
    assert control_main.contents[0].startswith("```astral-device\n")


def test_restart_updates_the_same_registry_and_heartbeat_messages(tmp_path):
    path = tmp_path / "identity.json"
    bot = SimpleNamespace(guilds=[])
    first = CategoryManager("HOST", "machineid", state_path=path,
                            collect_fn=stub_collector())
    guild = FakeGuild()
    bot.guilds = [guild]
    _run(ensure_host_categories(bot, "HOST", "machineid", manager=first,
                                config={"agentVersion": "0.1.0"}))
    control_main = guild.categories[0].channels[0]
    host_main = guild.categories[1].channels[1]
    first_record = device_messages(control_main)[0][1]
    first_registered = first_record["registeredAt"]
    control_messages = len(control_main.messages)
    heartbeat_messages_count = len(host_main.messages)

    restarted = CategoryManager("HOST", "machineid", state_path=path,
                                collect_fn=stub_collector())
    _run(ensure_host_categories(bot, "HOST", "machineid", manager=restarted,
                                config={"agentVersion": "2.0.0"}))

    assert len(guild.categories) == 2
    assert len(control_main.messages) == control_messages
    assert len(host_main.messages) == heartbeat_messages_count
    entries = device_messages(control_main)
    assert len(entries) == 1
    record = entries[0][1]
    assert record["installationId"] == first.installation_id
    assert record["registeredAt"] == first_registered
    assert record["agentVersion"] == "2.0.0"
    assert restarted._store.device_message_id(guild.id) == entries[0][0].id


def test_refresh_edits_the_same_heartbeat_and_registry_messages(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    _run(ensure_host_categories(SimpleNamespace(guilds=[guild]), "HOST", "machineid",
                                manager=manager, config={"agentVersion": "0.1.0"}))
    control_main = guild.categories[0].channels[0]
    host_main = guild.categories[1].channels[1]
    registry_message = device_messages(control_main)[0][0]
    heartbeat_message = heartbeat_messages(host_main)[0][0]
    before = len(control_main.messages), len(host_main.messages)

    later = datetime.datetime(2026, 10, 23, 12, 5, 0, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(devices, "utc_now", lambda: later)
    assert _run(manager.refresh(guild, {"agentVersion": "0.1.0"})) is True

    assert (len(control_main.messages), len(host_main.messages)) == before
    assert device_messages(control_main)[0][0] is registry_message
    assert heartbeat_messages(host_main)[0][0] is heartbeat_message
    record = devices.decode_record(registry_message.content)
    heartbeat = devices.decode_heartbeat(heartbeat_message.content)
    assert record["lastHeartbeat"] == "2026-10-23T12:05:00Z"
    assert heartbeat["lastHeartbeat"] == record["lastHeartbeat"]
    assert devices.classify_device(record) == "Connected"


def test_device_list_details_permissions_and_ambiguous_prefix(tmp_path, monkeypatch):
    from astral_remote.agent import discord_bot

    manager = make_manager(tmp_path)
    guild = FakeGuild()
    _run(ensure_host_categories(SimpleNamespace(guilds=[guild]), "HOST", "machineid",
                                manager=manager, config={"agentVersion": "0.1.0"}))
    control_main = guild.categories[0].channels[0]
    host_main = guild.categories[1].channels[1]
    monkeypatch.setattr(setup, "_default_manager", manager)
    handler = discord_bot.build_bot({"prefix": "!", "agentVersion": "0.1.0"}).on_message
    control_main.contents.clear()

    _run(handler(DeviceMemberMessage(control_main, "!device")))
    assert control_main.contents
    listing = control_main.contents[0]
    assert listing.startswith("```\n") and listing.endswith("\n```")
    assert "ASTRAL REMOTE — DEVICES" in listing
    assert "Total Devices : 1" in listing
    assert manager.installation_id in listing
    assert "HOST" in listing

    # A non-admin member gets a denial and no registry data.
    control_main.contents.clear()
    denied = DeviceMemberMessage(control_main, "!device", manage_guild=False, author_id=99)
    _run(handler(denied))
    assert len(control_main.contents) == 1
    assert "Permission denied" in control_main.contents[0]
    assert manager.installation_id not in control_main.contents[0]

    # A unique prefix resolves the same device.
    control_main.contents.clear()
    _run(handler(DeviceMemberMessage(control_main, f"!device {manager.installation_id[:8]}")))
    assert len(control_main.contents) == 1
    assert "ASTRAL REMOTE — DEVICE DETAILS" in control_main.contents[0]
    assert "Status" in control_main.contents[0] and "Connected" in control_main.contents[0]
    assert manager.installation_id in control_main.contents[0]
    assert "Public IP" not in control_main.contents[0]
    assert "MAC" not in control_main.contents[0]

    # Two records sharing a prefix are ambiguous, not silently chosen.
    _run(control_main.send(devices.encode_record({
        "installationId": "abc1111111111111", "hostname": "PC-1",
        "operatingSystem": "Windows 11 Pro", "agentVersion": "0.1.0",
        "lastHeartbeat": "2026-10-23T11:59:00Z", "registeredAt": "2026-10-20T00:00:00Z",
    })))
    _run(control_main.send(devices.encode_record({
        "installationId": "abc2222222222222", "hostname": "PC-2",
        "operatingSystem": "Windows 11 Pro", "agentVersion": "0.1.0",
        "lastHeartbeat": "2026-10-23T11:59:00Z", "registeredAt": "2026-10-20T00:00:00Z",
    })))
    control_main.contents.clear()
    _run(handler(DeviceMemberMessage(control_main, "!device abc")))
    assert "Ambiguous installation ID prefix." in control_main.contents[0]
    assert "abc1111111111111" in control_main.contents[0]
    assert "abc2222222222222" in control_main.contents[0]

    # Unknown IDs are reported as not found; no example device is invented.
    control_main.contents.clear()
    _run(handler(DeviceMemberMessage(control_main, "!device doesnotexist")))
    assert "Device not found" in control_main.contents[0]
    assert "DESKTOP" not in control_main.contents[0]

    # `device` is not available in the host #main, and the bot ignores its
    # own messages, DMs, and unknown channels.
    host_main.contents.clear()
    _run(handler(DeviceMemberMessage(host_main, "!device")))
    assert host_main.contents == []
    control_main.contents.clear()
    _run(handler(DeviceMemberMessage(control_main, "!device", author_id=9001, bot=True)))
    _run(handler(DeviceMemberMessage(FakeTextChannel(4242, "dm"), "!device")))
    assert control_main.contents == []


def test_old_heartbeat_is_disconnected_in_the_list(tmp_path, monkeypatch):
    from astral_remote.agent import discord_bot

    manager = make_manager(tmp_path)
    guild = FakeGuild()
    _run(ensure_host_categories(SimpleNamespace(guilds=[guild]), "HOST", "machineid",
                                manager=manager, config={"agentVersion": "0.1.0"}))
    control_main = guild.categories[0].channels[0]
    message, record = device_messages(control_main)[0]
    record["lastHeartbeat"] = "2026-10-22T00:00:00Z"
    record["updatedAt"] = "2026-10-22T00:00:00Z"
    message.content = devices.encode_record(record)
    monkeypatch.setattr(setup, "_default_manager", manager)
    handler = discord_bot.build_bot({"prefix": "!"}).on_message
    control_main.contents.clear()

    _run(handler(DeviceMemberMessage(control_main, "!device")))
    listing = control_main.contents[0]
    assert "Disconnected  : 1" in listing
    assert "[ DISCONNECTED ]" in listing
    assert "Last Seen" in listing


def test_multiple_agents_share_the_control_registry(tmp_path):
    guild = FakeGuild()
    managers = start_agents(tmp_path, guild, 2)
    control_main = guild.categories[0].channels[0]

    entries = device_messages(control_main)
    assert len(entries) == 2
    ids = {record["installationId"] for _, record in entries}
    assert ids == {manager.installation_id for manager in managers}
    for manager in managers:
        records = _run(manager.device_records(control_main.id))
        assert {record["installationId"] for record in records} == ids


def test_registration_write_failure_is_not_recorded_as_success(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    _run(manager.ensure_guild(guild))
    _run(manager.ensure_control(guild))
    control_main = guild.categories[1].channels[0]

    async def fail_send(content=None, view=None, **kwargs):
        raise RuntimeError("Missing Permissions")

    control_main.send = fail_send
    assert _run(manager.register_device(guild, {"agentVersion": "0.1.0"})) is False
    assert manager._store.device_message_id(guild.id) is None
    assert device_messages(control_main) == []


def test_heartbeat_write_failure_leaves_the_device_unknown(tmp_path):
    manager = make_manager(tmp_path)
    guild = FakeGuild()
    _run(manager.ensure_guild(guild))
    _run(manager.ensure_control(guild))
    control = next(category for category in guild.categories if category.name == CONTROL_CATEGORY_NAME)
    host = next(category for category in guild.categories if category.name != CONTROL_CATEGORY_NAME)
    control_main = control.channels[0]
    host_main = host.channels[1]

    async def fail_send(content=None, view=None, **kwargs):
        raise RuntimeError("Missing Permissions")

    host_main.send = fail_send
    assert _run(manager.register_device(guild, {"agentVersion": "0.1.0"})) is True
    record = device_messages(control_main)[0][1]
    assert record["lastHeartbeat"] is None
    assert devices.classify_device(record) == "Unknown"
    assert manager._store.heartbeat_message_id(guild.id) is None


def test_device_list_survives_a_partial_send_failure(tmp_path, monkeypatch):
    from astral_remote.agent import discord_bot

    manager = make_manager(tmp_path)
    guild = FakeGuild()
    _run(ensure_host_categories(SimpleNamespace(guilds=[guild]), "HOST", "machineid",
                                manager=manager, config={"agentVersion": "0.1.0"}))
    control_main = guild.categories[0].channels[0]
    for index in range(120):
        _run(control_main.send(devices.encode_record({
            "installationId": f"{index:016x}", "hostname": f"PC-{index:03d}",
            "operatingSystem": "Windows 11 Pro", "agentVersion": "0.1.0",
            "lastHeartbeat": "2026-10-23T11:59:00Z",
            "registeredAt": "2026-10-20T00:00:00Z",
        })))
    monkeypatch.setattr(setup, "_default_manager", manager)
    handler = discord_bot.build_bot({"prefix": "!"}).on_message
    control_main.contents.clear()
    records = _run(manager.device_records(control_main.id))
    parts = len(devices.format_devices(records, "!", now=devices.utc_now()))
    assert parts > 1

    original_send = control_main.send
    calls = {"count": 0}

    async def flaky_send(content=None, view=None, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("temporary send failure")
        return await original_send(content=content, view=view, **kwargs)

    control_main.send = flaky_send
    _run(handler(DeviceMemberMessage(control_main, "!device")))
    assert calls["count"] == parts
    # One part failed, but the remaining parts were still attempted.
    assert len(control_main.contents) == parts - 1
    assert all(part.startswith("```\n") for part in control_main.contents)
