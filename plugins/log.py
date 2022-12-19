from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from io import BytesIO
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, Iterator, List, Optional, Protocol, Sequence, Set, cast

import discord
from discord import (AllowedMentions, Attachment, File, Guild, Member, Message, Object, RawBulkMessageDeleteEvent,
    RawMessageDeleteEvent, RawMessageUpdateEvent, TextChannel, User)
from discord.utils import time_snowflake
from sqlalchemy import CHAR, TEXT, TIMESTAMP, BigInteger, ForeignKey, delete, select, update
from sqlalchemy.dialects.postgresql import BYTEA
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateSchema

from bot.client import client
from bot.cogs import Cog, cog
import bot.message_tracker
import plugins
import util.db
import util.db.kv
from util.discord import PlainItem, chunk_messages, format

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

sessionmaker = async_sessionmaker(engine, future=True, expire_on_commit=False)

@registry.mapped
class SavedMessage:
    __tablename__ = "messages"
    __table_args__ = {"schema": "log"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True,
        autoincrement=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(TEXT, nullable=False)
    nick: Mapped[Optional[str]] = mapped_column(TEXT)
    content: Mapped[bytes] = mapped_column(BYTEA,
        nullable=False)

    if TYPE_CHECKING:
        def __init__(self, *, id: int, channel_id: int, author_id: int, username: str, content: bytes,
            nick: Optional[str] = ...) -> None: ...

@registry.mapped
class SavedFile:
    __tablename__ = "files"
    __table_args__ = {"schema": "log"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True,
        autoincrement=False)
    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(SavedMessage.id), nullable=False)
    filename: Mapped[str] = mapped_column(TEXT, nullable=False)
    url: Mapped[str] = mapped_column(TEXT, nullable=False)
    local_filename: Mapped[Optional[str]] = mapped_column(TEXT)

    if TYPE_CHECKING:
        def __init__(self, *, id: int, message_id: int, filename: str, url: str, local_filename: Optional[str] = ...
            ) -> None: ...

