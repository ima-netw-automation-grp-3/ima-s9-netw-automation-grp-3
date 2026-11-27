"""Captures de paquets : sonde `dumpcap` dans le réseau d'un nœud + visionneuse Wireshark dans le navigateur.

Chaque capture est faite de deux conteneurs auxiliaires, créés à la demande (image Wireshark de LinuxServer) :
  - la sonde partage l'espace réseau du nœud (`network_mode: container:<nœud>`) et écrit un fichier pcap dans
    le volume `captures`, en continu ;
  - la visionneuse (Wireshark dans un bureau web, Selkies) lit ce fichier en direct. Elle n'a aucun port publié
    et reste sur le réseau compose par défaut : l'interface la sert sous /wireshark/<id>/ (voir api/wireshark.py), donc
    derrière le contrôle d'hôte/origine et l'authentification de l'interface, et les jobs CI ne l'atteignent pas.
L'état vit dans Docker (labels des conteneurs) : un redémarrage de l'interface ne perd aucune capture.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import BadRequest, NotFound
from .nodes.base import parse_docker_time
from .settings import Settings

if TYPE_CHECKING:
    from .docker_api import DockerAPI
    from .lab import LabService

LABEL = "sonic-lab.capture"
SIDECAR_PORT = 3001
PROBE_SETTLE = 1.0  # secondes
VIEWER_PROBE_TIMEOUT = 0.5  # secondes, test de connexion au port de la visionneuse
VIEWER_USER = "lab"

_IFACE_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,15}$")
_ID_RE = re.compile(r"^[0-9a-f]{8}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
# Filtre de capture (syntaxe pcap) : transmis en argument à dumpcap, jamais à un shell ; on borne seulement la forme.
_FILTER_RE = re.compile(r"^[A-Za-z0-9 .:/!&|()<>=\[\]_-]{0,200}$")

# dumpcap crée son fichier en 0600 : on le crée d'abord en 0644 pour que l'utilisateur non privilégié
# de la visionneuse puisse le lire (dumpcap conserve les droits d'un fichier existant).
_PROBE_ENTRYPOINT = [
    "sh",
    "-c",
    'f="/captures/$CAPTURE_FILE"; : > "$f"; chmod 644 "$f"; exec dumpcap "$@"',
    "dumpcap",
]


@dataclass(frozen=True)
class Capture:
    id: str
    node: str
    interface: str
    capture_filter: str
    filename: str
    created: float
    state: str  # running | stopped
    ended: float | None  # fin de la sonde, si arrêtée
    viewer: str  # starting | ready | stopped
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "node": self.node,
            "interface": self.interface,
            "filter": self.capture_filter,
            "filename": self.filename,
            "created": self.created,
            "state": self.state,
            "ended": self.ended,
            "viewer": self.viewer,
            "viewer_ready": self.viewer == "ready",
            "size": self.size,
            "url": f"/wireshark/{self.id}/",
            "download": f"/api/captures/{self.id}/download",
        }


def _iface_key(name: str) -> tuple:
    """Ordre naturel (Ethernet4 avant Ethernet12) : fils containerlab ethN, ports SONiC, autres, lo à la fin."""
    group = 3 if name == "lo" else 0 if re.fullmatch(r"eth\d+", name) else 1 if name.startswith("Ethernet") else 2
    parts = re.split(r"(\d+)", name)
    return (group, [int(p) if p.isdigit() else p.lower() for p in parts])


def check_filter(value: str) -> str:
    value = value.strip()
    if not _FILTER_RE.match(value):
        raise BadRequest("filtre de capture invalide (caractères autorisés : lettres, chiffres et . : / ! & | ( ) < > = [ ] _ -)")
    return value


class CaptureManager:
    def __init__(self, settings: Settings, docker: DockerAPI, lab: LabService) -> None:
        self.settings = settings
        self.docker = docker
        self.lab = lab
        self._passwords: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._listening: set[str] = set()  # visionneuses dont le serveur web répond déjà
        self._ended: dict[str, float | None] = {}  # fin des sondes arrêtées (immuable)

    # -- noms ---------------------------------------------------------------------------------------
    def probe_name(self, cid: str) -> str:
        return f"{self.settings.lab_name}-capture-{cid}"

    def viewer_name(self, cid: str) -> str:
        return f"{self.settings.lab_name}-wireshark-{cid}"

    def file_path(self, filename: str) -> Path:
        if Path(filename).name != filename or not _FILENAME_RE.match(filename):
            raise BadRequest("nom de fichier invalide")
        return self.settings.capture_dir / filename

    # -- lecture ------------------------------------------------------------------------------------
    async def interfaces(self, node: str) -> list[str]:
        running = await self.lab.running_node(node)
        res = await self.docker.exec(running.container, ["ls", "/sys/class/net"], timeout=10)
        names = [n for n in res.stdout.split() if _IFACE_RE.match(n)]
        return sorted(names, key=_iface_key)

    async def sessions(self) -> list[Capture]:
        probes = await self.docker.list_containers({f"{LABEL}.role": "probe"})
        viewers = {
            c["Labels"].get(LABEL): c for c in await self.docker.list_containers({f"{LABEL}.role": "viewer"})
        }
        probes = [c for c in probes if _ID_RE.match((c.get("Labels") or {}).get(LABEL, ""))]
        ids = [c["Labels"][LABEL] for c in probes]
        running = {cid for cid in ids if (viewers.get(cid) or {}).get("State") == "running"}
        await asyncio.gather(*(self._check_viewer(cid) for cid in running - self._listening))
        await asyncio.gather(
            *(self._probe_end(cid) for c, cid in zip(probes, ids) if c.get("State") != "running" and cid not in self._ended)
        )
        out = []
        for c, cid in zip(probes, ids):
            labels = c["Labels"]
            filename = labels.get(f"{LABEL}.file", "")
            try:
                size = self.file_path(filename).stat().st_size
            except (OSError, BadRequest):
                size = 0
            stopped = c.get("State") != "running"
            if cid not in running:
                viewer = "stopped"
            else:
                viewer = "ready" if cid in self._listening else "starting"
            out.append(
                Capture(
                    id=cid,
                    node=labels.get(f"{LABEL}.node", ""),
                    interface=labels.get(f"{LABEL}.interface", ""),
                    capture_filter=labels.get(f"{LABEL}.filter", ""),
                    filename=filename,
                    created=float(labels.get(f"{LABEL}.created") or 0),
                    state="stopped" if stopped else "running",
                    ended=self._ended.get(cid) if stopped else None,
                    viewer=viewer,
                    size=size,
                )
            )
        return sorted(out, key=lambda x: x.created, reverse=True)

    async def _check_viewer(self, cid: str) -> None:
        """Le conteneur tourne avant que son serveur web écoute : on attend que le port réponde."""
        if await self.viewer_listening(self.viewer_name(cid), SIDECAR_PORT):
            self._listening.add(cid)

    @staticmethod
    async def viewer_listening(host: str, port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), VIEWER_PROBE_TIMEOUT)
        except (OSError, TimeoutError):
            return False
        writer.close()
        return True

    async def _probe_end(self, cid: str) -> None:
        info = await self.docker.inspect(self.probe_name(cid))
        self._ended[cid] = parse_docker_time(((info or {}).get("State") or {}).get("FinishedAt"))

    async def get(self, cid: str) -> Capture:
        for c in await self.sessions():
            if c.id == cid:
                return c
        raise NotFound(f"capture inconnue : {cid}")

    async def viewer_password(self, cid: str) -> str | None:
        """Mot de passe de la visionneuse (injecté par le proxy, jamais montré au navigateur)."""
        if cid in self._passwords:
            return self._passwords[cid]
        info = await self.docker.inspect(self.viewer_name(cid))
        for item in ((info or {}).get("Config") or {}).get("Env") or []:
            if item.startswith("PASSWORD="):
                self._passwords[cid] = item.split("=", 1)[1]
                return self._passwords[cid]
        return None

    # -- cycle de vie -------------------------------------------------------------------------------
    async def start(self, node: str, interface: str, capture_filter: str = "") -> Capture:
        if not _IFACE_RE.match(interface):
            raise BadRequest(f"interface invalide : {interface!r}")
        capture_filter = check_filter(capture_filter)
        async with self._lock:
            if len(await self.sessions()) >= self.settings.capture_max_sessions:
                raise BadRequest(
                    f"{self.settings.capture_max_sessions} captures au maximum : supprimez-en une avant d'en lancer une autre"
                )
            running = await self.lab.running_node(node)
            if interface not in await self.interfaces(node):
                raise BadRequest(f"{node} n'a pas d'interface {interface!r}")
            await self.docker.ensure_image(self.settings.capture_image)

            cid = secrets.token_hex(4)
            created = time.time()
            filename = f"{node}-{interface}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(created))}-{cid}.pcap"
            self.file_path(filename)  # nom sûr : il est repris par le script de la visionneuse
            labels = {
                LABEL: cid,
                f"{LABEL}.node": node,
                f"{LABEL}.interface": interface,
                f"{LABEL}.filter": capture_filter,
                f"{LABEL}.file": filename,
                f"{LABEL}.created": f"{created:.0f}",
            }
            try:
                await self._start_probe(cid, running.container, interface, capture_filter, filename, labels)
                await self._start_viewer(cid, filename, labels)
            except Exception:
                await self._remove(cid)
                raise
        return await self.get(cid)

    async def _start_probe(
        self, cid: str, container: str, interface: str, capture_filter: str, filename: str, labels: dict[str, str]
    ) -> None:
        args = ["-q", "-i", interface, "-F", "pcap", "-w", f"/captures/{filename}", "-a", f"filesize:{self.settings.capture_max_mb * 1024}"]
        if capture_filter:
            args += ["-f", capture_filter]
        name = self.probe_name(cid)
        await self.docker.create_container(
            name,
            {
                "Image": self.settings.capture_image,
                "User": "0",
                "Env": [f"CAPTURE_FILE={filename}"],
                "Entrypoint": _PROBE_ENTRYPOINT,
                "Cmd": args,
                "Labels": {**labels, f"{LABEL}.role": "probe"},
                "HostConfig": {
                    "NetworkMode": f"container:{container}",
                    "CapAdd": ["NET_ADMIN", "NET_RAW"],
                    "Binds": [f"{self.settings.capture_volume}:/captures"],
                },
            },
        )
        await self.docker.start_container(name)
        await asyncio.sleep(PROBE_SETTLE)  # un filtre ou une interface refusés par dumpcap le terminent aussitôt
        info = await self.docker.inspect(name)
        if info and (info.get("State") or {}).get("Status") != "running":
            logs = (await self.docker.logs(name, tail=20)).strip()
            raise BadRequest(f"dumpcap s'est arrêté : {logs or 'raison inconnue'}")

    async def _start_viewer(self, cid: str, filename: str, labels: dict[str, str]) -> None:
        # hexadécimal : l'image le passe tel quel à `openssl passwd -apr1 "$PASSWORD"`, qui prend un mot de
        # passe commençant par « - » pour une option (htpasswd vide → 401 permanent, le navigateur demande
        # alors un identifiant) ; token_urlsafe le faisait une fois sur 64.
        password = secrets.token_hex(24)
        name = self.viewer_name(cid)
        autostart = f"tail -F -c +1 /captures/{filename} | wireshark -k -i - &\n"
        await self.docker.create_container(
            name,
            {
                "Image": self.settings.capture_image,
                "Env": [
                    f"CUSTOM_USER={VIEWER_USER}",
                    f"PASSWORD={password}",
                    f"SUBFOLDER=/wireshark/{cid}/",
                    "TITLE=Wireshark",
                    "NO_GAMEPAD=true",
                    "PIXELFLUX_WAYLAND=false",
                ],
                "Labels": {**labels, f"{LABEL}.role": "viewer"},
                "HostConfig": {
                    "NetworkMode": await self._network(),
                    # dumpcap (lancé par Wireshark pour lire l'entrée standard) porte des capacités de fichier
                    "CapAdd": ["NET_ADMIN", "NET_RAW"],
                    "ShmSize": 1024 * 1024 * 1024,
                    "Binds": [f"{self.settings.capture_volume}:/captures:ro"],
                },
            },
        )
        self._passwords[cid] = password
        await self.docker.put_files(name, {"autostart": autostart.encode()}, dest="/defaults")
        await self.docker.start_container(name)

    async def _network(self) -> str:
        if self.settings.capture_network:
            return self.settings.capture_network
        info = await self.docker.inspect(os.environ.get("HOSTNAME", ""))
        networks = list(((info or {}).get("NetworkSettings") or {}).get("Networks") or {})
        return networks[0] if networks else f"{self.settings.compose_project}_default"

    async def stop(self, cid: str) -> Capture:
        capture = await self.get(cid)
        await self.docker.stop_container(self.probe_name(capture.id))
        return await self.get(cid)

    async def _remove(self, cid: str) -> None:
        await self.docker.remove_container(self.viewer_name(cid))
        await self.docker.remove_container(self.probe_name(cid))
        self._passwords.pop(cid, None)
        self._listening.discard(cid)
        self._ended.pop(cid, None)

    async def delete(self, cid: str, *, delete_file: bool = True) -> None:
        capture = await self.get(cid)
        await self._remove(cid)
        if delete_file:
            self.file_path(capture.filename).unlink(missing_ok=True)
