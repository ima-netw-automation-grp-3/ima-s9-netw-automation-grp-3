"""Décodage des datagrammes sFlow v5 des routeurs SONiC (hsflowd), bibliothèque standard seule.

On ne garde que ce qui sert à voir les types de trafic :
  - les échantillons de flux (format 1, et format étendu 3, celui de hsflowd) : taux
    d'échantillonnage, port d'entrée, taille de la trame et en-tête brut du paquet (Ethernet) ;
  - les échantillons de compteurs (formats 2 et 4) sont seulement comptés : le collecteur relève déjà
    les compteurs des interfaces.

Les datagrammes viennent du réseau : tout est borné (échantillons, enregistrements, taille d'en-tête)
et une incohérence lève SflowError, qui fait écarter le datagramme entier. L'analyse de l'en-tête du
paquet, elle, s'arrête simplement là où l'en-tête est tronqué.

Numérotation des interfaces : hsflowd rapporte l'index SONiC du port (table PORT_INDEX_TABLE, écrite
par le bootstrap des routeurs comme le fait port_index_mapper dans SONiC) : EthernetN = N + 1,
PortChannelN = 1000 + N, VlanN = 2000 + N (sonic_py_common.port_util).
"""

from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass, field, replace

MAX_SAMPLES = 64  # par datagramme (hsflowd en met une demi-douzaine dans 1400 octets)
MAX_RECORDS = 16  # par échantillon
MAX_HEADER = 256  # octets d'en-tête de paquet conservés (hsflowd en envoie 128)
VXLAN_PORT = 4789

ETH_IPV4, ETH_IPV6, ETH_ARP, ETH_VLAN, ETH_QINQ, ETH_QINQ_OLD = 0x0800, 0x86DD, 0x0806, 0x8100, 0x88A8, 0x9100
ETHERTYPES = {ETH_ARP: "arp", 0x88CC: "lldp", 0x8809: "lacp", 0x8847: "mpls", 0x8848: "mpls", 0x88F7: "ptp"}
IP_PROTOCOLS = {
    1: "icmp",
    2: "igmp",
    6: "tcp",
    17: "udp",
    41: "ipv6",
    47: "gre",
    50: "esp",
    58: "icmpv6",
    89: "ospf",
    103: "pim",
    112: "vrrp",
    132: "sctp",
}
# ports bien connus -> application (le port du serveur, en destination ou en source)
APPS = {
    ("tcp", 20): "ftp",
    ("tcp", 21): "ftp",
    ("tcp", 22): "ssh",
    ("tcp", 23): "telnet",
    ("tcp", 25): "smtp",
    ("udp", 53): "dns",
    ("tcp", 53): "dns",
    ("udp", 67): "dhcp",
    ("udp", 68): "dhcp",
    ("tcp", 80): "http",
    ("tcp", 8080): "http",
    ("udp", 123): "ntp",
    ("udp", 161): "snmp",
    ("udp", 162): "snmp",
    ("tcp", 179): "bgp",
    ("tcp", 443): "https",
    ("udp", 443): "quic",
    ("udp", 514): "syslog",
    ("tcp", 830): "netconf",
    ("udp", 3784): "bfd",
    ("udp", 3785): "bfd",
    ("udp", 4789): "vxlan",
    ("tcp", 5201): "iperf3",
    ("udp", 5201): "iperf3",
    ("tcp", 5202): "iperf3",  # second serveur de make traffic (UDP), avec sa connexion de contrôle TCP
    ("udp", 5202): "iperf3",
    ("udp", 6343): "sflow",
}
_IPV6_EXTENSIONS = {0, 43, 60}  # hop-by-hop, routage, options de destination (longueur en octets/8 - 1)
_IPV6_FRAGMENT = 44


class SflowError(ValueError):
    """Datagramme illisible ou hors des limites : il est écarté entier."""


@dataclass(frozen=True)
class Packet:
    """Ce que l'en-tête échantillonné dit du paquet (None : absent ou tronqué)."""

    ethertype: int | None = None
    vlan: int | None = None
    ip_version: int | None = None
    src: str | None = None
    dst: str | None = None
    proto: int | None = None
    sport: int | None = None
    dport: int | None = None
    icmp_type: int | None = None
    vni: int | None = None
    inner: Packet | None = None  # paquet encapsulé (VXLAN)


@dataclass(frozen=True)
class FlowSample:
    sampling_rate: int
    sample_pool: int
    drops: int
    source_index: int  # index SONiC du port échantillonné (data source)
    input_if: int
    output_if: int
    frame_length: int = 0
    stripped: int = 0
    header_protocol: int | None = None
    header: bytes = b""

    @property
    def port_index(self) -> int:
        """Port d'entrée, à défaut la source de données (échantillonnage en entrée dans SONiC-VS)."""
        return self.input_if or self.source_index


