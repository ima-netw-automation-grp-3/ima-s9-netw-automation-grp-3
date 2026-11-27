"""Analyse des sorties texte des nœuds Linux (busybox ip, /proc/net/dev) et de ping."""

from __future__ import annotations

import re
from typing import Any

_LINK_RE = re.compile(r"^\d+:\s+([^:@\s]+)(?:@\S+)?:\s+<([^>]*)>(.*)$")
_ADDR_RE = re.compile(r"^\d+:\s+(\S+)\s+(inet6?)\s+(\S+)")
_MAC_RE = re.compile(r"link/\S+\s+([0-9a-f:]{17})")


def parse_ip_link(text: str) -> dict[str, dict[str, Any]]:
    """`ip -o link` (busybox ou iproute2) -> {ifname: {flags, mtu, state, mac}}."""
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        m = _LINK_RE.match(line.strip())
        if not m:
            continue
        name, flags, rest = m.group(1), m.group(2).split(","), m.group(3)
        mtu = re.search(r"\bmtu (\d+)", rest)
        state = re.search(r"\bstate (\S+)", rest)
        mac = _MAC_RE.search(rest)
        out[name] = {
            "flags": flags,
            "mtu": int(mtu.group(1)) if mtu else None,
            "state": state.group(1) if state else None,
            "mac": mac.group(1) if mac else None,
            "up": "UP" in flags,
            "lower_up": "LOWER_UP" in flags,
        }
    return out


def parse_ip_addr(text: str) -> dict[str, list[dict[str, str]]]:
    """`ip -o addr` -> {ifname: [{family, address}]}."""
    out: dict[str, list[dict[str, str]]] = {}
    for line in text.splitlines():
        m = _ADDR_RE.match(line.strip())
        if m:
            out.setdefault(m.group(1), []).append({"family": m.group(2), "address": m.group(3)})
    return out


def parse_ip_route(text: str) -> list[dict[str, str | None]]:
    """`ip route` -> [{prefix, via, dev, src, proto}]."""
    routes: list[dict[str, str | None]] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        route: dict[str, str | None] = {"prefix": parts[0], "via": None, "dev": None, "src": None, "proto": None}
        for i, tok in enumerate(parts[1:-1], start=1):
            if tok in ("via", "dev", "src", "proto"):
                route[tok] = parts[i + 1]
        routes.append(route)
    return routes


def parse_proc_net_dev(text: str) -> dict[str, dict[str, int]]:
    """/proc/net/dev -> {ifname: {rx_bytes, rx_packets, rx_errors, rx_dropped, tx_...}}."""
    out: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        if ":" not in line or "|" in line:
            continue
        name, _, data = line.partition(":")
        values = data.split()
        if len(values) < 16:
            continue
        v = [int(x) for x in values[:16]]
        out[name.strip()] = {
            "rx_bytes": v[0],
            "rx_packets": v[1],
            "rx_errors": v[2],
            "rx_dropped": v[3],
            "tx_bytes": v[8],
            "tx_packets": v[9],
            "tx_errors": v[10],
            "tx_dropped": v[11],
        }
    return out


_PING_TX_RE = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_PING_LOSS_RE = re.compile(r"([\d.]+)% packet loss")
_PING_RTT_RE = re.compile(r"(?:round-trip|rtt) min/avg/max(?:/mdev)? = ([\d.]+)/([\d.]+)/([\d.]+)")


def parse_ping(text: str) -> dict[str, Any]:
    """Résumé d'un ping (busybox ou iputils)."""
    tx = _PING_TX_RE.search(text)
    loss = _PING_LOSS_RE.search(text)
    rtt = _PING_RTT_RE.search(text)
    transmitted = int(tx.group(1)) if tx else 0
    received = int(tx.group(2)) if tx else 0
    return {
        "transmitted": transmitted,
        "received": received,
        "loss": float(loss.group(1)) if loss else (100.0 if transmitted else None),
        "rtt_min": float(rtt.group(1)) if rtt else None,
        "rtt_avg": float(rtt.group(2)) if rtt else None,
        "rtt_max": float(rtt.group(3)) if rtt else None,
        "ok": received > 0,
    }


def split_sections(text: str, marker: str = "=====") -> list[str]:
    """Découpe une sortie composée de plusieurs commandes séparées par un marqueur."""
    return [s.strip("\n") for s in text.split(marker)]
