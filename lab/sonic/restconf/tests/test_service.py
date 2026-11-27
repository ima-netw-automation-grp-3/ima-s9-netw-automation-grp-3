import json

import pytest
from conftest import PORT

from restconf.rfc8040 import MEDIA_JSON

ETH0 = "/restconf/data/sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0"


def call(service, method, path, body=None, headers=None, raw=None):
    hdrs = {"Accept": MEDIA_JSON, "Host": "172.20.30.11"}
    data = b""
    if body is not None or raw is not None:
        data = raw if raw is not None else json.dumps(body).encode()
        hdrs["Content-Type"] = MEDIA_JSON
    hdrs.update(headers or {})
    resp = service.handle(method, path, hdrs, data)
    doc = json.loads(resp.body) if resp.body and resp.headers.get("Content-Type") == MEDIA_JSON else None
    return resp, doc


def error_tag(doc):
    return doc["ietf-restconf:errors"]["error"][0]["error-tag"]


def test_discovery_and_root(service):
    resp, _ = call(service, "GET", "/.well-known/host-meta")
    assert resp.status == 200 and b"rel='restconf'" in resp.body
    resp, doc = call(service, "GET", "/restconf")
    assert doc["ietf-restconf:restconf"]["yang-library-version"] == "2016-06-21"
    resp, doc = call(service, "GET", "/restconf/yang-library-version")
    assert doc == {"ietf-restconf:yang-library-version": "2016-06-21"}
    resp, doc = call(service, "GET", "/restconf/operations")
    assert set(doc["ietf-restconf:operations"]) == {"sonic-lab:ping", "sonic-lab:preview"}
    resp, doc = call(service, "GET", "/autre")
    assert resp.status == 404


def test_modules_state_and_schema_download(service):
    resp, doc = call(service, "GET", "/restconf/data/ietf-yang-library:modules-state")
    modules = {m["name"]: m for m in doc["ietf-yang-library:modules-state"]["module"]}
    assert modules["sonic-port"]["schema"] == "https://172.20.30.11/restconf/yang/sonic-port@2019-07-01"
    assert modules["sonic-lab"]["namespace"] == "urn:sonic-lab:yang:sonic-lab"
    resp, _ = call(service, "GET", "/restconf/yang/sonic-port@2019-07-01")
    assert resp.status == 200 and resp.headers["Content-Type"] == "application/yang" and b"module sonic-port" in resp.body
    resp, _ = call(service, "GET", "/restconf/yang/sonic-lab")
    assert resp.status == 200 and b"module sonic-lab" in resp.body
    assert call(service, "GET", "/restconf/yang/inconnu")[0].status == 404
    assert call(service, "GET", "/restconf/yang/sonic-port@1999-01-01")[0].status == 404


def test_get_resources(service):
    resp, doc = call(service, "GET", ETH0)
    assert resp.status == 200
    assert doc["sonic-port:PORT_LIST"][0]["mtu"] == 9100 and doc["sonic-port:PORT_LIST"][0]["name"] == "Ethernet0"
    resp, doc = call(service, "GET", ETH0 + "/admin_status")
    assert doc == {"sonic-port:admin_status": "up"}
    resp, doc = call(service, "GET", "/restconf/data/sonic-port:sonic-port?depth=2")
    assert doc == {"sonic-port:sonic-port": {"PORT": {}}}
    resp, doc = call(service, "GET", "/restconf/data/sonic-vlan:sonic-vlan")  # conteneur vide
    assert resp.status == 200 and doc == {"sonic-vlan:sonic-vlan": {}}
    resp, doc = call(service, "GET", "/restconf/data/sonic-port:sonic-port/PORT/PORT_LIST=Ethernet99")
    assert resp.status == 404 and error_tag(doc) == "invalid-value"
    resp, doc = call(service, "GET", "/restconf/data/sonic-port:sonic-port/NOPE")
    assert resp.status == 400 and error_tag(doc) == "unknown-element"
    resp, _ = call(service, "HEAD", ETH0)
    assert resp.status == 200 and resp.body  # le serveur HTTP retire le corps
    resp, doc = call(service, "GET", ETH0, headers={"Accept": "application/yang-data+xml"})
    assert resp.status == 406


