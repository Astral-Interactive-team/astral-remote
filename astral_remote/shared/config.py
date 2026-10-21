# Non-secret agent configuration schema and validation.
# No configuration files exist: the builder embeds default values as the
# EXE payload and the agent reads the embedded module only. The bot token
# is never part of this schema and lives only in process memory.
# Ported from src/shared/config.js; validation messages kept identical.

from __future__ import annotations

from typing import Any

from astral_remote import AGENT_VERSION
from astral_remote.shared.errors import ConfigError

DEFAULT_STARTUP_NAME = "AstralRemote"

_LEGACY_KEYS = ("guildId", "applicationId", "authorizedUserIds", "allowedApps")


def default_runtime_config(install_to_appdata: bool = True, enable_autostart: bool = True,
                           startup_name: str = DEFAULT_STARTUP_NAME) -> dict[str, Any]:
    """Return the default non-secret runtime config embedded by the builder.

    `install_to_appdata` and `enable_autostart` are the build-time choices
    for the EXE's self-install behavior; both default to enabled.
    `startup_name` names the `%APPDATA%` subfolder and the autostart
    registry value and logon task.
    """
    name = str(startup_name).strip() or DEFAULT_STARTUP_NAME
    return {
        "agentVersion": AGENT_VERSION,
        "installToAppData": bool(install_to_appdata),
        "enableAutostart": bool(enable_autostart),
        "startupName": name,
    }


def validate_config(raw: Any) -> list[str]:
    """Validate a raw object. Empty list means valid. Never throws."""
    problems: list[str] = []
    if raw is None or not isinstance(raw, dict) or isinstance(raw, list):
        return ["Configuration must be a JSON object."]
    if not isinstance(raw.get("agentVersion"), str) or raw.get("agentVersion") == "":
        problems.append("agentVersion must be a non-empty string.")
    for flag in ("installToAppData", "enableAutostart"):
        if flag in raw and not isinstance(raw[flag], bool):
            problems.append(f"{flag} must be true or false.")
    if "startupName" in raw and (not isinstance(raw["startupName"], str) or raw["startupName"].strip() == ""):
        problems.append("startupName must be a non-empty string.")
    for legacy in _LEGACY_KEYS:
        if legacy in raw:
            problems.append(f"{legacy} was removed. Rebuild the EXE; no configuration file is used.")
    if "botToken" in raw or "token" in raw:
        problems.append("Configuration must never hold a token. Enter it at startup; it lives in memory only.")
    return problems


def require_valid_config(raw: Any) -> Any:
    """Throw ConfigError naming the first problem. Used by builder and agent."""
    problems = validate_config(raw)
    if problems:
        raise ConfigError(problems[0])
    return raw
