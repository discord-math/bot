import sys
import os
import atexit
import asyncio
import logging
import discord_client

will_restart: bool = False

logger: logging.Logger = logging.getLogger(__name__)

@atexit.register
def atexit_restart_maybe() -> None:
    if will_restart:
        logger.info("Re-executing {!r} {!r}".format(sys.executable, sys.argv))
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except:
            logger.critical("Restart failed", exc_info=True)

def restart() -> None:
    """
    Restart the bot by stopping the event loop and exec'ing during the shutdown of the python interpreter.
    """
    global will_restart
    logger.info("Restart requested", stack_info=True)
    will_restart = True
    try: # We cannot import util.asyncio here because the atexit handler needs to be registered before that of plugins
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    loop.create_task(discord_client.client.close())