def test_datastore(service):
    resp, doc = call(service, "GET", "/restconf/data")
    data = doc["ietf-restconf:data"]
    assert PORT in data and "sonic-lab:frr" in data and "ietf-yang-library:modules-state" in data
    resp, doc = call(service, "GET", "/restconf/data?content=config")
    assert "ietf-yang-library:modules-state" not in doc["ietf-restconf:data"]
    assert "state" not in doc["ietf-restconf:data"]["sonic-lab:frr"]
    resp, doc = call(service, "GET", "/restconf/data?depth=1")
    assert doc == {"ietf-restconf:data": {}}


def test_put_patch_post_delete(service, sonic):
    resp, _ = call(service, "PATCH", ETH0, {"sonic-port:PORT_LIST": [{"name": "Ethernet0", "description": "lan"}]})
    assert resp.status == 204
    assert sonic.configdb["PORT"]["Ethernet0"]["description"] == "lan"
    assert sonic.configdb["PORT"]["Ethernet0"]["mtu"] == "9100"  # merge : le reste est conservé
    assert sonic.applied[-1] == [{"op": "replace", "path": "/PORT/Ethernet0/description", "value": "lan"}]

    resp, _ = call(service, "PUT", ETH0 + "/mtu", {"sonic-port:mtu": 1500})
    assert resp.status == 204 and sonic.configdb["PORT"]["Ethernet0"]["mtu"] == "1500"

    body = {"sonic-interface:INTERFACE_LIST": [{"name": "Ethernet4"}]}
    resp, _ = call(service, "POST", "/restconf/data/sonic-interface:sonic-interface/INTERFACE", body)
    assert resp.status == 201
    assert resp.headers["Location"] == "/restconf/data/sonic-interface:sonic-interface/INTERFACE/INTERFACE_LIST=Ethernet4"
    assert "Ethernet4" in sonic.configdb["INTERFACE"]
    resp, doc = call(service, "POST", "/restconf/data/sonic-interface:sonic-interface/INTERFACE", body)
    assert resp.status == 409 and error_tag(doc) == "data-exists"

    path = "/restconf/data/sonic-interface:sonic-interface/INTERFACE/INTERFACE_IPPREFIX_LIST=Ethernet4,10.0.12.1%2F30"
    resp, _ = call(service, "PUT", path, {"sonic-interface:INTERFACE_IPPREFIX_LIST": [{}]})
    assert resp.status == 201 and "Ethernet4|10.0.12.1/30" in sonic.configdb["INTERFACE"]

    resp, _ = call(service, "DELETE", path)
    assert resp.status == 204 and "Ethernet4|10.0.12.1/30" not in sonic.configdb["INTERFACE"]
    resp, _ = call(service, "DELETE", path)
    assert resp.status == 404

    resp, _ = call(service, "PATCH", ETH0 + "/description", {"sonic-port:description": "x"})
    assert resp.status == 204
    resp, _ = call(service, "PATCH", "/restconf/data/sonic-port:sonic-port/PORT/PORT_LIST=Ethernet4/description", {"sonic-port:description": "x"})
    assert resp.status == 404  # PATCH ne crée pas

    resp, _ = call(service, "POST", "/restconf/data", {"sonic-vlan:sonic-vlan": {"VLAN": {"VLAN_LIST": [{"name": "Vlan10", "vlanid": 10}]}}})
    assert resp.status == 201 and sonic.configdb["VLAN"] == {"Vlan10": {"vlanid": "10"}}


def test_no_change_does_not_call_gcu(service, sonic):
    resp, _ = call(service, "PUT", ETH0 + "/mtu", {"sonic-port:mtu": 9100})
    assert resp.status == 204 and sonic.applied == []


