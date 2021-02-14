import asyncio
import weakref
import plugins
import util.discord

class FilteredQueue(asyncio.Queue):
    """An async queue that only accepts values that match the given filter"""

    __slots__ = "filter"

    def __init__(self, maxsize=0, *, loop=None, filter=None):
        self.filter = filter if filter != None else lambda _: True
        return super().__init__(maxsize, loop=loop)

    async def put(self, value):
        if self.filter(value):
            return await super().put(value)

    def put_nowait(self, value):
        if self.filter(value):
            return super().put_nowait(value)

reaction_queues = weakref.WeakSet()

class ReactionMonitor:
    __slots__ = ("loop", "queue", "end_time", "timeout_each")

    def __init__(self, *, loop=None, filter=None, guild_id=None,
        channel_id=None, message_id=None, author_id=None, event=None,
        emoji=None, timeout_each=None, timeout_total=None):

        self.loop = loop if loop != None else asyncio.get_running_loop()

        # for "add" and "remove", RawReactionActionEvent has the fields
        #    guild_id, channel_id, message_id, author_id, emoji
        # for "clear", RawReactionClearEvent has the fields
        #    guild_id, channel_id, message_id
        # for "clear_emoji", RawReactionClearEmojiEvent has the fields
        #    guild_id, channel_id, message_id, emoji
        def event_filter(ev, payload):
            return ((guild_id == None or payload.guild_id == guild_id)
                and (channel_id == None or payload.channel_id == channel_id)
                and (message_id == None or payload.message_id == message_id)
                and (author_id == None or not hasattr(payload, "user_id")
                    or payload.user_id == author_id)
                and (event == None or ev == event)
                and (emoji == None or not hasattr(payload, "emoji")
                    or payload.emoji == emoji
                    or payload.emoji.name == emoji
                    or payload.emoji.id == emoji)
                and (filter == None or filter(ev, payload)))

        self.timeout_each = timeout_each
        if timeout_total == None:
            self.end_time = None
        else:
            self.end_time = self.loop.time() + timeout_total

        def queue_filter(value):
            return isinstance(value, BaseException) or event_filter(*value)
        self.queue = FilteredQueue(maxsize=0,
            loop=self.loop, filter=queue_filter)

    def __enter__(self):
        reaction_queues.add(self.queue)
        return self

    def __exit__(self, exc_type, exc_val, tb):
        reaction_queues.discard(self.queue)

    # ugly arcane hack
    @(lambda f: lambda self: f(self).__await__())
    async def __await__(self):
        timeout = self.timeout_each
        if self.end_time != None:
            remaining = self.end_time - self.loop.time()
            if timeout == None or timeout > remaining:
                timeout = remaining
        value = await asyncio.wait_for(self.queue.get(), timeout)
        if isinstance(value, BaseException):
            raise value
        return value

    async def __aiter__(self):
        while True:
            try:
                yield await self
            except asyncio.TimeoutError:
                return

    def cancel(self, exc=None):
        if exc == None:
            exc = asyncio.CancelledError()
        try:
            raise exc
        except BaseException as exc:
            self.queue.put_nowait(exc)

def deliver_event(ev, payload):
    gen = reaction_queues.__iter__()
    def cont_deliver():
        try:
            for queue in gen:
                queue.put_nowait((ev, payload))
        except:
            cont_deliver()
            raise
    cont_deliver()

@util.discord.event("raw_reaction_add")
async def reaction_add(payload):
    deliver_event("add", payload)

@util.discord.event("raw_reaction_remove")
async def reaction_remove(payload):
    deliver_event("remove", payload)

@util.discord.event("raw_reaction_clear")
async def reaction_clear(payload):
    deliver_event("clear", payload)

@util.discord.event("raw_reaction_clear_emoji")
async def reaction_clear_emoji(payload):
    deliver_event("clear_emoji", payload)
