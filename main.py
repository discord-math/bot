import logging
import log_setup

try:
    import asyncio

    import util.restart
    import plugins
    import discord_client

    loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
    logger: logging.Logger = logging.getLogger(__name__)

    main_task: asyncio.Task[None] = loop.create_task(discord_client.main_task())

    try:
        plugins.load("plugins.autoload")
        loop.run_until_complete(main_task)
    except:
        logger.critical("Exception during main event loop", exc_info=True)
    finally:
        logger.info("Finalizing event loop")
        tasks = asyncio.all_tasks(loop=loop)
        for task in tasks:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, loop=loop, return_exceptions=True))
        for task in tasks:
            if not task.cancelled() and task.exception() is not None:
                logger.critical("Unhandled exception in task", exc_info=task.exception())
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
except:
    logger.critical("Exception in main", exc_info=True)
    raise
