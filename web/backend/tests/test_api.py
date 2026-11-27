import base64
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import check_exposure, create_app
from app.settings import DEFAULT_ALLOWED_HOSTS, Settings

from .conftest import _container, make_settings


def test_lab_state(client):
    data = client.get("/api/lab").json()
    assert data["name"] == "testlab"
    assert data["runner"]["state"] == "deployed"
    phases = {n["name"]: n["phase"] for n in data["nodes"]}
    assert phases == {"sonic1": "ready", "client1": "ready"}
    sonic1 = next(n for n in data["nodes"] if n["name"] == "sonic1")
    assert sonic1["addresses"] == {"Ethernet0": ["192.168.1.1/24"]}  # mgmt exclu
    link = data["links"][0]
    assert link["state"] == "up"
    assert link["z"]["port"] == "Ethernet0" and link["a"]["addresses"] == ["192.168.1.10/24"]
    assert link["a"]["wire_up"] is True and link["z"]["wire_up"] is True


def test_set_wire_only_on_link_ends(client, fake_docker):
    res = client.post("/api/nodes/client1/wires/eth1", json={"up": False}).json()
    assert res["ok"] and res["peer"] == {"node": "sonic1", "interface": "eth1"}
    argv = fake_docker.calls[-1]
    assert argv[:2] == ["sh", "-c"] and "ip link set dev" in argv[2] and argv[3:] == ["wire", "eth1", "down"]
    client.post("/api/nodes/sonic1/wires/eth1", json={"up": True})
    assert fake_docker.calls[-1][3:] == ["wire", "eth1", "up"]
    for node, iface in (("client1", "eth0"), ("client1", "eth2"), ("sonic1", "eth1;reboot"), ("nope", "eth1")):
        res = client.post(f"/api/nodes/{node}/wires/{iface}", json={"up": False})
        assert res.status_code == 400 and "extrémité de lien" in res.json()["error"]


def test_node_status_and_unknown_node(client):
    assert client.get("/api/nodes/client1").json()["interfaces"]["eth1"]["stats"]["rx_packets"] == 17
    res = client.get("/api/nodes/nope")
    assert res.status_code == 404 and "nœud inconnu" in res.json()["error"]


def test_get_config_hides_volatile_data(client):
    data = client.get("/api/nodes/sonic1/config", params={"domain": "configdb"}).json()
    cfg = json.loads(data["content"])
    assert "LOGGER" not in cfg and "mac" not in cfg["DEVICE_METADATA"]["localhost"]
    frr = client.get("/api/nodes/sonic1/config", params={"domain": "frr"}).json()
    assert frr["content"].startswith("router bgp 65001") and "fpm" not in frr["content"]
    assert "fpm address" in frr["raw"]
    startup = client.get("/api/nodes/sonic1/config", params={"domain": "frr", "source": "startup"}).json()
    assert startup["content"].startswith("router bgp 65001")


def test_preview_and_push_configdb(client, fake_docker):
    body = {"domain": "configdb", "mode": "merge", "content": json.dumps({"PORT": {"Ethernet0": {"admin_status": "down"}}})}
    preview = client.post("/api/nodes/sonic1/config/preview", json=body).json()
    assert preview["summary"] == {"add": 0, "modify": 1, "delete": 0}
    assert fake_docker.configdb["PORT"]["Ethernet0"]["admin_status"] == "up"  # aperçu = aucun effet
    res = client.post("/api/nodes/sonic1/config/push", json=body).json()
    assert res["ok"] and res["applied"] == 1
    assert fake_docker.configdb["PORT"]["Ethernet0"]["admin_status"] == "down"
    assert not any(k.startswith("/tmp/labweb-") for k in fake_docker.files) or any(
        c[:2] == ["rm", "-f"] for c in fake_docker.calls
    )
    diff = client.get("/api/nodes/sonic1/config/diff").json()
    assert diff["dirty"] and diff["configdb"]["changes"][0]["key"] == "Ethernet0"
    restore = client.post("/api/nodes/sonic1/config/restore", json={"source": "startup", "domains": ["configdb"]}).json()
    assert restore["ok"] and fake_docker.configdb["PORT"]["Ethernet0"]["admin_status"] == "up"


