import pytest

from app import configdb_diff as cd
from app.errors import BadRequest


def base_running():
    return {
        "DEVICE_METADATA": {"localhost": {"hostname": "sonic1", "mac": "aa:bb:cc:dd:ee:ff", "hwsku": "Force10-S6000"}},
        "PORT": {
            "Ethernet0": {"admin_status": "up", "lanes": "25,26,27,28", "mtu": "9100"},
            "Ethernet4": {"admin_status": "up", "lanes": "29,30,31,32", "mtu": "9100"},
        },
        "INTERFACE": {
            "Ethernet0": {},
            "Ethernet0|192.168.1.1/24": {},
            "Ethernet4": {},
            "Ethernet4|10.0.12.1/30": {},
        },
        "LOGGER": {"orchagent": {"LOGLEVEL": "NOTICE"}},
    }


def test_normalize_converts_values_and_drops_null():
    cfg = cd.normalize({"PORT": {"Ethernet0": {"mtu": 9100, "fec": True, "NULL": "NULL"}}, "VLAN": {"Vlan10": None}})
    assert cfg == {"PORT": {"Ethernet0": {"mtu": "9100", "fec": "true"}}, "VLAN": {"Vlan10": {}}}


@pytest.mark.parametrize(
    "bad",
    [[], {"PORT": []}, {"PORT": {"Ethernet0": "up"}}, {"PORT": {"Ethernet0": {"x": {"nested": 1}}}}],
)
def test_normalize_rejects_bad_structure(bad):
    with pytest.raises(BadRequest):
        cd.normalize(bad)


def test_merge_only_adds_and_updates():
    running = base_running()
    desired = {
        "PORT": {"Ethernet4": {"admin_status": "down"}},
        "INTERFACE": {"Ethernet0|192.168.1.2/24": {}},
    }
    plan = cd.plan(running, desired, "merge")
    assert [(o.op, o.table, o.key, o.data) for o in plan.ops] == [
        ("mod", "PORT", "Ethernet4", {"admin_status": "down"}),
        ("mod", "INTERFACE", "Ethernet0|192.168.1.2/24", {}),
    ]
    assert plan.summary == {"add": 1, "modify": 1, "delete": 0}
    # rien n'est supprimé en merge
    assert "Ethernet0|192.168.1.1/24" in plan.result["INTERFACE"]
    assert plan.result["PORT"]["Ethernet4"]["lanes"] == "29,30,31,32"


def test_merge_without_changes_has_no_ops():
    plan = cd.plan(base_running(), {"PORT": {"Ethernet0": {"admin_status": "up"}}}, "merge")
    assert plan.ops == [] and plan.changes == []


