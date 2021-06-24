import asyncio
import discord
from typing import Optional, Protocol
import discord_client
import util.discord
import util.db.kv
import util.asyncio

class KeepvanityConf(Protocol):
    guild: str
    vanity: Optional[str]

conf = util.db.kv.Config(__name__)

async def check_guild_vanity(guild: discord.Guild) -> None:
    try:
        if guild.id != int(conf.guild or 0):
            return
    except ValueError:
        return
    try:
        await guild.vanity_invite()
    except discord.NotFound:
        if conf.vanity is not None:
            await guild.edit(vanity_code=conf.vanity)

@util.discord.event("message")
async def boost_message(msg: discord.Message) -> None:
    if msg.type != discord.MessageType.premium_guild_tier_3:
        return
    if msg.guild is None:
        return
    await check_guild_vanity(msg.guild)

@util.discord.event("ready")
async def check_after_connect() -> None:
    for guild in discord_client.client.guilds:
        await check_guild_vanity(guild)

@util.asyncio.init_async
async def init_check_task() -> None:
    for guild in discord_client.client.guilds:
        await check_guild_vanity(guild)
