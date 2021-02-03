import logging
logging.basicConfig(level=logging.DEBUG)
import asyncio

import util.restart
import discord_client

loop = asyncio.get_event_loop()

loop.create_task(discord_client.main_task())

try:
    import plugins.autoload
    loop.run_forever()
except:
    logging.getLogger(__name__).critical("Exception during main event loop",
        exc_info=True)
finally:
    loop.close()
