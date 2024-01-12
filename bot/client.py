"""
This module defines the "client" singleton. Reloading the module restarts the connection to discord.
"""

import logging

from discord import AllowedMentions, Intents
from discord.ext.commands import Bot

import bot.main_tasks
import plugins
import static_config


logger = logging.getLogger(__name__)

intents = Intents.all()
intents.presences = False
client = Bot(
    command_prefix=(), max_messages=None, intents=intents, allowed_mentions=AllowedMentions(everyone=False, roles=False)
)


# Disable command functionality until reenabled again in bot.commands
@client.event
async def on_message(*args: object, **kwargs: object) -> None:
    pass


@client.event
async def on_error(event: str, *args: object, **kwargs: object) -> None:
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