def test_replace_deletes_missing_keys_and_orders_operations():
    running = base_running()
    desired = {
        "DEVICE_METADATA": {"localhost": {"hostname": "sonic1"}},
        "PORT": {"Ethernet0": {"admin_status": "up", "lanes": "25,26,27,28", "mtu": "9100"}},
        "INTERFACE": {"Ethernet0": {}, "Ethernet0|192.168.9.1/24": {}},
        "VLAN": {"Vlan10": {"vlanid": "10"}},
        "VLAN_MEMBER": {"Vlan10|Ethernet4": {"tagging_mode": "untagged"}},
    }
    plan = cd.plan(running, desired, "replace")
    ops = [(o.op, o.table, o.key) for o in plan.ops]
    # suppressions d'abord, IP avant l'interface de base
    assert ops[:3] == [
        ("delete", "INTERFACE", "Ethernet0|192.168.1.1/24"),
        ("delete", "INTERFACE", "Ethernet4|10.0.12.1/30"),
        ("delete", "INTERFACE", "Ethernet4"),
    ]
    writes = ops[3:]
    assert writes.index(("set", "VLAN", "Vlan10")) < writes.index(("set", "VLAN_MEMBER", "Vlan10|Ethernet4"))
    assert ("set", "INTERFACE", "Ethernet0|192.168.9.1/24") in writes
    # tables protégées : jamais de suppression (Ethernet4, LOGGER, mac conservés)
    assert "Ethernet4" in plan.result["PORT"]
    assert plan.result["LOGGER"] == running["LOGGER"]
    assert plan.result["DEVICE_METADATA"]["localhost"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert not any(o.table in ("PORT", "LOGGER", "DEVICE_METADATA") and o.op == "delete" for o in plan.ops)


def test_replace_removes_tables_absent_from_desired():
    running = base_running()
    running["STATIC_ROUTE"] = {"10.99.0.0/24": {"nexthop": "10.0.12.2"}}
    desired = {k: v for k, v in base_running().items() if k != "LOGGER"}
    plan = cd.plan(running, desired, "replace")
    assert [(o.op, o.table, o.key) for o in plan.ops] == [("delete", "STATIC_ROUTE", "10.99.0.0/24")]
    assert "STATIC_ROUTE" not in plan.result


def test_replace_replaces_modified_entry_entirely():
    running = {"VLAN": {"Vlan10": {"vlanid": "10", "mtu": "9100"}}}
    plan = cd.plan(running, {"VLAN": {"Vlan10": {"vlanid": "10"}}}, "replace")
    assert [(o.op, o.data) for o in plan.ops] == [("set", {"vlanid": "10"})]
    assert plan.changes[0].fields[0].to_dict() == {"name": "mtu", "before": "9100", "after": None}


def test_strip_volatile_and_diff_ignore_runtime_data():
    running = base_running()
    startup = cd.strip_volatile(running)
    assert "LOGGER" not in startup
    assert "mac" not in startup["DEVICE_METADATA"]["localhost"]
    assert "mac" in running["DEVICE_METADATA"]["localhost"]  # copie, pas de mutation
    assert cd.diff(startup, running) == []
    running["PORT"]["Ethernet4"]["admin_status"] = "down"
    changes = cd.diff(startup, running)
    assert [(c.action, c.table, c.key) for c in changes] == [("modify", "PORT", "Ethernet4")]


def test_unknown_mode_rejected():
    with pytest.raises(BadRequest):
        cd.plan({}, {}, "overwrite")  # type: ignore[arg-type]


def test_is_protected():
    for t in ("PORT", "BUFFER_PG", "DSCP_TO_TC_MAP", "MAP_PFC_PRIORITY_TO_QUEUE", "QUEUE", "LOGGER"):
        assert cd.is_protected(t)
    for t in ("INTERFACE", "VLAN", "STATIC_ROUTE", "ACL_RULE"):
        assert not cd.is_protected(t)


def test_replace_resets_port_fields_but_keeps_hardware_fields():
    running = {
        "PORT": {
            "Ethernet8": {
                "lanes": "33,34,35,36", "alias": "fortyGigE0/8", "index": "2", "speed": "100000",
                "admin_status": "up", "mtu": "9000", "description": "test", "mode": "access",
            },
            "Ethernet12": {"lanes": "37,38,39,40", "alias": "fortyGigE0/12"},
        }
    }  # fmt: skip
    # conf de démarrage complète : les champs ajoutés au runtime disparaissent
    startup = {"PORT": {"Ethernet8": {"lanes": "33,34,35,36", "alias": "fortyGigE0/8", "index": "2", "speed": "100000"}}}
    plan = cd.plan(running, startup, "replace")
    assert [(o.op, o.table, o.key) for o in plan.ops] == [("set", "PORT", "Ethernet8")]
    # champs du runtime retirés, admin_status / mtu remis explicitement à leur valeur par défaut
    assert plan.result["PORT"]["Ethernet8"] == {**startup["PORT"]["Ethernet8"], "admin_status": "down", "mtu": "9100"}
    assert cd.diff(startup, {"PORT": {"Ethernet8": plan.result["PORT"]["Ethernet8"]}}) == []
    assert "Ethernet12" in plan.result["PORT"]  # jamais de suppression de port
    # extrait partiel : les champs matériels absents sont conservés
    partial = cd.plan(running, {"PORT": {"Ethernet8": {"admin_status": "down"}}}, "replace")
    assert partial.result["PORT"]["Ethernet8"] == {
        "lanes": "33,34,35,36", "alias": "fortyGigE0/8", "index": "2", "speed": "100000",
        "admin_status": "down", "mtu": "9100",
    }  # fmt: skip


def test_replace_writes_defaults_for_removed_port_fields():
    hw = {"lanes": "33,34,35,36", "alias": "fortyGigE0/8"}
    running = {"PORT": {"Ethernet8": {**hw, "admin_status": "up", "mtu": "9000", "description": "x"}}}
    plan = cd.plan(running, {"PORT": {"Ethernet8": dict(hw)}}, "replace")
    # les démons n'appliquent pas de défaut quand un champ disparaît : il est écrit explicitement
    assert plan.ops[0].data == {**hw, "admin_status": "down", "mtu": "9100"}
    # idempotent : un second replace ne fait plus rien
    assert cd.plan(plan.result, {"PORT": {"Ethernet8": dict(hw)}}, "replace").ops == []
    # et la comparaison avec la conf de démarrage est propre
    assert cd.diff({"PORT": {"Ethernet8": dict(hw)}}, plan.result) == []


def test_preserve_representation_keeps_equivalent_entries():
    reference = {"PORT": {"Ethernet0": {"lanes": "1", "mtu": "9100"}, "Ethernet8": {"lanes": "2"}}}
    running = {"PORT": {"Ethernet0": {"lanes": "1"}, "Ethernet8": {"lanes": "2", "admin_status": "down", "mtu": "9100"}}}
    assert cd.preserve_representation(running, reference) == reference  # équivalent : forme d'origine conservée
    running["PORT"]["Ethernet8"]["admin_status"] = "up"
    assert cd.preserve_representation(running, reference)["PORT"]["Ethernet8"]["admin_status"] == "up"
