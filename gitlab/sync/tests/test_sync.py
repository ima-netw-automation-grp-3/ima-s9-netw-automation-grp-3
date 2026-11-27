"""Cycles complets avec un faux GitLab et de vrais dépôts git : export, restauration sur un poste
neuf, import après un git pull, divergences, suppressions."""

from __future__ import annotations

import json
import logging
import time

import pytest

from conftest import Machine, git


def make_demo(m: Machine, full_path: str = "grp/demo") -> dict[str, str]:
    """Projet avec main, dev (branche par défaut) et un tag annoté ; renvoie les commits."""
    namespace = full_path.rpartition("/")[0]
    if m.gitlab.find_namespace(namespace) is None:
        m.gitlab.add_group(namespace)
    m.gitlab.add_project(full_path, description="Démo réseau")
    w = m.work(full_path)
    c1 = w.commit("README.md", "v1")
    w.push("main")
    w.switch("dev", "main")
    c2 = w.commit("dev.txt", "dev")
    w.tag("v1")
    w.push("dev", "v1")
    m.gitlab.update_project(m.gitlab.project(full_path)["id"], default_branch="dev")
    return {"c1": c1, "c2": c2}


def doc(m: Machine, full_path: str) -> dict:
    return json.loads((m.repos / f"{full_path}.json").read_text())


# --- export ---------------------------------------------------------------------------------------


def test_export_writes_bundle_and_json_once(machines: tuple[Machine, Machine], tmp_path) -> None:
    a, _ = machines
    commits = make_demo(a)
    assert a.syncer.cycle() == 0

    data = doc(a, "grp/demo")
    assert data["refs"] == a.gitlab.refs("grp/demo")
    assert data["default_branch"] == "dev"
    assert data["description"] == "Démo réseau"
    assert data["namespace_kind"] == "group"

    clone = tmp_path / "clone"
    git("clone", "--quiet", str(a.repos / "grp/demo.bundle"), str(clone))
    assert git("rev-parse", "HEAD", cwd=clone) == commits["c2"]  # branche par défaut extraite
    assert git("rev-parse", "origin/main", cwd=clone) == commits["c1"]
    assert git("tag", cwd=clone) == "v1"

    before = {p: p.stat().st_mtime_ns for p in a.repos.rglob("*")}
    assert a.syncer.cycle() == 0
    assert {p: p.stat().st_mtime_ns for p in a.repos.rglob("*")} == before  # rien de réécrit


def test_empty_project_is_exported_without_bundle(machines: tuple[Machine, Machine]) -> None:
    a, b = machines
    a.gitlab.add_group("grp")
    a.gitlab.add_project("grp/vide")
    a.syncer.cycle()
    assert doc(a, "grp/vide")["refs"] == {}
    assert not (a.repos / "grp/vide.bundle").exists()

    b.pull_from(a)
    files = b.files()
    assert b.syncer.cycle() == 0
    assert b.gitlab.project("grp/vide") is not None
    assert b.files() == files


# --- restauration sur un poste neuf ----------------------------------------------------------------


def test_fresh_gitlab_gets_everything_back_without_rewriting_files(machines: tuple[Machine, Machine]) -> None:
    a, b = machines
    make_demo(a, "grp/sub/demo")  # sous-groupe
    a.syncer.cycle()

    b.pull_from(a)
    files = b.files()
    assert b.syncer.cycle() == 0

    assert b.gitlab.find_namespace("grp")["kind"] == "group"
    assert b.gitlab.find_namespace("grp/sub") is not None
    project = b.gitlab.project("grp/sub/demo")
    assert b.gitlab.refs("grp/sub/demo") == a.gitlab.refs("grp/sub/demo")
    assert project["default_branch"] == "dev"
    assert project["description"] == "Démo réseau"
    assert b.files() == files  # aucun export parasite : git status propre
    assert b.syncer.cycle() == 0
    assert b.files() == files


def test_default_branch_survives_gitlab_first_push_processing(machines: tuple[Machine, Machine]) -> None:
    """Le traitement asynchrone du premier push remet la branche par défaut sur la HEAD de création :
    elle doit donc être fixée à la création du projet, pas par un PUT après le push."""
    a, b = machines
    make_demo(a)  # branche par défaut : dev
    a.syncer.cycle()

    b.pull_from(a)
    files = b.files()
    b.syncer.cycle()
    project = b.gitlab.project("grp/demo")
    b.gitlab.after_first_push(project["id"])
    assert b.gitlab.project("grp/demo")["default_branch"] == "dev"
    assert b.gitlab.default_branch_puts == 0  # pas besoin du PUT de secours
    b.syncer.cycle()
    assert b.files() == files


