"""Lecture de la topologie containerlab (fichier YAML du lab)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import LabError

SONIC_KINDS = {"sonic-vs"}
_ETH_RE = re.compile(r"^eth(\d+)$")


@dataclass(frozen=True)
class Endpoint:
    node: str
    interface: str

    def to_dict(self) -> dict[str, str]:
        return {"node": self.node, "interface": self.interface}


@dataclass(frozen=True)
class Link:
    a: Endpoint
    z: Endpoint


@dataclass
class NodeDef:
    name: str
    kind: str
    image: str | None = None
    mgmt_ipv4: str | None = None

    @property
    def is_router(self) -> bool:
        return self.kind in SONIC_KINDS

    @property
    def role(self) -> str:
        return "router" if self.is_router else "client"


@dataclass
class Topology:
    name: str
    nodes: dict[str, NodeDef] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    mgmt_network: str | None = None
    mgmt_subnet: str | None = None

    def links_of(self, node: str) -> list[Link]:
        return [lk for lk in self.links if node in (lk.a.node, lk.z.node)]

    def peer_of(self, node: str, interface: str) -> Endpoint | None:
        for lk in self.links:
            if (lk.a.node, lk.a.interface) == (node, interface):
                return lk.z
            if (lk.z.node, lk.z.interface) == (node, interface):
                return lk.a
        return None


def sonic_port_for(interface: str) -> str | None:
    """ethN (interface containerlab) -> EthernetX (port SONiC), règle du HWSKU Force10-S6000 en vs.

    La correspondance exacte est relue dans le routeur (lanemap.ini) quand il tourne ; cette règle
    sert de repli quand le routeur n'est pas joignable.
    """
    m = _ETH_RE.match(interface)
    if not m or int(m.group(1)) < 1:
        return None
    return f"Ethernet{4 * (int(m.group(1)) - 1)}"


def _parse_endpoint(raw: Any) -> Endpoint:
    if isinstance(raw, str):
        node, _, iface = raw.partition(":")
        if not node or not iface:
            raise LabError(f"extrémité de lien invalide : {raw!r}")
        return Endpoint(node, iface)
    if isinstance(raw, dict) and "node" in raw:
        return Endpoint(str(raw["node"]), str(raw.get("interface", "")))
    raise LabError(f"extrémité de lien invalide : {raw!r}")


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
        result.nodes[str(name)] = NodeDef(
            name=str(name), kind=str(kind), image=image, mgmt_ipv4=node.get("mgmt-ipv4")
        )
    for link in topo.get("links") or []:
        endpoints = (link or {}).get("endpoints") or []
        if len(endpoints) != 2:
            continue  # liens spéciaux (host, macvlan...) ignorés
        result.links.append(Link(_parse_endpoint(endpoints[0]), _parse_endpoint(endpoints[1])))
    return result


def load_topology(path: Path) -> Topology:
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise LabError(f"fichier de topologie introuvable : {path}") from exc
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise LabError(f"fichier de topologie invalide : {exc}") from exc
    if data is not None and not isinstance(data, dict):
        raise LabError("fichier de topologie invalide : un dictionnaire YAML est attendu")
    try:
        return parse_topology(data or {})
    except (AttributeError, TypeError) as exc:  # structure inattendue (liste au lieu d'un dictionnaire…)
        raise LabError(f"fichier de topologie invalide : {exc}") from exc


class TopologyCache:
    """Recharge la topologie uniquement si le fichier a changé."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mtime: float | None = None
        self._topology: Topology | None = None

    def get(self) -> Topology:
        mtime = self._path.stat().st_mtime if self._path.exists() else None
        if self._topology is None or mtime != self._mtime:
            self._topology = load_topology(self._path)
            self._mtime = mtime
        return self._topology
