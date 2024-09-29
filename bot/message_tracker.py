"""
This module provides a message tracking interface. Other modules can "subscribe" to be notified about messages, and the
provided callback will be called exactly once for every message (to the best of our ability). This includes fetching the
channel history to retroactively inform new subscribers about old messages, and also checking the channel history upon
reconnecting to catch up on what we've missed.
"""

from __future__ import annotations

import asyncio
import bisect
from datetime import datetime, timedelta
import logging
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    cast,
    overload,
)

import discord
from discord import Guild, Message, Object, TextChannel, Thread, VoiceChannel
from discord.abc import GuildChannel
from discord.utils import time_snowflake
import sqlalchemy
from sqlalchemy import (
    BOOLEAN,
    TEXT,
    TIMESTAMP,
    BigInteger,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    delete,
    func,
    literal,
    literal_column,
    null,
    nulls_first,
    select,
    true,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import CreateSchema

from bot.client import client
from bot.cogs import Cog, cog
from bot.tasks import task
import plugins
import util.db
from util.discord import retry


logger: logging.Logger = logging.getLogger(__name__)

registry: sqlalchemy.orm.registry = sqlalchemy.orm.registry()

sessionmaker = async_sessionmaker(util.db.engine, future=True, expire_on_commit=False)


@registry.mapped
class Channel:
    __tablename__ = "channels"
    __table_args__ = {"schema": "message_tracker"}

    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    reachable: Mapped[bool] = mapped_column(BOOLEAN, nullable=False)

    if TYPE_CHECKING:

        def __init__(self, *, guild_id: int, id: int, reachable: bool) -> None: ...


@registry.mapped
class ChannelState:
    __tablename__ = "channel_states"
    __table_args__ = {"schema": "message_tracker"}

    channel_id: Mapped[int] = mapped_column(BigInteger, ForeignKey(Channel.id), primary_key=True, autoincrement=False)
    subscriber: Mapped[str] = mapped_column(TEXT, primary_key=True)
    # null means thread history fetch is complete
    earliest_thread_archive_ts: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    # last message id in the channel or any of its threads
    last_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    channel: Mapped[Channel] = relationship(Channel)

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            channel_id: int,
            subscriber: str,
            last_message_id: int,
            earliest_thread_archive_ts: Optional[datetime] = ...,
        ) -> None: ...


@registry.mapped
class ChannelRequest:
    __tablename__ = "channel_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subscriber: Mapped[str] = mapped_column(TEXT, nullable=False)
    # inclusive
    after_snowflake: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # exclusive
    before_snowflake: Mapped[int] = mapped_column(BigInteger, nullable=False)

    state: Mapped[ChannelState] = relationship(ChannelState)

    # TODO: EXCLUDE constraint on the snowflake ranges? What if conflict?
    __table_args__ = ForeignKeyConstraint(
        [channel_id, subscriber], [ChannelState.channel_id, ChannelState.subscriber]
    ), {"schema": "message_tracker"}

    if TYPE_CHECKING:

        def __init__(
            self, *, channel_id: int, subscriber: str, after_snowflake: int, before_snowflake: int, id: int = ...
        ) -> None: ...


@registry.mapped
class ThreadRequest:
    __tablename__ = "thread_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subscriber: Mapped[str] = mapped_column(TEXT, nullable=False)
    # inclusive
    after_snowflake: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # exclusive
    before_snowflake: Mapped[int] = mapped_column(BigInteger, nullable=False)

    state: Mapped[ChannelState] = relationship(ChannelState)

    __table_args__ = ForeignKeyConstraint(
        [channel_id, subscriber], [ChannelState.channel_id, ChannelState.subscriber]
    ), {"schema": "message_tracker"}

    if TYPE_CHECKING:

        def __init__(
            self,
            *,
            channel_id: int,
            thread_id: int,
            subscriber: str,
            after_snowflake: int,
            before_snowflake: int,
            id: int = ...,
        ) -> None: ...


@plugins.init
async def init_db() -> None:
    await util.db.init(util.db.get_ddl(CreateSchema("message_tracker"), registry.metadata.create_all))


Callback = Callable[[Iterable[Message]], Awaitable[None]]

fetch_map: Dict[str, Callback] = {}
events: Dict[str, Callback] = {}
events_guild: Dict[int, Dict[str, Callback]] = {}
events_channel: Dict[int, Dict[str, Callback]] = {}

last_archival_times: Dict[int, datetime] = {}


def approx_last_msg(channel: Union[TextChannel, VoiceChannel, Thread]) -> int:
    return channel.last_message_id if channel.last_message_id is not None else channel.id


def take_snapshot(channels: List[Union[TextChannel, VoiceChannel]]) -> Tuple[Dict[int, int], Dict[int, Dict[int, int]]]:
    return (
        {channel.id: approx_last_msg(channel) for channel in channels},
        {
            channel.id: {thread.id: approx_last_msg(thread) for thread in channel.threads}
            for channel in channels
            if isinstance(channel, TextChannel) and len(channel.threads)
        },
    )


