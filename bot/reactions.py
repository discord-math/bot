from __future__ import annotations

import asyncio
from typing import (Any, AsyncIterator, Callable, ContextManager, Dict, Generic, Literal, Optional, Tuple, TypeVar,
    Union, cast, overload)
import weakref

import discord
import discord.ext.commands

import bot.client
import bot.cogs
import util.asyncio
import util.discord

T = TypeVar("T")

class FilteredQueue(asyncio.Queue[T], Generic[T]):
    """An async queue that only accepts values that match the given filter"""

    __slots__ = "filter"

    def __init__(self, maxsize: int = 0, *, filter: Optional[Callable[[T], bool]] = None):
        self.filter: Callable[[T], bool]
        self.filter = filter if filter is not None else lambda _: True
        return super().__init__(maxsize)

    async def put(self, value: T) -> None:
        if self.filter(value):
            return await super().put(value)

    def put_nowait(self, value: T) -> None:
        if self.filter(value):
            return super().put_nowait(value)

ReactionEvent = Union[discord.RawReactionActionEvent, discord.RawReactionClearEvent, discord.RawReactionClearEmojiEvent]

reaction_queues: weakref.WeakSet[FilteredQueue[Union[BaseException, Tuple[str, ReactionEvent]]]]
reaction_queues = weakref.WeakSet()

