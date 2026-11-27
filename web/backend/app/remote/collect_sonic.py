"""Collecteur d'état exécuté DANS un routeur SONiC par l'interface web.

Envoyé tel quel dans le conteneur (`python3 -c <source> [--summary]`) : il ne doit dépendre que de
la bibliothèque standard et de swsscommon (Python 3.11 de l'image docker-sonic-vs).
Il écrit un objet JSON sur stdout ; chaque section est indépendante et ses erreurs sont reportées
dans "errors" sans empêcher la collecte des autres.
"""

import json
import os
import re
import subprocess
import sys
import time

READY_FLAG = "/var/run/lab-ready"
LANEMAP = "/usr/share/sonic/hwsku/lanemap.ini"
VERSION_FILE = "/etc/sonic/sonic_version.yml"
KEY_SERVICES = [
    "redis-server",
    "syncd",
    "orchagent",
    "portsyncd",
    "portmgrd",
    "intfmgrd",
    "zebra",
    "staticd",
    "bgpd",
    "fpmsyncd",
    "bgpcfgd",
]
_IFACE_RE = re.compile(r"^(Ethernet\d+|Loopback\d+|Vlan\d+|PortChannel\d+|eth0)$")
_SUPERVISOR_RE = re.compile(r"^(\S+)\s+([A-Z]+)\s*(.*)$")


def run(cmd, timeout=15):
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout
    )
    return proc.returncode, proc.stdout, proc.stderr


