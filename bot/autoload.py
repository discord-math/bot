"""
Automatically load certain plugins after bot initialization.
"""

import logging
from typing import Awaitable, Iterator, Optional, Protocol, Set, Tuple, cast

import bot.main_tasks
import plugins
import util.asyncio
import util.db.kv

class AutoloadConf(Awaitable[None], Protocol):
    def __getitem__(self, key: str) -> Optional[bool]: ...
    def __setitem__(self, key: str, val: Optional[bool]) -> None: ...
    def __iter__(self) -> Iterator[Tuple[str]]: ...

logger: logging.Logger = logging.getLogger(__name__)
conf: AutoloadConf

@plugins.init
async def init() -> None:
    global conf
    conf = cast(AutoloadConf, await util.db.kv.load(__name__))

    async def autoload() -> None:
        if (manager := plugins.PluginManager.of(__name__)) is None:
            logger.error("No plugin manager")
            return
        for name, in conf:
            try:
                # Sidestep plugin dependency tracking
                await manager.load(name)
            except:
                logger.critical("Exception during autoload of {}".format(name), exc_info=True)
    bot.main_tasks.create_task(autoload(), name="Plugin autoload")

def get_autoload() -> Set[str]:
    return {plugin for plugin, in conf}

async def set_autoload(plugin: str, status: bool) -> None:
    conf[plugin] = status or None
    await conf
