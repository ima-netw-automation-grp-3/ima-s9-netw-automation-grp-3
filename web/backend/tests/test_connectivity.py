"""Matrice de connectivité : choix des adresses selon la table de routage de la source."""

from __future__ import annotations

from app import connectivity as conn
from app.parsers import parse_ip_route

from . import conftest

# Serveur de stockage d'une topologie à VRF : une seule VRF routée, le reste part au management.
NETAPP_ROUTES = """default via 172.20.30.1 dev eth0
10.20.0.0/16 via 10.20.21.1 dev eth1
10.20.21.0/24 dev eth1 scope link  src 10.20.21.10
172.20.30.0/24 dev eth0 scope link  src 172.20.30.143
"""
# Serveur ACM : une carte par VRF.
ACM_ADDRESSES = [
    {"interface": "eth1", "ip": "10.10.10.11"},
    {"interface": "eth2", "ip": "10.20.20.11"},
    {"interface": "eth3", "ip": "10.30.30.11"},
]
MGMT = "172.20.30.0/24"


def routes(text: str) -> list[conn.Route]:
    return conn.ipv4_routes(parse_ip_route(text))


def test_longest_prefix_match_and_mgmt_devs():
    table = routes(NETAPP_ROUTES)
    assert conn.lookup(table, "10.20.22.10").dev == "eth1"
    assert conn.lookup(table, "10.1.0.1").dev == "eth0"  # route par défaut
    assert conn.lookup(routes("10.20.0.0/16 dev eth1\n"), "8.8.8.8") is None
    assert conn.mgmt_devs(table, MGMT) == {"eth0"}
    # management sur une autre interface : reconnu par le sous-réseau
    assert conn.mgmt_devs(routes("172.20.30.0/24 dev mgmt0 scope link\n"), MGMT) == {"eth0", "mgmt0"}
    # lignes non IPv4 ignorées
    assert routes("blackhole 10.9.0.0/16\nfe80::/64 dev eth1\n") == []


def test_plan_keeps_only_addresses_routed_in_the_lab():
    table = routes(NETAPP_ROUTES)
    tried, skipped = conn.plan(table, conn.mgmt_devs(table, MGMT), ACM_ADDRESSES)
    # l'ACM est joint sur sa carte de la VRF de stockage, pas sur la première (application)
    assert tried == [{"interface": "eth2", "ip": "10.20.20.11", "via": "eth1"}]
    assert len(skipped) == 2 and "management" in skipped[0]
    # loopback d'underlay invisible depuis la VRF : non routée
    tried, skipped = conn.plan(table, {"eth0"}, [{"interface": "Loopback0", "ip": "10.1.0.1"}])
    assert tried == [] and skipped
    # table inconnue : toutes les adresses sont essayées
    tried, skipped = conn.plan(None, {"eth0"}, ACM_ADDRESSES)
    assert [a["ip"] for a in tried] == [a["ip"] for a in ACM_ADDRESSES] and skipped == []


def _with_loopback(fake_docker):
    base = fake_docker._collector

    def collector():
        data = base()
        data["interfaces"]["Loopback0"] = {"addresses": ["10.255.0.1/32"]}
        return data

    fake_docker._collector = collector


def _pings(fake_docker):
    return [c for c in fake_docker.calls if c[0] == "ping"]


def test_api_unrouted_target_is_not_pinged(client, fake_docker):
    _with_loopback(fake_docker)
    data = client.post("/api/lab/connectivity").json()
    assert data["sources"] == ["client1"]
    assert data["targets"] == [
        {"node": "sonic1", "role": "router", "addresses": [{"interface": "Loopback0", "ip": "10.255.0.1"}]},
        {"node": "client1", "role": "client", "addresses": [{"interface": "eth1", "ip": "192.168.1.10"}]},
    ]
    (cell,) = data["results"]
    assert cell["status"] == "na" and not cell["ok"] and cell["ip"] is None
    assert "sortie par eth0" in cell["output"]
    assert data["totals"] == {"ok": 0, "fail": 0, "na": 1}
    assert _pings(fake_docker) == []


def test_api_routed_target_is_pinged(client, fake_docker, monkeypatch):
    _with_loopback(fake_docker)
    status = conftest.LINUX_STATUS.replace(
        "192.168.1.0/24 dev eth1", "10.0.0.0/8 via 192.168.1.1 dev eth1\n192.168.1.0/24 dev eth1"
    )
    monkeypatch.setattr(conftest, "LINUX_STATUS", status)
    data = client.post("/api/lab/connectivity", json={"count": 2}).json()
    (cell,) = data["results"]
    assert cell["status"] == "ok" and cell["ok"] and cell["ip"] == "10.255.0.1" and cell["via"] == "eth1"
    assert cell["rtt_avg"] == 0.2 and data["totals"] == {"ok": 1, "fail": 0, "na": 0}
    assert [c[-1] for c in _pings(fake_docker)] == ["10.255.0.1"]
