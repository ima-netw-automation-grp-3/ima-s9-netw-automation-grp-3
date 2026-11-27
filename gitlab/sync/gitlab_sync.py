#!/usr/bin/env python3
"""Synchronise les dépôts GitLab avec gitlab/repos/, dossier versionné avec le projet.

Chaque projet GitLab y est représenté par deux fichiers, rangés sous le chemin de son namespace :
  <namespace>/<projet>.json    nom, description, visibilité, branche par défaut, refs (branches, tags)
  <namespace>/<projet>.bundle  git bundle des branches et des tags (absent pour un dépôt vide)

Un cycle, au démarrage puis toutes les SYNC_INTERVAL secondes, compare pour chaque projet trois
états : la base (état commun au cycle précédent, en mémoire), le fichier et GitLab.
  - Fichier modifié depuis la base (clone neuf du projet, git pull, premier cycle) : import.
    Projet absent de GitLab : il est recréé, avec son namespace, ses branches et ses tags.
    Projet présent : les branches en avance sont poussées, les nouvelles branches et les nouveaux
    tags ajoutés. Une branche en retard est ignorée ; une branche divergente reste telle quelle dans
    GitLab (avertissement). Une branche supprimée du fichier n'est supprimée dans GitLab que si
    GitLab ne l'a pas modifiée depuis la base, et jamais au premier cycle.
  - GitLab différent du fichier : export (bundle et JSON réécrits).
  - Projet supprimé de GitLab après avoir été vu : ses fichiers sont supprimés.
GitLab a le dernier mot : supprimer un fichier ne supprime pas le projet, qui est réexporté.

Usage : gitlab_sync.py run      boucle (service gitlab-sync), dernier cycle à l'arrêt (SIGTERM) ;
                                SIGUSR1 déclenche un cycle immédiat
        gitlab_sync.py trigger  dans le conteneur du service : déclenche un cycle, attend sa fin et
                                affiche son compte rendu (make gitlab-sync)
        gitlab_sync.py once     un cycle autonome, sans l'état du service : comme au premier cycle,
                                rien n'est supprimé, un projet absent de GitLab est recréé
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("gitlab-sync")
log.setLevel(logging.INFO)  # comptes rendus de cycle, quelle que soit la config du journal

HEADS = "refs/heads/"
TAGS = "refs/tags/"
IMPORTED = "refs/import/"  # refs du bundle, chargées dans le miroir avant un push
GIT_TIMEOUT = 600


class SyncError(RuntimeError):
    """Erreur limitée à un projet : journalisée, le cycle continue avec les autres."""


class BundleError(SyncError):
    """Bundle absent ou illisible : l'import est abandonné, GitLab a le dernier mot."""


class ApiError(SyncError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"API GitLab, HTTP {status} : {message}")
        self.status = status
        self.message = message


# --- état d'un projet ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """État d'un projet (métadonnées et refs), tel que l'écrit le fichier JSON."""

    name: str
    description: str
    visibility: str
    namespace_kind: str
    default_branch: str | None
    refs: tuple[tuple[str, str], ...]  # (ref, sha), triées

    @classmethod
    def build(
        cls,
        *,
        name: str | None,
        description: str | None,
        visibility: str | None,
        namespace_kind: str | None,
        default_branch: str | None,
        refs: dict[str, str],
    ) -> Snapshot:
        refs = {ref: sha for ref, sha in refs.items() if ref.startswith((HEADS, TAGS))}
        branches = {ref[len(HEADS) :] for ref in refs if ref.startswith(HEADS)}
        return cls(
            name=name or "",
            description=description or "",
            visibility=visibility or "private",
            namespace_kind=namespace_kind or "group",
            # sans branche, GitLab annonce quand même un nom : sans objet, donc ignoré
            default_branch=default_branch if default_branch in branches else None,
            refs=tuple(sorted(refs.items())),
        )

    @classmethod
    def from_project(cls, project: dict[str, Any], refs: dict[str, str]) -> Snapshot:
        return cls.build(
            name=project.get("name"),
            description=project.get("description"),
            visibility=project.get("visibility"),
            namespace_kind=(project.get("namespace") or {}).get("kind"),
            default_branch=project.get("default_branch"),
            refs=refs,
        )

    @classmethod
    def from_json(cls, text: str) -> Snapshot:
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("refs", {}), dict):
            raise ValueError("format inattendu")
        refs = data.get("refs") or {}
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in refs.items()):
            raise ValueError("refs invalides")
        return cls.build(
            name=data.get("name"),
            description=data.get("description"),
            visibility=data.get("visibility"),
            namespace_kind=data.get("namespace_kind"),
            default_branch=data.get("default_branch"),
            refs=refs,
        )

    def to_json(self) -> str:
        data = {
            "name": self.name,
            "description": self.description,
            "visibility": self.visibility,
            "namespace_kind": self.namespace_kind,
            "default_branch": self.default_branch,
            "refs": dict(self.refs),
        }
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    @property
    def ref_map(self) -> dict[str, str]:
        return dict(self.refs)


