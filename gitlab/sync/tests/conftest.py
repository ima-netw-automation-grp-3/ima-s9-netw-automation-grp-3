"""Faux GitLab (le sous-ensemble de l'API utilisé par Syncer) et dépôts git locaux.

Chaque « machine » a son GitLab, son dossier gitlab/repos/ et son cache : copier les fichiers de
l'une à l'autre revient à un git pull du projet.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gitlab_sync import ApiError, Syncer  # noqa: E402

GIT_OPTIONS = [
    "-c", "user.name=Test",
    "-c", "user.email=test@example.com",
    "-c", "init.defaultBranch=main",
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
]


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *GIT_OPTIONS, *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} : {result.stderr}")
    return result.stdout.strip()


def bare_refs(repo: Path) -> dict[str, str]:
    out = git("for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags", cwd=repo)
    return {ref: sha for sha, ref in (line.split(" ", 1) for line in out.splitlines())}


class FakeGitLab:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.projects: dict[int, dict[str, Any]] = {}
        self.namespaces: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.defaults: dict[int, str] = {}
        self.created_head: dict[int, str] = {}  # HEAD posée à la création (paramètre default_branch)
        self.default_branch_puts = 0
        self.refuse_visibility = False
        self._last_id = 0

    def _id(self) -> int:
        self._last_id += 1
        return self._last_id

    # -- préparation des tests --------------------------------------------------------------------
    def add_user(self, username: str) -> dict[str, Any]:
        self.users[username] = {"id": self._id(), "username": username}
        namespace = {"id": self._id(), "kind": "user", "full_path": username}
        self.namespaces[username] = namespace
        return namespace

    def add_group(self, full_path: str) -> dict[str, Any]:
        namespace = {"id": self._id(), "kind": "group", "full_path": full_path}
        self.namespaces[full_path] = namespace
        return namespace

    def add_project(
        self, full_path: str, *, name: str | None = None, description: str = "", visibility: str = "private"
    ) -> dict[str, Any]:
        namespace_path, _, path = full_path.rpartition("/")
        namespace = self.namespaces[namespace_path]
        project_id = self._id()
        git("init", "--quiet", "--bare", str(self.repo(project_id)))
        self.projects[project_id] = {
            "id": project_id,
            "name": name or path,
            "path": path,
            "path_with_namespace": full_path,
            "description": description,
            "visibility": visibility,
            "namespace": {"id": namespace["id"], "kind": namespace["kind"], "full_path": namespace["full_path"]},
            "marked_for_deletion_at": None,
        }
        return self.get_project(project_id)

    def project(self, full_path: str) -> dict[str, Any] | None:
        for project_id, project in self.projects.items():
            if project["path_with_namespace"] == full_path:
                return self.get_project(project_id)
        return None

    def delete_project(self, full_path: str) -> None:
        project = self.project(full_path)
        assert project is not None
        del self.projects[project["id"]]
        shutil.rmtree(self.repo(project["id"]))

    def repo(self, project_id: int) -> Path:
        return self.root / f"{project_id}.git"

    def refs(self, full_path: str) -> dict[str, str]:
        project = self.project(full_path)
        assert project is not None
        return bare_refs(self.repo(project["id"]))

    def branches(self, project_id: int) -> list[str]:
        return sorted(ref[len("refs/heads/") :] for ref in bare_refs(self.repo(project_id)) if ref.startswith("refs/heads/"))

    # -- API utilisée par Syncer --------------------------------------------------------------------
    def current_user(self) -> dict[str, Any]:
        return {"username": "root", "is_admin": True}

    def list_projects(self) -> list[dict[str, Any]]:
        return [self.get_project(project_id) for project_id in sorted(self.projects)]

    def get_project(self, project_id: int) -> dict[str, Any]:
        project = dict(self.projects[project_id])
        branches = self.branches(project_id)
        default = self.defaults.get(project_id)
        if default not in branches:  # comme GitLab : main, sinon la première branche
            default = "main" if "main" in branches else (branches[0] if branches else None)
        project["default_branch"] = default
        return project

    def find_namespace(self, full_path: str) -> dict[str, Any] | None:
        return self.namespaces.get(full_path)

    def find_user(self, username: str) -> dict[str, Any] | None:
        return self.users.get(username)

    def create_group(self, name: str, path: str, parent_id: int | None) -> dict[str, Any]:
        parent = next((n for n in self.namespaces.values() if n["id"] == parent_id), None)
        return self.add_group(f"{parent['full_path']}/{path}" if parent else path)

    def create_project(
        self,
        namespace: dict[str, Any],
        *,
        name: str,
        path: str,
        description: str,
        visibility: str,
        default_branch: str | None = None,
    ) -> dict[str, Any]:
        if self.refuse_visibility and visibility != "private":
            raise ApiError(400, '{"visibility_level":["internal is not allowed in a private group."]}')
        project = self.add_project(
            f"{namespace['full_path']}/{path}", name=name, description=description, visibility=visibility
        )
        if default_branch:  # comme GitLab : HEAD du dépôt créé
            self.defaults[project["id"]] = self.created_head[project["id"]] = default_branch
        return project

    def update_project(self, project_id: int, **attributes: Any) -> None:
        branch = attributes["default_branch"]
        assert branch in self.branches(project_id)
        self.defaults[project_id] = branch
        self.default_branch_puts += 1

    def after_first_push(self, project_id: int) -> None:
        """Comme le traitement asynchrone du premier push dans GitLab : la branche par défaut
        redevient celle que pointe HEAD à la création (un PUT fait entre-temps est perdu)."""
        self.defaults[project_id] = self.created_head.get(project_id, "main")

    def remote_url(self, project: dict[str, Any]) -> str:
        return str(self.repo(project["id"]))


class Work:
    """Clone de travail d'un projet du faux GitLab, pour créer des commits et les pousser."""

    def __init__(self, path: Path, remote: str) -> None:
        self.dir = path
        git("clone", "--quiet", remote, str(path))

    def switch(self, branch: str, start: str | None = None) -> None:
        if start is None:
            git("switch", "--quiet", branch, cwd=self.dir)
        else:
            git("switch", "--quiet", "-C", branch, start, cwd=self.dir)

    def commit(self, name: str, content: str) -> str:
        (self.dir / name).write_text(content)
        git("add", name, cwd=self.dir)
        git("commit", "--quiet", "-m", f"{name} : {content}", cwd=self.dir)
        return git("rev-parse", "HEAD", cwd=self.dir)

    def tag(self, name: str) -> None:
        git("tag", "-a", name, "-m", name, cwd=self.dir)

    def push(self, *refspecs: str) -> None:
        git("push", "--quiet", "origin", *refspecs, cwd=self.dir)


