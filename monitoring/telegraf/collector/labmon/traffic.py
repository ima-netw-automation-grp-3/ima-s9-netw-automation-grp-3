"""Trafic typé entre deux nœuds Linux, pour voir les flux sFlow dans Grafana (make traffic).

    lab_collector.py traffic --src client1 --dst client2 --type mix --duration 60

Les routeurs échantillonnent un paquet sur 256 (minimum du modèle YANG sonic-sflow) : chaque type de
trafic génère donc du volume pendant toute la durée, sinon il serait à peine visible (un curl ou un
nslookup isolé n'a presque aucune chance d'être échantillonné). Les outils (iperf3, curl,
busybox-extras, dnsmasq) sont installés à la demande par apk, ce qui demande l'accès Internet des
clients par eth0, et disparaissent au redéploiement. Un lab_event (type traffic) marque chaque test
dans les graphes de Grafana.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import docker
from .config import Config
from .iperf import default_address, post
from .lineproto import Point, encode_all, ns
from .topology import TopologyCache

WWW = "/tmp/lab-www"
FILE_MB = 64
DNS_WORKERS = 8
DNS_ZONE = "lab.test"

# type -> (description, application attendue dans le tableau « Flux réseau »)
TYPES = {
    "tcp": ("iperf3 en TCP, port 5201, 200 Mbit/s", "iperf3"),
    "udp": ("iperf3 en UDP, port 5202, 50 Mbit/s", "iperf3"),
    "http": (f"téléchargements HTTP en boucle (curl, 200 Mbit/s) d'un fichier de {FILE_MB} Mo, port 80", "http"),
    "dns": (f"requêtes DNS en boucle ({DNS_WORKERS} nslookup en parallèle) vers dnsmasq, port 53", "dns"),
    "icmp": ("ping de 1200 octets toutes les 2 ms", "icmp"),
}
MIX = tuple(TYPES)
# commande -> paquet Alpine
PACKAGES = {"iperf3": "iperf3", "curl": "curl", "busybox-extras": "busybox-extras", "dnsmasq": "dnsmasq"}


class TrafficError(RuntimeError):
    pass


@dataclass
class Scenario:
    """Un type de trafic : outils, serveur à lancer sur DST, commande du client, lecture du résultat."""

    kind: str
    client_tools: tuple[str, ...] = ()
    server_tools: tuple[str, ...] = ()
    server_setup: list[list[str]] = field(default_factory=list)  # commandes, dans l'ordre
    server_daemon: list[str] | None = None  # docker exec -d
    server_stop: list[str] | None = None
    client: list[str] = field(default_factory=list)


def _loop(duration: int, body: str) -> str:
    """Boucle shell (busybox) qui répète body jusqu'à la fin de la durée et affiche le nombre de tours."""
    return f'end=$(($(date +%s) + {duration})); n=0; while [ "$(date +%s)" -lt "$end" ]; do {body} && n=$((n + 1)); done; echo "$n"'


def scenarios(kinds: tuple[str, ...], ip: str, duration: int) -> list[Scenario]:
    out = []
    for kind in kinds:
        if kind == "tcp":
            out.append(
                Scenario(
                    kind,
                    ("iperf3",),
                    ("iperf3",),
                    [["pkill", "-f", "^iperf3 -s -1 -p 5201"]],
                    ["iperf3", "-s", "-1", "-p", "5201", "--idle-timeout", str(duration + 30)],
                    None,
                    ["iperf3", "-c", ip, "-p", "5201", "-t", str(duration), "-b", "200M", "--connect-timeout", "3000"],
                )
            )
        elif kind == "udp":
            out.append(
                Scenario(
                    kind,
                    ("iperf3",),
                    ("iperf3",),
                    [["pkill", "-f", "^iperf3 -s -1 -p 5202"]],
                    ["iperf3", "-s", "-1", "-p", "5202", "--idle-timeout", str(duration + 30)],
                    None,
                    ["iperf3", "-c", ip, "-p", "5202", "-u", "-b", "50M", "-t", str(duration), "--connect-timeout", "3000"],
                )
            )
        elif kind == "http":
            make_file = f"mkdir -p {WWW} && [ -s {WWW}/fichier.bin ] || dd if=/dev/zero of={WWW}/fichier.bin bs=1M count={FILE_MB} 2>/dev/null"
            out.append(
                Scenario(
                    kind,
                    ("curl",),
                    ("busybox-extras",),
                    [["sh", "-c", make_file], ["pkill", "-f", "busybox-extras httpd -f -p 80"]],
                    ["busybox-extras", "httpd", "-f", "-p", "80", "-h", WWW],
                    ["pkill", "-f", "busybox-extras httpd -f -p 80"],
                    ["sh", "-c", _loop(duration, f"curl -s -o /dev/null --max-time 15 --limit-rate 25M http://{ip}/fichier.bin")],
                )
            )
        elif kind == "dns":
            # -type=a : sans lui, busybox demande aussi AAAA, que dnsmasq refuse (code d'erreur)
            worker = _loop(duration, f'nslookup -type=a "h$n.{DNS_ZONE}" {ip} >/dev/null 2>&1')
            client = f"for w in $(seq {DNS_WORKERS}); do ( {worker} ) & done; wait"
            out.append(
                Scenario(
                    kind,
                    (),
                    ("dnsmasq",),
                    [["pkill", "-x", "dnsmasq"]],
                    [
                        "dnsmasq",
                        "-k",
                        "--no-resolv",
                        "--no-hosts",
                        "--bind-interfaces",
                        f"--listen-address={ip}",
                        f"--address=/{DNS_ZONE}/10.99.0.1",
                        "--log-facility=-",
                    ],
                    ["pkill", "-x", "dnsmasq"],
                    ["sh", "-c", client],
                )
            )
        elif kind == "icmp":
            out.append(Scenario(kind, client=["ping", "-q", "-i", "0.002", "-s", "1200", "-w", str(duration), ip]))
        else:
            raise TrafficError(f"type de trafic inconnu : {kind} (types : {', '.join(TYPES)}, mix)")
    return out


