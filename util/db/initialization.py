"""
A simple database migration manager. A module can request to initialize
something in the database with the @init_for and @init decorators.
"""

import static_config
import hashlib
import plugins
import util.db as db

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

def init_for(name):
    """
    Decorate a function that returns a piece of SQL to initialize something in
    the database.

    @init_for("module name")
    def init():
        return "CREATE TABLE foo (bar TEXT)"

    The returned SQL will be hashed. If a hash for this module doesn't yet exist
    the SQL code will be executed and the hash saved. If the known hash for the
    module matches the computed one, nothing happens. Otherwise we look for a
    migration file in a configurable directory and run it, updating the known
    hash.
    """
    def init(fun):
        conn = db.connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sha1 FROM meta.schema_hashes WHERE name = %(name)s
                """, {"name": name})
            old_sha = cur.fetchone()
            sql = fun()
            sha = hashlib.sha1(sql.encode("utf")).digest()
            if old_sha:
                old_sha = bytes(old_sha[0])
                if old_sha != sha:
                    for dirname in static_config.DB["migrations"].split(":"):
                        filename = "{}/{}-{}-{}.sql".format(
                            dirname, name, old_sha.hex(), sha.hex())
                        try:
                            fp = open(filename, "r", encoding="utf")
                            break
                        except FileNotFoundError:
                            continue
                    else:
                        raise FileNotFoundError(
                            "Could not find {}-{}-{}.sql in {}".format(
                                name, old_sha.hex(), sha.hex(),
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

def init(fun):
    """
    Request database initialization for the current plugin.
    """
    return init_for(plugins.current_plugin())(fun)
