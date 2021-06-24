import logging
import logging.handlers
import time
import warnings
from typing import List, Tuple, Any, Optional, Callable
import static_config

logging.basicConfig(handlers=[], force=True)

@(lambda f: f())
def closure() -> None:
    old_showwarning = warnings.showwarning

    def showwarning(message, category, filename, lineno, file=None, line=None) -> None: # type: ignore
        if file is not None:
            old_showwarning = warnings.showwarning
        else:
            text = warnings.formatwarning(message, category, filename, lineno, line)
            logging.getLogger("__builtins__").error(text)

    warnings.showwarning = showwarning

logger: logging.Logger = logging.getLogger()
logger.setLevel(logging.NOTSET)

class Formatter(logging.Formatter):
    """A formatter that formats multi-line messages in a greppable fashion"""

    __slots__ = ()

    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03d"

    def format(self, record: Any) -> str:
        record.asctime = self.formatTime(record, self.datefmt)
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)

        lines = record.getMessage().split("\n")
        if record.exc_text:
            lines.extend(record.exc_text.split("\n"))
        if record.stack_info:
            lines.extend(self.formatStack(record.stack_info).split("\n"))

        lines = list(filter(bool, lines))

        output = []
        for i in range(len(lines)):
            record.message = lines[i]
            if len(lines) == 1:
                record.symbol = ":"
            elif i == 0:
                record.symbol = "{"
            elif i == len(lines) - 1:
                record.symbol = "}"
            else:
                record.symbol = "|"
            output.append(self.formatMessage(record))
        return "\n".join(output)

formatter: logging.Formatter = Formatter("%(asctime)s %(name)s %(levelname)s%(symbol)s %(message)s")

targets: List[Tuple[int, str, Optional[Callable[[logging.LogRecord], bool]]]] = (
    [ (logging.DEBUG, "debug.discord", lambda r: r.name.startswith("discord.") )
    , (logging.DEBUG, "debug", lambda r: not r.name.startswith("discord.") )
    , (logging.INFO, "info", None)
    , (logging.WARNING, "warning", None)
    , (logging.ERROR, "error", None)
    , (logging.CRITICAL, "critical", None) ])

for level, name, cond in targets:
    handler = logging.handlers.TimedRotatingFileHandler(
        filename="{}/{}.log".format(static_config.Log["directory"], name),
        when="midnight", utc=True, encoding="utf", errors="replace")
    handler.setLevel(level)
    handler.setFormatter(formatter)
    if cond:
        handler.addFilter(cond)
    logger.addHandler(handler)
