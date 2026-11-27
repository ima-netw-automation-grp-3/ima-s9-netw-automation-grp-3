"""Faux GitLab, faux containerlab/docker et fausse interface web pour tester l'agent GitOps.

Le lab de test est une copie figée du lab de base (tests/fixtures/lab/ : topologie et confs de
démarrage de sonic1, sonic2, client1, client2), indépendante de la topologie déployée dans lab/.
"""

from __future__ import annotations

import copy
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

HERE = Path(__file__).resolve().parent
LAB = HERE.parents[1]  # lab/
FIXTURE = HERE / "fixtures" / "lab"  # lab de base figé
sys.path.insert(0, str(LAB))

from gitops import agent as agentmod  # noqa: E402
from gitops.validate import Settings  # noqa: E402


def lab_files(root: Path) -> dict[str, bytes]:
    return agentmod.managed_files(root)


def make_archive(files: dict[str, bytes], top: str = "lab-infra-abc-abc") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, content in files.items():
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def with_topology(files: dict[str, bytes], change) -> dict[str, bytes]:
    """Copie des fichiers avec une topologie modifiée par change(dict)."""
    data = yaml.safe_load(files["sonic-lab.clab.yml"])
    change(data)
    out = dict(files)
    out["sonic-lab.clab.yml"] = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()
    return out


def add_client3(data: dict[str, Any]) -> None:
    data["topology"]["nodes"]["client3"] = {"kind": "linux", "mgmt-ipv4": "172.20.30.23", "exec": ["ip link set eth1 up"]}
    data["topology"]["links"].append({"endpoints": ["sonic2:eth4", "client3:eth1"]})


class FakeGitLab:
    """Projet unique en mémoire : branches, MR, archives, statuts, notes, déploiements."""

    def __init__(self) -> None:
        self.exists = True
        self.project_id = 7
        self.default_branch = "main"
        self.commits: dict[str, dict[str, bytes]] = {}
        self.branches: dict[str, str] = {}
        self.mrs: list[dict[str, Any]] = []
        self.ci: dict[str, list[dict[str, Any]]] = {}  # sha → statuts des jobs CI
        self.statuses: list[dict[str, Any]] = []
        self.notes: dict[int, list[dict[str, Any]]] = {}
        self.deployments: dict[int, str] = {}
        self.merged: dict[str, int] = {}  # sha de fusion → iid

    def commit(self, sha: str, files: dict[str, bytes], branch: str = "main", ci: str | None = "success") -> str:
        self.commits[sha] = files
        self.branches[branch] = sha
        if ci:
            self.ci[sha] = [{"name": "validate", "status": ci}]
        return sha

    # API utilisée par l'agent
    def project(self, path: str) -> dict[str, Any] | None:
        if not self.exists:
            return None
        return {"id": self.project_id, "default_branch": self.default_branch, "web_url": "http://gitlab/grp/lab-infra"}

    def branch_head(self, project_id: int, branch: str) -> str | None:
        return self.branches.get(branch)

    def open_merge_requests(self, project_id: int, target: str) -> list[dict[str, Any]]:
        return [mr for mr in self.mrs if mr.get("state", "opened") == "opened"]

    def archive(self, project_id: int, sha: str) -> bytes:
        return make_archive(self.commits[sha])

    def commit_statuses(self, project_id: int, sha: str) -> list[dict[str, Any]]:
        return self.ci.get(sha, []) + [{"name": s["name"], "status": s["state"]} for s in self.statuses if s["sha"] == sha]

    def commit_merge_requests(self, project_id: int, sha: str) -> list[dict[str, Any]]:
        return [{"iid": self.merged[sha]}] if sha in self.merged else []

    def set_status(self, project_id, sha, *, name, state, description, ref=None, target_url=None) -> None:
        self.statuses.append({"sha": sha, "name": name, "state": state, "description": description, "ref": ref})

    def upsert_note(self, project_id: int, iid: int, marker: str, body: str) -> dict[str, Any]:
        notes = self.notes.setdefault(iid, [])
        for n in notes:
            if marker in n["body"]:
                n["body"] = body
                n["updates"] += 1
                return n
        return self.add_note(project_id, iid, body)

    def add_note(self, project_id: int, iid: int, body: str) -> dict[str, Any]:
        note = {"id": 100 + sum(len(v) for v in self.notes.values()), "body": body, "updates": 0}
        self.notes.setdefault(iid, []).append(note)
        return note

    def create_deployment(self, project_id, *, sha, ref, environment) -> int:
        dep_id = len(self.deployments) + 1
        self.deployments[dep_id] = "running"
        return dep_id

    def finish_deployment(self, project_id, deployment_id, status) -> None:
        self.deployments[deployment_id] = status

    def last_status(self, name: str, sha: str) -> dict[str, Any] | None:
        found = [s for s in self.statuses if s["name"] == name and s["sha"] == sha]
        return found[-1] if found else None


