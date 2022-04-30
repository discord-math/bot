import logging
import log_setup

logger: logging.Logger = logging.getLogger(__name__)

try:
    import asyncio

    import util.restart
    import plugins
    import discord_client

    async def async_main() -> None:
        try:
            await plugins.load("plugins.autoload")
            await discord_client.main_task()
        except:
            logger.critical("Exception during main event loop", exc_info=True)
        finally:
            logger.info("Unloading all plugins")
            await plugins.unload_all()
            logger.info("Exiting main loop")

    asyncio.run(async_main())
except:
    logger.critical("Exception in main", exc_info=True)
    raise
