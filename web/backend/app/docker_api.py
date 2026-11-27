"""Accès à Docker (aiodocker) : liste des conteneurs, docker exec, copie de fichiers, TTY."""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import time
from dataclasses import dataclass
from typing import Any

import aiodocker
from aiodocker.exceptions import DockerError

from .errors import NodeUnavailable, OperationFailed

# Code de sortie de `timeout` (coreutils et busybox) quand la commande a été interrompue.
TIMEOUT_EXIT_CODE = 124


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """stdout et stderr concaténés (pour l'affichage)."""
        if self.stdout and self.stderr:
            return self.stdout.rstrip("\n") + "\n" + self.stderr
        return self.stdout or self.stderr

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration": round(self.duration, 3),
        }


class TtySession:
    """Session interactive (docker exec -it) utilisée par le terminal web."""

    def __init__(self, exec_: Any, stream: Any) -> None:
        self._exec = exec_
        self._stream = stream

    async def read(self) -> bytes | None:
        msg = await self._stream.read_out()
        return None if msg is None else bytes(msg.data)

    async def write(self, data: bytes) -> None:
        await self._stream.write_in(data)

    async def resize(self, cols: int, rows: int) -> None:
        try:
            await self._exec.resize(h=max(rows, 1), w=max(cols, 1))
        except DockerError:
            pass  # le processus peut déjà être terminé

    async def close(self) -> None:
        try:
            await self._stream.close()
        except Exception:  # noqa: BLE001 - fermeture best effort
            pass

    async def exit_code(self) -> int | None:
        try:
            info = await self._exec.inspect()
        except DockerError:
            return None
        return info.get("ExitCode")


