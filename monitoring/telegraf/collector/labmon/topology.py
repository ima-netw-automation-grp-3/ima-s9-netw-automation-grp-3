"""Lecture de la topologie containerlab (adapté de web/backend/app/topology.py).

Le fichier YAML donne les nœuds attendus (même s'ils ne sont pas déployés), leurs liens et le réseau de
management. Il est relu dès que sa date de modification change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ETH_RE = re.compile(r"^eth(\d+)$")


@dataclass(frozen=True)
class NodeDef:
    name: str
    kind: str
    image: str | None = None
    mgmt_ipv4: str | None = None


@dataclass(frozen=True)
class Link:
    a_node: str
    a_if: str
    z_node: str
    z_if: str

    @property
    def id(self) -> str:
        return f"{self.a_node}:{self.a_if}--{self.z_node}:{self.z_if}"


@dataclass
class Topology:
    name: str = "lab"
    nodes: dict[str, NodeDef] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    mgmt_network: str | None = None
    mgmt_subnet: str | None = None

    def peer_of(self, node: str, interface: str) -> tuple[str, str] | None:
        for lk in self.links:
            if (lk.a_node, lk.a_if) == (node, interface):
                return lk.z_node, lk.z_if
            if (lk.z_node, lk.z_if) == (node, interface):
                return lk.a_node, lk.a_if
        return None


def sonic_port_for(interface: str) -> str | None:
    """ethN (fil containerlab) -> EthernetX (port SONiC), règle du HWSKU Force10-S6000 en vs (repli)."""
    m = _ETH_RE.match(interface)
    if not m or int(m.group(1)) < 1:
        return None
    return f"Ethernet{4 * (int(m.group(1)) - 1)}"


def _endpoint(raw: Any) -> tuple[str, str] | None:
    if isinstance(raw, str) and ":" in raw:
        node, _, iface = raw.partition(":")
        return (node, iface) if node and iface else None
    if isinstance(raw, dict) and "node" in raw:
        return str(raw["node"]), str(raw.get("interface", ""))
    return None


def parse_topology(data: dict[str, Any]) -> Topology:
    topo = data.get("topology") or {}
    defaults = topo.get("defaults") or {}
    kinds = topo.get("kinds") or {}
    mgmt = data.get("mgmt") or {}
    result = Topology(
        name=str(data.get("name", "lab")),
        mgmt_network=mgmt.get("network"),
        mgmt_subnet=mgmt.get("ipv4-subnet"),
    )
    for name, node in (topo.get("nodes") or {}).items():
        node = node or {}
        kind = node.get("kind") or defaults.get("kind") or "linux"
        image = node.get("image") or (kinds.get(kind) or {}).get("image") or defaults.get("image")
        result.nodes[str(name)] = NodeDef(str(name), str(kind), image, node.get("mgmt-ipv4"))
    for link in topo.get("links") or []:
        endpoints = (link or {}).get("endpoints") or []
        if len(endpoints) != 2:
            continue
        a, z = _endpoint(endpoints[0]), _endpoint(endpoints[1])
        if a and z:
            result.links.append(Link(a[0], a[1], z[0], z[1]))
    return result


class TopologyCache:
    """Topologie relue uniquement si le fichier a changé ; topologie vide s'il est absent ou invalide."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._topology = Topology()
        self.error: str | None = None

    def get(self) -> Topology:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self.error = f"topologie introuvable : {self.path}"
            self._mtime, self._topology = None, Topology()
            return self._topology
        if mtime != self._mtime:
            try:
                self._topology = parse_topology(yaml.safe_load(self.path.read_text()) or {})
                self.error = None
            except (OSError, yaml.YAMLError) as exc:
                self.error = f"topologie invalide : {exc}"
                self._topology = Topology()
            self._mtime = mtime
        return self._topology