@registry.mapped
class SavedUser:
    __tablename__ = "users"
    __table_args__ = {"schema": "log"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    set_at: Mapped[datetime] = mapped_column(TIMESTAMP, primary_key=True)
    username: Mapped[str] = mapped_column(TEXT, nullable=False)
    discrim: Mapped[str] = mapped_column(CHAR(4), nullable=False)
    unset_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

    if TYPE_CHECKING:
        def __init__(self, *, id: int, set_at: datetime, username: str, discrim: str, unset_at: Optional[datetime] = ...
            ) -> None: ...

# This schema assumes single guild
@registry.mapped
class SavedNick:
    __tablename__ = "nicks"
    __table_args__ = {"schema": "log"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    set_at: Mapped[datetime] = mapped_column(TIMESTAMP, primary_key=True)
    nick: Mapped[Optional[str]] = mapped_column(TEXT)
    unset_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP)

    if TYPE_CHECKING:
        def __init__(self, *, id: int, set_at: datetime, nick: Optional[str], unset_at: Optional[datetime] = ...
            ) -> None: ...

def path_for(attm: Attachment) -> Path:
    return Path(conf.file_path, str(attm.id))

async def save_attachment(attm: Attachment) -> None:
    path = path_for(attm)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await attm.save(path, use_cached=True)
    except discord.HTTPException:
        await attm.save(path)

async def register_messages(msgs: Iterable[Message]) -> None:
    async with sessionmaker() as session:
        filepaths = set()
        try:
            for msg in msgs:
                if not msg.author.bot:
                    session.add(SavedMessage(id=msg.id, channel_id=msg.channel.id, author_id=msg.author.id,
                        username=msg.author.name + "#" + msg.author.discriminator,
                        nick=msg.author.nick if isinstance(msg.author, Member) else None,
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
            cutoff_time = datetime.utcnow() - timedelta(seconds=conf.keep)
            cutoff = time_snowflake(cutoff_time)
            async with sessionmaker() as session:
                stmt = (delete(SavedFile)
                    .where(SavedFile.id < cutoff)
                    .returning(SavedFile.local_filename))
                for filepath in (await session.execute(stmt)).scalars():
                    if filepath is not None:
                        try:
                            os.unlink(filepath)
                        except FileNotFoundError:
                            pass

                stmt = delete(SavedMessage).where(SavedMessage.id < cutoff)
                await session.execute(stmt)

                stmt = delete(SavedUser).where(SavedUser.unset_at < cutoff_time)
                await session.execute(stmt)

                stmt = delete(SavedNick).where(SavedNick.unset_at < cutoff_time)
                await session.execute(stmt)

                await session.commit()

            if isinstance(channel := client.get_channel(conf.temp_channel), TextChannel):
                await channel.purge(before=Object(cutoff), limit=None)
        except asyncio.CancelledError:
            raise
        except:
            logger.error("Exception in cleanup task", exc_info=True)

cleanup_task: asyncio.Task[None]

@plugins.init
async def init() -> None:
    global conf, cleanup_task
    await util.db.init(util.db.get_ddl(
        CreateSchema("log"),
        registry.metadata.create_all))
    conf = cast(LoggerConf, await util.db.kv.load(__name__))
    await bot.message_tracker.subscribe(__name__, None, register_messages, missing=True, retroactive=False)
    async def unsubscribe() -> None:
        await bot.message_tracker.unsubscribe(__name__, None)
    plugins.finalizer(unsubscribe)
    cleanup_task = asyncio.create_task(clean_old_messages())
    plugins.finalizer(cleanup_task.cancel)

def format_word_diff(old: str, new: str) -> Iterator[PlainItem]:
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, new).get_opcodes():
        if tag == "replace":
            yield PlainItem(format("~~{!i}~~**{!i}**", old[i1:i2], new[j1:j2]))
        elif tag == "delete":
            yield PlainItem(format("~~{!i}~~", old[i1:i2]))
        elif tag == "insert":
            yield PlainItem(format("**{!i}**", new[j1:j2]))
        elif tag == "equal":
            yield PlainItem(format("{!i}", new[j1:j2]))

def user_nick(user: str, nick: Optional[str]) -> str:
    if nick is None:
        return user
    else:
        return "{}({})".format(user, nick)

async def process_message_edit(update: RawMessageUpdateEvent) -> None:
    async with sessionmaker() as session:
        if (msg := await session.get(SavedMessage, update.message_id)) is not None:
            old_content = msg.content.decode("utf8")
            if "content" in update.data and (new_content := update.data["content"]) != old_content:
                msg.content = new_content.encode("utf8")
                await session.commit()
                for content, _ in chunk_messages([
                    PlainItem(format("**Message edit**: {!c} {!m}({}) {}: ", msg.channel_id, msg.author_id,
                        msg.author_id, user_nick(msg.username, msg.nick)))] + list(
                        format_word_diff(old_content, new_content))):
                    await client.get_partial_messageable(conf.temp_channel).send(content,
                        allowed_mentions=AllowedMentions.none())
            # TODO: attachment edits

async def process_message_delete(delete: RawMessageDeleteEvent) -> None:
    async with sessionmaker() as session:
        if (msg := await session.get(SavedMessage, delete.message_id)) is not None:
            stmt = select(SavedFile.url).where(SavedFile.message_id == msg.id)
            file_urls = list((await session.execute(stmt)).scalars())
            for content, _ in chunk_messages([
                PlainItem(format("**Message delete**: {!c} {!m}({}) {}: ", msg.channel_id, msg.author_id,
                    msg.author_id, user_nick(msg.username, msg.nick))),
                PlainItem(format("{!i}", msg.content.decode("utf8")))] + [
                    PlainItem("\n**Attachment: <{}>**".format(url)) for url in file_urls]):
                await client.get_partial_messageable(conf.temp_channel).send(content,
                    allowed_mentions=AllowedMentions.none())

async def process_message_bulk_delete(deletes: RawBulkMessageDeleteEvent) -> None:
    deleted_ids = list(deletes.message_ids)
    async with sessionmaker() as session:
        attms: Dict[int, List[str]] = {}
        stmt = (select(SavedFile.message_id, SavedFile.url)
            .where(SavedFile.message_id.in_(deleted_ids)))
        for message_id, url in await session.execute(stmt):
            if message_id not in attms:
                attms[message_id] = []
            attms[message_id].append(url)

        users: Set[int] = set()
        log = []
        stmt = (select(SavedMessage)
            .where(SavedMessage.id.in_(deleted_ids))
            .order_by(SavedMessage.id))
        for msg in (await session.execute(stmt)).scalars():
            users.add(msg.author_id)
            log.append("{} {}: {}".format(msg.author_id, user_nick(msg.username, msg.nick), msg.content.decode("utf8")))
            if msg.id in attms:
                log.append("Attachments: {}".format(", ".join(attms[msg.id])))

        await client.get_partial_messageable(conf.perm_channel).send(
            format("**Message bulk delete**: {!c} {}", deletes.channel_id,
                ", ".join(format("{!m}", user) for user in users)),
            file=File(BytesIO("\n".join(log).encode("utf8")), filename="log.txt"),
            allowed_mentions=AllowedMentions.none())

async def process_user_change(id: int, before_name: str, before_discr: str, after_name: str, after_discr: str) -> None:
    now = datetime.utcnow()
    async with sessionmaker() as session:
        stmt = (update(SavedUser)
            .values(unset_at=now)
            .where(SavedUser.id == id, SavedUser.unset_at == None)
            .returning(SavedUser.username, SavedUser.discrim))
        uds = await session.execute(stmt)
        if not any(before_name == username and before_discr == discr for username, discr in uds):
            session.add(SavedUser(id=id, set_at=now - timedelta(microseconds=1), unset_at=now, username=before_name,
                discrim=before_discr))
        session.add(SavedUser(id=id, set_at=now, username=after_name, discrim=after_discr))
        await session.commit()

async def process_nick_change(id: int, before: Optional[str], after: Optional[str]) -> None:
    now = datetime.utcnow()
    async with sessionmaker() as session:
        stmt = (update(SavedNick)
            .values(unset_at=now)
            .where(SavedNick.id == id, SavedNick.unset_at == None)
            .returning(SavedNick.nick))
        nicks = (await session.execute(stmt)).scalars()
        if before not in nicks:
            session.add(SavedNick(id=id, set_at=now - timedelta(microseconds=1), unset_at=now, nick=before))
        session.add(SavedNick(id=id, set_at=now, nick=after))
        await session.commit()

async def sync_single_name(id: int, username: str, discrim: str, nick: Optional[str]) -> None:
    now = datetime.utcnow()
    async with sessionmaker() as session:
        stmt = (update(SavedUser)
            .values(unset_at=now)
            .where(SavedUser.id == id, SavedUser.unset_at == None)
            .returning(SavedUser.username, SavedUser.discrim))
        uds = (await session.execute(stmt)).all()
        if len(uds) == 1 and uds[0][0] == username and uds[0][1] == discrim:
            await session.rollback()
        else:
            session.add(SavedUser(id=id, set_at=now, username=username, discrim=discrim))
            await session.commit()
        stmt = (update(SavedNick)
            .values(unset_at=now)
            .where(SavedNick.id == id, SavedNick.unset_at == None)
            .returning(SavedNick.nick))
        nicks = (await session.execute(stmt)).scalars().all()
        if len(nicks) == 1 and nicks[0] == nick:
            await session.rollback()
        else:
            session.add(SavedNick(id=id, set_at=now, nick=nick))
            await session.commit()

async def sync_names(members: Sequence[Member]) -> None:
    now = datetime.utcnow()
    async with sessionmaker() as session:
        user_map = defaultdict(list)
        stmt = select(SavedUser).where(SavedUser.unset_at == None)
        for user in (await session.execute(stmt)).scalars():
            user_map[user.id].append(user)
        nick_map = defaultdict(list)
        stmt = select(SavedNick).where(SavedNick.unset_at == None)
        for nick in (await session.execute(stmt)).scalars():
            nick_map[nick.id].append(nick)

        for member in members:
            users = user_map[member.id]
            if len(users) != 1 or users[0].username != member.name or users[0].discrim != member.discriminator:
                for user in users:
                    user.unset_at = now
                session.add(SavedUser(id=member.id, set_at=now, username=member.name, discrim=member.discriminator))
            nicks = nick_map[member.id]
            if len(nicks) != 1 or nicks[0].nick != member.nick:
                for nick in nicks:
                    nick.unset_at = now
                session.add(SavedNick(id=member.id, set_at=now, nick=member.nick))
        await session.commit()

@cog
class MessageLog(Cog):
    @Cog.listener()
    async def on_raw_message_edit(self, update: RawMessageUpdateEvent) -> None:
        bot.message_tracker.schedule(process_message_edit(update))

    @Cog.listener()
    async def on_raw_message_delete(self, delete: RawMessageDeleteEvent) -> None:
        if delete.channel_id == conf.temp_channel: return
        bot.message_tracker.schedule(process_message_delete(delete))

    @Cog.listener()
    async def on_raw_bulk_message_delete(self, deletes: RawBulkMessageDeleteEvent) -> None:
        if deletes.channel_id == conf.temp_channel: return
        bot.message_tracker.schedule(process_message_bulk_delete(deletes))

    @Cog.listener("on_member_join")
    async def log_member_join(self, member: Member) -> None:
        await client.get_partial_messageable(conf.perm_channel).send(
            format("**Member join**: {!m}({}) {}", member.id, member.id,
                user_nick(member.name + "#" + member.discriminator, member.nick)),
            allowed_mentions=AllowedMentions.none())

    @Cog.listener("on_member_join")
    async def sync_member_join(self, member: Member) -> None:
        await sync_single_name(member.id, member.name, member.discriminator, member.nick)

    @Cog.listener("on_member_remove")
    async def log_member_remove(self, member: Member) -> None:
        await client.get_partial_messageable(conf.perm_channel).send(
            format("**Member remove**: {!m}({}) {}", member.id, member.id,
                user_nick(member.name + "#" + member.discriminator, member.nick)),
            allowed_mentions=AllowedMentions.none())

    @Cog.listener("on_member_remove")
    async def sync_member_remove(self, member: Member) -> None:
        await sync_single_name(member.id, member.name, member.discriminator, member.nick)

    @Cog.listener("on_member_update")
    async def log_member_update(self, before: Member, after: Member) -> None:
        if before.nick != after.nick:
            await client.get_partial_messageable(conf.perm_channel).send(
                format("**Nick change**: {!m}({}) {}: {} -> {}", after.id, after.id,
                    after.name + "#" + after.discriminator, before.display_name, after.display_name),
                allowed_mentions=AllowedMentions.none())

    @Cog.listener("on_member_update")
    async def sync_member_update(self, before: Member, after: Member) -> None:
        if before.nick != after.nick:
            await process_nick_change(after.id, before.nick, after.nick)

    @Cog.listener("on_user_update")
    async def log_user_update(self, before: User, after: User) -> None:
        if before.name != after.name or before.discriminator != after.discriminator:
            await client.get_partial_messageable(conf.perm_channel).send(
                format("**Username change**: {!m}({}) {} -> {}", after.id, after.id,
                    before.name + "#" + before.discriminator, after.name + "#" + after.discriminator),
                allowed_mentions=AllowedMentions.none())

    @Cog.listener("on_user_update")
    async def sync_user_update(self, before: User, after: User) -> None:
        if before.name != after.name or before.discriminator != after.discriminator:
            await process_user_change(after.id, before.name, before.discriminator, after.name, after.discriminator)

    @Cog.listener()
    async def on_guild_available(self, guild: Guild) -> None:
        await sync_names(guild.members)
