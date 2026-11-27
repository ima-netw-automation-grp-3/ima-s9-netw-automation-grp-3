"""Mode GitOps de l'interface : état publié par l'agent du lab, sauvegardes en merge request.

Quand l'infra du lab suit un projet GitLab (agent lab/gitops/, service clab), la conf de démarrage
vit dans Git : « Sauvegarder running → startup » ne réécrit plus lab/configs/ mais propose les
fichiers dans une merge request. L'agent les installera à la fusion.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable

from .errors import OperationFailed
from .settings import Settings

Transport = Callable[..., Any]


class GitLabHTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def urllib_transport(base: str, token: str) -> Transport:
    def call(method: str, path: str, *, params: dict[str, Any] | None = None, data: Any = None, raw: bool = False) -> Any:
        url = f"{base}/api/v4{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read()
        except urllib.error.HTTPError as e:
            raise GitLabHTTPError(e.code, e.read().decode(errors="replace")[:300]) from None
        except (urllib.error.URLError, OSError) as e:
            raise GitLabHTTPError(0, f"GitLab injoignable ({base}) : {e}") from None
        if raw:
            return content.decode("utf-8", errors="replace")
        return json.loads(content) if content else None

    return call


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class GitOps:
    def __init__(self, settings: Settings, transport: Transport | None = None) -> None:
        self.settings = settings
        self.transport = transport or urllib_transport(settings.gitlab_url, settings.gitlab_token)

    def state(self) -> dict[str, Any]:
        """État publié par l'agent (lab/.gitops/state.json) ; enabled = sauvegardes en merge request."""
        try:
            data = json.loads(self.settings.gitops_state_path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        active = bool(data.get("active")) and bool(self.settings.gitlab_token)
        keys = ("project", "project_url", "default_branch", "applied_sha", "status", "message", "updated_at")
        return {"enabled": active, **{k: data.get(k) for k in keys}}

    @property
    def enabled(self) -> bool:
        return self.state()["enabled"]

    async def propose(self, files: dict[str, str], *, routers: list[str], description: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._propose, files, routers, description)

    def _propose(self, files: dict[str, str], routers: list[str], description: str) -> dict[str, Any]:
        project_path = self.state().get("project") or self.settings.gitops_project_path
        try:
            project = self.transport("GET", f"/projects/{_q(project_path)}")
            pid, default = project["id"], project.get("default_branch") or "main"
            actions = []
            for path, content in sorted(files.items()):
                try:
                    current = self.transport(
                        "GET", f"/projects/{pid}/repository/files/{_q(path)}/raw", params={"ref": default}, raw=True
                    )
                except GitLabHTTPError as e:
                    if e.status != 404:
                        raise
                    current = None
                if current != content:
                    actions.append({"action": "update" if current is not None else "create", "file_path": path, "content": content})
            if not actions:
                return {"ok": True, "gitops": True, "changed": False, "message": "conf de démarrage déjà à jour dans Git"}
            slug = re.sub(r"[^a-z0-9-]+", "-", "-".join(routers).lower()).strip("-") or "routeurs"
            branch = f"interface/sauvegarde-{slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            title = f"Sauvegarde de la running : {', '.join(routers)}"
            self.transport("POST", f"/projects/{pid}/repository/branches", data={"branch": branch, "ref": default})
            self.transport(
                "POST",
                f"/projects/{pid}/repository/commits",
                data={"branch": branch, "commit_message": title, "actions": actions},
            )
            mr = self.transport(
                "POST",
                f"/projects/{pid}/merge_requests",
                data={
                    "source_branch": branch,
                    "target_branch": default,
                    "title": title,
                    "description": description,
                    "remove_source_branch": True,
                },
            )
        except GitLabHTTPError as e:
            detail = f"HTTP {e.status} : {e}" if e.status else str(e)
            raise OperationFailed(f"merge request impossible dans {project_path} : {detail}") from None
        return {
            "ok": True,
            "gitops": True,
            "changed": True,
            "branch": branch,
            "files": [a["file_path"] for a in actions],
            "merge_request": {"iid": mr.get("iid"), "web_url": mr.get("web_url")},
        }
