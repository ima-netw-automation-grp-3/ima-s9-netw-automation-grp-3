"""Cycle complet avec un faux Docker : découverte, collecte, interfaces, liens, sondes, événements."""

import copy
import json
import os
import time

import pytest

from conftest import FIXTURES, datagram, ethernet, flow_sample, ipv4, l4, lines_of, load, parse_line, text
from labmon import docker
from labmon.collector import Collector
from labmon.config import Config
from labmon.docker import Container, ExecResult
from labmon.flows import FlowTable

TOPOLOGY = """
name: testlab
mgmt: {network: testlab-mgmt, ipv4-subnet: 172.20.30.0/24}
topology:
  nodes:
    sonic1: {kind: sonic-vs, mgmt-ipv4: 172.20.30.11}
    client1: {kind: linux, mgmt-ipv4: 172.20.30.21}
    client9: {kind: linux, mgmt-ipv4: 172.20.30.29}
  links:
    - endpoints: ["client1:eth1", "sonic1:eth1"]
"""


class FakeDocker:
    def __init__(self):
        self.sonic = load("sonic1.json")
        self.client = text("client1.txt")
        self.state = {"sonic1": "running", "client1": "running"}
        self.calls = []

    def list_containers(self, lab_name, mgmt_network, timeout=15.0):
        kinds = {"sonic1": "sonic-vs", "client1": "linux"}
        return [
            Container(f"id-{n}", f"clab-testlab-{n}", n, kinds[n], st, image="img", started_at=1.0, mgmt_ipv4="172.20.30.1")
            for n, st in self.state.items()
        ]

    def exec_in(self, container, argv, timeout, *, in_container_timeout=True, stdin=None):
        self.calls.append((container, argv[0], in_container_timeout))
        if argv[0] == "python3":
            return ExecResult(0, json.dumps(self.sonic), "")
        if "probe" in argv:
            targets = argv[argv.index("probe") + 4 :]
            out = []
            for t in targets:
                out += [f"{t} 64 bytes from {t}: seq={i} ttl=64 time=0.{i + 1}00 ms" for i in range(5)]
                out.append(f"{t} 5 packets transmitted, 5 packets received, 0% packet loss")
            return ExecResult(0, "\n".join(out) + "\n", "")
        return ExecResult(0, self.client, "")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    topo = tmp_path / "lab.clab.yml"
    topo.write_text(TOPOLOGY)
    fake = FakeDocker()
    monkeypatch.setattr(docker, "list_containers", fake.list_containers)
    monkeypatch.setattr(docker, "exec_in", fake.exec_in)
    cfg = Config(lab_name="testlab", topology_path=topo)
    return Collector(cfg), fake


def field(line, name):
    return parse_line(line)[2].get(name)


def tags(line):
    return parse_line(line)[1]


def test_full_cycle(setup):
    collector, fake = setup
    lines = collector.cycle()
    nodes = {tags(line)["node"]: line for line in lines_of(lines, "lab_node")}
    assert set(nodes) == {"sonic1", "client1", "client9"}
    assert field(nodes["sonic1"], "up") == "1.0" and field(nodes["sonic1"], "ready") == "1.0"
    assert field(nodes["client9"], "up") == "0.0" and field(nodes["client9"], "container_state") == '"absent"'
    states = [line for line in lines_of(lines, "lab_collector") if "task=state" in line]
    assert len(states) == 2 and all(field(line, "ok") == "1.0" for line in states)
    assert lines_of(lines, "lab_link") and "up=1.0" in lines_of(lines, "lab_link")[0]
    probes = lines_of(lines, "lab_probe")
    assert any(tags(line)["src"] == "client1" and tags(line)["dst_ip"] == "10.255.0.1" for line in probes)
    assert all(field(line, "reachable") == "1.0" for line in probes)
    assert not any("172.20.30." in line for line in probes)  # jamais le réseau de management
    # busybox : pas de `timeout` dans le conteneur (il y laisserait des zombies)
    assert ("clab-testlab-client1", "sh", False) in fake.calls
    assert ("clab-testlab-sonic1", "python3", True) in fake.calls
    cycle = [line for line in lines_of(lines, "lab_collector") if "task=cycle" in line][0]
    assert field(cycle, "nodes_total") == "3.0" and field(cycle, "nodes_running") == "2.0"


def test_rates_on_second_cycle_and_node_events(setup):
    collector, fake = setup
    collector.cycle()
    fake.sonic = copy.deepcopy(fake.sonic)
    fake.sonic["uptime"] += 10
    fake.sonic["netdev"]["Ethernet0"]["stats"]["tx_bytes"] += 12_500_000
    lines = collector.cycle()
    port = next(line for line in lines_of(lines, "lab_interface") if tags(line).get("node") == "sonic1" and tags(line).get("interface") == "Ethernet0")
    assert float(field(port, "tx_bps")) == pytest.approx(10_000_000.0)
    fake.state["sonic1"] = "exited"
    lines = collector.cycle()
    events = [line for line in lines_of(lines, "lab_event") if "type=node" in line]
    assert len(events) == 1 and "sonic1" in events[0] and "WARNING" in events[0]
    link = lines_of(lines, "lab_link")[0]
    assert field(link, "up") == "0.0"