class DockerAPI:
    """Fine surcouche d'aiodocker, facile à remplacer par un faux dans les tests."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url
        self._docker: aiodocker.Docker | None = None

    async def start(self) -> None:
        if self._docker is None:
            self._docker = aiodocker.Docker(url=self._url)

    async def close(self) -> None:
        if self._docker is not None:
            await self._docker.close()
            self._docker = None

    @property
    def docker(self) -> aiodocker.Docker:
        if self._docker is None:
            raise RuntimeError("DockerAPI non démarrée")
        return self._docker

    async def list_containers(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        """Conteneurs (même arrêtés) portant tous les labels donnés, au format /containers/json."""
        filters = {"label": [f"{k}={v}" for k, v in labels.items()]}
        containers = await self.docker.containers.list(all="true", filters=json.dumps(filters))
        return [c._container for c in containers]  # noqa: SLF001 - données brutes de l'API

    async def inspect(self, name: str) -> dict[str, Any] | None:
        try:
            container = await self.docker.containers.get(name)
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise
        return container._container  # noqa: SLF001

    async def exec(
        self,
        name: str,
        argv: list[str],
        *,
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Exécute argv dans le conteneur (sans TTY) et renvoie code de sortie + sorties.

        La commande est encapsulée dans `timeout` côté conteneur pour qu'un processus bloqué ne
        survive pas à la requête ; un délai de garde côté client complète le dispositif.
        """
        secs = max(int(timeout), 1)
        full = ["timeout", "-k", "3", str(secs), *argv]
        container = self.docker.containers.container(name)
        started = time.monotonic()
        try:
            exec_ = await container.exec(full, stdout=True, stderr=True, environment=env)
        except DockerError as exc:
            raise self._unavailable(name, exc) from exc

        out = bytearray()
        err = bytearray()

        async def _pump() -> None:
            async with exec_.start(detach=False) as stream:
                while True:
                    msg = await stream.read_out()
                    if msg is None:
                        break
                    (err if msg.stream == 2 else out).extend(msg.data)

        timed_out = False
        try:
            await asyncio.wait_for(_pump(), timeout=secs + 10)
        except TimeoutError:
            timed_out = True
        info = await exec_.inspect()
        code = info.get("ExitCode")
        if code is None:
            code = -1
        if code == TIMEOUT_EXIT_CODE:
            timed_out = True
        return ExecResult(
            exit_code=code,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            timed_out=timed_out,
            duration=time.monotonic() - started,
        )

    async def put_files(self, name: str, files: dict[str, bytes], dest: str = "/tmp") -> None:
        """Copie des fichiers (chemins relatifs à dest) dans le conteneur."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for rel, data in files.items():
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mtime = int(time.time())
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        container = self.docker.containers.container(name)
        try:
            await container.put_archive(dest, buf.getvalue())
        except DockerError as exc:
            raise self._unavailable(name, exc) from exc

    async def logs(self, name: str, tail: int = 200) -> str:
        container = self.docker.containers.container(name)
        try:
            lines = await container.log(stdout=True, stderr=True, tail=str(tail))
        except DockerError as exc:
            raise self._unavailable(name, exc) from exc
        return "".join(lines)

    async def restart(self, name: str, stop_timeout: int = 180) -> None:
        # Délai borné par asyncio.timeout : aiodocker ignore un `timeout=` entier.
        container = self.docker.containers.container(name)
        try:
            async with asyncio.timeout(stop_timeout + 60):
                await container.restart(t=stop_timeout)
        except TimeoutError as exc:
            raise OperationFailed(f"redémarrage de {name} : Docker muet après {stop_timeout + 60} s") from exc
        except DockerError as exc:
            raise OperationFailed(f"redémarrage de {name} impossible : {exc.message}") from exc

    async def open_tty(
        self,
        name: str,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        cols: int = 120,
        rows: int = 32,
    ) -> TtySession:
        container = self.docker.containers.container(name)
        try:
            exec_ = await container.exec(argv, stdin=True, tty=True, environment=env)
            stream = exec_.start(detach=False)
            await stream.__aenter__()
        except DockerError as exc:
            raise self._unavailable(name, exc) from exc
        session = TtySession(exec_, stream)
        await session.resize(cols, rows)
        return session

    # -- conteneurs auxiliaires (captures de paquets) -----------------------------------------------
    async def ensure_image(self, image: str) -> None:
        """Télécharge l'image si elle est absente (peut durer plusieurs minutes la première fois)."""
        try:
            await self.docker.images.inspect(image)
            return
        except DockerError as exc:
            if exc.status != 404:
                raise OperationFailed(f"docker : {exc.message}") from exc
        repo, _, tag = image.rpartition(":")
        if not repo or "/" in tag:
            repo, tag = image, "latest"
        try:
            await self.docker.images.pull(repo, tag=tag)
        except DockerError as exc:
            raise OperationFailed(f"téléchargement de l'image {image} impossible : {exc.message}") from exc

    async def create_container(self, name: str, config: dict[str, Any]) -> None:
        try:
            await self.docker.containers.create(config, name=name)
        except DockerError as exc:
            raise OperationFailed(f"création du conteneur {name} impossible : {exc.message}") from exc

    async def start_container(self, name: str) -> None:
        try:
            await self.docker.containers.container(name).start()
        except DockerError as exc:
            raise OperationFailed(f"démarrage du conteneur {name} impossible : {exc.message}") from exc

    async def stop_container(self, name: str, timeout: int = 5) -> None:
        """SIGTERM, puis SIGKILL après `timeout` secondes ; rend la main une fois le conteneur arrêté."""
        try:
            async with asyncio.timeout(timeout + 60):
                await self.docker.containers.container(name).stop(t=timeout)
        except TimeoutError as exc:
            raise OperationFailed(f"arrêt du conteneur {name} : Docker muet après {timeout + 60} s") from exc
        except DockerError as exc:
            if exc.status not in (304, 404):
                raise OperationFailed(f"arrêt du conteneur {name} impossible : {exc.message}") from exc

    async def remove_container(self, name: str) -> None:
        try:
            await self.docker.containers.container(name).delete(force=True)
        except DockerError as exc:
            if exc.status != 404:
                raise OperationFailed(f"suppression du conteneur {name} impossible : {exc.message}") from exc

    @staticmethod
    def _unavailable(name: str, exc: DockerError) -> NodeUnavailable:
        if exc.status == 404:
            return NodeUnavailable(f"le conteneur {name} n'existe pas (lab non déployé ?)")
        if exc.status == 409:
            return NodeUnavailable(f"le conteneur {name} n'est pas démarré")
        return NodeUnavailable(f"docker : {exc.message}")
