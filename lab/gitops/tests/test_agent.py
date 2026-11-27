import io
import json
import os
import tarfile

from conftest import add_client3, lab_files, make_archive, with_topology

from gitops import agent as agentmod
from gitops.plan import DEPLOY_MARKER, NOTE_MARKER


def adopt(env):
    """Le projet vient d'être créé depuis le lab : même contenu, adopté sans déploiement."""
    env.gitlab.commit("a" * 40, env.files, ci=None)
    env.agent.cycle()
    assert env.agent.state["applied_sha"] == "a" * 40


def test_inactive_without_project(env):
    env.gitlab.exists = False
    env.agent.cycle()
    assert env.agent.state["status"] == "inactive" and env.agent.state["active"] is False
    assert env.runner.calls == []


def test_waits_while_runner_busy(env):
    (env.lab / ".runner-status").write_text("deploying\n")
    env.gitlab.commit("a" * 40, env.files)
    env.agent.cycle()
    assert "applied_sha" not in env.agent.state


def test_adoption_without_deploy(env):
    adopt(env)
    assert env.runner.calls == []
    assert env.gitlab.last_status("lab/deploy", "a" * 40)["state"] == "success"
    state = json.loads((env.lab / ".gitops" / "state.json").read_text())
    assert state["status"] == "deployed" and state["project_url"].endswith("lab-infra")


def test_merge_request_plan(env):
    adopt(env)
    new = with_topology(env.files, add_client3)
    env.gitlab.commits["b" * 40] = new
    env.gitlab.mrs.append({"iid": 3, "sha": "b" * 40, "source_branch": "client3", "web_url": "http://gitlab/mr/3"})
    env.runner.plan = {"added-nodes": ["client3"], "recreated-nodes": ["sonic2"], "node-change-reasons": {"sonic2": "added link"},
                       "added-links": ["client3:eth1 -- sonic2:eth4"]}
    env.agent.cycle()
    note = env.gitlab.notes[3][0]
    assert NOTE_MARKER in note["body"] and "`client3`" in note["body"] and "```mermaid" in note["body"]
    status = env.gitlab.last_status("lab/plan", "b" * 40)
    assert status["state"] == "success" and status["ref"] == "client3" and "+1 nœud" in status["description"]
    assert env.runner.commands("containerlab deploy -t sonic-lab.clab.yml --dry-run")
    assert lab_files(env.lab) == env.files  # rien n'est appliqué par un plan
    n = len(env.runner.calls)
    env.agent.cycle()  # même commit, même état : pas de nouveau calcul
    assert len(env.runner.calls) == n
    env.gitlab.mrs[0]["sha"] = "c" * 40  # nouveau push : la note est mise à jour
    env.gitlab.commits["c" * 40] = new
    env.agent.cycle()
    assert len(env.gitlab.notes[3]) == 1 and env.gitlab.notes[3][0]["updates"] == 1


def test_merge_request_refused(env):
    adopt(env)
    bad = with_topology(env.files, lambda d: d["topology"]["nodes"]["client2"].update({"mgmt-ipv4": "172.20.30.21"}))
    env.gitlab.commits["b" * 40] = bad
    env.gitlab.mrs.append({"iid": 4, "sha": "b" * 40, "source_branch": "x"})
    env.agent.cycle()
    assert env.gitlab.last_status("lab/plan", "b" * 40)["state"] == "failed"
    assert "Refusé" in env.gitlab.notes[4][0]["body"]
    assert not env.runner.commands("containerlab")  # pas de dry-run sur une topologie refusée


