import psycopg2
import static_config
import util.db.log as log
import logging

logger = logging.getLogger(__name__)
connection_dsn = static_config.DB["dsn"]

def connection():
    conn = psycopg2.connect(connection_dsn,
        connection_factory=log.LoggingConnection)
    conn.initialize(logger)
    return conn

from util.db.initialization import init, init_for
