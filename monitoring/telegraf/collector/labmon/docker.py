"""Accès à Docker par la CLI : découverte des conteneurs du lab et docker exec avec délai."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class DockerError(RuntimeError):
    pass


@dataclass
class Container:
    id: str
    name: str
    node: str
    kind: str
    state: str
    image: str | None = None
    started_at: float | None = None
    mgmt_ipv4: str | None = None
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


def parse_started_at(raw: str | None) -> float | None:
    """StartedAt de docker inspect (nanosecondes, Z) -> epoch ; None si le conteneur n'a jamais démarré."""
    if not raw or raw.startswith("0001"):
        return None
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", raw)
    if not m:
        return None
    frac = (m.group(2) or ".0")[:7]
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    return datetime.fromisoformat(f"{m.group(1)}{frac}{tz}").timestamp()


def parse_inspect(data: list[dict[str, Any]], mgmt_network: str | None) -> list[Container]:
    """Sortie JSON de docker inspect -> conteneurs portant les labels containerlab."""
    out = []
    for c in data:
        labels = (c.get("Config") or {}).get("Labels") or {}
        node = labels.get("clab-node-name")
        if not node:
            continue
        nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
        net = nets.get(mgmt_network) if mgmt_network else None
        if net is None and nets:
            net = next(iter(nets.values()))
        state = c.get("State") or {}
        out.append(
            Container(
                id=c.get("Id", ""),
                name=(c.get("Name") or "").lstrip("/"),
                node=node,
                kind=labels.get("clab-node-kind", ""),
                state=state.get("Status", "unknown"),
                image=(c.get("Config") or {}).get("Image"),
                started_at=parse_started_at(state.get("StartedAt")) if state.get("Status") == "running" else None,
                mgmt_ipv4=(net or {}).get("IPAddress") or None,
                labels=labels,
            )
        )
    return out


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise DockerError("CLI docker introuvable") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerError(f"délai dépassé : {' '.join(argv[:3])}") from exc


def list_containers(lab_name: str, mgmt_network: str | None, timeout: float = 15.0) -> list[Container]:
    """Conteneurs du lab (même arrêtés), d'après le label containerlab=<lab>."""
    ps = _run(["docker", "ps", "-aq", "--no-trunc", "--filter", f"label=containerlab={lab_name}"], timeout)
    if ps.returncode != 0:
        raise DockerError(ps.stderr.strip()[:300] or "docker ps a échoué")
    ids = ps.stdout.split()
    if not ids:
        return []
    insp = _run(["docker", "inspect", *ids], timeout)
    if insp.returncode != 0 and not insp.stdout.strip():
        raise DockerError(insp.stderr.strip()[:300] or "docker inspect a échoué")
    return parse_inspect(json.loads(insp.stdout or "[]"), mgmt_network)


def exec_in(
    container: str,
    argv: list[str],
    timeout: float,
    *,
    in_container_timeout: bool = True,
    stdin: str | None = None,
) -> ExecResult:
    """docker exec sans TTY.

    Avec in_container_timeout, la commande est encadrée par `timeout -k 3` dans le conteneur, pour qu'un
    processus bloqué ne survive pas (comme web/backend/app/docker_api.py). À éviter sur les nœuds
    busybox dont le PID 1 est un shell : le `timeout` de busybox laisse alors un processus zombie à
    chaque appel. Le délai côté client s'applique dans tous les cas.
    """
    secs = max(int(timeout), 1)
    cmd = ["docker", "exec"]
    if stdin is not None:
        cmd.append("-i")
    cmd.append(container)
    if in_container_timeout:
        cmd += ["timeout", "-k", "3", str(secs)]
    cmd += argv
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=secs + 10, input=stdin, check=False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return ExecResult(-1, out, "délai dépassé", timed_out=True)
    except FileNotFoundError as exc:
        raise DockerError("CLI docker introuvable") from exc
    return ExecResult(proc.returncode, proc.stdout, proc.stderr, timed_out=proc.returncode == 124)


def exec_detached(container: str, argv: list[str]) -> None:
    """docker exec -d : processus lancé en arrière-plan, rattaché au runtime et non au PID 1 du conteneur."""
    proc = _run(["docker", "exec", "-d", container, *argv], 15)
    if proc.returncode != 0:
        raise DockerError(proc.stderr.strip()[:300] or f"docker exec -d {container} a échoué")
