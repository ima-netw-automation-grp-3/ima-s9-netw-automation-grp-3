"""Flux sFlow des routeurs : réception, agrégation entre deux cycles, points lab_flow.

Un thread (démon) reçoit les datagrammes UDP de hsflowd et ne fait qu'agréger, sous verrou. Il n'écrit
rien sur stdout et ne journalise rien : le cycle du collecteur vide l'agrégat (FlowTable.drain), donne
leurs noms aux nœuds et aux interfaces avec ce qu'il vient de découvrir (FlowView) et produit :

  lab_flow        un point par (nœud, interface, protocole, application, src, dst…) vu pendant le
                  cycle : octets et paquets estimés (taille × taux d'échantillonnage), échantillons
  lab_flow_agent  un point par routeur : datagrammes, échantillons, erreurs, datagrammes perdus

Le réseau de management est partagé avec les jobs CI : seuls les datagrammes venant de l'IP de
management d'un routeur découvert sont décodés, et le nombre de clés par cycle est plafonné.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from . import sflow
from .common import NodeObs, is_lab_address
from .lineproto import Point
from .topology import Topology, sonic_port_for

OVERFLOW = "autres"  # src et dst des flux au-delà du plafond de clés


@dataclass
class AgentStats:
    datagrams: int = 0
    flow_samples: int = 0
    counter_samples: int = 0
    errors: int = 0
    lost: int = 0
    drops: int = 0  # échantillons perdus par l'agent (compteur cumulé, dernière valeur)
    agent: str = ""


@dataclass
class Batch:
    """Ce que le thread a reçu depuis le dernier cycle."""

    flows: dict[tuple[Any, ...], list[float]] = field(default_factory=dict)
    agents: dict[str, AgentStats] = field(default_factory=dict)  # IP source -> statistiques
    datagrams: int = 0
    rejected: int = 0
    errors: int = 0
    overflow: int = 0
    last_error: str = ""


def flow_key(source: str, sample: sflow.FlowSample) -> tuple[Any, ...]:
    """Clé d'agrégation d'un échantillon : (source, port, protocole, application, src, dst, encapsulation, vni)."""
    pkt = sflow.parse_packet(sample.header) if sample.header_protocol == 1 else sflow.Packet()
    encap, vni = "none", None
    if pkt.inner is not None and pkt.inner.ethertype is not None:
        encap, vni, pkt = "vxlan", pkt.vni, pkt.inner
    proto, app = sflow.classify(pkt)
    return (source, sample.port_index, proto, app, pkt.src or "", pkt.dst or "", encap, vni)


class FlowTable:
    """Agrégat thread-safe des échantillons reçus entre deux cycles."""

    def __init__(self, max_keys: int = 2000) -> None:
        self.max_keys = max(1, max_keys)
        self._lock = threading.Lock()
        self._batch = Batch()
        self._allowed: frozenset[str] = frozenset()
        self._sequences: dict[tuple[str, str, int], int] = {}  # (source, agent, sous-agent) -> séquence

    def set_allowed(self, sources: Iterable[str]) -> None:
        """IP sources acceptées : l'IP de management des routeurs découverts au cycle courant."""
        allowed = frozenset(s for s in sources if s)
        with self._lock:
            self._allowed = allowed
            for key in [k for k in self._sequences if k[0] not in allowed]:
                del self._sequences[key]

    def add(self, source: str, data: bytes) -> None:
        with self._lock:
            self._batch.datagrams += 1
            if source not in self._allowed:
                self._batch.rejected += 1
                return
        try:
            dg = sflow.decode(data)
            keys = [(flow_key(source, s), s) for s in dg.flows]
        except (sflow.SflowError, ValueError) as exc:
            with self._lock:
                self._batch.errors += 1
                self._batch.agents.setdefault(source, AgentStats()).errors += 1
                self._batch.last_error = f"{source} : {exc}"
            return
        with self._lock:
            stats = self._batch.agents.setdefault(source, AgentStats())
            stats.agent = dg.agent
            stats.datagrams += 1
            stats.flow_samples += len(dg.flows)
            stats.counter_samples += dg.counters
            seq_key = (source, dg.agent, dg.sub_agent)
            previous = self._sequences.get(seq_key)
            if previous is not None and dg.sequence > previous + 1:
                stats.lost += min(dg.sequence - previous - 1, 1_000_000)
            self._sequences[seq_key] = dg.sequence  # séquence revenue en arrière : agent redémarré
            flows = self._batch.flows
            for key, sample in keys:
                stats.drops = max(stats.drops, sample.drops)
                if key not in flows and len(flows) >= self.max_keys:
                    self._batch.overflow += 1
                    key = (*key[:4], OVERFLOW, OVERFLOW, *key[6:])
                rate = max(sample.sampling_rate, 1)
                acc = flows.setdefault(key, [0.0, 0.0, 0.0])
                acc[0] += sample.frame_length * rate
                acc[1] += rate
                acc[2] += 1

    def drain(self) -> Batch:
        with self._lock:
            batch, self._batch = self._batch, Batch()
        return batch


