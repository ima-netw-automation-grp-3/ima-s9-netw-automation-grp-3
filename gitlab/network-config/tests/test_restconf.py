"""Application des documents (module sonic_lab.restconf.config) avec un faux routeur."""

import io
import json
from urllib.error import HTTPError

import pytest

from ansible_collections.sonic_lab.restconf.plugins.module_utils import restconf as rc

PORT = {"name": "sonic-port", "path": "sonic-port:sonic-port", "method": "patch", "content": {"sonic-port:sonic-port": {}}}
FRR = {"name": "frr", "path": "sonic-lab:frr/running-config", "method": "put", "content": {"sonic-lab:running-config": "router bgp 1\n"}}


class FakeRouter:
    """Répond aux previews selon `previews` (chemin -> sortie) et enregistre les écritures."""

    def __init__(self, previews, status=204):
        self.previews = previews
        self.status = status
        self.writes = []

    def operation(self, name, inp):
        assert name == "sonic-lab:preview" and set(inp) == {"path", "method", "data"}
        return self.previews[inp["path"]]

    def request(self, method, path, body=None):
        self.writes.append((method, path))
        return self.status, None if self.status < 300 else {"ietf-restconf:errors": {"error": [{"error-message": "refusé"}]}}


def test_unchanged_documents_are_not_written():
    router = FakeRouter({PORT["path"]: {"valid": True, "diff": "", "patch": "[]"}, FRR["path"]: {"valid": True, "diff": ""}})
    results = rc.apply_documents(router, [PORT, FRR], check_mode=False)
    assert [r["changed"] for r in results] == [False, False] and router.writes == []


def test_changes_are_written_in_order_with_upper_case_methods():
    router = FakeRouter({PORT["path"]: {"valid": True, "diff": "-a\n+b\n", "patch": '[{"op": "add"}]'}, FRR["path"]: {"valid": True, "diff": "-x\n+y\n", "resets": ["10.0.12.2"]}})
    results = rc.apply_documents(router, [PORT, FRR], check_mode=False)
    assert router.writes == [("PATCH", "/data/sonic-port:sonic-port"), ("PUT", "/data/sonic-lab:frr/running-config")]
    assert results[1]["resets"] == ["10.0.12.2"] and results[0]["status"] == 204
    sections = rc.diff_sections(results)
    assert sections[0]["prepared"].startswith("=== sonic-port : PATCH sonic-port:sonic-port\n-a\n+b")


def test_check_mode_only_previews():
    router = FakeRouter({PORT["path"]: {"valid": True, "diff": "+b\n", "patch": '[{"op": "add"}]'}})
    results = rc.apply_documents(router, [PORT], check_mode=True)
    assert results[0]["changed"] and router.writes == []


def test_config_db_change_is_decided_by_the_patch_not_the_text():
    # diff de présentation sans opération : rien à écrire
    assert not rc.has_changes("sonic-port:sonic-port", {"diff": "-x\n+x\n", "patch": "[]"})
    assert rc.has_changes("sonic-port:sonic-port", {"diff": "", "patch": '[{"op": "remove"}]'})
    assert rc.has_changes("sonic-lab:frr/running-config", {"diff": "", "commands": ["no neighbor x"]})


def test_refusal_stops_at_the_first_invalid_document():
    router = FakeRouter({PORT["path"]: {"valid": False, "errors": "YANG : mtu"}, FRR["path"]: {"valid": True, "diff": "+y"}})
    with pytest.raises(rc.ApplyError, match="refusé par le routeur : YANG : mtu") as exc:
        rc.apply_documents(router, [PORT, FRR], check_mode=False)
    assert [r["name"] for r in exc.value.results] == ["sonic-port"] and router.writes == []


def test_write_error_is_reported_with_the_restconf_message():
    router = FakeRouter({PORT["path"]: {"valid": True, "diff": "+b", "patch": '[{"op": "add"}]'}}, status=400)
    with pytest.raises(rc.ApplyError, match="HTTP 400 : refusé"):
        rc.apply_documents(router, [PORT], check_mode=False)


def test_client_sends_basic_auth_json_and_reads_errors():
    calls = []

    class Resp:
        def __init__(self, code, body):
            self.code, self.body = code, body

        def getcode(self):
            return self.code

        def read(self):
            return self.body

    def opener(url, **kw):
        calls.append((url, kw))
        if url.endswith("/data/missing"):
            raise HTTPError(url, 404, "Not Found", {}, io.BytesIO(b'{"ietf-restconf:errors":{"error":[{"error-message":"absent","error-path":"/x"}]}}'))
        return Resp(200, json.dumps({"sonic-lab:output": {"valid": True}}).encode())

    client = rc.Restconf("https://172.20.30.11/restconf/", "admin", "pw", opener=opener)
    assert client.operation("sonic-lab:preview", {"path": "p"}) == {"valid": True}
    url, kw = calls[0]
    assert url == "https://172.20.30.11/restconf/operations/sonic-lab:preview"
    assert kw["method"] == "POST" and kw["force_basic_auth"] and kw["url_password"] == "pw" and not kw["validate_certs"]
    assert json.loads(kw["data"]) == {"sonic-lab:input": {"path": "p"}} and kw["headers"]["Content-Type"] == rc.MEDIA
    status, doc = client.request("GET", "/data/missing")
    assert status == 404 and rc.errors_text(doc) == "absent (/x)"


def test_unreachable_router():
    def opener(url, **kw):
        raise OSError("connexion refusée")

    with pytest.raises(rc.RestconfError, match="injoignable"):
        rc.Restconf("https://192.0.2.1/restconf", "a", "b", opener=opener).request("GET", "/data/x")
