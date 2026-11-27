"""Décodage sFlow v5 : datagrammes réels de hsflowd (sonic1), puis cas construits."""

import pytest

from conftest import data, datagram, ethernet, flow_sample, ipv4, ipv6, l4, u32, vxlan
from labmon import sflow


def test_real_flow_datagram():
    dg = sflow.decode(data("sflow_flow.hex"))
    assert (dg.agent, dg.sub_agent, dg.counters, dg.others) == ("10.255.0.1", 100000, 0, 0)
    assert len(dg.flows) == 6
    sample = dg.flows[0]
    assert (sample.sampling_rate, sample.source_index, sample.input_if, sample.output_if) == (256, 5, 5, 0)
    assert (sample.frame_length, sample.stripped, sample.header_protocol, len(sample.header)) == (1246, 4, 1, 128)
    assert sflow.sonic_ifname(sample.port_index) == "Ethernet4"
    pkt = sflow.parse_packet(sample.header)
    assert (pkt.src, pkt.dst, pkt.proto, pkt.icmp_type) == ("192.168.2.10", "192.168.1.10", 1, 0)
    assert sflow.classify(pkt) == ("icmp", "icmp")


def test_real_counter_datagram_is_counted_only():
    dg = sflow.decode(data("sflow_counters.hex"))
    assert dg.flows == [] and dg.counters == 1 and dg.agent == "10.255.0.1"


@pytest.mark.parametrize(
    "header, expected",
    [
        (ethernet(ipv4("192.168.1.10", "192.168.2.10", 6, l4(40000, 80))), ("tcp", "http")),
        (ethernet(ipv4("192.168.2.10", "192.168.1.10", 6, l4(80, 40000))), ("tcp", "http")),
        (ethernet(ipv4("192.168.1.10", "192.168.2.10", 17, l4(40000, 53))), ("udp", "dns")),
        (ethernet(ipv4("192.168.1.10", "192.168.2.10", 6, l4(5201, 41000))), ("tcp", "iperf3")),
        (ethernet(ipv4("10.0.12.1", "10.0.12.2", 6, l4(179, 50000))), ("tcp", "bgp")),
        (ethernet(ipv4("192.168.1.10", "192.168.2.10", 6, l4(40000, 40001))), ("tcp", "autre-tcp")),
        (ethernet(ipv4("192.168.1.10", "192.168.2.10", 17, l4(40000, 40001))), ("udp", "autre-udp")),
        (ethernet(ipv4("192.168.1.10", "192.168.2.10", 47, b"\x00" * 8)), ("gre", "gre")),
        (ethernet(ipv6("2001:db8::1", "2001:db8::2", 6, l4(40000, 443)), ethertype=0x86DD), ("tcp", "https")),
        (ethernet(b"\x00" * 28, ethertype=0x0806), ("arp", "arp")),
        (ethernet(b"\x00" * 28, ethertype=0x88CC), ("lldp", "lldp")),
    ],
    ids=["http", "http-retour", "dns", "iperf3", "bgp", "tcp-autre", "udp-autre", "gre", "ipv6-https", "arp", "lldp"],
)
def test_classify(header, expected):
    assert sflow.classify(sflow.parse_packet(header)) == expected


def test_vlan_and_ipv4_fields():
    pkt = sflow.parse_packet(ethernet(ipv4("192.168.1.10", "192.168.2.10", 17, l4(1234, 53)), vlan=110))
    assert (pkt.vlan, pkt.ip_version, pkt.src, pkt.dst, pkt.sport, pkt.dport) == (110, 4, "192.168.1.10", "192.168.2.10", 1234, 53)


def test_vxlan_inner_packet():
    inner = ethernet(ipv4("10.10.1.11", "10.10.1.12", 6, l4(41000, 5201)))
    outer = ethernet(ipv4("10.255.1.1", "10.255.1.2", 17, l4(50000, 4789, b"\x00" * 4 + vxlan(10100, inner))))
    pkt = sflow.parse_packet(outer)
    assert pkt.vni == 10100 and pkt.inner is not None
    assert (pkt.inner.src, pkt.inner.dst) == ("10.10.1.11", "10.10.1.12")
    assert sflow.classify(pkt) == ("udp", "vxlan")
    assert sflow.classify(pkt.inner) == ("tcp", "iperf3")


def test_truncated_and_fragmented_headers_keep_what_is_known():
    full = ethernet(ipv4("192.168.1.10", "192.168.2.10", 6, l4(40000, 80)))
    pkt = sflow.parse_packet(full[:34])  # IPv4 complet, TCP absent
    assert (pkt.src, pkt.proto, pkt.sport) == ("192.168.1.10", 6, None)
    assert sflow.classify(pkt) == ("tcp", "tcp")
    frag = sflow.parse_packet(ethernet(ipv4("192.168.1.10", "192.168.2.10", 17, l4(1, 53), fragment=185)))
    assert frag.sport is None and frag.dport is None
    assert sflow.parse_packet(b"\x00" * 10) == sflow.Packet()


def test_constructed_datagram_both_sample_formats():
    header = ethernet(ipv4("192.168.1.10", "192.168.2.10", 6, l4(40000, 80)))
    dg = sflow.decode(datagram(flow_sample(header, rate=512, index=1, frame=1514), flow_sample(header, index=5, expanded=False)))
    assert [(s.sampling_rate, s.port_index, s.frame_length) for s in dg.flows] == [(512, 1, 1514), (256, 5, len(header) + 4)]
    assert dg.flows[0].header == header


@pytest.mark.parametrize(
    "raw, message",
    [
        (datagram(version=4), "version"),
        (datagram()[:20], "tronqué"),
        (u32(5, 3) + b"\x00" * 40, "type d'adresse"),
        (datagram()[:-4] + u32(sflow.MAX_SAMPLES + 1), "limite"),
        (datagram(u32(3, 4096) + b"\x00" * 8), "plus longue"),
    ],
    ids=["version", "tronque", "adresse", "trop-echantillons", "echantillon-trop-long"],
)
def test_bad_datagrams_are_rejected(raw, message):
    with pytest.raises(sflow.SflowError, match=message):
        sflow.decode(raw)


def test_unknown_samples_are_skipped():
    dg = sflow.decode(datagram(u32((9 << 12) | 7, 8) + b"\x00" * 8, u32(2, 4) + b"\x00" * 4))
    assert (len(dg.flows), dg.counters, dg.others) == (0, 1, 1)


def test_sonic_ifname():
    assert [sflow.sonic_ifname(i) for i in (1, 5, 125, 1001, 2010, 10000, 0, 99999)] == [
        "Ethernet0",
        "Ethernet4",
        "Ethernet124",
        "PortChannel1",
        "Vlan10",
        "eth0",
        None,
        None,
    ]
