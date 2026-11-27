"""Faux routeur pour les tests : CONFIG_DB en mémoire (quelques modules sonic-*) et FRR simulé.

Le paquet restconf est importé depuis lab/sonic ; frr_config.py et parsers.py viennent de
web/backend/app (dans le routeur, containerlab les monte à côté du paquet), ou de RESTCONF_SHARED
(stage de test Docker).
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # lab/sonic : paquet restconf
_shared = os.environ.get("RESTCONF_SHARED") or str(HERE.parents[3] / "web" / "backend" / "app")
sys.path.insert(1, _shared)  # frr_config, parsers

from restconf import labmodule  # noqa: E402
from restconf.datatree import CONTAINER, LEAF, LEAF_LIST, LIST, DictSchema, SchemaNode  # noqa: E402
from restconf.service import RestconfService  # noqa: E402
from restconf.store import InvalidData  # noqa: E402

PORT = "sonic-port:sonic-port"
INTF = "sonic-interface:sonic-interface"
VLAN = "sonic-vlan:sonic-vlan"


def _leaves(prefix: tuple[str, ...], *names: str, kind: str = LEAF) -> dict[tuple[str, ...], SchemaNode]:
    return {prefix + (n,): SchemaNode(kind) for n in names}


SCHEMA = DictSchema(
    {
        (PORT,): SchemaNode(CONTAINER),
        (PORT, "PORT"): SchemaNode(CONTAINER),
        (PORT, "PORT", "PORT_LIST"): SchemaNode(LIST, keys=("name",)),
        **_leaves((PORT, "PORT", "PORT_LIST"), "name", "admin_status", "mtu", "description", "lanes", "speed"),
        (INTF,): SchemaNode(CONTAINER),
        (INTF, "INTERFACE"): SchemaNode(CONTAINER),
        (INTF, "INTERFACE", "INTERFACE_LIST"): SchemaNode(LIST, keys=("name",)),
        **_leaves((INTF, "INTERFACE", "INTERFACE_LIST"), "name", "vrf_name"),
        (INTF, "INTERFACE", "INTERFACE_IPPREFIX_LIST"): SchemaNode(LIST, keys=("name", "ip-prefix")),
        **_leaves((INTF, "INTERFACE", "INTERFACE_IPPREFIX_LIST"), "name", "ip-prefix", "family"),
        (VLAN,): SchemaNode(CONTAINER),
        (VLAN, "VLAN"): SchemaNode(CONTAINER),
        (VLAN, "VLAN", "VLAN_LIST"): SchemaNode(LIST, keys=("name",)),
        **_leaves((VLAN, "VLAN", "VLAN_LIST"), "name", "vlanid", "description"),
        **_leaves((VLAN, "VLAN", "VLAN_LIST"), "dhcp_servers", kind=LEAF_LIST),
    }
)

STARTUP: dict[str, Any] = {
    "PORT": {
        "Ethernet0": {"admin_status": "up", "mtu": "9100", "description": "client1", "lanes": "25,26,27,28", "speed": "100000"},
        "Ethernet4": {"admin_status": "up", "mtu": "9100", "lanes": "29,30,31,32", "speed": "100000"},
        "Ethernet8": {"admin_status": "down", "mtu": "9100", "lanes": "33,34,35,36", "speed": "100000"},
    },
    "INTERFACE": {"Ethernet0": {}, "Ethernet0|192.168.1.1/24": {}},
}


class FakeSonic:
    """CONFIG_DB en mémoire, convertie vers/depuis un JSON RFC 7951 simplifié (3 modules)."""

    schema = SCHEMA

    def __init__(self, configdb: dict[str, Any] | None = None) -> None:
        self.configdb = copy.deepcopy(STARTUP if configdb is None else configdb)
        self.applied: list[list[dict[str, Any]]] = []
        self.dry_runs: list[list[dict[str, Any]]] = []
        self.refuse_apply: str | None = None  # message GCU simulé

    def modules(self) -> list[dict[str, Any]]:
        return [
            {"name": "sonic-port", "revision": "2019-07-01", "namespace": "http://github.com/sonic-net/sonic-port", "implemented": True, "downloadable": True},
            {"name": "sonic-types", "revision": "", "namespace": "http://github.com/sonic-net/sonic-types", "implemented": False, "downloadable": True},
        ]

    def schema_file(self, name: str, revision: str = "") -> Path | None:
        return HERE / "fixtures" / "sonic-port.yang" if name == "sonic-port" and revision in ("", "2019-07-01") else None

    # -- conversions ---------------------------------------------------------------------------
    def read(self) -> dict[str, Any]:
        return self.to_tree(self.configdb)

    @staticmethod
    def to_tree(cfg: dict[str, Any]) -> dict[str, Any]:
        tree: dict[str, Any] = {}
        ports = [
            {"name": k, **{f: (int(v) if f in ("mtu", "speed") else v) for f, v in sorted(fields.items())}}
            for k, fields in sorted(cfg.get("PORT", {}).items())
        ]
        if ports:
            tree[PORT] = {"PORT": {"PORT_LIST": ports}}
        intf: dict[str, Any] = {}
        for key, fields in sorted(cfg.get("INTERFACE", {}).items()):
            if "|" in key:
                name, prefix = key.split("|", 1)
                intf.setdefault("INTERFACE_IPPREFIX_LIST", []).append({"name": name, "ip-prefix": prefix, **fields})
            else:
                intf.setdefault("INTERFACE_LIST", []).append({"name": key, **fields})
        if intf:
            tree[INTF] = {"INTERFACE": intf}
        vlans = [
            {"name": k, **{f: (int(v) if f == "vlanid" else v) for f, v in sorted(fields.items())}}
            for k, fields in sorted(cfg.get("VLAN", {}).items())
        ]
        if vlans:
            tree[VLAN] = {"VLAN": {"VLAN_LIST": vlans}}
        return tree

    def to_configdb(self, tree: dict[str, Any]) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        for entry in tree.get(PORT, {}).get("PORT", {}).get("PORT_LIST", []):
            cfg.setdefault("PORT", {})[entry["name"]] = {k: str(v) for k, v in entry.items() if k != "name"}
        intf = tree.get(INTF, {}).get("INTERFACE", {})
        for entry in intf.get("INTERFACE_LIST", []):
            cfg.setdefault("INTERFACE", {})[entry["name"]] = {k: v for k, v in entry.items() if k != "name"}
        for entry in intf.get("INTERFACE_IPPREFIX_LIST", []):
            key = f"{entry['name']}|{entry['ip-prefix']}"
            cfg.setdefault("INTERFACE", {})[key] = {k: v for k, v in entry.items() if k not in ("name", "ip-prefix")}
        for entry in tree.get(VLAN, {}).get("VLAN", {}).get("VLAN_LIST", []):
            cfg.setdefault("VLAN", {})[entry["name"]] = {
                k: (v if isinstance(v, list) else str(v)) for k, v in entry.items() if k != "name"
            }
        return cfg

    # -- validation et application ------------------------------------------------------------
    def validate(self, tree: dict[str, Any]) -> None:
        """Quelques règles des vrais modèles : type de mtu, leafref INTERFACE → PORT, nœuds connus."""
        ports = {e["name"] for e in tree.get(PORT, {}).get("PORT", {}).get("PORT_LIST", [])}
        for i, entry in enumerate(tree.get(PORT, {}).get("PORT", {}).get("PORT_LIST", [])):
            mtu = entry.get("mtu")
            if mtu is not None and (not str(mtu).isdigit() or not 68 <= int(mtu) <= 9216):
                raise InvalidData(f'Invalid uint16 value "{mtu}".', f"/{PORT}/PORT/PORT_LIST[name='{entry['name']}']/mtu")
            unknown = set(entry) - {"name", "admin_status", "mtu", "description", "lanes", "speed"}
            if unknown:
                raise InvalidData(f'Node "{sorted(unknown)[0]}" not found as a child of "PORT_LIST" node.', f"/{PORT}/PORT/PORT_LIST[{i}]")
        intf = tree.get(INTF, {}).get("INTERFACE", {})
        for entry in intf.get("INTERFACE_LIST", []) + intf.get("INTERFACE_IPPREFIX_LIST", []):
            if entry["name"] not in ports:
                raise InvalidData(
                    f'Invalid leafref value "{entry["name"]}" - no target instance "/port:sonic-port/port:PORT/port:PORT_LIST/port:name" with the same value.',
                    f"/{INTF}/INTERFACE/INTERFACE_LIST[name='{entry['name']}']/name",
                )

    def apply(self, ops: list[dict[str, Any]], *, dry_run: bool = False) -> None:
        if self.refuse_apply:
            raise InvalidData(self.refuse_apply)
        if dry_run:
            self.dry_runs.append(ops)
            return
        self.applied.append(ops)
        for op in ops:
            parts = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].split("/")[1:]]
            node = self.configdb
            for p in parts[:-1]:
                node = node[p]
            if op["op"] == "remove":
                del node[parts[-1]]
            else:
                node[parts[-1]] = copy.deepcopy(op["value"])


FRR_RUNNING = """router bgp 65001
 bgp router-id 10.255.0.1
 neighbor 10.0.12.2 remote-as 65002
 !
 address-family ipv4 unicast
  network 192.168.1.0/24
 exit-address-family
