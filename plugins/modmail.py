import asyncio
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Dict, Optional

import discord
from discord import (
    Activity,
    ActivityType,
    AllowedMentions,
    Client,
    DMChannel,
    Intents,
    Message,
    MessageReference,
    TextChannel,
    Thread,
)
from discord.ext.commands import group
from sqlalchemy import TEXT, TIMESTAMP, BigInteger, select, update
from sqlalchemy.dialects.postgresql import INTERVAL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema
from sqlalchemy.sql.functions import current_timestamp

import bot.client
from bot.acl import privileged
from bot.cogs import Cog, cog
from bot.commands import Context
from bot.config import plugin_config_command
from bot.reactions import get_reaction
import plugins
import util.db
from util.discord import (
    DurationConverter,
    PartialChannelConverter,
    PartialGuildConverter,
    PartialRoleConverter,
    PlainItem,
    UserError,
    chunk_messages,
    format,
    retry,
)


registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)


@registry.mapped
class ModmailMessage:
    __tablename__ = "messages"
    __table_args__ = {"schema": "modmail"}

    dm_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dm_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    staff_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    if TYPE_CHECKING:

        def __init__(self, *, dm_channel_id: int, dm_message_id: int, staff_message_id: int) -> None: ...


@registry.mapped
class ModmailThread:
    __tablename__ = "threads"
    __table_args__ = {"schema": "modmail"}

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_first_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_used: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)

    if TYPE_CHECKING:

        def __init__(self, *, user_id: int, thread_first_message_id: int, last_used: datetime) -> None: ...


