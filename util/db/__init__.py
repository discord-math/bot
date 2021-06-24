import psycopg2
import psycopg2.extensions
import logging
from typing import Callable
import static_config
import util.db.log as util_db_log

logger: logging.Logger = logging.getLogger(__name__)
connection_dsn: str = static_config.DB["dsn"]

def connection() -> util_db_log.LoggingConnection:
    conn = psycopg2.connect(connection_dsn, connection_factory=util_db_log.LoggingConnection)
    conn.initialize(logger)
    return conn # type: ignore

from util.db.initialization import init as init, init_for as init_for
