import util.db as db
import json

@db.init_for(__name__)
def init():
    return """
        CREATE TABLE kv
            ( namespace TEXT NOT NULL
            , key TEXT NOT NULL
            , value TEXT NOT NULL
            , PRIMARY KEY(namespace, key) );
        CREATE INDEX kv_namespace_index
            ON kv USING BTREE(namespace);
        """

def cur_get_value(cur, namespace, key):
    cur.execute("""
        SELECT value FROM kv WHERE namespace = %(ns)s AND key = %(key)s
        """, {"ns": namespace, "key": key})
    value = cur.fetchone()
    return value[0] if value else None

def cur_get_key_values(cur, namespace):
    cur.execute("""
        SELECT key, value FROM kv WHERE namespace = %(ns)s
        """, {"ns": namespace})
    for key, value in cur:
        yield key, value

def cur_get_namespaces(cur):
    cur.execute("""
        SELECT DISTINCT namespace FROM kv
        """)
    for ns, in cur:
        yield ns

def cur_set_value(cur, namespace, key, value, log_value=True):
    if value == None:
        cur.execute("""
            DELETE FROM kv
            WHERE namespace = %(ns)s AND key = %(key)s
            """, {"ns": namespace, "key": key})
    else:
        cur.execute("""
            INSERT INTO kv (namespace, key, value)
            VALUES (%(ns)s, %(key)s, %(value)s)
            ON CONFLICT (namespace, key) DO UPDATE SET value = %(value)s
            """, {"ns": namespace, "key": key, "value": value},
            log_data=True if log_value else {"ns", "key"})

def cur_set_default(cur, namespace, key, value, log_value=True):
    if value != None:
        cur.execute("""
            INSERT INTO kv (namespace, key, value)
            VALUES (%(ns)s, %(key)s, %(value)s)
            ON CONFLICT (namespace, key) DO NOTHING
            """, {"ns": namespace, "key": key, "value": value},
            log_data=True if log_value else {"ns", "key"})

def cur_set_values(cur, namespace, dict, log_value=False):
    removals = [{"ns": namespace, "key": key}
        for key, value in dict.items() if value == None]
    additions = [{"ns": namespace, "key": key, "value": value}
        for key, value in dict.items() if value != None]
    if removals:
        cur.executemany("""
            DELETE FROM kv
            WHERE namespace = %(ns)s AND key = %(key)s
            """, removals, log_data=True)
    if additions:
        cur.executemany("""
            INSERT INTO kv (namespace, key, value)
            VALUES (%(ns)s, %(key)s, %(value)s)
            ON CONFLICT (namespace, key) DO UPDATE SET value = %(value)s
            """, additions, log_data=True if log_value else {"ns", "key"})

def cur_set_defaults(cur, namespace, dict, log_value=False):
    additions = [{"ns": namespace, "key": key, "value": value}
        for key, value in dict.items() if value != None]
    if additions:
        cur.executemany("""
            INSERT INTO kv (namespace, key, value)
            VALUES (%(ns)s, %(key)s, %(value)s)
            ON CONFLICT (namespace, key) DO NOTHING
            """, additions, log_data=True if log_value else {"ns", "key"})

def get_value(namespace, key):
    return cur_get_value(db.connection().cursor(), namespace, key)

def get_key_values(namespace):
    return cur_get_key_values(db.connection().cursor(), namespace)

def get_namespaces():
    return cur_get_namespaces(db.connection().cursor())

def set_value(namespace, key, value, log_value=True):
    with db.connection() as conn:
        cur_set_value(conn.cursor(), namespace, key, value, log_value=log_value)

def set_default(namespace, key, value, log_value=True):
    with db.connection() as conn:
        cur_set_default(conn.cursor(), namespace, key, value,
            log_value=log_value)

def set_values(namespace, dict, log_value=True):
    with db.connection() as conn:
        cur_set_values(conn.cursor(), namespace, dict, log_value=log_value)

def set_defaults(namespace, dict, log_value=True):
    with db.connection() as conn:
        cur_set_defaults(conn.cursor(), namespace, dict, log_value=log_value)

class ProxyClass:
    def __init__(self, namespace, encode, decode, log_value=False):
        self._namespace = namespace
        self._log_value = log_value
        self._encode = encode
        self._decode = decode

    def __getitem__(self, key):
        return self._decode(get_value(self._namespace, key))

    def __setitem__(self, key, value):
        set_value(self._namespace, key, self._encode(value),
            log_value=self._log_value)

    def __getattr__(self, key):
        if key.startswith("_"):
            return
        return self._decode(get_value(self._namespace, key))

    def __setattr__(self, key, value):
        if key.startswith("_"):
            self.__dict__[key] = value
            return
        set_value(self._namespace, key, self._encode(value),
            log_value=self._log_value)

class ConfigClass:
    def __init__(self, namespace, encode, decode, log_value=False):
        self._namespace = namespace
        self._log_value = log_value
        self._encode = encode
        self._decode = decode
        self._config = dict(get_key_values(self._namespace))

    def __getitem__(self, key):
        return self._decode(self._config.get(key))

    def __setitem__(self, key, value):
        ev = self._encode(value)
        self._config[key] = ev
        set_value(self._namespace, key, ev, log_value=self._log_value)

    def __getattr__(self, key):
        if key.startswith("_"):
            return
        return self._decode(self._config.get(key))

    def __setattr__(self, key, value):
        if key.startswith("_"):
            self.__dict__[key] = value
            return
        ev = self._encode(value)
        self._config[key] = ev
        set_value(self._namespace, key, ev, log_value=self._log_value)

def string_code(value):
    return value

class StringProxy(ProxyClass):
    def __init__(self, namespace, log_value=False):
        super().__init__(namespace, string_code, string_code, log_value)

class StringConfig(ConfigClass):
    def __init__(self, namespace, log_value=False):
        super().__init__(namespace, string_code, string_code, log_value)

def basic_encode(value):
    if value is None:
        return None
    elif type(value) is str:
        return "s" + value
    elif type(value) is int:
        return "i" + str(value)
    elif type(value) is float:
        return "f" + str(value)
    elif type(value) is bool:
        return "b" + str(value)
    else:
        raise TypeError("Cannot encode {} object".format(type(value)))

def basic_decode(value):
    if value is None:
        return None
    elif value.startswith("s"):
        return value[1:]
    elif value.startswith("i"):
        return int(value[1:])
    elif value.startswith("f"):
        return float(value[1:])
    elif value.startswith("b"):
        return value[1:] == "True"
    else:
        raise ValueError("Could not decode {}".format(value))

class BasicProxy(ProxyClass):
    def __init__(self, namespace, log_value=True):
        super().__init__(namespace, basic_encode, basic_decode, log_value)

class BasicConfig(ConfigClass):
    def __init__(self, namespace, log_value=True):
        super().__init__(namespace, basic_encode, basic_decode, log_value)

def json_encode(value):
    return json.dumps(value) if value != None else None

def json_decode(value):
    return json.loads(value) if value != None else None

class JsonProxy(ProxyClass):
    def __init__(self, namespace, log_value=False):
        super().__init__(namespace, json_encode, json_decode, log_value)

class JsonConfig(ConfigClass):
    def __init__(self, namespace, log_value=False):
        super().__init__(namespace, json_encode, json_decode, log_value)
