"""
Utilities for logging SQL queries
"""

from __future__ import annotations
import logging
import psycopg2
import psycopg2.extensions
from typing import List, Dict, Tuple, Sequence, Optional, Union, Callable, Iterator, Any, cast

class LoggingCursor(psycopg2.extensions.cursor): # type: ignore
    __slots__ = "logger"
    logger: logging.Logger

    def __init__(self, logger: logging.Logger, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.logger = logger

    def execute(self, sql: str, vars: Optional[Union[Tuple[Any, ...], Dict[str, Any]]] = None,
            log_data: Union[bool, Sequence[int], Sequence[str]] = True) -> None:
        """
        When log_data is a boolean, it controls whether we log the query with or without parameters substituted in.
        Otherwise it's understood to be an iterable that determines which indexes in "vars" are logged.
        """
        text: str
        if log_data and vars:
            strip_vars: Union[Tuple[Any, ...], Dict[str, Any]] = vars
            if not isinstance(log_data, bool):
                if isinstance(vars, dict):
                    strip_vars = {key: value if key in log_data else None for key, value in vars.items()}
                else:
                    strip_vars = tuple(vars[i] if i in log_data else None for i in range(len(vars)))
            text = self.mogrify(sql.strip(), strip_vars).decode("utf")
        else:
            text = sql.strip()
        self.logger.debug("Execute {}: {}".format(id(self.connection), text))
        super().execute(sql, vars)

    def executemany(self, sql: str, var_list: Union[Sequence[Tuple[Any, ...]], Sequence[Dict[str, Any]]],
            log_data: Union[bool, Sequence[int], Sequence[str]] = False) -> None:
        """
        When log_data is a boolean, it controls whether we log the query with or without parameters substituted in.
        Otherwise it's understood to be an iterable that determines which indexes in "var_list" are logged.
        """
        if log_data:
            strip_list: Union[Sequence[Tuple[Any, ...]], Sequence[Dict[str, Any]]]
            strip_list = var_list
            if not isinstance(log_data, bool):
                if len(var_list) and isinstance(var_list[0], dict):
                    strip_list = [{key: value
                        for key, value in vars.items() if key in log_data}
                        for vars in cast(Sequence[Dict[str, Any]], var_list)]
                else:
                    strip_list = [tuple(vars[i] if i in log_data else None
                        for i in range(len(vars)))
                        for vars in cast(Sequence[Tuple[Any, ...]], var_list)]
            self.logger.debug("ExecuteMany {}: {}; {}".format(id(self.connection), sql.strip(), repr(strip_list)))
        else:
            self.logger.debug("ExecuteMany {}: {}".format(id(self.connection), sql.strip()))
        super().executemany(sql, var_list)

    def callproc(self, procname: str, *args: Any) -> Any:
        self.logger.debug("CallProc {}: {}{}".format(id(self.connection), procname, repr(args)))
        return super().callproc(procname, *args)

    def __enter__(self) -> LoggingCursor:
        return super().__enter__() # type: ignore

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return super().fetchone() # type: ignore

    def fetchmany(self, size: Optional[int]) -> List[Tuple[Any, ...]]:
        if size is None:
            return super().fetchmany() # type: ignore
        else:
            return super().fetchmany(size) # type: ignore

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return super().fetchall() # type: ignore

    def __iter__(self) -> Iterator[Tuple[Any, ...]]:
        return super().__iter__() # type: ignore

def make_logging_cursor(logger: logging.Logger) -> Callable[..., LoggingCursor]:
    return lambda *args, **kwargs: LoggingCursor(logger, *args, **kwargs)

class LoggingNotices:
    __slots__ = "logger"
    logger: logging.Logger

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def append(self, text: str) -> None:
        self.logger.debug(text)

class LoggingConnection(psycopg2.extensions.connection): # type: ignore
    __slots__ = "logger"
    logger: Optional[logging.Logger]
    notices: Union[List[str], LoggingNotices]

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.logger = None

    def initialize(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.logger.debug("Connected to {}".format(self.dsn))
        self.cursor_factory = make_logging_cursor(self.logger)
        notices = cast(List[str], self.notices)
        self.notices = LoggingNotices(logger)
        for text in notices:
            self.notices.append(text)

    def ensure_init(self) -> logging.Logger:
        if self.logger is None:
            raise ValueError("LoggingConnection not initialized")
        return self.logger

    def rollback(self) -> None:
        logger = self.ensure_init()
        logger.debug("Rollback {}".format(id(self)))
        super().rollback()

    def commit(self) -> None:
        logger = self.ensure_init()
        logger.debug("Commit {}".format(id(self)))
        super().commit()

    def cancel(self) -> None:
        logger = self.ensure_init()
        logger.debug("Cancel {}".format(id(self)))
        super().commit()

    def cursor(self, *args: Any, **kwargs: Any) -> LoggingCursor:
        self.ensure_init()
        return super().cursor(*args, **kwargs) # type: ignore

    def __enter__(self) -> LoggingConnection:
        return super().__enter__() # type: ignore
