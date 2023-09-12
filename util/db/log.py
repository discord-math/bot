import logging
from typing import Any, Callable, Collection, Optional, Sequence, Union

from asyncpg import Connection, PostgresLogMessage, Record
from asyncpg.cursor import CursorFactory
from asyncpg.prepared_stmt import PreparedStatement
from asyncpg.transaction import Transaction

logger: logging.Logger = logging.getLogger(__name__)

severity_map = {
    "DEBUG": logging.DEBUG,
    "LOG": logging.DEBUG,
    "NOTICE": logging.INFO,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "FATAL": logging.ERROR,
    "PANIC": logging.ERROR}

def filter_single(log_data: Union[bool, Collection[int]], data: Sequence[object]) -> str:
    spec: Callable[[int], bool]
    if isinstance(log_data, bool):
        log_data_bool = log_data
        spec = lambda _: log_data_bool
    else:
        log_data_set = log_data
        spec = lambda i: i in log_data_set
    return "({})".format(",".join(repr(data[i]) if spec(i + 1) else "?" for i in range(len(data))))

def filter_multi(log_data: Union[bool, Collection[int]], data: Sequence[Sequence[object]]) -> str:
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

def fmt_query_single(query: str, log_data: Union[bool, Collection[int]], args: Sequence[object]) -> str:
    if log_data:
        return "{} % {}".format(query, filter_single(log_data, args))
    else:
        return query

def fmt_query_multi(query: str, log_data: Union[bool, Collection[int]], args: Sequence[Sequence[object]]) -> str:
    if log_data:
        return "{} % {}".format(query, filter_multi(log_data, args))
    else:
        return query

def fmt_table(name: str, schema: Optional[str]) -> str:
    return schema + "." + name if schema is not None else name

def log_message(conn: Connection, msg: PostgresLogMessage) -> None:
    severity = getattr(msg, "severity_en") or getattr(msg, "severity")
    logger.log(severity_map.get(severity, logging.INFO), "{} {}".format(id(conn), msg))

def log_termination(conn: Connection) -> None:
    logger.debug("{} closed".format(id(conn)))

class LoggingConnection(Connection):

    def __init__(self, proto: Any, transport: Any, *args: Any, **kwargs: Any):
        logger.debug("{} connected over {!r}".format(id(self), transport))
        super().__init__(proto, transport, *args, **kwargs)
        self.add_log_listener(log_message)
        self.add_termination_listener(log_termination)

    async def copy_from_query(self, query: str, *args: object, log_data: Union[bool, Collection[int]] = True,
        **kwargs: object) -> str:
        logger.debug("{} copy_from_query: {}".format(id(self), fmt_query_single(query, log_data, args)))
        return await super().copy_from_query(query, *args, **kwargs)

    async def copy_from_table(self, table_name: str, schema_name: Optional[str] = None, **kwargs: object) -> str:
        logger.debug("{}: copy_from_table: {}".format(id(self), fmt_table(table_name, schema_name)))
        return await super().copy_from_table(table_name, schema_name=schema_name, **kwargs)

    async def copy_records_to_table(self, table_name: str, schema_name: Optional[str] = None, **kwargs: object) -> str:
        logger.debug("{}: copy_records_to_table: {}".format(id(self), fmt_table(table_name, schema_name)))
        return await super().copy_records_to_table(table_name, schema_name=schema_name, **kwargs)

    async def copy_to_table(self, table_name: str, schema_name: Optional[str] = None, **kwargs: object) -> str:
        logger.debug("{}: copy_to_table: {}".format(id(self), fmt_table(table_name, schema_name)))
        return await super().copy_to_table(table_name, schema_name=schema_name, **kwargs)

    def cursor(self, query: str, *args: object, log_data: Union[bool, Collection[int]] = True, **kwargs: object
        ) -> CursorFactory:
        logger.debug("{}: cursor: {}".format(id(self), fmt_query_single(query, log_data, args)))
        return super().cursor(query, *args, **kwargs)

    async def execute(self, query: str, *args: object, log_data: Union[bool, Collection[int]] = True,
        **kwargs: Any) -> str:
        logger.debug("{} execute: {}".format(id(self), fmt_query_single(query, log_data, args)))
        return await super().execute(query, *args, **kwargs)

    async def executemany(self, query: str, args: Sequence[Sequence[object]],
        log_data: Union[bool, Collection[int]] = True, **kwargs: Any) -> None:
        logger.debug("{} executemany: {}".format(id(self), fmt_query_multi(query, log_data, args)))
        return await super().executemany(query, args, **kwargs)

    async def fetch(self, query: str, *args: object, # type: ignore
        log_data: Union[bool, Collection[int]] = True, **kwargs: object) -> Sequence[Record]:
        logger.debug("{} fetch: {}".format(id(self), fmt_query_single(query, log_data, args)))
        return await super().fetch(query, *args, **kwargs)

    async def fetchrow(self, query: str, *args: object, # type: ignore
        log_data: Union[bool, Collection[int]] = True, **kwargs: object) -> Optional[Record]:
        logger.debug("{} fetchrow: {}".format(id(self), fmt_query_single(query, log_data, args)))
        return await super().fetchrow(query, *args, **kwargs)

    async def fetchval(self, query: str, *args: object, # type: ignore
        log_data: Union[bool, Collection[int]] = True, **kwargs: Any) -> Optional[Record]:
        logger.debug("{} fetchval: {}".format(id(self), fmt_query_single(query, log_data, args)))
        return await super().fetchval(query, *args, **kwargs)

    def transaction(self, **kwargs: Any) -> Transaction:
        logger.debug("{} transaction".format(id(self)))
        return super().transaction(**kwargs)

    async def prepare(self, query: str, **kwargs: object) -> PreparedStatement:
        logger.debug("{} prepare: {}".format(id(self), query))
        # TODO: hook into PreparedStatement
        return await super().prepare(query, **kwargs)
