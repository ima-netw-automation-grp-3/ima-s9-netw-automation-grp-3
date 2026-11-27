"""Rendu des documents (playbooks/render.yml) : invariants vérifiés sur les host_vars du projet.

Pas de fichiers de référence : une modification de conf ne doit pas casser les tests, seulement une
incohérence (JSON invalide, ressource absente, adresse sur un port inconnu, FRR sans voisin…).
"""

import ipaddress
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook absent")


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    out = tmp_path_factory.mktemp("projet")
    shutil.copytree(PROJECT, out, dirs_exist_ok=True, ignore=shutil.ignore_patterns("build", "rollback", ".ansible", ".cache", "__pycache__"))
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(out / "ansible.cfg"),
        "ANSIBLE_HOME": str(out / ".ansible"),
        "ANSIBLE_LOCAL_TEMP": str(out / ".ansible" / "tmp"),
        "FLOW_COLLECTOR_IP": "172.20.30.99",  # le rendu ne doit pas dépendre du runner
    }
    res = subprocess.run(["ansible-playbook", "playbooks/render.yml"], cwd=out, env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-2000:]
    return out / "build"


def routers():
    return sorted(yaml.safe_load((PROJECT / "inventory" / "hosts.yml").read_text())["all"]["children"]["sonic"]["hosts"])


def host_vars(router):
    return yaml.safe_load((PROJECT / "inventory" / "host_vars" / f"{router}.yml").read_text())


@pytest.mark.parametrize("router", routers())
def test_documents_are_valid_single_resource_json(build, router):
    files = sorted(p.name for p in (build / router).iterdir())
    assert files == [
        "frr.conf",
        "sonic-interface.json",
        "sonic-loopback-interface.json",
        "sonic-port.patch.json",
        "sonic-sflow-sessions.patch.json",
        "sonic-sflow.json",
    ]
    for f in files:
        if f.endswith(".json"):
            doc = json.loads((build / router / f).read_text())
            module = f.split(".")[0].removesuffix("-sessions")
            assert list(doc) == [f"{module}:{module}"], f


@pytest.mark.parametrize("router", routers())
def test_addresses_ports_and_sflow_are_consistent(build, router):
    hv = host_vars(router)
    ports = {p["name"] for p in hv["ports"]}
    intf = json.loads((build / router / "sonic-interface.json").read_text())["sonic-interface:sonic-interface"]["INTERFACE"]
    for entry in intf["INTERFACE_IPPREFIX_LIST"]:
        assert entry["name"] in ports, f"{router} : adresse sur un port absent de « ports » : {entry}"
        ipaddress.ip_interface(entry["ip-prefix"])
    assert sorted({e["name"] for e in intf["INTERFACE_LIST"]}) == sorted({e["name"] for e in intf["INTERFACE_IPPREFIX_LIST"]})
    sflow = json.loads((build / router / "sonic-sflow.json").read_text())["sonic-sflow:sonic-sflow"]
    assert sflow["SFLOW"]["global"]["admin_state"] == "up"
    assert sflow["SFLOW_COLLECTOR"]["SFLOW_COLLECTOR_LIST"][0]["collector_ip"] == "172.20.30.99"
    sessions = {s["port"]: s for s in sflow["SFLOW_SESSION"]["SFLOW_SESSION_LIST"]}
    assert sessions.pop("all") == {"port": "all", "admin_state": "down"}  # ports non câblés : pas d'échantillonnage
    assert set(sessions) == ports and all(256 <= s["sample_rate"] <= 8388608 for s in sessions.values())
    patch = json.loads((build / router / "sonic-sflow-sessions.patch.json").read_text())["sonic-sflow:sonic-sflow"]
    assert patch == {"SFLOW_SESSION": sflow["SFLOW_SESSION"]}  # mêmes sessions, posées avant l'activation


@pytest.mark.parametrize("router", routers())
def test_frr_matches_bgp_host_vars(build, router):
    bgp = host_vars(router)["bgp"]
    text = (build / router / "frr.conf").read_text()
    assert text.startswith(f"router bgp {bgp['asn']}\n") and text.endswith("exit\n!\n")
    for n in bgp["neighbors"]:
        assert f" neighbor {n['address']} remote-as {n['remote_as']}\n" in text
    for net in bgp["networks"]:
        assert f"  network {ipaddress.ip_network(net)}\n" in text


def test_verify_targets_are_declared():
    for router in routers():
        hv = host_vars(router)
        assert hv["bgp"]["neighbors"], f"{router} : aucun voisin BGP à vérifier"
        for check in hv.get("verify", {}).get("ping", []):
            ipaddress.ip_address(check["destination"])


def test_playbooks_syntax():
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook absent")
    env = {**os.environ, "ANSIBLE_CONFIG": str(PROJECT / "ansible.cfg")}
    for pb in sorted((PROJECT / "playbooks").glob("*.yml")):
        res = subprocess.run(["ansible-playbook", "--syntax-check", str(pb)], cwd=PROJECT, env=env, capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"{pb.name} : {res.stdout[-1500:]}{res.stderr[-1500:]}"
