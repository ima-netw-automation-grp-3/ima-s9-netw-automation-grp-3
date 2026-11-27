"""Trafic typé (make traffic) : scénarios, lecture des résultats, orchestration avec un faux Docker."""

import pytest

from labmon import docker, traffic
from labmon.config import Config
from labmon.docker import Container, ExecResult

TOPOLOGY = """
name: testlab
mgmt: {network: testlab-mgmt, ipv4-subnet: 172.20.30.0/24}
topology:
  nodes:
    sonic1: {kind: sonic-vs}
    client1: {kind: linux}
    client2: {kind: linux}
"""


def test_scenarios_cover_every_type():
    plan = traffic.scenarios(traffic.MIX, "192.168.2.10", 30)
    assert [sc.kind for sc in plan] == ["tcp", "udp", "http", "dns", "icmp"]
    by_kind = {sc.kind: sc for sc in plan}
    assert by_kind["tcp"].client[:5] == ["iperf3", "-c", "192.168.2.10", "-p", "5201"]
    assert "-u" in by_kind["udp"].client and "5202" in by_kind["udp"].server_daemon
    assert by_kind["http"].server_daemon == ["busybox-extras", "httpd", "-f", "-p", "80", "-h", traffic.WWW]
    assert "curl -s -o /dev/null" in by_kind["http"].client[2] and "http://192.168.2.10/fichier.bin" in by_kind["http"].client[2]
    assert "--listen-address=192.168.2.10" in by_kind["dns"].server_daemon and by_kind["dns"].server_stop == ["pkill", "-x", "dnsmasq"]
    assert "nslookup" in by_kind["dns"].client[2] and "seq 8" in by_kind["dns"].client[2]
    assert by_kind["icmp"].client == ["ping", "-q", "-i", "0.002", "-s", "1200", "-w", "30", "192.168.2.10"]
    with pytest.raises(traffic.TrafficError, match="inconnu"):
        traffic.scenarios(("ftp",), "x", 1)


@pytest.mark.parametrize(
    "kind, stdout, expected",
    [
        ("tcp", "[  5]   0.00-30.00  sec   715 MBytes   200 Mbits/sec                  receiver\n", "iperf3 : 200 Mbits/sec reçus"),
        ("http", "12\n", "12 téléchargement(s) de 64 Mo"),
        ("dns", "1200\n1180\n1210\n", "3590 requête(s) DNS"),
        ("icmp", "--- 192.168.2.10 ping statistics ---\n14800 packets transmitted, 14800 packets received, 0% packet loss\n", "14800 ping(s), 14800 réponse(s)"),
    ],
)
def test_summary(kind, stdout, expected):
    assert traffic.summary(kind, ExecResult(0, stdout, "")) == expected


class FakeDocker:
    def __init__(self, kinds):
        self.kinds = kinds
        self.calls = []
        self.detached = []

    def list_containers(self, lab_name, mgmt_network, timeout=15.0):
        return [Container(f"id-{n}", f"clab-testlab-{n}", n, k, "running") for n, k in self.kinds.items()]

    def exec_in(self, container, argv, timeout, *, in_container_timeout=True, stdin=None):
        self.calls.append((container, argv))
        if argv[0] == "ping":
            return ExecResult(0, "100 packets transmitted, 100 packets received, 0% packet loss\n", "")
        return ExecResult(0, "3\n", "")

    def exec_detached(self, container, argv):
        self.detached.append((container, argv))


@pytest.fixture
def lab(tmp_path, monkeypatch):
    topo = tmp_path / "lab.clab.yml"
    topo.write_text(TOPOLOGY)
    fake = FakeDocker({"sonic1": "sonic-vs", "client1": "linux", "client2": "linux"})
    monkeypatch.setattr(docker, "list_containers", fake.list_containers)
    monkeypatch.setattr(docker, "exec_in", fake.exec_in)
    monkeypatch.setattr(docker, "exec_detached", fake.exec_detached)
    posted = []
    monkeypatch.setattr(traffic, "post", lambda lines, url: posted.extend(lines))
    monkeypatch.setattr(traffic.time, "sleep", lambda s: None)
    return Config(lab_name="testlab", topology_path=topo), fake, posted


def test_run_mix_orchestrates_servers_clients_and_events(lab):
    cfg, fake, posted = lab
    assert traffic.run(cfg, "client1", "client2", "mix", 20, dst_ip="192.168.2.10") == 0
    installs = [argv[2] for c, argv in fake.calls if argv[:2] == ["sh", "-c"] and "apk add" in argv[2]]
    assert any("curl" in s and "iperf3" in s for s in installs) and any("dnsmasq" in s and "busybox-extras" in s for s in installs)
    servers = [argv[0] for _, argv in fake.detached]
    assert servers == ["iperf3", "iperf3", "busybox-extras", "dnsmasq"]
    assert all(c == "clab-testlab-client2" for c, _ in fake.detached)
    clients = [argv for c, argv in fake.calls if c == "clab-testlab-client1" and argv[0] in ("iperf3", "ping", "sh") and "apk" not in " ".join(argv)]
    assert len(clients) == 5
    assert ["pkill", "-x", "dnsmasq"] in [argv for c, argv in fake.calls if c == "clab-testlab-client2"]
    assert len(posted) == 2 and all(line.startswith("lab_event,") and "type=traffic" in line for line in posted)
    assert "client1 → client2" in posted[0] and "requête(s) DNS" in posted[1]


def test_run_refuses_routers_and_missing_nodes(lab):
    cfg, _, _ = lab
    with pytest.raises(traffic.TrafficError, match="routeur SONiC"):
        traffic.run(cfg, "sonic1", "client2", "icmp", 5, dst_ip="192.168.2.10")
    with pytest.raises(traffic.TrafficError, match="n'est pas démarré"):
        traffic.run(cfg, "client1", "client9", "icmp", 5, dst_ip="192.168.2.10")
