"""Flux sFlow : agrégation, filtrage des sources, nommage des nœuds et des interfaces, écoute UDP."""

import socket
import time

from conftest import data, datagram, ethernet, flow_sample, ipv4, l4, lines_of, parse_line, vxlan
from labmon.common import NodeObs, mgmt_network
from labmon.flows import OVERFLOW, Batch, FlowListener, FlowTable, build_view, flow_points, status_point
from labmon.lineproto import encode_all
from labmon.topology import parse_topology

ROUTER = "172.20.30.11"
HTTP = ethernet(ipv4("192.168.1.10", "192.168.2.10", 6, l4(40000, 80)))
TOPOLOGY = parse_topology(
    {
        "mgmt": {"network": "m", "ipv4-subnet": "172.20.30.0/24"},
        "topology": {
            "nodes": {
                "sonic1": {"kind": "sonic-vs", "mgmt-ipv4": ROUTER},
                "sonic2": {"kind": "sonic-vs", "mgmt-ipv4": "172.20.30.12"},
                "client1": {"kind": "linux"},
                "client2": {"kind": "linux"},
            },
            "links": [
                {"endpoints": ["client1:eth1", "sonic1:eth1"]},
                {"endpoints": ["sonic1:eth2", "sonic2:eth2"]},
                {"endpoints": ["sonic2:eth1", "client2:eth1"]},
            ],
        },
    }
)
KINDS = {"sonic1": "sonic-vs", "sonic2": "sonic-vs", "client1": "linux", "client2": "linux"}
SONIC = frozenset({"sonic-vs"})
MGMT = mgmt_network("172.20.30.0/24")


def addr(address, prefixlen=24):
    return {"address": address, "prefixlen": prefixlen, "family": "inet", "scope": "global"}


def observations():
    sonic1 = NodeObs("sonic1", "sonic-vs", "router", addresses={"Ethernet0": [addr("192.168.1.1")], "Loopback0": [addr("10.255.0.1", 32)]})
    sonic1.wires = {"Ethernet0": "eth1", "Ethernet4": "eth2"}
    client1 = NodeObs("client1", "linux", "client", addresses={"eth1": [addr("192.168.1.10")], "eth0": [addr("172.20.30.21")]})
    client2 = NodeObs("client2", "linux", "client", addresses={"eth1": [addr("192.168.2.10")]})
    return {"sonic1": sonic1, "client1": client1, "client2": client2}


def view():
    return build_view(TOPOLOGY, {"sonic1": ROUTER, "sonic2": "172.20.30.12"}, observations(), KINDS, SONIC, MGMT)


def table(*sources):
    t = FlowTable()
    t.set_allowed(sources or (ROUTER,))
    return t


def test_unknown_sources_are_rejected_before_decoding():
    t = table()
    t.add("172.20.30.99", b"pas du sflow")
    batch = t.drain()
    assert (batch.datagrams, batch.rejected, batch.errors, batch.flows) == (1, 1, 0, {})


def test_aggregation_scales_by_sampling_rate():
    t = table()
    t.add(ROUTER, datagram(flow_sample(HTTP, rate=256, index=1, frame=1500), flow_sample(HTTP, rate=256, index=1, frame=500)))
    batch = t.drain()
    assert batch.flows == {(ROUTER, 1, "tcp", "http", "192.168.1.10", "192.168.2.10", "none", None): [2000 * 256, 512, 2]}
    stats = batch.agents[ROUTER]
    assert (stats.datagrams, stats.flow_samples, stats.agent) == (1, 2, "10.255.0.1")
    assert t.drain().flows == {}  # vidé au cycle


def test_sequence_gaps_count_lost_datagrams_and_restart_resets():
    t = table()
    for seq in (1, 2, 6, 1, 2):
        t.add(ROUTER, datagram(sequence=seq))
    assert t.drain().agents[ROUTER].lost == 3


def test_decode_errors_are_counted_per_source():
    t = table()
    t.add(ROUTER, datagram(version=4))
    batch = t.drain()
    assert batch.errors == 1 and batch.agents[ROUTER].errors == 1 and "version" in batch.last_error


def test_keys_are_capped():
    t = FlowTable(max_keys=2)
    t.set_allowed([ROUTER])
    samples = [flow_sample(ethernet(ipv4(f"192.168.1.{i}", "192.168.2.10", 6, l4(40000, 80))), index=1) for i in range(5)]
    t.add(ROUTER, datagram(*samples))
    batch = t.drain()
    assert batch.overflow == 3 and len(batch.flows) == 3
    assert batch.flows[(ROUTER, 1, "tcp", "http", OVERFLOW, OVERFLOW, "none", None)][2] == 3


