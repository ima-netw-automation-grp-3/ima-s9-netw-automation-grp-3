"""Structures et utilitaires partagés par les convertisseurs SONiC et Linux."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

from .lineproto import Point

BGP_STATE_CODES = {"idle": 1, "connect": 2, "active": 3, "opensent": 4, "openconfirm": 5, "established": 6}
SERVICE_STATE_CODES = {
    "STOPPED": 0,
    "STARTING": 1,
    "RUNNING": 2,
    "BACKOFF": 3,
    "STOPPING": 4,
    "EXITED": 5,
    "FATAL": 6,
    "UNKNOWN": 7,
}
_FRR_UPTIME_RE = re.compile(r"^(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


@dataclass
class NodeObs:
    """Ce qu'un cycle a relevé sur un nœud, sous une forme commune aux deux types de nœuds."""

    name: str
    kind: str
    role: str
    clock: float | None = None  # /proc/uptime : horloge monotone pour les débits
    netdev: dict[str, dict[str, Any]] = field(default_factory=dict)
    addresses: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    wires: dict[str, str] = field(default_factory=dict)  # port SONiC -> fil ethN
    port_speed: dict[str, float] = field(default_factory=dict)  # port SONiC -> Mbit/s (CONFIG_DB)
    port_oper: dict[str, bool] = field(default_factory=dict)  # port SONiC -> oper up (APPL_DB)
    node_fields: dict[str, Any] = field(default_factory=dict)
    points: list[Point] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    has_syslog: bool = False
    ready: bool | None = None


def bgp_state_code(state: str | None) -> int:
    if not state:
        return 0
    return BGP_STATE_CODES.get(state.split()[0].strip("()").lower(), 0)


def parse_frr_uptime(text: str | None) -> float | None:
    """Durée FRR ('00:32:21', '1d02h03m', '2w3d04h') -> secondes."""
    if not text:
        return None
    text = text.strip()
    parts = text.split(":")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        h, m, s = (int(p) for p in parts)
        return float(h * 3600 + m * 60 + s)
    m = _FRR_UPTIME_RE.match(text)
    if not m or not any(m.groups()):
        return None
    w, d, h, mi, s = (int(x or 0) for x in m.groups())
    return float(w * 604800 + d * 86400 + h * 3600 + mi * 60 + s)


def mgmt_network(subnet: str | None) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    if not subnet:
        return None
    try:
        return ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return None


def is_lab_address(address: str | None, scope: str | None, mgmt: Any) -> bool:
    """Adresse « utile » du lab : globale, ni loopback, ni lien-local, ni dans le réseau de management."""
    if not address or (scope and scope not in ("global", "universe")):
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    if mgmt is not None and ip.version == mgmt.version and ip in mgmt:
        return False
    return True
