# Persistent, non-secret Agent installation identity and per-guild state.
# A random installation ID is generated once and stored in
# %LOCALAPPDATA%\AstralRemote\identity.json so the same installation keeps
# the same identity across restarts. The file also remembers the Discord
# category this installation manages in each guild (plus the managed #info
# channel, the managed #main channel, the ordered IDs of the Components V2
# report messages posted in #info, and the reusable device registration and
# heartbeat message IDs), and the server-wide control category with its
# #main channel, so a fresh process can refresh the exact objects instead
# of searching by display name alone. The legacy control #logs channel ID
# is retained when present but is no longer created.
# On first load the pre-rename %LOCALAPPDATA%\AstralRelay\identity.json is
# copied into the new directory when no new file exists yet, so an upgrade
# keeps its installation ID and does not post a duplicate category.
# The file holds no bot token and no secrets. All reads and writes are
# best-effort; callers never receive an exception from this module.

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from astral_remote.shared.paths import legacy_state_dir, state_dir

STATE_FILENAME = "identity.json"
STATE_VERSION = 1
INSTALL_ID_BYTES = 8


def state_file(env=None) -> Path | None:
    """Resolve the absolute path of the identity/state JSON file."""
    directory = state_dir(env)
    return directory / STATE_FILENAME if directory is not None else None


def _adopt_legacy_state(target: Path, env=None) -> None:
    """Copy a pre-rename identity file into the new state directory once.

    The project rename moved the per-user state directory, so an installation
    upgraded from the old build would otherwise lose its installation ID and
    its managed category/channel/message IDs and create a duplicate category.
    The legacy file is left untouched, and every failure is ignored like the
    rest of the state handling so a read-only profile never breaks the Agent.
    """
    if target.exists():
        return
    legacy_directory = legacy_state_dir(env)
    if legacy_directory is None:
        return
    legacy = legacy_directory / STATE_FILENAME
    if not legacy.is_file():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(legacy.read_bytes())
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except OSError:
        pass



def _empty_state() -> dict:
    return {"version": STATE_VERSION, "installationId": "", "guilds": {}}


def _normalize(raw) -> dict:
    """Return a safe state dict, discarding malformed fields instead of raising."""
    state = _empty_state()
    if not isinstance(raw, dict):
        return state
    install = raw.get("installationId")
    if isinstance(install, str):
        state["installationId"] = install.strip()
    guilds = raw.get("guilds")
    if isinstance(guilds, dict):
        clean: dict = {}
        for key, value in guilds.items():
            if not isinstance(value, dict):
                continue
            category_id = value.get("categoryId")
            control_category_id = value.get("controlCategoryId")
            has_category = isinstance(category_id, int) and not isinstance(category_id, bool)
            has_control_category = isinstance(control_category_id, int) and not isinstance(control_category_id, bool)
            if not has_category and not has_control_category:
                continue
            name = value.get("name") if isinstance(value.get("name"), str) else ""
            record = {"name": name}
            if has_category:
                record["categoryId"] = category_id
            for extra in (
                "infoChannelId",
                "mainChannelId",
                "infoMessageId",
                "controlCategoryId",
                "controlMainChannelId",
                "controlLogsChannelId",
                "deviceMessageId",
                "heartbeatMessageId",
            ):
                number = value.get(extra)
                if isinstance(number, int) and not isinstance(number, bool):
                    record[extra] = number
            messages = value.get("infoMessages")
            if isinstance(messages, list):
                ids = [item for item in messages if isinstance(item, int) and not isinstance(item, bool)]
                if ids:
                    record["infoMessages"] = ids
            clean[str(key)] = record
        state["guilds"] = clean
    return state


def load_state(path=None, env=None) -> dict:
    """Load persisted state, or a fresh empty state when missing or corrupt."""
    explicit = path is not None
    target = Path(path) if explicit else state_file(env)
    if target is None:
        return _empty_state()
    if not explicit:
        _adopt_legacy_state(target, env)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    return _normalize(raw)


