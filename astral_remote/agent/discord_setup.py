# Per-computer Discord category setup and shared device registry.
# Each Agent installation manages exactly one category per guild so
# operators can tell managed computers apart. The category name carries a
# stable, non-secret installation ID (bracketed) that is matched on
# restarts instead of relying on the display hostname alone; the created
# category ID is also remembered locally. Alongside the category two
# managed text channels are ensured in order (channels are added one at a
# time): #info carries this computer's full Components V2 system report,
# and #main sits directly below it as the operator command channel. The
# bot refreshes the same tracked #info messages on later starts and, while
# connected, every minute. Host #main accepts `status`, stores the
# reusable per-computer heartbeat message, and answers with the facts of
# the computer that owns that category.
# A separate server-wide `CONTROL` category is ensured once per guild with
# only a #main channel. CONTROL #main is the shared administrator command
# channel for `device`; it also holds one machine-readable registration
# message per installation. Those messages are the server-wide registry,
# so every Agent reads the same Discord data and no local file is treated
# as the whole server's device list. A legacy #logs channel is left
# untouched but never created by this build.
# Registration happens without an approval step. Heartbeats are written to
# the computer's own host #main and are mirrored to its CONTROL registry
# message only after the Discord write succeeds. Consumers derive
# Connected/Disconnected/Unknown from the heartbeat age.
# Control setup starts before host setup and runs in parallel, so the
# control category appears while the first report is still being collected
# and published.
# Repeated on_ready events, reconnects, and concurrent startup tasks reuse
# the same category, guarded by a per-guild lock; the resolved category is
# cached in memory, so a reconnect never republishes the report. Existing
# duplicates left by older builds are reported for administrator review
# and never deleted. Missing Manage Channels permission or HTTP failures
# only warn and are not retried in a loop. The agent exposes no slash
# commands and no command tree; this module owns the whole Discord surface
# besides login.

from __future__ import annotations

import asyncio
import datetime

from astral_remote import AGENT_VERSION
from astral_remote.agent import devices
from astral_remote.agent.identity import StateStore
from astral_remote.shared.logging_utils import info, warn

MAX_CATEGORY_NAME = 100
LEGACY_HOST_PREFIX = "pc "
DEFAULT_HOST = "unknown-host"
DEFAULT_ID = "unknown-id"
INFO_CHANNEL_NAME = "info"
MAIN_CHANNEL_NAME = "main"
CONTROL_CATEGORY_NAME = "CONTROL"
LOGS_CHANNEL_NAME = "logs"

