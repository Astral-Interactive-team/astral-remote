# Per-computer Discord category setup.
# Each Agent installation manages exactly one category per guild so
# operators can tell managed computers apart. The category name carries a
# stable, non-secret installation ID (bracketed) that is matched on
# restarts instead of relying on the display hostname alone; the created
# category ID is also remembered locally. Alongside the category one
# managed #info text channel is ensured (channels are added one at a
# time); the bot publishes this computer's full Components V2 system
# report there and refreshes the same tracked messages on later starts.
# Repeated on_ready events, reconnects, and concurrent startup tasks reuse
# the same category, guarded by a per-guild lock; the resolved category is
# cached in memory, so a reconnect never republishes the report. Existing
# duplicates left by older builds are reported for administrator review
# and never deleted. Missing Manage Channels permission or HTTP failures
# only warn and are not retried in a loop. The agent exposes no Discord
# commands, so this module owns the whole Discord surface besides login.

from __future__ import annotations

import asyncio

from astral_remote.agent.identity import StateStore
from astral_remote.shared.logging_utils import info, warn

MAX_CATEGORY_NAME = 100
LEGACY_HOST_PREFIX = "pc "
DEFAULT_HOST = "unknown-host"
DEFAULT_ID = "unknown-id"
INFO_CHANNEL_NAME = "info"


def collect_inventory(agent: dict, env=None) -> dict:
    """Run the read-only system inventory for the #info report.

    Imported lazily so this module stays importable off Windows, and
    exposed at module level so tests and callers can substitute a fixed
    inventory without touching the collector.
    """
    from astral_remote.agent.windows.inventory import collect

    return collect(agent, env=env)


def _compose(prefix: str, hostname: str, marker: str) -> str:
    """Join `<prefix><host> <marker>` and cap the hostname so it fits Discord."""
    host = (hostname or DEFAULT_HOST).strip() or DEFAULT_HOST
    fixed = len(prefix) + 1 + len(marker)
    limit = MAX_CATEGORY_NAME - fixed
    if limit < 1:
        limit = 1
    if len(host) > limit:
        host = host[: max(limit - 1, 1)] + "…"
    return f"{prefix}{host} {marker}"


def host_category_name(hostname: str, install_id: str) -> str:
    """Deterministic category name for this installation: `<host> [<id>]`.

    The bracketed installation ID is the stable identifier; the hostname
    is only a display hint and may change without breaking the match.
    Never includes the Windows username or a filesystem path.
    """
    return _compose("", hostname, install_marker(install_id))


def legacy_host_category_name(hostname: str, host_id: str) -> str:
    """Category name emitted by older builds: `pc <host> (<host_id>)`."""
    hid = (host_id or DEFAULT_ID).strip() or DEFAULT_ID
    return _compose(LEGACY_HOST_PREFIX, hostname, f"({hid})")


def install_marker(install_id: str) -> str:
    """The stable, matchable fragment embedded in every managed category name."""
    ident = (install_id or DEFAULT_ID).strip() or DEFAULT_ID
    return f"[{ident}]"


def _report(message: str, detail=None) -> None:
    """Surface an operator-reviewable problem to stdout and the file log.

    The windowed EXE has no console, so duplicate/ambiguous-category
    reports also land in the sanitized file log. Reporting must never
    break setup.
    """
    warn(message, detail)
    try:
        from astral_remote.agent.error_log import log_warn

        suffix = f" {detail}" if detail else ""
        log_warn(f"{message}{suffix}")
    except Exception:  # noqa: BLE001 - reporting is best-effort
        pass


