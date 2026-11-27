"""Agent GitOps : la branche par défaut du projet GitLab de l'infra pilote le lab.

Boucle (toutes les GITOPS_INTERVAL secondes, quand le runner a fini de déployer) :
  1. branche par défaut : si sa tête n'est pas le commit appliqué, l'appliquer (validation, CI verte,
     sauvegarde, installation, destroy des nœuds redéfinis, containerlab deploy, attente, restauration
     des confs de démarrage, rollback en cas d'échec) et publier le statut lab/deploy ;
  2. merge requests ouvertes : plan containerlab de chaque nouveau commit, en commentaire et en statut
     lab/plan (recalculé quand le commit appliqué change).

L'agent n'exécute jamais de code venu du dépôt : il n'en installe que sonic-lab.clab.yml et
configs/<nœud>/{config_db.json,frr.conf}, après validation par son propre validateur.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from . import plan as planmod
from . import validate
from .gitlab import GitLab, GitLabError

TOPOLOGY = validate.TOPOLOGY_FILE
CONFIG_FILES = validate.CONFIG_FILES
READY_STATES = ("deployed", "failed")  # états du runner où l'agent peut agir
RUNNING_CI = {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
FAILED_CI = {"failed", "canceled"}
KEEP_BACKUPS = 20

Runner = Callable[..., subprocess.CompletedProcess]


def default_runner(cmd: list[str], *, timeout: float, cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]} : délai de {timeout:.0f} s dépassé")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} : {exc}")


def log(message: str) -> None:
    print(f"[gitops] {message}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Config:
    lab_dir: Path
    project: str
    gitlab_url: str
    token: str
    web_url: str = "http://127.0.0.1:8080"
    web_auth: tuple[str, str] | None = None
    lab_name: str = "sonic-lab"
    interval: float = 20.0
    ready_timeout: float = 420.0
    bootstrap_grace: float = 90.0  # s sans /var/run/lab-ready avant de relancer le bootstrap soi-même
    ci_grace: float = 90.0  # s sans aucun job CI avant de déployer quand même (projet sans CI)
    environment: str = "lab"
    stage_root: Path = Path("/tmp/gitops")
    validation: validate.Settings = field(default_factory=validate.Settings)

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ.get
        group = env("GITLAB_GROUP") or "ima-netw-automation-grp-3"
        bind = env("WEB_BIND") or "127.0.0.1"
        host = "127.0.0.1" if bind in ("0.0.0.0", "::", "") else bind  # 127.0.0.1 : accepté par le contrôle Host
        user, password = env("WEB_AUTH_USER") or "", env("WEB_AUTH_PASSWORD") or ""
        return cls(
            lab_dir=Path(env("LAB_DIR") or Path.cwd()),
            project=env("GITOPS_PROJECT") or f"{group}/lab-infra",
            gitlab_url=env("GITLAB_URL") or f"http://{env('GITLAB_IP') or '172.20.30.5'}",
            token=env("GITLAB_API_TOKEN") or "",
            web_url=env("WEB_URL") or f"http://{host}:{env('WEB_PORT') or '8080'}",
            web_auth=(user, password) if user and password else None,
            lab_name=env("LAB_NAME") or "sonic-lab",
            interval=float(env("GITOPS_INTERVAL") or 20),
            validation=validate.Settings.from_env(),
        )


class Web:
    """API de l'interface web : restauration de la conf de démarrage d'un routeur."""

    def __init__(self, url: str, auth: tuple[str, str] | None) -> None:
        self.url = url.rstrip("/")
        self.auth = auth

    def restore_startup(self, node: str) -> tuple[bool, str]:
        headers = {"Content-Type": "application/json"}
        if self.auth:
            headers["Authorization"] = "Basic " + base64.b64encode(":".join(self.auth).encode()).decode()
        req = urllib.request.Request(
            f"{self.url}/api/nodes/{node}/config/restore", data=b'{"source":"startup"}', headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code} : {e.read().decode(errors='replace')[:300]}"
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            return False, f"interface web injoignable ({self.url}) : {e}"
        return bool(data.get("ok")), "" if data.get("ok") else json.dumps(data, ensure_ascii=False)[:400]


class DeployError(Exception):
    pass


# --- fichiers du lab ----------------------------------------------------------------------------


def managed_files(root: Path) -> dict[str, bytes]:
    """Fichiers gérés par Git : la topologie et configs/<nœud>/{config_db.json,frr.conf}."""
    out: dict[str, bytes] = {}
    topo = root / TOPOLOGY
    if topo.is_file():
        out[TOPOLOGY] = topo.read_bytes()
    configs = root / "configs"
    if configs.is_dir():
        for node_dir in sorted(p for p in configs.iterdir() if p.is_dir() and not p.is_symlink()):
            for name in CONFIG_FILES:
                f = node_dir / name
                if f.is_file() and not f.is_symlink():
                    out[f"configs/{node_dir.name}/{name}"] = f.read_bytes()
    return out


def digests(files: dict[str, bytes]) -> dict[str, str]:
    return {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}


def _allowed_member(name: str) -> str | None:
    """Chemin d'un membre de l'archive (sans le dossier racine) s'il fait partie des fichiers gérés."""
    parts = name.split("/")[1:]  # archive GitLab : <projet>-<sha>-<sha>/...
    if any(p in ("", ".", "..") for p in parts):
        return None
    if parts == [TOPOLOGY]:
        return TOPOLOGY
    if len(parts) == 3 and parts[0] == "configs" and parts[2] in CONFIG_FILES:
        return "/".join(parts)
    return None


