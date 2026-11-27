"""Collecteur de métriques exécuté DANS un routeur SONiC (envoyé par lab_collector via docker exec).

Lancement : python3 -c <ce source> '<options JSON>'. Il ne dépend que de la bibliothèque standard et de
redis-py, tous deux présents dans l'image docker-sonic-vs (Python 3.11). Il écrit un objet JSON sur
stdout. Chaque section est indépendante : une erreur est reportée dans "errors" sans bloquer les autres.

Les données sont renvoyées brutes et sans liste figée (tous les netdevs, toutes les clés redis, tous les
VRF FRR, tous les programmes supervisord) : un élément ajouté au routeur apparaît au cycle suivant. La
conversion en points InfluxDB est faite côté Telegraf (labmon/sonic.py).
"""

import calendar
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

DB_CONFIG = "/var/run/redis/sonic-db/database_config.json"
READY_FLAG = "/var/run/lab-ready"
LANEMAP = "/usr/share/sonic/hwsku/lanemap.ini"
VERSION_FILE = "/etc/sonic/sonic_version.yml"
SYSLOG = "/var/log/syslog"
SYS_NET = "/sys/class/net"
SEVERITIES = ("EMERG", "ALERT", "CRIT", "ERR", "WARNING", "NOTICE", "INFO", "DEBUG")
DEFAULTS = {
    "routes_max": 500,
    "counters_max_keys": 5000,
    "syslog": None,  # {"inode": …, "offset": …} renvoyé au cycle précédent
    "syslog_max_bytes": 64 * 1024 * 1024,
    "events_max": 200,
    "event_regex": r"%ADJCHANGE|%NOTIFICATION|oper state set from|lab-bootstrap",
}
_SUPERVISOR_RE = re.compile(r"^(\S+)\s+([A-Z]+)\s*(.*)$")
_PID_RE = re.compile(r"\bpid (\d+)")
_UPTIME_RE = re.compile(r"uptime (?:(\d+) days?, )?(\d+):(\d\d):(\d\d)")
_OID_KEY_RE = re.compile(r"^([A-Z0-9_]+):(oid:0x[0-9a-fA-F]+)$")
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


# --- utilitaires ------------------------------------------------------------------------------
def run(cmd, timeout=15):
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout
    )
    return proc.returncode, proc.stdout, proc.stderr


def vtysh_json(command):
    rc, out, err = run(["vtysh", "-c", command])
    if rc != 0:
        raise RuntimeError((err or out).strip()[:300] or "vtysh : code %d" % rc)
    return json.loads(out or "{}")


