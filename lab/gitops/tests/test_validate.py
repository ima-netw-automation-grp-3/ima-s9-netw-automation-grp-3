import json
from pathlib import Path

import pytest
import yaml
from conftest import LAB

from gitops import validate as v


def write_topology(root: Path, change) -> Path:
    path = root / v.TOPOLOGY_FILE
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return root


def errors_after(lab: Path, change) -> list[str]:
    return v.validate(write_topology(lab, change)).errors


def test_real_lab_topology_is_valid():
    # la topologie du moment dans lab/, quelle qu'elle soit : seule sa validité est vérifiée
    report = v.validate(LAB)
    assert report.ok, report.errors


def test_lab_copy_is_valid(lab):
    assert v.validate(lab).errors == []


def nodes(d):
    return d["topology"]["nodes"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda d: d.update(name="autre"), "name doit rester"),
        (lambda d: d["mgmt"].update(network="autre"), "mgmt.network"),
        (lambda d: d["mgmt"].update({"ipv4-range": "172.20.30.128/25"}), "ipv4-range"),
        (lambda d: d.update(prefix="x"), "« prefix »"),
        (lambda d: nodes(d)["client2"].update({"mgmt-ipv4": "172.20.30.21"}), "déjà prise par client1"),
        (lambda d: nodes(d)["client2"].update({"mgmt-ipv4": "172.20.30.5"}), "réservée (GitLab)"),
        (lambda d: nodes(d)["client2"].update({"mgmt-ipv4": "172.20.30.6"}), "réservée (collecteur sFlow)"),
        (lambda d: nodes(d)["client2"].update({"mgmt-ipv4": "10.0.0.2"}), "hors de 172.20.30.0/24"),
        (lambda d: nodes(d)["client2"].pop("mgmt-ipv4"), "mgmt-ipv4 obligatoire"),
        (lambda d: nodes(d)["client2"].update(kind="srl"), "kind obligatoire"),
        (lambda d: nodes(d)["client2"].update(image="evil/image:latest"), "non autorisée"),
        (lambda d: nodes(d)["client2"].update({"network-mode": "host"}), "« network-mode »"),
        (lambda d: nodes(d)["client2"].update(binds=["/:/host"]), "source de bind non autorisée"),
        (lambda d: nodes(d)["sonic1"]["binds"].append("configs/sonic2/frr.conf:/x"), "configs/sonic2/frr.conf"),
        (lambda d: d["topology"]["kinds"]["sonic-vs"]["env"].pop("RESTCONF_USER"), "RESTCONF_USER manquante"),
        (lambda d: d["topology"]["kinds"]["sonic-vs"].update(exec=[]), "lab-bootstrap.sh"),
        (lambda d: d["topology"]["kinds"]["sonic-vs"]["binds"].pop(1), "bind du lab manquant : sonic/restconf"),
        (lambda d: nodes(d)["sonic1"].update(binds=[]), "bind de démarrage manquant"),
        (lambda d: d["topology"]["links"].append({"endpoints": ["sonic1:eth2", "client2:eth2"]}), "déjà utilisée"),
        (lambda d: d["topology"]["links"].append({"endpoints": ["sonic1:eth33", "client2:eth2"]}), "hors limites"),
        (lambda d: d["topology"]["links"].append({"endpoints": ["sonic1:eth5", "ghost:eth1"]}), "nœud inconnu"),
        (lambda d: d["topology"]["links"].append({"endpoints": ["sonic1:eth5", "sonic1:eth6"]}), "deux nœuds différents"),
        (lambda d: d["topology"]["links"].append({"type": "host", "endpoints": ["sonic1:eth5", "host:x"]}), "seule la forme"),
        (lambda d: nodes(d).update({"Bad_Name": {"kind": "linux", "mgmt-ipv4": "172.20.30.40"}}), "nom invalide"),
    ],
)
def test_rules(lab, change, expected):
    errors = errors_after(lab, change)
    assert any(expected in e for e in errors), errors


def test_new_router_needs_its_startup_files(lab):
    def add_sonic3(d):
        nodes(d)["sonic3"] = {
            "kind": "sonic-vs",
            "mgmt-ipv4": "172.20.30.13",
            "binds": ["configs/sonic3/config_db.json:/var/sonic/config_db.json:ro", "configs/sonic3/frr.conf:/etc/sonic/lab/frr.conf:ro"],
        }

    errors = errors_after(lab, add_sonic3)
    assert any("configs/sonic3/config_db.json absent" in e for e in errors)
    (lab / "configs" / "sonic3").mkdir()
    (lab / "configs" / "sonic3" / "config_db.json").write_text("{pas du json")
    (lab / "configs" / "sonic3" / "frr.conf").write_text("router bgp 65003\n")
    errors = v.validate(lab).errors
    assert any("JSON invalide" in e for e in errors)
    (lab / "configs" / "sonic3" / "config_db.json").write_text(json.dumps({"DEVICE_METADATA": {"localhost": {"hostname": "sonic3"}}}))
    assert v.validate(lab).ok


def test_warnings(lab):
    (lab / "configs" / "orphelin").mkdir()
    cfg = lab / "configs" / "sonic1" / "config_db.json"
    data = json.loads(cfg.read_text())
    data["DEVICE_METADATA"]["localhost"]["switch_type"] = "switch"
    cfg.write_text(json.dumps(data))
    report = v.validate(lab)
    assert report.ok
    assert any("orphelin" in w for w in report.warnings) and any("switch_type" in w for w in report.warnings)


def test_unreadable_topology(tmp_path):
    assert "absent" in v.validate(tmp_path).errors[0]
    (tmp_path / v.TOPOLOGY_FILE).write_text("a: [b\n")
    assert "illisible" in v.validate(tmp_path).errors[0]


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("GITOPS_ALLOWED_IMAGES", "a:1, b:2")
    monkeypatch.setenv("GITLAB_IP", "172.20.30.7")
    monkeypatch.setenv("FLOW_COLLECTOR_IP", "172.20.30.8")
    s = v.Settings.from_env()
    assert s.allowed_images == ("a:1", "b:2")
    assert s.reserved_ips == {"172.20.30.7": "GitLab", "172.20.30.8": "collecteur sFlow"}


def test_main_exit_codes(lab, capsys):
    assert v.main([str(lab)]) == 0
    assert "valide" in capsys.readouterr().out
    write_topology(lab, lambda d: d.update(name="x"))
    assert v.main([str(lab)]) == 1
