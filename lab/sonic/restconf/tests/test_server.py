"""Serveur HTTP réel sur 127.0.0.1 (sans TLS, puis avec TLS si openssl est disponible)."""

import base64
import http.client
import json
import shutil
import ssl
import threading

import pytest

from restconf.server import RestconfServer, ensure_certificate
from restconf.service import RestconfService

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}


@pytest.fixture
def server(sonic, frr):
    srv = RestconfServer(("127.0.0.1", 0), RestconfService(sonic, frr, scheme="http"), user="admin", password="secret")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, headers=None, conn=None):
    conn = conn or http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/yang-data+json"
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    return resp, resp.read()


def test_authentication(server):
    resp, body = request(server, "GET", "/restconf")
    assert resp.status == 401 and resp.getheader("WWW-Authenticate") == 'Basic realm="restconf"'
    assert json.loads(body)["ietf-restconf:errors"]["error"][0]["error-tag"] == "access-denied"
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:mauvais").decode()}
    assert request(server, "GET", "/restconf", headers=bad)[0].status == 401
    assert request(server, "GET", "/restconf", headers={"Authorization": "Basic %%%"})[0].status == 401
    resp, body = request(server, "GET", "/restconf", headers=AUTH)
    assert resp.status == 200 and resp.getheader("Content-Type") == "application/yang-data+json"
    assert request(server, "GET", "/.well-known/host-meta")[0].status == 200  # découverte publique


def test_keep_alive_head_and_write(server, sonic):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    path = "/restconf/data/sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0/description"
    resp, body = request(server, "HEAD", path, headers=AUTH, conn=conn)
    assert resp.status == 200 and body == b"" and int(resp.getheader("Content-Length")) > 0
    resp, body = request(server, "PUT", path, {"sonic-port:description": "via http"}, headers=AUTH, conn=conn)
    assert resp.status == 204
    assert sonic.configdb["PORT"]["Ethernet0"]["description"] == "via http"
    resp, body = request(server, "GET", path, headers={}, conn=conn)  # même connexion, sans identifiants
    assert resp.status == 401


def test_chunked_body_refused(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.putrequest("PUT", "/restconf/data/sonic-port:sonic-port")
    for k, v in {**AUTH, "Content-Type": "application/yang-data+json", "Transfer-Encoding": "chunked"}.items():
        conn.putheader(k, v)
    conn.endheaders()
    conn.send(b"2\r\n{}\r\n0\r\n\r\n")
    assert conn.getresponse().status == 411


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl absent")
def test_tls(tmp_path, sonic, frr):
    cert, key = ensure_certificate(tmp_path / "certs", "sonic-test", "127.0.0.1")
    assert ensure_certificate(tmp_path / "certs", "sonic-test", "127.0.0.1") == (cert, key)  # réutilisé
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    srv = RestconfServer(("127.0.0.1", 0), RestconfService(sonic, frr), user="admin", password="secret", ssl_context=context)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        client = ssl.create_default_context(cafile=str(cert))
        conn = http.client.HTTPSConnection("127.0.0.1", srv.server_address[1], context=client, timeout=10)
        resp, body = request(srv, "GET", "/restconf/data/sonic-lab:frr/state", headers=AUTH, conn=conn)
        assert resp.status == 200 and json.loads(body)["sonic-lab:state"]["bgp-neighbor"]
    finally:
        srv.shutdown()
        srv.server_close()