def extract(archive: bytes, dest: Path) -> dict[str, bytes]:
    """Extrait de l'archive les seuls fichiers gérés (fichiers ordinaires, pas de lien)."""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            rel = _allowed_member(member.name)
            if rel is None or not member.isfile() or member.size > 4 * 1024 * 1024:
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            files[rel] = fh.read()
    for rel, content in files.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return files


def write_in_place(path: Path, content: bytes, owner: tuple[int, int] | None) -> None:
    """Écrit sans changer d'inode : les confs de démarrage sont montées fichier par fichier dans les
    routeurs (un remplacement atomique laisserait le routeur sur l'ancien contenu)."""
    created = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return
    with open(path, "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    if created and owner:
        try:
            os.chown(path, *owner)
            os.chown(path.parent, *owner)
        except OSError:
            pass


# --- agent --------------------------------------------------------------------------------------


class Agent:
    def __init__(
        self,
        cfg: Config,
        gitlab: GitLab,
        *,
        runner: Runner = default_runner,
        web: Web | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.gitlab = gitlab
        self.runner = runner
        self.web = web or Web(cfg.web_url, cfg.web_auth)
        self.clock = clock
        self.sleep = sleep
        self.state_dir = cfg.lab_dir / ".gitops"
        self.state_path = self.state_dir / "state.json"
        self.state = self._load_state()
        self._last_log = ""

    # -- état ------------------------------------------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("plans", {})
        data.setdefault("seen", {})
        if data.get("status") == "deploying":  # agent arrêté en plein déploiement
            data["status"] = "interrupted"
            data["message"] = "déploiement interrompu (redémarrage du service clab) : vérification au prochain cycle"
        return data

    def save_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state["updated_at"] = now_iso()
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, self.state_path)

    def _status(self, status: str, message: str) -> None:
        self.state["status"] = status
        self.state["message"] = message
        self.save_state()

    def _log_once(self, message: str) -> None:
        if message != self._last_log:
            log(message)
            self._last_log = message

    @property
    def owner(self) -> tuple[int, int] | None:
        try:
            st = self.cfg.lab_dir.stat()
            return st.st_uid, st.st_gid
        except OSError:
            return None

    def runner_status(self) -> str:
        try:
            return (self.cfg.lab_dir / ".runner-status").read_text().strip()
        except OSError:
            return ""

    def set_runner_status(self, status: str) -> None:
        (self.cfg.lab_dir / ".runner-status").write_text(status + "\n")

    # -- boucle ----------------------------------------------------------------------------------
    def run_forever(self) -> None:
        log(f"projet suivi : {self.cfg.project} ({self.cfg.gitlab_url}), toutes les {self.cfg.interval:.0f} s")
        while True:
            try:
                self.cycle()
            except GitLabError as exc:
                self._log_once(f"GitLab : {exc}")
            except Exception as exc:  # garde-fou : l'agent ne doit pas s'arrêter
                log(f"erreur inattendue : {exc!r}")
            self.sleep(self.cfg.interval)

    def cycle(self) -> None:
        if self.runner_status() not in READY_STATES:
            return  # le runner déploie ou détruit le lab
        project = self.gitlab.project(self.cfg.project)
        if project is None:
            if self.state.get("active") is not False:
                self.state.update(active=False, project=self.cfg.project)
                self._status("inactive", f"projet {self.cfg.project} absent de GitLab (make infra-project)")
            self._log_once(f"mode GitOps inactif : projet {self.cfg.project} absent")
            return
        if self.state.get("project_id") not in (None, project["id"]):
            self.state.update(applied_sha=None, applied_files={}, failed_sha=None, plans={})  # projet recréé
        self.state.update(
            active=True,
            project=self.cfg.project,
            project_id=project["id"],
            project_url=project.get("web_url"),
            default_branch=project.get("default_branch") or "main",
        )
        self._last_log = ""
        self.apply_default_branch(project)
        mrs = self.gitlab.open_merge_requests(project["id"], self.state["default_branch"])
        for mr in mrs:
            self.plan_merge_request(project, mr)
        # oubli des MR fermées et des commits vus il y a plus d'un jour
        self.state["plans"] = {k: v for k, v in self.state["plans"].items() if k in {str(m["iid"]) for m in mrs}}
        self.state["seen"] = {k: v for k, v in self.state["seen"].items() if self.clock() - v < 86400}
        self.save_state()

    # -- staging ---------------------------------------------------------------------------------
    def stage(self, project_id: int, sha: str) -> tuple[Path, dict[str, bytes]]:
        """Commit → /tmp/gitops/<sha>/lab, avec les fichiers du lab (bootstrap, RESTCONF, web) en liens."""
        base = self.cfg.stage_root / sha
        shutil.rmtree(base, ignore_errors=True)
        root = base / "lab"
        root.mkdir(parents=True)
        files = extract(self.gitlab.archive(project_id, sha), root)
        (root / "sonic").symlink_to(self.cfg.lab_dir / "sonic")
        (base / "web").symlink_to(self.cfg.lab_dir.parent / "web")
        return root, files

    def unstage(self, sha: str) -> None:
        shutil.rmtree(self.cfg.stage_root / sha, ignore_errors=True)

    def dry_run(self, topology_dir: Path) -> tuple[dict[str, Any] | None, str]:
        res = self.runner(
            ["containerlab", "deploy", "-t", TOPOLOGY, "--dry-run", "-f", "json"], timeout=300, cwd=topology_dir
        )
        text = res.stdout or ""
        start = text.find("{")
        if res.returncode != 0 or start < 0:
            return None, (res.stderr or res.stdout or "containerlab --dry-run a échoué").strip()
        try:
            return json.loads(text[start:]), ""
        except json.JSONDecodeError as exc:
            return None, f"sortie de containerlab illisible : {exc}"

    def load(self, root: Path) -> dict[str, Any]:
        try:
            return yaml.safe_load((root / TOPOLOGY).read_text()) or {}
        except (OSError, yaml.YAMLError):
            return {}

    # -- plan d'une merge request ----------------------------------------------------------------
    def plan_merge_request(self, project: dict[str, Any], mr: dict[str, Any]) -> None:
        pid, iid, sha = project["id"], mr["iid"], mr.get("sha")
        if not sha or self.state.get("status") == "deploying":
            return
        key = f"{sha}:{self.state.get('applied_sha')}"
        if self.state["plans"].get(str(iid)) == key:
            return
        ref = mr.get("source_branch")
        self.gitlab.set_status(pid, sha, name="lab/plan", state="running", description="calcul du plan…", ref=ref)
        root, files = self.stage(pid, sha)
        try:
            report = validate.validate(root, self.cfg.validation)
            the_plan, clab_error, topology = None, "", self.load(root)
            if report.ok:
                clab, clab_error = self.dry_run(root)
                if clab is not None:
                    the_plan = planmod.compute(
                        self.load(self.cfg.lab_dir), topology, clab, managed_files(self.cfg.lab_dir), files
                    )
            ok = report.ok and the_plan is not None
            body = planmod.note(
                sha=sha,
                applied_sha=self.state.get("applied_sha"),
                plan=the_plan,
                errors=report.errors,
                warnings=report.warnings,
                topology=topology if ok else None,
                clab_error=clab_error,
            )
            note = self.gitlab.upsert_note(pid, iid, planmod.NOTE_MARKER, body)
            url = f"{mr.get('web_url')}#note_{note.get('id')}" if note and mr.get("web_url") else mr.get("web_url")
            description = the_plan.summary() if ok else f"refusé : {(report.errors or [clab_error])[0]}"
            self.gitlab.set_status(
                pid, sha, name="lab/plan", state="success" if ok else "failed", description=description, ref=ref,
                target_url=url,
            )
            self.state["plans"][str(iid)] = key
            log(f"MR !{iid} ({sha[:8]}) : plan {'OK' if ok else 'refusé'} : {description}")
        finally:
            self.unstage(sha)

    # -- branche par défaut ----------------------------------------------------------------------
    def ci_gate(self, project_id: int, sha: str) -> tuple[str, str]:
        """("ok"|"wait"|"fail", raison) d'après les jobs CI du commit (statuts lab/* exclus)."""
        statuses = [s for s in self.gitlab.commit_statuses(project_id, sha) if not str(s.get("name", "")).startswith("lab/")]
        seen = self.state["seen"].setdefault(sha, self.clock())
        if not statuses:
            return ("ok", "") if self.clock() - seen >= self.cfg.ci_grace else ("wait", "en attente de la CI")
        failed = [s["name"] for s in statuses if s.get("status") in FAILED_CI and not s.get("allow_failure")]
        if failed:
            return "fail", f"CI en échec : {', '.join(sorted(set(failed)))}"
        if any(s.get("status") in RUNNING_CI for s in statuses):
            return "wait", "CI en cours"
        return "ok", ""

    def apply_default_branch(self, project: dict[str, Any]) -> None:
        pid, branch = project["id"], self.state["default_branch"]
        head = self.gitlab.branch_head(pid, branch)
        if not head or head in (self.state.get("applied_sha"), self.state.get("failed_sha")):
            return
        current = managed_files(self.cfg.lab_dir)
        root, files = self.stage(pid, head)
        try:
            if files == current:
                # adoption (projet créé depuis le lab, redémarrage après interruption) ou commit sans
                # effet sur l'infra (README, CI) : rien à déployer
                self._applied(head, files, "conforme : rien à déployer")
                self.gitlab.set_status(pid, head, name="lab/deploy", state="success", description="lab déjà conforme", ref=branch)
                return
            gate, why = self.ci_gate(pid, head)
            if gate == "wait":
                return
            if gate == "fail":
                self.state["failed_sha"] = head
                self._status("failed", f"{head[:8]} non déployé : {why}")
                self.gitlab.set_status(pid, head, name="lab/deploy", state="failed", description=why, ref=branch)
                return
            self.deploy(project, head, root, files, current)
        finally:
            self.unstage(head)

    def _applied(self, sha: str, files: dict[str, bytes], message: str) -> None:
        self.state.update(applied_sha=sha, applied_files=digests(files), failed_sha=None, applied_at=now_iso())
        self._status("deployed", f"{sha[:8]} : {message}")
        log(f"commit {sha[:8]} appliqué : {message}")

    def deploy(
        self, project: dict[str, Any], sha: str, root: Path, files: dict[str, bytes], current: dict[str, bytes]
    ) -> None:
        pid, branch = project["id"], self.state["default_branch"]
        env_url = f"{project.get('web_url')}/-/environments" if project.get("web_url") else None
        self.gitlab.set_status(pid, sha, name="lab/deploy", state="running", description="déploiement…", ref=branch, target_url=env_url)
        deployment = self.gitlab.create_deployment(pid, sha=sha, ref=branch, environment=self.cfg.environment)
        self.state["applying_sha"] = sha
        self._status("deploying", f"déploiement de {sha[:8]}")
        log(f"déploiement de {sha[:8]}")

        the_plan: planmod.Plan | None = None
        ok, rolled_back, message = False, False, ""
        drift = self.drift(current)
        try:
            report = validate.validate(root, self.cfg.validation)
            if not report.ok:
                raise DeployError("topologie refusée : " + "; ".join(report.errors[:5]))
            clab, err = self.dry_run(root)
            if clab is None:
                raise DeployError(f"containerlab --dry-run : {err[-500:]}")
            old_topo, new_topo = self.load(self.cfg.lab_dir), self.load(root)
            the_plan = planmod.compute(old_topo, new_topo, clab, current, files)
            backup = self.backup(current, sha)
            log(f"plan : {the_plan.summary()} ; sauvegarde : {backup}")
            try:
                self.apply(files, the_plan, new_topo)
                ok, message = True, f"{the_plan.summary()}."
            except DeployError as exc:
                log(f"échec : {exc} : retour au commit précédent")
                message = f"échec : {exc}"
                try:
                    self.apply(current, the_plan, old_topo)
                    rolled_back = True
                    message += " Rollback effectué."
                except DeployError as exc2:
                    message += f" Rollback en échec : {exc2} (make redeploy)."
        except DeployError as exc:
            message = f"refusé avant toute modification : {exc}"
        finally:
            self.set_runner_status("deployed" if ok or rolled_back or not the_plan else "failed")

        state = "success" if ok else "failed"
        self.gitlab.set_status(pid, sha, name="lab/deploy", state=state, description=message, ref=branch, target_url=env_url)
        self.gitlab.finish_deployment(pid, deployment, state)
        self.state.pop("applying_sha", None)
        if ok:
            self._applied(sha, files, message)
        else:
            self.state["failed_sha"] = sha
            self._status("failed", f"{sha[:8]} : {message}")
            log(f"commit {sha[:8]} : {message}")
        try:
            for mr in self.gitlab.commit_merge_requests(pid, sha)[:1]:
                self.gitlab.add_note(
                    pid, mr["iid"],
                    planmod.deploy_note(sha=sha, ok=ok, plan=the_plan, message=message, drift=drift, rolled_back=rolled_back),
                )
        except GitLabError as exc:
            log(f"note de déploiement impossible : {exc}")

    # -- application -----------------------------------------------------------------------------
    def drift(self, current: dict[str, bytes]) -> list[str]:
        """Fichiers de lab/ modifiés hors Git depuis le dernier déploiement (sauvegardés, puis remplacés)."""
        applied = self.state.get("applied_files") or {}
        if not applied:
            return []
        cur = digests(current)
        return sorted(p for p in set(cur) | set(applied) if cur.get(p) != applied.get(p))

    def backup(self, files: dict[str, bytes], sha: str) -> Path:
        root = self.state_dir / "backups"
        dest = root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{sha[:8]}"
        for rel, content in files.items():
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for old in sorted(p for p in root.iterdir() if p.is_dir())[:-KEEP_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)
        return dest

    def install(self, files: dict[str, bytes]) -> None:
        """lab/ = exactement les fichiers gérés donnés : confs d'abord, topologie ensuite."""
        lab, owner = self.cfg.lab_dir, self.owner
        for rel, content in sorted(files.items()):
            if rel != TOPOLOGY:
                write_in_place(lab / rel, content, owner)
        for rel in managed_files(lab):
            if rel != TOPOLOGY and rel not in files:
                (lab / rel).unlink(missing_ok=True)
                try:
                    (lab / rel).parent.rmdir()
                except OSError:
                    pass
        write_in_place(lab / TOPOLOGY, files[TOPOLOGY], owner)

    def clab(self, *args: str, timeout: float = 900) -> None:
        res = self.runner(["containerlab", *args, "-t", TOPOLOGY], timeout=timeout, cwd=self.cfg.lab_dir)
        if res.returncode != 0:
            raise DeployError(f"containerlab {args[0]} : {(res.stderr or res.stdout).strip()[-600:]}")

    def apply(self, files: dict[str, bytes], the_plan: planmod.Plan, topology: dict[str, Any]) -> None:
        self.set_runner_status("deploying")
        self.install(files)
        if the_plan.redefined:
            self.clab("destroy", "--node-filter", ",".join(the_plan.redefined), timeout=300)
        if the_plan.topology_changed:
            self.clab("deploy")
        self.wait_ready(topology, the_plan)
        self.set_runner_status("deployed")
        for node in the_plan.restore:
            ok, detail = self.web.restore_startup(node)
            if not ok:
                raise DeployError(f"{node} : restauration de la conf de démarrage : {detail}")

    def container(self, node: str) -> str:
        return f"clab-{self.cfg.lab_name}-{node}"

    def _docker_ok(self, *args: str, timeout: float = 20) -> bool:
        return self.runner(["docker", *args], timeout=timeout).returncode == 0

    def wait_ready(self, topology: dict[str, Any], the_plan: planmod.Plan) -> None:
        """Tous les nœuds en marche, routeurs prêts (/var/run/lab-ready). Un routeur recréé dont le
        bootstrap ne s'est pas lancé reçoit un bootstrap explicite."""
        nodes = planmod.nodes_of(topology)
        fresh = set(the_plan.added) | set(the_plan.recreated)
        started = self.clock()
        rebooted: set[str] = set()
        while True:
            pending = []
            for name, node in nodes.items():
                c = self.container(name)
                res = self.runner(["docker", "inspect", "-f", "{{.State.Running}}", c], timeout=20)
                if res.returncode != 0 or res.stdout.strip() != "true":
                    pending.append(f"{name} (arrêté)")
                    continue
                if node.get("kind") == validate.SONIC and not self._docker_ok("exec", c, "test", "-f", "/var/run/lab-ready"):
                    pending.append(name)
                    if name in fresh and name not in rebooted and self.clock() - started > self.cfg.bootstrap_grace:
                        rebooted.add(name)
                        log(f"{name} : bootstrap relancé par l'agent")
                        self.runner(["docker", "exec", c, "bash", "/usr/local/bin/lab-bootstrap.sh"], timeout=400)
            if not pending:
                return
            if self.clock() - started > self.cfg.ready_timeout:
                raise DeployError(f"nœuds pas prêts après {self.cfg.ready_timeout:.0f} s : {', '.join(pending)}")
            self.sleep(5)


def main() -> int:
    cfg = Config.from_env()
    if not cfg.token:
        log("GITLAB_API_TOKEN absent : agent arrêté")
        return 1
    Agent(cfg, GitLab(cfg.gitlab_url, cfg.token)).run_forever()
    return 0