@dataclass(frozen=True)
class InvalidFile:
    reason: str


def short(ref: str) -> str:
    for prefix, kind in ((HEADS, "branche"), (TAGS, "tag")):
        if ref.startswith(prefix):
            return f"{kind} {ref[len(prefix) :]}"
    return ref


def describe(old: Snapshot | None, new: Snapshot) -> str:
    """Résumé lisible des différences, pour les logs."""
    if old is None:
        branches = sum(1 for ref, _ in new.refs if ref.startswith(HEADS))
        return f"{branches} branche(s), {len(new.refs) - branches} tag(s)"
    before, after = old.ref_map, new.ref_map
    parts = []
    for ref in sorted(set(before) | set(after)):
        if ref not in before:
            parts.append(f"+{short(ref)}")
        elif ref not in after:
            parts.append(f"-{short(ref)}")
        elif before[ref] != after[ref]:
            parts.append(f"{short(ref)} {before[ref][:8]}→{after[ref][:8]}")
    for attr in ("name", "description", "visibility", "default_branch"):
        if getattr(old, attr) != getattr(new, attr):
            parts.append(attr)
    return ", ".join(parts) or "aucun changement"


# --- sorties de git ------------------------------------------------------------------------------


def parse_refs(output: str) -> dict[str, str]:
    """Lignes « sha<espace ou tab>ref » (ls-remote, for-each-ref) : branches et tags, sans ^{}."""
    refs = {}
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, ref = parts[0], parts[1].strip()
        if ref.startswith((HEADS, TAGS)) and not ref.endswith("^{}"):
            refs[ref] = sha
    return refs


@dataclass(frozen=True)
class PushResult:
    ref: str
    flag: str  # « ! » refusé, « * » nouvelle, « - » supprimée, « » avance rapide, « = » inchangée
    summary: str

    @property
    def ok(self) -> bool:
        return self.flag != "!"


def parse_push_porcelain(output: str) -> list[PushResult]:
    """Sortie de git push --porcelain : « <flag>\\t<src>:<dst>\\t<résumé> » par ref."""
    results = []
    for line in output.splitlines():
        if len(line) < 2 or line[0] not in " +-*=!" or line[1] != "\t":
            continue
        fields = line.split("\t")
        dst = fields[1].rpartition(":")[2] if len(fields) > 1 else ""
        results.append(PushResult(dst, line[0], fields[2] if len(fields) > 2 else ""))
    return results


# --- fusion à trois des refs ---------------------------------------------------------------------


@dataclass(frozen=True)
class RefUpdate:
    ref: str
    old: str | None  # valeur actuelle dans GitLab (None : ref à créer)
    new: str | None  # valeur à pousser (None : ref à supprimer)


