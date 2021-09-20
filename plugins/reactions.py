from __future__ import annotations
import asyncio
import weakref
import discord
import discord.ext.commands
from typing import (Tuple, Union, Optional, Literal, AsyncIterator, Callable, Any, Generic, TypeVar, ContextManager,
    overload, cast)
import plugins
import plugins.cogs
import util.asyncio
import util.discord

T = TypeVar("T")

class FilteredQueue(asyncio.Queue[T], Generic[T]):
    """An async queue that only accepts values that match the given filter"""

    __slots__ = "filter"

    def __init__(self, maxsize: int = 0, *, loop: Optional[asyncio.AbstractEventLoop] = None,
        filter: Optional[Callable[[T], bool]] = None):
        self.filter: Callable[[T], bool]
        self.filter = filter if filter is not None else lambda _: True
        return super().__init__(maxsize, loop=loop)

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
        self.queue = FilteredQueue(maxsize=0, loop=self.loop, filter=queue_filter)

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

@plugins.cogs.cog
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
