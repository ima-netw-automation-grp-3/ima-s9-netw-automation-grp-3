"""Routeur SONiC (docker-sonic-vs) : état, configuration CONFIG_DB et FRR, bootstrap."""

from __future__ import annotations

import json
import time
from typing import Any

from .. import configdb_diff, frr_config
from ..configdb_diff import ConfigDB
from ..errors import BadRequest, NotFound, OperationFailed
from ..remote import source as remote_source
from ..store import check_domain
from .base import BaseNode

BOOT_GRACE = 180.0  # s : au-delà, supervisord injoignable = SONiC ne tourne pas
BOOTSTRAP_GRACE = 420.0  # s : au-delà, bootstrap considéré comme bloqué
BOOTSTRAP_SCRIPT = "/usr/local/bin/lab-bootstrap.sh"


dump_configdb = configdb_diff.dump


def parse_configdb_text(content: str) -> ConfigDB:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise BadRequest(f"JSON invalide (ligne {exc.lineno}, colonne {exc.colno}) : {exc.msg}") from exc
    return configdb_diff.normalize(data)


def compute_phase(data: dict[str, Any], started_at: float | None, now: float) -> tuple[str, str, float]:
    """Phase d'un routeur : booting | bootstrap | ready | warning | error (+ détail, progression)."""
    uptime = now - started_at if started_at else None
    if "services" not in data:
        if uptime is None or uptime < BOOT_GRACE:
            return "booting", "démarrage de SONiC (supervisord)…", 0.0
        return (
            "error",
            "SONiC ne tourne pas dans ce conteneur (redémarré hors containerlab ?) : redéployez le lab",
            0.0,
        )
    states = {s["name"]: s["state"] for s in data["services"]}
    key = data.get("key_services") or []
    running = [k for k in key if states.get(k) == "RUNNING"]
    progress = len(running) / len(key) if key else 1.0
    failed = [k for k in key if states.get(k) in ("FATAL", "EXITED", "BACKOFF")]
    if failed and (uptime is None or uptime > 60):
        return "error", "service(s) SONiC arrêté(s) : " + ", ".join(failed), progress
    if len(running) < len(key):
        return "booting", f"services SONiC démarrés : {len(running)}/{len(key)}", progress
    if not data.get("ready"):
        if uptime is not None and uptime > BOOTSTRAP_GRACE:
            return "warning", "bootstrap non terminé : relancez-le depuis l'interface", 1.0
        return "bootstrap", "application de la conf FRR de démarrage…", 0.95
    return "ready", "", 1.0


