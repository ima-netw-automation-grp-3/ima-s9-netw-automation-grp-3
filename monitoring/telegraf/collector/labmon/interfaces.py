"""Interfaces (tous les netdevs de tous les nœuds) et liens de la topologie."""

from __future__ import annotations

import re
from typing import Any

from .common import NodeObs, is_lab_address
from .lineproto import Point
from .state import NodeState
from .topology import Topology, sonic_port_for

IFF_UP = 0x1
# compteur -> (champ calculé, multiplicateur)
RATE_FIELDS = {
    "rx_bytes": ("rx_bps", 8.0),
    "tx_bytes": ("tx_bps", 8.0),
    "rx_packets": ("rx_pps", 1.0),
    "tx_packets": ("tx_pps", 1.0),
    "rx_errors": ("rx_errors_ps", 1.0),
    "tx_errors": ("tx_errors_ps", 1.0),
    "rx_dropped": ("rx_dropped_ps", 1.0),
    "tx_dropped": ("tx_dropped_ps", 1.0),
}
_TYPES = (
    (re.compile(r"^eth0$"), "mgmt"),
    (re.compile(r"^Ethernet\d+"), "port"),
    (re.compile(r"^eth\d+$"), "wire"),
    (re.compile(r"^(Loopback\d+|lo)$"), "loopback"),
    (re.compile(r"^Vlan\d+"), "vlan"),
    (re.compile(r"^PortChannel\d+"), "lag"),
    (re.compile(r"^(Bridge|br|docker|virbr)"), "bridge"),
    (re.compile(r"^(Vrf|vrf)"), "vrf"),
    (re.compile(r"^(vxlan|Vxlan|tun|gre|ip6gre|sit|ipip)"), "tunnel"),
)


def if_type(name: str) -> str:
    for pattern, kind in _TYPES:
        if pattern.match(name):
            return kind
    return "other"


def oper_up(nd: dict[str, Any]) -> bool:
    state = (nd.get("operstate") or "").lower()
    if state == "up":
        return True
    # loopbacks, tap et dummy restent « unknown » : on regarde le flag UP et la porteuse
    flags = nd.get("flags") or 0
    return state == "unknown" and bool(flags & IFF_UP) and nd.get("carrier") != 0


def wire_of(obs: NodeObs, ifname: str) -> str | None:
    """Fil containerlab (ethN) d'une interface : elle-même, ou le fil d'un port SONiC."""
    if if_type(ifname) == "wire":
        return ifname
    return obs.wires.get(ifname)


def peer_name(topo: Topology, kinds: dict[str, str], wire_to_port: dict[tuple[str, str], str], node: str, iface: str, sonic_kinds: frozenset[str]) -> str:
    """Nom lisible de l'extrémité distante : port SONiC (EthernetX) plutôt que fil ethN."""
    port = wire_to_port.get((node, iface))
    if port is None and kinds.get(node) in sonic_kinds:
        port = sonic_port_for(iface)
    return port or iface


def interface_points(
    obs: NodeObs,
    st: NodeState,
    topo: Topology,
    kinds: dict[str, str],
    wire_to_port: dict[tuple[str, str], str],
    sonic_kinds: frozenset[str],
    mgmt: Any,
    ts_ns: int,
) -> tuple[list[Point], dict[str, dict[str, Any]]]:
    """Points lab_interface du nœud, et un résumé par interface (état, débits) pour les liens."""
    points: list[Point] = []
    summary: dict[str, dict[str, Any]] = {}
    seen: set[Any] = set()
    for ifname, nd in sorted(obs.netdev.items()):
        typ = if_type(ifname)
        wire = wire_of(obs, ifname)
        peer = topo.peer_of(obs.name, wire) if wire else None
        stats = nd.get("stats") or {}
        up = obs.port_oper.get(ifname) if ifname in obs.port_oper else oper_up(nd)
        speed = obs.port_speed.get(ifname) or (nd.get("speed") if (nd.get("speed") or 0) > 0 else None)
        addrs = [
            f"{a.get('address')}/{a.get('prefixlen')}"
            for a in obs.addresses.get(ifname, [])
            if a.get("address") and a.get("scope") in ("global", "universe", None)
        ]
        fields: dict[str, Any] = {k: v for k, v in stats.items()}
        fields.update(
            oper_up=up,
            admin_up=bool((nd.get("flags") or 0) & IFF_UP),
            mtu=nd.get("mtu"),
            speed_mbps=speed,
            mac=nd.get("address") or "",
            addresses=",".join(addrs),
        )
        if nd.get("carrier") is not None:
            fields["carrier"] = nd["carrier"]
        rates: dict[str, float] = {}
        for counter, (name, factor) in RATE_FIELDS.items():
            key = (ifname, counter)
            seen.add(key)
            value = st.if_rates.rate(key, obs.clock, stats.get(counter))
            if value is not None:
                rates[name] = value * factor
        if speed:
            for direction in ("rx", "tx"):
                if f"{direction}_bps" in rates:
                    rates[f"{direction}_util_pct"] = rates[f"{direction}_bps"] / (speed * 1e6) * 100.0
        fields.update(rates)
        tags = {
            "node": obs.name,
            "kind": obs.kind,
            "role": obs.role,
            "interface": ifname,
            "if_type": typ,
            "linked": "yes" if peer else "no",
        }
        if peer:
            tags["peer_node"] = peer[0]
            tags["peer_interface"] = peer_name(topo, kinds, wire_to_port, peer[0], peer[1], sonic_kinds)
        points.append(Point("lab_interface", tags, fields, ts_ns))
        summary[ifname] = {"up": up, "has_ip": any(is_lab_address(a.get("address"), a.get("scope"), mgmt) for a in obs.addresses.get(ifname, [])), **rates}
    st.if_rates.keep_only(seen)  # interfaces disparues : on oublie leurs compteurs
    return points, summary


def link_points(
    topo: Topology,
    observations: dict[str, NodeObs],
    summaries: dict[str, dict[str, dict[str, Any]]],
    wire_to_port: dict[tuple[str, str], str],
    running: set[str],
    ts_ns: int,
) -> list[Point]:
    """lab_link : état et débit de chaque lien de la topologie, vu depuis ses deux extrémités."""
    points = []
    for lk in topo.links:
        fields: dict[str, Any] = {}
        ups = []
        for side, node, iface in (("a", lk.a_node, lk.a_if), ("z", lk.z_node, lk.z_if)):
            port = wire_to_port.get((node, iface))
            if port:
                fields[f"{side}_port"] = port
            obs = observations.get(node)
            info = (summaries.get(node) or {}).get(iface) or {}
            if node not in running or obs is None:
                up = False if node not in running else None
            elif port and port in obs.port_oper:
                up = obs.port_oper[port]  # côté SONiC : état opérationnel du port (comme l'interface web)
            else:
                up = info.get("up")
            if up is not None:
                fields[f"{side}_up"] = up
            ups.append(up)
            for metric in ("bps", "pps"):
                value = info.get(f"tx_{metric}")
                if value is not None:
                    other = "z" if side == "a" else "a"
                    fields[f"{side}_to_{other}_{metric}"] = value
        if all(u is True for u in ups):
            fields["up"] = True
        elif any(u is False for u in ups):
            fields["up"] = False
        tags = {"link": lk.id, "a_node": lk.a_node, "a_interface": lk.a_if, "z_node": lk.z_node, "z_interface": lk.z_if}
        points.append(Point("lab_link", tags, fields, ts_ns))
    return points