def plan_import(
    base: dict[str, str] | None,
    file: dict[str, str],
    gitlab: dict[str, str],
    is_ancestor: Callable[[str, str], bool],
) -> tuple[list[RefUpdate], list[str]]:
    """Mises à jour à pousser dans GitLab pour y reporter les changements du fichier.

    base : refs au dernier cycle (None au premier : rien n'est alors supprimé).
    is_ancestor(a, b) : a est un ancêtre de b (ou égal).
    Retourne les mises à jour et les avertissements.
    """
    updates: list[RefUpdate] = []
    warnings: list[str] = []
    known = base or {}
    for ref in sorted(set(file) | set(known)):
        f, b, g = file.get(ref), known.get(ref), gitlab.get(ref)
        if base is not None and f == b:
            continue  # pas modifiée côté fichier
        if f == g:
            continue  # déjà à jour dans GitLab
        if f is None:  # supprimée du fichier depuis la base
            if g == b:
                updates.append(RefUpdate(ref, g, None))
            continue  # modifiée dans GitLab entre-temps : conservée
        if g is None:
            updates.append(RefUpdate(ref, None, f))  # nouvelle dans le fichier
        elif ref.startswith(TAGS):
            warnings.append(f"{short(ref)} : cible différente dans GitLab, version de GitLab conservée")
        elif is_ancestor(g, f):
            updates.append(RefUpdate(ref, g, f))  # avance rapide
        elif not is_ancestor(f, g):
            warnings.append(
                f"{short(ref)} : divergente entre le fichier et GitLab, version de GitLab conservée "
                "(l'autre reste dans l'historique git du projet)"
            )
        # sinon GitLab est en avance : l'export mettra le fichier à jour
    return updates, warnings


# --- API GitLab ----------------------------------------------------------------------------------