def test_diff_ignores_frr_order(client, lab_dir):
    # conf de démarrage écrite dans un autre ordre que la running affichée par FRR : pas « modifiée »
    (lab_dir / "configs" / "sonic1" / "frr.conf").write_text(
        "ip route 10.99.0.0/24 Null0\n!\nrouter bgp 65001\n bgp router-id 10.255.0.1\n neighbor 10.0.12.2 remote-as 65002\nexit\n!\n"
    )
    assert client.get("/api/nodes/sonic1/config/diff").json()["dirty"]  # la route manque dans la running
    client.post("/api/nodes/sonic1/config/push", json={"domain": "frr", "mode": "merge", "content": "ip route 10.99.0.0/24 Null0\n"})
    diff = client.get("/api/nodes/sonic1/config/diff").json()
    assert not diff["dirty"] and diff["frr"]["diff"] == ""


def test_push_invalid_json_and_frr(client):
    res = client.post("/api/nodes/sonic1/config/push", json={"domain": "configdb", "content": "{oops"})
    assert res.status_code == 400 and "JSON invalide" in res.json()["error"]
    res = client.post("/api/nodes/sonic1/config/push", json={"domain": "frr", "content": "router bgp 65001\n foo\n"})
    assert res.status_code == 400
    assert res.json()["detail"] == "line 2: % Unknown command: foo"


def test_push_frr_replace_reports_resets(client):
    body = {"domain": "frr", "mode": "replace", "content": "router bgp 65001\n bgp router-id 10.255.0.1\nexit\n"}
    preview = client.post("/api/nodes/sonic1/config/preview", json=body).json()
    assert preview["valid"] and preview["replace"]["resets"] == ["router bgp 65001"]
    res = client.post("/api/nodes/sonic1/config/push", json=body).json()
    assert res["ok"] and [s["step"] for s in res["steps"]] == ["retrait", "application"]


def test_save_writes_startup_files(client, lab_dir):
    res = client.post("/api/nodes/sonic1/config/save").json()
    assert res["ok"] and res["paths"] == ["configs/sonic1/config_db.json", "configs/sonic1/frr.conf"]
    saved = json.loads((lab_dir / "configs/sonic1/config_db.json").read_text())
    assert "LOGGER" not in saved and saved["PORT"]["Ethernet0"]["admin_status"] == "up"
    assert (lab_dir / "configs/sonic1/frr.conf").read_text().startswith("router bgp 65001")


def test_snapshots_lifecycle(client):
    meta = client.post("/api/nodes/sonic1/snapshots", json={"label": "avant test"}).json()
    assert meta["id"].endswith("-avant-test")
    listing = client.get("/api/nodes/sonic1/snapshots").json()["snapshots"]
    assert [s["id"] for s in listing] == [meta["id"]]
    full = client.get(f"/api/nodes/sonic1/snapshots/{meta['id']}").json()
    assert set(full["contents"]) == {"configdb", "frr"}
    assert client.delete(f"/api/nodes/sonic1/snapshots/{meta['id']}").json() == {"ok": True}
    assert client.get("/api/nodes/sonic1/snapshots").json()["snapshots"] == []
    assert client.get("/api/nodes/client1/snapshots").status_code == 400  # pas un routeur


def test_exec_ping_logs_and_runner(client, fake_docker):
    res = client.post("/api/nodes/client1/exec", json={"command": "ip route"}).json()
    assert res["exit_code"] == 0 and "ip route" in res["output"]
    ping = client.post("/api/nodes/client1/ping", json={"target": "192.168.2.10", "count": 2}).json()
    assert ping["summary"]["ok"] and ping["summary"]["received"] == 2
    assert client.post("/api/nodes/client1/ping", json={"target": "-c 1 x"}).status_code == 400
    assert client.get("/api/nodes/sonic1/logs").json()["logs"] == "syslog\n"
    assert client.get("/api/lab/runner/logs").json()["logs"] == "deploy ok\n"  # codes ANSI retirés
    assert client.post("/api/lab/redeploy").json()["ok"]


def test_terminal_websocket(client, fake_docker):
    with client.websocket_connect("/ws/nodes/sonic1/terminal?shell=vtysh&cols=100&rows=30") as ws:
        assert ws.receive_bytes() == b"bienvenue\r\n"
        ws.send_text(json.dumps({"type": "input", "data": "show version\r"}))
        assert ws.receive_bytes() == b"echo:show version\r"
        ws.send_text(json.dumps({"type": "resize", "cols": 140, "rows": 40}))
        ws.send_text(json.dumps({"type": "input", "data": "exit"}))
        assert json.loads(ws.receive_text()) == {"type": "exit", "code": 0}
    assert fake_docker.tty is not None and fake_docker.tty.sizes == [(140, 40)]


