import logging
import asyncio
import sys
import threading
import types
from typing import List, Optional, Protocol, cast
import discord
import plugins
import discord_client
import util.discord
import util.db.kv
import util.asyncio

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

    async def log_discord(self, chan_id: int, client: discord.Client) -> None:
        with self.lock:
            queue = self.queue
            self.queue = []
        try:
            await util.discord.ChannelById(client, chan_id).send("\n".join(queue))
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

        client = discord_client.client
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

    @plugins.finalizer
    def finalizer() -> None:
        logging.getLogger().removeHandler(handler)