# Command availability is decided per managed channel. #info is the
# read-only dashboard and accepts no commands; the host #main accepts the
# read-only `status` command; the server-wide CONTROL #main accepts the
# administrator-only `device` command. Every other channel accepts nothing.
INFO_CHANNEL_COMMANDS: tuple[str, ...] = ()
MAIN_CHANNEL_COMMANDS: tuple[str, ...] = ("status",)
CONTROL_MAIN_COMMANDS: tuple[str, ...] = ("device",)


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
        self._main_channels: set = set()
        self._main_channel_objects: dict = {}
        self._control_mains: dict = {}
        self._control_main_ids: set = set()
        self._category_status: dict = {}
        self._duplicate_guilds: set = set()
        self._channel_counts: dict = {}
        self._os_name: str | None = None
        self._device_conflicts: set = set()
        self._device_warned: set = set()

    @property
    def installation_id(self) -> str:
        return self._store.installation_id

    def is_info_channel(self, channel_id) -> bool:
        """True when the channel is a managed #info channel for this process."""
        return channel_id is not None and channel_id in self._info_channels

    def is_main_channel(self, channel_id) -> bool:
        """True when the channel is a managed host #main channel for this process."""
        return channel_id is not None and channel_id in self._main_channels

    def is_control_main_channel(self, channel_id) -> bool:
        """True when the channel is this process's shared CONTROL #main."""
        return channel_id is not None and channel_id in self._control_main_ids

    def commands_for(self, channel_id) -> tuple[str, ...]:
        """Return the commands this process accepts in one managed channel.

        Host #main accepts `status`; the shared CONTROL #main accepts the
        administrator-only `device`; #info and every unmanaged channel
        accept none.
        """
        if channel_id is not None and channel_id in self._control_main_ids:
            return CONTROL_MAIN_COMMANDS
        if channel_id is not None and channel_id in self._main_channels:
            return MAIN_CHANNEL_COMMANDS
        return INFO_CHANNEL_COMMANDS

    def _lock(self, guild_id):
        """Return the setup lock for a key, recreating it on a new event loop."""
        loop = asyncio.get_running_loop()
        entry = self._locks.get(guild_id)
        if entry is None or entry[0] is not loop:
            lock = asyncio.Lock()
            self._locks[guild_id] = (loop, lock)
            return lock
        return entry[1]

    def _control_lock(self, guild_id):
        """Return the control-setup lock, separate from the host setup lock.

        A separate lock lets the control category be created while the host
        report is still being collected and published.
        """
        return self._lock(("control", guild_id))

    async def ensure_guild(self, guild, config=None, runtime=None):
        """Ensure exactly one managed category and its channels in a guild.

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
            await self._ensure_channels(guild, category, config, runtime)
            self._resolved[guild_id] = category
            return category

    async def refresh(self, guild, config=None, runtime=None) -> bool:
        """Republish the #info report for a guild already set up in this process.

        Returns True when the report was refreshed. Guilds that never
        resolved (missing permission, HTTP/API failure, ambiguous legacy
        state) are skipped, so the periodic refresh never becomes a retry
        loop for a broken setup and never creates a second category. The
        per-guild lock serializes the refresh with on_ready setup. Never
        raises.
        """
        guild_id = getattr(guild, "id", None)
        if guild_id is None or guild_id not in self._resolved:
            return False
        async with self._lock(guild_id):
            category = self._resolved.get(guild_id)
            if category is None:
                return False
            try:
                await self._ensure_channels(guild, category, config, runtime, announce=False)
                await self._refresh_device_presence(guild, config, runtime)
            except Exception as err:  # noqa: BLE001 - refresh must keep running
                warn("Host info refresh unavailable.", {"guild": guild_id, "message": str(err)})
                return False
        return True

    async def ensure_control(self, guild) -> object | None:
        """Ensure the server-wide CONTROL category and its #main channel.

        Exactly one control category is kept per guild, independent of this
        computer's host category. A legacy #logs channel is left in place
        but is not created by this build. The CONTROL #main is remembered
        and cached for device registration and the `device` command. A
        failure warns and never blocks host setup. Never raises.
        """
        guild_id = getattr(guild, "id", None)
        if guild_id is None:
            return None
        async with self._control_lock(guild_id):
            try:
                category = await self._ensure_control_category(guild)
            except Exception as err:  # noqa: BLE001 - Forbidden/HTTP only warn
                warn("Control category unavailable.", {"guild": guild_id, "message": str(err)})
                return None
            if category is None:
                return None
            try:
                main_channel = await self._ensure_control_channel(
                    category,
                    MAIN_CHANNEL_NAME,
                    self._store.control_main_channel_id(guild_id),
                    guild_id,
                )
            except Exception as err:  # noqa: BLE001 - Forbidden/HTTP only warn
                warn("Control main channel unavailable.", {"guild": guild_id, "message": str(err)})
                main_channel = None
            if main_channel is not None:
                main_id = getattr(main_channel, "id", None)
                if main_id is not None:
                    old = self._control_mains.get(guild_id)
                    if old is not None and getattr(old, "id", None) != main_id:
                        self._control_main_ids.discard(getattr(old, "id", None))
                    self._control_mains[guild_id] = main_channel
                    self._control_main_ids.add(main_id)
                    self._store.remember_control(guild_id, main_channel_id=main_id)
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

    def _find_category_by_name(self, guild, name: str):
        """Return a guild category by display name, case-insensitively."""
        target = str(name or "").strip().casefold()
        for category in self._categories(guild):
            if str(getattr(category, "name", "") or "").strip().casefold() == target:
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

    async def _ensure_channels(self, guild, category, config, runtime, announce: bool = True) -> None:
        """Ensure the managed #info and #main channels, then refresh the report.

        #info carries the read-only dashboard; #main is created right after
        it so it appears directly below #info and stays the operator
        command channel. Both steps are best-effort: a missing channel or
        a failed publication only warns, so category setup still succeeds.
        `announce` keeps the periodic refresh from logging every pass.
        Never raises.
        """
        guild_id = getattr(guild, "id", None)
        info_channel = None
        try:
            info_channel = await self._ensure_info_channel(guild, category)
        except Exception as err:  # noqa: BLE001 - Forbidden/HTTP only warn
            warn("Host info channel unavailable.", {"guild": guild_id, "message": str(err)})
        main_channel = None
        try:
            main_channel = await self._ensure_main_channel(guild, category)
        except Exception as err:  # noqa: BLE001 - Forbidden/HTTP only warn
            warn("Host main channel unavailable.", {"guild": guild_id, "message": str(err)})
        if main_channel is not None:
            main_id = getattr(main_channel, "id", None)
            if main_id is not None:
                # Each process tracks only its own #main channels, so an
                # answer describes this computer, not another category.
                self._main_channels.add(main_id)
                self._main_channel_objects[guild_id] = main_channel
        self._channel_counts[guild_id] = int(info_channel is not None) + int(main_channel is not None)
        if info_channel is None:
            return
        channel_id = getattr(info_channel, "id", None)
        if channel_id is not None:
            self._info_channels.add(channel_id)
        try:
            await self._publish_report(guild, info_channel, config, runtime, announce=announce)
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

    async def _ensure_main_channel(self, guild, category):
        """Return the managed #main channel, creating exactly one when missing.

        #main is the operator command channel, so the Agent never publishes
        the report there and never deletes messages posted in it. It is
        ensured after #info so a fresh category lists #info first.
        """
        guild_id = getattr(guild, "id", None)
        remembered = self._store.main_channel_id(guild_id)
        if remembered is not None:
            found = self._find_channel(category, remembered)
            if found is not None:
                return found
        existing = self._find_channel_by_name(category, MAIN_CHANNEL_NAME)
        if existing is not None:
            self._store.remember_main(guild_id, getattr(existing, "id", None))
            return existing
        create = getattr(category, "create_text_channel", None)
        if not callable(create):
            return None
        created = await create(MAIN_CHANNEL_NAME, reason="AstralRemote operator commands")
        info("Created host main channel.", {"guild": guild_id, "channel": getattr(created, "id", None)})
        self._store.remember_main(guild_id, getattr(created, "id", None))
        return created

    async def _ensure_control_category(self, guild):
        """Return the remembered or named control category, creating one."""
        guild_id = getattr(guild, "id", None)
        remembered = self._store.control_category_id(guild_id)
        if remembered is not None:
            found = self._find_by_id(guild, remembered)
            if found is not None:
                return found
        existing = self._find_category_by_name(guild, CONTROL_CATEGORY_NAME)
        if existing is not None:
            self._store.remember_control(guild_id, category_id=getattr(existing, "id", None))
            return existing
        try:
            created = await guild.create_category(CONTROL_CATEGORY_NAME, reason="AstralRemote control")
        except Exception:
            # Another Agent may have created the same category between the
            # lookup and the request. Re-search once before giving up.
            existing = self._find_category_by_name(guild, CONTROL_CATEGORY_NAME)
            if existing is None:
                raise
            self._store.remember_control(guild_id, category_id=getattr(existing, "id", None))
            return existing
        # Discord permits duplicate names. If a simultaneous Agent created
        # one too, reuse the oldest object and report the extra one instead
        # of creating more or deleting anything.
        matches = self._control_category_matches(guild)
        chosen = created
        if matches:
            chosen = min(matches, key=lambda category: int(getattr(category, "id", 0) or 0))
            if len(matches) > 1:
                _report("Multiple control categories detected; leaving them for administrator review.",
                        {"guild": guild_id, "count": len(matches),
                         "categoryIds": [getattr(c, "id", None) for c in matches]})
        self._store.remember_control(guild_id, category_id=getattr(chosen, "id", None))
        info("Created control category.", {"guild": guild_id, "category": getattr(chosen, "id", None)})
        return chosen

    def _control_category_matches(self, guild) -> list:
        """Return every guild category whose name matches CONTROL."""
        target = CONTROL_CATEGORY_NAME.strip().casefold()
        return [
            category
            for category in self._categories(guild)
            if str(getattr(category, "name", "") or "").strip().casefold() == target
        ]

    async def _ensure_control_channel(self, category, name: str, remembered, guild_id):
        """Return the remembered or named control channel, creating one.

        The CONTROL #main is the only control channel this build ensures.
        A create collision with another Agent triggers one re-search so an
        existing #main is reused. Duplicate control channels are reported
        and never deleted.
        """
        if remembered is not None:
            found = self._find_channel(category, remembered)
            if found is not None:
                return found
        existing = self._find_channel_by_name(category, name)
        if existing is not None:
            return existing
        create = getattr(category, "create_text_channel", None)
        if not callable(create):
            return None
        try:
            created = await create(name, reason="AstralRemote control")
        except Exception:
            existing = self._find_channel_by_name(category, name)
            if existing is None:
                raise
            return existing
        matches = [
            channel
            for channel in self._category_channels(category)
            if str(getattr(channel, "name", "") or "") == name
        ]
        chosen = created
        if matches:
            chosen = min(matches, key=lambda channel: int(getattr(channel, "id", 0) or 0))
            if len(matches) > 1:
                _report("Multiple control channels detected; leaving them for administrator review.",
                        {"guild": guild_id, "name": name,
                         "channelIds": [getattr(c, "id", None) for c in matches]})
        info("Created control channel.", {
            "guild": guild_id,
            "channel": getattr(chosen, "id", None),
            "name": name,
        })
        return chosen

    async def device_records(self, channel_id) -> list[dict] | None:
        """Read the shared registry from this process's CONTROL #main.

        Returns None when the channel is not a CONTROL #main known to this
        process. A Discord read failure raises to the caller so the command
        can report the registry as unavailable instead of inventing an
        empty device list. Duplicate entries are collapsed to the freshest
        record and reported once without deleting anything.
        """
        channel = self._control_channel_by_id(channel_id)
        if channel is None:
            return None
        messages = await self._read_registry_messages(channel)
        records, duplicates = devices.dedupe_records([record for _, record in messages])
        guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if duplicates and channel_id not in self._device_conflicts:
            self._device_conflicts.add(channel_id)
            _report("Duplicate device registry entries detected; using the newest record.",
                    {"guild": guild_id, "duplicates": duplicates})
        return records

    def _control_channel_by_id(self, channel_id):
        """Return a cached CONTROL #main by channel ID, or None."""
        if channel_id is None:
            return None
        for channel in self._control_mains.values():
            if getattr(channel, "id", None) == channel_id:
                return channel
        return None

    def _host_main_by_guild(self, guild_id):
        """Return the cached host #main channel for heartbeat writes."""
        return self._main_channel_objects.get(guild_id)

    def _is_bot_message(self, message, channel=None) -> bool:
        """True for any bot-authored message.

        Different installations may use different bot accounts but share the
        server's CONTROL channel. Registry reads therefore accept records
        from any bot; writes still touch only this installation's own
        message, as checked by `_is_own_message`.
        """
        return bool(getattr(getattr(message, "author", None), "bot", False))

    def _is_own_message(self, message, channel) -> bool:
        """True only for this client's own message in the given channel."""
        if not self._is_bot_message(message, channel):
            return False
        author = getattr(message, "author", None)
        me = getattr(getattr(channel, "guild", None), "me", None)
        if me is None:
            return True
        return getattr(author, "id", None) == getattr(me, "id", None)

    async def _read_history(self, channel) -> list:
        """Read all messages from a channel, or raise when unavailable."""
        history = getattr(channel, "history", None)
        if not callable(history):
            raise RuntimeError("channel history unavailable")
        return [message async for message in history(limit=None)]

    async def _read_registry_messages(self, channel) -> list[tuple]:
        """Return `(message, record)` pairs written by this bot."""
        entries = []
        for message in await self._read_history(channel):
            if not self._is_bot_message(message, channel):
                continue
            record = devices.decode_record(getattr(message, "content", None))
            if record is not None:
                entries.append((message, record))
        return entries

    async def _read_heartbeat_messages(self, channel) -> list[tuple]:
        """Return this bot's reusable heartbeat messages and records."""
        entries = []
        for message in await self._read_history(channel):
            if not self._is_bot_message(message, channel):
                continue
            record = devices.decode_heartbeat(getattr(message, "content", None))
            if record is not None:
                entries.append((message, record))
        return entries

    @staticmethod
    def _record_moment(record: dict):
        """Return the freshest timestamp available for duplicate selection."""
        for key in ("updatedAt", "lastHeartbeat", "registeredAt"):
            moment = devices.parse_timestamp(record.get(key))
            if moment is not None:
                return moment
        return None

    async def _find_own_message(self, channel, remembered_id, decoder, install_id):
        """Find this installation's reusable message, newest duplicate first.

        The remembered ID avoids a full history scan on every refresh. If it
        is missing, history is scanned for a bot-owned record carrying this
        installation ID. A duplicate is reported but never deleted.
        """
        if remembered_id is not None:
            fetch = getattr(channel, "fetch_message", None)
            if callable(fetch):
                try:
                    message = await fetch(remembered_id)
                except Exception:  # noqa: BLE001 - a deleted message is expected
                    message = None
                if message is not None and self._is_own_message(message, channel):
                    record = decoder(getattr(message, "content", None))
                    if record is not None and record.get("installationId") == install_id:
                        return message, record, 0
        if decoder is devices.decode_record:
            entries = await self._read_registry_messages(channel)
        else:
            entries = await self._read_heartbeat_messages(channel)
        own = [
            (message, record)
            for message, record in entries
            if self._is_own_message(message, channel)
            and record.get("installationId") == install_id
        ]
        if not own:
            return None, None, 0
        own.sort(
            key=lambda entry: self._record_moment(entry[1])
            or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
            reverse=True,
        )
        return own[0][0], own[0][1], len(own) - 1

    def _warn_device(self, guild_id, key: str, message: str, detail=None) -> None:
        """Report a device failure once per guild until it recovers."""
        token = (guild_id, key)
        if token in self._device_warned:
            return
        self._device_warned.add(token)
        _report(message, detail)

    def _clear_device_warning(self, guild_id, key: str) -> None:
        """Allow a recovered device operation to report a future failure."""
        self._device_warned.discard((guild_id, key))

    async def _upsert_message(self, channel, message, content: str, guild_id,
                              setter, key: str) -> bool:
        """Edit one reusable message or send it once, then remember its ID.

        A failed Discord write is never recorded as success. Edit failure is
        not followed by a blind send, because that would create a duplicate;
        the next pass starts from the remembered ID and can recover if the
        message was actually deleted.
        """
        try:
            if message is not None:
                await message.edit(content=content)
                updated = message
            else:
                updated = await channel.send(content=content)
        except Exception as err:  # noqa: BLE001 - missing permission/HTTP
            self._warn_device(guild_id, key, "Device registry write failed.",
                              {"guild": guild_id, "message": str(err)})
            return False
        self._clear_device_warning(guild_id, key)
        setter(guild_id, getattr(updated, "id", None))
        return True

    def _operating_system(self) -> str:
        """Return the cached short OS name used in registry records."""
        if self._os_name is None:
            try:
                from astral_remote.agent.windows.info import get_os_name

                self._os_name = get_os_name()
            except Exception:  # noqa: BLE001 - registry can still register
                self._os_name = "Unavailable"
        return self._os_name

    def _build_device_record(self, guild, category, config, runtime, existing,
                             now, heartbeat_ok: bool) -> dict:
        """Build one registration record, preserving first-registration data."""
        guild_id = getattr(guild, "id", None)
        settings = config or {}
        link = runtime or {}
        existing = existing if isinstance(existing, dict) else {}
        recorded_at = (
            devices.format_timestamp(now)
            if heartbeat_ok
            else existing.get("lastHeartbeat")
        )
        registered_at = existing.get("registeredAt")
        if devices.parse_timestamp(registered_at) is None:
            registered_at = devices.format_timestamp(now)
        category_id = getattr(category, "id", None)
        if not isinstance(category_id, int) or isinstance(category_id, bool):
            category_id = existing.get("categoryId")
        category_status = self._category_status.get(guild_id) or existing.get("categoryStatus")
        record = {
            "version": devices.RECORD_VERSION,
            "installationId": self._store.installation_id,
            "hostname": self._hostname,
            "agentVersion": settings.get("agentVersion") or AGENT_VERSION,
            "operatingSystem": existing.get("operatingSystem") or self._operating_system(),
            "registeredAt": registered_at,
            "lastHeartbeat": recorded_at,
            "latencyMs": devices.latency_milliseconds(link.get("latencySeconds")),
            "connection": devices.connection_label(link.get("connected")),
            "categoryStatus": category_status or "Unavailable",
            "updatedAt": devices.format_timestamp(now),
        }
        if isinstance(category_id, int) and not isinstance(category_id, bool):
            record["categoryId"] = category_id
        return record

    async def _write_heartbeat(self, guild, config, runtime, now) -> bool:
        """Write this computer's reusable heartbeat into its own host #main.

        The record lives in the host category, not the shared CONTROL
        channel, so each installation has an independently observable
        heartbeat. Only a successful edit or send returns True.
        """
        guild_id = getattr(guild, "id", None)
        channel = self._host_main_by_guild(guild_id)
        if channel is None:
            self._warn_device(guild_id, "heartbeat-channel", "Host main heartbeat channel unavailable.",
                              {"guild": guild_id})
            return False
        install_id = self._store.installation_id
        content = devices.encode_heartbeat({
            "version": devices.RECORD_VERSION,
            "installationId": install_id,
            "lastHeartbeat": devices.format_timestamp(now),
            "latencyMs": devices.latency_milliseconds((runtime or {}).get("latencySeconds")),
        })
        try:
            message, _, duplicates = await self._find_own_message(
                channel,
                self._store.heartbeat_message_id(guild_id),
                devices.decode_heartbeat,
                install_id,
            )
        except Exception as err:  # noqa: BLE001 - heartbeat read failure
            self._warn_device(guild_id, "heartbeat-read", "Heartbeat read failed.",
                              {"guild": guild_id, "message": str(err)})
            return False
        if duplicates:
            self._warn_device(guild_id, "heartbeat-duplicate",
                              "Duplicate heartbeat messages detected; using the newest.",
                              {"guild": guild_id, "count": duplicates})
        return await self._upsert_message(
            channel, message, content, guild_id,
            self._store.remember_heartbeat_message, "heartbeat-write",
        )

    async def _sync_device(self, guild, config, runtime, category, announce: bool) -> bool:
        """Register or refresh this installation in the shared registry."""
        guild_id = getattr(guild, "id", None)
        channel = self._control_mains.get(guild_id)
        if channel is None:
            if announce:
                self._warn_device(guild_id, "registry-channel",
                                  "Device registration skipped; CONTROL #main unavailable.",
                                  {"guild": guild_id})
            return False
        now = devices.utc_now()
        heartbeat_ok = await self._write_heartbeat(guild, config, runtime, now)
        try:
            message, existing, duplicates = await self._find_own_message(
                channel,
                self._store.device_message_id(guild_id),
                devices.decode_record,
                self._store.installation_id,
            )
        except Exception as err:  # noqa: BLE001 - a read failure is not registration
            self._warn_device(guild_id, "registry-read", "Device registry read failed.",
                              {"guild": guild_id, "message": str(err)})
            return False
        if duplicates:
            self._warn_device(guild_id, "registry-duplicate",
                              "Duplicate device registry entries detected; using the newest.",
                              {"guild": guild_id, "count": duplicates})
        record = self._build_device_record(
            guild, category or self._resolved.get(guild_id), config, runtime,
            existing, now, heartbeat_ok,
        )
        success = await self._upsert_message(
            channel, message, devices.encode_record(record), guild_id,
            self._store.remember_device_message, "registry-write",
        )
        if success and announce:
            info("Device registered.", {
                "guild": guild_id,
                "category": record.get("categoryId"),
                "connected": heartbeat_ok,
            })
        return success

    async def register_device(self, guild, config=None, runtime=None, category=None) -> bool:
        """Register this installation immediately, with no approval step.

        Existing records are updated in place and keep their original
        registration time. A missing CONTROL #main or a failed Discord
        write returns False and is logged; it is never recorded as a
        successful registration.
        """
        return await self._sync_device(guild, config, runtime, category, announce=True)

    async def _refresh_device_presence(self, guild, config=None, runtime=None) -> bool:
        """Refresh the heartbeat and registry entry during the periodic pass."""
        return await self._sync_device(guild, config, runtime, None, announce=False)

    def status_facts(self, channel_id, config=None, runtime=None) -> dict | None:
        """Return status facts for one of this process's managed #main channels.

        Each process tracks only its own #main channels, so a guild with
        several managed categories gets a different status from each one.
        Returns None outside this process's #main channels. Read-only: it
        reads live counters and the clock, and changes nothing.
        """
        if channel_id not in self._main_channels:
            return None
        from astral_remote.agent.windows.info import get_uptime_seconds

        settings = config or {}
        link = runtime or {}
        return {
            "hostname": self._hostname,
            "installationId": self._store.installation_id,
            "agentVersion": settings.get("agentVersion"),
            "uptimeSeconds": get_uptime_seconds(),
            "connected": bool(link.get("connected")),
            "latencySeconds": link.get("latencySeconds"),
            "reconnects": link.get("reconnects"),
            "lastUpdated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

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
            # Preserve the tri-state value: True/False are confirmed states,
            # anything else is Unknown and must not be reported as Connected.
            "connected": link.get("connected"),
            "latencySeconds": link.get("latencySeconds"),
            "reconnects": link.get("reconnects"),
            "categoryStatus": self._category_status.get(guild_id),
            "channelCount": self._channel_counts.get(guild_id),
            "duplicateWarning": (
                "Duplicates reported; review the agent log"
                if guild_id in self._duplicate_guilds else None
            ),
            "uptimeSeconds": get_uptime_seconds(),
        }

    async def _publish_report(self, guild, channel, config, runtime, announce: bool = True) -> None:
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
        if announce:
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


