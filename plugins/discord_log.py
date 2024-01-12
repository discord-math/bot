import asyncio
import logging
import sys
from threading import Lock
from types import FrameType
from typing import TYPE_CHECKING, Iterator, List, Literal, Optional, Union, cast

from discord import Client
from discord.ext.commands import command
from sqlalchemy import BigInteger, Computed
from sqlalchemy.ext.asyncio import async_sessionmaker
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column

from bot.client import client
from bot.commands import Context
from bot.config import plugin_config_command
import plugins
import util.db.kv
from util.discord import CodeItem, PartialTextChannelConverter, chunk_messages, format


registry = sqlalchemy.orm.registry()
sessionmaker = async_sessionmaker(util.db.engine, expire_on_commit=False)


@registry.mapped
class GlobalConfig:
    __tablename__ = "syslog_config"
    id: Mapped[int] = mapped_column(BigInteger, Computed("0"), primary_key=True)
    channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    if TYPE_CHECKING:

        def __init__(self, *, id: int = ..., channel_id: Optional[int] = ...) -> None:
            ...


conf: GlobalConfig
logger: logging.Logger = logging.getLogger(__name__)


class DiscordHandler(logging.Handler):
    __slots__ = "queue", "lock"
    queue: List[str]
    thread_lock: Lock

    def __init__(self, level: int = logging.NOTSET):
        self.queue = []
        self.thread_lock = Lock()  # just in case
        return super().__init__(level)

    def queue_pop(self) -> Optional[str]:
        with self.thread_lock:
            if len(self.queue) == 0:
                return None
            return self.queue.pop(0)

    async def log_discord(self, chan_id: int, client: Client) -> None:
        try:

            def fill_items() -> Iterator[CodeItem]:
                while (text := self.queue_pop()) is not None:
                    yield CodeItem(text, language="py", filename="log.txt")

            for content, files in chunk_messages(fill_items()):
                await client.get_partial_messageable(chan_id).send(content, files=files)
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

        if client.is_closed():
            return

        if conf.channel_id is None:
            return

        text = self.format(record)

        # Check the traceback for whether we are nested inside log_discord,
        # as a last resort measure
        frame: Optional[FrameType] = sys._getframe()
        while frame:
            if frame.f_code == self.log_discord.__code__:
                del frame
                return
            frame = frame.f_back
        del frame

        with self.thread_lock:
            if self.queue:
                self.queue.append(text)
            else:
                self.queue.append(text)
                asyncio.create_task(self.log_discord(conf.channel_id, client), name="Logging to Discord")


@plugins.init
async def init() -> None:
    global conf
    await util.db.init(util.db.get_ddl(registry.metadata.create_all))
    async with sessionmaker() as session:
        c = await session.get(GlobalConfig, 0)
        if not c:
            c = GlobalConfig(channel_id=cast(Optional[int], (await util.db.kv.load(__name__)).channel))
            session.add(c)
            await session.commit()
        conf = c

    handler: logging.Handler = DiscordHandler(logging.ERROR)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(handler)

    def finalizer() -> None:
        logging.getLogger().removeHandler(handler)

    plugins.finalizer(finalizer)


@plugin_config_command
@command("syslog")
async def config(ctx: Context, channel: Optional[Union[Literal["None"], PartialTextChannelConverter]]) -> None:
    global conf
    async with sessionmaker() as session:
        c = await session.get(GlobalConfig, 0)
        assert c
        if channel is None:
            await ctx.send("None" if c.channel_id is None else format("{!c}", conf.channel_id))
        else:
            c.channel_id = None if channel == "None" else channel.id
            await session.commit()
            conf = c
            await ctx.send("\u2705")