def test_validation_errors(service, sonic):
    resp, doc = call(service, "PUT", ETH0 + "/mtu", {"sonic-port:mtu": "abc"})
    assert resp.status == 400
    err = doc["ietf-restconf:errors"]["error"][0]
    assert err["error-type"] == "application" and err["error-tag"] == "invalid-value"
    assert "abc" in err["error-message"] and err["error-path"].endswith("/mtu")
    resp, doc = call(service, "POST", "/restconf/data/sonic-interface:sonic-interface/INTERFACE",
                     {"sonic-interface:INTERFACE_LIST": [{"name": "Ethernet99"}]})
    assert resp.status == 400 and "leafref" in doc["ietf-restconf:errors"]["error"][0]["error-message"]
    sonic.refuse_apply = "No steps found"
    resp, doc = call(service, "PUT", ETH0 + "/mtu", {"sonic-port:mtu": 1500})
    assert resp.status == 400 and "No steps found" in doc["ietf-restconf:errors"]["error"][0]["error-message"]
    assert sonic.applied == []


def test_request_format_errors(service):
    assert call(service, "PUT", ETH0 + "/mtu", raw=b"{pas du json")[0].status == 400
    resp, _ = call(service, "PUT", ETH0 + "/mtu", {"sonic-port:mtu": 1}, headers={"Content-Type": "application/xml"})
    assert resp.status == 415
    resp, _ = call(service, "PATCH", ETH0, {"x": 1}, headers={"Content-Type": "application/yang-patch+json"})
    assert resp.status == 415
    resp, doc = call(service, "PUT", ETH0 + "/mtu", {"sonic-port:admin_status": "up"})
    assert resp.status == 400 and error_tag(doc) == "malformed-message"
    assert call(service, "PUT", ETH0 + "/mtu?depth=1", {"sonic-port:mtu": 1})[0].status == 400
    assert call(service, "GET", ETH0 + "?fields=mtu")[0].status == 400
    assert call(service, "PUT", ETH0 + "/mtu")[0].status == 400  # corps manquant
    resp, _ = call(service, "DELETE", ETH0 + "/description", {"x": 1})
    assert resp.status == 400


def test_options_and_methods(service):
    resp, _ = call(service, "OPTIONS", ETH0)
    assert "PATCH" in resp.headers["Allow"] and resp.headers["Accept-Patch"] == MEDIA_JSON
    resp, doc = call(service, "DELETE", "/restconf/data/sonic-lab:frr")
    assert resp.status == 405 and "PUT" in resp.headers["Allow"]
    resp, _ = call(service, "PUT", "/restconf/data/sonic-lab:frr/state", {"sonic-lab:state": {}})
    assert resp.status == 405
    assert call(service, "POST", "/restconf")[0].status == 405
    assert call(service, "GET", "/restconf/operations/sonic-lab:ping")[0].status == 405


def test_lab_frr(service, frr):
    resp, doc = call(service, "GET", "/restconf/data/sonic-lab:frr")
    assert doc["sonic-lab:frr"]["running-config"].startswith("router bgp 65001")
    assert doc["sonic-lab:frr"]["state"]["bgp-neighbor"][0]["state"] == "Established"
    resp, doc = call(service, "GET", "/restconf/data/sonic-lab:frr/state/bgp-neighbor=10.0.12.2/state")
    assert doc == {"sonic-lab:state": "Established"}
    resp, doc = call(service, "GET", "/restconf/data/sonic-lab:frr?content=nonconfig")
    assert "running-config" not in doc["sonic-lab:frr"]

    resp, _ = call(service, "PUT", "/restconf/data/sonic-lab:frr/running-config", {"sonic-lab:running-config": "router bgp 65001\n"})
    assert resp.status == 204 and frr.calls[-1] == ("replace", "router bgp 65001\n")
    resp, _ = call(service, "PATCH", "/restconf/data/sonic-lab:frr", {"sonic-lab:frr": {"running-config": "ip route 1.1.1.0/24 Null0\n"}})
    assert resp.status == 204 and frr.calls[-1][0] == "merge"
    resp, _ = call(service, "PATCH", "/restconf/data/sonic-lab:frr", {"sonic-lab:frr": {}})
    assert resp.status == 204 and len(frr.calls) == 2  # rien à fusionner
    resp, doc = call(service, "PUT", "/restconf/data/sonic-lab:frr", {"sonic-lab:frr": {}})
    assert resp.status == 400  # PUT sans running-config : refusé (effacerait tout)
    resp, doc = call(service, "PUT", "/restconf/data/sonic-lab:frr/running-config", {"sonic-lab:running-config": "bogus\n"})
    assert resp.status == 400 and "Unknown command" in doc["ietf-restconf:errors"]["error"][0]["error-message"]
    resp, _ = call(service, "PUT", "/restconf/data/sonic-lab:frr", {"sonic-lab:frr": {"running-config": "x", "state": {}}})
    assert resp.status == 400


