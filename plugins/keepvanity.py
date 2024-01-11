from typing import TYPE_CHECKING

import discord
from discord import Guild, Message, MessageType
from discord.ext.commands import group
from sqlalchemy import TEXT, BigInteger, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column

from bot.client import client
from bot.cogs import Cog, cog
from bot.commands import Context
from bot.config import plugin_config_command
import plugins
import util.db.kv
from util.discord import PartialGuildConverter, format

registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine)

@registry.mapped
class GuildConfig:
    __tablename__ = "keep_vanity"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vanity: Mapped[str] = mapped_column(TEXT, nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, guild_id: int, vanity: str) -> None: ...

@plugins.init
async def init() -> None:
    await util.db.init(util.db.get_ddl(registry.metadata.create_all))

    async with sessionmaker() as session:
        conf = await util.db.kv.load(__name__)
        if isinstance(guild_id := conf.guild_id, int):
            if isinstance(vanity := conf.vanity, str):
                session.add(GuildConfig(guild_id=guild_id, vanity=vanity))
                conf.guild_id = None
                conf.vanity = None
                await session.commit()
                await conf

        for guild in client.guilds:
            await check_guild_vanity(session, guild)

async def check_guild_vanity(session: AsyncSession, guild: Guild) -> None:
    if conf := await session.get(GuildConfig, guild.id):
        try:
            if await guild.vanity_invite() is not None:
                return
        except discord.Forbidden:
            return
        except discord.NotFound:
            pass
        await guild.edit(vanity_code=conf.vanity)

@cog
class KeepVanity(Cog):
    """Restores the guild vanity URL as soon as enough boosts are available"""
    @Cog.listener()
    async def on_ready(self) -> None:
        async with sessionmaker() as session:
            for guild in client.guilds:
                await check_guild_vanity(session, guild)

    @Cog.listener()
    async def on_message(self, msg: Message) -> None:
        if msg.type != MessageType.premium_guild_tier_3:
            return
        if msg.guild is None:
            return
        async with sessionmaker() as session:
            await check_guild_vanity(session, msg.guild)

@plugin_config_command
@group("keepvanity", invoke_without_command=True)
async def config(ctx: Context) -> None:
    async with sessionmaker() as session:
        stmt = select(GuildConfig)
        await ctx.send("\n".join(format("- {!c}: {!i}", conf.guild_id, conf.vanity)
            for conf in (await session.execute(stmt)).scalars())
            or "No servers registered")

@config.command("add")
async def config_add(ctx: Context, server: PartialGuildConverter, vanity: str) -> None:
    async with sessionmaker() as session:
        session.add(GuildConfig(guild_id=server.id, vanity=vanity))
        await session.commit()
        await ctx.send("\u2705")

@config.command("remove")
async def config_remove(ctx: Context, server: PartialGuildConverter) -> None:
    async with sessionmaker() as session:
        await session.delete(await session.get(GuildConfig, server.id))
        await session.commit()
        await ctx.send("\u2705")