def test_deploy_topology_change(env):
    adopt(env)
    new = with_topology(env.files, add_client3)
    env.gitlab.commit("b" * 40, new)
    env.gitlab.merged["b" * 40] = 5
    env.runner.plan = {"added-nodes": ["client3"], "recreated-nodes": ["sonic2"], "added-links": ["client3:eth1 -- sonic2:eth4"]}
    env.agent.cycle()
    assert lab_files(env.lab) == new
    deploys = env.runner.commands("containerlab deploy -t sonic-lab.clab.yml")
    assert len(deploys) == 2 and "--dry-run" not in deploys[1]  # plan, puis réconciliation
    assert not env.runner.commands("containerlab destroy")
    assert env.agent.state["applied_sha"] == "b" * 40 and env.agent.state["status"] == "deployed"
    assert [s["state"] for s in env.gitlab.statuses if s["name"] == "lab/deploy" and s["sha"] == "b" * 40] == ["running", "success"]
    assert list(env.gitlab.deployments.values()) == ["success"]
    assert DEPLOY_MARKER in env.gitlab.notes[5][0]["body"] and "✔ Déployé" in env.gitlab.notes[5][0]["body"]
    assert (env.lab / ".runner-status").read_text().strip() == "deployed"
    assert any((env.lab / ".gitops" / "backups").iterdir())


def test_redefined_node_is_destroyed_first(env):
    adopt(env)
    new = with_topology(env.files, lambda d: d["topology"]["nodes"]["client1"]["exec"].append("echo x"))
    env.gitlab.commit("b" * 40, new)
    env.agent.cycle()
    cmds = [" ".join(c) for c in env.runner.calls if c[0] == "containerlab"]
    assert cmds[1] == "containerlab destroy --node-filter client1 -t sonic-lab.clab.yml"
    assert cmds[2] == "containerlab deploy -t sonic-lab.clab.yml"


def test_startup_config_only_is_restored_without_containerlab(env):
    adopt(env)
    new = dict(env.files)
    new["configs/sonic2/frr.conf"] = env.files["configs/sonic2/frr.conf"] + b"ip route 10.99.0.0/24 Null0\n"
    inode = os.stat(env.lab / "configs/sonic2/frr.conf").st_ino
    env.gitlab.commit("b" * 40, new)
    env.agent.cycle()
    assert env.web.restored == ["sonic2"]
    assert not [c for c in env.runner.calls if c[:2] == ["containerlab", "deploy"] and "--dry-run" not in c]
    assert (env.lab / "configs/sonic2/frr.conf").read_bytes().endswith(b"Null0\n")
    assert os.stat(env.lab / "configs/sonic2/frr.conf").st_ino == inode  # même inode : bind du routeur à jour


def test_ci_gate(env):
    adopt(env)
    new = with_topology(env.files, add_client3)
    env.gitlab.commit("b" * 40, new, ci="running")
    env.agent.cycle()
    assert env.agent.state["applied_sha"] == "a" * 40 and lab_files(env.lab) == env.files
    env.gitlab.ci["b" * 40] = [{"name": "validate", "status": "failed"}]
    env.agent.cycle()
    assert env.agent.state["failed_sha"] == "b" * 40
    assert "CI en échec" in env.gitlab.last_status("lab/deploy", "b" * 40)["description"]
    env.agent.cycle()  # pas de nouvelle tentative sur le même commit
    assert lab_files(env.lab) == env.files


def test_without_ci_deploys_after_grace(env):
    adopt(env)
    env.gitlab.commit("b" * 40, with_topology(env.files, add_client3), ci=None)
    env.agent.cycle()
    assert env.agent.state["applied_sha"] == "a" * 40
    env.clock.t += 31
    env.agent.cycle()
    assert env.agent.state["applied_sha"] == "b" * 40


def test_recreated_router_gets_bootstrap_if_missing(env):
    adopt(env)
    env.gitlab.commit("b" * 40, with_topology(env.files, add_client3))
    env.runner.plan = {"added-nodes": ["client3"], "recreated-nodes": ["sonic2"]}
    env.runner.not_ready = {"sonic2"}  # bootstrap non lancé par containerlab
    env.agent.cycle()
    assert env.runner.commands("docker exec clab-sonic-lab-sonic2 bash /usr/local/bin/lab-bootstrap.sh")
    assert env.agent.state["applied_sha"] == "b" * 40


