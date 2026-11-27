"""Service central : découverte des conteneurs du lab, état global et actions de lab."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Coroutine
from typing import Any

from . import connectivity as conn
from .cache import TTLCache
from .docker_api import DockerAPI
from .errors import BadRequest, Conflict, LabError, NodeUnavailable, NotFound
from .gitops import GitOps
from .nodes.base import BaseNode, ipv4_addresses, parse_started_at
from .nodes.linux import LinuxNode
from .nodes.sonic import SonicNode
from .settings import Settings
from .store import DOMAINS, Store
from .topology import Endpoint, Link, Topology, TopologyCache, sonic_port_for

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# États du runner (lab/.runner-status, plus « starting » déduit ici) pendant lesquels le lab change.
BUSY_RUNNER_STATES = frozenset({"loading-image", "starting", "deploying", "destroying"})
# Arrêt du service clab : SIGTERM, le runner détruit le lab, puis SIGKILL après ce délai. Égal au
# stop_grace_period du service dans docker-compose.yml.
RUNNER_STOP_TIMEOUT = 180
_RUNNING = ("running", "restarting")
_ACTIONS = {"start": "démarrage", "stop": "arrêt", "redeploy": "redéploiement"}
_WIRE_RE = re.compile(r"^eth[1-9][0-9]*$")
# Pings simultanés de la matrice de connectivité (un docker exec chacun).
_PING_CONCURRENCY = 24
# Couper/rétablir un câble ($1 = ethN, $2 = up|down ; sh POSIX : dash ou busybox). Passer l'interface
# à down fait supprimer par le noyau les routes qui la traversent (routes statiques des clients) :
# elles sont sauvegardées dans le nœud à la coupure et rejouées au rétablissement.
_WIRE_SCRIPT = r"""
i="$1"; f="/tmp/.sonic-lab-wire-$i.routes"
[ -e "/sys/class/net/$i" ] || { echo "interface $i absente du nœud" >&2; exit 1; }
if [ "$2" = down ]; then
  if [ $(( $(cat "/sys/class/net/$i/flags") & 1 )) -eq 1 ]; then
    for fam in inet inet6; do
      ip -f $fam route show 2>/dev/null | grep -E " dev $i( |\$)" | grep -v "proto kernel" \
        | grep -vE "^(fe80::|ff00::|multicast )" | sed -e "s/ linkdown//g" -e "s/ dead//g" -e "s/^/$fam /"
    done > "$f"
  fi
  ip link set dev "$i" down
else
  ip link set dev "$i" up || exit 1
  rc=0
  if [ -f "$f" ]; then
    while read -r fam route; do
      [ -n "$route" ] || continue
      ip -f "$fam" route replace $route || rc=1
    done < "$f"
    rm -f "$f"
  fi
  exit $rc
