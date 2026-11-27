"""Mesure de débit iperf3 à la demande entre deux nœuds Linux (make iperf SRC=… DST=…).

Les résultats (débit par seconde, retransmissions, RTT, résumé) sont écrits dans InfluxDB via le listener
local de Telegraf, avec un événement lab_event qui sert d'annotation dans Grafana.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from . import docker, linux
from .common import NodeObs, is_lab_address, mgmt_network
from .config import Config
from .interfaces import if_type
from .lineproto import Point, encode_all, ns
from .remote import source
from .state import NodeState
from .topology import TopologyCache

PORT = 5201


class IperfError(RuntimeError):
    pass


def fmt_bps(bps: float) -> str:
    for unit, scale in (("Gbit/s", 1e9), ("Mbit/s", 1e6), ("kbit/s", 1e3)):
        if bps >= scale:
            return f"{bps / scale:.2f} {unit}".replace(".", ",")
    return f"{bps:.0f} bit/s"


def default_address(container: docker.Container, mgmt_subnet: str | None) -> str:
    """Première adresse du lab (hors management) du nœud, de préférence sur un fil ethN."""
    res = docker.exec_in(container.name, ["sh", "-c", source("linux_collect.sh")], 15, in_container_timeout=False)
    obs = linux.build(NodeObs(container.node, container.kind, "client"), NodeState(), res.stdout, 0, mgmt_subnet, 0)
    mgmt = mgmt_network(mgmt_subnet)
    for iface in sorted(obs.addresses, key=lambda i: (if_type(i) != "wire", i)):
        for a in obs.addresses[iface]:
            if a["family"] == "inet" and is_lab_address(a["address"], a["scope"], mgmt):
                return a["address"]
    raise IperfError(f"aucune adresse IPv4 du lab sur {container.node}")


def ensure_iperf(container: docker.Container) -> None:
    script = "command -v iperf3 >/dev/null 2>&1 || { command -v apk >/dev/null 2>&1 && apk add -q iperf3; }; command -v iperf3"
    res = docker.exec_in(container.name, ["sh", "-c", script], 120, in_container_timeout=False)
    if res.exit_code != 0:
        raise IperfError(
            f"iperf3 indisponible sur {container.node} (installation par apk impossible : accès Internet ?) : {res.output[-300:]}"
        )


def parse_result(data: dict[str, Any], tags: dict[str, str], parallel: int) -> tuple[list[Point], dict[str, Any]]:
    """JSON d'iperf3 -J -> points lab_iperf (un par seconde + un résumé) et résumé lisible."""
    start = float(((data.get("start") or {}).get("timestamp") or {}).get("timesecs") or time.time())
    points = []
    for interval in data.get("intervals") or []:
        s = interval.get("sum") or {}
        streams = interval.get("streams") or []
        rtts = [st["rtt"] for st in streams if st.get("rtt")]
        fields: dict[str, Any] = {"bps": s.get("bits_per_second"), "bytes": s.get("bytes")}
        if s.get("retransmits") is not None:
            fields["retransmits"] = s["retransmits"]
        if rtts:
            fields["rtt_ms"] = sum(rtts) / len(rtts) / 1000.0
        points.append(Point("lab_iperf", {**tags, "sample": "interval"}, fields, ns(start + float(s.get("end") or 0))))
    end = data.get("end") or {}
    sent, received = end.get("sum_sent") or {}, end.get("sum_received") or {}
    senders = [st.get("sender") or {} for st in end.get("streams") or []]
    mean_rtts = [s["mean_rtt"] for s in senders if s.get("mean_rtt")]
    max_rtts = [s["max_rtt"] for s in senders if s.get("max_rtt")]
    summary: dict[str, Any] = {
        "sent_bps": sent.get("bits_per_second"),
        "received_bps": received.get("bits_per_second"),
        "retransmits": sent.get("retransmits"),
        "duration_s": received.get("seconds") or sent.get("seconds"),
        "bytes": received.get("bytes"),
        "parallel": parallel,
    }
    if mean_rtts:
        summary["mean_rtt_ms"] = sum(mean_rtts) / len(mean_rtts) / 1000.0
    if max_rtts:
        summary["max_rtt_ms"] = max(max_rtts) / 1000.0
    finished = start + float(summary["duration_s"] or 0)
    points.append(Point("lab_iperf", {**tags, "sample": "summary"}, summary, ns(finished)))
    return points, summary


