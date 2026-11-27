import json

import pytest
from conftest import HERE

from restconf import labmodule as lm
from restconf.rfc8040 import RestconfError


def test_bgp_neighbors_from_real_output():
    summary = json.loads((HERE / "fixtures" / "bgp-summary.json").read_text())
    neighbors = lm.bgp_neighbors(summary)
    assert neighbors == [
        {
            "address": "10.0.12.2",
            "remote-as": 65002,
            "state": "Established",
            "description": "sonic2",
            "uptime": neighbors[0]["uptime"],
            "prefixes-received": 2,
        }
    ]


def test_bgp_neighbors_merges_families_and_ignores_text_as():
    summary = {
        "ipv4Unicast": {"peers": {"10.0.0.2": {"remoteAs": 65002, "state": "Established", "pfxRcd": 3}}},
        "ipv6Unicast": {"peers": {"10.0.0.2": {"remoteAs": 65002, "state": "Established", "pfxRcd": 1}, "fe80::1": {"remoteAs": "external", "state": "Active"}}},
        "failedPeers": 0,
    }
    by_addr = {n["address"]: n for n in lm.bgp_neighbors(summary)}
    assert by_addr["10.0.0.2"]["prefixes-received"] == 4
    assert "remote-as" not in by_addr["fe80::1"] and by_addr["fe80::1"]["state"] == "Active"


def test_rpc_input():
    assert lm.rpc_input(None, "x") == {}
    assert lm.rpc_input({"sonic-lab:input": {"a": 1}}, "x") == {"a": 1}
    assert lm.rpc_input({"input": {}}, "x") == {}
    for bad in ({"sonic-lab:output": {}}, {"sonic-lab:input": 3}, [1]):
        with pytest.raises(RestconfError):
            lm.rpc_input(bad, "x")


def test_ping_input():
    assert lm.ping_input({"destination": "192.168.2.10"}) == ("192.168.2.10", None, 3)
    assert lm.ping_input({"destination": "h", "source": "Ethernet0", "count": "5"}) == ("h", "Ethernet0", 5)
    for bad in ({}, {"destination": "-f"}, {"destination": "a b"}, {"destination": "h", "count": 0},
                {"destination": "h", "count": 21}, {"destination": "h", "source": "-I"}, {"destination": "h", "x": 1},
                {"destination": "h", "count": True}):
        with pytest.raises(RestconfError):
            lm.ping_input(bad)


def test_ping_output():
    out = lm.ping_output({"transmitted": 3, "received": 2, "loss": 33.3333, "rtt_min": 0.1, "rtt_avg": 0.25, "rtt_max": 0.4, "ok": True}, "sortie")
    assert out == {
        "transmitted": 3, "received": 2, "success": True, "loss-percent": "33.3",
        "rtt-min": "0.100", "rtt-avg": "0.250", "rtt-max": "0.400", "output": "sortie",
    }
    assert lm.ping_output({"transmitted": 0, "received": 0, "loss": None, "ok": False}, "")["success"] is False


def test_preview_input():
    assert lm.preview_input({"path": "/restconf/data/sonic-port:sonic-port", "method": "PATCH", "data": {}}) == ("sonic-port:sonic-port", "PATCH", {})
    assert lm.preview_input({"path": "a:b", "method": "DELETE"}) == ("a:b", "DELETE", None)
    for bad in ({"method": "PUT", "data": {}}, {"path": "a:b", "method": "GET"}, {"path": "a:b", "method": "PUT"},
                {"path": "a:b", "method": "PUT", "data": {}, "x": 1}):
        with pytest.raises(RestconfError):
            lm.preview_input(bad)
