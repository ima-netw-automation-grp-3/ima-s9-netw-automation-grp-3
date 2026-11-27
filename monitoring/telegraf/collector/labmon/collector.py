"""Un cycle de collecte : découverte, état de chaque nœud, interfaces et liens, sondes, auto-surveillance."""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import docker, linux, sonic
from .common import NodeObs, mgmt_network
from .config import Config
from .flows import FlowListener, FlowTable, build_view, flow_points, status_point
from .interfaces import if_type, interface_points, link_points
from .lineproto import Point, encode_all, ns
from .probes import lab_targets, probe_points, targets_for
from .remote import source
from .state import NodeState, State
from .topology import Topology, TopologyCache


class Collector:
    def __init__(self, cfg: Config, flows: FlowTable | None = None, listener: FlowListener | None = None) -> None:
        self.cfg = cfg
        self.topology = TopologyCache(cfg.topology_path)
        self.state = State()
        self._logged: dict[str, str] = {}
        # flux sFlow (mode serve seulement) : agrégat rempli par le thread de réception. Sources acceptées
        # dès le démarrage d'après la topologie (IP de management des routeurs), puis à chaque cycle
        # d'après la découverte : sinon les datagrammes reçus avant le premier cycle seraient rejetés.
        self.flows = flows
        self.listener = listener
        if flows is not None:
            topo = self.topology.get()
            flows.set_allowed(n.mgmt_ipv4 for n in topo.nodes.values() if n.kind in cfg.sonic_kinds and n.mgmt_ipv4)

    # -- utilitaires ------------------------------------------------------------------------------
    def log(self, key: str, message: str | None) -> None:
        """Erreur sur stderr (journal Telegraf), une seule fois par changement, pour éviter le bruit."""
        if message == self._logged.get(key):
            return
        if message:
            print(f"[lab-collector] {key} : {message}", file=sys.stderr, flush=True)
        elif key in self._logged:
            print(f"[lab-collector] {key} : rétabli", file=sys.stderr, flush=True)
        self._logged[key] = message or ""

    def is_sonic(self, kind: str) -> bool:
        return kind in self.cfg.sonic_kinds

    # -- collecte d'un nœud -----------------------------------------------------------------------
    def collect_node(self, name: str, kind: str, container: docker.Container, st: NodeState, ts_ns: int, topo: Topology) -> tuple[NodeObs, float, str | None]:
        started = time.monotonic()
        obs = NodeObs(name=name, kind=kind, role="router" if self.is_sonic(kind) else "client")
        error = None
        try:
            if self.is_sonic(kind):
                opts = {"routes_max": self.cfg.routes_max, "event_regex": self.cfg.event_regex, "syslog": st.syslog}
                res = docker.exec_in(
                    container.name,
                    ["python3", "-c", source("sonic_collect.py"), json.dumps(opts)],
                    self.cfg.exec_timeout,
                )
                if not res.stdout.strip():
                    raise RuntimeError(res.output[:300] or f"sortie vide (code {res.exit_code})")
                sonic.build(obs, st, json.loads(res.stdout), ts_ns)
            else:
                res = docker.exec_in(
                    container.name,
                    ["sh", "-c", source("linux_collect.sh")],
                    self.cfg.exec_timeout,
                    in_container_timeout=False,
                )
                if not res.stdout.strip():
                    raise RuntimeError(res.output[:300] or f"sortie vide (code {res.exit_code})")
                linux.build(obs, st, res.stdout, ts_ns, topo.mgmt_subnet, self.cfg.routes_max)
        except Exception as exc:  # noqa: BLE001 - un nœud en échec ne bloque pas les autres
            error = f"{type(exc).__name__}: {exc}"
        return obs, time.monotonic() - started, error

    def probe_node(self, obs: NodeObs, container: docker.Container, targets: list[Any], ts_ns: int) -> tuple[list[Point], float, str | None]:
        started = time.monotonic()
        cfg = self.cfg
        try:
            res = docker.exec_in(
                container.name,
                ["sh", "-c", source("probe.sh"), "probe", str(cfg.probe_count), str(cfg.probe_interval), str(cfg.probe_deadline), *(t.ip for t in targets)],
                cfg.probe_deadline + 5,
                in_container_timeout=self.is_sonic(obs.kind),
            )
            points = probe_points(obs.name, targets, res.stdout, cfg.probe_count, ts_ns)
            error = None if res.stdout.strip() else (res.output[:300] or "aucune sortie")
        except Exception as exc:  # noqa: BLE001
            points, error = [], f"{type(exc).__name__}: {exc}"
        return points, time.monotonic() - started, error

    # -- événements calculés ----------------------------------------------------------------------
    @staticmethod
    def node_events(name: str, st: NodeState, up: bool, ready: bool | None, ts_ns: int) -> list[Point]:
        out = []

        def event(kind: str, message: str, severity: str) -> None:
            out.append(Point("lab_event", {"node": name, "program": "lab-collector", "severity": severity, "type": kind}, {"message": message}, ts_ns))

        if st.recreated:
            st.recreated = False
            event("node", f"{name} : conteneur recréé (redéploiement du lab)", "NOTICE")
        elif st.up is not None and st.up != up:
            event("node", f"{name} : conteneur {'démarré' if up else 'arrêté ou absent'}", "NOTICE" if up else "WARNING")
        if up and ready is not None and st.ready is not None and st.ready != ready:
            event("node", f"{name} : {'prêt' if ready else 'n’est plus prêt'}", "NOTICE" if ready else "WARNING")
        st.up = up
        if up and ready is not None:
            st.ready = ready
        return out

    @staticmethod
    def oper_events(obs: NodeObs, st: NodeState, summary: dict[str, dict[str, Any]], linked: set[str], ts_ns: int) -> list[Point]:
        """Changements d'état des interfaces câblées, pour les nœuds sans syslog (clients)."""
        out = []
        for iface in sorted(linked):
            up = (summary.get(iface) or {}).get("up")
            if up is None:
                continue
            prev = st.oper.get(iface)
            if prev is not None and prev != up:
                out.append(
                    Point(
                        "lab_event",
                        {"node": obs.name, "program": "lab-collector", "severity": "NOTICE" if up else "WARNING", "type": "port"},
                        {"message": f"{obs.name} : interface {iface} {'up' if up else 'down'}"},
                        ts_ns,
                    )
                )
            st.oper[iface] = up
        return out

    # -- cycle ------------------------------------------------------------------------------------
    def cycle(self) -> list[str]:
        started = time.monotonic()
        now = time.time()
        ts = ns(now)
        cfg = self.cfg
        topo = self.topology.get()
        self.log("topologie", self.topology.error)
        points: list[Point] = []
        discovery_error = None
        try:
            containers = docker.list_containers(cfg.lab_name, topo.mgmt_network)
        except docker.DockerError as exc:
            containers, discovery_error = [], str(exc)
        self.log("découverte", discovery_error)
        by_node = {c.node: c for c in containers}
        names = sorted(set(topo.nodes) | set(by_node))
        if discovery_error is None:
            for gone in sorted(set(self.state.nodes) - set(names)):
                points.append(
                    Point(
                        "lab_event",
                        {"node": gone, "program": "lab-collector", "severity": "NOTICE", "type": "node"},
                        {"message": f"{gone} : retiré du lab (ni dans la topologie, ni dans Docker)"},
                        ts,
                    )
                )
                del self.state.nodes[gone]
        kinds = {n: (by_node[n].kind if n in by_node and by_node[n].kind else topo.nodes[n].kind) for n in names}
        running = {n for n in names if n in by_node and by_node[n].running}
        mgmt_ips = {n: by_node[n].mgmt_ipv4 or "" for n in running}
        if self.flows is not None and discovery_error is None:
            # seuls les datagrammes venant de l'IP de management d'un routeur démarré sont décodés
            self.flows.set_allowed(ip for n, ip in mgmt_ips.items() if self.is_sonic(kinds[n]))

        # 1. état de chaque nœud démarré, en parallèle
        observations: dict[str, NodeObs] = {}
        errors: dict[str, str | None] = {}
        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            jobs = {
                n: pool.submit(self.collect_node, n, kinds[n], by_node[n], self.state.node(n, by_node[n].id), ts, topo)
                for n in sorted(running)
            }
            for n, job in jobs.items():
                obs, duration, error = job.result()
                observations[n] = obs
                errors[n] = error
                self.log(f"collecte {n}", error)
                points.append(
                    Point(
                        "lab_collector",
                        {"node": n, "task": "state"},
                        {
                            "duration_ms": duration * 1000.0,
                            "ok": error is None,
                            "errors": len(obs.errors) + (1 if error else 0),
                            "error": "; ".join(filter(None, [error, *(f"{k}: {v}" for k, v in sorted(obs.errors.items()))]))[:1000],
                        },
                        ts,
                    )
                )

        # 2. interfaces, liens, inventaire
        wire_to_port = {(n, wire): port for n, obs in observations.items() for port, wire in obs.wires.items()}
        mgmt = mgmt_network(topo.mgmt_subnet)
        summaries: dict[str, dict[str, dict[str, Any]]] = {}
        for n, obs in observations.items():
            if errors.get(n):
                continue
            st = self.state.node(n, by_node[n].id)
            ipoints, summary = interface_points(obs, st, topo, kinds, wire_to_port, cfg.sonic_kinds, mgmt, ts)
            summaries[n] = summary
            points.extend(obs.points)
            points.extend(ipoints)
            if not obs.has_syslog:
                linked = {i for i in obs.netdev if if_type(i) == "wire" and topo.peer_of(n, i)}
                points.extend(self.oper_events(obs, st, summary, linked, ts))
        points.extend(link_points(topo, observations, summaries, wire_to_port, running, ts))
        for n in names:
            c = by_node.get(n)
            obs = observations.get(n)
            st = self.state.node(n, c.id if c else None)
            up = n in running
            ready = obs.ready if obs is not None and not errors.get(n) else None
            points.extend(self.node_events(n, st, up, ready, ts))
            role = "router" if self.is_sonic(kinds[n]) else "client"
            fields: dict[str, Any] = {
                "up": up,
                "container_state": c.state if c else "absent",
                "mgmt_ipv4": (c.mgmt_ipv4 if c else None) or (topo.nodes[n].mgmt_ipv4 if n in topo.nodes else None) or "",
                "image": (c.image if c else None) or (topo.nodes[n].image if n in topo.nodes else None) or "",
                "in_topology": n in topo.nodes,
                "collected": obs is not None and not errors.get(n),
            }
            if c and c.started_at and up:
                fields["uptime_s"] = now - c.started_at
            if obs is not None and not errors.get(n):
                fields.update(obs.node_fields)
            points.append(Point("lab_node", {"node": n, "kind": kinds[n], "role": role}, fields, ts))

        collected = {n: o for n, o in observations.items() if not errors.get(n)}

        # 2 bis. flux sFlow reçus depuis le cycle précédent, nommés avec ce cycle
        if self.flows is not None:
            flows_started = time.monotonic()
            batch = self.flows.drain()
            view = build_view(topo, mgmt_ips, collected, kinds, cfg.sonic_kinds, mgmt)
            points.extend(flow_points(batch, view, ts))
            points.append(status_point(batch, self.listener, time.monotonic() - flows_started, ts))
            self.log("flux sFlow", (self.listener.error if self.listener else "") or batch.last_error or None)

        # 3. sondes depuis chaque nœud collecté, vers les adresses des autres
        all_targets = lab_targets(collected, mgmt)
        probe_jobs = {}
        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            for n, obs in collected.items():
                targets = targets_for(n, all_targets, cfg.probe_scope, cfg.probe_max_targets)
                if targets:
                    probe_jobs[n] = (targets, pool.submit(self.probe_node, obs, by_node[n], targets, ts))
            for n, (targets, job) in probe_jobs.items():
                ppoints, duration, error = job.result()
                points.extend(ppoints)
                self.log(f"sondes {n}", error)
                points.append(
                    Point(
                        "lab_collector",
                        {"node": n, "task": "probe"},
                        {"duration_ms": duration * 1000.0, "ok": error is None, "targets": len(targets), "error": error or ""},
                        ts,
                    )
                )

        lines = encode_all(points)
        cycle = Point(
            "lab_collector",
            {"task": "cycle"},
            {
                "duration_ms": (time.monotonic() - started) * 1000.0,
                "nodes_total": len(names),
                "nodes_running": len(running),
                "nodes_collected": len(collected),
                "points": len(lines) + 1,
                "error": "; ".join(filter(None, [discovery_error, self.topology.error])),
            },
            ts,
        )
        return lines + encode_all([cycle])
