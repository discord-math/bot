import asyncio
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Dict, Optional, Protocol, cast

import discord
from discord import (Activity, ActivityType, AllowedMentions, Client, DMChannel, Intents, Message, MessageReference,
    TextChannel, Thread)
from sqlalchemy import TIMESTAMP, BigInteger, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

import bot.client
from bot.cogs import Cog, cog
from bot.reactions import get_reaction
import plugins
import util.db
import util.db.kv
from util.discord import PlainItem, chunk_messages, format, retry

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = async_sessionmaker(engine, future=True)

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
        def __init__(self, *, user_id: int, thread_first_message_id: int, last_used: int) -> None: ...

class ModmailConf(Awaitable[None], Protocol):
    token: str
    guild: int
    channel: int
    role: int
    thread_expiry: int

conf: ModmailConf
logger: logging.Logger = logging.getLogger(__name__)

message_map: Dict[int, ModmailMessage] = {}

@plugins.init
async def init() -> None:
    global conf
    conf = cast(ModmailConf, await util.db.kv.load(__name__))

    conf.guild = int(conf.guild)
    conf.channel = int(conf.channel)
    conf.role = int(conf.role)
    await conf

    await util.db.init(util.db.get_ddl(
        CreateSchema("modmail"),
        registry.metadata.create_all))

    async with sessionmaker() as session:
        for msg in (await session.execute(select(ModmailMessage))).scalars():
            message_map[msg.staff_message_id] = msg

async def add_modmail(source: Message, copy: Message) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        msg = ModmailMessage(dm_channel_id=source.channel.id, dm_message_id=source.id, staff_message_id=copy.id)
        session.add(msg)
        await session.commit()
        message_map[msg.staff_message_id] = msg

async def update_thread(user_id: int) -> Optional[int]:
    async with sessionmaker() as session:
        stmt = (update(ModmailThread).returning(ModmailThread.thread_first_message_id)
            .where(ModmailThread.user_id == user_id,
                ModmailThread.last_used > func.current_timestamp() - timedelta(seconds=conf.thread_expiry))
            .values(last_used=func.current_timestamp())
            .execution_options(synchronize_session=False))

        thread = (await session.execute(stmt)).scalars().first()
        await session.commit()
        return thread

async def create_thread(user_id: int, msg_id: int) -> None:
    async with sessionmaker() as session:
        thread = ModmailThread(user_id=user_id, thread_first_message_id=msg_id,
            last_used=func.current_timestamp()) # type: ignore
        session.add(thread)
        await session.commit()

class ModMailClient(Client):
    async def on_ready(self) -> None:
        await self.change_presence(activity=Activity(type=ActivityType.watching, name="DMs"))

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        logger.error("Exception in modmail client {}".format(event_method), exc_info=True)

    async def on_message(self, msg: Message) -> None:
        if not msg.guild and self.user is not None and msg.author.id != self.user.id:
            try:
                guild = bot.client.client.get_guild(int(conf.guild))
                if guild is None: return
                channel = guild.get_channel(int(conf.channel))
                if not isinstance(channel, (TextChannel, Thread)): return
                role = guild.get_role(int(conf.role))
                if role is None: return
            except (ValueError, AttributeError):
                return
            thread_id = await update_thread(msg.author.id)

            items = [PlainItem(format("**From {}#{}** {} {!m} on {}:\n\n",
                    msg.author.name, msg.author.discriminator, msg.author.id, msg.author, msg.created_at)),
                PlainItem(msg.content)]
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
            copy_first = None
            for content, _ in chunk_messages(items):
                if reference is not None and copy_first is None:
                    copy = await retry(lambda: channel.send(content, allowed_mentions=mentions, reference=reference),
                        attempts=10)
                else:
                    copy = await retry(lambda: channel.send(content, allowed_mentions=mentions), attempts=10)
                await add_modmail(msg, copy)
                if copy_first is None:
                    copy_first = copy

            if thread_id is None and copy_first is not None:
                await retry(lambda: create_thread(msg.author.id, copy_first.id), attempts=10)

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
        modmail = message_map[msg.reference.message_id]

        anon_react = "\U0001F574"
        named_react = "\U0001F9CD"
        cancel_react = "\u274C"

        try:
            query = await msg.channel.send(
                "Reply anonymously {}, personally {}, or cancel {}".format(anon_react, named_react, cancel_react))
        except (discord.NotFound, discord.Forbidden):
            return

        result = await get_reaction(query, msg.author,
            {anon_react: "anon", named_react: "named", cancel_react: None}, timeout=120, unreact=False)

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
                    await chan.send(content, reference=MessageReference(message_id=modmail.dm_message_id,
                        channel_id=modmail.dm_channel_id, fail_if_not_exists=False))
            except (discord.NotFound, discord.Forbidden):
                await msg.channel.send("Could not deliver DM (User left guild?)")
            else:
                await msg.channel.send("Signed reply delivered" if result == "named" else "Anonymous reply delivered")

client: Client = ModMailClient(
    max_messages=None,
    intents=Intents(dm_messages=True),
    allowed_mentions=AllowedMentions(everyone=False, roles=False))

@plugins.init
async def init_task() -> None:
    async def run_modmail() -> None:
        try:
            async with client:
                await client.start(conf.token, reconnect=True)
        except asyncio.CancelledError:
            pass
        except:
            logger.error("Exception in modmail client task", exc_info=True)
        finally:
            await client.close()

    bot_task: asyncio.Task[None] = asyncio.create_task(run_modmail(), name="Modmail client")
    plugins.finalizer(bot_task.cancel)