async def approx_archival_ts(channel: Union[TextChannel, VoiceChannel]) -> Optional[datetime]:
    if isinstance(channel, VoiceChannel):
        return None
    if channel.id in last_archival_times:
        return last_archival_times[channel.id] + timedelta(milliseconds=1)
    async for thread in channel.archived_threads(limit=None):
        ts = last_archival_times.get(channel.id)
        if ts is None or ts < thread.archive_timestamp:
            ts = thread.archive_timestamp
        return ts + timedelta(milliseconds=1)
    else:
        return None


class MessageIDList(Sequence[int]):
    __slots__ = "msgs", "negate"
    msgs: List[Message]
    negate: bool

    def __init__(self, msgs: List[Message], *, negate: bool):
        self.msgs = msgs
        self.negate = negate

    @overload
    def __getitem__(self, i: int) -> int: ...

    @overload
    def __getitem__(self, i: slice) -> Sequence[int]: ...

    def __getitem__(self, i: object) -> object:
        assert isinstance(i, int)
        id = self.msgs[i].id
        return -id if self.negate else id

    def __len__(self) -> int:
        return len(self.msgs)


def index_after_msg_desc(msgs: List[Message], id: int) -> int:
    return bisect.bisect_right(MessageIDList(msgs, negate=True), -id)


def index_before_msg_asc(msgs: List[Message], id: int) -> int:
    return bisect.bisect_left(MessageIDList(msgs, negate=False), id)


