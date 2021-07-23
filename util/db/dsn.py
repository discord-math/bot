import re
import urllib.parse

dsn_re: re.Pattern[str] = re.compile(r"\s*(\w*)\s*=\s*(?:([^\s' \\]+)|'((?:[^'\\]|\\.)*)')\s*")
unquote_re: re.Pattern[str] = re.compile(r"\\(.)")

def dsn_to_uri(dsn: str) -> str:
    """
    Convert a key=value style DSN into a postgres:// URI
    """
    if dsn.startswith("postgres://") or dsn.startswith("postgresql://"):
        return dsn
    if "=" not in dsn:
        return "postgres://" + urllib.parse.quote(dsn, safe="")
    kvs = []
    for key, val, val_quoted in dsn_re.findall(dsn):
        if not val:
            val = unquote_re.sub(r"\1", val_quoted)
        kvs.append((key, val))
    return "postgres://?" + urllib.parse.urlencode(kvs)

def uri_to_asyncpg(uri: str) -> str:
    return "postgresql+asyncpg://?dsn=" + urllib.parse.quote(uri, safe="")
