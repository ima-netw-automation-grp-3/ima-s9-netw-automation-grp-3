from conftest import FIXTURES
from labmon.common import NodeObs, mgmt_network
from labmon.interfaces import if_type, interface_points, link_points, oper_up
from labmon.state import NodeState
from labmon.topology import TopologyCache

TOPO = TopologyCache(FIXTURES / "topology.yml").get()
KINDS = {n: d.kind for n, d in TOPO.nodes.items()}
SONIC = frozenset({"sonic-vs"})
MGMT = mgmt_network(TOPO.mgmt_subnet)


def nd(rx, tx, state="up", flags=0x1003, speed=10000):
    return {"stats": {"rx_bytes": rx, "tx_bytes": tx, "rx_packets": rx // 100, "tx_packets": tx // 100}, "operstate": state, "flags": flags, "carrier": 1, "mtu": 9100, "speed": speed}


def client(clock, rx, tx):
    obs = NodeObs("client1", "linux", "client", clock=clock)
    obs.netdev = {"eth0": nd(0, 0), "eth1": nd(rx, tx)}
    obs.addresses = {"eth1": [{"family": "inet", "address": "192.168.1.10", "prefixlen": 24, "scope": "global"}]}
    return obs


def router(clock, rx, tx, oper=True):
    obs = NodeObs("sonic1", "sonic-vs", "router", clock=clock)
    obs.netdev = {"Ethernet0": nd(rx, tx, "unknown", speed=10000), "eth1": nd(rx, tx), "Loopback0": nd(0, 0, "unknown", 0x41)}
    obs.wires = {"Ethernet0": "eth1"}
    obs.port_speed = {"Ethernet0": 100000.0}
    obs.port_oper = {"Ethernet0": oper}
    return obs


def test_if_type():
    assert [if_type(n) for n in ("eth0", "eth3", "Ethernet12", "Loopback0", "Vlan100", "PortChannel1", "Bridge", "dummy", "Vrf1")] == [
        "mgmt",
        "wire",
        "port",
        "loopback",
        "vlan",
        "lag",
        "bridge",
        "other",
        "vrf",
    ]


def test_oper_up_for_unknown_state_devices():
    assert oper_up({"operstate": "unknown", "flags": 0x41, "carrier": 1})
    assert not oper_up({"operstate": "unknown", "flags": 0x40, "carrier": 1})
    assert not oper_up({"operstate": "down", "flags": 0x1003})


def test_rates_util_and_peers():
    st = NodeState()
    w2p = {("sonic1", "eth1"): "Ethernet0"}
    interface_points(client(100.0, 1000, 2000), st, TOPO, KINDS, w2p, SONIC, MGMT, 1)
    points, summary = interface_points(client(110.0, 126000, 2000), st, TOPO, KINDS, w2p, SONIC, MGMT, 2)
    eth1 = next(p for p in points if p.tags["interface"] == "eth1")
    assert eth1.tags["linked"] == "yes" and eth1.tags["peer_node"] == "sonic1" and eth1.tags["peer_interface"] == "Ethernet0"
    assert eth1.fields["rx_bps"] == 100000.0 and eth1.fields["tx_bps"] == 0.0
    assert abs(eth1.fields["rx_util_pct"] - 0.001) < 1e-9  # 100 kbit/s sur 10 Gbit/s
    assert summary["eth1"]["up"] is True and summary["eth1"]["has_ip"] is True
    eth0 = next(p for p in points if p.tags["interface"] == "eth0")
    assert eth0.tags["if_type"] == "mgmt" and eth0.tags["linked"] == "no"


def test_counter_reset_gives_no_rate():
    st = NodeState()
    interface_points(client(100.0, 5000, 5000), st, TOPO, KINDS, {}, SONIC, MGMT, 1)
    points, _ = interface_points(client(110.0, 10, 10), st, TOPO, KINDS, {}, SONIC, MGMT, 2)
    eth1 = next(p for p in points if p.tags["interface"] == "eth1")
    assert "rx_bps" not in eth1.fields


def test_sonic_port_uses_configdb_speed_and_oper():
    st = NodeState()
    interface_points(router(10.0, 0, 0, oper=False), st, TOPO, KINDS, {}, SONIC, MGMT, 1)
    points, summary = interface_points(router(20.0, 0, 1_250_000_000, oper=False), st, TOPO, KINDS, {}, SONIC, MGMT, 2)
    port = next(p for p in points if p.tags["interface"] == "Ethernet0")
    assert port.tags["if_type"] == "port" and port.tags["peer_node"] == "client1"
    assert port.fields["speed_mbps"] == 100000.0 and port.fields["oper_up"] is False
    assert abs(port.fields["tx_util_pct"] - 1.0) < 1e-9  # 1 Gbit/s sur 100 Gbit/s
    assert summary["Loopback0"]["up"] is True


def test_link_state_follows_sonic_port():
    obs = {"client1": client(1.0, 0, 0), "sonic1": router(1.0, 0, 0, oper=False)}
    summaries = {"client1": {"eth1": {"up": True, "tx_bps": 5.0}}, "sonic1": {"eth1": {"up": True, "tx_bps": 7.0}}}
    w2p = {("sonic1", "eth1"): "Ethernet0"}
    points = link_points(TOPO, obs, summaries, w2p, {"client1", "sonic1"}, 1)
    link = next(p for p in points if p.tags["link"] == "client1:eth1--sonic1:eth1")
    assert link.fields["up"] is False and link.fields["a_up"] is True and link.fields["z_up"] is False
    assert link.fields["a_to_z_bps"] == 5.0 and link.fields["z_to_a_bps"] == 7.0 and link.fields["z_port"] == "Ethernet0"
    other = next(p for p in points if p.tags["link"] == "sonic2:eth1--client2:eth1")
    assert other.fields["up"] is False  # nœuds non démarrés