def test_user_project_waits_for_its_user(machines: tuple[Machine, Machine], caplog) -> None:
    a, b = machines
    a.gitlab.add_user("alice")
    a.gitlab.add_project("alice/perso")
    w = a.work("alice/perso")
    w.commit("a.txt", "a")
    w.push("main")
    a.syncer.cycle()

    b.pull_from(a)
    files = b.files()
    with caplog.at_level(logging.WARNING):
        assert b.syncer.cycle() == 1
        assert b.syncer.cycle() == 1
    assert b.gitlab.project("alice/perso") is None
    assert b.files() == files  # ni supprimés ni réécrits
    assert sum("GITLAB_USERS" in r.getMessage() for r in caplog.records) == 1  # averti une seule fois

    b.gitlab.add_user("alice")
    assert b.syncer.cycle() == 0
    project = b.gitlab.project("alice/perso")
    assert project["namespace"]["kind"] == "user"
    assert b.gitlab.refs("alice/perso") == a.gitlab.refs("alice/perso")


def test_refused_visibility_falls_back_to_private(machines: tuple[Machine, Machine]) -> None:
    a, b = machines
    a.gitlab.add_group("grp")
    a.gitlab.add_project("grp/interne", visibility="internal")
    a.syncer.cycle()

    b.pull_from(a)
    b.gitlab.refuse_visibility = True
    assert b.syncer.cycle() == 0
    assert b.gitlab.project("grp/interne")["visibility"] == "private"
    assert doc(b, "grp/interne")["visibility"] == "private"


def test_missing_bundle_restores_an_empty_project(machines: tuple[Machine, Machine]) -> None:
    a, b = machines
    make_demo(a)
    a.syncer.cycle()

    b.pull_from(a)
    (b.repos / "grp/demo.bundle").unlink()
    assert b.syncer.cycle() == 0
    assert b.gitlab.refs("grp/demo") == {}
    assert doc(b, "grp/demo")["refs"] == {}  # GitLab a le dernier mot


# --- import après un git pull ---------------------------------------------------------------------


def test_pulled_changes_are_imported(machines: tuple[Machine, Machine]) -> None:
    a, b = machines
    make_demo(a)
    a.syncer.cycle()
    b.pull_from(a)
    b.syncer.cycle()

    w = a.work("grp/demo")  # sur A : main avance, nouvelle branche, nouveau tag
    w.commit("README.md", "v2")
    w.switch("feat", "main")
    w.commit("feat.txt", "feat")
    w.tag("v2")
    w.push("main", "feat", "v2")
    a.syncer.cycle()

    b.pull_from(a)
    files = b.files()
    assert b.syncer.cycle() == 0
    assert b.gitlab.refs("grp/demo") == a.gitlab.refs("grp/demo")
    assert b.files() == files


def test_pulled_changes_are_imported_after_a_restart(machines: tuple[Machine, Machine]) -> None:
    a, b = machines
    make_demo(a)
    a.syncer.cycle()
    b.pull_from(a)
    b.syncer.cycle()

    w = a.work("grp/demo")
    w.commit("README.md", "v2")
    w.push("main")
    a.syncer.cycle()

    b.restart()  # make down / make up : base perdue
    b.pull_from(a)
    assert b.syncer.cycle() == 0
    assert b.gitlab.refs("grp/demo") == a.gitlab.refs("grp/demo")


def test_divergence_keeps_gitlab_and_warns(machines: tuple[Machine, Machine], caplog) -> None:
    a, b = machines
    make_demo(a)
    a.syncer.cycle()
    b.pull_from(a)
    b.syncer.cycle()

    wa = a.work("grp/demo")
    wa.commit("README.md", "version A")
    wa.push("main")
    a.syncer.cycle()
    wb = b.work("grp/demo")
    head_b = wb.commit("README.md", "version B")
    wb.push("main")  # B pousse dans son GitLab sans attendre le cycle

    b.pull_from(a)
    with caplog.at_level(logging.WARNING):
        assert b.syncer.cycle() == 0
    assert b.gitlab.refs("grp/demo")["refs/heads/main"] == head_b
    assert doc(b, "grp/demo")["refs"]["refs/heads/main"] == head_b
    assert any("branche main : divergente" in r.getMessage() for r in caplog.records)


def test_branch_deleted_on_another_machine_is_deleted(machines: tuple[Machine, Machine]) -> None:
    a, b = machines
    make_demo(a)
    w = a.work("grp/demo")
    w.switch("feat", "origin/main")
    w.commit("feat.txt", "feat")
    w.push("feat")
    a.syncer.cycle()
    b.pull_from(a)
    b.syncer.cycle()
    assert "refs/heads/feat" in b.gitlab.refs("grp/demo")

    w.push(":feat")  # supprimée sur A
    a.syncer.cycle()
    b.pull_from(a)
    assert b.syncer.cycle() == 0
    assert "refs/heads/feat" not in b.gitlab.refs("grp/demo")


