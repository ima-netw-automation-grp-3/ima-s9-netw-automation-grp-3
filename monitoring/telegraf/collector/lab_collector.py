#!/usr/bin/env python3
"""Collecteur de métriques du lab SONiC.

  lab_collector.py serve    boucle Telegraf inputs.execd : un cycle à chaque ligne reçue sur stdin,
                            métriques au format line protocol sur stdout ; reçoit aussi les flux
                            sFlow des routeurs (UDP SFLOW_PORT), agrégés à chaque cycle
  lab_collector.py once     un cycle affiché sur stdout (--rates : deux cycles, pour voir les débits)
  lab_collector.py iperf    test de débit iperf3 entre deux nœuds Linux, résultat écrit dans InfluxDB
  lab_collector.py traffic  trafic typé (tcp, udp, http, dns, icmp ou mix) entre deux nœuds Linux,
                            pour le voir dans les flux sFlow (tableau « Flux réseau »)

Configuration par variables d'environnement : voir labmon/config.py.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from labmon import docker, iperf, traffic  # noqa: E402
from labmon.collector import Collector  # noqa: E402
from labmon.config import Config  # noqa: E402
from labmon.flows import FlowListener, FlowTable  # noqa: E402


def serve(collector: Collector) -> int:
    """Telegraf écrit une ligne sur stdin à chaque intervalle. Les signaux accumulés pendant un cycle
    trop long arrivent ensemble dans le même read : ils ne déclenchent qu'un seul cycle."""
    fd = sys.stdin.fileno()
    while True:
        try:
            data = os.read(fd, 4096)
        except InterruptedError:
            continue
        if not data:
            return 0  # stdin fermé : Telegraf s'arrête
        try:
            lines = collector.cycle()
        except Exception:  # noqa: BLE001 - le collecteur ne doit jamais s'arrêter sur une erreur
            print("[lab-collector] cycle en échec :\n" + traceback.format_exc(), file=sys.stderr, flush=True)
            continue
        if lines:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab_collector.py", description="Collecteur de métriques du lab SONiC")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="boucle Telegraf inputs.execd (signal STDIN)")
    once = sub.add_parser("once", help="un cycle de collecte sur stdout")
    once.add_argument("--rates", action="store_true", help="deux cycles espacés de 2 s, pour inclure les débits")
    ip = sub.add_parser("iperf", help="test de débit iperf3 entre deux nœuds Linux")
    ip.add_argument("--src", default="client1", help="client iperf3 (défaut : client1)")
    ip.add_argument("--dst", default="client2", help="serveur iperf3 (défaut : client2)")
    ip.add_argument("--duration", type=int, default=10, help="durée en secondes (défaut : 10)")
    ip.add_argument("--reverse", action="store_true", help="le serveur émet vers le client (iperf3 -R)")
    ip.add_argument("--parallel", type=int, default=1, help="nombre de flux parallèles (iperf3 -P)")
    ip.add_argument("--dst-ip", default=None, help="adresse cible (défaut : première adresse du lab de DST)")
    tr = sub.add_parser("traffic", help="trafic typé entre deux nœuds Linux, pour les flux sFlow")
    tr.add_argument("--src", default="client1", help="client (défaut : client1)")
    tr.add_argument("--dst", default="client2", help="serveur (défaut : client2)")
    tr.add_argument("--type", default="mix", choices=[*traffic.TYPES, "mix"], help="type de trafic (défaut : mix, tous en même temps)")
    tr.add_argument("--duration", type=int, default=60, help="durée en secondes (défaut : 60)")
    tr.add_argument("--dst-ip", default=None, help="adresse cible (défaut : première adresse du lab de DST)")
    args = parser.parse_args(argv)
    cfg = Config.from_env()

    if args.cmd == "serve":
        flows = listener = None
        if cfg.sflow_port:
            flows = FlowTable(cfg.flow_max_keys)
            listener = FlowListener(flows, cfg.sflow_bind, cfg.sflow_port).start()
        return serve(Collector(cfg, flows, listener))
    if args.cmd == "once":
        collector = Collector(cfg)
        if args.rates:
            collector.cycle()
            time.sleep(2)
        sys.stdout.write("\n".join(collector.cycle()) + "\n")
        return 0
    try:
        if args.cmd == "traffic":
            return traffic.run(cfg, args.src, args.dst, kind=args.type, duration=max(5, args.duration), dst_ip=args.dst_ip)
        return iperf.run(
            cfg,
            args.src,
            args.dst,
            duration=max(1, args.duration),
            reverse=args.reverse,
            parallel=max(1, args.parallel),
            dst_ip=args.dst_ip,
        )
    except (iperf.IperfError, traffic.TrafficError, docker.DockerError, OSError) as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