class SonicNode(BaseNode):
    kind = "sonic-vs"
    role = "router"
    shell_cmd = ["bash", "-c"]
    shells = {"bash": ["bash", "-l"], "vtysh": ["vtysh"]}

    # -- état -----------------------------------------------------------------------------------
    async def collect(self, *, summary: bool) -> dict[str, Any]:
        argv = ["python3", "-c", remote_source("collect_sonic")]
        if summary:
            argv.append("--summary")
        res = await self.exec(argv, timeout=30)
        if not res.stdout.strip():
            raise OperationFailed(f"collecte impossible : {res.output.strip()[:400] or 'sortie vide'}")
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise OperationFailed(f"réponse du collecteur illisible : {exc}") from exc

    async def status(self, *, summary: bool = False, started_at: float | None = None) -> dict[str, Any]:
        try:
            data = await self.collect(summary=summary)
        except OperationFailed as exc:
            return {"phase": "error", "detail": exc.message, "progress": 0.0, "errors": {}}
        phase, detail, progress = compute_phase(data, started_at, time.time())
        data.update(phase=phase, detail=detail, progress=progress)
        return data

    async def logs(self, lines: int = 200) -> str:
        res = await self.exec(["tail", "-n", str(min(max(lines, 10), 5000)), "/var/log/syslog"], timeout=15)
        return res.output

    async def bootstrap(self) -> dict[str, Any]:
        res = await self.exec(["bash", BOOTSTRAP_SCRIPT], timeout=330)
        return res.to_dict()

    # -- lecture de configuration --------------------------------------------------------------
    async def running_configdb(self) -> ConfigDB:
        res = await self.exec(["sonic-cfggen", "-d", "--print-data"], timeout=30)
        if not res.ok:
            raise OperationFailed(f"lecture de CONFIG_DB impossible : {res.output.strip()[:400]}")
        return configdb_diff.normalize(json.loads(res.stdout))

    async def running_frr(self) -> str:
        res = await self.exec(["vtysh", "-c", "show running-config"], timeout=20)
        if not res.ok:
            raise OperationFailed(f"lecture de la conf FRR impossible : {res.output.strip()[:400]}")
        return res.stdout

    def _stored(self, domain: str, source: str) -> str:
        if source == "startup":
            content = self.store.read_startup(self.name, domain)
            if content is None:
                raise NotFound(f"pas de conf de démarrage {domain} pour {self.name}")
            return content
        if source.startswith("snapshot:"):
            return self.store.read_snapshot(self.name, source.split(":", 1)[1], domain)
        raise BadRequest(f"source inconnue : {source!r} (running, startup ou snapshot:<id>)")

    async def get_config(self, domain: str, source: str = "running") -> dict[str, Any]:
        check_domain(domain)
        out: dict[str, Any] = {"node": self.name, "domain": domain, "source": source}
        if source == "running":
            if domain == "configdb":
                out["content"] = dump_configdb(configdb_diff.strip_volatile(await self.running_configdb()))
            else:
                raw = await self.running_frr()
                out["content"] = frr_config.normalize(raw)
                out["raw"] = raw
        else:
            out["content"] = self._stored(domain, source)
        return out

    async def validate_frr(self, content: str) -> dict[str, Any]:
        path = await self.upload(content, ".conf")
        try:
            res = await self.exec(["vtysh", "-C", "-f", path], timeout=30)
        finally:
            await self.cleanup(path)
        return {"valid": res.ok, "errors": "" if res.ok else frr_config.clean_vtysh_error(res.output)}

    # -- aperçu / push -------------------------------------------------------------------------
    @staticmethod
    def _check_mode(mode: str) -> str:
        if mode not in ("merge", "replace"):
            raise BadRequest(f"mode inconnu : {mode!r} (merge ou replace)")
        return mode

    async def preview(self, domain: str, mode: str, content: str) -> dict[str, Any]:
        check_domain(domain)
        self._check_mode(mode)
        if domain == "configdb":
            desired = parse_configdb_text(content)
            running = await self.running_configdb()
            plan = configdb_diff.plan(running, desired, mode)  # type: ignore[arg-type]
            return {
                "domain": domain,
                "valid": True,
                "errors": "",
                **plan.to_dict(),
                "before": dump_configdb(configdb_diff.strip_volatile(running)),
                "after": dump_configdb(configdb_diff.strip_volatile(plan.result)),
                "diff": configdb_diff.unified_diff(running, plan.result, before_name="running", after_name="après push"),
            }
        validation = await self.validate_frr(content)
        raw = await self.running_frr()
        out: dict[str, Any] = {
            "domain": domain,
            "mode": mode,
            **validation,
            "before": frr_config.normalize(raw),
            "diff": frr_config.unified_diff(raw, content),
        }
        if mode == "replace":
            out["replace"] = frr_config.replace_plan(raw, content).to_dict()
            out["after"] = frr_config.normalize(content)
        else:
            out["additions"] = frr_config.merge_additions(raw, content)
        return out

    async def push(self, domain: str, mode: str, content: str) -> dict[str, Any]:
        check_domain(domain)
        self._check_mode(mode)
        if domain == "configdb":
            return await self._push_configdb(mode, content)
        return await self._push_frr(mode, content)

    async def _push_configdb(self, mode: str, content: str) -> dict[str, Any]:
        desired = parse_configdb_text(content)
        running = await self.running_configdb()
        plan = configdb_diff.plan(running, desired, mode)  # type: ignore[arg-type]
        base = {"domain": "configdb", "mode": mode, "summary": plan.summary, "changes": [c.to_dict() for c in plan.changes]}
        if not plan.ops:
            return {**base, "ok": True, "applied": 0, "failed": [], "message": "aucun changement à appliquer"}
        ops_path = await self.upload(json.dumps([o.to_dict() for o in plan.ops]), ".json")
        try:
            res = await self.exec(["python3", "-c", remote_source("apply_configdb"), ops_path], timeout=60)
        finally:
            await self.cleanup(ops_path)
        try:
            result = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise OperationFailed(f"application impossible : {res.output.strip()[:400]}") from exc
        failed = [r for r in result.get("results", []) if not r.get("ok")]
        return {
            **base,
            "ok": not failed,
            "applied": len(plan.ops) - len(failed),
            "failed": failed,
            "message": f"{len(plan.ops) - len(failed)} opération(s) appliquée(s)",
        }

    async def _push_frr(self, mode: str, content: str) -> dict[str, Any]:
        validation = await self.validate_frr(content)
        if not validation["valid"]:
            raise BadRequest("configuration FRR invalide", detail=validation["errors"])
        raw_before = await self.running_frr()
        steps: list[dict[str, Any]] = []
        resets: list[str] = []
        if mode == "replace":
            rp = frr_config.replace_plan(raw_before, content)
            resets = rp.resets
            if rp.commands:
                steps.append(await self._vtysh_file("retrait", "\n".join(rp.commands) + "\n"))
        steps.append(await self._vtysh_file("application", content))
        raw_after = await self.running_frr()
        return {
            "domain": "frr",
            "mode": mode,
            "ok": all(s["exit_code"] == 0 for s in steps),
            "steps": steps,
            "resets": resets,
            "diff": frr_config.unified_diff(raw_before, raw_after, before_name="avant", after_name="après"),
            "message": "configuration FRR appliquée",
        }

    async def _vtysh_file(self, step: str, content: str) -> dict[str, Any]:
        path = await self.upload(content, ".conf")
        try:
            res = await self.exec(["vtysh", "-f", path], timeout=60)
        finally:
            await self.cleanup(path)
        # vtysh affiche « [pid|démon] done » pour chaque démon : bruit sans intérêt
        noise = ("done", "sending configuration", "Waiting for children")
        lines = [ln for ln in res.output.splitlines() if ln.strip() and not any(n in ln for n in noise)]
        return {"step": step, "exit_code": res.exit_code, "output": "\n".join(lines), "commands": content}

    # -- sauvegarde / restauration ------------------------------------------------------------
    async def snapshot_contents(self) -> dict[str, str]:
        cfg = configdb_diff.strip_volatile(await self.running_configdb())
        frr = frr_config.normalize(await self.running_frr())
        return {"configdb": dump_configdb(cfg), "frr": frr}

    async def startup_contents(self) -> dict[str, str]:
        """Running mise en forme de conf de démarrage (représentation du fichier actuel conservée)."""
        contents = await self.snapshot_contents()
        current = self.store.read_startup(self.name, "configdb")
        if current:
            try:
                reference = parse_configdb_text(current)
            except BadRequest:
                reference = None
            if reference is not None:
                running = parse_configdb_text(contents["configdb"])
                contents["configdb"] = dump_configdb(configdb_diff.preserve_representation(running, reference))
        return contents

    async def save_startup(self) -> dict[str, Any]:
        """Écrit la running dans lab/configs/<routeur>/ (hors mode GitOps)."""
        contents = await self.startup_contents()
        paths = [
            str(self.store.write_startup(self.name, domain, content).relative_to(self.settings.lab_dir))
            for domain, content in contents.items()
        ]
        res = await self.exec(["config", "save", "-y"], timeout=60)
        return {"ok": True, "paths": paths, "config_save": res.to_dict()}

    async def restore(self, source: str, domains: list[str] | None = None) -> dict[str, Any]:
        domains = domains or ["configdb", "frr"]
        contents = {d: self._stored(check_domain(d), source) for d in domains}
        results = {}
        for domain in ("configdb", "frr"):  # CONFIG_DB d'abord : interfaces/IP avant BGP
            if domain in contents:
                results[domain] = await self.push(domain, "replace", contents[domain])
        return {"ok": all(r.get("ok") for r in results.values()), "source": source, "results": results}

    async def diff_against(self, source: str) -> dict[str, Any]:
        ref_cfg = parse_configdb_text(self._stored("configdb", source))
        ref_frr = self._stored("frr", source)
        running_cfg = await self.running_configdb()
        changes = configdb_diff.diff(ref_cfg, running_cfg)
        running_frr = await self.running_frr()
        # même conf dans un autre ordre (fichier écrit à la main, ou dans Git) : pas une différence
        same = frr_config.equivalent(ref_frr, running_frr)
        frr_diff = "" if same else frr_config.unified_diff(ref_frr, running_frr, before_name=source, after_name="running")
        return {
            "against": source,
            "configdb": {
                "changes": [c.to_dict() for c in changes],
                "count": len(changes),
                "diff": configdb_diff.unified_diff(ref_cfg, running_cfg, before_name=source, after_name="running"),
            },
            "frr": {"diff": frr_diff},
            "dirty": bool(changes or frr_diff),
        }