async def select_fetch_task(
    session: AsyncSession, subscribers: Iterable[str]
) -> Union[None, Tuple[int, int, None, None], Tuple[int, int, None, int], Tuple[int, int, int, int]]:
    subs = list(subscribers)
    stmt = (
        union_all(
            select(
                Channel.guild_id,
                ChannelState.channel_id,
                sqlalchemy.cast(null(), BigInteger).label("thread_id"),
                null().label("before_snowflake"),
            )
            .join(ChannelState.channel)
            .where(
                Channel.reachable, ChannelState.subscriber.in_(subs), ChannelState.earliest_thread_archive_ts != None
            )
            .order_by(ChannelState.earliest_thread_archive_ts.desc())
            .limit(1),
            select(
                Channel.guild_id, ChannelRequest.channel_id, null().label("thread_id"), ChannelRequest.before_snowflake
            )
            .join(ChannelRequest.state)
            .join(ChannelState.channel)
            .where(Channel.reachable, ChannelRequest.subscriber.in_(subs))
            .order_by(ChannelRequest.before_snowflake.desc())
            .limit(1),
            select(Channel.guild_id, ThreadRequest.channel_id, ThreadRequest.thread_id, ThreadRequest.before_snowflake)
            .join(ThreadRequest.state)
            .join(ChannelState.channel)
            .where(Channel.reachable, ThreadRequest.subscriber.in_(subs))
            .order_by(ThreadRequest.before_snowflake.desc())
            .limit(1),
        )
        .order_by(nulls_first(literal_column("before_snowflake").desc()))
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    guild_id, channel_id, thread_id, before = row
    return guild_id, channel_id, thread_id, before


async def select_channel_requests_overlapping(
    session: AsyncSession, subscribers: Iterable[str], channel_id: int, before: int
) -> Iterable[ChannelRequest]:
    subs = list(subscribers)
    nonrec_first = (
        select(ChannelRequest.after_snowflake)
        .where(
            ChannelRequest.channel_id == channel_id,
            ChannelRequest.subscriber.in_(subs),
            ChannelRequest.before_snowflake >= before,
        )
        .order_by(ChannelRequest.before_snowflake)
        .limit(1)
        .cte("first", recursive=True)
    )
    rec_first = (
        select(ChannelRequest.after_snowflake)
        .where(
            ChannelRequest.channel_id == channel_id,
            ChannelRequest.subscriber.in_(subs),
            ChannelRequest.before_snowflake >= nonrec_first.c.after_snowflake,
        )
        .order_by(ChannelRequest.before_snowflake)
        .limit(1)
        .lateral()
    )
    first = nonrec_first.union(select(rec_first.c.after_snowflake).select_from(nonrec_first.join(rec_first, true())))
    min_after = select(func.min(first.c.after_snowflake)).scalar_subquery()
    stmt = select(ChannelRequest).where(
        ChannelRequest.channel_id == channel_id,
        ChannelRequest.subscriber.in_(subs),
        ChannelRequest.before_snowflake <= before,
        ChannelRequest.after_snowflake >= min_after,
    )
    return (await session.execute(stmt)).scalars()


async def select_thread_requests_overlapping(
    session: AsyncSession, subscribers: Iterable[str], thread_id: int, before: int
) -> Iterable[ThreadRequest]:
    subs = list(subscribers)
    nonrec_first = (
        select(ThreadRequest.after_snowflake)
        .where(
            ThreadRequest.thread_id == thread_id,
            ThreadRequest.subscriber.in_(subs),
            ThreadRequest.before_snowflake >= before,
        )
        .order_by(ThreadRequest.before_snowflake)
        .limit(1)
        .cte("first", recursive=True)
    )
    rec_first = (
        select(ThreadRequest.after_snowflake)
        .where(
            ThreadRequest.thread_id == thread_id,
            ThreadRequest.subscriber.in_(subs),
            ThreadRequest.before_snowflake >= nonrec_first.c.after_snowflake,
        )
        .order_by(ThreadRequest.before_snowflake)
        .limit(1)
        .lateral()
    )
    first = nonrec_first.union(select(rec_first.c.after_snowflake).select_from(nonrec_first.join(rec_first, true())))
    min_after = select(func.min(first.c.after_snowflake)).scalar_subquery()
    stmt = select(ThreadRequest).where(
        ThreadRequest.thread_id == thread_id,
        ThreadRequest.subscriber.in_(subs),
        ThreadRequest.before_snowflake <= before,
        ThreadRequest.after_snowflake >= min_after,
    )
    return (await session.execute(stmt)).scalars()


async def select_archive_ts(
    session: AsyncSession, subscribers: Iterable[str], channel_id: int
) -> Iterable[ChannelState]:
    stmt = select(ChannelState).where(ChannelState.earliest_thread_archive_ts != None)
    return (await session.execute(stmt)).scalars()


async def mark_channel_unreachable(session: AsyncSession, channel_id: int) -> None:
    stmt = update(Channel).where(Channel.id == channel_id).values(reachable=False)
    await session.execute(stmt)


async def mark_guild_unreachable(session: AsyncSession, guild_id: int) -> None:
    stmt = update(Channel).where(Channel.guild_id == guild_id).values(reachable=False)
    await session.execute(stmt)


async def drop_thread_requests(session: AsyncSession, thread_id: int) -> None:
    stmt = delete(ThreadRequest).where(ThreadRequest.thread_id == thread_id)
    await session.execute(stmt)


async def fetch_thread_archive(session: AsyncSession, channel: TextChannel) -> None:
    states = list(await select_archive_ts(session, fetch_map.keys(), channel.id))
    logger.debug(
        "Loading archived threads in {}: {}".format(
            channel.id,
            ", ".join("{!r}: {}".format(state.subscriber, state.earliest_thread_archive_ts) for state in states),
        )
    )
    if not states:
        return
    max_archival_ts = max(cast(datetime, state.earliest_thread_archive_ts) for state in states)

    try:
        threads: List[Thread] = []
        async for thread in channel.archived_threads(limit=50, before=max_archival_ts):
            threads.append(thread)
    except (discord.NotFound, discord.Forbidden):
        logger.warning("Cannot iterate archived threads in {}, marking unreachable".format(channel.id))
        await mark_channel_unreachable(session, channel.id)
        return

    if not threads:
        logger.debug(
            "No more threads in {} (subscribers: {})".format(
                channel.id, ", ".join("{!r}".format(state.subscriber) for state in states)
            )
        )
        for state in states:
            state.earliest_thread_archive_ts = None
        return

    logger.debug(
        "Fetched archived threads {}-{} in {}".format(
            threads[0].archive_timestamp, threads[-1].archive_timestamp, channel.id
        )
    )

    for state in states:
        assert state.earliest_thread_archive_ts is not None
        for thread in threads:
            if thread.archive_timestamp < state.earliest_thread_archive_ts:
                if thread.last_message_id is not None:
                    logger.debug(
                        "Requesting archived thread {} for {!r} up to {}".format(
                            thread.id, state.subscriber, thread.last_message_id
                        )
                    )
                    session.add(
                        ThreadRequest(
                            thread_id=thread.id,
                            channel_id=channel.id,
                            subscriber=state.subscriber,
                            after_snowflake=thread.id,
                            before_snowflake=thread.last_message_id + 1,
                        )
                    )
        if threads[-1].archive_timestamp < state.earliest_thread_archive_ts:
            state.earliest_thread_archive_ts = threads[-1].archive_timestamp


async def fetch_channel_messages(
    session: AsyncSession, channel: Union[TextChannel, VoiceChannel], before_snowflake: int
) -> None:
    requests = list(await select_channel_requests_overlapping(session, fetch_map.keys(), channel.id, before_snowflake))
    logger.debug(
        "Loading channel messages in {}: {}".format(
            channel.id,
            ", ".join(
                "{!r}: {}-{}".format(request.subscriber, request.before_snowflake, request.after_snowflake)
                for request in requests
            ),
        )
    )
    if not requests:
        return
    max_before = max(request.before_snowflake for request in requests)
    min_after = min(request.after_snowflake for request in requests)

    try:
        history = []
        async for msg in channel.history(limit=1000, before=Object(max_before)):
            if msg.id < min_after:
                break
            history.append(msg)
    except (discord.NotFound, discord.Forbidden):
        logger.warning("Cannot read message history in {}, marking unreachable".format(channel.id))
        await mark_channel_unreachable(session, channel.id)
        return

    if history:
        logger.debug("Fetched {}-{} from {}".format(history[0].id, history[-1].id, channel.id))
    else:
        # TODO: find out that the request is exhausted on the previous iteration when/if we receive a truncated list
        logger.debug("Fetched no messages from {}".format(channel.id))

    exception = None
    for request in requests:
        if (cb := fetch_map.get(request.subscriber)) is not None:
            idx_from = index_after_msg_desc(history, request.before_snowflake)
            idx_to = index_after_msg_desc(history, request.after_snowflake)
            if idx_from < idx_to:
                logger.debug(
                    "Notifying {!r} about {}-{} in {}".format(
                        request.subscriber, history[idx_from].id, history[idx_to - 1].id, channel.id
                    )
                )
                try:
                    await cb(history[i] for i in range(idx_from, idx_to))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "Exception when calling callback for {!r}, will redeliver".format(request.subscriber),
                        exc_info=True,
                    )
                    exception = exc
                    continue
            if idx_to < len(history) or not history:
                logger.debug(
                    "Done with request for {}-{} in {} for {!r}".format(
                        request.before_snowflake, request.after_snowflake, channel.id, request.subscriber
                    )
                )
                await session.delete(request)
            else:
                request.before_snowflake = history[-1].id
                logger.debug(
                    "Remaining {}-{} in {} for {!r}".format(
                        request.before_snowflake, request.after_snowflake, channel.id, request.subscriber
                    )
                )
    if exception is not None:
        raise exception