class FlowListener:
    """Thread de réception UDP ; se réattache seul si l'adresse n'est pas (encore) disponible."""

    RETRY_S = 10.0

    def __init__(self, table: FlowTable, host: str, port: int) -> None:
        self.table = table
        self.host = host
        self.port = port
        self.listening = False
        self.error = ""
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sflow", daemon=True)

    def start(self) -> FlowListener:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()

    def bound_port(self) -> int | None:
        return self._sock.getsockname()[1] if self._sock is not None and self.listening else None

    def _bind(self) -> socket.socket | None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            sock.bind((self.host, self.port))
            sock.settimeout(1.0)
        except OSError as exc:
            self.error = f"écoute UDP {self.host}:{self.port} impossible : {exc}"
            return None
        self.error = ""
        return sock

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sock = self._bind()
            if self._sock is None:
                self._stop.wait(self.RETRY_S)
                continue
            self.listening = True
            try:
                while not self._stop.is_set():
                    try:
                        data, addr = self._sock.recvfrom(65535)
                    except TimeoutError:
                        continue
                    self.table.add(addr[0], data)
            except OSError as exc:
                if not self._stop.is_set():
                    self.error = f"réception sFlow interrompue : {exc}"
            finally:
                self.listening = False
                self._sock.close()


@dataclass
class FlowNode:
    name: str
    kind: str
    peers: dict[str, tuple[str, bool]] = field(default_factory=dict)  # interface -> (voisin, voisin routeur ?)


@dataclass
class FlowView:
    """Ce que le cycle sait des nœuds, pour nommer les flux."""

    by_source: dict[str, str] = field(default_factory=dict)  # IP de management ou d'agent -> nœud
    nodes: dict[str, FlowNode] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)  # adresse du lab -> nœud (adresses uniques)


def build_view(
    topo: Topology,
    mgmt_ips: dict[str, str],
    observations: dict[str, NodeObs],
    kinds: dict[str, str],
    sonic_kinds: frozenset[str],
    mgmt: Any,
) -> FlowView:
    """Vue du cycle : routeurs par IP de management (et par adresse du lab, pour l'IP d'agent),
    voisins de chaque port SONiC d'après la topologie, propriétaire de chaque adresse du lab."""
    view = FlowView()
    owners: dict[str, set[str]] = {}
    for n, obs in observations.items():
        for addrs in obs.addresses.values():
            for a in addrs:
                if is_lab_address(a.get("address"), a.get("scope"), mgmt):
                    owners.setdefault(a["address"], set()).add(n)
    # une adresse portée par plusieurs nœuds (passerelle anycast d'EVPN…) ne désigne personne
    view.owners = {addr: next(iter(nodes)) for addr, nodes in owners.items() if len(nodes) == 1}
    routers = sorted(n for n in kinds if kinds[n] in sonic_kinds)
    for n in routers:
        if mgmt_ips.get(n):
            view.by_source[mgmt_ips[n]] = n
    for addr, n in view.owners.items():
        if kinds.get(n) in sonic_kinds:
            view.by_source.setdefault(addr, n)
    for n in routers:
        obs = observations.get(n)
        port_of = {wire: port for port, wire in obs.wires.items()} if obs is not None else {}
        node = FlowNode(n, kinds[n])
        for lk in topo.links:
            for (a, a_if), (z, _) in (((lk.a_node, lk.a_if), (lk.z_node, lk.z_if)), ((lk.z_node, lk.z_if), (lk.a_node, lk.a_if))):
                if a == n:
                    port = port_of.get(a_if) or sonic_port_for(a_if) or a_if
                    node.peers[port] = (z, kinds.get(z) in sonic_kinds)
        view.nodes[n] = node
    return view


