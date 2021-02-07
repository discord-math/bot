"""
Automatically load certain plugins after bot initialization.
"""

import importlib
import logging
import util.db.kv
import plugins

conf = util.db.kv.Config(__name__)
if conf.autoload == None:
    conf.autoload = []

def get_autoload():
    return conf.autoload

def set_autoload(autoload):
    conf.autoload = autoload

logger = logging.getLogger(__name__)

for name in conf.autoload:
    try:
        # Sidestep plugin dependency tracking
        plugins.load(name)
    except:
        logger.critical("Exception during autoload of {}".format(name),
            exc_info=True)
