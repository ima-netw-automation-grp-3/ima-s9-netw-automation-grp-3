from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.errors import BadRequest, NotFound
from app.nodes.base import check_target, ipv4_addresses, parse_started_at
from app.nodes.sonic import compute_phase
from app.store import Store
from app.topology import load_topology, sonic_port_for

# Copie figée du lab de base (lab/sonic-lab.clab.yml) : le test ne dépend pas de la topologie du moment.
LAB_TOPOLOGY = Path(__file__).resolve().parent / "fixtures" / "sonic-lab.clab.yml"


def test_lab_topology_file():
    topo = load_topology(LAB_TOPOLOGY)
    assert topo.name == "sonic-lab"
    assert set(topo.nodes) == {"sonic1", "sonic2", "client1", "client2"}
    assert topo.nodes["sonic1"].is_router and topo.nodes["sonic1"].image == "docker-sonic-vs:latest"
    assert topo.nodes["client1"].role == "client" and topo.nodes["client1"].image == "alpine:3.22"
    assert topo.mgmt_subnet == "172.20.30.0/24"
    assert len(topo.links) == 3
    assert topo.peer_of("sonic1", "eth2").node == "sonic2"


def test_sonic_port_mapping():
    assert sonic_port_for("eth1") == "Ethernet0"
    assert sonic_port_for("eth2") == "Ethernet4"
    assert sonic_port_for("eth0") is None and sonic_port_for("Ethernet0") is None


def test_store_startup_and_snapshots(tmp_path):
    store = Store(tmp_path / "configs", tmp_path / "backups", owner_ref=tmp_path)
    assert store.read_startup("sonic1", "frr") is None
    store.write_startup("sonic1", "frr", "router bgp 1\n")
    assert store.read_startup("sonic1", "frr") == "router bgp 1\n"
    meta = store.create_snapshot("sonic1", {"configdb": "{}\n", "frr": "!\n"}, label="Avant test BGP !")
    assert meta["id"].endswith("-avant-test-bgp") and meta["domains"] == ["configdb", "frr"]
    other = store.create_snapshot("sonic1", {"frr": "!\n"}, label="Avant test BGP !")
    assert other["id"] != meta["id"]
    assert [s["id"] for s in store.list_snapshots("sonic1")][0] == other["id"]
    assert store.read_snapshot("sonic1", meta["id"], "frr") == "!\n"
    store.delete_snapshot("sonic1", meta["id"])
    with pytest.raises(NotFound):
        store.get_snapshot("sonic1", meta["id"])


@pytest.mark.parametrize("snap_id", ["../../etc", "20261119-104233/../x", "x"])
def test_store_rejects_bad_snapshot_ids(tmp_path, snap_id):
    store = Store(tmp_path / "configs", tmp_path / "backups")
    with pytest.raises(BadRequest):
        store.read_snapshot("sonic1", snap_id, "frr")


def test_store_rejects_bad_node_and_domain(tmp_path):
    store = Store(tmp_path / "configs", tmp_path / "backups")
    with pytest.raises(BadRequest):
        store.startup_path("../x", "frr")
    with pytest.raises(BadRequest):
        store.startup_path("sonic1", "ssh")


SERVICES_OK = [{"name": n, "state": "RUNNING"} for n in ("orchagent", "bgpd")]


@pytest.mark.parametrize(
    ("data", "uptime", "phase"),
    [
        ({"errors": {"services": "x"}}, 10, "booting"),
        ({"errors": {"services": "x"}}, 600, "error"),
        ({"services": [{"name": "orchagent", "state": "RUNNING"}, {"name": "bgpd", "state": "STOPPED"}]}, 20, "booting"),
        ({"services": [{"name": "orchagent", "state": "FATAL"}, {"name": "bgpd", "state": "RUNNING"}]}, 120, "error"),
        ({"services": SERVICES_OK, "ready": False}, 30, "bootstrap"),
        ({"services": SERVICES_OK, "ready": False}, 3600, "warning"),
        ({"services": SERVICES_OK, "ready": True}, 30, "ready"),
    ],
)
def test_compute_phase(data, uptime, phase):
    data = {"key_services": ["orchagent", "bgpd"], **data}
    assert compute_phase(data, started_at=1000.0, now=1000.0 + uptime)[0] == phase


def test_helpers():
    expected = datetime(2026, 11, 19, 10, 12, 0, 123456, tzinfo=UTC).timestamp()
    assert parse_started_at({"State": {"StartedAt": "2026-11-19T10:12:00.123456789Z"}}) == pytest.approx(expected)
    assert parse_started_at({"State": {"StartedAt": "0001-01-01T00:00:00Z"}}) is None
    assert ipv4_addresses(["172.20.30.11/24", "10.0.12.1/30", "fe80::1/64"], "172.20.30.0/24") == ["10.0.12.1/30"]
    assert check_target(" 192.168.2.10 ") == "192.168.2.10"
    for bad in ("-f 1.1.1.1", "1.1.1.1; reboot", "$(id)", ""):
        with pytest.raises(BadRequest):
            check_target(bad)