class GitLab:
    """Accès minimal à l'API REST v4 (bibliothèque standard), avec un jeton administrateur."""

    def __init__(self, url: str, token: str, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None
    ) -> tuple[Any, Any]:
        url = f"{self.url}/api/v4{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"PRIVATE-TOKEN": self.token, "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return (json.loads(raw) if raw else None), response.headers
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                payload = json.loads(raw)
                message = payload.get("message") or payload.get("error") or payload
            except (ValueError, AttributeError):
                message = raw.strip()[:200] or e.reason
            if not isinstance(message, str):
                message = json.dumps(message, ensure_ascii=False)
            raise ApiError(e.code, message) from None

    def _get_or_none(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self._request("GET", path, params)[0]
        except ApiError as e:
            if e.status == 404:
                return None
            raise

    def current_user(self) -> dict[str, Any]:
        return self._request("GET", "/user")[0]

    def list_projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        page: str | None = "1"
        while page:
            data, headers = self._request(
                "GET", "/projects", {"per_page": 100, "page": page, "order_by": "id", "sort": "asc"}
            )
            projects.extend(data or [])
            page = headers.get("X-Next-Page") or None
        return projects

    def get_project(self, project_id: int) -> dict[str, Any]:
        return self._request("GET", f"/projects/{project_id}")[0]

    def find_namespace(self, full_path: str) -> dict[str, Any] | None:
        return self._get_or_none(f"/namespaces/{urllib.parse.quote(full_path, safe='')}")

    def find_user(self, username: str) -> dict[str, Any] | None:
        users = self._get_or_none("/users", {"username": username}) or []
        return users[0] if users else None

    def create_group(self, name: str, path: str, parent_id: int | None) -> dict[str, Any]:
        data: dict[str, Any] = {"name": name, "path": path, "visibility": "private"}
        if parent_id is not None:
            data["parent_id"] = parent_id
        group = self._request("POST", "/groups", data=data)[0]
        return {"id": group["id"], "kind": "group", "full_path": group["full_path"]}

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
        data: dict[str, Any] = {
            "name": name,
            "path": path,
            "description": description,
            "visibility": visibility,
            "initialize_with_readme": False,
        }
        if default_branch:
            # HEAD du dépôt créé : posée avant le premier push. Un PUT après coup serait défait par
            # le traitement asynchrone du premier push (ProtectDefaultBranchService → change_head).
            data["default_branch"] = default_branch
        if namespace.get("kind") == "user":
            user = self.find_user(namespace["full_path"])
            if user is None:
                raise SyncError(f"utilisateur {namespace['full_path']} introuvable")
            return self._request("POST", f"/projects/user/{user['id']}", data=data)[0]
        data["namespace_id"] = namespace["id"]
        return self._request("POST", "/projects", data=data)[0]

    def update_project(self, project_id: int, **attributes: Any) -> None:
        self._request("PUT", f"/projects/{project_id}", data=attributes)

    def remote_url(self, project: dict[str, Any]) -> str:
        return f"{self.url}/{project['path_with_namespace']}.git"


def git_auth_env(token: str) -> dict[str, str]:
    """Authentification HTTP de git par variables d'environnement : le jeton n'apparaît ni dans les
    URL ni dans les arguments des commandes."""
    basic = base64.b64encode(f"root:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


# --- synchronisation -----------------------------------------------------------------------------


class Syncer:
    def __init__(self, gitlab: Any, repos_dir: Path, cache_dir: Path, git_env: dict[str, str] | None = None) -> None:
        self.gitlab = gitlab
        self.repos_dir = repos_dir
        self.cache_dir = cache_dir
        self.git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(git_env or {})}
        self.base: dict[str, Snapshot] = {}
        self._warned: set[tuple[str, str]] = set()

    # -- journal --------------------------------------------------------------------------------
    def warn(self, path: str, message: str) -> None:
        """Avertissement journalisé une seule fois tant qu'il se répète."""
        if (path, message) not in self._warned:
            self._warned.add((path, message))
            log.warning("%s : %s", path, message)

    # -- git ------------------------------------------------------------------------------------
    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=self.git_env,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise SyncError(f"git {args[0]} : délai de {GIT_TIMEOUT} s dépassé") from None
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise SyncError(f"git {args[0]} : {detail[-1] if detail else f'code {result.returncode}'}")
        return result

    def mirror(self, project_id: int) -> Path:
        path = self.cache_dir / "mirrors" / f"{project_id}.git"
        if not (path / "HEAD").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.git("-c", "init.defaultBranch=main", "init", "--quiet", "--bare", str(path))
        return path

    def local_refs(self, mirror: Path, prefix: str = "") -> dict[str, str]:
        out = self.git("for-each-ref", "--format=%(objectname) %(refname)", cwd=mirror).stdout
        if not prefix:
            return parse_refs(out)
        refs = {}
        for line in out.splitlines():
            sha, _, ref = line.partition(" ")
            if ref.startswith(prefix):
                refs["refs/" + ref[len(prefix) :]] = sha  # refs/import/heads/x -> refs/heads/x
        return refs

    def fetch_gitlab(self, mirror: Path, url: str) -> dict[str, str]:
        """Branches et tags de GitLab copiés dans le miroir ; renvoie les refs."""
        self.git(
            "fetch", "--quiet", "--prune", "--no-tags", url,
            "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
            cwd=mirror,
        )
        return self.local_refs(mirror)

    def fetch_bundle(self, mirror: Path, bundle: Path) -> dict[str, str]:
        """Branches et tags du bundle chargés dans le miroir sous refs/import/ ; renvoie les refs."""
        if not bundle.is_file():
            raise BundleError(f"{bundle.name} absent")
        try:
            self.git(
                "fetch", "--quiet", "--prune", "--no-tags", str(bundle),
                f"+refs/heads/*:{IMPORTED}heads/*", f"+refs/tags/*:{IMPORTED}tags/*",
                cwd=mirror,
            )
        except SyncError as e:
            raise BundleError(f"{bundle.name} illisible ({e})") from None
        return self.local_refs(mirror, IMPORTED)

    def is_ancestor(self, mirror: Path, ancestor: str, descendant: str) -> bool:
        result = self.git("merge-base", "--is-ancestor", ancestor, descendant, cwd=mirror, check=False)
        return result.returncode == 0

    def push(self, mirror: Path, url: str, updates: list[RefUpdate]) -> list[PushResult]:
        """Pousse sans forcer : GitLab refuse toute mise à jour qui n'est pas une avance rapide."""
        refspecs = [f"{u.new or ''}:{u.ref}" for u in updates]
        result = self.git("push", "--porcelain", url, *refspecs, cwd=mirror, check=False)
        results = parse_push_porcelain(result.stdout)
        if result.returncode != 0 and not any(not r.ok for r in results):
            detail = result.stderr.strip().splitlines()
            raise SyncError(f"git push : {detail[-1] if detail else f'code {result.returncode}'}")
        return results

    # -- fichiers -------------------------------------------------------------------------------
    def paths(self, path: str) -> tuple[Path, Path]:
        base = self.repos_dir.joinpath(*path.split("/"))
        return base.with_name(base.name + ".json"), base.with_name(base.name + ".bundle")

    def scan_files(self) -> dict[str, Snapshot | InvalidFile]:
        files: dict[str, Snapshot | InvalidFile] = {}
        for json_path in sorted(self.repos_dir.rglob("*.json")):
            rel = json_path.relative_to(self.repos_dir).with_suffix("")
            if any(part.startswith(".") for part in rel.parts) or len(rel.parts) < 2:
                continue
            try:
                files[rel.as_posix()] = Snapshot.from_json(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                files[rel.as_posix()] = InvalidFile(f"{json_path.name} illisible ({e})")
        return files

    def _owner(self) -> tuple[int, int] | None:
        try:
            st = self.repos_dir.stat()
        except OSError:
            return None
        return st.st_uid, st.st_gid

    def _chown(self, path: Path) -> None:
        """Les fichiers appartiennent au propriétaire de gitlab/repos/ (pas à root)."""
        owner = self._owner()
        if owner is not None:
            with contextlib.suppress(PermissionError):
                os.chown(path, *owner)

    def _mkdirs(self, path: Path) -> None:
        missing = []
        p = path
        while not p.exists():
            missing.append(p)
            p = p.parent
        path.mkdir(parents=True, exist_ok=True)
        for d in reversed(missing):
            self._chown(d)

    def _replace(self, tmp: Path, target: Path) -> None:
        self._chown(tmp)
        os.replace(tmp, target)

    def write_files(self, path: str, mirror: Path, state: Snapshot) -> None:
        json_path, bundle_path = self.paths(path)
        self._mkdirs(json_path.parent)
        if state.refs:
            tmp = bundle_path.with_name(bundle_path.name + ".tmp")
            revs = ["--branches", "--tags"]
            if state.default_branch:
                self.git("symbolic-ref", "HEAD", f"{HEADS}{state.default_branch}", cwd=mirror)
                revs.insert(0, "HEAD")  # git clone du bundle extrait la branche par défaut
            self.git("bundle", "create", str(tmp), *revs, cwd=mirror)
            self._replace(tmp, bundle_path)
        else:
            bundle_path.unlink(missing_ok=True)
        tmp = json_path.with_name(json_path.name + ".tmp")
        tmp.write_text(state.to_json(), encoding="utf-8")
        self._replace(tmp, json_path)  # écrit en dernier : le JSON fait foi

    def remove_files(self, path: str) -> None:
        json_path, bundle_path = self.paths(path)
        json_path.unlink(missing_ok=True)
        bundle_path.unlink(missing_ok=True)
        parent = json_path.parent
        while parent != self.repos_dir and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    def cleanup_tmp(self) -> None:
        """Fichiers temporaires laissés par un arrêt brutal."""
        for tmp in self.repos_dir.rglob("*.tmp"):
            tmp.unlink(missing_ok=True)

    # -- cycle ----------------------------------------------------------------------------------
    def cycle(self) -> int:
        """Un passage sur tous les projets ; renvoie le nombre de projets en erreur."""
        projects = {
            p["path_with_namespace"]: p
            for p in self.gitlab.list_projects()
            # projet supprimé, en attente de suppression définitive (renommé et archivé)
            if not p.get("marked_for_deletion_at") and not p.get("marked_for_deletion_on")
        }
        files = self.scan_files()
        errors = 0
        for path in sorted(set(projects) | set(files)):
            try:
                self.sync_project(path, projects.get(path), files.get(path))
            except SyncError as e:
                errors += 1
                self.warn(path, str(e))
            except Exception:  # noqa: BLE001 - un projet ne doit pas bloquer les autres
                errors += 1
                log.exception("%s : erreur inattendue", path)
        self._cleanup_mirrors({p["id"] for p in projects.values()})
        return errors

    def sync_project(
        self, path: str, project: dict[str, Any] | None, file_state: Snapshot | InvalidFile | None
    ) -> None:
        if isinstance(file_state, InvalidFile):
            self.warn(path, f"{file_state.reason} : projet ignoré")
            return
        base = self.base.get(path)
        if file_state is not None and file_state != base:
            if project is None:
                project = self.restore(path, file_state)
            else:
                self.import_refs(path, project, file_state, base)
                project = self.gitlab.get_project(project["id"])
        elif project is None:
            if base is not None:  # vu dans GitLab au cycle précédent, supprimé depuis
                self.remove_files(path)
                log.info("%s : supprimé de GitLab, fichiers retirés", path)
            self.base.pop(path, None)
            return
        self.base[path] = self.export(path, project, file_state)
        self._warned = {w for w in self._warned if w[0] != path}

    def export(self, path: str, project: dict[str, Any], file_state: Snapshot | None) -> Snapshot:
        mirror = self.mirror(project["id"])
        state = Snapshot.from_project(project, self.fetch_gitlab(mirror, self.gitlab.remote_url(project)))
        if state != file_state:
            self.write_files(path, mirror, state)
            log.info("%s : exporté (%s)", path, describe(file_state, state))
        return state

    def import_refs(self, path: str, project: dict[str, Any], file_state: Snapshot, base: Snapshot | None) -> None:
        url = self.gitlab.remote_url(project)
        mirror = self.mirror(project["id"])
        gitlab_refs = self.fetch_gitlab(mirror, url)
        _, bundle_path = self.paths(path)
        loaded = False

        def load_bundle() -> None:
            nonlocal loaded
            if not loaded:
                self.fetch_bundle(mirror, bundle_path)
                loaded = True

        def is_ancestor(ancestor: str, descendant: str) -> bool:
            load_bundle()
            return self.is_ancestor(mirror, ancestor, descendant)

        try:
            updates, warnings = plan_import(
                base.ref_map if base is not None else None, file_state.ref_map, gitlab_refs, is_ancestor
            )
            if any(u.new for u in updates):
                load_bundle()
        except BundleError as e:
            self.warn(path, f"{e} : import impossible, état de GitLab conservé")
            return
        for message in warnings:
            self.warn(path, message)
        if updates:
            self._push(path, mirror, url, updates)

    def restore(self, path: str, file_state: Snapshot) -> dict[str, Any]:
        namespace_path, _, project_path = path.rpartition("/")
        namespace = self.ensure_namespace(namespace_path, file_state.namespace_kind)
        attributes = {
            "name": file_state.name or project_path,
            "path": project_path,
            "description": file_state.description,
            "visibility": file_state.visibility,
            "default_branch": file_state.default_branch,
        }
        try:
            project = self.gitlab.create_project(namespace, **attributes)
        except ApiError as e:
            if e.status != 400 or "visibility" not in e.message.lower() or file_state.visibility == "private":
                raise
            self.warn(path, f"visibilité {file_state.visibility} refusée ({e.message}) : projet créé en privé")
            project = self.gitlab.create_project(namespace, **{**attributes, "visibility": "private"})
        log.info("%s : projet recréé dans GitLab (%s)", path, describe(None, file_state))
        if file_state.refs:
            mirror = self.mirror(project["id"])
            _, bundle_path = self.paths(path)
            try:
                self.fetch_bundle(mirror, bundle_path)
            except BundleError as e:
                self.warn(path, f"{e} : projet recréé vide")
            else:
                updates = [RefUpdate(ref, None, sha) for ref, sha in file_state.refs]
                self._push(path, mirror, self.gitlab.remote_url(project), updates)
                project = self.gitlab.get_project(project["id"])
                if file_state.default_branch and project.get("default_branch") != file_state.default_branch:
                    self.gitlab.update_project(project["id"], default_branch=file_state.default_branch)
        return self.gitlab.get_project(project["id"])

    def ensure_namespace(self, namespace_path: str, kind: str) -> dict[str, Any]:
        namespace = self.gitlab.find_namespace(namespace_path)
        if namespace is not None:
            return namespace
        if kind == "user":
            raise SyncError(
                f"utilisateur {namespace_path} absent de GitLab (à ajouter à GITLAB_USERS) : projet non restauré"
            )
        parent: dict[str, Any] | None = None
        parts = namespace_path.split("/")
        for i, part in enumerate(parts):
            full_path = "/".join(parts[: i + 1])
            namespace = self.gitlab.find_namespace(full_path)
            if namespace is None:
                namespace = self.gitlab.create_group(part, part, parent["id"] if parent else None)
                log.info("%s : groupe créé dans GitLab", full_path)
            parent = namespace
        assert parent is not None
        return parent

    def _push(self, path: str, mirror: Path, url: str, updates: list[RefUpdate]) -> None:
        results = self.push(mirror, url, updates)
        done = [r for r in results if r.ok and r.flag != "="]
        if done:
            log.info("%s : importé dans GitLab (%s)", path, ", ".join(f"{short(r.ref)} {r.summary}" for r in done))
        for r in results:
            if not r.ok:
                self.warn(path, f"{short(r.ref)} refusée par GitLab : {r.summary}")

    def _cleanup_mirrors(self, project_ids: set[int]) -> None:
        mirrors = self.cache_dir / "mirrors"
        if not mirrors.is_dir():
            return
        for entry in mirrors.iterdir():
            if entry.suffix == ".git" and entry.stem.isdigit() and int(entry.stem) not in project_ids:
                shutil.rmtree(entry, ignore_errors=True)

    # -- attente de GitLab ------------------------------------------------------------------------
    def wait_ready(self, stop: threading.Event, timeout: float | None = None) -> bool:
        """Attend que l'API réponde avec le jeton d'un administrateur : le provisioning le crée en
        dernier, il marque donc la fin de la configuration de base."""
        start = time.monotonic()
        last_reason, last_log = None, 0.0
        while not stop.is_set():
            try:
                user = self.gitlab.current_user()
                if user.get("is_admin"):
                    log.info("GitLab prêt (jeton de %s)", user.get("username"))
                    return True
                reason = "le jeton GITLAB_API_TOKEN n'est pas celui d'un administrateur"
            except ApiError as e:
                reason = "jeton refusé : configuration de base en cours ?" if e.status == 401 else str(e)
            except OSError as e:
                reason = f"GitLab injoignable ({getattr(e, 'reason', e)})"
            except ValueError as e:
                reason = f"réponse inattendue de GitLab ({e})"
            now = time.monotonic()
            if reason != last_reason or now - last_log >= 60:
                log.info("en attente de GitLab : %s", reason)
                last_reason, last_log = reason, now
            if timeout is not None and now - start >= timeout:
                return False
            stop.wait(10)
        return False


# --- point d'entrée ------------------------------------------------------------------------------


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Un seul cycle à la fois (boucle du service et make gitlab-sync)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.prefix = "" if record.levelno <= logging.INFO else f"{record.levelname} "
        return super().format(record)


class Recorder(logging.Handler):
    """Messages journalisés pendant un cycle, repris dans son compte rendu."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(Formatter("%(prefix)s%(message)s"))
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


def run_cycle(syncer: Syncer, lock: Path, status: Path) -> int:
    """Un cycle du service ; son compte rendu (début, fin, erreurs, messages) est écrit dans status,
    où « trigger » l'attend."""
    recorder = Recorder()
    log.addHandler(recorder)
    started, errors = time.time(), 0
    try:
        with locked(lock):
            errors = syncer.cycle()
    except (SyncError, OSError) as e:
        errors = 1
        log.warning("cycle interrompu : %s", e)
    finally:
        log.removeHandler(recorder)
    report = {"started_at": started, "finished_at": time.time(), "errors": errors, "messages": recorder.messages}
    tmp = status.with_name(status.name + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, status)
    return errors


def wait_for_cycle(status: Path, since: float, timeout: float, poll: float = 0.5) -> dict[str, Any] | None:
    """Compte rendu du premier cycle commencé après « since », ou None si le délai est dépassé."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            report = json.loads(status.read_text(encoding="utf-8"))
            if report.get("started_at", 0) >= since:
                return report
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def trigger(status: Path, timeout: float = 120) -> int:
    """Déclenche un cycle du service (PID 1 du conteneur) et affiche son compte rendu. Contrairement
    à « once », le service connaît l'état du cycle précédent : un projet supprimé dans GitLab voit ses
    fichiers supprimés au lieu d'être recréé."""
    since = time.time()
    try:
        os.kill(1, signal.SIGUSR1)
    except OSError as e:
        log.error("service gitlab-sync introuvable (%s) : à lancer dans son conteneur", e)
        return 1
    report = wait_for_cycle(status, since, timeout)
    if report is None:
        log.error("aucun cycle terminé en %g s : GitLab est-il prêt ? (docker compose logs gitlab-sync)", timeout)
        return 1
    for message in report.get("messages") or ["rien à synchroniser"]:
        print(message)
    return 1 if report.get("errors") else 0


def main(argv: list[str]) -> int:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(Formatter("%(asctime)s [gitlab-sync] %(prefix)s%(message)s", "%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    command = argv[1] if len(argv) > 1 else "run"
    if command not in ("run", "once", "trigger"):
        print(__doc__)
        return 2
    cache_dir = Path(os.environ.get("CACHE_DIR", "/var/cache/gitlab-sync"))
    status = cache_dir / "last-cycle.json"
    if command == "trigger":
        return trigger(status)
    token = os.environ.get("GITLAB_API_TOKEN", "")
    if not token:
        log.error("GITLAB_API_TOKEN est vide")
        return 2
    repos_dir = Path(os.environ.get("REPOS_DIR", "/repos"))
    interval = max(5.0, float(os.environ.get("SYNC_INTERVAL", "60")))
    gitlab = GitLab(os.environ.get("GITLAB_URL", "http://gitlab"), token)
    syncer = Syncer(gitlab, repos_dir, cache_dir, git_auth_env(token))
    lock = cache_dir / "sync.lock"
    repos_dir.mkdir(parents=True, exist_ok=True)

    if command == "once":
        if not syncer.wait_ready(threading.Event(), timeout=60):
            log.error("GitLab n'est pas prêt")
            return 1
        with locked(lock):
            try:
                return 1 if syncer.cycle() else 0
            except (SyncError, OSError) as e:
                log.error("synchronisation interrompue : %s", e)
                return 1

    stop, wake = threading.Event(), threading.Event()

    def on_stop(signum: int, _frame: Any) -> None:
        stop.set()
        wake.set()

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGUSR1, lambda _s, _f: wake.set())

    log.info("dépôts : %s, toutes les %g s", repos_dir, interval)
    with locked(lock):
        syncer.cleanup_tmp()
    if not syncer.wait_ready(stop):
        return 0
    while True:
        run_cycle(syncer, lock, status)
        if stop.is_set():
            break
        wake.wait(interval)  # SIGUSR1 (trigger) l'interrompt
        wake.clear()
        if stop.is_set():
            log.info("arrêt : dernière synchronisation")
            run_cycle(syncer, lock, status)
            break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