def save_state(state, path=None, env=None) -> bool:
    """Persist state atomically. Returns True on success. Never raises."""
    target = Path(path) if path is not None else state_file(env)
    if target is None:
        return False
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(_normalize(state), ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


class StateStore:
    """In-memory view of the persisted state with write-through saves.

    Generates the installation ID lazily on first use and remembers one
    managed host category plus the managed #info and #main channels and the
    report message IDs per guild. It also remembers the server-wide control
    category and its #main and #logs channels. Reads and writes are
    fault-tolerant, so a missing or unwritable state file never breaks the
    agent.
    """

    def __init__(self, path=None, env=None):
        self._path = Path(path) if path is not None else None
        self._env = env
        self._state = load_state(self._path, env)

    @property
    def installation_id(self) -> str:
        """Return the stable installation ID, generating and saving it once."""
        current = self._state.get("installationId") or ""
        if current:
            return current
        generated = secrets.token_hex(INSTALL_ID_BYTES)
        self._state["installationId"] = generated
        self.save()
        return generated

    def _record(self, guild_id) -> dict | None:
        record = self._state.get("guilds", {}).get(str(guild_id))
        return record if isinstance(record, dict) else None

    def category_id(self, guild_id) -> int | None:
        """Return the remembered category ID for a guild, or None."""
        record = self._record(guild_id)
        return record.get("categoryId") if record else None

    def info_channel_id(self, guild_id) -> int | None:
        """Return the remembered managed #info channel ID for a guild, or None."""
        record = self._record(guild_id)
        return record.get("infoChannelId") if record else None

    def main_channel_id(self, guild_id) -> int | None:
        """Return the remembered managed #main channel ID for a guild, or None."""
        record = self._record(guild_id)
        return record.get("mainChannelId") if record else None

    def control_category_id(self, guild_id) -> int | None:
        """Return the remembered server-wide control category ID, or None."""
        record = self._record(guild_id)
        return record.get("controlCategoryId") if record else None

    def control_main_channel_id(self, guild_id) -> int | None:
        """Return the remembered control #main channel ID, or None."""
        record = self._record(guild_id)
        return record.get("controlMainChannelId") if record else None

    def control_logs_channel_id(self, guild_id) -> int | None:
        """Return the remembered control #logs channel ID, or None.

        Older builds created #logs. The field is read only so that an
        upgrade can preserve the existing channel ID without creating a
        new logging channel of its own.
        """
        record = self._record(guild_id)
        return record.get("controlLogsChannelId") if record else None

    def device_message_id(self, guild_id) -> int | None:
        """Return the remembered shared device-registration message ID."""
        record = self._record(guild_id)
        return record.get("deviceMessageId") if record else None

    def heartbeat_message_id(self, guild_id) -> int | None:
        """Return the remembered per-computer heartbeat message ID."""
        record = self._record(guild_id)
        return record.get("heartbeatMessageId") if record else None

    def info_message_id(self, guild_id) -> int | None:
        """Return the single legacy report message ID for a guild, or None."""
        record = self._record(guild_id)
        return record.get("infoMessageId") if record else None

    def info_messages(self, guild_id) -> list[int]:
        """Return the tracked report message IDs for a guild, oldest first.

        Falls back to the single message ID written by earlier builds so
        an existing #info panel is refreshed in place instead of being
        duplicated after an upgrade.
        """
        record = self._record(guild_id)
        if not record:
            return []
        stored = record.get("infoMessages")
        if isinstance(stored, list) and stored:
            return [int(item) for item in stored]
        legacy = record.get("infoMessageId")
        if isinstance(legacy, int) and not isinstance(legacy, bool):
            return [int(legacy)]
        return []

    def _guild_record(self, guild_id) -> dict:
        """Return the mutable per-guild record, creating it when absent."""
        record = self._state.setdefault("guilds", {}).get(str(guild_id))
        if not isinstance(record, dict):
            record = {}
            self._state["guilds"][str(guild_id)] = record
        return record

    def remember(self, guild_id, category_id, name: str) -> None:
        """Persist the managed category for a guild, keeping other fields."""
        record = self._guild_record(guild_id)
        record["categoryId"] = int(category_id)
        record["name"] = str(name)
        self.save()

    def remember_info(self, guild_id, channel_id=None, message_id=None) -> None:
        """Persist the managed #info channel and message IDs for a guild."""
        record = self._guild_record(guild_id)
        if channel_id is not None:
            record["infoChannelId"] = int(channel_id)
        if message_id is not None:
            record["infoMessageId"] = int(message_id)
        self.save()

    def remember_main(self, guild_id, channel_id=None) -> None:
        """Persist the managed #main channel ID for a guild."""
        record = self._guild_record(guild_id)
        if channel_id is not None:
            record["mainChannelId"] = int(channel_id)
        self.save()

    def remember_control(self, guild_id, category_id=None, main_channel_id=None,
                         logs_channel_id=None) -> None:
        """Persist the server-wide control category and channel IDs."""
        record = self._guild_record(guild_id)
        if category_id is not None:
            record["controlCategoryId"] = int(category_id)
        if main_channel_id is not None:
            record["controlMainChannelId"] = int(main_channel_id)
        if logs_channel_id is not None:
            record["controlLogsChannelId"] = int(logs_channel_id)
        self.save()

    def remember_info_messages(self, guild_id, message_ids) -> None:
        """Persist the ordered report message IDs, replacing earlier shapes.

        The legacy single-message field is dropped so the next read uses
        the full ordered list exclusively.
        """
        record = self._guild_record(guild_id)
        ids = [
            int(item)
            for item in (message_ids or [])
            if isinstance(item, int) and not isinstance(item, bool)
        ]
        if ids:
            record["infoMessages"] = ids
        else:
            record.pop("infoMessages", None)
        record.pop("infoMessageId", None)
        self.save()

    def remember_device_message(self, guild_id, message_id=None) -> None:
        """Persist the reusable shared device-registration message ID."""
        record = self._guild_record(guild_id)
        if message_id is not None:
            record["deviceMessageId"] = int(message_id)
        self.save()

    def remember_heartbeat_message(self, guild_id, message_id=None) -> None:
        """Persist the reusable per-computer heartbeat message ID."""
        record = self._guild_record(guild_id)
        if message_id is not None:
            record["heartbeatMessageId"] = int(message_id)
        self.save()

    def save(self) -> bool:
        return save_state(self._state, self._path, self._env)
