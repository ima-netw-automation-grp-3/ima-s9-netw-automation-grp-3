"""Nœud Linux (clients Alpine du lab)."""

from __future__ import annotations

from typing import Any

from ..parsers import parse_ip_addr, parse_ip_link, parse_ip_route, parse_ping, parse_proc_net_dev, split_sections
from .base import BaseNode, check_target, ipv4_addresses

_MARK = "====="
_STATUS_SCRIPT = f"ip -o link; echo {_MARK}; ip -o addr; echo {_MARK}; ip route; echo {_MARK}; cat /proc/net/dev"


class LinuxNode(BaseNode):
    kind = "linux"
    role = "client"
    shell_cmd = ["sh", "-c"]
    shells = {"sh": ["sh", "-l"]}

    async def status(self, *, summary: bool = False) -> dict[str, Any]:
        res = await self.exec(["sh", "-c", _STATUS_SCRIPT], timeout=15)
        if not res.ok:
            return {"phase": "error", "detail": res.output.strip() or "collecte impossible", "errors": {}}
        parts = split_sections(res.stdout, _MARK)
        parts += [""] * (4 - len(parts))
        links = parse_ip_link(parts[0])
        addrs = parse_ip_addr(parts[1])
        counters = parse_proc_net_dev(parts[3])
        interfaces = {}
        for name, link in links.items():
            if name == "lo":
                continue
            interfaces[name] = {
                "operstate": link["state"],
                "up": link["up"] and link["lower_up"],
                "admin_up": link["up"],
                "mtu": link["mtu"],
                "mac": link["mac"],
                "addresses": [a["address"] for a in addrs.get(name, [])],
                "stats": counters.get(name, {}),
            }
        data_ifaces = [n for n in interfaces if n != "eth0"]
        configured = any(ipv4_addresses(interfaces[n]["addresses"], self.mgmt_subnet) for n in data_ifaces)
        return {
            "phase": "ready" if configured else "bootstrap",
            "detail": "" if configured else "aucune adresse IPv4 sur les interfaces du lab",
            "interfaces": interfaces,
            "routes": parse_ip_route(parts[2]),
            "errors": {},
        }

    async def traceroute(self, target: str) -> dict[str, Any]:
        target = check_target(target)
        res = await self.exec(["traceroute", "-n", "-w", "2", "-q", "1", "-m", "15", target], timeout=45)
        return {"command": f"traceroute -n {target}", **res.to_dict()}

    async def ping(self, target: str, count: int = 3) -> dict[str, Any]:
        # busybox ping : -W = délai d'attente par réponse
        target = check_target(target)
        count = min(max(int(count), 1), 20)
        res = await self.exec(
            ["ping", "-c", str(count), "-W", "1", "-w", str(count + 2), target], timeout=count + 10
        )
        return {"command": f"ping -c {count} {target}", **res.to_dict(), "summary": parse_ping(res.output)}