@dataclass
class Datagram:
    agent: str
    sub_agent: int
    sequence: int
    uptime_ms: int
    flows: list[FlowSample] = field(default_factory=list)
    counters: int = 0  # échantillons de compteurs, ignorés
    others: int = 0  # échantillons d'un autre type (drops, extensions d'éditeurs…)


class _Reader:
    """Lecture XDR bornée d'une tranche [pos, end) de données."""

    def __init__(self, data: bytes, pos: int = 0, end: int | None = None) -> None:
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def u32(self) -> int:
        if self.pos + 4 > self.end:
            raise SflowError("datagramme tronqué")
        (value,) = struct.unpack_from("!I", self.data, self.pos)
        self.pos += 4
        return value

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > self.end:
            raise SflowError("datagramme tronqué")
        out = self.data[self.pos : self.pos + size]
        self.pos += size
        return out

    def sub(self, size: int) -> _Reader:
        """Les size octets suivants, dans un lecteur à part ; size est arrondi au multiple de 4 (XDR)."""
        padded = (size + 3) & ~3
        if size < 0 or self.pos + padded > self.end:
            raise SflowError("structure plus longue que le datagramme")
        child = _Reader(self.data, self.pos, self.pos + size)
        self.pos += padded
        return child

    def opaque(self, limit: int) -> bytes:
        """Données opaques XDR (longueur, octets, bourrage) ; au plus limit octets gardés."""
        size = self.u32()
        child = self.sub(size)
        return child.take(min(size, limit))


def decode(data: bytes) -> Datagram:
    """Datagramme sFlow v5 -> Datagram ; SflowError s'il est illisible."""
    r = _Reader(data)
    version = r.u32()
    if version != 5:
        raise SflowError(f"version sFlow {version} non prise en charge")
    addr_type = r.u32()
    if addr_type == 1:
        agent = str(ipaddress.IPv4Address(r.take(4)))
    elif addr_type == 2:
        agent = str(ipaddress.IPv6Address(r.take(16)))
    else:
        raise SflowError(f"type d'adresse d'agent inconnu : {addr_type}")
    dg = Datagram(agent=agent, sub_agent=r.u32(), sequence=r.u32(), uptime_ms=r.u32())
    count = r.u32()
    if count > MAX_SAMPLES:
        raise SflowError(f"{count} échantillons dans un datagramme (limite {MAX_SAMPLES})")
    for _ in range(count):
        fmt = r.u32()
        body = r.sub(r.u32())
        enterprise, number = fmt >> 12, fmt & 0xFFF
        if enterprise == 0 and number in (1, 3):
            dg.flows.append(_flow_sample(body, expanded=number == 3))
        elif enterprise == 0 and number in (2, 4):
            dg.counters += 1
        else:
            dg.others += 1
    return dg


def _flow_sample(r: _Reader, expanded: bool) -> FlowSample:
    r.u32()  # numéro de séquence de l'échantillon
    if expanded:
        r.u32()  # classe de la source de données
        source_index = r.u32()
    else:
        source_index = r.u32() & 0xFFFFFF
    rate, pool, drops = r.u32(), r.u32(), r.u32()
    if expanded:
        r.u32()  # format de l'interface d'entrée
        input_if = r.u32()
        r.u32()
        output_if = r.u32()
    else:
        raw_in, raw_out = r.u32(), r.u32()
        input_if = raw_in & 0x3FFFFFFF if raw_in >> 30 == 0 else 0
        output_if = raw_out & 0x3FFFFFFF if raw_out >> 30 == 0 else 0
    sample = {"sampling_rate": rate, "sample_pool": pool, "drops": drops, "source_index": source_index}
    count = r.u32()
    if count > MAX_RECORDS:
        raise SflowError(f"{count} enregistrements dans un échantillon (limite {MAX_RECORDS})")
    header: dict[str, object] = {}
    for _ in range(count):
        fmt = r.u32()
        body = r.sub(r.u32())
        if fmt == 1 and not header:  # en-tête brut du paquet (enterprise 0, format 1)
            header = {
                "header_protocol": body.u32(),
                "frame_length": body.u32(),
                "stripped": body.u32(),
                "header": body.opaque(MAX_HEADER),
            }
    return FlowSample(input_if=input_if, output_if=output_if, **sample, **header)  # type: ignore[arg-type]