def read_text(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def read_int(path):
    value = read_text(path)
    if value is None:
        return None
    try:
        return int(value, 0) if value.startswith("0x") else int(value)
    except ValueError:
        return None


def read_uptime():
    with open("/proc/uptime") as fh:
        return float(fh.read().split()[0])


def is_number(value):
    return isinstance(value, str) and bool(_NUMBER_RE.match(value.strip()))


# --- analyseurs purs (testés côté Telegraf) ------------------------------------------------------
def parse_supervisorctl(text):
    services = []
    for line in text.splitlines():
        m = _SUPERVISOR_RE.match(line.strip())
        if not m:
            continue
        detail = m.group(3).strip()
        svc = {"name": m.group(1), "state": m.group(2), "detail": detail}
        pid = _PID_RE.search(detail)
        if pid and m.group(2) == "RUNNING":
            svc["pid"] = int(pid.group(1))
        up = _UPTIME_RE.search(detail)
        if up:
            days, h, mi, s = (int(x or 0) for x in up.groups())
            svc["uptime_s"] = days * 86400 + h * 3600 + mi * 60 + s
        services.append(svc)
    return services


def parse_lanemap(text):
    """lanemap.ini (ethN:lanes) -> {lanes: ethN}."""
    out = {}
    for line in text.splitlines():
        if ":" in line:
            eth, _, lanes = line.strip().partition(":")
            out[lanes.strip()] = eth.strip()
    return out


def parse_version_yml(text):
    out = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith(("#", "---", " ", "-")):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip("'\"")
    return out


def parse_proc_stat(text):
    """Contenu de /proc/<pid>/stat -> champs utiles (le nom du processus peut contenir des espaces)."""
    rest = text[text.rfind(")") + 2 :].split()
    return {
        "ppid": int(rest[1]),
        "ticks": int(rest[11]) + int(rest[12]),
        "threads": int(rest[17]),
        "rss_pages": int(rest[21]),
    }


def table_of(key, separator):
    """Nom de table d'une clé redis SONiC (ASIC_DB : une table par type d'objet SAI)."""
    if key.startswith("ASIC_STATE:"):
        return ":".join(key.split(":", 2)[:2])
    return key.split(separator, 1)[0]


def program_of(token):
    """Jeton programme du syslog SONiC ('bgp#bgpd[707]:', '#orchagent:', '#lldp-syncd') -> nom court."""
    prog = token.rstrip(":")
    prog = re.sub(r"\[\d+\]$", "", prog)
    if "#" in prog:
        prog = prog.rsplit("#", 1)[1]
    return prog or "unknown"


def parse_syslog_line(line):
    """'Nov 19 13:23:59.148617 sonic1 NOTICE #orchagent: message' -> (horodatage, sévérité, programme, message)."""
    parts = line.split(None, 5)
    if len(parts) < 6 or parts[4] not in SEVERITIES:
        return None
    token, _, message = parts[5].partition(" ")
    return " ".join(parts[:3]), parts[4], program_of(token), message.strip()


def syslog_epoch(stamp, now=None):
    """'Nov 19 13:23:59.148617' (UTC, sans année) -> epoch ; l'année est celle de now, ou la précédente."""
    now = now or time.time()
    year = datetime.fromtimestamp(now, tz=timezone.utc).year
    fmt = "%Y %b %d %H:%M:%S.%f" if "." in stamp else "%Y %b %d %H:%M:%S"
    dt = datetime.strptime("%d %s" % (year, " ".join(stamp.split())), fmt)
    epoch = calendar.timegm(dt.timetuple()) + dt.microsecond / 1e6
    if epoch > now + 86400:
        epoch = calendar.timegm(dt.replace(year=year - 1).timetuple()) + dt.microsecond / 1e6
    return epoch


def summarize_routes(data, limit):
    """Sortie de 'show ip route vrf all json' -> {vrf: {préfixe: [routes]}}, plafonnée à limit préfixes."""
    out, kept, truncated = {}, 0, False
    for vrf in sorted(data):
        prefixes = data[vrf]
        if not isinstance(prefixes, dict):
            continue
        for prefix in sorted(prefixes):
            if kept >= limit:
                truncated = True
                break
            entries = prefixes[prefix]
            if not isinstance(entries, list):
                continue
            kept += 1
            out.setdefault(vrf, {})[prefix] = [
                {
                    "protocol": r.get("protocol"),
                    "selected": bool(r.get("selected")),
                    "installed": bool(r.get("installed")),
                    "offloaded": bool(r.get("offloaded")),
                    "distance": r.get("distance"),
                    "metric": r.get("metric"),
                    "uptime": r.get("uptime"),
                    "nexthops": [
                        {
                            "ip": nh.get("ip"),
                            "interface": nh.get("interfaceName"),
                            "active": bool(nh.get("active")),
                            "fib": bool(nh.get("fib")),
                            "connected": bool(nh.get("directlyConnected")),
                        }
                        for nh in r.get("nexthops", [])
                    ],
                }
                for r in entries
            ]
    return out, truncated


# --- bases redis -------------------------------------------------------------------------------
def connect_dbs():
    """Une connexion par base distincte de database_config.json : {nom: (connexion, séparateur)}."""
    import redis

    with open(DB_CONFIG) as fh:
        cfg = json.load(fh)
    instances = cfg.get("INSTANCES", {})
    out, seen = {}, set()
    for name in sorted(cfg.get("DATABASES", {})):
        db = cfg["DATABASES"][name]
        ident = (db.get("instance"), db.get("id"))
        if ident in seen:  # ex. FLEX_COUNTER_DB et PFC_WD_DB partagent la base 5
            continue
        seen.add(ident)
        inst = instances.get(db.get("instance"), {})
        if inst.get("unix_socket_path"):
            conn = redis.Redis(unix_socket_path=inst["unix_socket_path"], db=db["id"], decode_responses=True)
        else:
            conn = redis.Redis(
                host=inst.get("hostname", "127.0.0.1"), port=inst.get("port", 6379), db=db["id"], decode_responses=True
            )
        out[name] = (conn, db.get("separator", ":"))
    return out


def hgetall_many(conn, keys):
    pipe = conn.pipeline(transaction=False)
    for key in keys:
        pipe.hgetall(key)
    return dict(zip(keys, pipe.execute()))


# --- sections ----------------------------------------------------------------------------------
def collect_meta(dbs):
    meta = {"hostname": socket.gethostname(), "ready": os.path.exists(READY_FLAG), "kernel": os.uname().release}
    text = read_text(VERSION_FILE)
    if text:
        meta["version"] = parse_version_yml(text)
    if "CONFIG_DB" in dbs:
        meta["device"] = dbs["CONFIG_DB"][0].hgetall("DEVICE_METADATA|localhost")
    _, out, _ = run(["vtysh", "-c", "show version"])
    first = out.strip().splitlines()[0] if out.strip() else ""
    meta["frr"] = first.split(" (")[0]
    _, out, _ = run(["vtysh", "-c", "show daemons"])
    meta["frr_daemons"] = out.split()
    return meta


def collect_netdev():
    out = {}
    for name in sorted(os.listdir(SYS_NET)):
        if name == "lo":
            continue
        base = os.path.join(SYS_NET, name)
        stats = {}
        try:
            entries = os.listdir(os.path.join(base, "statistics"))
        except OSError:
            entries = []
        for stat in entries:
            value = read_int(os.path.join(base, "statistics", stat))
            if value is not None:
                stats[stat] = value
        out[name] = {
            "stats": stats,
            "operstate": read_text(base + "/operstate"),
            "carrier": read_int(base + "/carrier"),
            "mtu": read_int(base + "/mtu"),
            "speed": read_int(base + "/speed"),
            "flags": read_int(base + "/flags"),
            "address": read_text(base + "/address"),
        }
    return out


def collect_addresses():
    rc, out, err = run(["ip", "-j", "addr", "show"])
    if rc != 0:
        raise RuntimeError(err.strip()[:300])
    result = {}
    for link in json.loads(out or "[]"):
        result[link.get("ifname")] = [
            {"family": a.get("family"), "address": a.get("local"), "prefixlen": a.get("prefixlen"), "scope": a.get("scope")}
            for a in link.get("addr_info", [])
        ]
    return result


def collect_ports(dbs):
    def table(dbname, prefix):
        conn, sep = dbs[dbname]
        keys = sorted(conn.scan_iter(match=prefix + sep + "*", count=1000))
        return {k.split(sep, 1)[1]: v for k, v in hgetall_many(conn, keys).items()}

    lanemap = {}
    text = read_text(LANEMAP)
    if text:
        lanemap = parse_lanemap(text)
    return {
        "config": table("CONFIG_DB", "PORT"),
        "appl": table("APPL_DB", "PORT_TABLE"),
        "state": table("STATE_DB", "PORT_TABLE"),
        "lanemap": lanemap,
    }


def collect_db(dbs):
    out = {}
    for name, (conn, sep) in dbs.items():
        counts = {}
        for key in conn.scan_iter(count=1000):
            table = table_of(key, sep)
            counts[table] = counts.get(table, 0) + 1
        if counts:
            out[name] = counts
    return out


def collect_counters(dbs, limit):
    conn, _ = dbs["COUNTERS_DB"]
    keys = sorted(conn.scan_iter(count=1000))
    maps = [k for k in keys if k.startswith("COUNTERS_") and k.endswith("_NAME_MAP")]
    names = {}
    for key, mapping in hgetall_many(conn, maps).items():
        otype = key[len("COUNTERS_") : -len("_NAME_MAP")]
        for name, oid in mapping.items():
            names[oid] = (otype, name)
    candidates = [k for k in keys if not k.endswith("_MAP")]
    truncated = len(candidates) > limit
    candidates = candidates[:limit]
    pipe = conn.pipeline(transaction=False)
    for key in candidates:
        pipe.type(key)
    hashes = [k for k, t in zip(candidates, pipe.execute()) if t == "hash"]
    crm, objects = {}, []
    for key, values in hgetall_many(conn, hashes).items():
        if key.startswith("CRM:"):
            crm[key[4:]] = values
            continue
        m = _OID_KEY_RE.match(key)
        if m:
            source = m.group(1)
            otype, name = names.get(m.group(2), ("OID", m.group(2)))
        else:
            parts = key.split(":", 2)
            source = parts[0]
            otype = parts[1] if len(parts) > 1 else source
            name = parts[2] if len(parts) > 2 else otype
        numeric = {f: v for f, v in values.items() if is_number(v)}
        if numeric:
            objects.append({"source": source, "type": otype, "object": name, "values": numeric})
    return {"objects": objects, "crm": crm, "truncated": truncated}


def collect_redis(dbs):
    conn, _ = next(iter(dbs.values()))
    return conn.info()


def read_procs():
    procs = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry) as fh:
                procs[int(entry)] = parse_proc_stat(fh.read())
        except (OSError, ValueError, IndexError):
            continue
    return procs