def test_failure_rolls_back(env):
    adopt(env)
    new = with_topology(env.files, add_client3)
    env.gitlab.commit("b" * 40, new)
    env.gitlab.merged["b" * 40] = 6
    env.runner.plan = {"added-nodes": ["client3"]}
    env.runner.fail_once = {"deploy"}  # la réconciliation échoue, le rollback réussit
    env.agent.cycle()
    assert lab_files(env.lab) == env.files  # fichiers d'avant remis
    assert env.agent.state["applied_sha"] == "a" * 40 and env.agent.state["failed_sha"] == "b" * 40
    status = env.gitlab.last_status("lab/deploy", "b" * 40)
    assert status["state"] == "failed" and "Rollback effectué" in status["description"]
    assert "✘ Échec" in env.gitlab.notes[6][0]["body"] and "rollback" in env.gitlab.notes[6][0]["body"]
    assert (env.lab / ".runner-status").read_text().strip() == "deployed"
    env.agent.cycle()  # pas de nouvelle tentative sur le même commit
    assert lab_files(env.lab) == env.files


def test_failed_rollback_is_reported(env):
    adopt(env)
    env.gitlab.commit("b" * 40, with_topology(env.files, add_client3))
    env.runner.plan = {"added-nodes": ["client3"]}
    env.runner.fail = {"deploy": "réseau absent"}
    env.agent.cycle()
    status = env.gitlab.last_status("lab/deploy", "b" * 40)
    assert "Rollback en échec" in status["description"] and "make redeploy" in status["description"]
    assert (env.lab / ".runner-status").read_text().strip() == "failed"


def test_refused_on_main_changes_nothing(env):
    adopt(env)
    bad = with_topology(env.files, lambda d: d.update(name="autre"))
    env.gitlab.commit("b" * 40, bad)
    env.agent.cycle()
    assert lab_files(env.lab) == env.files and not env.runner.calls
    assert "refusé avant toute modification" in env.gitlab.last_status("lab/deploy", "b" * 40)["description"]
    assert (env.lab / ".runner-status").read_text().strip() == "deployed"


def test_drift_is_reported(env):
    adopt(env)
    (env.lab / "configs/sonic1/frr.conf").write_text("modifié à la main\n")
    env.gitlab.commit("b" * 40, with_topology(env.files, add_client3))
    env.gitlab.merged["b" * 40] = 7
    env.agent.cycle()
    assert "configs/sonic1/frr.conf" in env.gitlab.notes[7][0]["body"]
    backups = list((env.lab / ".gitops" / "backups").iterdir())
    assert (backups[0] / "configs/sonic1/frr.conf").read_text() == "modifié à la main\n"


def test_interrupted_state_on_restart(env):
    env.agent.state["status"] = "deploying"
    env.agent.save_state()
    again = agentmod.Agent(env.cfg, env.gitlab, runner=env.runner, web=env.web, clock=env.clock, sleep=env.clock.sleep)
    assert again.state["status"] == "interrupted"


def test_extract_keeps_only_managed_files(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("top/sonic-lab.clab.yml", b"t"), ("top/configs/sonic1/frr.conf", b"f"),
                           ("top/configs/sonic1/evil.sh", b"x"), ("top/ci/validate.py", b"x"),
                           ("top/../escape", b"x"), ("top/configs/../../x/frr.conf", b"x")]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("top/configs/sonic2/frr.conf")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/shadow"
        tar.addfile(link)
    files = agentmod.extract(buf.getvalue(), tmp_path)
    assert files == {"sonic-lab.clab.yml": b"t", "configs/sonic1/frr.conf": b"f"}
    assert not (tmp_path / "ci").exists()


def test_make_archive_roundtrip(tmp_path, env):
    assert agentmod.extract(make_archive(env.files), tmp_path) == env.files
