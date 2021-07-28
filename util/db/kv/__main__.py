import asyncio
import sys
import util.db.kv

def usage() -> None:
    print("Usage:", file=sys.stderr)
    print("    python -m util.db.kv", file=sys.stderr)
    print("        ( --delete <namespace> <key1,key2,...>", file=sys.stderr)
    print("        | [<namespace> [<key1,key2,...> [<value>]]] ) ", file=sys.stderr)

async def main() -> None:
    if len(sys.argv) == 1:
        for nsp in await util.db.kv.get_namespaces():
            print(nsp)
    elif sys.argv[1] == "--delete":
        if len(sys.argv) == 4:
            nsp = sys.argv[2]
            key = sys.argv[3].split(",")
            await util.db.kv.set_raw_value(nsp, key, None)
        else:
            usage()
    elif len(sys.argv) == 2:
        nsp = sys.argv[1]
        for tkey in await util.db.kv.get_raw_key_values(nsp):
            print(",".join(tkey))
    elif len(sys.argv) == 3:
        nsp = sys.argv[1]
        key = sys.argv[2].split(",")
        value = await util.db.kv.get_raw_value(nsp, key)
        if value != None:
            print(value)
    elif len(sys.argv) == 4:
        nsp = sys.argv[1]
        key = sys.argv[2].split(",")
        value = sys.argv[3]
        await util.db.kv.set_raw_value(nsp, key, value)
    else:
        usage()

asyncio.run(main())
