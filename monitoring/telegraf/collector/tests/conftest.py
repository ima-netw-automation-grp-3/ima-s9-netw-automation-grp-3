"""Fixtures communes : sorties réelles capturées sur le lab (tests/fixtures/)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))


def text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load(name: str) -> dict[str, Any]:
    return json.loads(text(name))


def lines_of(lines: list[str], measurement: str) -> list[str]:
    return [line for line in lines if line.split(",", 1)[0].split(" ", 1)[0] == measurement]


def _split(text: str, sep: str) -> list[str]:
    """Découpe sur sep hors échappements (\\) et hors chaînes entre guillemets."""
    parts, buf, quoted, i = [], [], False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            buf.append(text[i : i + 2])
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def parse_line(line: str) -> tuple[str, dict[str, str], dict[str, str], int | None]:
    """Ligne line protocol -> (mesure, tags, champs bruts, horodatage)."""
    head, fields, *rest = _split(line, " ")
    measurement, *tags = _split(head, ",")
    tag_map = dict(t.split("=", 1) for t in tags)
    field_map = dict(f.split("=", 1) for f in _split(fields, ","))
    return measurement, tag_map, field_map, int(rest[0]) if rest else None


# --- sFlow : datagrammes réels (sonic1, hsflowd 2.1.26) et générateurs pour les cas construits ------


def data(name: str) -> bytes:
    """Fixture binaire stockée en hexadécimal (une ligne)."""
    return bytes.fromhex(text(name).strip())


def u32(*values: int) -> bytes:
    return b"".join(v.to_bytes(4, "big") for v in values)


def ethernet(payload: bytes, ethertype: int = 0x0800, vlan: int | None = None) -> bytes:
    head = bytes.fromhex("aac6fb260c63") + bytes.fromhex("9ea0b3b16401")
    if vlan is not None:
        head += (0x8100).to_bytes(2, "big") + vlan.to_bytes(2, "big")
    return head + ethertype.to_bytes(2, "big") + payload


def ipv4(src: str, dst: str, proto: int, payload: bytes, fragment: int = 0) -> bytes:
    import ipaddress

    total = 20 + len(payload)
    head = bytes([0x45, 0]) + total.to_bytes(2, "big") + b"\x00\x01" + fragment.to_bytes(2, "big")
    head += bytes([64, proto]) + b"\x00\x00" + ipaddress.IPv4Address(src).packed + ipaddress.IPv4Address(dst).packed
    return head + payload


def ipv6(src: str, dst: str, proto: int, payload: bytes) -> bytes:
    import ipaddress

    head = bytes([0x60, 0, 0, 0]) + len(payload).to_bytes(2, "big") + bytes([proto, 64])
    return head + ipaddress.IPv6Address(src).packed + ipaddress.IPv6Address(dst).packed + payload


def l4(sport: int, dport: int, extra: bytes = b"\x00" * 16) -> bytes:
    return sport.to_bytes(2, "big") + dport.to_bytes(2, "big") + extra


def vxlan(vni: int, inner: bytes) -> bytes:
    return bytes([0x08, 0, 0, 0]) + vni.to_bytes(3, "big") + b"\x00" + inner


def flow_sample(header: bytes, rate: int = 256, index: int = 1, frame: int | None = None, expanded: bool = True, drops: int = 0) -> bytes:
    """Échantillon de flux sFlow (format 3 étendu, ou 1) avec un enregistrement d'en-tête brut."""
    padded = header + b"\x00" * (-len(header) % 4)
    record = u32(1, frame if frame is not None else len(header) + 4, 4, len(header)) + padded
    records = u32(1043, 8) + b"\x00" * 8 + u32(1, len(record)) + record  # horodatage hsflowd + en-tête
    if expanded:
        body = u32(7, 0, index, rate, 1000, drops, 0, index, 0, 0, 2) + records
        return u32(3, len(body)) + body
    body = u32(7, index, rate, 1000, drops, index, 0, 2) + records
    return u32(1, len(body)) + body


def datagram(*samples: bytes, agent: str = "10.255.0.1", sequence: int = 1, version: int = 5) -> bytes:
    import ipaddress

    head = u32(version, 1) + ipaddress.IPv4Address(agent).packed + u32(100000, sequence, 1000, len(samples))
    return head + b"".join(samples)