def natural_key(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


# --- analyseurs purs (testés côté backend) -------------------------------------------------
def parse_supervisorctl(text):
    services = []
    for line in text.splitlines():
        m = _SUPERVISOR_RE.match(line.strip())
        if m:
            services.append({"name": m.group(1), "state": m.group(2), "detail": m.group(3).strip()})
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
        if ":" in line and not line.startswith(("#", "---")):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip("'\"")
    return out


def summarize_bgp(data):
    result = {"router_id": None, "as": None, "peers": []}
    for afi, info in sorted(data.items()):
        if not isinstance(info, dict) or "peers" not in info:
            continue
        result["router_id"] = result["router_id"] or info.get("routerId")
        result["as"] = result["as"] or info.get("as")
        for peer, p in sorted(info["peers"].items()):
            result["peers"].append(
                {
                    "afi": afi,
                    "peer": peer,
                    "hostname": p.get("hostname"),
                    "remote_as": p.get("remoteAs"),
                    "state": p.get("state"),
                    "uptime": p.get("peerUptime"),
                    "pfx_rcd": p.get("pfxRcd"),
                    "pfx_snt": p.get("pfxSnt"),
                    "msg_rcvd": p.get("msgRcvd"),
                    "msg_sent": p.get("msgSent"),
                    "desc": p.get("desc"),
                }
            )
    return result


def summarize_routes(data):
    routes = []
    for prefix, entries in sorted(data.items(), key=lambda kv: kv[0]):
        for r in entries:
            routes.append(
                {
                    "prefix": prefix,
                    "protocol": r.get("protocol"),
                    "selected": bool(r.get("selected")),
                    "installed": bool(r.get("installed")),
                    "distance": r.get("distance"),
                    "metric": r.get("metric"),
                    "uptime": r.get("uptime"),
                    "nexthops": [
                        {
                            "ip": nh.get("ip"),
                            "interface": nh.get("interfaceName"),
                            "active": bool(nh.get("active")),
                            "connected": bool(nh.get("directlyConnected")),
                        }
                        for nh in r.get("nexthops", [])
                    ],
                }
            )
    return routes


# --- collecte ------------------------------------------------------------------------------
def collect_services():
    _, out, err = run(["supervisorctl", "status"])
    services = parse_supervisorctl(out)
    if not services:
        raise RuntimeError((err or out or "supervisord injoignable").strip())
    return services


def _dbs():
    from swsscommon.swsscommon import ConfigDBConnector, SonicV2Connector

    cfg = ConfigDBConnector()
    cfg.connect(wait_for_init=False)
    db = SonicV2Connector(use_unix_socket_path=True)
    db.connect(db.APPL_DB)
    db.connect(db.STATE_DB)
    return cfg, db


def collect_ports():
    cfg, db = _dbs()
    wires = {}
    if os.path.exists(LANEMAP):
        with open(LANEMAP) as fh:
            wires = parse_lanemap(fh.read())
    ports = []
    for name, conf in sorted(cfg.get_table("PORT").items(), key=lambda kv: natural_key(kv[0])):
        appl = dict(db.get_all(db.APPL_DB, "PORT_TABLE:" + name) or {})
        lanes = conf.get("lanes", "")
        ports.append(
            {
                "name": name,
                "alias": conf.get("alias"),
                "lanes": lanes,
                "speed": conf.get("speed"),
                "mtu": conf.get("mtu", appl.get("mtu")),
                "description": conf.get("description", ""),
                "admin_status": conf.get("admin_status", "down"),
                "oper_status": appl.get("oper_status", "down"),
                "wire": wires.get(lanes),
                "wire_up": _wire_up(wires.get(lanes)),
            }
        )
    return ports


def _wire_up(wire):
    """État administratif (IFF_UP) de l'interface noyau ethN reliée au port, None si inconnue."""
    if not wire:
        return None
    try:
        with open("/sys/class/net/%s/flags" % wire) as fh:
            return bool(int(fh.read().strip(), 16) & 1)
    except (OSError, ValueError):
        return None


def collect_interfaces():
    _, out, _ = run(["ip", "-j", "addr", "show"])
    _, stats_out, _ = run(["ip", "-j", "-s", "link", "show"])
    stats = {}
    for link in json.loads(stats_out or "[]"):
        s = link.get("stats64") or link.get("stats") or {}
        rx, tx = s.get("rx", {}), s.get("tx", {})
        stats[link.get("ifname")] = {
            "rx_bytes": rx.get("bytes", 0),
            "rx_packets": rx.get("packets", 0),
            "rx_errors": rx.get("errors", 0),
            "rx_dropped": rx.get("dropped", 0),
            "tx_bytes": tx.get("bytes", 0),
            "tx_packets": tx.get("packets", 0),
            "tx_errors": tx.get("errors", 0),
            "tx_dropped": tx.get("dropped", 0),
        }
    result = {}
    for link in json.loads(out or "[]"):
        name = link.get("ifname", "")
        if not _IFACE_RE.match(name):
            continue
        result[name] = {
            "operstate": link.get("operstate"),
            "mtu": link.get("mtu"),
            "mac": link.get("address"),
            "addresses": [
                "%s/%s" % (a.get("local"), a.get("prefixlen"))
                for a in link.get("addr_info", [])
                if a.get("family") in ("inet", "inet6") and a.get("scope") != "link"
            ],
            "stats": stats.get(name, {}),
        }
    return result


def collect_bgp():
    rc, out, err = run(["vtysh", "-c", "show bgp summary json"])
    if rc != 0:
        raise RuntimeError((err or out).strip())
    try:
        data = json.loads(out or "{}")
    except ValueError:
        data = {}
    return summarize_bgp(data)


def collect_routes():
    rc, out, err = run(["vtysh", "-c", "show ip route json"])
    if rc != 0:
        raise RuntimeError((err or out).strip())
    return summarize_routes(json.loads(out or "{}"))


def collect_neighbors():
    _, out, _ = run(["ip", "-j", "neigh", "show"])
    return [
        {"ip": n.get("dst"), "dev": n.get("dev"), "mac": n.get("lladdr"), "state": ",".join(n.get("state", []))}
        for n in json.loads(out or "[]")
        if n.get("dev") != "eth0"
    ]


def collect_lldp():
    _, db = _dbs()
    neighbors = []
    for key in sorted(db.keys(db.APPL_DB, "LLDP_ENTRY_TABLE:*") or []):
        local = key.split(":", 1)[1]
        if local == "eth0":
            continue
        entry = dict(db.get_all(db.APPL_DB, key) or {})
        neighbors.append(
            {
                "local_port": local,
                "system_name": entry.get("lldp_rem_sys_name"),
                "port_id": entry.get("lldp_rem_port_id"),
                "port_desc": entry.get("lldp_rem_port_desc"),
                "mgmt_ip": entry.get("lldp_rem_man_addr"),
            }
        )
    return neighbors


def collect_version():
    with open(VERSION_FILE) as fh:
        info = parse_version_yml(fh.read())
    _, frr, _ = run(["vtysh", "-c", "show version"])
    first = frr.strip().splitlines()[0] if frr.strip() else ""
    info["frr"] = first.split(" (")[0]
    return info


def main(argv):
    summary = "--summary" in argv
    out = {"ts": time.time(), "summary": summary, "ready": os.path.exists(READY_FLAG), "errors": {}}
    sections = [
        ("services", collect_services),
        ("ports", collect_ports),
        ("interfaces", collect_interfaces),
        ("bgp", collect_bgp),
    ]
    if not summary:
        sections += [
            ("routes", collect_routes),
            ("neighbors", collect_neighbors),
            ("lldp", collect_lldp),
            ("version", collect_version),
        ]
    for name, fn in sections:
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 - une section en échec ne bloque pas les autres
            out["errors"][name] = "%s: %s" % (type(exc).__name__, exc)
    out["key_services"] = KEY_SERVICES
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1:])
