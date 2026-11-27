"""Métrologie active : sondes ping maillées entre les nœuds du lab.

Les cibles sont recalculées à chaque cycle à partir des adresses relevées sur les nœuds : une adresse
ajoutée devient une cible au cycle suivant, une adresse retirée disparaît.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .common import NodeObs, is_lab_address
from .interfaces import if_type
from .lineproto import Point

_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms")
_SUMMARY_RE = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")


@dataclass(frozen=True)
class Target:
    node: str
    interface: str
    ip: str
    primary: bool = False

    @property
    def label(self) -> str:
        return f"{self.node}:{self.interface}"


def lab_targets(observations: dict[str, NodeObs], mgmt: Any) -> list[Target]:
    """Toutes les adresses du lab (hors management) ; la « principale » de chaque nœud est marquée.

    Principale : Loopback0 (ou la première loopback) d'un routeur ; sinon la première adresse d'une
    interface reliée par un fil.
    """
    out: list[Target] = []
    for name in sorted(observations):
        obs = observations[name]
        found: list[Target] = []
        for iface in sorted(obs.addresses, key=lambda i: (if_type(i) != "loopback", i != "Loopback0", i)):
            if if_type(iface) in ("mgmt", "bridge"):
                continue
            for a in obs.addresses[iface]:
                if is_lab_address(a.get("address"), a.get("scope"), mgmt):
                    found.append(Target(name, iface, a["address"]))
        if found:
            if obs.role == "router":
                first = next((t for t in found if if_type(t.interface) == "loopback"), found[0])
            else:
                first = next((t for t in found if if_type(t.interface) in ("wire", "port", "vlan", "lag")), found[0])
            found = [Target(t.node, t.interface, t.ip, t is first) for t in found]
        out.extend(found)
    return out


def targets_for(source: str, targets: list[Target], scope: str, limit: int) -> list[Target]:
    if scope == "off":
        return []
    own = {t.ip for t in targets if t.node == source}
    chosen = [t for t in targets if t.node != source and t.ip not in own and (scope == "all" or t.primary)]
    seen: set[str] = set()
    unique = []
    for t in chosen:
        if t.ip not in seen:
            seen.add(t.ip)
            unique.append(t)
    return unique[:limit]


def parse_probe_output(text: str) -> dict[str, dict[str, Any]]:
    """Sortie de remote/probe.sh (lignes préfixées par la cible) -> {cible: rtts, envoyés, reçus, erreur}."""
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        target, _, rest = line.partition(" ")
        if not target:
            continue
        res = out.setdefault(target, {"rtts": [], "sent": None, "received": None, "error": None})
        m = _TIME_RE.search(rest)
        if m and ("bytes from" in rest or "seq=" in rest):
            res["rtts"].append(float(m.group(1)))
            continue
        m = _SUMMARY_RE.search(rest)
        if m:
            res["sent"], res["received"] = int(m.group(1)), int(m.group(2))
            continue
        low = rest.lower()
        if res["error"] is None and ("unreachable" in low or "bad address" in low or "unknown host" in low or "ping:" in low):
            res["error"] = rest.strip()
    return out


def probe_stats(res: dict[str, Any], count: int) -> dict[str, Any]:
    rtts = res.get("rtts") or []
    sent = res.get("sent") if res.get("sent") is not None else count
    received = res.get("received") if res.get("received") is not None else len(rtts)
    fields: dict[str, Any] = {
        "sent": sent,
        "received": received,
        "loss_pct": (1.0 - received / sent) * 100.0 if sent else 100.0,
        "reachable": received > 0,
    }
    if rtts:
        avg = sum(rtts) / len(rtts)
        fields.update(
            rtt_min_ms=min(rtts),
            rtt_avg_ms=avg,
            rtt_max_ms=max(rtts),
            rtt_stddev_ms=math.sqrt(sum((r - avg) ** 2 for r in rtts) / len(rtts)),
        )
        if len(rtts) > 1:
            # gigue : moyenne des écarts entre RTT successifs (esprit de la RFC 3550)
            fields["jitter_ms"] = sum(abs(b - a) for a, b in zip(rtts, rtts[1:])) / (len(rtts) - 1)
    if res.get("error"):
        fields["error"] = res["error"]
    return fields


def probe_points(source: str, targets: list[Target], text: str, count: int, ts_ns: int) -> list[Point]:
    parsed = parse_probe_output(text)
    points = []
    for t in targets:
        fields = probe_stats(parsed.get(t.ip) or {}, count)
        tags = {"src": source, "dst": t.node, "dst_ip": t.ip, "dst_interface": t.interface, "target": t.label}
        points.append(Point("lab_probe", tags, fields, ts_ns))
    return points