def test_first_cycle_never_deletes_in_gitlab(machines: tuple[Machine, Machine]) -> None:
    a, b = machines
    make_demo(a)
    a.syncer.cycle()
    b.pull_from(a)
    b.syncer.cycle()
    w = b.work("grp/demo")
    w.switch("local", "origin/main")
    w.commit("local.txt", "local")
    w.push("local")  # branche que les fichiers ne connaissent pas encore

    b.restart()
    assert b.syncer.cycle() == 0
    assert "refs/heads/local" in b.gitlab.refs("grp/demo")
    assert "refs/heads/local" in doc(b, "grp/demo")["refs"]  # exportée


# --- suppressions ---------------------------------------------------------------------------------


def test_project_deleted_in_gitlab_removes_its_files(machines: tuple[Machine, Machine]) -> None:
    a, _ = machines
    make_demo(a, "grp/sub/demo")
    (a.repos / "README.md").write_text("doc")
    a.syncer.cycle()
    a.gitlab.delete_project("grp/sub/demo")
    assert a.syncer.cycle() == 0
    assert not (a.repos / "grp").exists()  # dossiers vides retirés
    assert (a.repos / "README.md").exists()


def test_project_marked_for_deletion_is_treated_as_deleted(machines: tuple[Machine, Machine]) -> None:
    a, _ = machines
    make_demo(a)
    a.syncer.cycle()
    project = a.gitlab.project("grp/demo")
    a.gitlab.projects[project["id"]]["marked_for_deletion_at"] = "2026-11-20T10:00:00Z"
    a.syncer.cycle()
    assert not (a.repos / "grp/demo.json").exists()


def test_files_are_not_deleted_at_startup(machines: tuple[Machine, Machine]) -> None:
    a, _ = machines
    make_demo(a)
    a.syncer.cycle()
    a.gitlab.delete_project("grp/demo")
    a.restart()  # au premier cycle, un fichier sans projet est restauré, pas supprimé
    assert a.syncer.cycle() == 0
    assert a.gitlab.project("grp/demo") is not None


def test_deleted_file_is_exported_again(machines: tuple[Machine, Machine]) -> None:
    a, _ = machines
    make_demo(a)
    a.syncer.cycle()
    files = a.files()
    (a.repos / "grp/demo.json").unlink()
    (a.repos / "grp/demo.bundle").unlink()
    assert a.syncer.cycle() == 0
    assert a.gitlab.project("grp/demo") is not None
    assert json.loads(a.files()["grp/demo.json"]) == json.loads(files["grp/demo.json"])


# --- fichiers -------------------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["<<<<<<< HEAD\n", "{}garbage"])
def test_unreadable_json_freezes_the_project(machines: tuple[Machine, Machine], caplog, content: str) -> None:
    a, _ = machines
    make_demo(a)
    a.syncer.cycle()
    (a.repos / "grp/demo.json").write_text(content)
    w = a.work("grp/demo")
    w.commit("README.md", "v2")
    w.push("main")
    with caplog.at_level(logging.WARNING):
        a.syncer.cycle()
    assert (a.repos / "grp/demo.json").read_text() == content  # ni écrasé ni supprimé
    assert any("illisible" in r.getMessage() for r in caplog.records)


def test_stale_temporary_files_are_removed(machines: tuple[Machine, Machine]) -> None:
    a, _ = machines
    (a.repos / "grp").mkdir()
    (a.repos / "grp/demo.json.tmp").write_text("{")
    a.syncer.cleanup_tmp()
    assert not (a.repos / "grp/demo.json.tmp").exists()


# --- compte rendu de cycle (make gitlab-sync) ------------------------------------------------------


def test_cycle_report_is_what_trigger_waits_for(machines: tuple[Machine, Machine], tmp_path) -> None:
    from gitlab_sync import run_cycle, wait_for_cycle

    a, _ = machines
    make_demo(a)
    status = tmp_path / "last-cycle.json"
    since = time.time()
    assert run_cycle(a.syncer, tmp_path / "sync.lock", status) == 0
    report = wait_for_cycle(status, since, timeout=1)
    assert report is not None
    assert report["errors"] == 0
    assert report["messages"] == ["grp/demo : exporté (2 branche(s), 1 tag(s))"]
    # un cycle commencé avant la demande ne compte pas
    assert wait_for_cycle(status, time.time() + 60, timeout=0.2, poll=0.05) is None
    assert run_cycle(a.syncer, tmp_path / "sync.lock", status) == 0
    assert json.loads(status.read_text())["messages"] == []  # rien à synchroniser
