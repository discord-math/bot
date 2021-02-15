import logging
import asyncio
import sys
import threading
import plugins
import discord_client
import util.discord
import util.db.kv

logger = logging.getLogger(__name__)
conf = util.db.kv.Config(__name__)

class DiscordHandler(logging.Handler):
    __slots__ = "queue", "lock"

    def __init__(self, level=logging.NOTSET):
        self.queue = []
        self.lock = threading.Lock() # just in case
        return super().__init__(level)

    async def log_discord(self, chan_id, client):
        with self.lock:
            queue = self.queue
            self.queue = []
        try:
            await util.discord.ChannelById(client, chan_id).send(
                "\n".join(queue))
        except:
            logger.critical("Could not report exception to Discord",
                exc_info=True, extra={"no_discord":True})

    def emit(self, record):
        if hasattr(record, "no_discord"):
            return
        try:
            if asyncio.get_event_loop().is_closed():
                return
        except:
            return

        chan_id = conf.channel
        if chan_id == None:
            return
        try:
            chan_id = int(chan_id)
        except ValueError:
            return

        client = discord_client.client
        if client.is_closed():
            return

        text = self.format(record)

        # Check the traceback for whether we are nested inside log_discord,
        # as a last resort measure
        frame = sys._getframe()
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
                asyncio.get_event_loop().create_task(
                    self.log_discord(chan_id, client))

handler = DiscordHandler(logging.ERROR)
handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
logging.getLogger().addHandler(handler)

@plugins.finalizer
def finalizer():
    logging.getLogger().removeHandler(handler)