exit
!
"""


class FakeFrr:
    def __init__(self) -> None:
        self.config = FRR_RUNNING
        self.calls: list[tuple[str, str]] = []
        self.pings: list[tuple[str, str | None, int]] = []

    def running(self) -> str:
        return self.config

    def neighbors(self) -> list[dict[str, Any]]:
        return [{"address": "10.0.12.2", "remote-as": 65002, "state": "Established", "prefixes-received": 2}]

    @staticmethod
    def _errors(text: str) -> str:
        return "% Unknown command: bogus" if "bogus" in text else ""

    def preview(self, text: str, mode: str) -> dict[str, Any]:
        errors = self._errors(text)
        out: dict[str, Any] = {"valid": not errors, "diff": f"--- running\n+++ après ({mode})\n"}
        if errors:
            out["errors"] = errors
        return out

    def apply(self, text: str, mode: str) -> dict[str, Any]:
        errors = self._errors(text)
        if errors:
            raise InvalidData(f"configuration FRR invalide : {errors}")
        self.calls.append((mode, text))
        self.config = text if mode == "replace" else self.config + text
        return {"steps": [{"step": "application", "exit_code": 0, "output": ""}], "resets": []}

    def ping(self, destination: str, source: str | None, count: int) -> dict[str, Any]:
        self.pings.append((destination, source, count))
        return labmodule.ping_output(
            {"transmitted": count, "received": count, "loss": 0.0, "rtt_min": 0.1, "rtt_avg": 0.2, "rtt_max": 0.3, "ok": True},
            f"{count} packets transmitted, {count} received",
        )


@pytest.fixture
def sonic() -> FakeSonic:
    return FakeSonic()


@pytest.fixture
def frr() -> FakeFrr:
    return FakeFrr()


@pytest.fixture
def service(sonic: FakeSonic, frr: FakeFrr) -> RestconfService:
    return RestconfService(sonic, frr, scheme="https")
