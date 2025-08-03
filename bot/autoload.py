"""
Automatically load certain plugins after bot initialization.
"""

import logging
from typing import TYPE_CHECKING

from sqlalchemy import TEXT, BigInteger, select
from sqlalchemy.ext.asyncio import AsyncSession
import sqlalchemy.orm
from sqlalchemy.orm import Mapped, mapped_column

import bot.main_tasks
import plugins
import util.db.kv


registry = sqlalchemy.orm.registry()


@registry.mapped
class AutoloadedPlugin:
    __tablename__ = "autoload"

    name: Mapped[str] = mapped_column(TEXT, primary_key=True)
    order: Mapped[int] = mapped_column(BigInteger, nullable=False)

    if TYPE_CHECKING:

        def __init__(self, *, name: str, order: int) -> None: ...


logger: logging.Logger = logging.getLogger(__name__)


@plugins.init
async def init() -> None:
    if (manager := plugins.PluginManager.of(__name__)) is None:
        logger.error("No plugin manager")
        return
    await util.db.init(util.db.get_ddl(registry.metadata.create_all))

    async def autoload() -> None:
        async with AsyncSession(util.db.engine) as session:
            conf = await util.db.kv.load(__name__)
            for key in [key for key, in conf]:
                session.add(AutoloadedPlugin(name=key, order=0))
                conf[key] = None
            await session.commit()
            await conf

            stmt = select(AutoloadedPlugin).order_by(AutoloadedPlugin.order)
            for plugin in (await session.execute(stmt)).scalars():
                try:
                    # Sidestep plugin dependency tracking
                    await manager.load(plugin.name)
                except:
                    logger.critical("Exception during autoload of {}".format(plugin.name), exc_info=True)

    bot.main_tasks.create_task(autoload(), name="Plugin autoload")
