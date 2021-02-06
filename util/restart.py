import sys
import os
import atexit
import discord_client
import asyncio
import logging

will_restart = False

logger = logging.getLogger(__name__)

@atexit.register
def atexit_restart_maybe():
    if will_restart:
        logger.info("Re-executing {!r} {!r}".format(sys.executable, sys.argv))
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except:
            logger.critical("Restart failed", exc_info=True)

def restart():
    """
    Restart the bot by stopping the event loop and exec'ing during the shutdown
    of the python interpreter.
    """
    global will_restart
    logger.info("Restart requested", stack_info=True)
    asyncio.get_event_loop().stop()
    will_restart = True