def test_vxlan_flows_report_the_inner_traffic():
    inner = ethernet(ipv4("10.10.1.11", "10.10.1.12", 6, l4(41000, 5201)))
    outer = ethernet(ipv4("10.255.1.1", "10.255.1.2", 17, l4(50000, 4789, b"\x00" * 4 + vxlan(10100, inner))))
    t = table()
    t.add(ROUTER, datagram(flow_sample(outer, index=5)))
    (key,) = t.drain().flows
    assert key == (ROUTER, 5, "tcp", "iperf3", "10.10.1.11", "10.10.1.12", "vxlan", 10100)


def test_view_names_routers_ports_peers_and_addresses():
    v = view()
    assert v.by_source[ROUTER] == "sonic1" and v.by_source["10.255.0.1"] == "sonic1"
    assert v.by_source["172.20.30.12"] == "sonic2"  # routeur non collecté : connu par son IP de management
    assert v.nodes["sonic1"].peers == {"Ethernet0": ("client1", False), "Ethernet4": ("sonic2", True)}
    assert v.nodes["sonic2"].peers["Ethernet0"] == ("client2", False)  # sans observation : règle ethN -> EthernetX
    assert v.owners["192.168.1.10"] == "client1" and "172.20.30.21" not in v.owners  # pas d'adresse de management


def test_anycast_addresses_name_nobody():
    obs = observations()
    obs["client2"].addresses["eth1"].append(addr("192.168.1.1"))
    v = build_view(TOPOLOGY, {"sonic1": ROUTER}, obs, KINDS, SONIC, MGMT)
    assert "192.168.1.1" not in v.owners


def test_flow_points_tags_fields_and_reaggregation():
    t = table(ROUTER, "172.20.30.50")
    t.add(ROUTER, datagram(flow_sample(HTTP, index=1, frame=1000)))
    t.add(ROUTER, datagram(flow_sample(HTTP, index=5, frame=1000), sequence=2))
    # même routeur vu par son IP d'agent depuis une autre source : mêmes tags, une seule ligne
    t.add("172.20.30.50", datagram(flow_sample(HTTP, index=1, frame=1000)))
    lines = encode_all(flow_points(t.drain(), view(), 10**18))
    flows = [parse_line(line) for line in lines_of(lines, "lab_flow")]
    edge = [f for f in flows if f[1]["interface"] == "Ethernet0"]
    assert len(edge) == 1 and len(flows) == 2
    _, tags, fields, ts = edge[0]
    assert tags == {
        "node": "sonic1",
        "interface": "Ethernet0",
        "peer_node": "client1",
        "edge": "yes",
        "proto": "tcp",
        "app": "http",
        "src": "192.168.1.10",
        "dst": "192.168.2.10",
        "src_node": "client1",
        "dst_node": "client2",
        "encap": "none",
    }
    assert (fields["bytes"], fields["packets"], fields["samples"], ts) == ("512000.0", "512.0", "2.0", 10**18)
    core = [f for f in flows if f[1]["interface"] == "Ethernet4"][0][1]
    assert (core["peer_node"], core["edge"]) == ("sonic2", "no")
    agents = [parse_line(line) for line in lines_of(lines, "lab_flow_agent")]
    assert len(agents) == 1  # deux sources, un seul routeur : une seule série
    _, tags, fields, _ = agents[0]
    assert tags == {"node": "sonic1"} and fields["datagrams"] == "3.0" and fields["agent"] == '"10.255.0.1"'


def test_unresolved_source_keeps_its_ip():
    t = table("172.20.30.77")
    t.add("172.20.30.77", datagram(flow_sample(HTTP, index=1), agent="10.99.0.1"))
    (point,) = [p for p in flow_points(t.drain(), view(), 0) if p.measurement == "lab_flow"]
    assert point.tags["node"] == "172.20.30.77" and point.tags["edge"] == "no"


def test_status_point():
    batch = Batch(datagrams=3, rejected=1)
    point = status_point(batch, None, 0.002, 0)
    assert point.tags == {"task": "flows"} and point.fields["listening"] is False and point.fields["rejected"] == 1


def test_listener_receives_datagrams():
    t = table("127.0.0.1")
    listener = FlowListener(t, "127.0.0.1", 0).start()
    try:
        deadline = time.monotonic() + 5
        while listener.bound_port() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        port = listener.bound_port()
        assert port, listener.error
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(data("sflow_flow.hex"), ("127.0.0.1", port))
        batch = Batch()
        while not batch.flows and time.monotonic() < deadline:
            time.sleep(0.05)
            batch = t.drain()
        assert sum(acc[2] for acc in batch.flows.values()) == 6
        assert next(iter(batch.flows))[1] == 5  # Ethernet4
    finally:
        listener.stop()


def test_listener_reports_bind_errors():
    t = table()
    listener = FlowListener(t, "192.0.2.1", 6343)  # adresse absente du conteneur de test
    assert listener._bind() is None and "impossible" in listener.error
