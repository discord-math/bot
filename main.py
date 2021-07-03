import logging
import log_setup

logger: logging.Logger = logging.getLogger(__name__)

try:
    import asyncio

    import util.restart
    import plugins
    import discord_client

    loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()

    async def async_main() -> None:
        await plugins.load("plugins.autoload")
        await discord_client.main_task()

    try:
        loop.run_until_complete(async_main())
    except:
        logger.critical("Exception during main event loop", exc_info=True)
    finally:
        logger.info("Finalizing event loop")
        tasks = asyncio.all_tasks(loop=loop)
        for task in tasks:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        for task in tasks:
            if not task.cancelled() and task.exception() is not None:
                logger.critical("Unhandled exception in task", exc_info=task.exception())
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
except:
    logger.critical("Exception in main", exc_info=True)
    raise
