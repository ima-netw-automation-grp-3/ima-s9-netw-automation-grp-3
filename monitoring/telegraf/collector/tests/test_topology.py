import os
import time

from conftest import FIXTURES
from labmon.topology import TopologyCache, parse_topology, sonic_port_for


def test_real_topology():
    topo = TopologyCache(FIXTURES / "topology.yml").get()
    assert set(topo.nodes) == {"sonic1", "sonic2", "client1", "client2"}
    assert topo.nodes["sonic1"].kind == "sonic-vs" and topo.nodes["client1"].image == "alpine:3.22"
    assert topo.mgmt_network == "sonic-lab-mgmt" and topo.mgmt_subnet == "172.20.30.0/24"
    assert topo.peer_of("sonic1", "eth2") == ("sonic2", "eth2")
    assert topo.peer_of("client1", "eth1") == ("sonic1", "eth1")
    assert topo.peer_of("sonic1", "eth9") is None


def test_dict_endpoints_and_defaults():
    topo = parse_topology(
        {
            "topology": {
                "defaults": {"kind": "linux"},
                "nodes": {"a": None, "b": {"kind": "sonic-vs"}},
                "links": [{"endpoints": [{"node": "a", "interface": "eth1"}, "b:eth1"]}, {"endpoints": ["a:eth2"]}],
            }
        }
    )
    assert topo.nodes["a"].kind == "linux" and len(topo.links) == 1 and topo.links[0].id == "a:eth1--b:eth1"


def test_sonic_port_for():
    assert sonic_port_for("eth1") == "Ethernet0" and sonic_port_for("eth3") == "Ethernet8"
    assert sonic_port_for("eth0") is None and sonic_port_for("lo") is None


def test_cache_reloads_and_survives_errors(tmp_path):
    path = tmp_path / "lab.clab.yml"
    cache = TopologyCache(path)
    assert cache.get().nodes == {} and "introuvable" in cache.error
    path.write_text("name: t\ntopology:\n  nodes:\n    n1: {kind: linux}\n")
    assert set(cache.get().nodes) == {"n1"} and cache.error is None
    path.write_text("name: t\ntopology:\n  nodes:\n    n1: {kind: linux}\n    n2: {kind: linux}\n")
    future = time.time() + 5
    os.utime(path, (future, future))
    assert set(cache.get().nodes) == {"n1", "n2"}
    path.write_text("topology: [\n")
    os.utime(path, (future + 5, future + 5))
    assert cache.get().nodes == {} and "invalide" in cache.error
