import asyncio
import io
import logging
import sys
import threading
import types
from typing import List, Optional, Protocol, cast

import discord

import bot.client
import plugins
import util.asyncio
import util.db.kv
import util.discord

class LoggingConf(Protocol):
    channel: Optional[str]

conf: LoggingConf
logger: logging.Logger = logging.getLogger(__name__)

class DiscordHandler(logging.Handler):
    __slots__ = "queue", "lock"
    queue: List[str]
    lock: threading.Lock

    def __init__(self, level: int = logging.NOTSET):
        self.queue = []
        self.lock = threading.Lock() # just in case
        return super().__init__(level)

    def queue_pop(self) -> Optional[str]:
        with self.lock:
            if len(self.queue) == 0:
                return None
            return self.queue.pop(0)

    async def log_discord(self, chan_id: int, client: discord.Client) -> None:
        try:
            message = ""
            while (text := self.queue_pop()) is not None:
                codeblock = util.discord.format("{!b:py}", text)
                if len(message) + len(codeblock) > 2000:
                    if len(message) > 0:
                        await util.discord.ChannelById(client, chan_id).send(message)
                    if len(codeblock) > 2000:
                        await util.discord.ChannelById(client, chan_id).send(
                            file=discord.File(io.BytesIO(text.encode("utf8")), filename="log.txt"))
                        message = ""
                    else:
                        message = codeblock
                else:
                    message += codeblock
            if len(message) > 0:
                await util.discord.ChannelById(client, chan_id).send(message)
        except:
            logger.critical("Could not report exception to Discord", exc_info=True, extra={"no_discord": True})

    def emit(self, record: logging.LogRecord) -> None:
        if hasattr(record, "no_discord"):
            return
        try:
            if asyncio.get_event_loop().is_closed():
                return
        except:
            return

        if conf.channel is None:
            return
        try:
            chan_id = int(conf.channel)
        except ValueError:
            return

        client = bot.client.client
        if client.is_closed():
            return

        text = self.format(record)

        # Check the traceback for whether we are nested inside log_discord,
        # as a last resort measure
        frame: Optional[types.FrameType] = sys._getframe()
        while frame:
            if frame.f_code == self.log_discord.__code__:
                del frame
                return
            frame = frame.f_back
        del frame

        with self.lock:
            if self.queue:
                self.queue.append(text)
            else:
                self.queue.append(text)
                util.asyncio.run_async(self.log_discord, chan_id, client)

@plugins.init
async def init() -> None:
    global conf
    conf = cast(LoggingConf, await util.db.kv.load(__name__))

    handler: logging.Handler = DiscordHandler(logging.ERROR)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(handler)

    def finalizer() -> None:
        logging.getLogger().removeHandler(handler)
    plugins.finalizer(finalizer)
