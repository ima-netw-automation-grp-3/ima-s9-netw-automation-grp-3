"""Frr (vtysh) avec des commandes simulées : même séquence que l'interface web, avec frr_config.py."""

import subprocess
from pathlib import Path

import pytest
from conftest import FRR_RUNNING, HERE

from restconf import frr as frr_mod
from restconf.store import InvalidData, OperationFailed

RUNNING_RAW = "Building configuration...\n\nCurrent configuration:\n!\nfrr version 10.7.1\nhostname sonic1\n!\n" + FRR_RUNNING


class FakeVtysh:
    def __init__(self):
        self.calls = []
        self.files = []
        self.fail_check = ""

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        out, code = "", 0
        if cmd[:2] == ["vtysh", "-c"] and cmd[2] == "show running-config":
            out = RUNNING_RAW
        elif cmd[:2] == ["vtysh", "-c"] and cmd[2] == "show bgp summary json":
            out = (HERE / "fixtures" / "bgp-summary.json").read_text()
        elif cmd[:2] == ["vtysh", "-C"]:
            self.files.append(("check", Path(cmd[3]).read_text()))
            out, code = (self.fail_check, 1) if self.fail_check else ("", 0)
        elif cmd[:2] == ["vtysh", "-f"]:
            self.files.append(("apply", Path(cmd[2]).read_text()))
            out = "[1234|bgpd] done\n"
        elif cmd[0] == "ping":
            out = "3 packets transmitted, 3 received, 0% packet loss, time 2003ms\nrtt min/avg/max/mdev = 0.1/0.2/0.3/0.0 ms\n"
        return subprocess.CompletedProcess(cmd, code, out, "")


@pytest.fixture
def vtysh(monkeypatch):
    fake = FakeVtysh()
    monkeypatch.setattr(frr_mod, "_run", fake)
    return fake


def test_running_and_neighbors(vtysh):
    f = frr_mod.Frr()
    assert f.running().startswith("router bgp 65001")  # lignes système retirées
    assert f.neighbors()[0]["state"] == "Established"


def test_replace_removes_then_applies(vtysh):
    desired = FRR_RUNNING.replace(" neighbor 10.0.12.2 remote-as 65002\n", "")
    result = frr_mod.Frr().apply(desired, "replace")
    kinds = [k for k, _ in vtysh.files]
    assert kinds == ["check", "apply", "apply"]
    assert vtysh.files[1][1].startswith("no router bgp 65001")  # bloc modifié : recréé
    assert result["resets"] == ["router bgp 65001"]
    assert [s["step"] for s in result["steps"]] == ["retrait", "application"]
    assert result["steps"][-1]["output"] == ""  # bruit de vtysh filtré


def test_merge_and_invalid(vtysh):
    frr_mod.Frr().apply("ip route 10.9.9.0/24 Null0\n", "merge")
    assert [k for k, _ in vtysh.files] == ["check", "apply"]
    vtysh.fail_check = "line 1: % Unknown command: bogus"
    with pytest.raises(InvalidData) as exc:
        frr_mod.Frr().apply("bogus\n", "replace")
    assert "Unknown command" in exc.value.message


def test_preview(vtysh):
    out = frr_mod.Frr().preview(FRR_RUNNING + "ip route 10.9.9.0/24 Null0\n", "merge")
    assert out["valid"] and "+ip route 10.9.9.0/24 Null0" in out["diff"]
    out = frr_mod.Frr().preview("", "replace")
    assert out["commands"] == "no router bgp 65001\n" and out["valid"]
    assert not any(k == "apply" for k, _ in vtysh.files)  # aperçu : rien appliqué


def test_ping(vtysh):
    out = frr_mod.Frr().ping("192.168.2.10", "192.168.1.1", 3)
    assert vtysh.calls[-1] == ["ping", "-c", "3", "-w", "5", "-I", "192.168.1.1", "192.168.2.10"]
    assert out["success"] and out["loss-percent"] == "0.0" and out["rtt-max"] == "0.300"


def test_command_failure(monkeypatch):
    def boom(cmd, timeout):
        return subprocess.CompletedProcess(cmd, 1, "", "vtysh: connexion refusée")

    monkeypatch.setattr(frr_mod, "_run", boom)
    with pytest.raises(OperationFailed):
        frr_mod.Frr().running()
