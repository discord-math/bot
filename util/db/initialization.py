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
                    filename = "{}/{}-{}-{}.sql".format(
                        static_config.DB["migrations"],
                        name,
                        old_sha.hex(),
                        sha.hex())
                    with open(filename, "r", encoding="utf") as f:
                        cur.execute(f.read())
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
    return init_for(plugins.current_plugin())(fun)