async def fetch_thread_messages(session: AsyncSession, thread: Thread, before_snowflake: int) -> None:
    requests = list(await select_thread_requests_overlapping(session, fetch_map.keys(), thread.id, before_snowflake))
    logger.debug(
        "Loading messages in thread {} in channel {}: {}".format(
            thread.id,
            thread.parent_id,
            ", ".join(
                "{!r}: {}-{}".format(request.subscriber, request.before_snowflake, request.after_snowflake)
                for request in requests
            ),
        )
    )
    if not requests:
        return
    max_before = max(request.before_snowflake for request in requests)
    min_after = min(request.after_snowflake for request in requests)

    try:
        history = []
        async for msg in thread.history(limit=1000, before=Object(max_before)):
            if msg.id < min_after:
                break
            history.append(msg)
    except (discord.NotFound, discord.Forbidden):
        logger.warning(
            "Cannot read message history in thread {}, marking channel {} unreachable".format(
                thread.id, thread.parent_id
            )
        )
        await mark_channel_unreachable(session, thread.parent_id)
        return

    if history:
        logger.debug(
            "Fetched {}-{} from thread {} from channel {}".format(
                history[0].id, history[-1].id, thread.id, thread.parent_id
            )
        )
    else:
        logger.debug("Fetched no messages from thread {} from channel {}".format(thread.id, thread.parent_id))

    exception = None
    for request in requests:
        if (cb := fetch_map.get(request.subscriber)) is not None:
            idx_from = index_after_msg_desc(history, request.before_snowflake)
            idx_to = index_after_msg_desc(history, request.after_snowflake)
            if idx_from < idx_to:
                logger.debug(
                    "Notifying {!r} about {}-{} in thread {} in channel {}".format(
                        request.subscriber, history[idx_from].id, history[idx_to - 1].id, thread.id, thread.parent_id
                    )
                )
                try:
                    await cb(history[i] for i in range(idx_from, idx_to))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "Exception when calling callback for {!r}, will redeliver".format(request.subscriber),
                        exc_info=True,
                    )
                    exception = exc
                    continue
            if idx_to < len(history) or not history:
                logger.debug(
                    "Done with request for {}-{} in thread {} for {!r}".format(
                        request.before_snowflake, request.after_snowflake, thread.id, request.subscriber
                    )
                )
                await session.delete(request)
            else:
                request.before_snowflake = history[-1].id
                logger.debug(
                    "Remaining {}-{} in thread {} for {!r}".format(
                        request.before_snowflake, request.after_snowflake, thread.id, request.subscriber
                    )
                )
    if exception is not None:
        raise exception


@task(name="Message tracker fetch task", exc_backoff_base=10)
async def fetch_task() -> None:
    await client.wait_until_ready()

    async with sessionmaker() as session:
        row = await select_fetch_task(session, fetch_map.keys())
        if row is None:
            return
        else:
            # There's more so run again after this iteration
            fetch_task.run_once()

        guild_id, channel_id, thread_id, before_snowflake = row

        logger.debug("Looking at channel {} (thread={}, before={})".format(channel_id, thread_id, before_snowflake))

        if (guild := client.get_guild(guild_id)) is None:
            logger.warning("Guild {} not found, marking unreachable".format(guild_id))
            await mark_guild_unreachable(session, guild_id)
            await session.commit()
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, (TextChannel, VoiceChannel)):
            logger.warning("Channel {} not found in {}, marking unreachable".format(channel_id, guild_id))
            await mark_channel_unreachable(session, channel_id)
            await session.commit()
            return

        try:
            if before_snowflake is None:
                if isinstance(channel, TextChannel):
                    await fetch_thread_archive(session, channel)
            elif thread_id is None:
                await fetch_channel_messages(session, channel, before_snowflake)
            else:
                if not isinstance(thread := await guild.fetch_channel(thread_id), Thread):
                    logger.warning("Thread {} not found in {}, dropping all requests".format(thread_id, guild_id))
                    await drop_thread_requests(session, thread_id)
                    await session.commit()
                    return
                await fetch_thread_messages(session, thread, before_snowflake)
        finally:
            await session.commit()


executor_queue: asyncio.Queue[Awaitable[None]] = asyncio.Queue()


def schedule(cb: Awaitable[None]) -> None:
    executor_queue.put_nowait(cb)


T = TypeVar("T")


async def return_result(cb: Awaitable[T], result: asyncio.Future[T]) -> None:
    try:
        result.set_result(await cb)
    except BaseException as exc:
        result.set_exception(exc)
        raise


async def schedule_and_wait(cb: Awaitable[T]) -> T:
    result: asyncio.Future[T] = asyncio.Future()
    schedule(return_result(cb, result))
    return await result


