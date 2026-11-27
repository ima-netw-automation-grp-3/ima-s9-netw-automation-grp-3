from restconf import configdb_patch as cp


def test_diff_levels():
    before = {
        "PORT": {"Ethernet0": {"mtu": "9100", "description": "a"}, "Ethernet4": {"mtu": "9100"}},
        "INTERFACE": {"Ethernet0": {}, "Ethernet0|192.168.1.1/24": {}},
        "VLAN": {"Vlan1": {"vlanid": "1"}},
    }
    after = {
        "PORT": {"Ethernet0": {"mtu": "1500", "admin_status": "up"}, "Ethernet4": {"mtu": "9100"}},
        "INTERFACE": {"Ethernet0": {}, "Ethernet0|10.0.0.1/24": {}},
        "LOOPBACK_INTERFACE": {"Loopback0": {}},
    }
    assert cp.diff(before, after) == [  # ordre : tables, clés et champs triés (GCU réordonne lui-même)
        {"op": "add", "path": "/INTERFACE/Ethernet0|10.0.0.1~124", "value": {}},
        {"op": "remove", "path": "/INTERFACE/Ethernet0|192.168.1.1~124"},
        {"op": "add", "path": "/LOOPBACK_INTERFACE", "value": {"Loopback0": {}}},
        {"op": "add", "path": "/PORT/Ethernet0/admin_status", "value": "up"},
        {"op": "remove", "path": "/PORT/Ethernet0/description"},
        {"op": "replace", "path": "/PORT/Ethernet0/mtu", "value": "1500"},
        {"op": "remove", "path": "/VLAN"},
    ]
    assert cp.diff(before, before) == []


def test_escape_and_lists():
    assert cp.escape("a/b~c") == "a~1b~0c"
    before = {"VLAN": {"Vlan5": {"dhcp_servers": ["1.1.1.1"]}}}
    after = {"VLAN": {"Vlan5": {"dhcp_servers": ["1.1.1.1", "2.2.2.2"]}}}
    assert cp.diff(before, after) == [{"op": "replace", "path": "/VLAN/Vlan5/dhcp_servers", "value": ["1.1.1.1", "2.2.2.2"]}]


def test_unified_diff():
    text = cp.unified_diff({"a": 1}, {"a": 2})
    assert "-  \"a\": 1" in text and "+  \"a\": 2" in text
    assert cp.unified_diff({"a": 1}, {"a": 1}) == ""