def summary(kind: str, res: docker.ExecResult) -> str:
    """Résultat d'un client, en une ligne."""
    out = res.stdout or ""
    if kind in ("tcp", "udp"):
        m = re.findall(r"([\d.]+ [KMG]?bits/sec).*(?:sender|receiver)", out)
        return f"iperf3 : {m[-1]} reçus" if m else f"iperf3 en échec : {res.output[-200:]}"
    if kind == "http":
        n = out.strip().splitlines()[-1] if out.strip() else "0"
        return f"{n} téléchargement(s) de {FILE_MB} Mo"
    if kind == "dns":
        total = sum(int(x) for x in out.split() if x.isdigit())
        return f"{total} requête(s) DNS"
    if kind == "icmp":
        m = re.search(r"(\d+) packets transmitted, (\d+) packets received", out)
        return f"{m.group(1)} ping(s), {m.group(2)} réponse(s)" if m else f"ping en échec : {res.output[-200:]}"
    return res.output[-200:]


def ensure_tools(container: docker.Container, tools: tuple[str, ...]) -> None:
    if not tools:
        return
    checks = " ".join(f'command -v {t} >/dev/null 2>&1 || missing="$missing {PACKAGES[t]}";' for t in tools)
    script = f'missing=""; {checks} [ -z "$missing" ] || apk add -q $missing'
    res = docker.exec_in(container.name, ["sh", "-c", script], 180, in_container_timeout=False)
    if res.exit_code != 0:
        raise TrafficError(f"outils indisponibles sur {container.node} ({', '.join(tools)}) : installation par apk impossible (accès Internet ?) : {res.output[-300:]}")


def run(cfg: Config, src: str, dst: str, kind: str = "mix", duration: int = 60, dst_ip: str | None = None) -> int:
    kinds = MIX if kind == "mix" else (kind,)
    topo = TopologyCache(cfg.topology_path).get()
    containers = {c.node: c for c in docker.list_containers(cfg.lab_name, topo.mgmt_network)}
    for node in (src, dst):
        c = containers.get(node)
        if c is None or not c.running:
            raise TrafficError(f"{node} n'est pas démarré (nœuds : {', '.join(sorted(containers)) or 'aucun'})")
        if c.kind in cfg.sonic_kinds:
            raise TrafficError(f"{node} est un routeur SONiC : le trafic se génère entre nœuds Linux (clients)")
    client, server = containers[src], containers[dst]
    ip = dst_ip or default_address(server, topo.mgmt_subnet)
    plan = scenarios(kinds, ip, duration)
    print(f"trafic {kind} : {src} → {dst} ({ip}), {duration} s")
    for sc in plan:
        print(f"  {sc.kind:5} {TYPES[sc.kind][0]}  →  application « {TYPES[sc.kind][1]} »")
    ensure_tools(client, tuple(sorted({t for sc in plan for t in sc.client_tools})))
    ensure_tools(server, tuple(sorted({t for sc in plan for t in sc.server_tools})))
    for sc in plan:
        for argv in sc.server_setup:
            docker.exec_in(server.name, argv, 60, in_container_timeout=False)
        if sc.server_daemon:
            docker.exec_detached(server.name, sc.server_daemon)
    time.sleep(1)  # serveurs prêts
    started = time.time()
    names = ", ".join(sc.kind for sc in plan)
    post(encode_all([_event(src, f"trafic {names} : {src} → {dst} ({ip}), {duration} s", started)]), cfg.listener_url)
    try:
        with ThreadPoolExecutor(max_workers=len(plan)) as pool:
            jobs = {sc.kind: pool.submit(docker.exec_in, client.name, sc.client, duration + 60, in_container_timeout=False) for sc in plan}
            results = {k: job.result() for k, job in jobs.items()}
    finally:
        for sc in plan:
            if sc.server_stop:
                docker.exec_in(server.name, sc.server_stop, 15, in_container_timeout=False)
    lines = [f"{k} : {summary(k, res)}" for k, res in results.items()]
    for line in lines:
        print(f"  {line}")
    post(encode_all([_event(src, f"fin du trafic {src} → {dst} : " + " ; ".join(lines), time.time())]), cfg.listener_url)
    print("Grafana : tableau « Flux réseau », liste « Trafic » › « Entrée du réseau » (les routeurs échantillonnent 1 paquet sur 256)")
    return 0


def _event(node: str, message: str, ts: float) -> Point:
    return Point("lab_event", {"node": node, "program": "traffic", "severity": "NOTICE", "type": "traffic"}, {"message": message}, ns(ts))
