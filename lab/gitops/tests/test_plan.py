import copy

import yaml
from conftest import FIXTURE, add_client3

from gitops import plan as p

TOPO = yaml.safe_load((FIXTURE / "sonic-lab.clab.yml").read_text())
# sortie réelle de containerlab 0.83 deploy --dry-run -f json (client3 et deux liens ajoutés)
CLAB_REAL = {
    "dry-run": True, "deployed-lab": False, "added-nodes": ["client3"], "deleted-nodes": [],
    "recreated-nodes": ["sonic1", "sonic2"], "started-nodes": [],
    "added-links": ["sonic1:eth3 -- sonic2:eth3", "client3:eth1 -- sonic2:eth4"], "deleted-endpoints": [],
    "restarted-nodes": [], "node-change-reasons": {"sonic1": "added link", "sonic2": "added link"},
}
EMPTY = {"added-nodes": [], "deleted-nodes": [], "recreated-nodes": []}
FILES = {"configs/sonic1/frr.conf": b"a", "configs/sonic2/frr.conf": b"b", "configs/sonic1/config_db.json": b"{}"}


def changed(change):
    data = copy.deepcopy(TOPO)
    change(data)
    return data


def test_containerlab_plan_is_reported():
    new = changed(add_client3)
    plan = p.compute(TOPO, new, CLAB_REAL, FILES, FILES)
    assert plan.added == ["client3"] and plan.recreated == {"sonic1": "lien ajouté", "sonic2": "lien ajouté"}
    assert plan.topology_changed and plan.restore == []
    assert plan.summary() == "+1 nœud(s), 2 recréé(s), 2 lien(s)"


def test_definition_change_is_recreated_with_cascade():
    new = changed(lambda d: d["topology"]["nodes"]["client1"]["exec"].append("ip link set eth1 mtu 1500"))
    plan = p.compute(TOPO, new, EMPTY, FILES, FILES)
    assert plan.redefined == ["client1"]
    assert plan.recreated == {"client1": "définition modifiée", "sonic1": "lien vers client1 (recréé)"}


def test_kind_change_redefines_all_its_nodes():
    new = changed(lambda d: d["topology"]["kinds"]["sonic-vs"]["env"].update(RESTCONF_USER="ops"))
    plan = p.compute(TOPO, new, EMPTY, FILES, FILES)
    assert plan.redefined == ["sonic1", "sonic2"]


def test_startup_config_changes():
    new_files = {**FILES, "configs/sonic2/frr.conf": b"b2"}
    plan = p.compute(TOPO, TOPO, EMPTY, FILES, new_files)
    assert plan.config_changes == {"sonic2": ["frr.conf"]} and plan.restore == ["sonic2"]
    assert plan.changed and not plan.topology_changed
    # routeur recréé : il charge déjà la nouvelle conf au démarrage, pas de restauration
    plan = p.compute(TOPO, TOPO, {**EMPTY, "recreated-nodes": ["sonic2"]}, FILES, new_files)
    assert plan.restore == []


def test_no_change():
    plan = p.compute(TOPO, TOPO, EMPTY, FILES, FILES)
    assert not plan.changed and plan.summary() == "aucun changement"


def test_note_and_mermaid():
    new = changed(add_client3)
    plan = p.compute(TOPO, new, CLAB_REAL, FILES, {**FILES, "configs/sonic1/frr.conf": b"x"})
    body = p.note(sha="abcdef123456", applied_sha="0123456789", plan=plan, errors=[], warnings=["w"], topology=new)
    assert body.startswith(p.NOTE_MARKER) and "`abcdef12`" in body and "`01234567`" in body
    assert "| Nœuds ajoutés | `client3` |" in body and "`sonic1` (lien ajouté)" in body
    assert "```mermaid" in body and "class n_client3 added" in body and "⚠️ Un routeur recréé" in body
    assert "- ⚠ w" in body
    graph = p.mermaid(new, plan)
    assert 'n_sonic2 ---|"eth4 · eth1"| n_client3' in graph


def test_note_refused_and_unchanged():
    body = p.note(sha="a" * 40, applied_sha=None, plan=None, errors=["IP en double"], warnings=[], topology=None)
    assert "✘ Refusé" in body and "- ✘ IP en double" in body and "mermaid" not in body
    body = p.note(sha="a" * 40, applied_sha=None, plan=None, errors=[], warnings=[], topology=None, clab_error="boom")
    assert "boom" in body
    plan = p.compute(TOPO, TOPO, EMPTY, FILES, FILES)
    body = p.note(sha="a" * 40, applied_sha="b" * 40, plan=plan, errors=[], warnings=[], topology=TOPO)
    assert "Aucun changement" in body


def test_deploy_note():
    plan = p.compute(TOPO, changed(add_client3), CLAB_REAL, FILES, FILES)
    body = p.deploy_note(sha="c" * 40, ok=True, plan=plan, message="ok.", drift=["configs/sonic1/frr.conf"], rolled_back=False)
    assert "✔ Déployé" in body and "Dérive locale" in body
    body = p.deploy_note(sha="c" * 40, ok=False, plan=plan, message="échec", drift=[], rolled_back=True)
    assert "✘ Échec" in body and "rollback" in body