def post(lines: list[str], url: str) -> None:
    req = urllib.request.Request(url, data=("\n".join(lines) + "\n").encode(), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                raise IperfError(f"écriture refusée par Telegraf : HTTP {resp.status}")
    except urllib.error.URLError as exc:
        raise IperfError(f"listener Telegraf injoignable ({url}) : {exc}") from exc


def run(cfg: Config, src: str, dst: str, duration: int = 10, reverse: bool = False, parallel: int = 1, dst_ip: str | None = None) -> int:
    topo = TopologyCache(cfg.topology_path).get()
    containers = {c.node: c for c in docker.list_containers(cfg.lab_name, topo.mgmt_network)}
    for node in (src, dst):
        c = containers.get(node)
        if c is None or not c.running:
            raise IperfError(f"{node} n'est pas démarré (nœuds : {', '.join(sorted(containers)) or 'aucun'})")
        if c.kind in cfg.sonic_kinds:
            raise IperfError(f"{node} est un routeur SONiC : iperf3 ne tourne que sur les nœuds Linux (clients)")
    client, server = containers[src], containers[dst]
    ip = dst_ip or default_address(server, topo.mgmt_subnet)
    print(f"iperf3 : {src} → {dst} ({ip}), {duration} s{', inverse' if reverse else ''}{f', {parallel} flux' if parallel > 1 else ''}")
    ensure_iperf(client)
    ensure_iperf(server)
    # serveur au premier plan d'un docker exec détaché : rien n'est rattaché au PID 1 du conteneur
    # (un shell qui ne récolte pas ses orphelins), et il s'arrête seul après un test ou une inactivité
    docker.exec_in(server.name, ["pkill", "-f", "^iperf3 -s"], 10, in_container_timeout=False)  # serveur resté d'un test interrompu
    docker.exec_detached(server.name, ["iperf3", "-s", "-1", "-p", str(PORT), "--idle-timeout", str(duration + 30)])
    argv = ["iperf3", "-c", ip, "-p", str(PORT), "-t", str(duration), "-J", "--connect-timeout", "3000"]
    if reverse:
        argv.append("-R")
    if parallel > 1:
        argv += ["-P", str(parallel)]
    data: dict[str, Any] = {}
    for _ in range(5):
        time.sleep(1)
        res = docker.exec_in(client.name, argv, duration + 30, in_container_timeout=False)
        try:
            data = json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            data = {"error": res.output[-300:]}
        if not data.get("error"):
            break
    if data.get("error"):
        raise IperfError(f"test iperf3 en échec : {data['error']}")
    sender, receiver = (dst, src) if reverse else (src, dst)
    test_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    tags = {"src": sender, "dst": receiver, "dst_ip": ip, "client": src, "server": dst, "test_id": test_id}
    points, summary = parse_result(data, tags, parallel)
    rtt = f", RTT moyen {summary['mean_rtt_ms']:.2f} ms".replace(".", ",") if summary.get("mean_rtt_ms") else ""
    retr = summary.get("retransmits")
    message = (
        f"iperf3 {sender} → {receiver} ({ip}) : {fmt_bps(summary.get('received_bps') or 0)} reçus"
        + (f", {retr} retransmission(s)" if retr is not None else "")
        + rtt
    )
    points.append(Point("lab_event", {"node": src, "program": "iperf3", "severity": "NOTICE", "type": "iperf"}, {"message": message}, ns(time.time())))
    post(encode_all(points), cfg.listener_url)
    print(message)
    print(f"résultat écrit dans InfluxDB (lab_iperf, test_id={test_id})")
    return 0
