"""Fonctions pures : état d'un projet, sorties de git, fusion à trois des refs, client de l'API."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from gitlab_sync import (
    ApiError,
    GitLab,
    RefUpdate,
    Snapshot,
    describe,
    git_auth_env,
    parse_push_porcelain,
    parse_refs,
    plan_import,
)

A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40


def snap(refs: dict[str, str], **kw: Any) -> Snapshot:
    attrs = {"name": "demo", "description": "", "visibility": "private", "namespace_kind": "group", "default_branch": "main"}
    attrs.update(kw)
    return Snapshot.build(refs=refs, **attrs)


# --- Snapshot -------------------------------------------------------------------------------------


def test_snapshot_json_is_stable_and_roundtrips() -> None:
    s = snap({"refs/tags/v1": B, "refs/heads/main": A}, description="Démo")
    text = s.to_json()
    assert text.endswith("\n")
    assert list(json.loads(text)) == sorted(json.loads(text))  # clés triées : diffs propres
    assert "Démo" in text  # pas d'échappement des accents
    assert Snapshot.from_json(text) == s
    assert s.to_json() == Snapshot.from_json(text).to_json()


def test_snapshot_normalizes_gitlab_values() -> None:
    project = {"name": "demo", "description": None, "visibility": "internal", "namespace": {"kind": "user"},
               "default_branch": "main"}
    s = Snapshot.from_project(project, {"refs/heads/dev": A, "HEAD": A, "refs/merge-requests/1/head": B})
    assert s.description == ""
    assert s.refs == (("refs/heads/dev", A),)  # ni HEAD ni refs internes de GitLab
    assert s.default_branch is None  # main n'existe pas : sans objet
    assert s.namespace_kind == "user"


@pytest.mark.parametrize("text", ["<<<<<<< HEAD", "[]", '{"refs": []}', '{"refs": {"refs/heads/main": 1}}'])
def test_snapshot_rejects_invalid_files(text: str) -> None:
    with pytest.raises(ValueError):
        Snapshot.from_json(text)


def test_describe() -> None:
    old = snap({"refs/heads/main": A, "refs/heads/old": B})
    new = snap({"refs/heads/main": C, "refs/tags/v1": D}, description="x")
    assert describe(old, new) == f"branche main {A[:8]}→{C[:8]}, -branche old, +tag v1, description"
    assert describe(None, new) == "1 branche(s), 1 tag(s)"


# --- sorties de git -------------------------------------------------------------------------------


def test_parse_refs_ls_remote_and_for_each_ref() -> None:
    out = f"{A}\tHEAD\n{A}\trefs/heads/main\n{B}\trefs/tags/v1\n{C}\trefs/tags/v1^{{}}\n{D}\trefs/merge-requests/1/head\n"
    assert parse_refs(out) == {"refs/heads/main": A, "refs/tags/v1": B}
    assert parse_refs(f"{A} refs/heads/feat/x\n") == {"refs/heads/feat/x": A}


def test_parse_push_porcelain() -> None:
    out = (
        "To /srv/demo.git\n"
        f"*\t{A}:refs/heads/new\t[new branch]\n"
        f" \t{B}:refs/heads/main\t{A[:7]}..{B[:7]}\n"
        "-\t:refs/heads/old\t[deleted]\n"
        f"!\t{C}:refs/heads/dev\t[rejected] (non-fast-forward)\n"
        f"=\t{D}:refs/tags/v1\t[up to date]\n"
        "Done\n"
    )
    results = parse_push_porcelain(out)
    assert [(r.ref, r.flag, r.ok) for r in results] == [
        ("refs/heads/new", "*", True),
        ("refs/heads/main", " ", True),
        ("refs/heads/old", "-", True),
        ("refs/heads/dev", "!", False),
        ("refs/tags/v1", "=", True),
    ]
    assert results[3].summary == "[rejected] (non-fast-forward)"


def test_git_auth_env_keeps_token_out_of_urls() -> None:
    env = git_auth_env("secret-token")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == "Authorization: Basic cm9vdDpzZWNyZXQtdG9rZW4="  # root:secret-token


# --- fusion à trois -------------------------------------------------------------------------------

# graphe : A ← B ← C (linéaire), D diverge de A
ANCESTRY = {(A, B), (A, C), (B, C), (A, D)}


def is_ancestor(x: str, y: str) -> bool:
    return x == y or (x, y) in ANCESTRY


def test_first_cycle_creates_and_fast_forwards_but_never_deletes() -> None:
    file = {"refs/heads/main": C, "refs/heads/new": A, "refs/tags/v2": B}
    gitlab = {"refs/heads/main": A, "refs/heads/only-in-gitlab": B}
    updates, warnings = plan_import(None, file, gitlab, is_ancestor)
    assert updates == [
        RefUpdate("refs/heads/main", A, C),
        RefUpdate("refs/heads/new", None, A),
        RefUpdate("refs/tags/v2", None, B),
    ]
    assert warnings == []


def test_gitlab_ahead_is_left_alone() -> None:
    updates, warnings = plan_import(None, {"refs/heads/main": A}, {"refs/heads/main": C}, is_ancestor)
    assert (updates, warnings) == ([], [])


def test_divergence_and_moved_tag_keep_gitlab_version() -> None:
    file = {"refs/heads/main": D, "refs/tags/v1": A}
    gitlab = {"refs/heads/main": C, "refs/tags/v1": B}
    updates, warnings = plan_import(None, file, gitlab, is_ancestor)
    assert updates == []
    assert len(warnings) == 2
    assert "branche main : divergente" in warnings[0]
    assert "tag v1 : cible différente" in warnings[1]


def test_with_base_only_file_changes_are_imported() -> None:
    base = {"refs/heads/main": A, "refs/heads/dev": A, "refs/heads/gone": A, "refs/heads/kept": A}
    file = {"refs/heads/main": A, "refs/heads/dev": B, "refs/heads/kept": A}  # dev avancée, gone supprimée
    gitlab = {"refs/heads/main": C, "refs/heads/dev": A, "refs/heads/gone": A, "refs/heads/kept": A}
    updates, _ = plan_import(base, file, gitlab, is_ancestor)
    # main : avancée dans GitLab seulement (rien à importer) ; dev : avance rapide ; gone : supprimée
    assert updates == [RefUpdate("refs/heads/dev", A, B), RefUpdate("refs/heads/gone", A, None)]


def test_deletion_in_file_does_not_remove_a_branch_changed_in_gitlab() -> None:
    base = {"refs/heads/main": A, "refs/heads/feat": A}
    file = {"refs/heads/main": A}
    gitlab = {"refs/heads/main": A, "refs/heads/feat": B}
    assert plan_import(base, file, gitlab, is_ancestor) == ([], [])


def test_branch_changed_in_file_but_deleted_in_gitlab_is_recreated() -> None:
    base = {"refs/heads/feat": A}
    updates, _ = plan_import(base, {"refs/heads/feat": B}, {}, is_ancestor)
    assert updates == [RefUpdate("refs/heads/feat", None, B)]


# --- client de l'API (serveur HTTP local) ---------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, Any, dict[str, str]]] = {}
    seen: list[tuple[str, str, Any, bytes]] = []

    def _handle(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Handler.seen.append((self.command, self.path, self.headers, body))  # en-têtes insensibles à la casse
        status, payload, headers = Handler.routes.get(f"{self.command} {self.path}", (404, {"message": "404 Not Found"}, {}))
        data = json.dumps(payload).encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = _handle

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def api() -> Any:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Handler.routes, Handler.seen = {}, []
    yield GitLab(f"http://127.0.0.1:{server.server_port}/", "tok")
    server.shutdown()


def test_api_paginates_and_authenticates(api: GitLab) -> None:
    page = "/api/v4/projects?per_page=100&page={}&order_by=id&sort=asc"
    Handler.routes = {
        f"GET {page.format(1)}": (200, [{"id": 1}], {"X-Next-Page": "2"}),
        f"GET {page.format(2)}": (200, [{"id": 2}], {"X-Next-Page": ""}),
    }
    assert [p["id"] for p in api.list_projects()] == [1, 2]
    assert all(headers.get("PRIVATE-TOKEN") == "tok" for _, _, headers, _ in Handler.seen)


def test_api_errors(api: GitLab) -> None:
    Handler.routes = {
        "POST /api/v4/projects": (400, {"message": {"visibility_level": ["internal is not allowed"]}}, {}),
        "GET /api/v4/namespaces/grp%2Fsub": (200, {"id": 7, "kind": "group", "full_path": "grp/sub"}, {}),
    }
    assert api.find_namespace("grp/missing") is None  # 404 -> None
    assert api.find_namespace("grp/sub")["id"] == 7  # chemin encodé
    with pytest.raises(ApiError) as excinfo:
        api.create_project({"id": 7, "kind": "group"}, name="x", path="x", description="", visibility="internal")
    assert excinfo.value.status == 400
    assert "visibility_level" in excinfo.value.message
    assert json.loads(Handler.seen[-1][3])["namespace_id"] == 7