def flow_points(batch: Batch, view: FlowView, ts_ns: int) -> list[Point]:
    """Agrégat d'un cycle -> points lab_flow et lab_flow_agent (clés finales réagrégées)."""
    merged: dict[tuple[tuple[str, str], ...], list[float]] = {}
    for (source, index, proto, app, src, dst, encap, vni), acc in batch.flows.items():
        agent = batch.agents.get(source)
        node = view.by_source.get(source) or (view.by_source.get(agent.agent) if agent else None) or source
        interface = sflow.sonic_ifname(index) or f"index-{index}"
        peer, peer_is_router = view.nodes[node].peers.get(interface, ("", False)) if node in view.nodes else ("", False)
        tags = {
            "node": node,
            "interface": interface,
            "peer_node": peer,
            # trafic entrant dans le réseau : reçu d'un nœud qui n'est pas un routeur (compté une fois)
            "edge": "yes" if peer and not peer_is_router else "no",
            "proto": proto,
            "app": app,
            "src": src,
            "dst": dst,
            "src_node": view.owners.get(src, ""),
            "dst_node": view.owners.get(dst, ""),
            "encap": encap,
            "vni": "" if vni is None else str(vni),
        }
        key = tuple(sorted(tags.items()))
        total = merged.setdefault(key, [0.0, 0.0, 0.0])
        for i in range(3):
            total[i] += acc[i]
    points = [
        Point("lab_flow", dict(key), {"bytes": acc[0], "packets": acc[1], "samples": acc[2]}, ts_ns)
        for key, acc in sorted(merged.items())
    ]
    per_node: dict[str, AgentStats] = {}
    agents_of: dict[str, set[str]] = {}
    for source, stats in batch.agents.items():
        node = view.by_source.get(source) or view.by_source.get(stats.agent) or source
        total = per_node.setdefault(node, AgentStats())
        total.datagrams += stats.datagrams
        total.flow_samples += stats.flow_samples
        total.counter_samples += stats.counter_samples
        total.errors += stats.errors
        total.lost += stats.lost
        total.drops = max(total.drops, stats.drops)
        if stats.agent:
            agents_of.setdefault(node, set()).add(stats.agent)
    for node, stats in sorted(per_node.items()):
        points.append(
            Point(
                "lab_flow_agent",
                {"node": node},
                {
                    "datagrams": stats.datagrams,
                    "flow_samples": stats.flow_samples,
                    "counter_samples": stats.counter_samples,
                    "errors": stats.errors,
                    "lost": stats.lost,
                    "sample_drops": stats.drops,
                    "agent": ",".join(sorted(agents_of.get(node, ()))),
                },
                ts_ns,
            )
        )
    return points


def status_point(batch: Batch, listener: FlowListener | None, duration_s: float, ts_ns: int) -> Point:
    """État de la réception sFlow pour ce cycle (lab_collector, task=flows)."""
    return Point(
        "lab_collector",
        {"task": "flows"},
        {
            "listening": bool(listener and listener.listening),
            "datagrams": batch.datagrams,
            "rejected": batch.rejected,
            "errors": batch.errors,
            "keys": len(batch.flows),
            "overflow": batch.overflow,
            "duration_ms": duration_s * 1000.0,
            "error": "; ".join(filter(None, [listener.error if listener else "", batch.last_error]))[:1000],
        },
        ts_ns,
    )
