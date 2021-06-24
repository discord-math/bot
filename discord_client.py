"""
This module defines the "client" singleton. It really should be a singleton so it should never be re-created.
"""

import discord
import asyncio
import static_config
import logging

logger: logging.Logger = logging.getLogger(__name__)

try:
    client
    logger.warn("Refusing to re-create the Discord client", stack_info=True)
except NameError:
    client: discord.Client = discord.Client(
        loop=asyncio.get_event_loop(),
        max_messages=None,
        intents=discord.Intents.all(),
        allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))

async def main_task() -> None:
    try:
        await client.login(static_config.Discord["token"])
        await client.connect(reconnect=True)
    except:
        logger.critical("Exception in main Discord task", exc_info=True)
        client.loop.stop()
