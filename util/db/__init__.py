import contextlib
import asyncpg
import sqlalchemy
import sqlalchemy.schema
import sqlalchemy.ext.asyncio
import sqlalchemy.dialects.postgresql
import logging
from typing import Dict, AsyncIterator, Callable, Any
import static_config
import util.db.log as util_db_log
import util.db.dsn as util_db_dsn

logger: logging.Logger = logging.getLogger(__name__)
connection_dsn: str = static_config.DB["dsn"]

class LoggingConnection(util_db_log.LoggingConnection(logger)): # type: ignore
    pass

connection_uri: str = util_db_dsn.dsn_to_uri(connection_dsn)
async_connection_uri: str = util_db_dsn.uri_to_asyncpg(connection_uri)

@contextlib.asynccontextmanager
async def connection() -> AsyncIterator[LoggingConnection]:
    conn = await asyncpg.connect(connection_uri, connection_class=LoggingConnection)
    try:
        yield conn
    finally:
        await conn.close()

def create_async_engine(connect_args: Dict[str, Any] = {}, **kwargs: Any) -> sqlalchemy.ext.asyncio.AsyncEngine:
    args = connect_args.copy()
    args.setdefault("connection_class", LoggingConnection)
    return sqlalchemy.ext.asyncio.create_async_engine(async_connection_uri, connect_args=args, **kwargs)

from util.db.initialization import init as init, init_for as init_for

def get_ddl(*cbs: Callable[[sqlalchemy.engine.Engine], None]) -> str:
    # By default sqlalchemy treats asyncpg as if it had paramstyle="format", which means it tries to escape percent
    # signs. We don't want that so we have to override the paramstyle. Ideally "numeric" would be the right choice here
    # but that doesn't work.
    dialect = sqlalchemy.dialects.postgresql.dialect(paramstyle="qmark") # type: ignore
    ddls = []

    def executor(sql: sqlalchemy.schema.DDLElement) -> None:
        ddls.append(str(sql.compile(dialect=dialect)) + ";")
    mock_engine = sqlalchemy.create_mock_engine("postgresql://", executor)
    for cb in cbs:
        cb(mock_engine) # type: ignore

    return "\n".join(ddls)
