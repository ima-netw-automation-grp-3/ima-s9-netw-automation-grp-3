from app import parsers
from app.remote import collect_sonic as cs

BUSYBOX_LINK = """1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN qlen 1000\\    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
2: eth0@if26: <BROADCAST,MULTICAST,UP,LOWER_UP,M-DOWN> mtu 1500 qdisc noqueue state UP \\    link/ether 3e:68:dc:d2:96:8e brd ff:ff:ff:ff:ff:ff
28: eth1@if29: <BROADCAST,MULTICAST,UP,LOWER_UP,M-DOWN> mtu 9500 qdisc noqueue state UP \\    link/ether aa:c1:ab:55:88:cb brd ff:ff:ff:ff:ff:ff
"""
BUSYBOX_ADDR = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 172.20.30.21/24 brd 172.20.30.255 scope global eth0\\       valid_lft forever preferred_lft forever
28: eth1    inet 192.168.1.10/24 scope global eth1\\       valid_lft forever preferred_lft forever
"""
BUSYBOX_ROUTE = """default via 172.20.30.1 dev eth0
10.0.0.0/8 via 192.168.1.1 dev eth1
192.168.1.0/24 dev eth1 scope link  src 192.168.1.10
"""
PROC_NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
  eth1:    1168      17    0   32    0     0          0         0     2692      27    0    0    0     0       0          0
"""


def test_busybox_ip_link():
    links = parsers.parse_ip_link(BUSYBOX_LINK)
    assert set(links) == {"lo", "eth0", "eth1"}
    assert links["eth1"]["mtu"] == 9500 and links["eth1"]["state"] == "UP" and links["eth1"]["up"]
    assert links["eth1"]["mac"] == "aa:c1:ab:55:88:cb"


def test_busybox_ip_addr_and_route():
    addrs = parsers.parse_ip_addr(BUSYBOX_ADDR)
    assert addrs["eth1"] == [{"family": "inet", "address": "192.168.1.10/24"}]
    routes = parsers.parse_ip_route(BUSYBOX_ROUTE)
    assert routes[1] == {"prefix": "10.0.0.0/8", "via": "192.168.1.1", "dev": "eth1", "src": None, "proto": None}
    assert routes[2]["src"] == "192.168.1.10"


def test_proc_net_dev():
    stats = parsers.parse_proc_net_dev(PROC_NET_DEV)
    assert stats["eth1"] == {
        "rx_bytes": 1168, "rx_packets": 17, "rx_errors": 0, "rx_dropped": 32,
        "tx_bytes": 2692, "tx_packets": 27, "tx_errors": 0, "tx_dropped": 0,
    }  # fmt: skip


def test_ping_busybox_and_iputils():
    busybox = "3 packets transmitted, 3 packets received, 0% packet loss\nround-trip min/avg/max = 0.676/0.865/1.198 ms\n"
    assert parsers.parse_ping(busybox) == {
        "transmitted": 3, "received": 3, "loss": 0.0, "rtt_min": 0.676, "rtt_avg": 0.865, "rtt_max": 1.198, "ok": True,
    }  # fmt: skip
    iputils = "2 packets transmitted, 0 received, 100% packet loss, time 1001ms\n"
    res = parsers.parse_ping(iputils)
    assert res["ok"] is False and res["loss"] == 100.0 and res["rtt_avg"] is None


def test_collect_sonic_parsers():
    services = cs.parse_supervisorctl(
        "bgpd                             RUNNING   pid 637, uptime 0:00:01\n"
        "start.sh                         EXITED    Nov 19 10:04 AM\n"
        "natsyncd                         STARTING  \n"
    )
    assert services[0] == {"name": "bgpd", "state": "RUNNING", "detail": "pid 637, uptime 0:00:01"}
    assert services[2] == {"name": "natsyncd", "state": "STARTING", "detail": ""}
    assert cs.parse_lanemap("eth1:25,26,27,28\neth2:29,30,31,32\n") == {"25,26,27,28": "eth1", "29,30,31,32": "eth2"}
    assert cs.parse_version_yml("---\nbuild_version: 'master.1'\nasic_type: vs\n") == {
        "build_version": "master.1",
        "asic_type": "vs",
    }


def test_collect_sonic_bgp_and_routes_summaries():
    bgp = cs.summarize_bgp(
        {
            "ipv4Unicast": {
                "routerId": "10.255.0.1",
                "as": 65001,
                "peers": {"10.0.12.2": {"remoteAs": 65002, "state": "Established", "pfxRcd": 2, "desc": "sonic2"}},
            }
        }
    )
    assert bgp["router_id"] == "10.255.0.1" and bgp["as"] == 65001
    assert bgp["peers"][0]["peer"] == "10.0.12.2" and bgp["peers"][0]["state"] == "Established"
    assert cs.summarize_bgp({}) == {"router_id": None, "as": None, "peers": []}
    routes = cs.summarize_routes(
        {"192.168.2.0/24": [{"protocol": "bgp", "selected": True, "nexthops": [{"ip": "10.0.12.2", "interfaceName": "Ethernet4", "active": True}]}]}
    )
    assert routes[0]["nexthops"][0] == {"ip": "10.0.12.2", "interface": "Ethernet4", "active": True, "connected": False}
