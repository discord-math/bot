from __future__ import annotations

import asyncio
import datetime
import difflib
import io
import logging
import os
import pathlib
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Protocol, Set, cast

import discord
import discord.ext.commands
import sqlalchemy
import sqlalchemy.dialects.postgresql
import sqlalchemy.ext.asyncio
import sqlalchemy.orm

import bot.client
import bot.cogs
import bot.message_tracker
import plugins
import util.db
import util.db.kv
import util.discord

logger: logging.Logger = logging.getLogger(__name__)

class LoggerConf(Protocol):
    temp_channel: int
    perm_channel: int
    keep: int
    interval: int
    file_path: str

conf: LoggerConf

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

engine = util.db.create_async_engine()
plugins.finalizer(engine.dispose)

sessionmaker = sqlalchemy.ext.asyncio.async_sessionmaker(engine, future=True, expire_on_commit=False)

@registry.mapped
class SavedMessage:
    __tablename__ = "saved_messages"

    id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, primary_key=True,
        autoincrement=False)
    channel_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    author_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, nullable=False)
    username: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT, nullable=False)
    nick: sqlalchemy.orm.Mapped[Optional[str]] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT)
    content: sqlalchemy.orm.Mapped[bytes] = sqlalchemy.orm.mapped_column(sqlalchemy.dialects.postgresql.BYTEA,
        nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, id: int, channel_id: int, author_id: int, username: str, content: bytes,
            nick: Optional[str] = ...) -> None: ...

@registry.mapped
class SavedFile:
    __tablename__ = "saved_files"

    id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger, primary_key=True,
        autoincrement=False)
    message_id: sqlalchemy.orm.Mapped[int] = sqlalchemy.orm.mapped_column(sqlalchemy.BigInteger,
        sqlalchemy.ForeignKey(SavedMessage.id), nullable=False) # type: ignore
    filename: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT, nullable=False)
    url: sqlalchemy.orm.Mapped[str] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT, nullable=False)
    local_filename: sqlalchemy.orm.Mapped[Optional[str]] = sqlalchemy.orm.mapped_column(sqlalchemy.TEXT)

    if TYPE_CHECKING:
        def __init__(self, *, id: int, message_id: int, filename: str, url: str, local_filename: Optional[str] = ...
            ) -> None: ...

def path_for(attm: discord.Attachment) -> pathlib.Path:
    return pathlib.Path(conf.file_path, str(attm.id))

async def save_attachment(attm: discord.Attachment) -> None:
    path = path_for(attm)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await attm.save(path, use_cached=True)
    except discord.HTTPException:
        await attm.save(path)

async def register_messages(msgs: Iterable[discord.Message]) -> None:
    async with sessionmaker() as session:
        filepaths = set()
        try:
            for msg in msgs:
                if not msg.author.bot:
                    session.add(SavedMessage(id=msg.id, channel_id=msg.channel.id, author_id=msg.author.id,
                        username=msg.author.name + "#" + msg.author.discriminator,
                        nick=msg.author.nick if isinstance(msg.author, discord.Member) else None,
                        content=msg.content.encode("utf8")))
                    filepaths |= {path_for(attm) for attm in msg.attachments}
                    attm_data = await asyncio.gather(*[save_attachment(attm) for attm in msg.attachments],
                        return_exceptions=True)
                    await session.flush()
                    for attm, exc in zip(msg.attachments, attm_data):
                        if exc is not None:
                            logger.info("Could not save attachment {} for {}".format(attm.proxy_url, msg.id),
                                exc_info=exc)
                            try:
                                os.unlink(path_for(attm))
                            except FileNotFoundError:
                                pass
                        session.add(SavedFile(id=attm.id, message_id=msg.id, filename=attm.filename, url=attm.url,
                            local_filename=str(path_for(attm)) if exc is None else None))
            await session.commit()
            filepaths = set()
        finally:
            for filepath in filepaths:
                try:
                    os.unlink(filepath)
                except FileNotFoundError:
                    pass

async def clean_old_messages() -> None:
    while True:
        try:
            await asyncio.sleep(conf.interval)
            cutoff = discord.utils.time_snowflake(datetime.datetime.now() - datetime.timedelta(seconds=conf.keep))
            async with sessionmaker() as session:
                stmt = (sqlalchemy.delete(SavedFile)
                    .where(SavedFile.id < cutoff)
                    .returning(SavedFile.local_filename))
                for filepath in (await session.execute(stmt)).scalars():
                    if filepath is not None:
                        try:
                            os.unlink(filepath)
                        except FileNotFoundError:
                            pass
                stmt = (sqlalchemy.delete(SavedMessage)
                    .where(SavedMessage.id < cutoff))
                await session.execute(stmt)
                await session.commit()
            if isinstance(channel := bot.client.client.get_channel(conf.temp_channel), discord.TextChannel):
                await channel.purge(before=discord.Object(cutoff), limit=None)
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in cleanup task", exc_info=True)

cleanup_task: asyncio.Task[None]

@plugins.init
async def init() -> None:
    global conf, cleanup_task
    await util.db.init(util.db.get_ddl(registry.metadata.create_all))
    conf = cast(LoggerConf, await util.db.kv.load(__name__))
    await bot.message_tracker.subscribe(__name__, None, register_messages, missing=True, retroactive=False)
    async def unsubscribe() -> None:
        await bot.message_tracker.unsubscribe(__name__, None)
    plugins.finalizer(unsubscribe)
    cleanup_task = asyncio.create_task(clean_old_messages())
    plugins.finalizer(cleanup_task.cancel)

