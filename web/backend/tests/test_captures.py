import re

import pytest

from app import captures as captures_module


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(captures_module, "PROBE_SETTLE", 0)
    monkeypatch.setattr(captures_module.CaptureManager, "viewer_listening", staticmethod(_listening))


LISTENING = {"value": True}


async def _listening(host, port):
    return LISTENING["value"]


def start(client, **body):
    return client.post("/api/captures", json={"node": "sonic1", "interface": "eth1", **body})


def test_interfaces_listed(client):
    data = client.get("/api/captures/interfaces", params={"node": "sonic1"}).json()
    assert data["interfaces"] == ["eth0", "eth1", "lo"]


def test_interfaces_natural_order():
    names = ["lo", "Ethernet12", "Bridge", "eth10", "Ethernet4", "eth2", "Ethernet100", "Ethernet0", "eth0"]
    assert sorted(names, key=captures_module._iface_key) == [
        "eth0", "eth2", "eth10", "Ethernet0", "Ethernet4", "Ethernet12", "Ethernet100", "Bridge", "lo",
    ]  # fmt: skip


def test_viewer_starting_until_port_answers(client, monkeypatch):
    monkeypatch.setitem(LISTENING, "value", False)
    cap = start(client).json()
    assert cap["viewer"] == "starting" and not cap["viewer_ready"]
    page = client.get(cap["url"], headers={"Accept": "text/html"})
    assert page.status_code == 503 and 'http-equiv="refresh"' in page.text
    monkeypatch.setitem(LISTENING, "value", True)
    assert client.get("/api/captures").json()["captures"][0]["viewer"] == "ready"


def test_capture_lifecycle(client, fake_docker, lab_dir):
    res = start(client, filter="icmp or arp")
    assert res.status_code == 200, res.text
    cap = res.json()
    assert cap["state"] == "running" and cap["viewer"] == "ready" and cap["ended"] is None and cap["url"] == f"/wireshark/{cap['id']}/"
    assert fake_docker.pulled

    probe = fake_docker.created[f"testlab-capture-{cap['id']}"]["config"]
    assert probe["HostConfig"]["NetworkMode"] == "container:clab-testlab-sonic1"
    assert probe["Cmd"][:5] == ["-q", "-i", "eth1", "-F", "pcap"] and probe["Cmd"][-2:] == ["-f", "icmp or arp"]
    viewer = fake_docker.created[f"testlab-wireshark-{cap['id']}"]["config"]
    assert f"SUBFOLDER=/wireshark/{cap['id']}/" in viewer["Env"]
    assert viewer["HostConfig"]["NetworkMode"] == "testlab_default"
    assert cap["filename"] in fake_docker.files["/defaults/autostart"].decode()
    password = next(e.split("=", 1)[1] for e in viewer["Env"] if e.startswith("PASSWORD="))
    # passé à `openssl passwd` sans « -- » par l'image : ne doit pas ressembler à une option
    assert re.fullmatch(r"[0-9a-f]{48}", password)

    assert client.get("/api/captures").json()["captures"][0]["id"] == cap["id"]

    (lab_dir / "captures").mkdir()
    (lab_dir / "captures" / cap["filename"]).write_bytes(b"\xd4\xc3\xb2\xa1")
    dl = client.get(cap["download"])
    assert dl.status_code == 200 and dl.content == b"\xd4\xc3\xb2\xa1"
    assert cap["filename"] in dl.headers["content-disposition"]

    stopped = client.post(f"/api/captures/{cap['id']}/stop").json()
    assert stopped["state"] == "stopped" and stopped["size"] == 4
    assert stopped["ended"] == 1767261605.123456

    assert client.delete(f"/api/captures/{cap['id']}").status_code == 200
    assert client.get("/api/captures").json()["captures"] == []
    assert not (lab_dir / "captures" / cap["filename"]).exists()
    assert not fake_docker.created


@pytest.mark.parametrize(
    "body",
    [{"interface": "eth9"}, {"interface": "eth1; id"}, {"filter": "tcp; rm -rf /"}, {"filter": "$(id)"}],
)
def test_invalid_requests_rejected(client, fake_docker, body):
    assert start(client, **body).status_code == 400
    assert not fake_docker.created


def test_probe_failure_is_reported_and_cleaned(client, fake_docker):
    res = start(client, filter="badfilter")
    # le faux Docker termine la sonde quand son Cmd contient « badfilter »
    assert res.status_code == 400 and "syntax error" in res.json()["error"]
    assert not fake_docker.created


def test_session_limit(client):
    for _ in range(3):
        assert start(client).status_code == 200
    res = start(client)
    assert res.status_code == 400 and "au maximum" in res.json()["error"]


def test_unknown_capture_and_viewer(client):
    assert client.get("/api/captures/abcdef01/download").status_code == 404
    assert client.get("/wireshark/abcdef01/").status_code == 404