fi
"""


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class LabService:
    def __init__(self, settings: Settings, docker: DockerAPI, store: Store) -> None:
        self.settings = settings
        self.docker = docker
        self.store = store
        self._topology = TopologyCache(settings.topology_path)
        self._cache = TTLCache(settings.status_ttl)
        self._locks: dict[str, asyncio.Lock] = {}
        # Action en cours sur le service clab (une seule à la fois) et erreur de la dernière.
        self._action: str | None = None
        self._action_task: asyncio.Task[None] | None = None
        self._action_error: str | None = None
        self._action_lock = asyncio.Lock()
        self.gitops = GitOps(settings)

    # -- utilitaires -----------------------------------------------------------------------------
    def topology(self) -> Topology:
        return self._topology.get()

    @property
    def topology_file(self) -> str:
        """Fichier de topologie, tel que l'utilisateur le voit (depuis la racine du projet)."""
        return f"lab/{self.settings.topology_file}"

    def _topology_or_error(self) -> tuple[Topology | None, str | None]:
        """Topologie du fichier et ce qui empêcherait de la déployer.

        Un fichier illisible (en cours de modification, par exemple) donne (None, message) au lieu
        d'une erreur : l'état du lab et ses boutons restent disponibles.
        """
        try:
            topo = self.topology()
        except LabError as exc:
            return None, exc.message
        if topo.name != self.settings.lab_name:
            return topo, (
                f"{self.topology_file} : « name: {topo.name} » ; le lab doit s'appeler "
                f"« {self.settings.lab_name} » (le déploiement sera refusé)"
            )
        return topo, None

    def lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    def invalidate(self, name: str | None = None) -> None:
        if name is None:
            self._cache.invalidate()
        else:
            self._cache.invalidate(f"status:{name}:")

    async def containers(self) -> dict[str, dict[str, Any]]:
        """Conteneurs du lab indexés par nom de nœud (labels posés par containerlab)."""

        async def load() -> dict[str, dict[str, Any]]:
            raw = await self.docker.list_containers({"containerlab": self.settings.lab_name})
            topo, _ = self._topology_or_error()
            mgmt_net = topo.mgmt_network if topo else None
            out: dict[str, dict[str, Any]] = {}
            for c in raw:
                labels = c.get("Labels") or {}
                node = labels.get("clab-node-name")
                if not node:
                    continue
                nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
                net = nets.get(mgmt_net) if mgmt_net else None
                if net is None and nets:
                    net = next(iter(nets.values()))
                out[node] = {
                    "name": (c.get("Names") or [f"/clab-{self.settings.lab_name}-{node}"])[0].lstrip("/"),
                    "id": c.get("Id"),
                    "state": c.get("State"),
                    "status": c.get("Status"),
                    "image": c.get("Image"),
                    "kind": labels.get("clab-node-kind"),
                    "mgmt_ipv4": (net or {}).get("IPAddress") or None,
                }
            return out

        return await self._cache.get("containers", load)

    async def get_node(self, name: str) -> BaseNode:
        topo = self.topology()
        nd = topo.nodes.get(name)
        if nd is None:
            raise NotFound(f"nœud inconnu : {name}")
        container = (await self.containers()).get(name, {}).get("name") or f"clab-{self.settings.lab_name}-{name}"
        cls = SonicNode if nd.is_router else LinuxNode
        return cls(nd, container, self.docker, self.settings, self.store, mgmt_subnet=topo.mgmt_subnet)

    async def running_node(self, name: str) -> BaseNode:
        node = await self.get_node(name)
        c = (await self.containers()).get(name)
        if not c:
            raise NodeUnavailable(f"{name} n'est pas déployé (lab arrêté ou en cours de déploiement)")
        if c["state"] != "running":
            raise NodeUnavailable(f"le conteneur de {name} est {c['state']}")
        return node

    async def router(self, name: str) -> SonicNode:
        node = await self.running_node(name)
        if not isinstance(node, SonicNode):
            raise BadRequest(f"{name} n'est pas un routeur SONiC")
        return node

    # -- état des nœuds --------------------------------------------------------------------------
    async def node_status(self, name: str, *, summary: bool = False) -> dict[str, Any]:
        async def load() -> dict[str, Any]:
            node = await self.get_node(name)
            c = (await self.containers()).get(name)
            base: dict[str, Any] = {
                **node.describe(),
                "container_state": c["state"] if c else "absent",
                "container_status": c["status"] if c else None,
                "mgmt_ipv4": (c or {}).get("mgmt_ipv4") or node.node.mgmt_ipv4,
            }
            if not c or c["state"] != "running":
                detail = "conteneur absent (lab non déployé)" if not c else f"conteneur {c['state']}"
                return {**base, "phase": "down", "detail": detail, "progress": 0.0, "errors": {}}
            started = parse_started_at(await self.docker.inspect(node.container))
            if isinstance(node, SonicNode):
                st = await node.status(summary=summary, started_at=started)
            else:
                st = await node.status(summary=summary)
            return {**base, "started_at": started, **st}

        return await self._cache.get(f"status:{name}:{'summary' if summary else 'full'}", load)

    # -- runner (service compose « clab ») : démarrer, arrêter, redéployer le lab ----------------
    # Le lab tourne exactement quand le conteneur du runner tourne : l'arrêter (SIGTERM) détruit le
    # lab, le démarrer déploie lab/sonic-lab.clab.yml tel qu'il est à ce moment (lab/clab-runner.sh).
    async def _runner_container(self) -> dict[str, Any] | None:
        found = await self.docker.list_containers(
            {
                "com.docker.compose.project": self.settings.compose_project,
                "com.docker.compose.service": self.settings.runner_service,
            }
        )
        return found[0] if found else None

    @staticmethod
    def _container_name(c: dict[str, Any]) -> str:
        return (c.get("Names") or ["/?"])[0].lstrip("/")

    def _runner_file_state(self) -> str | None:
        try:
            return self.settings.runner_status_path.read_text().strip() or None
        except OSError:
            return None

    def _pending(self) -> str | None:
        task = self._action_task
        return self._action if task is not None and not task.done() else None

    async def runner(self) -> dict[str, Any]:
        state = self._runner_file_state()
        c = await self._runner_container()
        if c is None:
            state = "absent"
        elif c.get("State") not in _RUNNING:
            # conteneur arrêté : le runner a écrit « stopped » en partant (ou a été tué en route)
            state = state if state in ("stopped", "failed") else "stopped"
        elif state == "stopped":
            state = "starting"  # runner tout juste démarré : il n'a pas encore écrit son état
        return {
            "state": state or "unknown",
            "container": self._container_name(c) if c else None,
            "container_state": c.get("State") if c else None,
            "container_status": c.get("Status") if c else None,
            "action": self._pending(),  # start | stop | redeploy, lancée depuis cette interface
            "error": self._action_error,  # échec de la dernière action
        }

    async def runner_logs(self, tail: int = 300) -> str:
        c = await self._runner_container()
        if c is None:
            return "service « clab » introuvable : lancez `make up` (docker compose up -d)."
        return strip_ansi(await self.docker.logs(self._container_name(c), tail=tail))

    async def _runner_for(self, action: str) -> dict[str, Any]:
        """Conteneur du runner, si aucune autre action n'est en cours (à appeler sous _action_lock)."""
        c = await self._runner_container()
        if c is None:
            raise BadRequest("service « clab » introuvable : lancez `make up` (docker compose up -d)")
        pending = self._pending()
        if pending is not None and pending != action:
            raise Conflict(f"{_ACTIONS[pending]} du lab en cours : réessayez quand il sera terminé")
        return c

    def _launch(self, action: str, coro: Coroutine[Any, Any, None]) -> None:
        """Action longue (arrêt, redéploiement) en tâche de fond ; son échec est gardé pour l'état."""
        self._action, self._action_error = action, None
        task = asyncio.create_task(coro)
        self._action_task = task

        def done(t: asyncio.Task[None]) -> None:
            self.invalidate()
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                detail = exc.message if isinstance(exc, LabError) else f"{type(exc).__name__}: {exc}"
                self._action_error = f"{_ACTIONS[action]} du lab : {detail}"

        task.add_done_callback(done)

    async def start(self) -> dict[str, Any]:
        """Démarre le service clab arrêté : le runner déploie le fichier de topologie actuel."""
        async with self._action_lock:
            c = await self._runner_for("start")
            if c.get("State") in _RUNNING:
                if self._runner_file_state() == "destroying":
                    raise Conflict("arrêt du lab en cours : attendez qu'il soit arrêté pour le démarrer")
                return {"ok": True, "message": "le lab est déjà démarré"}
            self._action_error = None
            await self.docker.start_container(self._container_name(c))
        self.invalidate()
        return {"ok": True, "message": f"démarrage du lab : containerlab déploie {self.topology_file}"}

    async def stop(self) -> dict[str, Any]:
        """Arrête le service clab : le runner détruit le lab (tâche de fond, jusqu'à 3 min)."""
        async with self._action_lock:
            c = await self._runner_for("stop")
            if self._pending() == "stop":
                return {"ok": True, "message": "arrêt du lab déjà en cours"}
            if c.get("State") not in _RUNNING:
                return {"ok": True, "message": "le lab est déjà arrêté"}
            name = self._container_name(c)
            self._launch("stop", self.docker.stop_container(name, timeout=RUNNER_STOP_TIMEOUT))
        self.invalidate()
        return {"ok": True, "message": "arrêt du lab : containerlab détruit les nœuds"}

    async def redeploy(self) -> dict[str, Any]:
        """Redémarre le service clab : destruction du lab, puis déploiement du fichier actuel."""
        async with self._action_lock:
            c = await self._runner_for("redeploy")
            if self._pending() == "redeploy":
                return {"ok": True, "message": "redéploiement déjà en cours"}
            if c.get("State") in _RUNNING and self._runner_file_state() == "destroying":
                raise Conflict("arrêt du lab en cours : attendez qu'il soit arrêté, puis démarrez-le")
            name = self._container_name(c)
            self._launch("redeploy", self.docker.restart(name, stop_timeout=RUNNER_STOP_TIMEOUT))
        self.invalidate()
        message = f"redéploiement lancé : destruction du lab, puis déploiement de {self.topology_file}"
        return {"ok": True, "message": message}

    # -- vue d'ensemble --------------------------------------------------------------------------
    def _endpoint(self, topo: Topology, ep: Endpoint, statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
        nd = topo.nodes.get(ep.node)
        st = statuses.get(ep.node) or {}
        info: dict[str, Any] = {**ep.to_dict(), "port": None, "up": None, "wire_up": None, "addresses": []}
        interfaces = st.get("interfaces") or {}
        if nd is not None and nd.is_router:
            ports = st.get("ports") or []
            port = next((p["name"] for p in ports if p.get("wire") == ep.interface), None)
            port = port or sonic_port_for(ep.interface)
            p = next((x for x in ports if x["name"] == port), None)
            info["port"] = port
            if p is not None:
                info["up"] = p.get("oper_status") == "up"
                info["admin"] = p.get("admin_status")
                info["wire_up"] = p.get("wire_up")
            iface = interfaces.get(port or "") or {}
        else:
            iface = interfaces.get(ep.interface) or {}
            if iface:
                info["up"] = bool(iface.get("up"))
                info["wire_up"] = iface.get("admin_up")
        info["addresses"] = ipv4_addresses(iface.get("addresses") or [], topo.mgmt_subnet)
        return info

    def _link(self, topo: Topology, lk: Link, statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
        a = self._endpoint(topo, lk.a, statuses)
        z = self._endpoint(topo, lk.z, statuses)
        if a["up"] is False or z["up"] is False:
            state = "down"
        elif a["up"] and z["up"]:
            state = "up"
        else:
            state = "unknown"
        return {"id": f"{lk.a.node}:{lk.a.interface}--{lk.z.node}:{lk.z.interface}", "a": a, "z": z, "state": state}

    @staticmethod
    def _bgp_sessions(topo: Topology, statuses: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        owner: dict[str, str] = {}
        for name, st in statuses.items():
            for iface in (st.get("interfaces") or {}).values():
                for addr in iface.get("addresses") or []:
                    owner[addr.split("/")[0]] = name
        sessions: dict[tuple[str, str], dict[str, Any]] = {}
        for name, st in statuses.items():
            nd = topo.nodes.get(name)
            if nd is None or not nd.is_router:
                continue
            for peer in (st.get("bgp") or {}).get("peers") or []:
                other = owner.get(peer["peer"], peer["peer"])
                key = tuple(sorted((name, other)))
                entry = sessions.setdefault(key, {"a": key[0], "z": key[1], "peers": []})
                entry["peers"].append({"router": name, **peer})
        out = []
        for entry in sessions.values():
            states = {p.get("state") for p in entry["peers"]}
            entry["state"] = "Established" if states == {"Established"} else next(
                (s for s in states if s != "Established"), "Unknown"
            )
            out.append(entry)
        return sorted(out, key=lambda e: (e["a"], e["z"]))

    def _card(self, topo: Topology, st: dict[str, Any]) -> dict[str, Any]:
        keep = (
            "name", "kind", "role", "image", "container", "shells", "container_state", "container_status",
            "mgmt_ipv4", "phase", "detail", "progress", "started_at",
        )  # fmt: skip
        card = {k: st.get(k) for k in keep}
        addresses: dict[str, list[str]] = {}
        for iface, data in (st.get("interfaces") or {}).items():
            v4 = ipv4_addresses(data.get("addresses") or [], topo.mgmt_subnet)
            if v4 and iface != "eth0":
                addresses[iface] = v4
        card["addresses"] = addresses
        if st.get("role") == "router":
            bgp = st.get("bgp") or {}
            card["bgp"] = {"as": bgp.get("as"), "router_id": bgp.get("router_id"), "peers": bgp.get("peers") or []}
            card["ports"] = [p for p in st.get("ports") or [] if p.get("wire")]
        return card

    async def state(self) -> dict[str, Any]:
        loaded, topology_error = self._topology_or_error()
        topo = loaded or Topology(name=self.settings.lab_name)
        runner = await self.runner()
        names = list(topo.nodes)
        results = await asyncio.gather(*(self.node_status(n, summary=True) for n in names), return_exceptions=True)
        statuses: dict[str, dict[str, Any]] = {}
        action, rstate = runner["action"], runner["state"]
        stopping = action == "stop" or (rstate == "destroying" and action != "redeploy")
        deploying = action == "redeploy" or rstate in BUSY_RUNNER_STATES
        for name, res in zip(names, results, strict=True):
            if isinstance(res, BaseException):
                nd = topo.nodes[name]
                res = {"name": name, "kind": nd.kind, "role": nd.role, "phase": "error", "detail": str(res)}
            if res.get("phase") in ("error", "down") and (stopping or deploying):
                # conteneurs en cours de destruction / création : ce n'est pas une panne
                phase, detail = ("down", "arrêt") if stopping else ("booting", "déploiement")
                res = {**res, "phase": phase, "detail": f"{detail} du lab en cours…", "progress": 0.0}
            statuses[name] = res
        sessions = self._bgp_sessions(topo, statuses)
        links = [self._link(topo, lk, statuses) for lk in topo.links]
        for link in links:
            pair = tuple(sorted((link["a"]["node"], link["z"]["node"])))
            link["bgp"] = next((s for s in sessions if (s["a"], s["z"]) == pair), None)
        # Conteneurs du lab dont le nœud n'est plus dans le fichier (topologie changée pendant que le
        # lab tournait) : supprimés au prochain démarrage ou redéploiement.
        stale = sorted(set(await self.containers()) - set(topo.nodes)) if loaded else []
        return {
            "name": topo.name,
            "runner": runner,
            "mgmt": {"network": topo.mgmt_network, "subnet": topo.mgmt_subnet},
            "topology_file": self.topology_file,
            "topology_error": topology_error,
            "nodes": [self._card(topo, statuses[n]) for n in names],
            "links": links,
            "bgp_sessions": sessions,
            "stale_nodes": stale,
            "gitops": self.gitops.state(),
        }

    # -- actions de lab --------------------------------------------------------------------------
    async def connectivity(self, count: int = 2) -> dict[str, Any]:
        """Matrice de ping : chaque client vers les autres clients et les loopbacks des routeurs.

        Une cible a plusieurs adresses (client avec une carte par VRF) : la source essaie, dans l'ordre,
        celles que sa table de routage envoie dans le lab, et la case est verte dès que l'une répond.
        Sans aucune adresse routée par le lab, la case est « na » (non routée) : ce n'est pas une panne
        mais une segmentation (VRF, plateforme), voir `connectivity.py`.
        """
        state = await self.state()
        mgmt_subnet = state["mgmt"]["subnet"]
        targets = []
        for card in state["nodes"]:
            if card["role"] == "client":
                ifaces = sorted(card["addresses"].items())
            else:
                ifaces = [("Loopback0", card["addresses"].get("Loopback0", []))]
            addresses = [{"interface": i, "ip": a.split("/")[0]} for i, addrs in ifaces for a in addrs]
            if addresses:
                targets.append({"node": card["name"], "role": card["role"], "addresses": addresses})
        sources = [c["name"] for c in state["nodes"] if c["role"] == "client" and c["phase"] == "ready"]
        tables: dict[str, list[conn.Route] | None] = {}
        for src in sources:
            try:
                raw = (await self.node_status(src, summary=True)).get("routes")
            except Exception:  # noqa: BLE001 - table inconnue : toutes les adresses sont essayées
                raw = None
            tables[src] = conn.ipv4_routes(raw) if raw else None  # inconnue : toutes les adresses
        limit = asyncio.Semaphore(_PING_CONCURRENCY)

        async def one(src: str, tgt: dict[str, Any]) -> dict[str, Any]:
            routes = tables[src]
            tried, skipped = conn.plan(routes, conn.mgmt_devs(routes or [], mgmt_subnet), tgt["addresses"])
            cell: dict[str, Any] = {
                "source": src, "node": tgt["node"], "status": "na", "ok": False, "interface": None,
                "ip": None, "via": None, "loss": None, "rtt_avg": None,
            }  # fmt: skip
            outputs = []
            for addr in tried:
                try:
                    node = await self.running_node(src)
                    async with limit:
                        res = await node.ping(addr["ip"], count)
                    summary, out = res["summary"], (res["stdout"] or res["stderr"]).strip()
                except Exception as exc:  # noqa: BLE001 - une case en erreur ne bloque pas la matrice
                    summary, out = {"ok": False, "loss": None, "rtt_avg": None}, str(exc)
                cell.update(
                    status="ok" if summary.get("ok") else "fail", ok=bool(summary.get("ok")),
                    interface=addr["interface"], ip=addr["ip"], via=addr["via"] or None,
                    loss=summary.get("loss"), rtt_avg=summary.get("rtt_avg"),
                )  # fmt: skip
                via = f" via {addr['via']}" if addr["via"] else ""
                outputs.append(f"$ ping {addr['ip']} ({addr['interface']}){via}\n{out}")
                if cell["ok"]:
                    break
            if skipped:
                outputs.append("Adresses non routées par le lab depuis " + src + " :\n" + "\n".join(skipped))
            cell["output"] = "\n\n".join(outputs)
            return cell

        jobs = [one(s, t) for s in sources for t in targets if t["node"] != s]
        results = await asyncio.gather(*jobs)
        totals = {k: sum(1 for r in results if r["status"] == k) for k in ("ok", "fail", "na")}
        return {"sources": sources, "targets": targets, "results": results, "totals": totals}

    async def set_wire(self, name: str, interface: str, up: bool) -> dict[str, Any]:
        """Coupe/rétablit un câble du lab hors configuration du nœud : `ip link set ethN down|up`
        dans son espace réseau (le pair perd la porteuse). Seules les extrémités de liens sont admises."""
        topo = self.topology()
        link = next(
            (lk for lk in topo.links_of(name) if (name, interface) in ((lk.a.node, lk.a.interface), (lk.z.node, lk.z.interface))),
            None,
        )
        if link is None or not _WIRE_RE.match(interface):
            raise BadRequest(f"{interface} n'est pas une extrémité de lien de {name}")
        peer = link.z if (link.a.node, link.a.interface) == (name, interface) else link.a
        node = await self.running_node(name)
        async with self.lock(name):
            res = await node.exec(["sh", "-c", _WIRE_SCRIPT, "wire", interface, "up" if up else "down"], timeout=20)
        self.invalidate()
        return {
            "ok": res.ok,
            "node": name,
            "interface": interface,
            "up": up,
            "peer": peer.to_dict(),
            "output": res.output.strip(),
        }

    async def routers(self) -> list[str]:
        containers = await self.containers()
        return [
            n for n, nd in self.topology().nodes.items()
            if nd.is_router and containers.get(n, {}).get("state") == "running"
        ]  # fmt: skip

    async def save_startup(self, names: list[str]) -> dict[str, Any]:
        """Running → conf de démarrage : fichiers de lab/configs/, ou merge request en mode GitOps."""
        if not self.gitops.enabled:
            out = {}
            for name in names:
                async with self.lock(name):
                    out[name] = await (await self.router(name)).save_startup()
            return {"ok": all(r["ok"] for r in out.values()), "gitops": False, "results": out}
        files: dict[str, str] = {}
        for name in names:
            async with self.lock(name):
                contents = await (await self.router(name)).startup_contents()
            for domain, content in contents.items():
                files[f"configs/{name}/{DOMAINS[domain]}"] = content
        description = (
            "Conf running sauvegardée depuis l'interface web du lab (« Sauvegarder running → startup »).\n\n"
            "Une fois fusionnée, l'agent GitOps l'installe comme conf de démarrage ; les routeurs sont "
            "remis dans cette conf sans être recréés (leur running y est déjà conforme)."
        )
        return await self.gitops.propose(files, routers=names, description=description)

    async def save_all(self) -> dict[str, Any]:
        return await self.save_startup(await self.routers())

    async def reset_all(self) -> dict[str, Any]:
        out = {}
        for name in await self.routers():
            async with self.lock(name):
                out[name] = await (await self.router(name)).restore("startup")
            self.invalidate(name)
        return {"ok": all(r["ok"] for r in out.values()), "results": out}