def format_word_diff(old: str, new: str) -> str:
    output = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag == "replace":
            output.append(util.discord.format("~~{!i}~~**{!i}**", old[i1:i2], new[j1:j2]))
        elif tag == "delete":
            output.append(util.discord.format("~~{!i}~~", old[i1:i2]))
        elif tag == "insert":
            output.append(util.discord.format("**{!i}**", new[j1:j2]))
        elif tag == "equal":
            output.append(util.discord.format("{!i}", new[j1:j2]))
    return "".join(output)

def user_nick(user: str, nick: Optional[str]) -> str:
    if nick is None:
        return user
    else:
        return "{}({})".format(user, nick)

async def process_message_edit(update: discord.RawMessageUpdateEvent) -> None:
    async with sessionmaker() as session:
        if (msg := await session.get(SavedMessage, update.message_id)) is not None:
            old_content = msg.content.decode("utf8")
            if "content" in update.data and (new_content := update.data["content"]) != old_content:
                msg.content = new_content.encode("utf8")
                await session.commit()
                await util.discord.ChannelById(bot.client.client, conf.temp_channel).send(
                    util.discord.format("**Message edit**: {!c} {!m}({}) {}: {}", msg.channel_id, msg.author_id,
                        msg.author_id, user_nick(msg.username, msg.nick),
                        format_word_diff(old_content, new_content))[:2000], # TODO: split
                    allowed_mentions=discord.AllowedMentions.none())
            # TODO: attachment edits

async def process_message_delete(delete: discord.RawMessageDeleteEvent) -> None:
    async with sessionmaker() as session:
        if (msg := await session.get(SavedMessage, delete.message_id)) is not None:
            stmt = sqlalchemy.select(SavedFile.url).where(SavedFile.message_id == msg.id)
            file_urls = list((await session.execute(stmt)).scalars())
            att_list = ""
            if len(file_urls) > 0:
                att_list = "\n**Attachments: {}**".format(", ".join("<{}>".format(url) for url in file_urls))
            await util.discord.ChannelById(bot.client.client, conf.temp_channel).send(
                util.discord.format("**Message delete**: {!c} {!m}({}) {}: {!i}{}", msg.channel_id, msg.author_id,
                    msg.author_id, user_nick(msg.username, msg.nick), msg.content.decode("utf8"), att_list)[:2000],
                    allowed_mentions=discord.AllowedMentions.none())

async def process_message_bulk_delete(deletes: discord.RawBulkMessageDeleteEvent) -> None:
    deleted_ids = list(deletes.message_ids)
    async with sessionmaker() as session:
        attms: Dict[int, List[str]] = {}
        stmt = (sqlalchemy.select(SavedFile.message_id, SavedFile.url)
            .where(SavedFile.message_id.in_(deleted_ids)))
        for message_id, url in await session.execute(stmt):
            if message_id not in attms:
                attms[message_id] = []
            attms[message_id].append(url)

        users: Set[int] = set()
        log = []
        stmt = (sqlalchemy.select(SavedMessage)
            .where(SavedMessage.id.in_(deleted_ids))
            .order_by(SavedMessage.id))
        for msg in (await session.execute(stmt)).scalars():
            users.add(msg.author_id)
            log.append("{} {}: {}".format(msg.author_id, user_nick(msg.username, msg.nick), msg.content.decode("utf8")))
            if msg.id in attms:
                log.append("Attachments: {}".format(", ".join(attms[msg.id])))

        await util.discord.ChannelById(bot.client.client, conf.perm_channel).send(
            util.discord.format("**Message bulk delete**: {!c} {}", deletes.channel_id,
                ", ".join(util.discord.format("{!m}", user) for user in users)),
            file=discord.File(io.BytesIO("\n".join(log).encode("utf8")), filename="log.txt"),
            allowed_mentions=discord.AllowedMentions.none())

@bot.cogs.cog
class MessageLog(discord.ext.commands.Cog):
    @discord.ext.commands.Cog.listener()
    async def on_raw_message_edit(self, update: discord.RawMessageUpdateEvent) -> None:
        bot.message_tracker.schedule(process_message_edit(update))

    @discord.ext.commands.Cog.listener()
    async def on_raw_message_delete(self, delete: discord.RawMessageDeleteEvent) -> None:
        if delete.channel_id == conf.temp_channel: return
        bot.message_tracker.schedule(process_message_delete(delete))

    @discord.ext.commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, deletes: discord.RawBulkMessageDeleteEvent) -> None:
        if deletes.channel_id == conf.temp_channel: return
        bot.message_tracker.schedule(process_message_bulk_delete(deletes))

    @discord.ext.commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await util.discord.ChannelById(bot.client.client, conf.perm_channel).send(
            util.discord.format("**Member join**: {!m}({}) {}", member.id, member.id,
                user_nick(member.name + "#" + member.discriminator, member.nick)),
            allowed_mentions=discord.AllowedMentions.none())

    @discord.ext.commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await util.discord.ChannelById(bot.client.client, conf.perm_channel).send(
            util.discord.format("**Member remove**: {!m}({}) {}", member.id, member.id,
                user_nick(member.name + "#" + member.discriminator, member.nick)),
            allowed_mentions=discord.AllowedMentions.none())

    @discord.ext.commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick != after.nick:
            await util.discord.ChannelById(bot.client.client, conf.perm_channel).send(
                util.discord.format("**Nick change**: {!m}({}) {}: {} -> {}", after.id, after.id,
                    after.name + "#" + after.discriminator, before.display_name, after.display_name),
                allowed_mentions=discord.AllowedMentions.none())

    @discord.ext.commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if before.name != after.name or before.discriminator != after.discriminator:
            await util.discord.ChannelById(bot.client.client, conf.perm_channel).send(
                util.discord.format("**Username change**: {!m}({}) {} -> {}", after.id, after.id,
                    before.name + "#" + before.discriminator, after.name + "#" + after.discriminator),
                allowed_mentions=discord.AllowedMentions.none())