@registry.mapped
class GuildConfig:
    __tablename__ = "guilds"
    __table_args__ = {"schema": "modmail"}

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token: Mapped[str] = mapped_column(TEXT, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_expiry: Mapped[timedelta] = mapped_column(INTERVAL, nullable=False)

    if TYPE_CHECKING:

        def __init__(
            self, *, guild_id: int, token: str, channel_id: int, role_id: int, thread_expiry: timedelta
        ) -> None: ...


logger: logging.Logger = logging.getLogger(__name__)

message_map: Dict[int, ModmailMessage] = {}


@plugins.init
async def init() -> None:
    await util.db.init(util.db.get_ddl(CreateSchema("modmail"), registry.metadata.create_all))

    async with sessionmaker() as session:
        for msg in (await session.execute(select(ModmailMessage))).scalars():
            message_map[msg.staff_message_id] = msg


async def add_modmail(source: Message, copy: Message) -> None:
    async with sessionmaker() as session:
        msg = ModmailMessage(dm_channel_id=source.channel.id, dm_message_id=source.id, staff_message_id=copy.id)
        session.add(msg)
        await session.commit()
        message_map[msg.staff_message_id] = msg


async def update_thread(conf: GuildConfig, user_id: int) -> Optional[int]:
    async with sessionmaker() as session:
        stmt = (
            update(ModmailThread)
            .returning(ModmailThread.thread_first_message_id)
            .where(ModmailThread.user_id == user_id, ModmailThread.last_used > current_timestamp() - conf.thread_expiry)
            .values(last_used=current_timestamp())
            .execution_options(synchronize_session=False)
        )
        thread = (await session.execute(stmt)).scalars().first()
        await session.commit()
        return thread


async def create_thread(user_id: int, msg_id: int) -> None:
    async with sessionmaker() as session:
        session.add(
            ModmailThread(
                user_id=user_id, thread_first_message_id=msg_id, last_used=current_timestamp()  # type: ignore
            )
        )
        await session.commit()


class ModMailClient(Client):
    conf: GuildConfig

    async def on_ready(self) -> None:
        await self.change_presence(activity=Activity(type=ActivityType.watching, name="DMs"))

    async def on_error(self, event_method: str, *args: object, **kwargs: object) -> None:
        logger.error("Exception in modmail client {}".format(event_method), exc_info=True)

    async def on_message(self, msg: Message) -> None:
        if not msg.guild and self.user is not None and msg.author.id != self.user.id:
            try:
                guild = bot.client.client.get_guild(self.conf.guild_id)
                if guild is None:
                    return
                channel = guild.get_channel(self.conf.channel_id)
                if not isinstance(channel, (TextChannel, Thread)):
                    return
                role = guild.get_role(self.conf.role_id)
                if role is None:
                    return
            except (ValueError, AttributeError):
                return
            thread_id = await update_thread(self.conf, msg.author.id)

            items = [PlainItem(msg.content)]

            footer = "".join("\n**Attachment:** {} {}".format(att.filename, att.url) for att in msg.attachments)
            if thread_id is None:
                footer += format("\n{!m}", role)
            if footer:
                items.append(PlainItem("\n" + footer))

            mentions = AllowedMentions.none()
            mentions.roles = [role]
            reference = None
            if thread_id is not None:
                reference = MessageReference(message_id=thread_id, channel_id=channel.id, fail_if_not_exists=False)

            embed = (
                discord.Embed(
                    title=format("Modmail from {}#{}", msg.author.name, msg.author.discriminator),
                    timestamp=msg.created_at,
                )
                .add_field(name="From", value=format("{!m}", msg.author))
                .add_field(name="ID", value=msg.author.id)
            )
            if reference is not None:
                header = await retry(
                    lambda: channel.send(embed=embed, allowed_mentions=mentions, reference=reference), attempts=10
                )
            else:
                header = await retry(lambda: channel.send(embed=embed, allowed_mentions=mentions), attempts=10)
            await add_modmail(msg, header)

            for content, _ in chunk_messages(items):
                copy = await retry(lambda: channel.send(content, allowed_mentions=mentions), attempts=10)
                await add_modmail(msg, copy)

            if thread_id is None:
                await create_thread(msg.author.id, header.id)

            await msg.add_reaction("\u2709")


@cog
class Modmail(Cog):
    """Handle modmail messages"""

    @Cog.listener("on_message")
    async def modmail_reply(self, msg: Message) -> None:
        if msg.author.bot:
            return
        if msg.reference is None or msg.reference.message_id is None:
            return
        if msg.reference.message_id not in message_map:
            return
        if not msg.guild:
            return
        if not (client := clients.get(msg.guild.id)):
            return
        modmail = message_map[msg.reference.message_id]

        anon_react = "\U0001F574"
        named_react = "\U0001F9CD"
        cancel_react = "\u274C"

        try:
            query = await msg.channel.send(
                "Reply anonymously {}, personally {}, or cancel {}".format(anon_react, named_react, cancel_react)
            )
        except (discord.NotFound, discord.Forbidden):
            return

        result = await get_reaction(
            query,
            msg.author,
            {anon_react: "anon", named_react: "named", cancel_react: None},
            timeout=120,
            unreact=False,
        )

        await query.delete()
        if result is None:
            await msg.channel.send("Cancelled")
        else:
            items = []
            if result == "named":
                items.append(PlainItem(format("**From {}** {!m}:\n\n", msg.author.display_name, msg.author)))
            items.append(PlainItem(msg.content))
            for att in msg.attachments:
                items.append(PlainItem("\n**Attachment:** {}".format(att.url)))

            try:
                chan = await client.fetch_channel(modmail.dm_channel_id)
                if not isinstance(chan, DMChannel):
                    await msg.channel.send("Could not deliver DM (DM closed)")
                    return
                for content, _ in chunk_messages(items):
                    await chan.send(
                        content,
                        reference=MessageReference(
                            message_id=modmail.dm_message_id, channel_id=modmail.dm_channel_id, fail_if_not_exists=False
                        ),
                    )
            except (discord.NotFound, discord.Forbidden):
                await msg.channel.send("Could not deliver DM (User left guild?)")
            else:
                await msg.channel.send("Signed reply delivered" if result == "named" else "Anonymous reply delivered")


clients: Dict[int, ModMailClient] = {}


@plugins.init
async def init_task() -> None:
    async def run_modmail(conf: GuildConfig) -> None:
        client = clients[conf.guild_id] = ModMailClient(
            max_messages=None,
            intents=Intents(dm_messages=True),
            allowed_mentions=AllowedMentions(everyone=False, roles=False),
        )
        client.conf = conf
        try:
            async with client:
                await client.start(conf.token, reconnect=True)
        except asyncio.CancelledError:
            pass
        except:
            logger.error("Exception in modmail client task", exc_info=True)
        finally:
            await client.close()

    async with sessionmaker() as session:
        for conf in (await session.execute(select(GuildConfig))).scalars():
            task = asyncio.create_task(run_modmail(conf), name="Modmail client for {}".format(conf.guild_id))
            plugins.finalizer(task.cancel)


class GuildContext(Context):
    guild_id: int


@plugin_config_command
@group("modmail")
@privileged
async def config(ctx: GuildContext, server: PartialGuildConverter) -> None:
    ctx.guild_id = server.id


@config.command("new")
@privileged
async def config_new(
    ctx: GuildContext,
    token: str,
    channel: PartialChannelConverter,
    role: PartialRoleConverter,
    thread_expiry: DurationConverter,
) -> None:
    async with sessionmaker() as session:
        session.add(
            GuildConfig(
                guild_id=ctx.guild_id, token=token, channel_id=channel.id, role_id=role.id, thread_expiry=thread_expiry
            )
        )
        await session.commit()
        await ctx.send("\u2705")


async def get_conf(session: AsyncSession, ctx: GuildContext) -> GuildConfig:
    if (conf := await session.get(GuildConfig, ctx.guild_id)) is None:
        raise UserError("No config for {}".format(ctx.guild_id))
    return conf


@config.command("token")
@privileged
async def config_token(ctx: GuildContext, token: Optional[str]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if token is None:
            await ctx.send(format("{!i}", conf.token))
        else:
            conf.token = token
            await session.commit()
            await ctx.send("\u2705")


@config.command("channel")
@privileged
async def config_channel(ctx: GuildContext, channel: Optional[PartialChannelConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if channel is None:
            await ctx.send(format("{!c}", conf.channel_id))
        else:
            conf.channel_id = channel.id
            await session.commit()
            await ctx.send("\u2705")


@config.command("role")
@privileged
async def config_role(ctx: GuildContext, role: Optional[PartialRoleConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if role is None:
            await ctx.send(format("{!M}", conf.role_id), allowed_mentions=AllowedMentions.none())
        else:
            conf.role_id = role.id
            await session.commit()
            await ctx.send("\u2705")


@config.command("thread_expiry")
@privileged
async def config_thread_expiry(ctx: GuildContext, thread_expiry: Optional[DurationConverter]) -> None:
    async with sessionmaker() as session:
        conf = await get_conf(session, ctx)
        if thread_expiry is None:
            await ctx.send(str(thread_expiry))
        else:
            conf.thread_expiry = thread_expiry
            await session.commit()
            await ctx.send("\u2705")