class Machine:
    """Un poste : son GitLab, son gitlab/repos/ et son service de synchronisation."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.gitlab = FakeGitLab(root / "gitlab")
        self.repos = root / "repos"
        self.repos.mkdir(parents=True, exist_ok=True)
        self.syncer = Syncer(self.gitlab, self.repos, root / "cache")
        self._works = 0

    def restart(self) -> None:
        """Redémarrage du service : la base en mémoire est perdue."""
        self.syncer = Syncer(self.gitlab, self.repos, self.root / "cache")

    def work(self, full_path: str) -> Work:
        self._works += 1
        project = self.gitlab.project(full_path)
        assert project is not None
        return Work(self.root / f"work{self._works}", self.gitlab.remote_url(project))

    def files(self) -> dict[str, bytes]:
        return {p.relative_to(self.repos).as_posix(): p.read_bytes() for p in sorted(self.repos.rglob("*")) if p.is_file()}

    def pull_from(self, other: Machine) -> None:
        """git pull du projet : les fichiers exportés par l'autre poste remplacent les nôtres."""
        shutil.rmtree(self.repos)
        shutil.copytree(other.repos, self.repos)


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ni la configuration git de l'utilisateur ni celle du système."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def machines(tmp_path: Path) -> tuple[Machine, Machine]:
    return Machine(tmp_path / "a"), Machine(tmp_path / "b")
