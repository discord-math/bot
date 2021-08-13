"""
A simple database migration manager. A module can request to initialize something in the database with the @init_for
and @init decorators.
"""

import logging
import static_config
import hashlib
import plugins
import util.db as db
from typing import Callable

logger = logging.getLogger(__name__)

meta_initialized = False

async def initialize_meta() -> None:
    global meta_initialized
    if not meta_initialized:
        logger.debug("Initializing migration metadata")
        try:
            async with db.connection() as conn:
                await conn.execute("""
                    CREATE SCHEMA IF NOT EXISTS meta
                    """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS meta.schema_hashes
                    ( name TEXT NOT NULL PRIMARY KEY
                    , sha1 BYTEA NOT NULL )
                    """)
        finally:
            meta_initialized = True

async def init_for(name: str, schema: str) -> None:
    """
    Pass DDL SQL statements to initialize something in the database.

    await init_for("module name", "CREATE TABLE foo (bar TEXT)")

    The SQL will be hashed. If a hash for this module doesn't yet exist the SQL code will be executed and the
    hash saved. If the known hash for the module matches the computed one, nothing happens. Otherwise we look for a
    migration file in a configurable directory and run it, updating the known hash.
    """
    logger.debug("Schema for {}:\n{}".format(name, schema))
    async with db.connection() as conn:
        async with conn.transaction():
            await initialize_meta()
            old_sha = await conn.fetchval("SELECT sha1 FROM meta.schema_hashes WHERE name = $1", name)
            sha = hashlib.sha1(schema.encode("utf")).digest()
            logger.debug("{}: old {} new {}".format(name, old_sha.hex() if old_sha is not None else None, sha.hex()))
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
                        logger.debug("{}: Loading migration {}".format(name, filename))
                        await conn.execute(fp.read())
                        await conn.execute("UPDATE meta.schema_hashes SET sha1 = $2 WHERE name = $1", name, sha)
            else:
                await conn.execute(schema)
                await conn.execute("INSERT INTO meta.schema_hashes (name, sha1) VALUES ($1, $2)", name, sha)

async def init(schema: str) -> None:
    """
    Request database initialization for the current plugin.
    """
    await init_for(plugins.current_plugin(), schema)
