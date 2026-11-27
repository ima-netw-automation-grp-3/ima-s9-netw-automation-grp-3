import copy

import pytest
from conftest import INTF, PORT, SCHEMA, VLAN, FakeSonic

from restconf import datatree as dt
from restconf.rfc8040 import RestconfError, Segment, parse_path


def target(path: str) -> dt.Target:
    return dt.resolve(SCHEMA, parse_path(path))


@pytest.fixture
def tree():
    return FakeSonic().read()


def test_resolve_builds_steps():
    t = target("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0/mtu")
    assert t.module == "sonic-port" and t.top == PORT
    assert t.schema_path == (PORT, "PORT", "PORT_LIST", "mtu")
    assert t.member == "sonic-port:mtu" and not t.is_instance
    assert target("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0").is_instance


@pytest.mark.parametrize(
    ("path", "tag"),
    [
        ("sonic-port:sonic-port/NOPE", "unknown-element"),
        ("sonic-port:sonic-port/PORT/PORT_LIST/mtu", "invalid-value"),  # clés manquantes au milieu
        ("sonic-port:sonic-port/PORT/PORT_LIST=a,b", "invalid-value"),  # trop de clés
        ("sonic-port:sonic-port/PORT=x", "invalid-value"),  # « = » sur un conteneur
        ("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0/mtu/x", "invalid-value"),  # après une feuille
        ("sonic-port:sonic-port/sonic-vlan:VLAN", "unknown-element"),  # autre module
        ("unknown:thing", "unknown-element"),
    ],
)
def test_resolve_errors(path, tag):
    with pytest.raises(RestconfError) as exc:
        target(path)
    assert exc.value.status == 400 and exc.value.tag == tag


def test_resolve_requires_module_on_first_segment():
    with pytest.raises(RestconfError):
        dt.resolve(SCHEMA, [Segment(None, "sonic-port")])


def test_find(tree):
    assert dt.find(tree, target("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0/mtu")) == 9100
    entry = dt.find(tree, target("sonic-interface:sonic-interface/INTERFACE/INTERFACE_IPPREFIX_LIST=Ethernet0,192.168.1.1%2F24"))
    assert entry == {"name": "Ethernet0", "ip-prefix": "192.168.1.1/24"}
    assert len(dt.find(tree, target("sonic-port:sonic-port/PORT/PORT_LIST"))) == 3  # liste entière
    for missing in ("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet99", "sonic-port:sonic-port/PORT/PORT_LIST=Ethernet4/description"):
        with pytest.raises(dt.Missing):
            dt.find(tree, target(missing))


def test_wrap_and_unwrap():
    t = target("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0")
    assert dt.wrap(t, {"name": "Ethernet0"}) == {"sonic-port:PORT_LIST": [{"name": "Ethernet0"}]}
    # clé absente du corps : reprise de l'URI ; préfixe du même module toléré
    assert dt.unwrap(t, {"sonic-port:PORT_LIST": [{"sonic-port:mtu": 1500}]}) == {"mtu": 1500, "name": "Ethernet0"}
    leaf = target("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0/mtu")
    assert dt.unwrap(leaf, {"sonic-port:mtu": 1500}) == 1500
    assert dt.unwrap(leaf, {"mtu": 1500}) == 1500  # nom non qualifié accepté
    bad_bodies = [
        (t, {"sonic-port:PORT_LIST": [{"name": "Ethernet4"}]}),  # clé différente de l'URI
        (t, {"sonic-port:PORT_LIST": [{}, {}]}),
        (t, {"sonic-port:PORT": {}}),
        (leaf, {"sonic-port:mtu": {"x": 1}}),
        (target("sonic-port:sonic-port/PORT"), {"sonic-port:PORT": 3}),
        (leaf, {"a": 1, "b": 2}),
    ]
    for tg, body in bad_bodies:
        with pytest.raises(RestconfError):
            dt.unwrap(tg, body)


def test_put_creates_and_replaces(tree):
    t = target("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0")
    assert dt.put(tree, t, {"name": "Ethernet0", "mtu": 1500}) is False
    assert dt.find(tree, t) == {"name": "Ethernet0", "mtu": 1500}  # remplacé : les autres champs disparaissent
    created = target("sonic-vlan:sonic-vlan/VLAN/VLAN_LIST=Vlan100/vlanid")
    assert dt.put(tree, created, 100) is True  # ancêtres créés, clé posée
    assert tree[VLAN] == {"VLAN": {"VLAN_LIST": [{"name": "Vlan100", "vlanid": 100}]}}
    with pytest.raises(RestconfError):
        dt.put(tree, target("sonic-port:sonic-port/PORT/PORT_LIST"), [])