async def executor() -> None:
    while True:
        try:
            await (await executor_queue.get())
        except asyncio.CancelledError:
            logger.info("Executor cancelled")
            try:
                while True:
                    await executor_queue.get_nowait()
            except asyncio.queues.QueueEmpty:
                pass
            logger.info("Executor finished with remaining items")
            break
        except:
            logger.error("Exception in executor", exc_info=True)


executor_task: asyncio.Task[None]


@plugins.init
async def init_executor() -> None:
    global executor_task
    executor_task = asyncio.create_task(executor())

    async def cancel_executor() -> None:
        async def kill_executor() -> None:
            raise asyncio.CancelledError()

        try:
            await schedule_and_wait(kill_executor())
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await executor_task
            except asyncio.CancelledError:
                pass

    plugins.finalizer(cancel_executor)
    if client.is_ready():
        chans = [
            channel
            for guild in client.guilds
            for channel in guild.channels
            if isinstance(channel, (TextChannel, VoiceChannel))
        ]
        last_msgs, thread_last_msgs = take_snapshot(chans)
        schedule(process_ready(last_msgs, thread_last_msgs))
    fetch_task.run_once()


async def process_ready(last_msgs: Dict[int, int], thread_last_msgs: Dict[int, Dict[int, int]]) -> None:
    async with sessionmaker() as session:
        logger.debug("Looking for missing messages in on_ready")

        stmt = select(Channel)
        have_chans = set()
        for (chan,) in await session.execute(stmt):
            have_chans.add(chan.id)
            if chan.reachable:
                if chan.id not in last_msgs:
                    logger.debug("Channel {} is missing, marking unreachable".format(chan.id))
                    chan.reachable = False
            elif chan.id in last_msgs:
                chan.reachable = True
        for channel_id in last_msgs:
            if channel_id not in have_chans:
                channel = client.get_channel(channel_id)
                if not isinstance(channel, (TextChannel, VoiceChannel)):
                    continue
                subscribers = set()
                for sub in events:
                    subscribers.add(sub)
                if channel.guild.id in events_guild:
                    for sub in events_guild[channel.guild.id]:
                        subscribers.add(sub)
                logger.debug(
                    "Found new channel {}, adding for {}".format(
                        channel_id, ", ".join(repr(sub) for sub in subscribers)
                    )
                )
                session.add(Channel(guild_id=channel.guild.id, id=channel_id, reachable=True))
                archive_ts = await approx_archival_ts(channel)
                for sub in subscribers:
                    session.add(
                        ChannelState(
                            channel_id=channel_id,
                            subscriber=sub,
                            earliest_thread_archive_ts=archive_ts,
                            last_message_id=last_msgs[channel_id],
                        )
                    )
        await session.commit()

        stmt = (
            select(ChannelState)
            .join(ChannelState.channel)
            .where(Channel.reachable, ChannelState.subscriber.in_(list(fetch_map.keys())))
        )
        states = [state for state, in await session.execute(stmt) if state.channel_id in last_msgs]

        min_last_msgs: Dict[int, int] = {}
        for state in states:
            if state.channel_id not in min_last_msgs or state.last_message_id < min_last_msgs[state.channel_id]:
                min_last_msgs[state.channel_id] = state.last_message_id

        archived_threads: Dict[int, List[Thread]] = {channel_id: [] for channel_id in min_last_msgs}
        for channel_id in min_last_msgs:
            if channel_id not in last_msgs:
                continue
            channel = client.get_channel(channel_id)
            if not isinstance(channel, TextChannel):
                continue
            async for thread in channel.archived_threads(limit=None):
                assert thread.archive_timestamp is not None
                if thread.archive_timestamp < Object(min_last_msgs[channel_id]).created_at:
                    break
                if channel_id in thread_last_msgs and thread.id in thread_last_msgs[channel_id]:
                    continue
                archived_threads[channel_id].append(thread)
            logger.debug(
                "Found archived threads in {}: {}".format(
                    channel_id, ", ".join(str(thread.id) for thread in archived_threads[channel_id])
                )
            )

        for state in states:
            if state.channel_id in last_msgs and state.last_message_id < last_msgs[state.channel_id]:
                logger.debug(
                    "Requesting channel {} for {!r} from {} to {}".format(
                        state.channel_id, state.subscriber, state.last_message_id, last_msgs[state.channel_id]
                    )
                )
                session.add(
                    ChannelRequest(
                        channel_id=state.channel_id,
                        subscriber=state.subscriber,
                        after_snowflake=state.last_message_id + 1,
                        before_snowflake=last_msgs[state.channel_id] + 1,
                    )
                )
            if state.channel_id in thread_last_msgs:
                for thread_id, thread_last_msg in thread_last_msgs[state.channel_id].items():
                    if state.last_message_id < thread_last_msg:
                        logger.debug(
                            "Requesting thread {} in {} for {!r} from {} to {}".format(
                                thread_id, state.channel_id, state.subscriber, state.last_message_id, thread_last_msg
                            )
                        )
                        session.add(
                            ThreadRequest(
                                thread_id=thread_id,
                                channel_id=state.channel_id,
                                subscriber=state.subscriber,
                                after_snowflake=state.last_message_id + 1,
                                before_snowflake=thread_last_msg + 1,
                            )
                        )
            for thread in archived_threads[state.channel_id]:
                if thread.archive_timestamp < Object(state.last_message_id).created_at:
                    continue
                before = time_snowflake(thread.archive_timestamp + timedelta(milliseconds=1))
                if state.last_message_id < before - 1:
                    logger.debug(
                        "Requesting archived thread {} in {} for {!r} from {} to {}".format(
                            thread.id, state.channel_id, state.subscriber, state.last_message_id, before
                        )
                    )
                    session.add(
                        ThreadRequest(
                            thread_id=thread.id,
                            channel_id=state.channel_id,
                            subscriber=state.subscriber,
                            after_snowflake=state.last_message_id + 1,
                            before_snowflake=before,
                        )
                    )

            if state.channel_id in thread_last_msgs:
                max_thread_msg = max(thread_last_msgs[state.channel_id].values())
                if max_thread_msg > state.last_message_id:
                    state.last_message_id = max_thread_msg
            if state.channel_id in last_msgs:
                if last_msgs[state.channel_id] > state.last_message_id:
                    state.last_message_id = last_msgs[state.channel_id]

        await session.commit()
        fetch_task.run_once()