def collect_services():
    _, out, err = run(["supervisorctl", "status"])
    services = parse_supervisorctl(out)
    if not services:
        raise RuntimeError((err or out or "supervisord injoignable").strip()[:300])
    procs = read_procs()
    children = {}
    for pid, p in procs.items():
        children.setdefault(p["ppid"], []).append(pid)
    tick = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    for svc in services:
        root = svc.get("pid")
        if root not in procs:
            continue
        todo, tree = [root], []
        while todo:  # le programme et tous ses descendants (scripts lanceurs, workers…)
            pid = todo.pop()
            tree.append(pid)
            todo.extend(children.get(pid, []))
        fds = 0
        for pid in tree:
            try:
                fds += len(os.listdir("/proc/%d/fd" % pid))
            except OSError:
                pass
        svc["proc"] = {
            "processes": len(tree),
            "cpu_seconds": sum(procs[p]["ticks"] for p in tree) / tick,
            "threads": sum(procs[p]["threads"] for p in tree),
            "rss_bytes": sum(procs[p]["rss_pages"] for p in tree) * page,
            "fds": fds,
        }
    return services


def collect_bgp():
    return {
        "summary": vtysh_json("show bgp vrf all summary json"),
        "neighbors": vtysh_json("show bgp vrf all neighbors json"),
    }


