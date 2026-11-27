"""Faux client Docker : simule un lab (1 routeur SONiC + 1 client) sans conteneur réel."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import configdb_diff
from app.docker_api import ExecResult
from app.errors import OperationFailed
from app.main import create_app
from app.remote import source
from app.settings import Settings

TOPOLOGY = """
name: testlab
mgmt:
  network: testlab-mgmt
  ipv4-subnet: 172.20.30.0/24
topology:
  kinds:
    sonic-vs:
      image: docker-sonic-vs:latest
    linux:
      image: alpine:3.22
  nodes:
    sonic1: {kind: sonic-vs, mgmt-ipv4: 172.20.30.11}
    client1: {kind: linux, mgmt-ipv4: 172.20.30.21}
  links:
    - endpoints: ["client1:eth1", "sonic1:eth1"]
"""

FRR_RUNNING = """Building configuration...

Current configuration:
!
frr version 10.7.1
frr defaults traditional
hostname sonic1
fpm address 127.0.0.1
!
router bgp 65001
 bgp router-id 10.255.0.1
 neighbor 10.0.12.2 remote-as 65002
exit
!
end
"""

LINUX_STATUS = """2: eth0@if26: <BROADCAST,MULTICAST,UP,LOWER_UP,M-DOWN> mtu 1500 qdisc noqueue state UP \\    link/ether 3e:68:dc:d2:96:8e brd ff:ff:ff:ff:ff:ff
28: eth1@if29: <BROADCAST,MULTICAST,UP,LOWER_UP,M-DOWN> mtu 9500 qdisc noqueue state UP \\    link/ether aa:c1:ab:55:88:cb brd ff:ff:ff:ff:ff:ff
=====
2: eth0    inet 172.20.30.21/24 brd 172.20.30.255 scope global eth0\\       valid_lft forever preferred_lft forever
28: eth1    inet 192.168.1.10/24 scope global eth1\\       valid_lft forever preferred_lft forever
=====
default via 172.20.30.1 dev eth0
192.168.1.0/24 dev eth1 scope link  src 192.168.1.10
=====
Inter-|   Receive
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
  eth1:    1168      17    0   32    0     0          0         0     2692      27    0    0    0     0       0          0
