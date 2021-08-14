import asyncio
import sqlalchemy
import sqlalchemy.schema
import sqlalchemy.orm
import sqlalchemy.ext.asyncio
import sqlalchemy.dialects.postgresql
import discord
import logging
import datetime
from typing import Dict, Tuple, Optional, Awaitable, Any, Protocol, cast
import discord_client
import util.db
import util.db.kv
import plugins
import plugins.reactions

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
@plugins.finalizer
async def cleanup_engine() -> None:
    await engine.dispose()

@registry.mapped
class ModmailMessage:
    __tablename__ = "messages"
    __table_args__ = {"schema": "modmail"}

    dm_channel_id: int = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=False)
    dm_message_id: int = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=False)
    staff_message_id: int = sqlalchemy.Column(sqlalchemy.BigInteger, primary_key=True)

@registry.mapped
class ModmailThread:
    __tablename__ = "threads"
    __table_args__ = {"schema": "modmail"}

    user_id: int = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=False)
    thread_first_message_id: int = sqlalchemy.Column(sqlalchemy.BigInteger, primary_key=True)
    last_used: datetime.datetime = sqlalchemy.Column(sqlalchemy.TIMESTAMP, nullable=False)

class ModmailConf(Protocol, Awaitable[None]):
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
        sqlalchemy.schema.CreateSchema("modmail").execute,
        registry.metadata.create_all))

    async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
        for msg in (await session.execute(sqlalchemy.select(ModmailMessage))).scalars():
            message_map[msg.staff_message_id] = msg

async def add_modmail(source: discord.Message, copy: discord.Message) -> None:
    async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
        msg = ModmailMessage(dm_channel_id=source.channel.id, dm_message_id=source.id, staff_message_id=copy.id)
        session.add(msg)
        await session.flush()
        message_map[msg.staff_message_id] = msg

async def update_thread(user_id: int) -> Optional[int]:
    async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
        stmt = (sqlalchemy.update(ModmailThread).returning(ModmailThread.thread_first_message_id)
            .where(ModmailThread.user_id == user_id,
                ModmailThread.last_used > sqlalchemy.func.current_timestamp() -
                    datetime.timedelta(seconds=conf.thread_expiry))
            .values(last_used=sqlalchemy.func.current_timestamp())
            .execution_options(synchronize_session=False))

        thread = (await session.execute(stmt)).scalars().first()
        await session.commit()
        return thread

async def create_thread(user_id: int, msg_id: int) -> None:
    async with sqlalchemy.ext.asyncio.AsyncSession(engine) as session:
        thread = ModmailThread(user_id=user_id, thread_first_message_id=msg_id,
            last_used=sqlalchemy.func.current_timestamp()) # type: ignore
        session.add(thread)
        await session.commit()

class ModMailClient(discord.Client):
    async def on_ready(self) -> None:
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="DMs"))

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        logger.error("Exception in modmail client {}".format(event_method), exc_info=True)

    async def on_message(self, msg: discord.Message) -> None:
        if not msg.guild and msg.author.id != self.user.id:
            try:
                guild = discord_client.client.get_guild(int(conf.guild))
                if guild is None: return
                channel = guild.get_channel(int(conf.channel))
                if not isinstance(channel, discord.TextChannel): return
                role = guild.get_role(int(conf.role))
                if role is None: return
            except (ValueError, AttributeError):
                return
            thread_id = await update_thread(msg.author.id)
            header = util.discord.format("**From {}#{}** {} {!m} on {}:\n\n",
                msg.author.name, msg.author.discriminator, msg.author.id, msg.author, msg.created_at)
            footer = "".join("\n**Attachment:** {} {}".format(att.filename, att.url) for att in msg.attachments)
            if thread_id is None:
                footer += util.discord.format("\n{!m}", role)
            if footer:
                footer = "\n" + footer
            text = msg.content
            mentions = discord.AllowedMentions.none()
            mentions.roles = [role]
            reference = None
            if thread_id is not None:
                reference = discord.MessageReference(
                    message_id=thread_id, channel_id=channel.id, fail_if_not_exists=False)
            copy_first = None
            sent_footer = False
            for i in range(0, len(header) + len(text), 2000):
                part = (header + text)[i:i + 2000]
                if len(part) + len(footer) <= 2000:
                    part += footer
                    sent_footer = True
                copy = await channel.send(part,
                    allowed_mentions=mentions, reference=reference if copy_first is None else None)
                await add_modmail(msg, copy)
                if copy_first is None:
                    copy_first = copy
            if not sent_footer:
                copy = await channel.send(footer,
                    allowed_mentions=mentions)
                await add_modmail(msg, copy)
            if thread_id is None and copy_first is not None:
                await create_thread(msg.author.id, copy_first.id)
            await msg.add_reaction("\u2709")

@util.discord.event("message")
async def modmail_reply(msg: discord.Message) -> None:
    if msg.reference and msg.reference.message_id in message_map and not msg.author.bot:
        modmail = message_map[msg.reference.message_id]

        anon_react = "\U0001F574"
        named_react = "\U0001F9CD"
        cancel_react = "\u274C"

        try:
            query = await msg.channel.send(
                "Reply anonymously {}, personally {}, or cancel {}".format(anon_react, named_react, cancel_react))
        except (discord.NotFound, discord.Forbidden):
            return

        reactions = (anon_react, named_react, cancel_react)
        try:
            with plugins.reactions.ReactionMonitor(channel_id=query.channel.id, message_id=query.id,
                author_id=msg.author.id, event="add", filter=lambda _, p: p.emoji.name in reactions,
                timeout_each=120) as mon:
                for react in reactions:
                    await query.add_reaction(react)
                _, payload = await mon
                reaction = payload.emoji.name
        except (discord.NotFound, discord.Forbidden):
            return
        except asyncio.TimeoutError:
            reaction = cancel_react

        await query.delete()
        if reaction == cancel_react:
            await msg.channel.send("Cancelled")
        else:
            header = ""
            if reaction == named_react:
                name = isinstance(msg.author, discord.Member) and msg.author.nick or msg.author.name
                header = util.discord.format("**From {}** {!m}:\n\n", name, msg.author)
            try:
                chan = await client.fetch_channel(modmail.dm_channel_id)
                if not isinstance(chan, discord.DMChannel):
                    await msg.channel.send("Could not deliver DM (DM closed)")
                    return
                await chan.send(header + msg.content,
                    reference=discord.MessageReference(
                        message_id=modmail.dm_message_id, channel_id=modmail.dm_channel_id, fail_if_not_exists=False))
            except (discord.NotFound, discord.Forbidden):
                await msg.channel.send("Could not deliver DM (User left guild?)")
            else:
                await msg.channel.send("Message delivered")

client: discord.Client = ModMailClient(
    loop=asyncio.get_event_loop(),
    max_messages=None,
    intents=discord.Intents(dm_messages=True),
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))

@plugins.init
async def init_task() -> None:
    async def run_modmail() -> None:
        try:
            await client.start(conf.token, reconnect=True)
        except asyncio.CancelledError:
            pass
        except:
            logger.error("Exception in modmail client task", exc_info=True)
        finally:
            await client.close()

    bot_task: asyncio.Task[None] = util.asyncio.run_async(run_modmail)
    @plugins.finalizer
    def cancel_bot_task() -> None:
        bot_task.cancel()