class CategoryManager:
    """Coordinate one managed category per guild for this installation.

    Holds the persistent identity store, one asyncio.Lock per guild
    (recreated when the running event loop changes), and in-memory
    caches of resolved and failed guilds. Resolved guilds short-circuit
    repeated on_ready events; failed guilds are skipped so permission or
    HTTP errors never trigger a creation loop. Instances are safe to
    share; each guild is serialized by its own lock.
    """

    def __init__(self, hostname: str, host_id: str, store: StateStore | None = None,
                 state_path=None, env=None, collect_fn=None):
        self._hostname = hostname
        self._host_id = host_id
        self._store = store if store is not None else StateStore(state_path, env)
        self._env = env
        self._collect = collect_fn if collect_fn is not None else collect_inventory
        self._locks: dict = {}
        self._resolved: dict = {}
        self._failed: set = set()
        self._info_channels: set = set()
        self._category_status: dict = {}
        self._duplicate_guilds: set = set()
        self._channel_counts: dict = {}

    @property
    def installation_id(self) -> str:
        return self._store.installation_id

    def is_info_channel(self, channel_id) -> bool:
        """True when the channel is a managed #info channel for this process."""
        return channel_id is not None and channel_id in self._info_channels

    def _lock(self, guild_id):
        """Return this guild's lock, recreating it when the event loop changes."""
        loop = asyncio.get_running_loop()
        entry = self._locks.get(guild_id)
        if entry is None or entry[0] is not loop:
            lock = asyncio.Lock()
            self._locks[guild_id] = (loop, lock)
            return lock
        return entry[1]

    async def ensure_guild(self, guild, config=None, runtime=None):
        """Ensure exactly one managed category and its #info report in a guild.

        Returns the category, or None when it cannot be ensured (no
        permission, HTTP error, ambiguous legacy state). Never raises.
        A guild already resolved in this process is returned unchanged,
        so reconnects and repeated on_ready events never republish.
        """
        guild_id = getattr(guild, "id", None)
        if guild_id is None:
            return None
        if guild_id in self._resolved:
            return self._resolved[guild_id]
        if guild_id in self._failed:
            return None
        async with self._lock(guild_id):
            if guild_id in self._resolved:
                return self._resolved[guild_id]
            if guild_id in self._failed:
                return None
            try:
                category = await self._resolve(guild)
            except Exception as err:  # noqa: BLE001 - Forbidden/HTTP/Hierarchy only warn
                warn("Host category unavailable.", {"guild": guild_id, "message": str(err)})
                self._failed.add(guild_id)
                return None
            if category is None:
                # Ambiguous or unrecoverable: do not retry in a loop.
                self._failed.add(guild_id)
                return None
            await self._ensure_info(guild, category, config, runtime)
            self._resolved[guild_id] = category
            return category

    async def _resolve(self, guild):
        guild_id = getattr(guild, "id", None)
        install_id = self._store.installation_id
        expected = host_category_name(self._hostname, install_id)

        # 1. Reuse the exact category remembered from a previous run.
        remembered = self._store.category_id(guild_id)
        if remembered is not None:
            found = self._find_by_id(guild, remembered)
            if found is not None:
                self._set_status(guild_id, "Reused remembered category", found)
                matches = self._managed_matches(guild, install_id)
                if len(matches) > 1:
                    self._report_duplicates(guild_id, matches)
                elif getattr(found, "name", None) != expected:
                    await self._rename(found, expected)
                return found

        # 2. Reuse any category already carrying this installation's marker.
        matches = self._managed_matches(guild, install_id)
        if matches:
            chosen = matches[0]
            self._store.remember(guild_id, getattr(chosen, "id", None), getattr(chosen, "name", expected))
            self._set_status(guild_id, "Matched installation marker", chosen)
            if getattr(chosen, "name", None) != expected:
                await self._rename(chosen, expected)
            if len(matches) > 1:
                self._report_duplicates(guild_id, matches)
            return chosen

        # 3. Adopt a legacy category from an older build when unambiguous.
        legacy = self._legacy_matches(guild)
        if len(legacy) == 1:
            chosen = legacy[0]
            self._store.remember(guild_id, getattr(chosen, "id", None), getattr(chosen, "name", expected))
            self._set_status(guild_id, "Adopted legacy category", chosen)
            await self._rename(chosen, expected)
            info("Adopted a legacy host category.", {"guild": guild_id, "category": getattr(chosen, "id", None)})
            return chosen
        if len(legacy) > 1:
            _report("Ambiguous legacy host categories; not creating another.",
                    {"guild": guild_id, "count": len(legacy),
                     "categoryIds": [getattr(c, "id", None) for c in legacy]})
            return None

        # 4. Nothing matched: create exactly one managed category.
        created = await guild.create_category(expected, reason="AstralRemote host identification")
        self._store.remember(guild_id, getattr(created, "id", None), getattr(created, "name", expected))
        self._set_status(guild_id, "Created new category", created)
        info("Created host category.", {"guild": guild_id, "category": getattr(created, "id", None)})
        return created

    def _set_status(self, guild_id, label: str, category) -> None:
        """Remember how this run resolved the category, for the report."""
        self._category_status[guild_id] = f"{label} (id {getattr(category, 'id', None)})"

    def _categories(self, guild) -> list:
        try:
            return list(getattr(guild, "categories", []) or [])
        except Exception:  # noqa: BLE001 - cache shape is external
            return []

    def _find_by_id(self, guild, category_id):
        for category in self._categories(guild):
            if getattr(category, "id", None) == category_id:
                return category
        return None

    def _managed_matches(self, guild, install_id) -> list:
        marker = install_marker(install_id)
        return [c for c in self._categories(guild) if marker in str(getattr(c, "name", "") or "")]

    def _legacy_matches(self, guild) -> list:
        legacy_name = legacy_host_category_name(self._hostname, self._host_id)
        return [c for c in self._categories(guild) if str(getattr(c, "name", "") or "") == legacy_name]

    def _report_duplicates(self, guild_id, matches) -> None:
        self._duplicate_guilds.add(guild_id)
        _report("Multiple host categories detected; leaving them for administrator review.",
                {"guild": guild_id, "count": len(matches),
                 "categoryIds": [getattr(c, "id", None) for c in matches]})

    async def _rename(self, category, new_name: str) -> None:
        """Best-effort rename of an adopted category. Never raises."""
        edit = getattr(category, "edit", None)
        if not callable(edit):
            return
        try:
            await edit(name=new_name)
        except Exception as err:  # noqa: BLE001 - rename is cosmetic
            warn("Host category rename skipped.", {"message": str(err)})

    async def _ensure_info(self, guild, category, config, runtime) -> None:
        """Ensure the managed #info channel and refresh the system report.

        Best-effort: a missing channel or a failed publication only warns,
        so category setup still succeeds. Never raises.
        """
        guild_id = getattr(guild, "id", None)
        try:
            channel = await self._ensure_info_channel(guild, category)
        except Exception as err:  # noqa: BLE001 - Forbidden/HTTP only warn
            warn("Host info channel unavailable.", {"guild": guild_id, "message": str(err)})
            return
        if channel is None:
            return
        channel_id = getattr(channel, "id", None)
        if channel_id is not None:
            self._info_channels.add(channel_id)
        self._channel_counts[guild_id] = 1
        try:
            await self._publish_report(guild, channel, config, runtime)
        except Exception as err:  # noqa: BLE001 - publishing only warns
            warn("Host info report unavailable.", {"guild": guild_id, "message": str(err)})

    async def _ensure_info_channel(self, guild, category):
        """Return the managed #info channel, creating exactly one when missing."""
        guild_id = getattr(guild, "id", None)
        remembered = self._store.info_channel_id(guild_id)
        if remembered is not None:
            found = self._find_channel(category, remembered)
            if found is not None:
                return found
        existing = self._find_channel_by_name(category, INFO_CHANNEL_NAME)
        if existing is not None:
            self._store.remember_info(guild_id, getattr(existing, "id", None))
            return existing
        create = getattr(category, "create_text_channel", None)
        if not callable(create):
            return None
        created = await create(INFO_CHANNEL_NAME, reason="AstralRemote host info")
        info("Created host info channel.", {"guild": guild_id, "channel": getattr(created, "id", None)})
        self._store.remember_info(guild_id, getattr(created, "id", None))
        return created

    def _agent_facts(self, guild, config, runtime) -> dict:
        """Assemble the Agent facts the report shows for this guild.

        Only facts this build really maintains are supplied; the report
        renders Unavailable for anything the collector cannot confirm.
        """
        from astral_remote.agent.windows.info import get_local_ip, get_uptime_seconds

        guild_id = getattr(guild, "id", None)
        settings = config or {}
        link = runtime or {}
        return {
            "hostname": self._hostname,
            "installationId": self._store.installation_id,
            "localIpv4": get_local_ip(),
            "agentVersion": settings.get("agentVersion"),
            "startupName": settings.get("startupName"),
            "connected": bool(link.get("connected", True)),
            "latencySeconds": link.get("latencySeconds"),
            "reconnects": link.get("reconnects"),
            "connection": "Connected",
            "categoryStatus": self._category_status.get(guild_id),
            "channelCount": self._channel_counts.get(guild_id),
            "duplicateWarning": (
                "Duplicates reported; review the agent log"
                if guild_id in self._duplicate_guilds else None
            ),
            "uptimeSeconds": get_uptime_seconds(),
        }

    async def _publish_report(self, guild, channel, config, runtime) -> None:
        """Collect the inventory off the event loop and publish the report."""
        from astral_remote.agent.info_report import build_report, build_views, publish_report

        guild_id = getattr(guild, "id", None)
        facts = self._agent_facts(guild, config, runtime)
        # Collection walks the registry, samples the CPU, and runs one
        # PowerShell pass, so it runs on a worker thread and never blocks
        # the Discord event loop.
        inventory = await asyncio.to_thread(self._collect, facts, self._env)
        _, pages = build_report(inventory)
        views = build_views(pages)
        outcome = await publish_report(channel, views, self._store, guild_id)
        info("Host info report published.", {
            "guild": guild_id,
            "parts": len(views),
            "reused": outcome["reused"],
            "failedPart": outcome["failedAt"],
        })

    def _category_channels(self, category) -> list:
        try:
            return list(getattr(category, "channels", []) or [])
        except Exception:  # noqa: BLE001 - cache shape is external
            return []

    def _find_channel(self, category, channel_id):
        for channel in self._category_channels(category):
            if getattr(channel, "id", None) == channel_id:
                return channel
        return None

    def _find_channel_by_name(self, category, name):
        for channel in self._category_channels(category):
            if str(getattr(channel, "name", "") or "") == name:
                return channel
        return None


