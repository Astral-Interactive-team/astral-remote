# Guard test: the agent exposes only the read-only `status` command.
# The !pc command group and everything that existed only to serve it were
# removed: the destructive command handlers, the authorization gate, the
# Start Menu application catalog and launcher, the session lock helper, and
# the enabledCommands/requiredPermission configuration keys. This test
# fails loudly if any of that surface is reintroduced. The one command that
# exists is `status` in managed #main channels; it is read-only and uses no
# discord.py command framework.

import importlib.util

from astral_remote.shared.config import default_runtime_config, validate_config


def _spec_or_none(name: str):
    """Return the module spec, or None when the module is not importable."""
    try:
        return importlib.util.find_spec(name)
    except ModuleNotFoundError:
        return None


def test_removed_command_modules_are_not_importable():
    for name in (
        "astral_remote.agent.commands",
        "astral_remote.agent.commands.status_cmd",
        "astral_remote.agent.commands.apps_cmd",
        "astral_remote.agent.commands.open_cmd",
        "astral_remote.agent.commands.lock_cmd",
        "astral_remote.agent.security",
        "astral_remote.agent.security.authorize",
        "astral_remote.agent.windows.apps",
        "astral_remote.agent.windows.lock",
    ):
        assert _spec_or_none(name) is None, name


def test_bot_registers_no_prefix_command_machinery():
    from pathlib import Path

    from astral_remote.agent import discord_bot

    text = Path(discord_bot.__file__).read_text(encoding="utf-8")
    for forbidden in ("command_prefix", "process_commands", "add_command", "on_command_error", "CommandTree"):
        assert forbidden not in text, forbidden
    assert "parse_command" in text


def test_only_status_is_available_and_only_in_main():
    from astral_remote.agent.discord_setup import INFO_CHANNEL_COMMANDS, MAIN_CHANNEL_COMMANDS

    assert MAIN_CHANNEL_COMMANDS == ("status",)
    assert INFO_CHANNEL_COMMANDS == ()


def test_config_schema_has_no_command_or_permission_fields():
    defaults = default_runtime_config()
    assert "enabledCommands" not in defaults
    assert "requiredPermission" not in defaults
    assert validate_config(defaults) == []
