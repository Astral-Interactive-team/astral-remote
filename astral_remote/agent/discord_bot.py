# Discord client wiring: login, category/channel maintenance, commands, shutdown.
# Production agent entry calls start_bot() after the token is available.
# There is no slash command surface, no command tree, and no
# application-command registration or synchronization. Discord work is to
# create or adopt the managed per-computer category, publish the
# Components V2 system report into the managed #info channel, keep that
# channel clean, and answer the read-only `status` prefix command in the
# managed #main channel with a plain code-block message.
# #main is ensured directly below #info as the operator command channel,
# so its messages are never deleted and the report is never published
# there. on_message deletes member messages posted in a managed #info
# channel so the report stays readable, and answers `status` in this
# process's #main channels with the facts of that channel's category.
# Bot messages and every other channel are ignored. on_resumed counts
# gateway session resumes for the #info agent section. A background task
# republishes the #info report every minute while the client is connected.
# Command replies echo no user input, so no system detail leaks.

from __future__ import annotations

import asyncio

from astral_remote.shared.logging_utils import error, info, warn

# How often the #info report is refreshed while the Agent stays connected.
REFRESH_INTERVAL_SECONDS = 60


def _start_refresh(client, config: dict) -> None:
    """Start the periodic #info refresh, once per client.

    on_ready fires again after a full re-identify, so an already running
    task is reused instead of stacking refresh loops.
    """
    task = getattr(client, "_astral_refresh_task", None)
    if task is not None and not task.done():
        return
    client._astral_refresh_task = asyncio.create_task(_refresh_loop(client, config))


async def _refresh_loop(client, config: dict) -> None:
    """Republish the #info report every REFRESH_INTERVAL_SECONDS.

    The first refresh waits one interval, because on_ready already
    published a report during setup. A failed pass never stops the loop.
    """
    from astral_remote.agent.discord_setup import refresh_host_categories

    while not client.is_closed():
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            await refresh_host_categories(client, config=config)
        except Exception:
            pass


def build_bot(config: dict):
    """Create the Discord client. discord.py imports stay local to this function."""
    import discord

    intents = discord.Intents.default()
    intents.guilds = True
    # Message events are still received without this intent, but keeping
    # it explicit preserves the ability to inspect member messages in
    # managed #info channels on every supported discord.py version.
    intents.message_content = True

    client = discord.Client(intents=intents)
    # Gateway session resumes are counted here so the #info report can
    # show a real reconnect count instead of a placeholder.
    client._astral_reconnects = 0

    @client.event
    async def on_ready() -> None:
        content = getattr(client.intents, "message_content", False)
        if not content:
            error("Message Content Intent unavailable; member messages in #info cannot be read. "
                  "Enable it in code and in the Developer Portal.")
        from astral_remote.agent.discord_setup import ensure_host_categories
        from astral_remote.agent.windows.info import get_hostname, get_machine_id

        try:
            await ensure_host_categories(client, get_hostname(), get_machine_id(), config=config)
        except Exception:
            pass
        _start_refresh(client, config)
        info("Discord client ready.")

    @client.event
    async def on_resumed() -> None:
        """Count gateway session resumes for the #info agent section."""
        client._astral_reconnects = int(getattr(client, "_astral_reconnects", 0)) + 1

    @client.event
    async def on_message(message) -> None:
        """Clean managed #info and answer commands in managed #main.

        #info is bot-owned, so member messages there are removed immediately
        (when permitted). Managed #main channels accept the commands this
        build allows (`status`) and answer with a plain code-block message
        for that channel's own category. The prefix comes from the
        build-time config; unknown commands, bot messages, and every other
        channel are ignored.
        """
        from astral_remote.agent.channel_commands import format_status, parse_command
        from astral_remote.agent.discord_setup import (
            commands_for_channel,
            is_info_channel,
            runtime_facts,
            status_for_channel,
        )
        from astral_remote.shared.config import normalize_prefix

        if getattr(getattr(message, "author", None), "bot", False):
            return
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        try:
            info_channel = is_info_channel(channel_id)
            commands = commands_for_channel(channel_id)
        except Exception:
            return
        if info_channel:
            try:
                await message.delete()
            except Exception:
                pass
            return
        if not commands:
            return
        prefix = normalize_prefix((config or {}).get("prefix"))
        command = parse_command(getattr(message, "content", None), prefix)
        if command not in commands:
            return
        if command != "status":
            return
        try:
            facts = status_for_channel(channel_id, config, runtime_facts(client))
            if facts is None:
                return
            await message.channel.send(format_status(facts))
        except Exception as err:  # noqa: BLE001 - a reply must never break the client
            warn("Status command failed.", {"channel": channel_id, "message": str(err)})

    return client


async def start_bot(config: dict, token: str):
    """Log in and run until interrupted. Caller owns shutdown signaling."""
    client = build_bot(config)
    info("Logging in to Discord.")
    try:
        await client.start(token.strip())
    finally:
        task = getattr(client, "_astral_refresh_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    return client
