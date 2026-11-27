"""Matrice de connectivité : choix des adresses à pinguer (fonctions pures, sans Docker).

Une source ne pingue une adresse que si sa table de routage l'envoie dans le lab. Une adresse que la
source ne joint que par sa route par défaut du management (eth0, réseau docker) n'est pas un échec du
lab mais une cible hors de son plan de routage : VRF ou plateforme différente, loopback d'underlay
invisible depuis une VRF… La case est alors « non routée » au lieu d'un faux 100 % de perte.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

# Interface de management des nœuds containerlab (toujours eth0).
MGMT_IFACE = "eth0"


@dataclass(frozen=True)
class Route:
    network: ipaddress.IPv4Network
    dev: str | None


def ipv4_routes(routes: list[dict[str, Any]]) -> list[Route]:
    """Routes IPv4 issues de `parsers.parse_ip_route` (lignes non reconnues ignorées)."""
    out = []
    for r in routes:
        prefix = r.get("prefix") or ""
        try:
            net = ipaddress.IPv4Network("0.0.0.0/0" if prefix == "default" else prefix, strict=False)
        except ValueError:
            continue  # IPv6, blackhole/unreachable, multicast…
        out.append(Route(net, r.get("dev")))
    return out


def lookup(routes: list[Route], ip: str) -> Route | None:
    """Route la plus spécifique pour `ip` (None : aucune)."""
    addr = ipaddress.IPv4Address(ip)
    best: Route | None = None
    for r in routes:
        if addr in r.network and (best is None or r.network.prefixlen > best.network.prefixlen):
            best = r
    return best


def mgmt_devs(routes: list[Route], mgmt_subnet: str | None) -> set[str]:
    """Interfaces de management de la source : eth0 et celles qui portent le sous-réseau de management."""
    devs = {MGMT_IFACE}
    if mgmt_subnet:
        mgmt = ipaddress.ip_network(mgmt_subnet, strict=False)
        for r in routes:
            if r.dev and r.network.prefixlen and r.network.overlaps(mgmt):
                devs.add(r.dev)
    return devs


def plan(routes: list[Route] | None, mgmt: set[str], addresses: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    """Adresses de la cible à essayer (dans l'ordre), et pourquoi les autres sont écartées.

    Sans table de routage connue (`routes` None), toutes les adresses sont essayées.
    """
    if routes is None:
        return [{**a, "via": ""} for a in addresses], []
    tried, skipped = [], []
    for a in addresses:
        r = lookup(routes, a["ip"])
        if r is None or not r.dev:
            skipped.append(f"{a['ip']} ({a['interface']}) : aucune route")
        elif r.dev in mgmt:
            skipped.append(f"{a['ip']} ({a['interface']}) : hors du lab, sortie par {r.dev} (management)")
        else:
            tried.append({**a, "via": r.dev})
    return tried, skipped
