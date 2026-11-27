"""Conversion de la sortie de remote/sonic_collect.py (routeur SONiC) en points InfluxDB."""

from __future__ import annotations

import re
from typing import Any

from .common import SERVICE_STATE_CODES, NodeObs, bgp_state_code, parse_frr_uptime
from .lineproto import Point, generic_fields, ns_us, num
from .state import NodeState

_CRM_RE = re.compile(r"^crm_stats_(.+)_(used|available)$")
_MESSAGE_FIELDS = {
    "opensSent": "opens_sent",
    "opensRecv": "opens_recv",
    "notificationsSent": "notifications_sent",
    "notificationsRecv": "notifications_recv",
    "updatesSent": "updates_sent",
    "updatesRecv": "updates_recv",
    "keepalivesSent": "keepalives_sent",
    "keepalivesRecv": "keepalives_recv",
    "routeRefreshSent": "route_refresh_sent",
    "routeRefreshRecv": "route_refresh_recv",
    "capabilitySent": "capability_sent",
    "capabilityRecv": "capability_recv",
    "totalSent": "messages_sent",
    "totalRecv": "messages_recv",
    "depthInq": "depth_inq",
    "depthOutq": "depth_outq",
}


def event_type(program: str, message: str) -> str:
    if "%ADJCHANGE" in message or "%NOTIFICATION" in message or program == "bgpd":
        return "bgp"
    if "oper state set from" in message or "oper status" in message.lower():
        return "port"
    if program == "lab-bootstrap":
        return "bootstrap"
    return "syslog"


