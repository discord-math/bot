from typing import Awaitable, Optional, Protocol, cast

import discord
from discord import Guild, Message, MessageType

from bot.client import client
from bot.cogs import Cog, cog
import plugins
import util.db.kv

class KeepvanityConf(Awaitable[None], Protocol):
    guild: int
    vanity: Optional[str]

conf: KeepvanityConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(KeepvanityConf, await util.db.kv.load(__name__))

    conf.guild = int(conf.guild)
    await conf

async def check_guild_vanity(guild: Guild) -> None:
    if guild.id != conf.guild:
        return
    try:
        await guild.vanity_invite()
    except discord.NotFound:
        if conf.vanity is not None:
            await guild.edit(vanity_code=conf.vanity)

@cog
class KeepVanity(Cog):
    """Restores the guild vanity URL as soon as enough boosts are available"""
    @Cog.listener()
    async def on_ready(self) -> None:
        for guild in client.guilds:
            await check_guild_vanity(guild)

    @Cog.listener()
    async def on_message(self, msg: Message) -> None:
        if msg.type != MessageType.premium_guild_tier_3:
            return
        if msg.guild is None:
            return
        await check_guild_vanity(msg.guild)

@plugins.init
async def init_check_task() -> None:
    for guild in client.guilds:
        try:
            await check_guild_vanity(guild)
        except (discord.NotFound, discord.Forbidden):
            pass