def test_redeploy_is_reported_once(setup, monkeypatch):
    collector, fake = setup
    collector.cycle()
    original = fake.list_containers

    def recreated(*args, **kwargs):
        out = original(*args, **kwargs)
        for c in out:
            c.id = "new-" + c.id  # mêmes nœuds, nouveaux conteneurs
        return out

    monkeypatch.setattr(docker, "list_containers", recreated)
    lines = collector.cycle()
    events = [line for line in lines_of(lines, "lab_event") if tags(line)["type"] == "node"]
    assert sorted(tags(line)["node"] for line in events) == ["client1", "sonic1"]
    assert all("recréé" in field(line, "message") for line in events)
    lines = collector.cycle()
    assert not [line for line in lines_of(lines, "lab_event") if tags(line)["type"] == "node"]


def test_node_removed_from_lab_is_reported(setup):
    collector, fake = setup
    collector.cycle()
    del fake.state["client1"]  # conteneur supprimé…
    collector.topology.path.write_text(TOPOLOGY.replace("    client1: {kind: linux, mgmt-ipv4: 172.20.30.21}\n", ""))
    future = time.time() + 5
    os.utime(collector.topology.path, (future, future))  # …et nœud retiré de la topologie
    lines = collector.cycle()
    events = [line for line in lines_of(lines, "lab_event") if tags(line)["type"] == "node"]
    assert [tags(line)["node"] for line in events] == ["client1"] and "retiré du lab" in field(events[0], "message")
    assert "client1" not in {tags(line)["node"] for line in lines_of(lines, "lab_node")}


def test_docker_failure_does_not_crash(setup, monkeypatch):
    collector, _ = setup

    def boom(*_args, **_kwargs):
        raise docker.DockerError("socket Docker injoignable")

    monkeypatch.setattr(docker, "list_containers", boom)
    lines = collector.cycle()
    cycle = lines_of(lines, "lab_collector")[-1]
    assert "socket Docker injoignable" in cycle
    assert all(field(line, "up") == "0.0" for line in lines_of(lines, "lab_node"))


def test_node_collection_error_is_reported(setup):
    collector, fake = setup
    fake.sonic = None  # json.dumps(None) -> "null" : sortie inexploitable

    lines = collector.cycle()
    state = next(line for line in lines_of(lines, "lab_collector") if tags(line).get("node") == "sonic1" and tags(line)["task"] == "state")
    assert field(state, "ok") == "0.0"
    node = next(line for line in lines_of(lines, "lab_node") if tags(line)["node"] == "sonic1")
    assert field(node, "up") == "1.0" and field(node, "collected") == "0.0"


def test_fixture_topology_is_the_lab_one():
    assert (FIXTURES / "topology.yml").read_text().startswith("# Lab SONiC")


def test_cycle_names_sflow_flows(tmp_path, monkeypatch):
    topo = tmp_path / "lab.clab.yml"
    topo.write_text(TOPOLOGY)
    fake = FakeDocker()
    monkeypatch.setattr(docker, "list_containers", fake.list_containers)
    monkeypatch.setattr(docker, "exec_in", fake.exec_in)
    table = FlowTable()
    collector = Collector(Config(lab_name="testlab", topology_path=topo), table)
    # avant tout cycle : l'IP de management des routeurs de la topologie est déjà acceptée
    table.add("172.20.30.11", datagram())
    assert table.drain().rejected == 0
    collector.cycle()  # découverte : l'IP de management du routeur devient une source acceptée
    http = ethernet(ipv4("192.168.1.10", "192.168.2.10", 6, l4(40000, 80)))
    table.add("172.20.30.1", datagram(flow_sample(http, index=1, frame=1000)))  # IP de management du faux Docker
    table.add("172.20.30.99", datagram(flow_sample(http, index=1)))  # pas un routeur : écarté
    lines = collector.cycle()
    (flow,) = [parse_line(line) for line in lines_of(lines, "lab_flow")]
    tags, fields = flow[1], flow[2]
    assert (tags["node"], tags["interface"], tags["peer_node"], tags["edge"], tags["app"]) == ("sonic1", "Ethernet0", "client1", "yes", "http")
    assert fields["bytes"] == "256000.0"
    status = [parse_line(line) for line in lines_of(lines, "lab_collector") if "task=flows" in line]
    assert status and status[0][2]["datagrams"] == "2.0" and status[0][2]["rejected"] == "1.0"
    assert lines_of(lines, "lab_flow_agent")
    # sans agrégat (mode once, tests) : aucune mesure de flux
    assert not [line for line in Collector(Config(lab_name="testlab", topology_path=topo)).cycle() if "task=flows" in line]
