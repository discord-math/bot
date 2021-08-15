"""
A simple key-value store that associates to each module name and a string key a
piece of JSON. If a module needs more efficient or structured storage it should
probably have its own DB handling code.
"""

from __future__ import annotations
import asyncio
import asyncpg
import contextlib
import json
import weakref
from typing import Optional, Dict, Iterator, AsyncIterator, Tuple, Set, Sequence, Union, Any, cast
import util.asyncio
import util.db as util_db
import util.frozen_list
import util.frozen_dict

schema_initialized = False

async def init_schema() -> None:
    global schema_initialized
    if not schema_initialized:
        await util_db.init_for(__name__, """
            CREATE TABLE kv
                ( namespace TEXT NOT NULL
                , key TEXT ARRAY NOT NULL
                , value TEXT NOT NULL
                , PRIMARY KEY(namespace, key) );
            CREATE INDEX kv_namespace_index
                ON kv USING BTREE(namespace);
            """)
        schema_initialized = True

def json_freeze(value: Optional[Any]) -> Optional[Any]:
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
    def default(self, obj: Any) -> Any:
        if isinstance(obj, util.frozen_list.FrozenList):
            return obj.copy()
        elif isinstance(obj, util.frozen_dict.FrozenDict):
            return obj.copy()
        else:
            return super().default(obj)

def json_encode(value: Any) -> Optional[str]:
    return json.dumps(value, cls=ThawingJSONEncoder) if value is not None else None

def json_decode(text: Optional[str]) -> Any:
    return json_freeze(json.loads(text)) if text is not None else None

@contextlib.asynccontextmanager
async def connect() -> AsyncIterator[asyncpg.Connection]:
    await init_schema()
    async with util_db.connection() as conn:
        yield conn

async def get_raw_value(namespace: Sequence[str], key: Sequence[str]) -> Optional[str]:
    async with connect() as conn:
        val = await conn.fetchval("""
            SELECT value FROM kv WHERE namespace = $1 AND key = $2
            """, namespace, tuple(key))
        return cast(Optional[str], val)

async def get_raw_key_values(namespace: str) -> Dict[Tuple[str, ...], str]:
    async with connect() as conn:
        rows = await conn.fetch("""
            SELECT key, value FROM kv WHERE namespace = $1
            """, namespace)
        return {tuple(row["key"]): row["value"] for row in rows}

async def get_raw_glob(namespace: str, length: int, parts: Dict[int, str]) -> Dict[Tuple[str, ...], str]:
    async with connect() as conn:
        arg = 2
        clauses = []
        for k in parts:
            arg += 1
            clauses.append("key[{}] = ${}".format(k, arg))
        clause = " AND ".join(clauses) if clauses else "TRUE"
        rows = await conn.fetch("""
            SELECT key, value FROM kv
            WHERE namespace = $1 AND ARRAY_LENGTH(key, 1) = $2 AND ({})
            """.format(clause), namespace, length, *parts.values())
        return {tuple(row["key"]): row["value"] for row in rows}

async def get_namespaces() -> Sequence[str]:
    async with connect() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT namespace FROM kv
            """)
        return [row["namespace"] for row in rows]

async def set_raw_value(namespace: str, key: Sequence[str], value: Optional[str], log_value: bool = True) -> None:
    async with connect() as conn:
        if value is None:
            await conn.execute("""
                DELETE FROM kv
                WHERE namespace = $1 AND key = $2
                """, namespace, tuple(key))
        else:
            await conn.execute("""
                INSERT INTO kv (namespace, key, value)
                VALUES ($1, $2, $3)
                ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value
                """, namespace, tuple(key), value, log_data=True if log_value else {1, 2})

async def set_raw_values(namespace: str, dict: Dict[Sequence[str], Optional[str]], log_value: bool = False) -> None:
    removals = [(namespace, tuple(key)) for key, value in dict.items() if value is None]
    updates = [(namespace, tuple(key), value) for key, value in dict.items() if value is not None]
    async with connect() as conn:
        async with conn.transaction():
            if removals:
                await conn.executemany("""
                    DELETE FROM kv
                    WHERE namespace = $1 AND key = $2
                    """, removals)
            if updates:
                await conn.executemany("""
                    INSERT INTO kv (namespace, key, value)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (namespace, key) DO UPDATE SET value = EXCLUDED.value
                    """, updates, log_data=True if log_value else {1, 2})

class ConfigStore(Dict[Tuple[str, ...], str]):
    __slots__ = ("__weakref__", "ready")
    ready: asyncio.Event

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.ready = asyncio.Event()

config_stores: weakref.WeakValueDictionary[str, ConfigStore]
config_stores = weakref.WeakValueDictionary()

KeyType = Union[str, int, Sequence[Union[str, int]]]

def encode_key(key: KeyType) -> Tuple[str, ...]:
    if isinstance(key, (str, int)):
        key = (key,)
    return tuple(str(k) for k in key)

class Config:
    """
    This object encapsulates access to the key-value store for a fixed module. Upon construction we load all the pairs
    from the DB into memory. The in-memory copy is shared across Config objects for the same module.
    __iter__ and __getitem__/__getattr__ will read from this in-memory copy.
    __setitem__/__setattr__ will update the in-memory copy. awaiting will commit the keys that were modified by this
    Config object to the DB (the values may have since been overwritten by other Config objects)
    """
    __slots__ = "_namespace", "_log_value", "_store", "_dirty"
    _namespace: str
    _log_value: bool
    _store: ConfigStore
    _dirty: Set[Tuple[str, ...]]

    def __init__(self, namespace: str, log_value: bool, store: ConfigStore):
        self._namespace = namespace
        self._log_value = log_value
        self._store = store
        self._dirty = set()

    def __iter__(self) -> Iterator[Tuple[str, ...]]:
        return self._store.__iter__()

    def __getitem__(self, key: KeyType) -> Any:
        return json_decode(self._store.get(encode_key(key)))

    def __setitem__(self, key: KeyType, value: Any) -> None:
        ek = encode_key(key)
        ev = json_encode(value)
        if ev is None:
            self._store.pop(ek, None)
        else:
            self._store[ek] = ev
        self._dirty.add(ek)

    @util.asyncio.__await__
    async def __await__(self) -> None:
        dirty = self._dirty
        self._dirty = set()
        try:
            await set_raw_values(self._namespace, {key: self._store.get(key) for key in dirty})
        except:
            self._dirty.update(dirty)
            raise

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            return None
        return self[key]

    def __setattr__(self, key: str, value: Any) -> None:
        if key.startswith("_"):
            return super().__setattr__(key, value)
        self[key] = value

async def load(namespace: str, log_value: bool = False) -> Config:
    store = config_stores.get(namespace)
    if store is None:
        store = ConfigStore()
        config_stores[namespace] = store
        store.update(await get_raw_key_values(namespace))
        store.ready.set()
    await store.ready.wait()
    return Config(namespace, log_value, store)