class FakeRunner:
    """containerlab et docker simulés : enregistre les commandes, répond selon des réglages."""

    def __init__(self, lab: Path) -> None:
        self.lab = lab
        self.calls: list[list[str]] = []
        self.plan: dict[str, Any] = {}
        self.fail: dict[str, str] = {}  # « deploy », « destroy », « dry-run » → message d'erreur
        self.fail_once: set[str] = set()  # actions qui échouent une seule fois (puis réussissent)
        self.not_ready: set[str] = set()  # nœuds sans /var/run/lab-ready
        self.stopped: set[str] = set()

    def __call__(self, cmd: list[str], *, timeout: float, cwd: Path | None = None) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        ok = subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "containerlab":
            action = "dry-run" if "--dry-run" in cmd else cmd[1]
            if action in self.fail:
                return subprocess.CompletedProcess(cmd, 1, "", self.fail[action])
            if action in self.fail_once:
                self.fail_once.discard(action)
                return subprocess.CompletedProcess(cmd, 1, "", f"{action} : erreur simulée")
            if action == "dry-run":
                base = {"dry-run": True, "added-nodes": [], "deleted-nodes": [], "recreated-nodes": [],
                        "started-nodes": [], "added-links": [], "deleted-endpoints": [], "restarted-nodes": []}
                return subprocess.CompletedProcess(cmd, 0, json.dumps({**base, **self.plan}), "INFO ...")
            return ok
        if cmd[:2] == ["docker", "inspect"]:
            node = cmd[-1].rsplit("-", 1)[-1]
            return subprocess.CompletedProcess(cmd, 0, "false\n" if node in self.stopped else "true\n", "")
        if cmd[:2] == ["docker", "exec"] and cmd[3:5] == ["test", "-f"]:
            node = cmd[2].rsplit("-", 1)[-1]
            return subprocess.CompletedProcess(cmd, 1 if node in self.not_ready else 0, "", "")
        if cmd[:2] == ["docker", "exec"] and cmd[-1].endswith("lab-bootstrap.sh"):
            self.not_ready.discard(cmd[2].rsplit("-", 1)[-1])  # le bootstrap relancé aboutit
        return ok

    def commands(self, prefix: str) -> list[list[str]]:
        return [c for c in self.calls if " ".join(c).startswith(prefix)]


class FakeWeb:
    def __init__(self) -> None:
        self.restored: list[str] = []
        self.fail: set[str] = set()

    def restore_startup(self, node: str) -> tuple[bool, str]:
        self.restored.append(node)
        return (node not in self.fail), ("erreur simulée" if node in self.fail else "")


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture
def lab(tmp_path: Path) -> Path:
    """Dossier lab/ de test : topologie et confs du lab de base, plus les dossiers montés par les binds."""
    root = tmp_path / "proj" / "lab"
    (root / "configs").mkdir(parents=True)
    shutil.copy(FIXTURE / "sonic-lab.clab.yml", root / "sonic-lab.clab.yml")
    for node_dir in (FIXTURE / "configs").iterdir():
        if node_dir.is_dir():
            shutil.copytree(node_dir, root / "configs" / node_dir.name)
    (root / "sonic").mkdir()
    (tmp_path / "proj" / "web").mkdir()
    (root / ".runner-status").write_text("deployed\n")
    return root


@pytest.fixture
def gitlab() -> FakeGitLab:
    return FakeGitLab()


@pytest.fixture
def env(lab: Path, gitlab: FakeGitLab, tmp_path: Path):
    runner, web, clock = FakeRunner(lab), FakeWeb(), Clock()
    cfg = agentmod.Config(
        lab_dir=lab, project="grp/lab-infra", gitlab_url="http://gitlab", token="t",
        stage_root=tmp_path / "stage", ready_timeout=60, bootstrap_grace=20, ci_grace=30,
        validation=Settings(),
    )
    agent = agentmod.Agent(cfg, gitlab, runner=runner, web=web, clock=clock, sleep=clock.sleep)

    class Env:
        pass

    e = Env()
    e.lab, e.gitlab, e.runner, e.web, e.clock, e.agent, e.cfg = lab, gitlab, runner, web, clock, agent, cfg
    e.files = lab_files(lab)
    e.deepcopy = copy.deepcopy
    return e