async def process_thread_unarchival(thread: Thread, ts: datetime) -> None:
    async with sessionmaker() as session:
        logger.debug("Processing unarchival of thread {} in {} (since {})".format(thread.id, thread.parent_id, ts))
        # include unreachable and unsubscribed just in case
        stmt = select(ChannelState).where(
            ChannelState.channel_id == thread.parent_id, ts < ChannelState.earliest_thread_archive_ts
        )
        for (state,) in await session.execute(stmt):
            before = time_snowflake(thread.archive_timestamp + timedelta(milliseconds=1))
            logger.debug(
                "Requesting unarchived thread {} in {} for {!r} up to {}".format(
                    thread.id, state.channel_id, state.subscriber, before
                )
            )
            session.add(
                ThreadRequest(
                    thread_id=thread.id,
                    channel_id=state.channel_id,
                    subscriber=state.subscriber,
                    after_snowflake=thread.id,
                    before_snowflake=before,
                )
            )
        await session.commit()
        fetch_task.run_once()


async def process_permission_update(channel_id: int) -> None:
    async with sessionmaker() as session:
        logger.debug("Marking channel {} reachable".format(channel_id))
        stmt = update(Channel).where(Channel.id == channel_id).values(reachable=True)
        await session.execute(stmt)
        await session.commit()
        fetch_task.run_once()


async def process_channel_creation(channel_id: int, guild_id: int) -> None:
    async with sessionmaker() as session:
        subscribers = set()
        for sub in events:
            subscribers.add(sub)
        if guild_id in events_guild:
            for sub in events_guild[guild_id]:
                subscribers.add(sub)
        session.add(Channel(guild_id=guild_id, id=channel_id, reachable=True))
        for sub in subscribers:
            session.add(
                ChannelState(
                    channel_id=channel_id, subscriber=sub, earliest_thread_archive_ts=None, last_message_id=channel_id
                )
            )
        await session.commit()


async def process_channel_deletion(channel_id: int) -> None:
    async with sessionmaker() as session:
        stmt = update(Channel).where(Channel.id == channel_id).values(reachable=False)
        await session.execute(stmt)
        await session.commit()


# TODO: something that chunks nearby messages (at least if they arrive faster than they are processed)
async def process_message(msg: Message) -> None:
    assert msg.guild is not None
    channel_id = msg.channel.parent_id if isinstance(msg.channel, Thread) else msg.channel.id
    guild_id = msg.guild.id
    subscribers = {}
    for sub, cb in events.items():
        subscribers[sub] = cb
    if guild_id in events_guild:
        for sub, cb in events_guild[guild_id].items():
            subscribers[sub] = cb
    if channel_id in events_channel:
        for sub, cb in events_channel[channel_id].items():
            subscribers[sub] = cb
    requests_added = False
    async with sessionmaker() as session:
        # stmt: Union[Select[Any], Update]
        subscriber_order = list(subscribers)
        results = await asyncio.gather(*(subscribers[sub]((msg,)) for sub in subscriber_order), return_exceptions=True)
        for sub, result in zip(subscriber_order, results):
            if isinstance(result, Exception):
                logger.error("Exception when calling callback for {!r}, will redeliver".format(sub), exc_info=result)
                stmt = select(1).where(ChannelState.channel_id == channel_id, ChannelState.subscriber == sub)
                if (await session.execute(stmt)).scalar():
                    if isinstance(msg.channel, Thread):
                        session.add(
                            ThreadRequest(
                                thread_id=msg.channel.id,
                                channel_id=channel_id,
                                subscriber=sub,
                                after_snowflake=msg.id,
                                before_snowflake=msg.id + 1,
                            )
                        )
                    else:
                        session.add(
                            ChannelRequest(
                                channel_id=channel_id,
                                subscriber=sub,
                                after_snowflake=msg.id,
                                before_snowflake=msg.id + 1,
                            )
                        )
                    requests_added = True

        stmt = (
            update(ChannelState)
            .where(ChannelState.channel_id == channel_id, ChannelState.subscriber.in_(list(subscribers)))
            .values(last_message_id=func.greatest(ChannelState.last_message_id, literal(msg.id, type_=BigInteger)))
        )
        await session.execute(stmt)
        await session.commit()
    if requests_added:
        fetch_task.run_once()


