"""Client de l'API GitLab pour l'agent GitOps (bibliothèque standard ; le jeton n'est jamais journalisé)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class GitLabError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status} : {message}" if status else message)
        self.status = status


def quote(path: str) -> str:
    return urllib.parse.quote(path, safe="")


class GitLab:
    def __init__(self, url: str, token: str, timeout: float = 60.0) -> None:
        self.api = url.rstrip("/") + "/api/v4"
        self.token = token
        self.timeout = timeout

    # -- transport -------------------------------------------------------------------------------
    def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, data: Any = None, raw: bool = False
    ) -> Any:
        url = self.api + path
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
                content = response.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise GitLabError(e.code, detail) from None
        except (urllib.error.URLError, OSError) as e:
            raise GitLabError(0, f"GitLab injoignable ({self.api}) : {e}") from None
        if raw:
            return content
        return json.loads(content) if content else None

    def _get_or_none(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self._request("GET", path, params=params)
        except GitLabError as e:
            if e.status == 404:
                return None
            raise

    # -- lecture -----------------------------------------------------------------------------------
    def project(self, path: str) -> dict[str, Any] | None:
        return self._get_or_none(f"/projects/{quote(path)}")

    def branch_head(self, project_id: int, branch: str) -> str | None:
        data = self._get_or_none(f"/projects/{project_id}/repository/branches/{quote(branch)}")
        return data["commit"]["id"] if data else None

    def open_merge_requests(self, project_id: int, target: str) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/projects/{project_id}/merge_requests",
            params={"state": "opened", "target_branch": target, "per_page": 100},
        ) or []

    def archive(self, project_id: int, sha: str) -> bytes:
        return self._request("GET", f"/projects/{project_id}/repository/archive.tar.gz", params={"sha": sha}, raw=True)

    def commit_statuses(self, project_id: int, sha: str) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/projects/{project_id}/repository/commits/{sha}/statuses", params={"all": "true", "per_page": 100}
        ) or []

    def commit_merge_requests(self, project_id: int, sha: str) -> list[dict[str, Any]]:
        return self._get_or_none(f"/projects/{project_id}/repository/commits/{sha}/merge_requests") or []

    def notes(self, project_id: int, iid: int) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/projects/{project_id}/merge_requests/{iid}/notes", params={"per_page": 100, "sort": "asc"}
        ) or []

    # -- écriture ----------------------------------------------------------------------------------
    def set_status(
        self, project_id: int, sha: str, *, name: str, state: str, description: str, ref: str | None = None,
        target_url: str | None = None,
    ) -> None:
        data: dict[str, Any] = {"state": state, "name": name, "description": description[:250]}
        if ref:
            data["ref"] = ref
        if target_url:
            data["target_url"] = target_url
        self._request("POST", f"/projects/{project_id}/statuses/{sha}", data=data)

    def upsert_note(self, project_id: int, iid: int, marker: str, body: str) -> dict[str, Any]:
        """Crée la note de l'agent sur la MR, ou met à jour celle qui porte le marqueur."""
        for n in self.notes(project_id, iid):
            if marker in (n.get("body") or ""):
                return self._request("PUT", f"/projects/{project_id}/merge_requests/{iid}/notes/{n['id']}", data={"body": body})
        return self.add_note(project_id, iid, body)

    def add_note(self, project_id: int, iid: int, body: str) -> dict[str, Any]:
        return self._request("POST", f"/projects/{project_id}/merge_requests/{iid}/notes", data={"body": body})

    def create_deployment(self, project_id: int, *, sha: str, ref: str, environment: str) -> int | None:
        try:
            data = self._request(
                "POST",
                f"/projects/{project_id}/deployments",
                data={"environment": environment, "sha": sha, "ref": ref, "tag": False, "status": "running"},
            )
        except GitLabError:
            return None  # l'historique des déploiements est un plus, pas une condition
        return data.get("id") if data else None

    def finish_deployment(self, project_id: int, deployment_id: int | None, status: str) -> None:
        if deployment_id is None:
            return
        try:
            self._request("PUT", f"/projects/{project_id}/deployments/{deployment_id}", data={"status": status})
        except GitLabError:
            pass
