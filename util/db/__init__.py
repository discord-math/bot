import contextlib
from typing import Any, AsyncIterator, Callable, Dict, Union

import asyncpg
import sqlalchemy
from sqlalchemy import Connection
import sqlalchemy.dialects.postgresql
import sqlalchemy.ext.asyncio
from sqlalchemy.schema import DDLElement, ExecutableDDLElement

import static_config
import util.db.dsn as util_db_dsn
import util.db.log as util_db_log

connection_dsn: str = static_config.DB["dsn"]

connection_uri: str = util_db_dsn.dsn_to_uri(connection_dsn)
async_connection_uri: str = util_db_dsn.uri_to_asyncpg(connection_uri)

@contextlib.asynccontextmanager
async def connection() -> AsyncIterator[util_db_log.LoggingConnection]:
    conn = await asyncpg.connect(connection_uri, connection_class=util_db_log.LoggingConnection)
    try:
        yield conn
    finally:
        await conn.close()

def create_async_engine(connect_args: Dict[str, Any] = {}, **kwargs: Any) -> sqlalchemy.ext.asyncio.AsyncEngine:
    args = connect_args.copy()
    args.setdefault("connection_class", util_db_log.LoggingConnection)
    return sqlalchemy.ext.asyncio.create_async_engine(async_connection_uri,
        pool_pre_ping=True, connect_args=args, **kwargs)

from util.db.initialization import init as init
from util.db.initialization import init_for as init_for

def get_ddl(*cbs: Union[DDLElement, Callable[[Connection], None]]) -> str:
    # By default sqlalchemy treats asyncpg as if it had paramstyle="format", which means it tries to escape percent
    # signs. We don't want that so we have to override the paramstyle. Ideally "numeric" would be the right choice here
    # but that doesn't work.
    dialect = sqlalchemy.dialects.postgresql.dialect(paramstyle="qmark")
    ddls = []

    def executor(sql: ExecutableDDLElement, *args: Any, **kwargs: Any) -> None:
        ddls.append(str(sql.compile(dialect=dialect)) + ";")
    conn = sqlalchemy.create_mock_engine(sqlalchemy.make_url("postgresql://"), executor)
    for cb in cbs:
        if isinstance(cb, DDLElement):
            conn.execute(cb)
        else:
            cb(conn) # type: ignore

    return "\n".join(ddls)
