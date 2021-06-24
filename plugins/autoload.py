"""
Automatically load certain plugins after bot initialization.
"""

import logging
from typing import Iterable, Protocol, cast
import util.frozen_list
import util.db.kv
import plugins

class AutoloadConf(Protocol):
    autoload: util.frozen_list.FrozenList[str]

conf = cast(AutoloadConf, util.db.kv.Config(__name__))
if conf.autoload is None:
    conf.autoload = util.frozen_list.FrozenList()

def get_autoload() -> util.frozen_list.FrozenList[str]:
    return conf.autoload

def set_autoload(autoload: Iterable[str]) -> None:
    conf.autoload = util.frozen_list.FrozenList(autoload)

logger: logging.Logger = logging.getLogger(__name__)

for name in conf.autoload:
    try:
        # Sidestep plugin dependency tracking
        plugins.load(name)
    except:
        logger.critical("Exception during autoload of {}".format(name), exc_info=True)
