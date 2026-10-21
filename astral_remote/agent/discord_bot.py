# Discord client wiring: login, category/#info maintenance, shutdown.
# Production agent entry calls start_bot() after the token is available.
# The agent exposes no commands: no prefix commands, no slash commands,
# no CommandTree, and no application-command registration or
# synchronization. Its only Discord work is to create or adopt the
# managed per-computer category, publish the Components V2 system report
# into the managed #info channel, and keep that channel clean.
# on_message deletes member messages posted in a managed #info channel so
# the report stays readable; bot messages and every other channel are
# ignored. on_resumed counts gateway session resumes for the #info agent
# section. Replies are never sent, so no reply text can leak system
# details.

from __future__ import annotations

from astral_remote.shared.logging_utils import error, info


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
        info("Discord client ready.")

    @client.event
    async def on_resumed() -> None:
        """Count gateway session resumes for the #info agent section."""
        client._astral_reconnects = int(getattr(client, "_astral_reconnects", 0)) + 1

    @client.event
    async def on_message(message) -> None:
        """Delete member chatter in managed #info channels.

        The information channel is bot-owned, so member messages sent
        there are removed immediately. If deletion is not permitted the
        message is left in place. Bot messages and every other channel
        are ignored: the agent registers no commands.
        """
        from astral_remote.agent.discord_setup import is_info_channel

        if getattr(getattr(message, "author", None), "bot", False):
            return
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        try:
            info_channel = is_info_channel(channel_id)
        except Exception:
            info_channel = False
        if not info_channel:
            return
        try:
            await message.delete()
        except Exception:
            pass

    return client


async def start_bot(config: dict, token: str):
    """Log in and run until interrupted. Caller owns shutdown signaling."""
    client = build_bot(config)
    info("Logging in to Discord.")
    await client.start(token.strip())
    return client