class SonicConverter:
    def __init__(self, obs: NodeObs, st: NodeState, data: dict[str, Any], ts_ns: int) -> None:
        self.obs = obs
        self.st = st
        self.data = data
        self.ts = ts_ns
        self.base = {"node": obs.name}

    def add(self, measurement: str, tags: dict[str, Any], fields: dict[str, Any], ts: int | None = None) -> None:
        self.obs.points.append(Point(measurement, {**self.base, **tags}, fields, self.ts if ts is None else ts))

    def event(self, kind: str, message: str, severity: str = "NOTICE") -> None:
        self.add("lab_event", {"program": "lab-collector", "severity": severity, "type": kind}, {"message": message})

    def bgp_events(self, key: tuple[str, str], p: dict[str, Any], state: str, fields: dict[str, Any]) -> None:
        """Événements BGP par comparaison avec le relevé précédent.

        FRR de cette image ne journalise rien (journalisation désactivée) : on détecte les changements
        d'état, et aussi les sessions coupées puis rétablies entre deux relevés (uptime qui recule,
        compteurs de connexions qui augmentent).
        """
        vrf, peer = key
        current = {
            "state": state,
            "uptime": fields["uptime_s"],
            "established": p.get("connectionsEstablished") or 0,
            "dropped": p.get("connectionsDropped") or 0,
        }
        previous = self.st.bgp.get(key)
        self.st.bgp[key] = current
        if previous is None:
            return
        name = f"{peer} ({p.get('nbrDesc') or p.get('hostname') or 'AS ' + str(p.get('remoteAs'))})"
        if previous["state"] != state:
            up = state == "Established"
            reason = f" ; dernier reset : {p.get('lastResetDueTo')}" if not up and p.get("lastResetDueTo") else ""
            self.event("bgp", f"BGP {vrf} : voisin {name} {previous['state']} → {state}{reason}", "NOTICE" if up else "WARNING")
        elif state == "Established" and (
            current["uptime"] < previous["uptime"]
            or current["established"] > previous["established"]
            or current["dropped"] > previous["dropped"]
        ):
            reason = f" (dernier reset : {p.get('lastResetDueTo')})" if p.get("lastResetDueTo") else ""
            self.event(
                "bgp",
                f"BGP {vrf} : voisin {name} réinitialisé puis rétabli entre deux relevés, "
                f"établi depuis {current['uptime']:.0f} s{reason}",
                "WARNING",
            )

    # -- sections ---------------------------------------------------------------------------------
    def meta(self) -> None:
        meta = self.data.get("meta") or {}
        version = meta.get("version") or {}
        device = meta.get("device") or {}
        self.obs.ready = bool(meta.get("ready"))
        self.obs.node_fields.update(
            ready=self.obs.ready,
            hostname=meta.get("hostname") or "",
            kernel=meta.get("kernel") or "",
            sonic_version=version.get("build_version") or "",
            sonic_os_version=str(version.get("sonic_os_version") or ""),
            debian_version=str(version.get("debian_version") or ""),
            asic_type=version.get("asic_type") or "",
            build_date=version.get("build_date") or "",
            commit_id=str(version.get("commit_id") or ""),
            hwsku=device.get("hwsku") or "",
            platform=device.get("platform") or "",
            switch_type=device.get("switch_type") or "",
            frr_version=meta.get("frr") or "",
            frr_daemons=",".join(meta.get("frr_daemons") or []),
        )

    def ports(self) -> None:
        ports = self.data.get("ports") or {}
        config, appl, state = ports.get("config") or {}, ports.get("appl") or {}, ports.get("state") or {}
        lanemap = ports.get("lanemap") or {}
        for name, cfg in config.items():
            ap, stt = appl.get(name) or {}, state.get(name) or {}
            wire = lanemap.get(cfg.get("lanes", ""))
            if wire:
                self.obs.wires[name] = wire
            speed = num(cfg.get("speed") or ap.get("speed"))
            if speed:
                self.obs.port_speed[name] = speed
            oper = (ap.get("oper_status") or stt.get("netdev_oper_status") or "down") == "up"
            self.obs.port_oper[name] = oper
            fields: dict[str, Any] = {
                **generic_fields(cfg, "cfg_"),
                **generic_fields(ap, "appl_"),
                **generic_fields(stt, "state_"),
                "admin_up": cfg.get("admin_status", "down") == "up",
                "oper_up": oper,
                "speed_mbps": speed,
                "mtu": num(cfg.get("mtu") or ap.get("mtu")),
                "index": num(cfg.get("index")),
                "description": cfg.get("description", ""),
                "lanes": cfg.get("lanes", ""),
                "cabled": bool(wire),
            }
            self.add("lab_sonic_port", {"port": name, "alias": cfg.get("alias"), "wire": wire}, fields)

    def bgp(self) -> None:
        bgp = self.data.get("bgp") or {}
        summary, neighbors = bgp.get("summary") or {}, bgp.get("neighbors") or {}
        afi_info: dict[tuple[str, str], dict[str, Any]] = {}
        seen = set()
        for vrf, peers in sorted(neighbors.items()):
            if not isinstance(peers, dict):
                continue
            for peer, p in sorted(peers.items()):
                if not isinstance(p, dict) or "bgpState" not in p:
                    continue
                state = p.get("bgpState") or "Unknown"
                established = state == "Established"
                ms = p.get("messageStats") or {}
                fields: dict[str, Any] = {
                    "state": state,
                    "state_code": bgp_state_code(state),
                    "established": established,
                    "uptime_s": (p.get("bgpTimerUpMsec") or 0) / 1000.0 if established else 0.0,
                    "connections_established": p.get("connectionsEstablished"),
                    "connections_dropped": p.get("connectionsDropped"),
                    "last_reset": p.get("lastResetDueTo") or "",
                    "last_reset_code": p.get("lastResetCode"),
                    "last_reset_ago_s": (p["lastResetTimerMsecs"] / 1000.0) if p.get("lastResetTimerMsecs") is not None else None,
                    "hold_time_s": (p["bgpTimerHoldTimeMsecs"] / 1000.0) if p.get("bgpTimerHoldTimeMsecs") is not None else None,
                    "keepalive_s": (p["bgpTimerKeepAliveIntervalMsecs"] / 1000.0) if p.get("bgpTimerKeepAliveIntervalMsecs") is not None else None,
                    "rtt_ms": p.get("estimatedRttInMsecs"),
                    "update_elapsed_s": (p["bgpInUpdateElapsedTimeMsecs"] / 1000.0) if p.get("bgpInUpdateElapsedTimeMsecs") is not None else None,
                    "description": p.get("nbrDesc") or "",
                    "hostname": p.get("hostname") or "",
                    "remote_router_id": p.get("remoteRouterId") or "",
                    "local_router_id": p.get("localRouterId") or "",
                    "local_address": p.get("hostLocal") or "",
                    "remote_port": p.get("portForeign"),
                }
                for src, dst in _MESSAGE_FIELDS.items():
                    if src in ms:
                        fields[dst] = ms[src]
                tags = {
                    "vrf": vrf,
                    "peer": peer,
                    "remote_as": p.get("remoteAs"),
                    "local_as": p.get("localAs"),
                    "peer_type": "ebgp" if p.get("nbrExternalLink") else "ibgp",
                }
                self.add("lab_bgp_peer", tags, fields)
                for afi, info in (p.get("addressFamilyInfo") or {}).items():
                    afi_info[(vrf, peer, afi)] = info
                key = (vrf, peer)
                seen.add(key)
                self.bgp_events(key, p, state, fields)
        for key in [k for k in self.st.bgp if k not in seen]:
            self.event("bgp", f"BGP {key[0]} : voisin {key[1]} supprimé de la configuration")
            del self.st.bgp[key]
        for vrf, afis in sorted(summary.items()):
            if not isinstance(afis, dict):
                continue
            for afi, info in sorted(afis.items()):
                if not isinstance(info, dict) or "peers" not in info:
                    continue
                self.add(
                    "lab_bgp",
                    {"vrf": vrf, "afi": afi},
                    {
                        "as": info.get("as"),
                        "router_id": info.get("routerId") or "",
                        "table_version": info.get("tableVersion"),
                        "rib_count": info.get("ribCount"),
                        "rib_memory": info.get("ribMemory"),
                        "peer_count": info.get("peerCount"),
                        "peer_memory": info.get("peerMemory"),
                        "failed_peers": info.get("failedPeers"),
                        "total_peers": info.get("totalPeers"),
                        "dynamic_peers": info.get("dynamicPeers"),
                        "established_peers": sum(1 for p in (info.get("peers") or {}).values() if p.get("state") == "Established"),
                    },
                )
                for peer, p in sorted((info.get("peers") or {}).items()):
                    extra = afi_info.get((vrf, peer, afi)) or {}
                    self.add(
                        "lab_bgp_afi",
                        {"vrf": vrf, "afi": afi, "peer": peer},
                        {
                            "state": p.get("state") or "",
                            "established": p.get("state") == "Established",
                            "pfx_rcd": p.get("pfxRcd"),
                            "pfx_snt": p.get("pfxSnt"),
                            "accepted": extra.get("acceptedPrefixCounter"),
                            "sent": extra.get("sentPrefixCounter"),
                            "msg_rcvd": p.get("msgRcvd"),
                            "msg_sent": p.get("msgSent"),
                            "inq": p.get("inq"),
                            "outq": p.get("outq"),
                            "table_version": p.get("tableVersion"),
                            "uptime_s": (p.get("peerUptimeMsec") or 0) / 1000.0,
                        },
                    )

    def routes(self) -> None:
        for afi, info in sorted((self.data.get("routes") or {}).items()):
            for vrf, summary in sorted((info.get("summary") or {}).items()):
                if not isinstance(summary, dict):
                    continue
                for r in summary.get("routes") or []:
                    self.add(
                        "lab_route",
                        {"vrf": vrf, "afi": afi, "protocol": r.get("type")},
                        {"rib": r.get("rib"), "fib": r.get("fib"), "fib_offloaded": r.get("fibOffLoaded"), "fib_trapped": r.get("fibTrapped")},
                    )
                self.add(
                    "lab_route",
                    {"vrf": vrf, "afi": afi, "protocol": "total"},
                    {"rib": summary.get("routesTotal"), "fib": summary.get("routesTotalFib")},
                )
            for vrf, prefixes in sorted((info.get("routes") or {}).items()):
                for prefix, entries in sorted(prefixes.items()):
                    for proto in sorted({e.get("protocol") or "unknown" for e in entries}):
                        group = [e for e in entries if (e.get("protocol") or "unknown") == proto]
                        best = next((e for e in group if e.get("selected")), group[0])
                        hops = best.get("nexthops") or []
                        self.add(
                            "lab_route_entry",
                            {"vrf": vrf, "afi": afi, "prefix": prefix, "protocol": proto},
                            {
                                "present": True,
                                "selected": any(e.get("selected") for e in group),
                                "installed": any(e.get("installed") for e in group),
                                "offloaded": any(e.get("offloaded") for e in group),
                                "distance": best.get("distance"),
                                "metric": best.get("metric"),
                                "uptime_s": parse_frr_uptime(best.get("uptime")),
                                "nexthops": len(hops),
                                "active_nexthops": sum(1 for h in hops if h.get("active")),
                                "via": ",".join(h.get("ip") or h.get("interface") or "" for h in hops),
                                "interfaces": ",".join(h.get("interface") or "" for h in hops),
                            },
                        )
            if info.get("truncated"):
                self.obs.errors.setdefault("routes", f"table {afi} tronquée (ROUTES_MAX)")

    def neighbors(self) -> None:
        counts: dict[tuple[str, str, str], int] = {}
        for n in self.data.get("neighbors") or []:
            family = "ipv6" if ":" in (n.get("dst") or "") else "ipv4"
            state = ",".join(n.get("state") or []) or "NONE"
            key = (n.get("dev") or "?", family, state)
            counts[key] = counts.get(key, 0) + 1
        for (dev, family, state), count in sorted(counts.items()):
            self.add("lab_neighbor", {"interface": dev, "family": family, "state": state}, {"count": count})

    def services(self) -> None:
        clock = self.obs.clock
        seen = set()
        for svc in self.data.get("services") or []:
            proc = svc.get("proc") or {}
            state = svc.get("state") or "UNKNOWN"
            fields: dict[str, Any] = {
                "state": state,
                "state_code": SERVICE_STATE_CODES.get(state, 7),
                "running": state == "RUNNING",
                "failed": state in ("FATAL", "BACKOFF"),
                "uptime_s": svc.get("uptime_s"),
                "pid": svc.get("pid"),
                "detail": svc.get("detail") or "",
                **{k: proc.get(k) for k in ("processes", "cpu_seconds", "threads", "rss_bytes", "fds")},
            }
            seen.add(svc["name"])
            cpu = self.st.svc_rates.rate(svc["name"], clock, proc.get("cpu_seconds"))
            if cpu is not None:
                fields["cpu_pct"] = cpu * 100.0
            self.add("lab_sonic_service", {"service": svc["name"]}, fields)
        self.st.svc_rates.keep_only(seen)

    def db(self) -> None:
        for db, tables in sorted((self.data.get("db") or {}).items()):
            for table, count in sorted(tables.items()):
                self.add("lab_sonic_db", {"db": db, "table": table}, {"keys": count})

    def counters(self) -> None:
        counters = self.data.get("counters") or {}
        for scope, values in sorted((counters.get("crm") or {}).items()):
            resources: dict[str, dict[str, float]] = {}
            for name, value in values.items():
                m = _CRM_RE.match(name)
                if m and num(value) is not None:
                    resources.setdefault(m.group(1), {})[m.group(2)] = num(value)  # type: ignore[assignment]
            parts = scope.split(":")
            tags: dict[str, Any] = {}
            if parts[0] == "ACL_STATS" and len(parts) == 3:
                tags = {"stage": parts[1], "bind_point": parts[2]}
            elif parts[0] != "STATS":
                tags = {"scope": scope}
            for resource, v in sorted(resources.items()):
                used, avail = v.get("used"), v.get("available")
                fields: dict[str, Any] = {"used": used, "available": avail}
                if used is not None and avail is not None:
                    fields["total"] = used + avail
                    fields["used_pct"] = used / (used + avail) * 100.0 if used + avail else 0.0
                self.add("lab_sonic_crm", {**tags, "resource": resource}, fields)
        for obj in counters.get("objects") or []:
            self.add(
                "lab_sonic_counters",
                {"source": obj.get("source"), "object_type": obj.get("type"), "object": obj.get("object")},
                {k: num(v) for k, v in (obj.get("values") or {}).items()},
            )
        if counters.get("truncated"):
            self.obs.errors.setdefault("counters", "COUNTERS_DB tronquée (counters_max_keys)")

    def redis(self) -> None:
        info = self.data.get("redis") or {}
        fields: dict[str, Any] = {}
        for key, value in info.items():
            if isinstance(value, dict):
                for sub, v in value.items():
                    if num(v) is not None and not isinstance(v, str):
                        fields[f"{key}_{sub}"] = v
            elif not isinstance(value, str) and num(value) is not None:
                fields[key] = value
        if fields:
            self.add("lab_sonic_redis", {}, fields)

    def lldp(self) -> None:
        for port, e in sorted((self.data.get("lldp") or {}).items()):
            self.add(
                "lab_sonic_lldp",
                {
                    "local_port": port,
                    "remote_system": e.get("lldp_rem_sys_name"),
                    "remote_port": e.get("lldp_rem_port_desc") or e.get("lldp_rem_port_id"),
                },
                {
                    "present": True,
                    "remote_mgmt_ip": e.get("lldp_rem_man_addr") or "",
                    "remote_port_id": e.get("lldp_rem_port_id") or "",
                    "remote_chassis_id": e.get("lldp_rem_chassis_id") or "",
                    "remote_description": e.get("lldp_rem_sys_desc") or "",
                    "remote_capabilities": e.get("lldp_rem_sys_cap_enabled") or "",
                },
            )

    def syslog(self) -> None:
        log = self.data.get("syslog")
        if not log:
            return
        self.obs.has_syslog = True
        self.st.syslog = {"inode": log.get("inode"), "offset": log.get("end", 0)}
        if log.get("reset"):
            self.st.syslog_totals = {}
        for key, count in (log.get("counts") or {}).items():
            program, _, severity = key.partition("\t")
            k = (program, severity)
            self.st.syslog_totals[k] = self.st.syslog_totals.get(k, 0) + count
        for (program, severity), total in sorted(self.st.syslog_totals.items()):
            self.add("lab_syslog", {"program": program, "severity": severity}, {"lines_total": total})
        for ev in log.get("events") or []:
            self.add(
                "lab_event",
                {"program": ev.get("program"), "severity": ev.get("severity"), "type": event_type(ev.get("program") or "", ev.get("message") or "")},
                {"message": (ev.get("message") or "").lstrip(":- ")},
                ts=ns_us(ev["ts"]),
            )


def build(obs: NodeObs, st: NodeState, data: dict[str, Any], ts_ns: int) -> NodeObs:
    """Remplit obs (points + structures communes) à partir de la sortie JSON du collecteur SONiC."""
    obs.clock = data.get("uptime")
    obs.netdev = data.get("netdev") or {}
    obs.addresses = data.get("addresses") or {}
    obs.errors.update(data.get("errors") or {})
    conv = SonicConverter(obs, st, data, ts_ns)
    for section in ("meta", "ports", "bgp", "routes", "neighbors", "services", "db", "counters", "redis", "lldp", "syslog"):
        try:
            getattr(conv, section)()
        except Exception as exc:  # noqa: BLE001 - une section mal formée ne bloque pas les autres
            obs.errors[f"convert:{section}"] = f"{type(exc).__name__}: {exc}"
    return obs
