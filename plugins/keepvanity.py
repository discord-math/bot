import asyncio
import discord
import discord_client
import util.discord
import util.db.kv
import util.asyncio

conf = util.db.kv.Config(__name__)

async def check_guild_vanity(guild):
    try:
        if guild.id != int(conf.guild or 0):
            return
    except ValueError:
        return
    try:
        await guild.vanity_invite()
    except discord.NotFound:
        if conf.vanity:
            await guild.edit(vanity_code=conf.vanity)

@util.discord.event("message")
async def boost_message(msg):
    if msg.type != discord.MessageType.premium_guild_tier_3:
        return
    await check_guild_vanity(msg.guild)

@util.discord.event("ready")
async def check_after_connect():
    for guild in discord_client.client.guilds:
        await check_guild_vanity(guild)

@util.asyncio.init_async
async def init_check_task():
    for guild in discord_client.client.guilds:
        await check_guild_vanity(guild)
