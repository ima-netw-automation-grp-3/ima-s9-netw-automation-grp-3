from conftest import text
from labmon.common import NodeObs, mgmt_network
from labmon.probes import Target, lab_targets, parse_probe_output, probe_points, probe_stats, targets_for


def test_parse_busybox_output():
    res = parse_probe_output(text("probe_busybox.txt"))
    assert set(res) == {"192.168.2.10", "10.255.0.1", "192.168.9.9"}
    ok = res["192.168.2.10"]
    assert ok["sent"] == 5 and ok["received"] == 5 and len(ok["rtts"]) == 5
    lost = res["192.168.9.9"]
    assert lost["received"] == 0 and lost["rtts"] == []


def test_parse_iputils_output():
    res = parse_probe_output(text("probe_iputils.txt"))
    assert res["10.0.12.2"]["received"] == 5 and len(res["10.0.12.2"]["rtts"]) == 5
    assert res["192.168.9.9"]["received"] == 0 and res["192.168.9.9"]["sent"] >= 5


def test_stats_jitter_and_loss():
    fields = probe_stats({"rtts": [1.0, 3.0, 2.0], "sent": 4, "received": 3}, 4)
    assert fields["rtt_min_ms"] == 1.0 and fields["rtt_max_ms"] == 3.0 and fields["rtt_avg_ms"] == 2.0
    assert fields["jitter_ms"] == 1.5  # (|3-1| + |2-3|) / 2
    assert fields["loss_pct"] == 25.0 and fields["reachable"] is True
    empty = probe_stats({}, 5)
    assert empty["loss_pct"] == 100.0 and empty["reachable"] is False and "rtt_avg_ms" not in empty


def test_unreachable_error_is_kept():
    res = parse_probe_output("10.9.9.9 ping: sendto: Network unreachable\n")
    assert probe_stats(res["10.9.9.9"], 5)["error"].endswith("Network unreachable")


def _obs(name, role, addrs):
    obs = NodeObs(name, "sonic-vs" if role == "router" else "linux", role)
    for iface, ip in addrs:
        obs.addresses.setdefault(iface, []).append({"family": "inet", "address": ip, "prefixlen": 24, "scope": "global"})
    return obs


def test_targets_all_and_primary():
    observations = {
        "sonic1": _obs("sonic1", "router", [("eth0", "172.20.30.11"), ("Ethernet0", "192.168.1.1"), ("Loopback0", "10.255.0.1")]),
        "client1": _obs("client1", "client", [("eth0", "172.20.30.21"), ("eth1", "192.168.1.10")]),
        "client2": _obs("client2", "client", [("eth1", "192.168.2.10")]),
    }
    targets = lab_targets(observations, mgmt_network("172.20.30.0/24"))
    assert {t.ip for t in targets} == {"192.168.1.1", "10.255.0.1", "192.168.1.10", "192.168.2.10"}
    primary = {t.node: t.ip for t in targets if t.primary}
    assert primary == {"sonic1": "10.255.0.1", "client1": "192.168.1.10", "client2": "192.168.2.10"}
    assert {t.ip for t in targets_for("client1", targets, "all", 64)} == {"192.168.1.1", "10.255.0.1", "192.168.2.10"}
    assert {t.ip for t in targets_for("client1", targets, "primary", 64)} == {"10.255.0.1", "192.168.2.10"}
    assert targets_for("client1", targets, "off", 64) == []
    assert len(targets_for("client1", targets, "all", 1)) == 1


def test_probe_points_tags():
    targets = [Target("client2", "eth1", "192.168.2.10", True)]
    points = probe_points("client1", targets, text("probe_busybox.txt"), 5, 1_000)
    assert points[0].tags == {"src": "client1", "dst": "client2", "dst_ip": "192.168.2.10", "dst_interface": "eth1", "target": "client2:eth1"}
    assert points[0].fields["reachable"] is True
