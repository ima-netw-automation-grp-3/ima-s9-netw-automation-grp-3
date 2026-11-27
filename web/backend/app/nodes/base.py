"""Fonctions communes aux nœuds du lab."""

from __future__ import annotations

import ipaddress
import re
import shlex
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..docker_api import DockerAPI, ExecResult
from ..errors import BadRequest
from ..parsers import parse_ping

if TYPE_CHECKING:
    from ..settings import Settings
    from ..store import Store
    from ..topology import NodeDef

# Cible de ping/traceroute : adresse IP ou nom d'hôte simple (pas d'injection d'options).
_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:_-]{0,252}$")


def check_target(target: str) -> str:
    target = target.strip()
    if not _TARGET_RE.match(target):
        raise BadRequest(f"cible invalide : {target!r}")
    return target


def parse_started_at(info: dict[str, Any] | None) -> float | None:
    """Horodatage (epoch) de démarrage du conteneur à partir de docker inspect."""
    if not info:
        return None
    return parse_docker_time((info.get("State") or {}).get("StartedAt"))


def parse_docker_time(raw: str | None) -> float | None:
    """Horodatage (epoch) d'une date de docker inspect (`StartedAt`, `FinishedAt`) ; None si absente."""
    if not raw or raw.startswith("0001"):
        return None
    # Docker renvoie des nanosecondes : on tronque aux microsecondes pour fromisoformat
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", raw)
    if not m:
        return None
    frac = (m.group(2) or ".0")[:7]
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(f"{m.group(1)}{frac}{tz}").timestamp()


def ipv4_addresses(addresses: list[str], exclude: str | None = None) -> list[str]:
    """Adresses IPv4 (CIDR) hors réseau de management."""
    net = ipaddress.ip_network(exclude, strict=False) if exclude else None
    out = []
    for a in addresses:
        try:
            iface = ipaddress.ip_interface(a)
        except ValueError:
            continue
        if iface.version != 4 or (net is not None and iface.ip in net):
            continue
        out.append(str(iface))
    return out


class BaseNode:
    kind = "linux"
    role = "client"
    shell_cmd = ["sh", "-c"]
    shells: dict[str, list[str]] = {"sh": ["sh", "-l"]}

    def __init__(
        self,
        node: NodeDef,
        container: str,
        docker: DockerAPI,
        settings: Settings,
        store: Store,
        mgmt_subnet: str | None = None,
    ) -> None:
        self.node = node
        self.name = node.name
        self.container = container
        self.docker = docker
        self.settings = settings
        self.store = store
        self.mgmt_subnet = mgmt_subnet

    # -- exécution ---------------------------------------------------------------------------
    async def exec(self, argv: list[str], *, timeout: float | None = None) -> ExecResult:
        return await self.docker.exec(self.container, argv, timeout=timeout or self.settings.exec_timeout)

    async def run(self, command: str, *, timeout: float | None = None) -> ExecResult:
        """Commande shell libre (onglet « Commandes » de l'interface)."""
        if not command.strip():
            raise BadRequest("commande vide")
        return await self.exec([*self.shell_cmd, command], timeout=timeout)

    async def upload(self, content: str | bytes, suffix: str) -> str:
        """Dépose un fichier temporaire dans le conteneur et renvoie son chemin."""
        name = f"labweb-{uuid.uuid4().hex[:12]}{suffix}"
        data = content.encode("utf-8") if isinstance(content, str) else content
        await self.docker.put_files(self.container, {name: data}, dest="/tmp")
        return f"/tmp/{name}"

    async def cleanup(self, *paths: str) -> None:
        if paths:
            await self.exec(["rm", "-f", *paths], timeout=10)

    def shell_argv(self, shell: str | None) -> list[str]:
        if not shell:
            shell = next(iter(self.shells))
        if shell not in self.shells:
            raise BadRequest(f"shell inconnu pour {self.name} : {shell!r} ({', '.join(self.shells)})")
        return self.shells[shell]

    # -- diagnostics -------------------------------------------------------------------------
    async def ping(self, target: str, count: int = 3) -> dict[str, Any]:
        target = check_target(target)
        count = min(max(int(count), 1), 20)
        res = await self.exec(["ping", "-c", str(count), "-w", str(count + 2), target], timeout=count + 10)
        return {"command": f"ping -c {count} {target}", **res.to_dict(), "summary": parse_ping(res.output)}

    async def traceroute(self, target: str) -> dict[str, Any]:
        raise BadRequest(f"traceroute n'est pas disponible sur {self.name}")

    async def logs(self, lines: int = 200) -> str:
        return await self.docker.logs(self.container, tail=lines)

    async def status(self, *, summary: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.node.kind,
            "role": self.role,
            "image": self.node.image,
            "container": self.container,
            "shells": list(self.shells),
        }

    @staticmethod
    def quote(argv: list[str]) -> str:
        return " ".join(shlex.quote(a) for a in argv)

    @staticmethod
    def now() -> float:
        return time.time()