def collect_routes(limit):
    out = {}
    for afi, cmd in (("ipv4", "ip"), ("ipv6", "ipv6")):
        routes, truncated = summarize_routes(vtysh_json("show %s route vrf all json" % cmd), limit)
        out[afi] = {
            "summary": vtysh_json("show %s route vrf all summary json" % cmd),
            "routes": routes,
            "truncated": truncated,
        }
    return out


def collect_neighbors():
    rc, out, err = run(["ip", "-j", "neigh", "show"])
    if rc != 0:
        raise RuntimeError(err.strip()[:300])
    return [
        {"dst": n.get("dst"), "dev": n.get("dev"), "lladdr": n.get("lladdr"), "state": n.get("state") or []}
        for n in json.loads(out or "[]")
    ]


def collect_lldp(dbs):
    conn, sep = dbs["APPL_DB"]
    keys = sorted(conn.scan_iter(match="LLDP_ENTRY_TABLE" + sep + "*", count=1000))
    return {k.split(sep, 1)[1]: v for k, v in hgetall_many(conn, keys).items()}


def collect_syslog(opts):
    """Lecture incrémentale du syslog : comptes par (programme, sévérité) et lignes « événements »."""
    prev = opts.get("syslog") or {}
    st = os.stat(SYSLOG)
    start = prev.get("offset", 0) if prev.get("inode") == st.st_ino else 0
    if start > st.st_size:  # fichier tronqué
        start = 0
    reset = start == 0
    if reset and st.st_size > opts["syslog_max_bytes"]:
        start = st.st_size - opts["syslog_max_bytes"]
    with open(SYSLOG, "rb") as fh:
        fh.seek(start)
        data = fh.read(min(st.st_size - start, opts["syslog_max_bytes"]))
    if reset and start > 0:  # on ignore la ligne coupée par le plafond
        cut = data.find(b"\n") + 1
        data, start = data[cut:], start + cut
    last = data.rfind(b"\n")
    end = start + last + 1 if last >= 0 else start
    counts, events = {}, []
    pattern = re.compile(opts["event_regex"]) if opts.get("event_regex") else None
    now = time.time()
    for line in data[: last + 1].decode("utf-8", "replace").splitlines():
        parsed = parse_syslog_line(line)
        if parsed is None:
            key = "unknown\tUNKNOWN"
            counts[key] = counts.get(key, 0) + 1
            continue
        stamp, severity, program, message = parsed
        key = program + "\t" + severity
        counts[key] = counts.get(key, 0) + 1
        if pattern and pattern.search(line) and len(events) < opts["events_max"]:
            try:
                ts = syslog_epoch(stamp, now)
            except ValueError:
                continue
            events.append({"ts": ts, "severity": severity, "program": program, "message": message})
    return {"inode": st.st_ino, "start": start, "end": end, "reset": reset, "counts": counts, "events": events}


