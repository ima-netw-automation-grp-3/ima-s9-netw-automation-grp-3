import copy

from conftest import load
from labmon import sonic
from labmon.common import NodeObs
from labmon.lineproto import encode_all
from labmon.state import NodeState

TS = 1_795_096_000_000_000_000


def build(data, st=None):
    st = st or NodeState()
    obs = sonic.build(NodeObs("sonic1", "sonic-vs", "router"), st, data, TS)
    return obs, st, encode_all(obs.points)


def by_measurement(obs):
    out = {}
    for p in obs.points:
        out.setdefault(p.measurement, []).append(p)
    return out


def test_build_covers_all_sections():
    obs, _, lines = build(load("sonic1.json"))
    assert not obs.errors, obs.errors
    names = set(by_measurement(obs))
    assert {
        "lab_sonic_port",
        "lab_bgp",
        "lab_bgp_peer",
        "lab_bgp_afi",
        "lab_route",
        "lab_route_entry",
        "lab_neighbor",
        "lab_sonic_service",
        "lab_sonic_db",
        "lab_sonic_crm",
        "lab_sonic_counters",
        "lab_sonic_redis",
        "lab_sonic_lldp",
        "lab_syslog",
        "lab_event",
    } <= names
    assert len(lines) == len(obs.points)
    assert obs.ready is True and obs.node_fields["hwsku"] == "Force10-S6000"
    assert obs.node_fields["frr_version"].startswith("FRRouting")


def test_ports_wires_and_speeds():
    obs, _, _ = build(load("sonic1.json"))
    assert obs.wires == {"Ethernet0": "eth1", "Ethernet4": "eth2"}
    assert obs.port_speed["Ethernet0"] == 100000.0
    assert obs.port_oper["Ethernet0"] is True and obs.port_oper["Ethernet8"] is False
    port = next(p for p in obs.points if p.measurement == "lab_sonic_port" and p.tags["port"] == "Ethernet8")
    assert port.tags.get("wire") is None and port.fields["cabled"] is False and port.fields["admin_up"] is False


def test_bgp_peer_fields():
    obs, _, _ = build(load("sonic1.json"))
    peer = next(p for p in obs.points if p.measurement == "lab_bgp_peer")
    assert peer.tags == {"node": "sonic1", "vrf": "default", "peer": "10.0.12.2", "remote_as": 65002, "local_as": 65001, "peer_type": "ebgp"}
    assert peer.fields["state"] == "Established" and peer.fields["state_code"] == 6
    assert peer.fields["keepalive_s"] == 60.0 and peer.fields["description"] == "sonic2"
    afi = next(p for p in obs.points if p.measurement == "lab_bgp_afi")
    assert afi.fields["pfx_rcd"] == 2 and afi.fields["accepted"] == 2


def test_bgp_state_change_emits_event():
    data = load("sonic1.json")
    _, st, _ = build(data)
    down = copy.deepcopy(data)
    peer = down["bgp"]["neighbors"]["default"]["10.0.12.2"]
    peer["bgpState"] = "Active"
    peer["lastResetDueTo"] = "Interface down"
    obs, _, _ = build(down, st)
    events = [p for p in obs.points if p.measurement == "lab_event" and p.tags["program"] == "lab-collector"]
    assert len(events) == 1
    assert events[0].tags["type"] == "bgp" and events[0].tags["severity"] == "WARNING"
    assert "Established → Active" in events[0].fields["message"] and "Interface down" in events[0].fields["message"]
    peer_point = next(p for p in obs.points if p.measurement == "lab_bgp_peer")
    assert peer_point.fields["established"] is False and peer_point.fields["uptime_s"] == 0.0


def test_bgp_flap_between_polls_emits_event():
    """Session coupée puis rétablie entre deux relevés : l'état reste Established, l'uptime recule."""
    data = load("sonic1.json")
    _, st, _ = build(data)
    flapped = copy.deepcopy(data)
    peer = flapped["bgp"]["neighbors"]["default"]["10.0.12.2"]
    peer["bgpTimerUpMsec"] = 5000
    obs, _, _ = build(flapped, st)
    events = [p for p in obs.points if p.measurement == "lab_event" and p.tags["program"] == "lab-collector"]
    assert len(events) == 1 and events[0].tags["severity"] == "WARNING"
    assert "réinitialisé puis rétabli" in events[0].fields["message"]
    obs, _, _ = build(flapped, st)  # relevé suivant identique : rien de nouveau
    assert not [p for p in obs.points if p.measurement == "lab_event" and p.tags["program"] == "lab-collector"]


def test_service_cpu_rate_on_second_cycle():
    data = load("sonic1.json")
    _, st, _ = build(data)
    later = copy.deepcopy(data)
    later["uptime"] += 10
    for svc in later["services"]:
        if svc["name"] == "orchagent":
            svc["proc"]["cpu_seconds"] += 0.5
    obs, _, _ = build(later, st)
    orch = next(p for p in obs.points if p.measurement == "lab_sonic_service" and p.tags["service"] == "orchagent")
    assert abs(orch.fields["cpu_pct"] - 5.0) < 1e-6
    assert orch.fields["state_code"] == 2 and orch.fields["running"] is True


def test_crm_and_counters():
    obs, _, _ = build(load("sonic1.json"))
    crm = {(p.tags.get("stage"), p.tags.get("bind_point"), p.tags["resource"]): p.fields for p in obs.points if p.measurement == "lab_sonic_crm"}
    route = crm[(None, None, "ipv4_route")]
    assert route["used"] + route["available"] == route["total"] and 0 <= route["used_pct"] <= 100
    assert ("INGRESS", "PORT", "acl_table") in crm
    counters = [p for p in obs.points if p.measurement == "lab_sonic_counters"]
    assert any(p.tags["source"] == "NOTIFICATION_CONSUMER_STATS" for p in counters)


def test_syslog_totals_accumulate_then_reset():
    data = load("sonic1.json")
    obs, st, _ = build(data)
    total = sum(st.syslog_totals.values())
    assert total == sum(data["syslog"]["counts"].values())
    assert st.syslog == {"inode": data["syslog"]["inode"], "offset": data["syslog"]["end"]}
    events = [p for p in obs.points if p.measurement == "lab_event"]
    assert events and all(p.ts_ns != TS for p in events)  # horodatage du syslog, pas du cycle
    assert not any(p.fields["message"].startswith(":-") for p in events)
    incr = copy.deepcopy(data)
    incr["syslog"].update(reset=False, counts={"orchagent\tNOTICE": 3}, events=[])
    build(incr, st)
    assert sum(st.syslog_totals.values()) == total + 3
    build(data, st)  # relecture complète (conteneur recréé ou fichier tronqué)
    assert sum(st.syslog_totals.values()) == total


def test_section_errors_are_isolated():
    data = load("sonic1.json")
    data["bgp"] = {"summary": "inattendu", "neighbors": None}
    data["errors"] = {"routes": "RuntimeError: bgpd is not running"}
    obs, _, _ = build(data)
    assert obs.errors["routes"].startswith("RuntimeError")
    assert any(p.measurement == "lab_sonic_port" for p in obs.points)
