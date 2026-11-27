"""Configuration et état FRR (vtysh), ping : données du module sonic-lab. S'exécute dans le routeur.

Le replace FRR et l'analyse de ping réutilisent les modules purs de l'interface web
(web/backend/app/frr_config.py et parsers.py), montés à côté de ce paquet par containerlab : les
deux chemins (interface web et RESTCONF) appliquent donc exactement la même logique.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

import frr_config
from parsers import parse_ping

from . import labmodule
from .store import InvalidData, OperationFailed

# vtysh affiche « [pid|démon] done » pour chaque démon : bruit sans intérêt
_NOISE = ("done", "sending configuration", "Waiting for children")


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OperationFailed(f"{cmd[0]} : pas de réponse en {timeout:.0f} s") from None


class Frr:
    def running_raw(self) -> str:
        res = _run(["vtysh", "-c", "show running-config"], 30)
        if res.returncode != 0:
            raise OperationFailed(f"lecture de la conf FRR impossible : {(res.stdout + res.stderr).strip()[:400]}")
        return res.stdout

    def running(self) -> str:
        return frr_config.normalize(self.running_raw())

    def neighbors(self) -> list[dict[str, Any]]:
        res = _run(["vtysh", "-c", "show bgp summary json"], 30)
        if res.returncode != 0:
            raise OperationFailed(f"show bgp summary json : {(res.stdout + res.stderr).strip()[:400]}")
        try:
            data = json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            raise OperationFailed("show bgp summary json : sortie illisible") from None
        return labmodule.bgp_neighbors(data if isinstance(data, dict) else {})

    @staticmethod
    def _file(content: str) -> str:
        fd, path = tempfile.mkstemp(prefix="restconf-frr-", suffix=".conf")
        with os.fdopen(fd, "w") as f:
            f.write(content if content.endswith("\n") else content + "\n")
        os.chmod(path, 0o644)  # lisible par vtysh quel que soit l'utilisateur des démons
        return path

    def validate(self, text: str) -> str:
        """Erreurs de `vtysh -C` ("" si la configuration est valide)."""
        path = self._file(text)
        try:
            res = _run(["vtysh", "-C", "-f", path], 30)
        finally:
            os.unlink(path)
        return "" if res.returncode == 0 else frr_config.clean_vtysh_error(res.stdout + res.stderr)

    def preview(self, text: str, mode: str) -> dict[str, Any]:
        errors = self.validate(text)
        raw = self.running_raw()
        out: dict[str, Any] = {"valid": not errors}
        if errors:
            out["errors"] = errors
        if mode == "replace":
            plan = frr_config.replace_plan(raw, text)
            out["diff"] = frr_config.unified_diff(raw, text, before_name="running", after_name="après")
            out["commands"] = "\n".join(plan.commands) + ("\n" if plan.commands else "")
            if plan.resets:
                out["resets"] = plan.resets
        else:
            merged = raw + "\n" + frr_config.merge_additions(raw, text)
            out["diff"] = frr_config.unified_diff(raw, merged, before_name="running", after_name="après")
        return out

    def apply(self, text: str, mode: str) -> dict[str, Any]:
        """Même séquence que l'interface web (SonicNode._push_frr) : validation, retraits, merge."""
        errors = self.validate(text)
        if errors:
            raise InvalidData(f"configuration FRR invalide : {errors}")
        steps: list[dict[str, Any]] = []
        resets: list[str] = []
        if mode == "replace":
            plan = frr_config.replace_plan(self.running_raw(), text)
            resets = plan.resets
            if plan.commands:
                steps.append(self._vtysh_file("retrait", "\n".join(plan.commands) + "\n"))
        steps.append(self._vtysh_file("application", text))
        failed = [s for s in steps if s["exit_code"] != 0]
        if failed:
            raise OperationFailed("vtysh : échec de l'étape « {} » : {}".format(failed[0]["step"], failed[0]["output"][:800]))
        return {"steps": steps, "resets": resets}

    def _vtysh_file(self, step: str, content: str) -> dict[str, Any]:
        path = self._file(content)
        try:
            res = _run(["vtysh", "-f", path], 60)
        finally:
            os.unlink(path)
        lines = [ln for ln in (res.stdout + res.stderr).splitlines() if ln.strip() and not any(n in ln for n in _NOISE)]
        return {"step": step, "exit_code": res.returncode, "output": "\n".join(lines)}

    def ping(self, destination: str, source: str | None, count: int) -> dict[str, Any]:
        cmd = ["ping", "-c", str(count), "-w", str(count + 2)]
        if source:
            cmd += ["-I", source]
        cmd.append(destination)
        res = _run(cmd, count + 10)
        output = (res.stdout + res.stderr).strip()
        return labmodule.ping_output(parse_ping(output), output)
