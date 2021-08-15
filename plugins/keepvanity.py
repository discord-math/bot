import asyncio
import discord
import discord.ext.commands
import discord.ext.typed_commands
from typing import Optional, Awaitable, Protocol, cast
import discord_client
import plugins
import plugins.cogs
import util.discord
import util.db.kv
import util.asyncio

class KeepvanityConf(Protocol, Awaitable[None]):
    guild: int
    vanity: Optional[str]

conf: KeepvanityConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(KeepvanityConf, await util.db.kv.load(__name__))

    conf.guild = int(conf.guild)
    await conf

async def check_guild_vanity(guild: discord.Guild) -> None:
    if guild.id != conf.guild:
        return
    try:
        await guild.vanity_invite()
    except discord.NotFound:
        if conf.vanity is not None:
            await guild.edit(vanity_code=conf.vanity)

@plugins.cogs.cog
class KeepVanity(discord.ext.typed_commands.Cog[discord.ext.commands.Context]):
    """Restores the guild vanity URL as soon as enough boosts are available"""
    @discord.ext.commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in discord_client.client.guilds:
            await check_guild_vanity(guild)

    @discord.ext.commands.Cog.listener()
    async def on_message(self, msg: discord.Message) -> None:
        if msg.type != discord.MessageType.premium_guild_tier_3:
            return
        if msg.guild is None:
            return
        await check_guild_vanity(msg.guild)

@plugins.init
async def init_check_task() -> None:
    for guild in discord_client.client.guilds:
        try:
            await check_guild_vanity(guild)
        except (discord.NotFound, discord.Forbidden):
            pass
