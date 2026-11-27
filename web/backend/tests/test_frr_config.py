from app import frr_config as fc

RUNNING = """Building configuration...

Current configuration:
!
frr version 10.7.1
frr defaults traditional
hostname sonic1
zebra nexthop-group keep 1
fpm address 127.0.0.1
no fpm use-next-hop-groups
service integrated-vtysh-config
!
ip route 10.99.0.0/24 Null0
!
interface Ethernet0
 description lan
exit
!
route-map RM_SET_SRC permit 10
 set src 10.255.0.1
exit
!
router bgp 65001
 bgp router-id 10.255.0.1
 neighbor 10.0.12.2 remote-as 65002
 !
 address-family ipv4 unicast
  network 10.255.0.1/32
  network 192.168.1.0/24
 exit-address-family
exit
!
ip protocol bgp route-map RM_SET_SRC
!
ip nht resolve-via-default
!
ipv6 nht resolve-via-default
!
end
"""

STARTUP = """router bgp 65001
 bgp router-id 10.255.0.1
 neighbor 10.0.12.2 remote-as 65002
 !
 address-family ipv4 unicast
  network 10.255.0.1/32
  network 192.168.1.0/24
 exit-address-family
exit
!
"""


def test_normalize_strips_system_lines():
    out = fc.normalize(RUNNING)
    for noise in ("frr version", "fpm", "nht", "RM_SET_SRC", "hostname", "Building", "service integrated"):
        assert noise not in out
    assert out.startswith("ip route 10.99.0.0/24 Null0\n!\ninterface Ethernet0\n description lan\nexit\n!")
    assert "router bgp 65001\n bgp router-id 10.255.0.1" in out


def test_indentation_is_normalized():
    four_spaces = "router bgp 65001\n    bgp router-id 10.255.0.1\n    address-family ipv4 unicast\n        network 10.0.0.0/8\n    exit-address-family\nexit\n"
    one_space = "router bgp 65001\n bgp router-id 10.255.0.1\n address-family ipv4 unicast\n  network 10.0.0.0/8\n exit-address-family\nexit\n"
    assert fc.normalize(four_spaces) == fc.normalize(one_space)


def test_replace_plan_removes_missing_items_and_resets_router_blocks():
    desired = STARTUP.replace("  network 192.168.1.0/24\n", "")
    plan = fc.replace_plan(RUNNING, desired)
    assert plan.commands == [
        "no ip route 10.99.0.0/24 Null0",
        "interface Ethernet0",
        " no description lan",
        "exit",
        "no router bgp 65001",
    ]
    assert plan.resets == ["router bgp 65001"]
    assert set(plan.removed) == {"ip route 10.99.0.0/24 Null0", "interface Ethernet0"}


def test_replace_plan_only_additions_needs_no_removal():
    desired = STARTUP.replace("  network 192.168.1.0/24\n", "  network 192.168.1.0/24\n  network 172.16.0.0/16\n")
    desired += "interface Ethernet0\n description lan\nexit\nip route 10.99.0.0/24 Null0\n"
    plan = fc.replace_plan(RUNNING, desired)
    assert plan.commands == [] and plan.resets == []


def test_replace_plan_never_touches_system_lines():
    plan = fc.replace_plan(RUNNING, "")
    joined = "\n".join(plan.commands)
    assert "fpm" not in joined and "nht" not in joined and "RM_SET_SRC" not in joined
    assert "no router bgp 65001" in plan.commands


def test_negate():
    assert fc.negate("no bgp ebgp-requires-policy") == "bgp ebgp-requires-policy"
    assert fc.negate(" ip route 1.2.3.0/24 Null0") == "no ip route 1.2.3.0/24 Null0"


def test_merge_additions_lists_new_lines_with_context():
    desired = STARTUP.replace("  network 192.168.1.0/24\n", "  network 192.168.1.0/24\n  network 172.16.0.0/16\n")
    desired += "ip route 10.98.0.0/24 Null0\n"
    out = fc.merge_additions(RUNNING, desired)
    assert "router bgp 65001\n  network 172.16.0.0/16\nexit" in out
    assert "ip route 10.98.0.0/24 Null0" in out
    assert "10.255.0.1/32" not in out


def test_unified_diff_empty_when_only_system_lines_differ():
    running_like_startup = RUNNING.replace("ip route 10.99.0.0/24 Null0\n", "").replace(
        "interface Ethernet0\n description lan\nexit\n", ""
    )
    assert fc.unified_diff(running_like_startup, STARTUP) == ""


def test_equivalent_ignores_top_level_order_only():
    a = "router bgp 1\n neighbor 1.1.1.1 remote-as 2\nexit\n!\nip route 10.9.0.0/24 Null0\n!\n"
    b = "ip route 10.9.0.0/24 Null0\n!\nrouter bgp 1\n neighbor 1.1.1.1 remote-as 2\nexit\n!\n"
    assert fc.equivalent(a, b) and fc.unified_diff(a, b) != ""
    assert not fc.equivalent(a, b.replace("remote-as 2", "remote-as 3"))
    assert not fc.equivalent(a, "router bgp 1\n neighbor 1.1.1.1 remote-as 2\nexit\n")
    # dans un bloc, l'ordre compte (séquences de route-map, de prefix-list...)
    c = "router bgp 1\n neighbor 1.1.1.1 remote-as 2\n neighbor 1.1.1.1 description x\nexit\n"
    d = "router bgp 1\n neighbor 1.1.1.1 description x\n neighbor 1.1.1.1 remote-as 2\nexit\n"
    assert not fc.equivalent(c, d)


def test_clean_vtysh_error():
    raw = "line 2: % Unknown command[28]:  neighbor foo bar\nConfiguration file[/etc/frr/frr.conf] processing failure: 2\n"
    assert fc.clean_vtysh_error(raw) == "line 2: % Unknown command[28]:  neighbor foo bar"


def test_duplicate_blocks_are_merged():
    text = "router bgp 1\n neighbor 1.1.1.1 remote-as 2\nexit\nrouter bgp 1\n neighbor 1.1.1.1 description x\nexit\n"
    parsed = fc.parse(text)
    assert len(parsed.items) == 1
    assert parsed.items[0].children == (" neighbor 1.1.1.1 remote-as 2", " neighbor 1.1.1.1 description x")


VRF_RUNNING = """vrf VrfSTOCKAGE
 vni 50002
 ip nht resolve-via-default
exit-vrf
!
router bgp 65012 vrf VrfSTOCKAGE
 bgp router-id 10.1.0.13
exit
!
"""


def test_exit_vrf_closes_vrf_block():
    items = fc.parse(VRF_RUNNING).items
    assert [it.header for it in items] == ["vrf VrfSTOCKAGE", "router bgp 65012 vrf VrfSTOCKAGE"]
    assert items[0].children == (" vni 50002", " ip nht resolve-via-default")
    # la conf normalisée (sauvegarde, snapshots) ne contient plus de « exit-vrf » orphelin, refusé par vtysh
    assert "exit-vrf" not in fc.normalize(VRF_RUNNING)
    assert fc.replace_plan(VRF_RUNNING, VRF_RUNNING.replace("exit-vrf", "exit")).commands == []
    assert fc.equivalent(VRF_RUNNING, fc.normalize(VRF_RUNNING))

