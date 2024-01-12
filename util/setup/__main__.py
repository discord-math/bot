import asyncio
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateSchema

import plugins


logging.basicConfig()
logging.getLogger().setLevel(logging.INFO)

manager = plugins.PluginManager(["bot", "util"])
manager.register()

import bot.acl
import bot.autoload
import bot.commands
import util.db


async def async_main() -> None:
    async with AsyncSession(util.db.engine) as session:
        logging.info("Connecting to database")
        await session.execute(select(1))
        logging.info("Creating schema for bot.acl")
        await util.db.init_for(
            "bot.acl", util.db.get_ddl(CreateSchema("permissions"), bot.acl.registry.metadata.create_all)
        )
        logging.info("Creating schema for bot.commands")
        await util.db.init_for("bot.commands", util.db.get_ddl(bot.commands.registry.metadata.create_all))
        logging.info("Creating schema for bot.autoload")
        await util.db.init_for("bot.autoload", util.db.get_ddl(bot.autoload.registry.metadata.create_all))

        logging.info("Deleting ACLs")
        await session.execute(delete(bot.acl.CommandPermissions))
        await session.execute(delete(bot.acl.ActionPermissions))
        await session.execute(delete(bot.acl.ACL))
        admin_id = await asyncio.get_event_loop().run_in_executor(
            None, lambda: int(input("Input your Discord user ID: "))
        )
        logging.info('Creating "admin" ACL assigned to {}'.format(admin_id))
        session.add(bot.acl.ACL(name="admin", data=bot.acl.UserACL(admin_id).serialize(), meta="admin"))
        session.add(bot.acl.ActionPermissions(name="acl_override", acl="admin"))

        logging.info("Deleting global command config")
        await session.execute(delete(bot.commands.GlobalConfig))
        prefix = await asyncio.get_event_loop().run_in_executor(None, lambda: input("Input command prefix: "))
        logging.info("Creating global command config with prefix {!r}".format(prefix))
        session.add(bot.commands.GlobalConfig(prefix=prefix))

        logging.info("Deleting autoload")
        await session.execute(delete(bot.autoload.AutoloadedPlugin))
        autoloaded = ["plugins.eval", "plugins.bot_manager", "plugins.db_manager"]
        logging.info("Adding {!r} to autoload".format(autoloaded))
        for p in autoloaded:
            session.add(bot.autoload.AutoloadedPlugin(name=p, order=0))

        logging.info("Committing transaction")
        await session.commit()

    await manager.unload_all()


asyncio.run(async_main())
