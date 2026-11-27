"""Serveur HTTPS du RESTCONF : TLS, authentification HTTP Basic, journal. La logique est dans service.py.

Variables d'environnement (passées aux routeurs par lab/sonic-lab.clab.yml) :
  RESTCONF_USER, RESTCONF_PASSWORD   identifiants HTTP Basic (admin / YourPaSsWoRd par défaut)
  RESTCONF_BIND                      adresse d'écoute (défaut : IPv4 d'eth0, l'interface de management)
  RESTCONF_PORT                      port (443)
  RESTCONF_CERT_DIR                  certificat auto-signé, généré au premier démarrage
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .rfc8040 import RestconfError
from .service import Response, RestconfService

log = logging.getLogger("restconf")

MAX_BODY = 16 * 1024 * 1024
PUBLIC_PATHS = frozenset({"/.well-known/host-meta"})  # découverte : sans authentification
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "YourPaSsWoRd"  # mot de passe par défaut de SONiC


class Handler(BaseHTTPRequestHandler):
    server: RestconfServer
    server_version = "sonic-lab-restconf/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 120  # s : une connexion inactive ne bloque pas un thread indéfiniment

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def _handle(self, method: str) -> None:
        started = time.monotonic()
        try:
            resp = self._process(method)
        except Exception as exc:  # garde-fou
            log.exception("erreur interne")
            resp = RestconfService.error_response(RestconfError(500, "operation-failed", f"erreur interne : {exc}"))
        self._send(method, resp)
        log.info(
            "%s %s %s %s %d %.0f ms",
            self.client_address[0],
            self._user or "-",
            method,
            self.path,
            resp.status,
            (time.monotonic() - started) * 1000,
        )

    _user: str | None = None

    def _process(self, method: str) -> Response:
        self._user = None  # connexion HTTP/1.1 persistante : chaque requête s'authentifie
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self.close_connection = True
            return RestconfService.error_response(
                RestconfError(411, "malformed-message", "corps « chunked » non pris en charge : envoyez Content-Length")
            )
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return RestconfService.error_response(
                RestconfError(413 if length > MAX_BODY else 400, "malformed-message", "Content-Length invalide ou corps trop gros")
            )
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        if path not in PUBLIC_PATHS and not self._authorized():
            resp = RestconfService.error_response(RestconfError(401, "access-denied", "authentification HTTP Basic requise"))
            resp.headers["WWW-Authenticate"] = 'Basic realm="restconf"'
            return resp
        return self.server.service.handle(method, self.path, dict(self.headers.items()), body)

    def _authorized(self) -> bool:
        scheme, _, value = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(value.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        expected = f"{self.server.user}:{self.server.password}".encode()
        if hmac.compare_digest(decoded, expected):
            self._user = self.server.user
            return True
        return False

    def _send(self, method: str, resp: Response) -> None:
        body = resp.body or b""
        self.send_response(resp.status)
        for name, value in resp.headers.items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if method != "HEAD" and body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (signature imposée)
        log.debug(format, *args)  # le journal des requêtes est écrit par _handle


class RestconfServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        service: RestconfService,
        *,
        user: str,
        password: str,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(address, Handler)
        self.service = service
        self.user = user
        self.password = password
        if ssl_context is not None:
            # poignée de main TLS dans le thread de la connexion, pas dans la boucle d'acceptation
            self.socket = ssl_context.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        log.warning("connexion %s interrompue : %s", client_address[0] if client_address else "?", exc)


def mgmt_address(interface: str = "eth0") -> str | None:
    """IPv4 de l'interface de management (eth0 dans un nœud containerlab)."""
    try:
        res = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", interface], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)/", res.stdout)
    return m.group(1) if m else None


def ensure_certificate(cert_dir: Path, hostname: str, address: str | None) -> tuple[Path, Path]:
    """Certificat auto-signé (CN = nom du routeur, SAN = nom et IP de management), créé une fois."""
    cert, key = cert_dir / "cert.pem", cert_dir / "key.pem"
    if cert.is_file() and key.is_file():
        return cert, key
    cert_dir.mkdir(parents=True, exist_ok=True)
    san = f"DNS:{hostname}" + (f",IP:{address}" if address else "")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-subj", f"/CN={hostname}",
         "-addext", f"subjectAltName={san}", "-keyout", str(key), "-out", str(cert)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    os.chmod(key, 0o600)
    return cert, key


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)  # supervisord → syslog
    from .frr import Frr  # bibliothèques du routeur : importées seulement ici
    from .sonic import SonicConfigDB

    user = os.environ.get("RESTCONF_USER") or DEFAULT_USER
    password = os.environ.get("RESTCONF_PASSWORD") or DEFAULT_PASSWORD
    port = int(os.environ.get("RESTCONF_PORT") or 443)
    address = os.environ.get("RESTCONF_BIND") or mgmt_address() or "0.0.0.0"
    hostname = socket.gethostname()
    cert_dir = Path(os.environ.get("RESTCONF_CERT_DIR") or "/etc/sonic/lab-restconf")

    sonic = SonicConfigDB()
    service = RestconfService(sonic, Frr(), scheme="https")
    cert, key = ensure_certificate(cert_dir, hostname, None if address == "0.0.0.0" else address)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    server = RestconfServer((address, port), service, user=user, password=password, ssl_context=context)
    log.info(
        "RESTCONF à l'écoute : https://%s:%d/restconf (%d modules YANG, utilisateur %s)",
        address,
        port,
        len(sonic.modules()) + 1,
        user,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
