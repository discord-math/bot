"""
A simple key-value store that associates to each module name and a string key a
piece of JSON. If a module needs more efficient or structured storage it should
probably have its own DB handling code.
"""

import util.db as db
import util.frozen_list
import util.frozen_dict
import json
import weakref

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
            ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value
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
            ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value
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

def json_freeze(value):
    if isinstance(value, list):
        return util.frozen_list.FrozenList(
            json_freeze(v) for v in value)
    elif isinstance(value, dict):
        return util.frozen_dict.FrozenDict(
            (k, json_freeze(v)) for k, v in value.items())
    else:
        return value

class ThawingJSONEncoder(json.JSONEncoder):
    __slots__ = ()
    def default(self, obj):
        if isinstance(obj, util.frozen_list.FrozenList):
            return obj.copy()
        elif isinstance(obj, util.frozen_dict.FrozenDict):
            return obj.copy()
        else:
            return super().default(obj)

def json_encode(value):
    return json.dumps(value, cls=ThawingJSONEncoder) if value != None else None

def json_decode(text):
    return json_freeze(json.loads(text)) if text != None else None

class Proxy:
    """
    This object encapsulates access to the key-value store for a fixed module.
    No efforts are made to cache anything: every __getitem__/__getattr__ and
    __setitem__/__setattr__ and __iter__ makes a respective DB query.
    """
    __slots__ = "_namespace", "_log_value"

    def __init__(self, namespace, log_value=False):
        self._namespace = namespace
        self._log_value = log_value

    def __iter__(self):
        for key, _ in get_key_values(self._namespace):
            yield key

    def __getitem__(self, key):
        return json_decode(get_value(self._namespace, key))

    def __setitem__(self, key, value):
        set_value(self._namespace, key, json_encode(value),
            log_value=self._log_value)

    def __getattr__(self, key):
        if key.startswith("_"):
            return
        return json_decode(get_value(self._namespace, key))

    def __setattr__(self, key, value):
        if key.startswith("_"):
            return super().__setattr__(key, value)
        set_value(self._namespace, key, json_encode(value),
            log_value=self._log_value)

class ConfigStore(dict):
    __slots__ = "__weakref__"

config_stores = weakref.WeakValueDictionary()

class Config:
    """
    This object encapsulates access to the key-value store for a fixed module.
    Upon construction this makes a DB query fetching all the data. __iter__ and
    __getitem__/__getattr__ will read from this in-memory copy,
    __setitem__/__setattr__ will update the in-memory copy but also immediately
    make a DB query to store the change.
    """
    __slots__ = "_namespace", "_log_value", "_config"

    def __init__(self, namespace, log_value=False):
        self._namespace = namespace
        self._log_value = log_value

        config = config_stores.get(namespace)
        if config == None:
            config = ConfigStore(get_key_values(namespace))
            config_stores[namespace] = config
        self._config = config

    def __iter__(self):
        return self._config.__iter__()

    def __getitem__(self, key):
        return json_decode(self._config.get(key))

    def __setitem__(self, key, value):
        ev = json_encode(value)
        if value is None:
            del self._config[key]
        else:
            self._config[key] = ev
        set_value(self._namespace, key, ev, log_value=self._log_value)

    def __getattr__(self, key):
        if key.startswith("_"):
            return
        return json_decode(self._config.get(key))

    def __setattr__(self, key, value):
        if key.startswith("_"):
            return super().__setattr__(key, value)
        ev = json_encode(value)
        if value is None:
            del self._config[key]
        else:
            self._config[key] = ev
        set_value(self._namespace, key, ev, log_value=self._log_value)
