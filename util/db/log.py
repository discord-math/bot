import logging
import asyncpg
import asyncpg.exceptions
import asyncpg.cursor
import asyncpg.transaction
import asyncpg.prepared_stmt
from typing import Any, Sequence, Union, Optional, Type, Callable

severity_map = {
    "DEBUG": logging.DEBUG,
    "LOG": logging.DEBUG,
    "NOTICE": logging.INFO,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "FATAL": logging.ERROR,
    "PANIC": logging.ERROR}

def filter_single(log_data: Union[bool, Sequence[int]], data: Sequence[Any]) -> str:
    spec: Callable[[int], bool]
    if isinstance(log_data, bool):
        log_data_bool = log_data
        spec = lambda _: log_data_bool
    else:
        log_data_set = log_data
        spec = lambda i: i in log_data_set
    return "({})".format(",".join(repr(data[i]) if spec(i + 1) else "?" for i in range(len(data))))

def filter_multi(log_data: Union[bool, Sequence[int]], data: Sequence[Sequence[Any]]) -> str:
    spec: Callable[[int], bool]
    if isinstance(log_data, bool):
        log_data_bool = log_data
        spec = lambda _: log_data_bool
    else:
        log_data_set = log_data
        spec = lambda i: i in log_data_set
    return ",".join(
        "({})".format(",".join(repr(datum[i]) if spec(i + 1) else "?" for i in range(len(datum))))
        for datum in data)

def fmt_query_single(query: str, log_data: Union[bool, Sequence[int]], args: Sequence[Any]) -> str:
    if log_data:
        return "{} % {}".format(query, filter_single(log_data, args))
    else:
        return query

def fmt_query_multi(query: str, log_data: Union[bool, Sequence[int]], args: Sequence[Sequence[Any]]) -> str:
    if log_data:
        return "{} % {}".format(query, filter_multi(log_data, args))
    else:
        return query

def fmt_table(name: str, schema: Optional[str]) -> str:
    return schema + "." + name if schema is not None else name

def LoggingConnection(logger: logging.Logger) -> Type[asyncpg.connection.Connection]:

    def log_message(conn: asyncpg.connection.Connection, msg: asyncpg.exceptions.PostgresLogMessage) -> None:
        severity = getattr(msg, "severity_en") or getattr(msg, "severity")
        logger.log(severity_map.get(severity, logging.INFO), "{} {}".format(id(conn), msg))

    def log_termination(conn: asyncpg.connection.Connection) -> None:
        logger.debug("{} closed".format(id(conn)))

    the_logger = logger
    class LoggingConnection(asyncpg.connection.Connection): # type: ignore
        logger: logging.Logger = the_logger

        def __init__(self, proto: Any, transport: Any, *args: Any, **kwargs: Any):
            self.logger.debug("{} connected over {!r}".format(id(self), transport))
            super().__init__(proto, transport, *args, **kwargs)
            self.add_log_listener(log_message)
            self.add_termination_listener(log_termination)

        async def copy_from_query(self, query: str, *args: Sequence[Any], log_data: Union[bool, Sequence[int]] = True,
            **kwargs: Any) -> str:
            self.logger.debug("{} copy_from_query: {}".format(id(self), fmt_query_single(query, log_data, args)))
            return await super().copy_from_query(query, *args, **kwargs) # type: ignore

        async def copy_from_table(self, table_name: str, schema_name: Optional[str] = None, **kwargs: Any) -> str:
            self.logger.debug("{}: copy_from_table: {}".format(id(self), fmt_table(table_name, schema_name)))
            return await super().copy_from_table(table_name, schema_name=schema_name, **kwargs) # type: ignore

        async def copy_records_to_table(self, table_name: str, schema_name: Optional[str] = None, **kwargs: Any) -> str:
            self.logger.debug("{}: copy_records_to_table: {}".format(id(self), fmt_table(table_name, schema_name)))
            return await super().copy_records_to_table(table_name, schema_name=schema_name, **kwargs) # type: ignore

        async def copy_to_table(self, table_name: str, schema_name: Optional[str] = None, **kwargs: Any) -> str:
            self.logger.debug("{}: copy_to_table: {}".format(id(self), fmt_table(table_name, schema_name)))
            return await super().copy_to_table(table_name, schema_name=schema_name, **kwargs) # type: ignore

        def cursor(self, query: str, *args: Sequence[Any], log_data: Union[bool, Sequence[int]] = True, **kwargs: Any
            ) -> asyncpg.cursor.CursorFactory:
            self.logger.debug("{}: cursor: {}".format(id(self), fmt_query_single(query, log_data, args)))
            return super().cursor(query, *args, **kwargs)

        async def execute(self, query: str, *args: Sequence[Any], log_data: Union[bool, Sequence[int]] = True,
            **kwargs: Any) -> str:
            self.logger.debug("{} execute: {}".format(id(self), fmt_query_single(query, log_data, args)))
            return await super().execute(query, *args, **kwargs) # type: ignore

        async def executemany(self, query: str, args: Sequence[Sequence[Any]],
            log_data: Union[bool, Sequence[int]] = True, **kwargs: Any) -> None:
            self.logger.debug("{} executemany: {}".format(id(self), fmt_query_multi(query, log_data, args)))
            return await super().executemany(query, args, **kwargs) # type: ignore

        async def fetch(self, query: str, *args: Sequence[Any], log_data: Union[bool, Sequence[int]] = True,
            **kwargs: Any) -> Sequence[asyncpg.Record]:
            self.logger.debug("{} fetch: {}".format(id(self), fmt_query_single(query, log_data, args)))
            return await super().fetch(query, *args, **kwargs) # type: ignore

        async def fetchrow(self, query: str, *args: Sequence[Any], log_data: Union[bool, Sequence[int]] = True,
            **kwargs: Any) -> Optional[asyncpg.Record]:
            self.logger.debug("{} fetchrow: {}".format(id(self), fmt_query_single(query, log_data, args)))
            return await super().fetchrow(query, *args, **kwargs)

        async def fetchval(self, query: str, *args: Sequence[Any], log_data: Union[bool, Sequence[int]] = True,
            **kwargs: Any) -> Optional[asyncpg.Record]:
            self.logger.debug("{} fetchval: {}".format(id(self), fmt_query_single(query, log_data, args)))
            return await super().fetchval(query, *args, **kwargs)

        def transaction(self, **kwargs: Any) -> asyncpg.transaction.Transaction:
            self.logger.debug("{} transaction".format(id(self)))
            return super().transaction(**kwargs)

        async def prepare(self, query: str, **kwargs: Any) -> asyncpg.prepared_stmt.PreparedStatement:
            self.logger.debug("{} prepare: {}".format(id(self), query))
            # TODO: hook into PreparedStatement
            return await super().prepare(query, **kwargs)

    return LoggingConnection
