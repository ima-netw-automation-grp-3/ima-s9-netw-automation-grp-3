"""Applique une liste d'opérations CONFIG_DB, exécuté DANS un routeur SONiC.

Usage : python3 -c <source> <fichier-ops.json>
  ops = [{"op": "set"|"mod"|"delete", "table": ..., "key": ..., "data": {...}|null}, ...]
  - set    : remplace l'entrée entière (ConfigDBConnector.set_entry) ;
  - mod    : fusionne les champs (ConfigDBConnector.mod_entry) ;
  - delete : supprime l'entrée (set_entry(..., None)).
Les démons SONiC (orchagent, intfmgrd, ...) réagissent aux changements de CONFIG_DB.
Écrit {"results": [...], "ok": bool} sur stdout. stdlib + swsscommon uniquement.
"""

import json
import sys


def main(argv):
    from swsscommon.swsscommon import ConfigDBConnector

    with open(argv[0]) as fh:
        ops = json.load(fh)
    db = ConfigDBConnector()
    db.connect(wait_for_init=False)
    results = []
    for op in ops:
        res = {"op": op["op"], "table": op["table"], "key": op["key"], "ok": True}
        try:
            if op["op"] == "delete":
                db.set_entry(op["table"], op["key"], None)
            elif op["op"] == "set":
                db.set_entry(op["table"], op["key"], op.get("data") or {})
            elif op["op"] == "mod":
                db.mod_entry(op["table"], op["key"], op.get("data") or {})
            else:
                raise ValueError("opération inconnue : %r" % op["op"])
        except Exception as exc:  # noqa: BLE001 - on continue et on rapporte chaque échec
            res["ok"] = False
            res["error"] = "%s: %s" % (type(exc).__name__, exc)
        results.append(res)
    sys.stdout.write(json.dumps({"results": results, "ok": all(r["ok"] for r in results)}))


if __name__ == "__main__":
    main(sys.argv[1:])