def is_main_channel(channel_id) -> bool:
    """True when the channel is a managed host #main channel of this process."""
    manager = _default_manager
    return manager is not None and manager.is_main_channel(channel_id)


def is_control_main_channel(channel_id) -> bool:
    """True when the channel is the shared CONTROL #main of this process."""
    manager = _default_manager
    return manager is not None and manager.is_control_main_channel(channel_id)


async def devices_for_channel(channel_id) -> list[dict] | None:
    """Read the shared device registry for a CONTROL #main, or None.

    A Discord read failure propagates so the command handler can report an
    unavailable registry instead of claiming there are no devices.
    """
    manager = _default_manager
    if manager is None:
        return None
    return await manager.device_records(channel_id)


def commands_for_channel(channel_id) -> tuple[str, ...]:
    """Commands this process accepts in a managed channel (empty elsewhere)."""
    manager = _default_manager
    return manager.commands_for(channel_id) if manager is not None else ()


def status_for_channel(channel_id, config=None, runtime=None) -> dict | None:
    """Status facts for a managed #main channel's computer, or None."""
    manager = _default_manager
    return manager.status_facts(channel_id, config, runtime) if manager is not None else None


def _gateway_state(bot):
    """Return the confirmed Discord Gateway state: True, False, or None.

    A closed client or socket is a confirmed loss. A live socket with a
    completed READY state (or a usable heartbeat latency) is a confirmed
    connection. Anything else is unknown, never assumed Connected.
    """
    closed = getattr(bot, "is_closed", None)
    if callable(closed):
        try:
            if closed():
                return False
        except Exception:  # noqa: BLE001 - an unreadable state is not a confirmation
            return None
    ws = getattr(bot, "ws", None)
    socket = getattr(ws, "socket", None) if ws is not None else None
    if socket is None:
        return None
    if getattr(socket, "closed", False):
        return False
    ready = getattr(bot, "is_ready", None)
    if callable(ready):
        try:
            if ready():
                return True
        except Exception:  # noqa: BLE001 - fall through to the latency check
            return None
    latency = getattr(bot, "latency", None)
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        return None
    if latency != latency or latency < 0 or latency > 10.0:
        return None
    return True