_default_manager: CategoryManager | None = None


def default_manager(hostname: str, host_id: str) -> CategoryManager:
    """Return the process-wide manager reused across on_ready events."""
    global _default_manager
    if _default_manager is None:
        _default_manager = CategoryManager(hostname, host_id)
    return _default_manager


def is_info_channel(channel_id) -> bool:
    """True when the channel is a managed #info channel of this process."""
    manager = _default_manager
    return manager is not None and manager.is_info_channel(channel_id)


def runtime_facts(bot) -> dict:
    """Read the bot's live link facts for the #info report.

    discord.py reports nan or infinity for latency before the first
    heartbeat, so an unusable value becomes Unavailable instead of a
    bogus number. Reconnect count is whatever the bot counted so far.
    """
    latency = getattr(bot, "latency", None)
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        latency = None
    elif latency != latency or latency < 0 or latency > 10.0:
        latency = None
    reconnects = getattr(bot, "_astral_reconnects", None)
    return {
        "connected": True,
        "latencySeconds": latency,
        "reconnects": reconnects if isinstance(reconnects, int) else None,
    }


async def ensure_host_categories(bot, hostname: str, host_id: str, manager: CategoryManager | None = None,
                                 config=None) -> int:
    """Ensure this installation's category and #info report in every guild.

    One managed category per guild is reused across reconnects. Returns
    the guild count attempted. Never raises. An explicit manager may be
    passed for isolated callers and tests.
    """
    active = manager if manager is not None else default_manager(hostname, host_id)
    runtime = runtime_facts(bot)
    guilds = list(getattr(bot, "guilds", []) or [])
    for guild in guilds:
        try:
            await active.ensure_guild(guild, config, runtime)
        except Exception:
            continue
    return len(guilds)