def test_terminal_rejects_unknown_shell(client):
    with client.websocket_connect("/ws/nodes/client1/terminal?shell=vtysh") as ws:
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "error" and "shell inconnu" in msg["message"]


def test_spa_fallback(client):
    assert client.get("/nodes/sonic1").text == "<html>SPA</html>"
    res = client.get("/api/does-not-exist")
    assert res.status_code == 404 and res.headers["content-type"].startswith("application/json")


def test_spa_page_is_revalidated(client, lab_dir):
    # la page pointe vers le bundle du moment : jamais servie depuis le cache sans revalidation
    for path in ("/", "/nodes/sonic1"):
        assert client.get(path).headers["cache-control"] == "no-cache"
    (lab_dir / "static" / "app-1234.js").write_text("console.log(1)")
    assert "cache-control" not in client.get("/app-1234.js").headers


def test_basic_auth(lab_dir, fake_docker):
    app = create_app(make_settings(lab_dir, auth_user="admin", auth_password="s3cret"), docker=fake_docker)
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/lab").status_code == 401
        token = base64.b64encode(b"admin:s3cret").decode()
        assert c.get("/api/lab", headers={"Authorization": f"Basic {token}"}).status_code == 200
        bad = base64.b64encode(b"admin:nope").decode()
        assert c.get("/api/lab", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_basic_auth_non_ascii_password(lab_dir, fake_docker):
    # compare_digest refuse les chaînes non ASCII : la comparaison se fait en octets, sans erreur 500
    app = create_app(make_settings(lab_dir, auth_user="admin", auth_password="mot-de-passe-é"), docker=fake_docker)
    with TestClient(app) as c:
        good = base64.b64encode("admin:mot-de-passe-é".encode()).decode()
        assert c.get("/api/lab", headers={"Authorization": f"Basic {good}"}).status_code == 200
        bad = base64.b64encode("admin:é".encode()).decode()
        assert c.get("/api/lab", headers={"Authorization": f"Basic {bad}"}).status_code == 401


@pytest.mark.parametrize(
    ("env", "exposed"),
    [
        ({}, False),
        ({"WEB_BIND": "127.0.0.1", "WEB_ALLOWED_HOSTS": "localhost,127.0.0.1"}, False),
        ({"WEB_BIND": "0.0.0.0"}, True),
        ({"WEB_ALLOWED_HOSTS": "lab.example.com"}, True),  # derrière un reverse proxy
        ({"WEB_ALLOWED_HOSTS": "*"}, True),
    ],
)
def test_exposed_interface_requires_auth(monkeypatch, env, exposed):
    for var in ("WEB_BIND", "WEB_ALLOWED_HOSTS", "WEB_AUTH_USER", "WEB_AUTH_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    settings = Settings.from_env()
    assert settings.exposed is exposed
    if not exposed:
        check_exposure(settings)
        return
    with pytest.raises(RuntimeError, match="WEB_AUTH_USER"):
        check_exposure(settings)
    with pytest.raises(RuntimeError):
        create_app()  # le lancement réel (uvicorn --factory) s'arrête avant de servir quoi que ce soit
    monkeypatch.setenv("WEB_AUTH_USER", "admin")
    monkeypatch.setenv("WEB_AUTH_PASSWORD", "s3cret")
    check_exposure(Settings.from_env())  # authentifiée : acceptée


def test_nodes_shown_booting_while_deploying(client, lab_dir, fake_docker):
    (lab_dir / ".runner-status").write_text("deploying\n")
    fake_docker.containers[0]["State"] = "exited"  # sonic1 en cours de recréation
    data = client.get("/api/lab").json()
    sonic1 = next(n for n in data["nodes"] if n["name"] == "sonic1")
    assert sonic1["phase"] == "booting" and "déploiement" in sonic1["detail"]


RUNNER = "testlab-clab-1"


def wait_idle(client) -> dict:
    """Attend la fin de l'action de lab lancée en tâche de fond ; renvoie l'état du runner."""
    for _ in range(200):
        runner = client.get("/api/lab").json()["runner"]
        if runner["action"] is None:
            return runner
        time.sleep(0.02)
    raise AssertionError("action de lab jamais terminée")


def test_stop_then_start_lab(client, lab_dir, fake_docker):
    res = client.post("/api/lab/stop").json()
    assert res["ok"] and "arrêt" in res["message"]
    runner = wait_idle(client)
    # arrêt du service clab avec le délai du stop_grace_period : le runner détruit le lab
    assert fake_docker.lifecycle == [("stop", RUNNER, 180)]
    assert runner["state"] == "stopped" and runner["container_state"] == "exited" and runner["error"] is None
    assert "déjà arrêté" in client.post("/api/lab/stop").json()["message"]

    (lab_dir / ".runner-status").write_text("stopped\n")  # écrit par le trap du runner
    res = client.post("/api/lab/start").json()
    assert res["ok"] and "lab/sonic-lab.clab.yml" in res["message"]
    assert fake_docker.lifecycle[-1] == ("start", RUNNER, None)
    # conteneur démarré, état pas encore réécrit par le runner : démarrage en cours
    assert client.get("/api/lab").json()["runner"]["state"] == "starting"
    assert "déjà démarré" in client.post("/api/lab/start").json()["message"]


def test_lab_actions_exclusive(client, fake_docker):
    fake_docker.stop_gate = threading.Event()
    try:
        assert client.post("/api/lab/stop").json()["ok"]
        assert client.get("/api/lab").json()["runner"]["action"] == "stop"
        for path in ("/api/lab/start", "/api/lab/redeploy"):
            res = client.post(path)
            assert res.status_code == 409 and "arrêt du lab en cours" in res.json()["error"]
        assert "déjà en cours" in client.post("/api/lab/stop").json()["message"]
    finally:
        fake_docker.stop_gate.set()
    assert wait_idle(client)["state"] == "stopped"
    assert [a for a, _, _ in fake_docker.lifecycle] == ["stop"]


def test_lab_action_error_reported(client, fake_docker):
    fake_docker.stop_error = "délai dépassé"
    assert client.post("/api/lab/stop").json()["ok"]
    assert wait_idle(client)["error"] == "arrêt du lab : délai dépassé"


def test_start_refused_while_runner_destroys(client, lab_dir):
    # arrêt lancé ailleurs (make stop, ou avant un redémarrage de l'interface) : le runner détruit le lab
    (lab_dir / ".runner-status").write_text("destroying\n")
    assert client.post("/api/lab/start").status_code == 409
    assert client.post("/api/lab/redeploy").status_code == 409


def test_redeploy_restarts_runner(client, lab_dir, fake_docker):
    res = client.post("/api/lab/redeploy").json()
    assert res["ok"] and "déploiement de lab/sonic-lab.clab.yml" in res["message"]
    wait_idle(client)
    assert fake_docker.lifecycle == [("restart", RUNNER, 180)]
    assert (lab_dir / ".runner-status").read_text() == "deployed\n"  # plus écrit par l'interface


def test_lab_actions_without_runner_service(client, fake_docker):
    fake_docker.containers = [c for c in fake_docker.containers if c["Id"] != "runner"]
    assert client.get("/api/lab").json()["runner"]["state"] == "absent"
    for path in ("/api/lab/start", "/api/lab/stop", "/api/lab/redeploy"):
        res = client.post(path)
        assert res.status_code == 400 and "make up" in res.json()["error"]


def test_nodes_shown_down_while_stopping(client, lab_dir, fake_docker):
    (lab_dir / ".runner-status").write_text("destroying\n")
    fake_docker.containers[0]["State"] = "exited"  # sonic1 en cours de destruction
    data = client.get("/api/lab").json()
    sonic1 = next(n for n in data["nodes"] if n["name"] == "sonic1")
    assert sonic1["phase"] == "down" and "arrêt" in sonic1["detail"]


def test_stale_nodes_listed(client, fake_docker):
    # topologie changée pendant que le lab tournait : old1 n'est plus dans le fichier
    fake_docker.containers.append(_container("old1", "linux", "172.20.30.99"))
    data = client.get("/api/lab").json()
    assert data["stale_nodes"] == ["old1"]
    assert {n["name"] for n in data["nodes"]} == {"sonic1", "client1"}
    assert data["topology_file"] == "lab/sonic-lab.clab.yml" and data["topology_error"] is None


@pytest.mark.parametrize("content", ["name: testlab\ntopology: [\n", "- une liste\n", "topology: [a, b]\n"])
def test_unreadable_topology_keeps_lab_state(client, lab_dir, content):
    (lab_dir / "sonic-lab.clab.yml").write_text(content)
    res = client.get("/api/lab")
    assert res.status_code == 200
    data = res.json()
    assert "fichier de topologie invalide" in data["topology_error"]
    assert data["nodes"] == [] and data["links"] == [] and data["stale_nodes"] == []
    assert data["name"] == "testlab" and data["runner"]["state"] == "deployed"
    assert client.post("/api/lab/stop").json()["ok"]  # les actions restent possibles


def test_topology_with_other_lab_name(client, lab_dir):
    path = lab_dir / "sonic-lab.clab.yml"
    path.write_text(path.read_text().replace("name: testlab", "name: autre-lab"))
    data = client.get("/api/lab").json()
    assert "« testlab »" in data["topology_error"] and "refusé" in data["topology_error"]
    assert {n["name"] for n in data["nodes"]} == {"sonic1", "client1"}  # aperçu du fichier


def test_cross_site_requests_rejected(client):
    assert client.post("/api/lab/redeploy", headers={"Origin": "http://testserver"}).status_code == 200
    res = client.post("/api/lab/redeploy", headers={"Origin": "https://evil.example"})
    assert res.status_code == 403 and "origine refusée" in res.json()["error"]
    res = client.post("/api/lab/stop", headers={"Origin": "https://evil.example"})
    assert res.status_code == 403 and "origine refusée" in res.json()["error"]
    # lecture intersite : la réponse reste illisible pour le site tiers (CORS), la requête est permise
    assert client.get("/api/lab", headers={"Origin": "https://evil.example"}).status_code == 200
    assert client.post("/api/lab/connectivity").status_code == 200  # client sans Origin (curl)


def test_websocket_from_foreign_origin_rejected(client, fake_docker):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/nodes/sonic1/terminal?shell=bash", headers={"Origin": "https://evil.example"}):
            pass
    assert exc.value.code == 1008
    assert fake_docker.tty is None  # aucun docker exec lancé


def test_host_header_checked_against_allowed_hosts(lab_dir, fake_docker):
    app = create_app(make_settings(lab_dir, allowed_hosts=DEFAULT_ALLOWED_HOSTS), docker=fake_docker)
    for base, expected in (("http://127.0.0.1:8080", 200), ("http://localhost:8080", 200), ("http://evil.example:8080", 400)):
        with TestClient(app, base_url=base) as c:
            assert c.get("/api/health").status_code == expected, base
    star = create_app(make_settings(lab_dir, allowed_hosts=("*",)), docker=fake_docker)
    with TestClient(star, base_url="http://lab.example:8080") as c:
        assert c.get("/api/health").status_code == 200


def test_custom_allowed_hosts_keep_loopback(monkeypatch, lab_dir, fake_docker):
    # WEB_ALLOWED_HOSTS personnalisé : la boucle locale reste acceptée (healthcheck, make smoke)
    monkeypatch.setenv("WEB_ALLOWED_HOSTS", "netops.example, Lab.Example")
    hosts = Settings.from_env().allowed_hosts
    assert hosts[: len(DEFAULT_ALLOWED_HOSTS)] == DEFAULT_ALLOWED_HOSTS
    assert {"netops.example", "lab.example"} <= set(hosts)
    app = create_app(make_settings(lab_dir, allowed_hosts=hosts), docker=fake_docker)
    for base, expected in (("http://127.0.0.1:8080", 200), ("http://netops.example", 200), ("http://evil.example", 400)):
        with TestClient(app, base_url=base) as c:
            assert c.get("/api/health").status_code == expected, base
    monkeypatch.delenv("WEB_ALLOWED_HOSTS")
    assert Settings.from_env().allowed_hosts == DEFAULT_ALLOWED_HOSTS


def test_monitoring_info(client):
    client.app.state.monitoring.probe = lambda: False
    data = client.get("/api/monitoring").json()
    assert data["grafana"] == {"url": None, "port": 3000, "available": False}
    assert data["dashboards"]["router"] == "lab-router" and data["dashboards"]["client"] == "lab-client"
    assert data["dashboards"]["flows"] == "lab-flows"  # onglet « Flux » des routeurs


def test_monitoring_public_url_and_availability(lab_dir, fake_docker):
    app = create_app(make_settings(lab_dir, grafana_url="https://grafana.lab", grafana_port=3300), docker=fake_docker)
    app.state.monitoring.probe = lambda: True
    with TestClient(app) as c:
        data = c.get("/api/monitoring").json()
    assert data["grafana"] == {"url": "https://grafana.lab", "port": 3300, "available": True}
