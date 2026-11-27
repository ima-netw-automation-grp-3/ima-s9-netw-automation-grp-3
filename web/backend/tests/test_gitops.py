"""Mode GitOps : « Sauvegarder running → startup » ouvre une merge request au lieu d'écrire lab/configs/."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.gitops import GitLabHTTPError
from app.main import create_app

from .conftest import make_settings


class FakeGitLabAPI:
    """Sous-ensemble de l'API GitLab utilisé par l'interface, en mémoire."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}  # fichiers de la branche par défaut
        self.branches: list[str] = []
        self.commits: list[dict[str, Any]] = []
        self.mrs: list[dict[str, Any]] = []
        self.down = False

    def __call__(self, method: str, path: str, *, params=None, data=None, raw=False) -> Any:
        if self.down:
            raise GitLabHTTPError(0, "GitLab injoignable")
        if method == "GET" and path == "/projects/grp%2Flab-infra":
            return {"id": 9, "default_branch": "main"}
        if method == "GET" and path.startswith("/projects/9/repository/files/"):
            name = path.split("/files/")[1].removesuffix("/raw").replace("%2F", "/")
            if name not in self.files:
                raise GitLabHTTPError(404, "absent")
            return self.files[name]
        if method == "POST" and path == "/projects/9/repository/branches":
            self.branches.append(data["branch"])
            return {}
        if method == "POST" and path == "/projects/9/repository/commits":
            self.commits.append(data)
            return {"id": "c" * 40}
        if method == "POST" and path == "/projects/9/merge_requests":
            mr = {"iid": len(self.mrs) + 1, "web_url": f"http://gitlab/grp/lab-infra/-/merge_requests/{len(self.mrs) + 1}", **data}
            self.mrs.append(mr)
            return mr
        raise AssertionError(f"appel inattendu : {method} {path}")


@pytest.fixture
def gitops_client(lab_dir, fake_docker):
    (lab_dir / ".gitops").mkdir()
    (lab_dir / ".gitops" / "state.json").write_text(json.dumps({
        "active": True, "project": "grp/lab-infra", "project_url": "http://gitlab/grp/lab-infra",
        "default_branch": "main", "applied_sha": "a" * 40, "status": "deployed", "message": "ok",
    }))
    app = create_app(make_settings(lab_dir, gitlab_token="t", gitlab_group="grp"), docker=fake_docker)
    api = FakeGitLabAPI()
    app.state.lab.gitops.transport = api
    with TestClient(app) as c:
        yield c, api, lab_dir


def test_state_without_agent(client):
    assert client.get("/api/lab").json()["gitops"]["enabled"] is False


def test_state_needs_token(lab_dir, fake_docker):
    (lab_dir / ".gitops").mkdir()
    (lab_dir / ".gitops" / "state.json").write_text(json.dumps({"active": True, "project": "grp/lab-infra"}))
    app = create_app(make_settings(lab_dir), docker=fake_docker)  # pas de GITLAB_API_TOKEN
    with TestClient(app) as c:
        assert c.get("/api/lab").json()["gitops"]["enabled"] is False


def test_save_opens_merge_request(gitops_client):
    c, api, lab_dir = gitops_client
    before = (lab_dir / "configs" / "sonic1" / "frr.conf").read_text()
    state = c.get("/api/lab").json()["gitops"]
    assert state["enabled"] and state["applied_sha"] == "a" * 40 and state["project_url"].endswith("lab-infra")

    res = c.post("/api/nodes/sonic1/config/save").json()
    assert res["gitops"] and res["changed"]
    assert res["merge_request"] == {"iid": 1, "web_url": "http://gitlab/grp/lab-infra/-/merge_requests/1"}
    assert sorted(res["files"]) == ["configs/sonic1/config_db.json", "configs/sonic1/frr.conf"]
    commit = api.commits[0]
    assert commit["branch"].startswith("interface/sauvegarde-sonic1-") and commit["branch"] == api.branches[0]
    assert {a["action"] for a in commit["actions"]} == {"create"}
    assert api.mrs[0]["target_branch"] == "main" and api.mrs[0]["remove_source_branch"] is True
    assert (lab_dir / "configs" / "sonic1" / "frr.conf").read_text() == before  # Git fait foi : rien en local

    # MR fusionnée : la branche par défaut contient ces fichiers ; une nouvelle sauvegarde n'a rien à proposer
    api.files = {a["file_path"]: a["content"] for a in commit["actions"]}
    res = c.post("/api/nodes/sonic1/config/save").json()
    assert res == {"ok": True, "gitops": True, "changed": False, "message": "conf de démarrage déjà à jour dans Git"}
    assert len(api.mrs) == 1


def test_save_all_is_one_merge_request(gitops_client):
    c, api, _ = gitops_client
    api.files = {"configs/sonic1/frr.conf": "ancienne conf\n"}
    res = c.post("/api/lab/save-all").json()
    assert res["changed"] and len(api.mrs) == 1 and "sonic1" in api.mrs[0]["title"]
    actions = {a["file_path"]: a["action"] for a in api.commits[0]["actions"]}
    assert actions["configs/sonic1/frr.conf"] == "update"


def test_gitlab_unreachable_is_an_error_not_a_local_save(gitops_client):
    c, api, lab_dir = gitops_client
    api.down = True
    before = (lab_dir / "configs" / "sonic1" / "config_db.json").read_text()
    res = c.post("/api/nodes/sonic1/config/save")
    assert res.status_code == 502 and "merge request impossible" in res.json()["error"]
    assert (lab_dir / "configs" / "sonic1" / "config_db.json").read_text() == before