def test_merge(tree):
    port = target("sonic-port:sonic-port/PORT")
    dt.merge(SCHEMA, tree, port, {"PORT_LIST": [{"name": "Ethernet0", "mtu": 1500}, {"name": "Ethernet12", "mtu": 9100}]})
    entries = {e["name"]: e for e in tree[PORT]["PORT"]["PORT_LIST"]}
    assert entries["Ethernet0"]["mtu"] == 1500 and entries["Ethernet0"]["description"] == "client1"  # fusion
    assert entries["Ethernet12"] == {"name": "Ethernet12", "mtu": 9100}  # ajout
    leaf = target("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet4/description")
    with pytest.raises(RestconfError) as exc:  # PATCH ne crée pas de feuille
        dt.merge(SCHEMA, tree, leaf, "x")
    assert exc.value.status == 404
    dt.merge(SCHEMA, tree, target("sonic-vlan:sonic-vlan"), {"VLAN": {"VLAN_LIST": [{"name": "Vlan5", "dhcp_servers": ["1.1.1.1"]}]}})
    dt.merge(SCHEMA, tree, target("sonic-vlan:sonic-vlan/VLAN/VLAN_LIST=Vlan5"), {"name": "Vlan5", "dhcp_servers": ["1.1.1.1", "2.2.2.2"]})
    assert tree[VLAN]["VLAN"]["VLAN_LIST"][0]["dhcp_servers"] == ["1.1.1.1", "2.2.2.2"]


def test_create(tree):
    parent = parse_path("sonic-port:sonic-port/PORT")
    t = dt.create(SCHEMA, tree, parent, {"sonic-port:PORT_LIST": [{"name": "Ethernet12", "mtu": 9100}]})
    assert t.uri == "sonic-port:sonic-port/PORT/PORT_LIST=Ethernet12"
    with pytest.raises(RestconfError) as exc:
        dt.create(SCHEMA, tree, parent, {"sonic-port:PORT_LIST": [{"name": "Ethernet12"}]})
    assert exc.value.status == 409 and exc.value.tag == "data-exists"
    top = dt.create(SCHEMA, tree, [], {"sonic-vlan:sonic-vlan": {"VLAN": {"VLAN_LIST": [{"name": "Vlan1"}]}}})
    assert top.uri == "sonic-vlan:sonic-vlan" and tree[VLAN]["VLAN"]["VLAN_LIST"] == [{"name": "Vlan1"}]
    for body in ({"sonic-port:PORT_LIST": [{"mtu": 1}]}, {"sonic-vlan:VLAN": {}}, {"a": 1, "b": 2}):
        with pytest.raises(RestconfError):
            dt.create(SCHEMA, tree, parent, body)
    with pytest.raises(RestconfError):  # datastore : nom qualifié obligatoire
        dt.create(SCHEMA, tree, [], {"sonic-vlan": {}})


def test_delete_and_prune(tree):
    dt.delete(tree, target("sonic-interface:sonic-interface/INTERFACE/INTERFACE_IPPREFIX_LIST=Ethernet0,192.168.1.1%2F24"))
    dt.delete(tree, target("sonic-interface:sonic-interface/INTERFACE/INTERFACE_LIST=Ethernet0"))
    dt.prune(tree)
    assert INTF not in tree
    with pytest.raises(RestconfError) as exc:
        dt.delete(tree, target("sonic-interface:sonic-interface/INTERFACE/INTERFACE_LIST=Ethernet0"))
    assert exc.value.status == 404
    dt.delete(tree, target("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0/description"))
    assert "description" not in dt.find(tree, target("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0"))


def test_filter_content_and_depth():
    from restconf import labmodule

    frr = {"running-config": "router bgp 1\n", "state": {"bgp-neighbor": [{"address": "10.0.0.1", "state": "Established"}]}}
    path = (labmodule.FRR,)
    assert dt.filter_content(labmodule.SCHEMA, path, frr, "config") == {"running-config": "router bgp 1\n"}
    assert dt.filter_content(labmodule.SCHEMA, path, frr, "nonconfig") == {"state": frr["state"]}
    assert dt.filter_content(labmodule.SCHEMA, path, frr, "all") is frr
    assert dt.filter_content(SCHEMA, (PORT,), {"PORT": {"PORT_LIST": [{"name": "E0"}]}}, "nonconfig") is None
    value = {"PORT": {"PORT_LIST": [{"name": "E0", "mtu": 1}]}}
    assert dt.limit_depth(value, 1) == {}
    assert dt.limit_depth(value, 2) == {"PORT": {}}
    assert dt.limit_depth(value, 3) == {"PORT": {"PORT_LIST": [{}]}}
    assert dt.limit_depth(value, 4) == value and dt.limit_depth(value, None) is value


def test_strip_module_keeps_other_modules():
    value = {"sonic-port:PORT": {"x": 1, "other:y": 2}}
    assert dt.strip_module(copy.deepcopy(value), "sonic-port") == {"PORT": {"x": 1, "other:y": 2}}
