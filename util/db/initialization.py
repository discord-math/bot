"""
A simple database migration manager. A module can request to initialize something in the database with the @init_for
and @init decorators.
"""

import static_config
import hashlib
import plugins
import util.db as db
from typing import Callable

with db.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE SCHEMA IF NOT EXISTS meta
            """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta.schema_hashes
            ( name TEXT NOT NULL PRIMARY KEY
            , sha1 BYTEA NOT NULL )
            """)

def init_for(name: str) -> Callable[[Callable[[], str]], Callable[[], str]]:
    """
    Decorate a function that returns a piece of SQL to initialize something in the database.

    @init_for("module name")
    def init():
        return "CREATE TABLE foo (bar TEXT)"

    The returned SQL will be hashed. If a hash for this module doesn't yet exist the SQL code will be executed and the
    hash saved. If the known hash for the module matches the computed one, nothing happens. Otherwise we look for a
    migration file in a configurable directory and run it, updating the known hash.
    """
    def init(fun: Callable[[], str]) -> Callable[[], str]:
        conn = db.connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sha1 FROM meta.schema_hashes WHERE name = %(name)s
                """, {"name": name})
            old_row = cur.fetchone()
            sql = fun()
            sha = hashlib.sha1(sql.encode("utf")).digest()
            if old_row is not None:
                old_sha = bytes(old_row[0])
                if old_sha != sha:
                    for dirname in static_config.DB["migrations"].split(":"):
                        filename = "{}/{}-{}-{}.sql".format(dirname, name, old_sha.hex(), sha.hex())
                        try:
                            fp = open(filename, "r", encoding="utf")
                            break
                        except FileNotFoundError:
                            continue
                    else:
                        raise FileNotFoundError(
                            "Could not find {}-{}-{}.sql in {}".format(name, old_sha.hex(), sha.hex(),
                                static_config.DB["migrations"]))
                    with fp:
                        cur.execute(fp.read())
                    cur.execute("""
                        UPDATE meta.schema_hashes
                        SET sha1 = %(sha)s
                        WHERE name = %(name)s
                        """, {"name": name, "sha": sha})
                    conn.commit()
            else:
                cur.execute(sql)
                cur.execute("""
                    INSERT INTO meta.schema_hashes (name, sha1)
                    VALUES (%(name)s, %(sha)s)
                    """, {"name": name, "sha": sha})
                conn.commit()
        return fun
    return init

def init(fun: Callable[[], str]) -> Callable[[], str]:
    """
    Request database initialization for the current plugin.
    """
    return init_for(plugins.current_plugin())(fun)

meta_initialized = False

async def initialize_meta() -> None:
    global meta_initialized
    if not meta_initialized:
        conn = await db.connection_async()
        try:
            await conn.execute("""
                CREATE SCHEMA IF NOT EXISTS meta
                """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS meta.schema_hashes
                ( name TEXT NOT NULL PRIMARY KEY
                , sha1 BYTEA NOT NULL )
                """)
        finally:
            await conn.close()
            meta_initialized = True

async def init_async_for(name: str, schema: str) -> None:
    """
    Pass DDL SQL statements to initialize something in the database.

    await init_for("module name", "CREATE TABLE foo (bar TEXT)")

    The SQL will be hashed. If a hash for this module doesn't yet exist the SQL code will be executed and the
    hash saved. If the known hash for the module matches the computed one, nothing happens. Otherwise we look for a
    migration file in a configurable directory and run it, updating the known hash.
    """
    conn = await db.connection_async()
    try:
        async with conn.transaction():
            await initialize_meta()
            old_sha = await conn.fetchval("SELECT sha1 FROM meta.schema_hashes WHERE name = $1", name)
            sha = hashlib.sha1(schema.encode("utf")).digest()
            if old_sha is not None:
                if old_sha != sha:
                    for dirname in static_config.DB["migrations"].split(":"):
                        filename = "{}/{}-{}-{}.sql".format(dirname, name, old_sha.hex(), sha.hex())
                        try:
                            fp = open(filename, "r", encoding="utf")
                            break
                        except FileNotFoundError:
                            continue
                    else:
                        raise FileNotFoundError(
                            "Could not find {}-{}-{}.sql in {}".format(name, old_sha.hex(), sha.hex(),
                                static_config.DB["migrations"]))
                    with fp:
                        await conn.execute(fp.read())
                        await conn.execute("UPDATE meta.schema_hashes SET sha1 = $2 WHERE name = $1", name, sha)
            else:
                await conn.execute(schema)
                await conn.execute("INSERT INTO meta.schema_hashes (name, sha1) VALUES ($1, $2)", name, sha)
    finally:
        await conn.close()

async def init_async(schema: str) -> None:
    """
    Request database initialization for the current plugin.
    """
    await init_async_for(plugins.current_plugin(), schema)
