"""
This module defines the "client" singleton. Reloading the module restarts the connection to discord.
"""

import asyncio
import logging
from typing import Any

import discord
import discord.ext.commands

import bot.main_tasks
import plugins
import static_config

logger = logging.getLogger(__name__)

intents = discord.Intents.all()
intents.presences = False
client = discord.ext.commands.Bot(
    command_prefix=(),
    loop=asyncio.get_event_loop(),
    max_messages=None,
    intents=intents,
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))

# Disable command functionality until reenabled again in plugins.commands
@client.event
async def on_message(*args: Any, **kwargs: Any) -> None:
    pass

@client.event
async def on_error(event: str, *args: Any, **kwargs: Any) -> None:
    logger.error("Uncaught exception in {}".format(event), exc_info=True)

async def main_task() -> None:
    try:
        async with client:
            await client.start(static_config.Discord["token"], reconnect=True)
    except:
        logger.critical("Exception in main Discord task", exc_info=True)
    finally:
        await client.close()

@plugins.init
def init() -> None:
    task = bot.main_tasks.create_task(main_task(), name="Discord client")
    plugins.finalizer(task.cancel)
