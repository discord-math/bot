import util.db.kv
import sys

def usage() -> None:
    print("Usage:", file=sys.stderr)
    print("    python -m util.db.kv", file=sys.stderr)
    print("        ( --delete <namespace> <key>", file=sys.stderr)
    print("        | [<namespace> [<key> [<value>]]] ) ", file=sys.stderr)

if len(sys.argv) == 1:
    for nsp in util.db.kv.get_namespaces():
        print(nsp)
elif sys.argv[1] == "--delete":
    if len(sys.argv) == 4:
        nsp = sys.argv[2]
        key = sys.argv[3]
        util.db.kv.set_value(nsp, key, None)
    else:
        usage()
elif len(sys.argv) == 2:
    nsp = sys.argv[1]
    for key, _ in util.db.kv.get_key_values(nsp):
        print(key)
elif len(sys.argv) == 3:
    nsp = sys.argv[1]
    key = sys.argv[2]
    value = util.db.kv.get_value(nsp, key)
    if value != None:
        print(value)
elif len(sys.argv) == 4:
    nsp = sys.argv[1]
    key = sys.argv[2]
    value = sys.argv[3]
    util.db.kv.set_value(nsp, key, value)
else:
    usage()
