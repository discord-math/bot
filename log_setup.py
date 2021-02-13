import logging
import logging.handlers
import static_config

logging.basicConfig(handlers=[], force=True)
logger = logging.getLogger()
logger.setLevel(logging.NOTSET)

class Formatter(logging.Formatter):
    """A formatter that formats multi-line messages in a greppable fashion"""
    
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03d"

    def format(self, record):
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

formatter = Formatter(
    "%(asctime)s %(name)s %(levelname)s%(symbol)s %(message)s")

for level, name in (
    [ (logging.DEBUG, "debug")
    , (logging.INFO, "info")
    , (logging.WARNING, "warning")
    , (logging.ERROR, "error")
    , (logging.CRITICAL, "critical") ]):
    handler = logging.handlers.TimedRotatingFileHandler(
        filename="{}/{}.log".format(static_config.Log["directory"], name),
        when="midnight", utc=True, encoding="utf", errors="replace")
    handler.setLevel(level)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