"""


def _container(node: str, kind: str, ip: str, state: str = "running") -> dict[str, Any]:
    return {
        "Id": f"id-{node}",
        "Names": [f"/clab-testlab-{node}"],
        "State": state,
        "Status": "Up 10 minutes",
        "Image": "img",
        "Labels": {"containerlab": "testlab", "clab-node-name": node, "clab-node-kind": kind},
        "NetworkSettings": {"Networks": {"testlab-mgmt": {"IPAddress": ip}}},
    }


class FakeTty:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.queue.put_nowait(b"bienvenue\r\n")
        self.sizes: list[tuple[int, int]] = []

    async def read(self) -> bytes | None:
        return await self.queue.get()

    async def write(self, data: bytes) -> None:
        if data.strip() == b"exit":
            await self.queue.put(None)
        else:
            await self.queue.put(b"echo:" + data)

    async def resize(self, cols: int, rows: int) -> None:
        self.sizes.append((cols, rows))

    async def close(self) -> None:
        await self.queue.put(None)

    async def exit_code(self) -> int | None:
        return 0


class FakeDocker:
    def __init__(self) -> None:
        self.containers = [
            _container("sonic1", "sonic-vs", "172.20.30.11"),
            _container("client1", "linux", "172.20.30.21"),
            {
                "Id": "runner",
                "Names": ["/testlab-clab-1"],
                "State": "running",
                "Status": "Up 10 minutes (healthy)",
                "Labels": {"com.docker.compose.project": "testlab", "com.docker.compose.service": "clab"},
            },
        ]
        self.configdb: dict[str, Any] = {
            "DEVICE_METADATA": {"localhost": {"hostname": "sonic1", "mac": "aa:bb:cc:dd:ee:ff"}},
            "PORT": {"Ethernet0": {"admin_status": "up", "lanes": "25,26,27,28", "mtu": "9100"}},
            "INTERFACE": {"Ethernet0": {}, "Ethernet0|192.168.1.1/24": {}},
            "LOGGER": {"orchagent": {"LOGLEVEL": "NOTICE"}},
        }
        self.frr = FRR_RUNNING
        self.files: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.restarted: str | None = None
        # cycle de vie des conteneurs nommés (runner) : (action, nom, délai d'arrêt)
        self.lifecycle: list[tuple[str, str, int | None]] = []
        self.stop_gate: threading.Event | None = None  # arrêt bloqué jusqu'à set()
        self.stop_error: str | None = None  # arrêt en échec avec ce message
        self.tty: FakeTty | None = None
        self.created: dict[str, dict[str, Any]] = {}
        self.pulled: list[str] = []

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_containers(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        every = [*self.containers, *self.created.values()]
        return [c for c in every if all(c["Labels"].get(k) == v for k, v in labels.items())]

    async def inspect(self, name: str) -> dict[str, Any] | None:
        started = datetime.fromtimestamp(time.time() - 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        extra = self.created.get(name, {})
        state = {"StartedAt": started, "FinishedAt": "2026-01-01T10:00:05.123456789Z", "Status": extra.get("State", "running")}
        return {"State": state, "Config": {"Env": (extra.get("config") or {}).get("Env", [])}}

    # -- conteneurs auxiliaires (captures) -----------------------------------------------------------
    async def ensure_image(self, image: str) -> None:
        self.pulled.append(image)

    async def create_container(self, name: str, config: dict[str, Any]) -> None:
        self.created[name] = {"Id": name, "Names": [f"/{name}"], "State": "created", "Labels": config["Labels"], "config": config}

    def _named(self, name: str) -> dict[str, Any] | None:
        return next((c for c in self.containers if (c.get("Names") or [""])[0].lstrip("/") == name), None)

    async def start_container(self, name: str) -> None:
        if name in self.created:
            cmd = self.created[name]["config"].get("Cmd") or []
            self.created[name]["State"] = "exited" if "badfilter" in cmd else "running"
            return
        self.lifecycle.append(("start", name, None))
        if (c := self._named(name)) is not None:
            c["State"] = "running"

    async def stop_container(self, name: str, timeout: int = 5) -> None:
        if name in self.created:
            self.created[name]["State"] = "exited"
            return
        self.lifecycle.append(("stop", name, timeout))
        if self.stop_gate is not None:
            await asyncio.to_thread(self.stop_gate.wait, 10)
        if self.stop_error:
            raise OperationFailed(self.stop_error)
        if (c := self._named(name)) is not None:
            c["State"] = "exited"

    async def remove_container(self, name: str) -> None:
        self.created.pop(name, None)

    async def put_files(self, name: str, files: dict[str, bytes], dest: str = "/tmp") -> None:
        for rel, data in files.items():
            self.files[f"{dest}/{rel}"] = data

    async def logs(self, name: str, tail: int = 200) -> str:
        if name in self.created:
            return "dumpcap: syntax error in filter expression\n"
        return "\x1b[1mdeploy\x1b[0m ok\n"

    async def restart(self, name: str, stop_timeout: int = 180) -> None:
        self.restarted = name
        self.lifecycle.append(("restart", name, stop_timeout))
        if (c := self._named(name)) is not None:
            c["State"] = "running"

    async def open_tty(self, name, argv, *, env=None, cols=120, rows=32) -> FakeTty:
        self.tty = FakeTty()
        return self.tty

    def _collector(self) -> dict[str, Any]:
        return {
            "ready": True,
            "errors": {},
            "key_services": ["orchagent", "bgpd"],
            "services": [{"name": "orchagent", "state": "RUNNING"}, {"name": "bgpd", "state": "RUNNING"}],
            "ports": [{"name": "Ethernet0", "admin_status": "up", "oper_status": "up", "wire": "eth1", "wire_up": True}],
            "interfaces": {
                "Ethernet0": {"addresses": ["192.168.1.1/24"], "stats": {}},
                "eth0": {"addresses": ["172.20.30.11/24"]},
            },
            "bgp": {"as": 65001, "router_id": "10.255.0.1", "peers": []},
        }

    async def exec(self, name: str, argv: list[str], *, timeout: float = 30, env=None) -> ExecResult:
        self.calls.append(argv)

        def ok(out: str = "") -> ExecResult:
            return ExecResult(0, out, "")

        if argv[:2] == ["python3", "-c"] and argv[2] == source("collect_sonic"):
            return ok(json.dumps(self._collector()))
        if argv[:2] == ["python3", "-c"] and argv[2] == source("apply_configdb"):
            ops = json.loads(self.files[argv[3]])
            self.configdb = configdb_diff.apply_ops(
                self.configdb, [configdb_diff.Op(o["op"], o["table"], o["key"], o["data"]) for o in ops]
            )
            results = [{"op": o["op"], "table": o["table"], "key": o["key"], "ok": True} for o in ops]
            return ok(json.dumps({"results": results, "ok": True}))
        if argv[0] == "sonic-cfggen":
            return ok(json.dumps(self.configdb))
        if argv == ["vtysh", "-c", "show running-config"]:
            return ok(self.frr)
        if argv[:2] == ["vtysh", "-C"]:
            content = self.files[argv[3]].decode()
            if "foo" in content:
                return ExecResult(2, "line 2: % Unknown command: foo\nConfiguration file[/etc/frr/frr.conf] processing failure: 2\n", "")
            return ok()
        if argv[:2] == ["vtysh", "-f"]:
            content = self.files[argv[2]].decode()
            self.frr = self.frr.replace("end\n", content + "end\n")
            return ok("[1|bgpd] done\n")
        if argv == ["ls", "/sys/class/net"]:
            return ok("eth0\neth1\nlo\n")
        if argv[0] in ("rm", "config", "tail"):
            return ok("syslog\n" if argv[0] == "tail" else "")
        if argv[:2] == ["sh", "-c"] and argv[2].startswith("ip -o link"):
            return ok(LINUX_STATUS)
        if argv[0] == "ping":
            return ok("2 packets transmitted, 2 packets received, 0% packet loss\nround-trip min/avg/max = 0.1/0.2/0.3 ms\n")
        if argv[:2] in (["sh", "-c"], ["bash", "-c"]):
            return ok(f"exécuté: {argv[2]}\n")
        return ExecResult(127, "", f"commande non simulée: {argv}")


@pytest.fixture
def lab_dir(tmp_path: Path) -> Path:
    (tmp_path / "sonic-lab.clab.yml").write_text(TOPOLOGY)
    (tmp_path / ".runner-status").write_text("deployed\n")
    (tmp_path / "configs" / "sonic1").mkdir(parents=True)
    startup = configdb_diff.strip_volatile(FakeDocker().configdb)
    (tmp_path / "configs" / "sonic1" / "config_db.json").write_text(json.dumps(startup, indent=4))
    (tmp_path / "configs" / "sonic1" / "frr.conf").write_text(
        "router bgp 65001\n bgp router-id 10.255.0.1\n neighbor 10.0.12.2 remote-as 65002\nexit\n!\n"
    )
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>SPA</html>")
    return tmp_path


def make_settings(lab_dir: Path, **kw: Any) -> Settings:
    kw.setdefault("allowed_hosts", ("testserver",))  # hôte utilisé par TestClient
    return Settings(
        lab_name="testlab",
        lab_dir=lab_dir,
        compose_project="testlab",
        status_ttl=0.0,
        static_dir=lab_dir / "static",
        capture_dir=lab_dir / "captures",
        capture_network="testlab_default",
        **kw,
    )


@pytest.fixture
def fake_docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def client(lab_dir: Path, fake_docker: FakeDocker):
    app = create_app(make_settings(lab_dir), docker=fake_docker)  # type: ignore[arg-type]
    with TestClient(app) as c:
        yield c