@cog
class MessageTracker(Cog):
    @Cog.listener()
    async def on_ready(self) -> None:
        chans = [
            channel
            for guild in client.guilds
            for channel in guild.channels
            if isinstance(channel, (TextChannel, VoiceChannel))
        ]
        last_msgs, thread_last_msgs = take_snapshot(chans)
        schedule(process_ready(last_msgs, thread_last_msgs))

    @Cog.listener()
    async def on_message(self, msg: Message) -> None:
        if isinstance(msg.channel, (TextChannel, VoiceChannel, Thread)):
            schedule(process_message(msg))

    @Cog.listener()
    async def on_thread_update(self, before: Thread, after: Thread) -> None:
        if before.archived and not after.archived:
            assert before.archive_timestamp is not None
            # If this fails to commit, or if we've missed the event we will have missed the start of the thread.
            # I don't know how to fix this.
            schedule(process_thread_unarchival(after, before.archive_timestamp))
        elif not before.archived and after.archived:
            assert after.archive_timestamp is not None
            ts = last_archival_times.get(after.parent_id)
            if ts is None or after.archive_timestamp > ts:
                last_archival_times[after.parent_id] = after.archive_timestamp

    @Cog.listener()
    async def on_guild_channel_update(self, before: GuildChannel, after: GuildChannel) -> None:
        if isinstance(after, (TextChannel, VoiceChannel)) and before.overwrites != after.overwrites:
            schedule(process_permission_update(after.id))

    @Cog.listener()
    async def on_guild_channel_create(self, channel: GuildChannel) -> None:
        if isinstance(channel, (TextChannel, VoiceChannel)):
            schedule(process_channel_creation(channel.id, channel.guild.id))

    @Cog.listener()
    async def on_guild_channel_delete(self, channel: GuildChannel) -> None:
        if isinstance(channel, (TextChannel, VoiceChannel)):
            schedule(process_channel_deletion(channel.id))