class ReactionMonitor(ContextManager['ReactionMonitor[T]'], Generic[T]):
    __slots__ = ("loop", "queue", "end_time", "timeout_each")
    loop: asyncio.AbstractEventLoop
    queue: FilteredQueue[Union[BaseException, Tuple[str, ReactionEvent]]]
    end_time: Optional[float]
    timeout_each: Optional[float]

    @overload
    def __init__(self: ReactionMonitor[discord.RawReactionActionEvent], *, event: Literal["add", "remove"],
        filter: Optional[Callable[[str, discord.RawReactionActionEvent], bool]] = None,
        guild_id: Optional[int] = None, channel_id: Optional[int] = None, message_id: Optional[int] = None,
        author_id: Optional[int] = None, emoji: Optional[Union[discord.PartialEmoji, discord.Emoji, str, int]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None, timeout_each: Optional[float] = None,
        timeout_total: Optional[float] = None): ...
    @overload
    def __init__(self: ReactionMonitor[discord.RawReactionClearEvent], *, event: Literal["clear"],
        filter: Optional[Callable[[str, discord.RawReactionClearEvent], bool]] = None,
        guild_id: Optional[int] = None, channel_id: Optional[int] = None, message_id: Optional[int] = None,
        author_id: Optional[int] = None, emoji: Optional[Union[discord.PartialEmoji, discord.Emoji, str, int]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None, timeout_each: Optional[float] = None,
        timeout_total: Optional[float] = None): ...
    @overload
    def __init__(self: ReactionMonitor[discord.RawReactionClearEmojiEvent], *, event: Literal["clear_emoji"],
        filter: Optional[Callable[[str, discord.RawReactionClearEmojiEvent], bool]] = None,
        guild_id: Optional[int] = None, channel_id: Optional[int] = None, message_id: Optional[int] = None,
        author_id: Optional[int] = None, emoji: Optional[Union[discord.PartialEmoji, discord.Emoji, str, int]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None, timeout_each: Optional[float] = None,
        timeout_total: Optional[float] = None): ...
    @overload
    def __init__(self: ReactionMonitor[ReactionEvent], *, event: None = None,
        filter: Optional[Callable[[str, ReactionEvent], bool]] = None,
        guild_id: Optional[int] = None, channel_id: Optional[int] = None, message_id: Optional[int] = None,
        author_id: Optional[int] = None, emoji: Optional[Union[discord.PartialEmoji, discord.Emoji, str, int]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None, timeout_each: Optional[float] = None,
        timeout_total: Optional[float] = None): ...
    def __init__(self: ReactionMonitor[Any], *, event: Optional[str] = None,
        filter: Optional[Callable[[str, Any], bool]] = None,
        guild_id: Optional[int] = None, channel_id: Optional[int] = None, message_id: Optional[int] = None,
        author_id: Optional[int] = None, emoji: Optional[Union[discord.PartialEmoji, discord.Emoji, str, int]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None, timeout_each: Optional[float] = None,
        timeout_total: Optional[float] = None):

        self.loop = loop if loop is not None else asyncio.get_running_loop()

        # for "add" and "remove", RawReactionActionEvent has the fields
        #    guild_id, channel_id, message_id, author_id, emoji
        # for "clear", RawReactionClearEvent has the fields
        #    guild_id, channel_id, message_id
        # for "clear_emoji", RawReactionClearEmojiEvent has the fields
        #    guild_id, channel_id, message_id, emoji
        def event_filter(ev: str, payload: ReactionEvent) -> bool:
            return ((guild_id is None or payload.guild_id == guild_id)
                and (channel_id is None or payload.channel_id == channel_id)
                and (message_id is None or payload.message_id == message_id)
                and (author_id is None or not hasattr(payload, "user_id")
                    or payload.user_id == author_id) # type: ignore
                and (event is None or ev == event)
                and (emoji is None or not hasattr(payload, "emoji")
                    or payload.emoji == emoji # type: ignore
                    or payload.emoji.name == emoji # type: ignore
                    or payload.emoji.id == emoji) # type: ignore
                and (filter is None or filter(ev, payload)))

        self.timeout_each = timeout_each
        if timeout_total is None:
            self.end_time = None
        else:
            self.end_time = self.loop.time() + timeout_total

        def queue_filter(value: Union[BaseException, Tuple[str, ReactionEvent]]) -> bool:
            return isinstance(value, BaseException) or event_filter(*value)
        self.queue = FilteredQueue(maxsize=0, filter=queue_filter)

    def __enter__(self) -> ReactionMonitor[T]:
        reaction_queues.add(self.queue)
        return self

    def __exit__(self, exc_type, exc_val, tb) -> None: # type: ignore
        reaction_queues.discard(self.queue)

    @util.asyncio.__await__
    async def __await__(self) -> Tuple[str, T]:
        timeout = self.timeout_each
        if self.end_time is not None:
            remaining = self.end_time - self.loop.time()
            if timeout is None or timeout > remaining:
                timeout = remaining
        value = await asyncio.wait_for(self.queue.get(), timeout)
        if isinstance(value, BaseException):
            raise value
        return cast(Tuple[str, T], value)

    async def __aiter__(self) -> AsyncIterator[Tuple[str, ReactionEvent]]:
        while True:
            try:
                yield await self
            except asyncio.TimeoutError:
                return

    def cancel(self, exc: Optional[BaseException] = None) -> None:
        if exc is None:
            exc = asyncio.CancelledError()
        try:
            raise exc
        except BaseException as exc:
            self.queue.put_nowait(exc)

def deliver_event(ev: str, payload: ReactionEvent) -> None:
    gen = reaction_queues.__iter__()
    def cont_deliver() -> None:
        try:
            for queue in gen:
                queue.put_nowait((ev, payload))
        except:
            cont_deliver()
            raise
    cont_deliver()

@bot.cogs.cog
class Reactions(discord.ext.commands.Cog):
    @discord.ext.commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        deliver_event("add", payload)

    @discord.ext.commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        deliver_event("remove", payload)

    @discord.ext.commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        deliver_event("clear", payload)

    @discord.ext.commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        deliver_event("clear_emoji", payload)

def emoji_key(emoji: Union[discord.Emoji, discord.PartialEmoji, str]) -> Union[str, int]:
    if isinstance(emoji, str):
        return emoji
    elif emoji.id is None:
        return emoji.name
    else:
        return emoji.id

async def get_reaction(msg: discord.Message, user: discord.abc.Snowflake,
    reactions: Dict[Union[discord.Emoji, discord.PartialEmoji, str], T], *,
    timeout: Optional[float] = None, unreact: bool = True) -> Optional[T]:
    assert bot.client.client.user is not None
    reacts = {emoji_key(key): value for key, value in reactions.items()}
    with ReactionMonitor(channel_id=msg.channel.id, message_id=msg.id, author_id=user.id,
        event="add", filter=lambda _, p: emoji_key(p.emoji) in reacts, timeout_each=timeout) as mon:
        try:
            await asyncio.gather(*(msg.add_reaction(key) for key in reactions))
        except (discord.NotFound, discord.Forbidden):
            pass
        try:
            _, payload = await mon
        except asyncio.TimeoutError:
            return None
    if unreact:
        try:
            await asyncio.gather(*(msg.remove_reaction(key, bot.client.client.user)
                for key in reactions if emoji_key(key) != emoji_key(payload.emoji)))
        except (discord.NotFound, discord.Forbidden):
            pass
    return reacts.get(emoji_key(payload.emoji))

async def get_input(msg: discord.Message, user: discord.abc.Snowflake,
    reactions: Dict[Union[discord.Emoji, discord.PartialEmoji, str], T], *,
    timeout: Optional[float] = None, unreact: bool = True) -> Optional[Union[T, discord.Message]]:
    assert bot.client.client.user is not None
    reacts = {emoji_key(key): value for key, value in reactions.items()}
    with ReactionMonitor(channel_id=msg.channel.id, message_id=msg.id, author_id=user.id,
        event="add", filter=lambda _, p: emoji_key(p.emoji) in reacts, timeout_each=timeout) as mon:
        try:
            await asyncio.gather(*(msg.add_reaction(key) for key in reactions))
        except (discord.NotFound, discord.Forbidden):
            pass
        msg_task = asyncio.create_task(bot.client.client.wait_for("message",
            check=lambda m: m.channel == msg.channel and m.author.id == user.id))
        reaction_task = asyncio.ensure_future(mon)
        try:
            done, _ = await asyncio.wait((msg_task, reaction_task), timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)
        except asyncio.TimeoutError:
            return None
    if msg_task in done:
        reaction_task.cancel()
        if unreact:
            try:
                await asyncio.gather(*(msg.remove_reaction(key, bot.client.client.user) for key in reactions))
            except (discord.NotFound, discord.Forbidden):
                pass
        return msg_task.result()
    elif reaction_task in done:
        msg_task.cancel()
        _, payload = reaction_task.result()
        if unreact:
            try:
                await asyncio.gather(*(msg.remove_reaction(key, bot.client.client.user)
                    for key in reactions if emoji_key(key) != emoji_key(payload.emoji)))
            except (discord.NotFound, discord.Forbidden):
                pass
        return reacts.get(emoji_key(payload.emoji))
    else:
        return None
