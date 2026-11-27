from conftest import text
from labmon import linux
from labmon.common import NodeObs
from labmon.state import NodeState


def build(output=None):
    obs = NodeObs("client1", "linux", "client")
    return linux.build(obs, NodeState(), output or text("client1.txt"), 1_000, "172.20.30.0/24", 500)


def test_parse_real_output():
    obs = build()
    assert not obs.errors
    assert obs.clock and obs.clock > 0
    assert set(obs.netdev) == {"eth0", "eth1"}
    eth1 = obs.netdev["eth1"]
    assert eth1["operstate"] == "up" and eth1["mtu"] == 9100 and eth1["flags"] & 1
    assert eth1["stats"]["rx_bytes"] > 0
    assert {"address": "192.168.1.10", "family": "inet", "prefixlen": 24, "scope": "global"} in obs.addresses["eth1"]
    assert obs.ready is True and obs.node_fields["os"].startswith("Alpine")


def test_routes_and_neighbors():
    obs = build()
    totals = {(p.tags["afi"], p.tags["protocol"]): p.fields["rib"] for p in obs.points if p.measurement == "lab_route"}
    assert totals[("ipv4", "total")] == 5 and totals[("ipv4", "static")] == 3 and totals[("ipv4", "connected")] == 2
    entries = {p.tags["prefix"]: p.fields for p in obs.points if p.measurement == "lab_route_entry"}
    assert entries["0.0.0.0/0"]["via"] == "172.20.30.1"
    assert entries["192.168.1.0/24"]["interfaces"] == "eth1"
    neigh = [p for p in obs.points if p.measurement == "lab_neighbor"]
    assert neigh and all(p.fields["count"] >= 1 for p in neigh)


def test_parse_route_variants():
    assert linux.parse_route("10.0.0.0/8 via 192.168.1.1 dev eth1 proto bgp metric 20")["proto"] == "bgp"
    r = linux.parse_route("192.168.1.0/24 dev eth1 scope link  src 192.168.1.10 ")
    assert r["proto"] == "connected" and r["dev"] == "eth1"
    assert linux.parse_route("unreachable 10.9.0.0/16 metric 10")["prefix"] == "10.9.0.0/16"


def test_parse_neighbor_busybox():
    n = linux.parse_neighbor("192.168.1.1 dev eth1 lladdr 0a:f6:9c:28:64:c1 used 0/0/0 probes 1 STALE")
    assert n == {"dst": "192.168.1.1", "dev": "eth1", "state": "STALE"}


def test_incomplete_output_is_reported():
    obs = build("@meta\nuptime=12.5\n@netdev\n")
    assert "output" in obs.errors and obs.clock == 12.5