async def process_subscription(
    subscriber: str,
    event_dict: Dict[str, Callback],
    cb: Callback,
    last_msgs: Dict[int, int],
    thread_last_msgs: Dict[int, Dict[int, int]],
    retroactive: bool,
) -> None:
    async with sessionmaker() as session:
        stmt = select(ChannelState).join(ChannelState.channel).where(ChannelState.subscriber == subscriber)
        have_chans = set()
        for (state,) in await session.execute(stmt):
            if state.channel_id in last_msgs:
                have_chans.add(state.channel_id)
        for channel_id in last_msgs:
            if channel_id not in have_chans:
                channel = client.get_channel(channel_id)
                if not isinstance(channel, (TextChannel, VoiceChannel)):
                    continue
                logger.debug("Channel {} is now subscibed for {!r}".format(channel_id, subscriber))
                stmt = (
                    insert(Channel)
                    .values(guild_id=channel.guild.id, id=channel_id, reachable=True)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                await session.execute(stmt)

                archive_ts = await approx_archival_ts(channel) if retroactive else None
                session.add(
                    ChannelState(
                        channel_id=channel_id,
                        subscriber=subscriber,
                        earliest_thread_archive_ts=archive_ts,
                        last_message_id=last_msgs[channel_id],
                    )
                )
                if retroactive:
                    logger.debug(
                        "Retroactively querying channel {} before {} for {!r}".format(
                            channel_id, last_msgs[channel_id] + 1, subscriber
                        )
                    )
                    session.add(
                        ChannelRequest(
                            channel_id=channel_id,
                            subscriber=subscriber,
                            after_snowflake=channel_id,
                            before_snowflake=last_msgs[channel_id] + 1,
                        )
                    )
                    if channel_id in thread_last_msgs:
                        for thread_id, thread_last_msg in thread_last_msgs[channel_id].items():
                            logger.debug(
                                "Retroactively querying thread {} in channel {} before {} for {!r}".format(
                                    thread_id, channel_id, thread_last_msg, subscriber
                                )
                            )
                            session.add(
                                ThreadRequest(
                                    thread_id=thread_id,
                                    channel_id=channel_id,
                                    subscriber=subscriber,
                                    after_snowflake=thread_id,
                                    before_snowflake=thread_last_msg + 1,
                                )
                            )
        await session.commit()

        logger.debug("Looking for missing messages when subscribing {!r}".format(subscriber))
        stmt = (
            select(ChannelState)
            .join(ChannelState.channel)
            .where(Channel.reachable, ChannelState.subscriber == subscriber)
        )
        states = [state for state in (await session.execute(stmt)).scalars() if state.channel_id in last_msgs]

        old_last_msgs = {state.channel_id: state.last_message_id for state in states}
        archived_threads: Dict[int, List[Thread]] = {channel_id: [] for channel_id in last_msgs}

        async def find_archived_threads(channel_id: int) -> None:
            channel = client.get_channel(channel_id)
            if not isinstance(channel, TextChannel):
                return
            try:
                async for thread in channel.archived_threads(limit=None):
                    assert thread.archive_timestamp is not None
                    if channel_id in old_last_msgs:
                        if thread.archive_timestamp < Object(old_last_msgs[channel_id]).created_at:
                            break
                    if channel_id in thread_last_msgs and thread.id in thread_last_msgs[channel_id]:
                        continue
                    archived_threads[channel_id].append(thread)
            except discord.Forbidden:
                return
            logger.debug(
                "Found archived threads in {}: {}".format(
                    channel_id, ", ".join(str(thread.id) for thread in archived_threads[channel_id])
                )
            )

        await asyncio.gather(*(retry(lambda: find_archived_threads(channel_id)) for channel_id in last_msgs))

        for state in states:
            if state.channel_id in last_msgs and state.last_message_id < last_msgs[state.channel_id]:
                logger.debug(
                    "Requesting channel {} for {!r} from {} to {}".format(
                        state.channel_id, subscriber, state.last_message_id, last_msgs[state.channel_id]
                    )
                )
                session.add(
                    ChannelRequest(
                        channel_id=state.channel_id,
                        subscriber=subscriber,
                        after_snowflake=state.last_message_id + 1,
                        before_snowflake=last_msgs[state.channel_id] + 1,
                    )
                )
            if state.channel_id in thread_last_msgs:
                for thread_id, thread_last_msg in thread_last_msgs[state.channel_id].items():
                    if state.last_message_id < thread_last_msg:
                        logger.debug(
                            "Requesting thread {} in {} for {!r} from {} to {}".format(
                                thread_id, state.channel_id, subscriber, state.last_message_id, thread_last_msg
                            )
                        )
                        session.add(
                            ThreadRequest(
                                thread_id=thread_id,
                                channel_id=state.channel_id,
                                subscriber=subscriber,
                                after_snowflake=state.last_message_id + 1,
                                before_snowflake=thread_last_msg + 1,
                            )
                        )
            for thread in archived_threads[state.channel_id]:
                before = time_snowflake(thread.archive_timestamp + timedelta(milliseconds=1))
                if state.last_message_id < before - 1:
                    logger.debug(
                        "Requesting archived thread {} in {} for {!r} from {} to {}".format(
                            thread.id, state.channel_id, subscriber, state.last_message_id, before
                        )
                    )
                    session.add(
                        ThreadRequest(
                            thread_id=thread.id,
                            channel_id=state.channel_id,
                            subscriber=subscriber,
                            after_snowflake=state.last_message_id + 1,
                            before_snowflake=before,
                        )
                    )

            if state.channel_id in thread_last_msgs:
                max_thread_msg = max(thread_last_msgs[state.channel_id].values())
                if max_thread_msg > state.last_message_id:
                    state.last_message_id = max_thread_msg
            if state.channel_id in last_msgs:
                if last_msgs[state.channel_id] > state.last_message_id:
                    state.last_message_id = last_msgs[state.channel_id]

        await session.commit()
        fetch_map[subscriber] = cb
        fetch_task.run_once()
        event_dict[subscriber] = cb


async def subscribe(
    name: str,
    channels: Optional[Union[Guild, TextChannel, VoiceChannel]],
    cb: Callback,
    *,
    missing: bool,
    retroactive: bool,
) -> None:
    """
    Subscribe the callback to be called for all messages in given channel, given guild, or all guilds. If missing is
    True, the callback will also be called whenever we reconnect to discord and fetch messages we missed. If retroactive
    is also True, the callback will also be called retroactively for all messages in the history. The
    missing/retroactive fetching status (as well as the list of channels) is preserved across restarts, and the callback
    may be called for channels registered in previous restarts as well. The callbacks are identified by their names,
    and when registering the same name multiple times, either of the provided functions could be called.
    """
    if channels is None:
        event_dict = events
    elif isinstance(channels, Guild):
        event_dict = events_guild.setdefault(channels.id, {})
    else:
        event_dict = events_channel.setdefault(channels.id, {})
    if missing or retroactive:
        if channels is None:
            chans = [
                channel
                for guild in client.guilds
                for channel in guild.channels
                if isinstance(channel, (TextChannel, VoiceChannel))
            ]
        elif isinstance(channels, Guild):
            chans = [channel for channel in channels.channels if isinstance(channel, (TextChannel, VoiceChannel))]
        else:
            chans = [channels]
        last_msgs, thread_last_msgs = take_snapshot(chans)
        await schedule_and_wait(process_subscription(name, event_dict, cb, last_msgs, thread_last_msgs, retroactive))
        # TODO: If we have on_message -> subscribe, then this could get queued before the respective process_message,
        # which would be a problem, because last_msgs will contain that message, meaning it would be fetched, and at
        # the same time process_message will notify the subscriber
    else:
        event_dict[name] = cb


async def process_unsubscription(subscriber: str, event_dict: Dict[str, Callback]) -> None:
    fetch_map.pop(subscriber, None)
    event_dict.pop(subscriber, None)


async def unsubscribe(name: str, channels: Optional[Union[Guild, TextChannel, VoiceChannel]]) -> None:
    if channels is None:
        event_dict = events
    elif isinstance(channels, Guild):
        event_dict = events_guild.setdefault(channels.id, {})
    else:
        event_dict = events_channel.setdefault(channels.id, {})
    await schedule_and_wait(process_unsubscription(name, event_dict))
    # TODO: same issue as with subscribing
