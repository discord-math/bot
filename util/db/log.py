import psycopg2
import psycopg2.extensions

class LoggingCursor(psycopg2.extensions.cursor):
    def __init__(self, logger, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = logger

    def execute(self, sql, vars=None, log_data=True):
        if log_data:
            strip_vars = vars
            if type(log_data) is not bool:
                if type(vars) is dict:
                    strip_vars = {key: value if key in log_data else None
                        for key, value in vars.items()}
                else:
                    strip_vars = [vars[i] if i in log_data else None
                        for i in range(len(vars))]
            text = self.mogrify(sql.strip(), strip_vars).decode("utf")
        else:
            text = sql.strip()
        self.logger.info("Execute {}: {}".format(id(self.connection), text))
        super().execute(sql, vars)

    def executemany(self, sql, var_list, log_data=False):
        if log_data:
            strip_list = var_list
            if type(log_data) is not bool:
                if len(var_list) and type(var_list[0]) is dict:
                    strip_list = [{key: value
                        for key, value in vars.items() if key in log_data}
                        for vars in var_list]
                else:
                    strip_list = [[vars[i] if i in log_data else None
                        for i in range(len(vars))]
                        for vars in var_list]
            self.logger.info("ExecuteMany {}: {}; {}".format(
                id(self.connection),
                sql.strip(),
                repr(strip_list)))
        else:
            self.logger.info("ExecuteMany {}: {}".format(
                id(self.connection),
                sql.strip()))
        super().executemany(sql, var_list)

    def callproc(procname, *args):
        self.logger.info("CallProc {}: {}{}".format(
            id(self.connection),
            procname,
            repr(args)))
        return super().callproc(procname, *args)

def make_logging_cursor(logger):
    return lambda *args, **kwargs: LoggingCursor(logger, *args, **kwargs)

class LoggingNotices():
    def __init__(self, logger):
        self.logger = logger

    def append(self, text):
        logger.info(text)

class LoggingConnection(psycopg2.extensions.connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = None

    def initialize(self, logger):
        self.logger = logger
        self.logger.info("Connected to {}".format(self.dsn))
        self.cursor_factory = make_logging_cursor(self.logger)
        notices = self.notices
        self.notices = LoggingNotices(logger)
        for text in notices:
            self.notices.append(text)

    def ensure_init(self):
        if not self.logger:
            raise ValueError("LoggingConnection not initialized")
    def rollback(self):
        self.ensure_init()
        self.logger.info("Rollback {}".format(id(self)))
        super().rollback()

    def commit(self):
        self.ensure_init()
        self.logger.info("Commit {}".format(id(self)))
        super().commit()

    def cancel(self):
        self.ensure_init()
        self.logger.info("Cancel {}".format(id(self)))
        super().commit()

    def cursor(self, *args, **kwargs):
        self.ensure_init()
        return super().cursor(*args, **kwargs)