def runtime_facts(bot) -> dict:
    """Read the bot's live link facts for the #info report.

    discord.py reports nan or infinity for latency before the first
    heartbeat, so an unusable value becomes Unavailable instead of a
    bogus number. Reconnect count is whatever the bot counted so far.
    Connection is the confirmed Gateway state, with None rendered as
    Unknown rather than Connected.
    """
    latency = getattr(bot, "latency", None)
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        latency = None
    elif latency != latency or latency < 0 or latency > 10.0:
        latency = None
    reconnects = getattr(bot, "_astral_reconnects", None)
    return {
        "connected": _gateway_state(bot),
        "latencySeconds": latency,
        "reconnects": reconnects if isinstance(reconnects, int) else None,
    }


async def ensure_host_categories(bot, hostname: str, host_id: str, manager: CategoryManager | None = None,
                                 config=None) -> int:
    """Ensure categories, channels, control setup, and device registration.

    One managed host category per guild is reused across reconnects, and
    one server-wide CONTROL category with #main is ensured beside it. Both
    run in parallel, so the control category appears while the host report
    is still being collected and published. Registration runs after both,
    because the shared record includes the host category outcome. Returns
    the guild count attempted. Never raises. An explicit manager may be
    passed for isolated callers and tests.
    """
    active = manager if manager is not None else default_manager(hostname, host_id)
    runtime = runtime_facts(bot)
    guilds = list(getattr(bot, "guilds", []) or [])
    for guild in guilds:
        # return_exceptions keeps one side's failure from cancelling the
        # other; both methods already warn instead of raising. Control is
        # started first so its category request goes out before the host
        # report begins collecting.
        control_result, host_result = await asyncio.gather(
            active.ensure_control(guild),
            active.ensure_guild(guild, config, runtime),
            return_exceptions=True,
        )
        category = None if isinstance(host_result, BaseException) else host_result
        try:
            await active.register_device(guild, config, runtime, category)
        except Exception as err:  # noqa: BLE001 - registration is best-effort
            _report("Device registration failed.", {"guild": getattr(guild, "id", None),
                                                    "message": str(err)})
    return len(guilds)


async def refresh_host_categories(bot, config=None, manager: CategoryManager | None = None) -> int:
    """Republish the #info report for every guild set up in this process.

    Called by the Agent's periodic refresh. Only guilds already resolved
    by ensure_host_categories are touched, so nothing is created here and
    a refused or broken setup is never retried. Returns the number of
    guilds refreshed. Never raises.
    """
    active = manager if manager is not None else _default_manager
    if active is None:
        return 0
    runtime = runtime_facts(bot)
    refreshed = 0
    for guild in list(getattr(bot, "guilds", []) or []):
        try:
            if await active.refresh(guild, config, runtime):
                refreshed += 1
        except Exception:
            continue
    return refreshed
