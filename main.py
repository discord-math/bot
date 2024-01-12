import logging

import log_setup  # type: ignore


logger: logging.Logger = logging.getLogger(__name__)

try:
    import asyncio

    import plugins

    manager = plugins.PluginManager(["bot", "plugins", "util"])
    manager.register()

    async def async_main() -> None:
        main_tasks = None
        try:
            main_tasks = await manager.load("bot.main_tasks")
            await manager.load("bot.autoload")
            await main_tasks.wait()
        except:
            logger.critical("Exception during main event loop", exc_info=True)
        finally:
            logger.info("Unloading all plugins")
            await manager.unload_all()
            logger.info("Cancelling main tasks")
            if main_tasks:
                main_tasks.cancel()
                await main_tasks.wait_all()
                logger.info("Exiting main loop")

    asyncio.run(async_main())
except:
    logger.critical("Exception in main", exc_info=True)
    raise
