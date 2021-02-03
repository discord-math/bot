import discord
import asyncio
import static_config
import logging

logger = logging.getLogger(__name__)

client = discord.Client(
    loop=asyncio.get_event_loop(),
    max_messages=None,
    intents=discord.Intents.all())

async def main_task():
    try:
        await client.login(static_config.Discord["token"])
        await client.connect(reconnect=True)
    except:
        logger.critical("Exception in main Discord task", exc_info=True)
        client.loop.stop()
