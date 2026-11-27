"""Conversion de la sortie de remote/linux_collect.sh (nœud Linux, busybox ou non) en points InfluxDB."""

from __future__ import annotations

import re
from typing import Any

from .common import NodeObs, is_lab_address, mgmt_network
from .lineproto import Point, num
from .state import NodeState

_ADDR_RE = re.compile(r"^\d+:\s+(\S+)\s+(inet6?)\s+([^/\s]+)/(\d+)(?:.*?\sscope\s+(\S+))?")
_PROTO_RE = re.compile(r"\bproto\s+(\S+)")
_NEIGH_STATES = {"REACHABLE", "STALE", "DELAY", "PROBE", "FAILED", "INCOMPLETE", "PERMANENT", "NOARP", "NONE"}


def split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("@"):
            current = line[1:].strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def parse_netdev(lines: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) < 4:
            if len(parts) == 3:  # attribut vide (ex. adresse MAC absente)
                parts.append("")
            else:
                continue
        kind, iface, attr, value = parts
        nd = out.setdefault(iface, {"stats": {}})
        if kind == "stat":
            n = num(value)
            if n is not None:
                nd["stats"][attr] = int(n)
        elif attr in ("operstate", "address"):
            nd[attr] = value.strip()
        else:
            try:
                nd[attr] = int(value.strip(), 0)
            except ValueError:
                nd[attr] = None
    return out


def parse_addresses(lines: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Sortie de `ip -o addr` (busybox ou iproute2) -> {interface: [adresses]}."""
    out: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        m = _ADDR_RE.match(line)
        if not m:
            continue
        iface, family, address, prefixlen, scope = m.groups()
        out.setdefault(iface.split("@")[0], []).append(
            {"family": family, "address": address, "prefixlen": int(prefixlen), "scope": scope or "global"}
        )
    return out


def parse_route(line: str) -> dict[str, Any] | None:
    tokens = line.split()
    if not tokens:
        return None
    prefix = tokens[0]
    if prefix in ("unreachable", "blackhole", "prohibit", "local", "broadcast", "multicast") and len(tokens) > 1:
        prefix = tokens[1]
    info: dict[str, Any] = {"prefix": prefix, "via": None, "dev": None, "proto": None, "metric": None}
    for key in ("via", "dev", "metric", "src", "scope"):
        if key in tokens:
            i = tokens.index(key)
            if i + 1 < len(tokens):
                info[key] = tokens[i + 1]
    m = _PROTO_RE.search(line)
    if m:
        info["proto"] = m.group(1)
    if info["proto"] is None:
        # busybox n'affiche pas toujours « proto » : connecté sans passerelle, statique sinon
        info["proto"] = "static" if info["via"] else "connected"
    return info


def parse_neighbor(line: str) -> dict[str, Any] | None:
    tokens = line.split()
    if len(tokens) < 3:
        return None
    dev = tokens[tokens.index("dev") + 1] if "dev" in tokens and tokens.index("dev") + 1 < len(tokens) else "?"
    state = next((t for t in reversed(tokens) if t in _NEIGH_STATES), "NONE")
    return {"dst": tokens[0], "dev": dev, "state": state}


def build(obs: NodeObs, st: NodeState, text: str, ts_ns: int, mgmt_subnet: str | None, routes_max: int) -> NodeObs:
    sections = split_sections(text)
    if "end" not in sections:
        obs.errors["output"] = "sortie incomplète du collecteur Linux"
    meta: dict[str, str] = {}
    for line in sections.get("meta", []):
        key, _, value = line.partition("=")
        meta[key.strip()] = value.strip()
    obs.clock = num(meta.get("uptime"))
    obs.netdev = parse_netdev(sections.get("netdev", []))
    obs.addresses = parse_addresses(sections.get("addr", []))
    mgmt = mgmt_network(mgmt_subnet)
    configured = any(
        is_lab_address(a["address"], a["scope"], mgmt) for name, addrs in obs.addresses.items() if name != "eth0" for a in addrs
    )
    obs.ready = configured
    obs.node_fields.update(
        ready=configured,
        hostname=meta.get("hostname", ""),
        kernel=meta.get("kernel", ""),
        os=meta.get("os", ""),
        processes=num(meta.get("processes")),
    )
    base = {"node": obs.name}
    counts: dict[tuple[str, str], int] = {}
    kept = 0
    for afi, key in (("ipv4", "route4"), ("ipv6", "route6")):
        for line in sections.get(key, []):
            route = parse_route(line)
            if route is None:
                continue
            counts[(afi, route["proto"])] = counts.get((afi, route["proto"]), 0) + 1
            if kept >= routes_max:
                continue
            kept += 1
            prefix = route["prefix"]
            if prefix == "default":
                prefix = "0.0.0.0/0" if afi == "ipv4" else "::/0"
            obs.points.append(
                Point(
                    "lab_route_entry",
                    {**base, "vrf": "default", "afi": afi, "prefix": prefix, "protocol": route["proto"]},
                    {
                        "present": True,
                        "selected": True,
                        "installed": True,
                        "metric": num(route["metric"]),
                        "nexthops": 1,
                        "via": route["via"] or route["dev"] or "",
                        "interfaces": route["dev"] or "",
                    },
                    ts_ns,
                )
            )
    for afi in ("ipv4", "ipv6"):
        total = 0
        for (a, proto), count in sorted(counts.items()):
            if a != afi:
                continue
            total += count
            obs.points.append(Point("lab_route", {**base, "vrf": "default", "afi": afi, "protocol": proto}, {"rib": count, "fib": count}, ts_ns))
        obs.points.append(Point("lab_route", {**base, "vrf": "default", "afi": afi, "protocol": "total"}, {"rib": total, "fib": total}, ts_ns))
    neigh: dict[tuple[str, str, str], int] = {}
    for line in sections.get("neigh", []):
        n = parse_neighbor(line)
        if n is None:
            continue
        key = (n["dev"], "ipv6" if ":" in n["dst"] else "ipv4", n["state"])
        neigh[key] = neigh.get(key, 0) + 1
    for (dev, family, state), count in sorted(neigh.items()):
        obs.points.append(Point("lab_neighbor", {**base, "interface": dev, "family": family, "state": state}, {"count": count}, ts_ns))
    return obs