def test_ping(service, frr):
    resp, doc = call(service, "POST", "/restconf/operations/sonic-lab:ping",
                     {"sonic-lab:input": {"destination": "192.168.2.10", "source": "192.168.1.1", "count": 2}})
    assert resp.status == 200
    out = doc["sonic-lab:output"]
    assert out["success"] is True and out["received"] == 2 and out["rtt-avg"] == "0.200"
    assert frr.pings == [("192.168.2.10", "192.168.1.1", 2)]
    resp, doc = call(service, "POST", "/restconf/operations/sonic-lab:ping", {"sonic-lab:input": {"destination": "-f"}})
    assert resp.status == 400
    assert call(service, "POST", "/restconf/operations/sonic-lab:inconnue", {})[0].status == 404


@pytest.mark.parametrize(
    ("inp", "valid"),
    [
        ({"path": ETH0 + "/mtu", "method": "PUT", "data": {"sonic-port:mtu": 1500}}, True),
        ({"path": ETH0 + "/mtu", "method": "PUT", "data": {"sonic-port:mtu": "abc"}}, False),
        ({"path": "sonic-port:sonic-port/NOPE", "method": "DELETE"}, False),
        ({"path": "sonic-port:sonic-port/PORT/PORT_LIST=Ethernet4/description", "method": "DELETE"}, False),
        ({"path": "sonic-lab:frr/running-config", "method": "PUT", "data": {"sonic-lab:running-config": "router bgp 1\n"}}, True),
        ({"path": "sonic-lab:frr/running-config", "method": "PUT", "data": {"sonic-lab:running-config": "bogus\n"}}, False),
        ({"path": "sonic-lab:frr", "method": "DELETE"}, False),
    ],
)
def test_preview(service, sonic, inp, valid):
    configdb = json.loads(json.dumps(sonic.configdb))
    resp, doc = call(service, "POST", "/restconf/operations/sonic-lab:preview", {"sonic-lab:input": inp})
    assert resp.status == 200
    out = doc["sonic-lab:output"]
    assert out["valid"] is valid, out
    assert (not valid) == ("errors" in out)
    assert sonic.configdb == configdb and sonic.applied == []  # rien n'est appliqué


def test_preview_reports_patch_and_runs_gcu_dry_run(service, sonic):
    inp = {"path": ETH0 + "/mtu", "method": "PUT", "data": {"sonic-port:mtu": 1500}}
    _, doc = call(service, "POST", "/restconf/operations/sonic-lab:preview", {"sonic-lab:input": inp})
    out = doc["sonic-lab:output"]
    assert json.loads(out["patch"]) == [{"op": "replace", "path": "/PORT/Ethernet0/mtu", "value": "1500"}]
    lines = out["diff"].splitlines()
    assert any(ln.startswith("-") and '"mtu": 9100' in ln for ln in lines)
    assert any(ln.startswith("+") and '"mtu": 1500' in ln for ln in lines)
    assert sonic.dry_runs == [json.loads(out["patch"])]
    sonic.refuse_apply = "patch refusé"
    _, doc = call(service, "POST", "/restconf/operations/sonic-lab:preview", {"sonic-lab:input": inp})
    assert doc["sonic-lab:output"] == {"valid": False, "errors": "patch refusé"}