def main(argv):
    started = time.time()
    opts = dict(DEFAULTS)
    if argv:
        opts.update(json.loads(argv[0]))
    out = {"ts": started, "uptime": read_uptime(), "errors": {}}
    try:
        dbs = connect_dbs()
    except Exception as exc:  # noqa: BLE001
        dbs = {}
        out["errors"]["redis"] = "%s: %s" % (type(exc).__name__, exc)
    sections = {
        "meta": lambda: collect_meta(dbs),
        "netdev": collect_netdev,
        "addresses": collect_addresses,
        "neighbors": collect_neighbors,
        "services": collect_services,
        "bgp": collect_bgp,
        "routes": lambda: collect_routes(opts["routes_max"]),
        "syslog": lambda: collect_syslog(opts),
    }
    if dbs:
        sections.update(
            {
                "ports": lambda: collect_ports(dbs),
                "db": lambda: collect_db(dbs),
                "counters": lambda: collect_counters(dbs, opts["counters_max_keys"]),
                "redis": lambda: collect_redis(dbs),
                "lldp": lambda: collect_lldp(dbs),
            }
        )
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {name: pool.submit(fn) for name, fn in sections.items()}
        for name, future in futures.items():
            try:
                out[name] = future.result()
            except Exception as exc:  # noqa: BLE001 - une section en échec ne bloque pas les autres
                out["errors"][name] = "%s: %s" % (type(exc).__name__, exc)
    out["duration"] = time.time() - started
    sys.stdout.write(json.dumps(out, separators=(",", ":"), default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