def parse_packet(data: bytes, _depth: int = 0) -> Packet:
    """En-tête Ethernet -> Packet ; s'arrête sans erreur là où l'en-tête est tronqué."""
    if len(data) < 14:
        return Packet()
    (ethertype,) = struct.unpack_from("!H", data, 12)
    off, vlan = 14, None
    for _ in range(2):  # 802.1Q, QinQ
        if ethertype not in (ETH_VLAN, ETH_QINQ, ETH_QINQ_OLD) or len(data) < off + 4:
            break
        tci, ethertype = struct.unpack_from("!HH", data, off)
        if vlan is None:
            vlan = tci & 0x0FFF
        off += 4
    if ethertype == ETH_IPV4 and len(data) >= off + 20 and data[off] >> 4 == 4:
        ihl = (data[off] & 0x0F) * 4
        proto = data[off + 9]
        src = str(ipaddress.IPv4Address(data[off + 12 : off + 16]))
        dst = str(ipaddress.IPv4Address(data[off + 16 : off + 20]))
        fragment = struct.unpack_from("!H", data, off + 6)[0] & 0x1FFF
        l4 = off + ihl if ihl >= 20 and fragment == 0 else None  # fragments suivants : pas de ports
        return _transport(data, l4, Packet(ethertype, vlan, 4, src, dst, proto), _depth)
    if ethertype == ETH_IPV6 and len(data) >= off + 40 and data[off] >> 4 == 6:
        proto = data[off + 6]
        src = str(ipaddress.IPv6Address(data[off + 8 : off + 24]))
        dst = str(ipaddress.IPv6Address(data[off + 24 : off + 40]))
        l4: int | None = off + 40
        for _ in range(4):  # quelques en-têtes d'extension, pas plus
            if proto in _IPV6_EXTENSIONS and l4 is not None and len(data) >= l4 + 2:
                proto, l4 = data[l4], l4 + (data[l4 + 1] + 1) * 8
            elif proto == _IPV6_FRAGMENT and l4 is not None and len(data) >= l4 + 8:
                first = struct.unpack_from("!H", data, l4 + 2)[0] & 0xFFF8 == 0
                proto, l4 = data[l4], (l4 + 8 if first else None)
            else:
                break
        return _transport(data, l4, Packet(ethertype, vlan, 6, src, dst, proto), _depth)
    return Packet(ethertype=ethertype, vlan=vlan)


def _transport(data: bytes, l4: int | None, pkt: Packet, depth: int) -> Packet:
    """Ports TCP/UDP/SCTP ou type ICMP ; paquet interne pour VXLAN (un seul niveau)."""
    if l4 is None:
        return pkt
    if pkt.proto in (6, 17, 132) and len(data) >= l4 + 4:
        sport, dport = struct.unpack_from("!HH", data, l4)
        pkt = replace(pkt, sport=sport, dport=dport)
        # VXLAN : drapeau I, puis VNI sur 24 bits ; le paquet interne commence après les 8 octets
        if pkt.proto == 17 and dport == VXLAN_PORT and depth == 0 and len(data) >= l4 + 16 and data[l4 + 8] & 0x08:
            vni = int.from_bytes(data[l4 + 12 : l4 + 15], "big")
            return replace(pkt, vni=vni, inner=parse_packet(data[l4 + 16 :], depth + 1))
        return pkt
    if pkt.proto in (1, 58) and len(data) > l4:
        return replace(pkt, icmp_type=data[l4])
    return pkt


def protocol_name(pkt: Packet) -> str:
    """Protocole lisible : tcp, udp, icmp… ; pour un paquet non IP, son ethertype (arp, lldp…)."""
    if pkt.proto is not None:
        return IP_PROTOCOLS.get(pkt.proto, f"ip-{pkt.proto}")
    if pkt.ethertype is None:
        return "inconnu"
    return ETHERTYPES.get(pkt.ethertype, f"eth-0x{pkt.ethertype:04x}")


def classify(pkt: Packet) -> tuple[str, str]:
    """(protocole, application) d'un paquet : application d'après le port bien connu du serveur."""
    proto = protocol_name(pkt)
    if pkt.sport is None or pkt.dport is None:
        return proto, proto  # icmp, arp, fragments… : l'application est le protocole
    app = APPS.get((proto, pkt.dport)) or APPS.get((proto, pkt.sport))
    return proto, app or f"autre-{proto}"


def sonic_ifname(index: int) -> str | None:
    """Index SONiC d'interface (rapporté par hsflowd) -> nom (sonic_py_common.port_util)."""
    for base, top, prefix in (
        (1, 1000, "Ethernet"),
        (1000, 2000, "PortChannel"),
        (2000, 9000, "Vlan"),
        (9000, 10000, "Ethernet-BP"),
        (10000, 11000, "eth"),
    ):
        if base <= index < top:
            return f"{prefix}{index - base}"
    return None
