import pytest

from restconf import rfc8040 as r


def test_parse_path_segments_and_keys():
    segs = r.parse_path("sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0/mtu")
    assert segs == [
        r.Segment("sonic-port", "sonic-port"),
        r.Segment(None, "PORT"),
        r.Segment(None, "PORT_LIST", ("Ethernet0",)),
        r.Segment(None, "mtu"),
    ]
    assert r.format_path(segs) == "sonic-port:sonic-port/PORT/PORT_LIST=Ethernet0/mtu"


def test_parse_path_decodes_after_splitting():
    # virgule et barre oblique encodées dans les valeurs de clé (RFC 8040 § 3.5.3)
    segs = r.parse_path("sonic-interface:sonic-interface/INTERFACE/INTERFACE_IPPREFIX_LIST=Ethernet0,192.168.1.1%2F24")
    assert segs[-1].keys == ("Ethernet0", "192.168.1.1/24")
    assert r.parse_path("m:c/L=a%2Cb")[-1].keys == ("a,b",)
    assert r.parse_path("m:c/L=")[-1].keys == ("",)
    assert r.parse_path("/") == [] and r.parse_path("") == []


@pytest.mark.parametrize("bad", ["m:c//x", "m:c/1bad", "m:c/a b", "bad:id:x"])
def test_parse_path_rejects_invalid(bad):
    with pytest.raises(r.RestconfError) as exc:
        r.parse_path(bad)
    assert exc.value.status == 400


def test_parse_query():
    assert r.parse_query("") == r.Query()
    assert r.parse_query("content=config&depth=2") == r.Query("config", 2)
    assert r.parse_query("depth=unbounded").depth is None
    for bad in ("content=x", "depth=0", "depth=a", "fields=x", "depth=1&depth=2"):
        with pytest.raises(r.RestconfError):
            r.parse_query(bad)


def test_media_types():
    assert r.media_type("application/yang-data+json; charset=utf-8") == "application/yang-data+json"
    assert r.accepts_json(None) and r.accepts_json("*/*") and r.accepts_json("application/json")
    assert r.accepts_json("application/yang-data+xml, application/yang-data+json;q=0.5")
    assert not r.accepts_json("application/yang-data+xml")


def test_error_document():
    err = r.RestconfError(400, "invalid-value", "mtu invalide", error_type="application", path="/a/b")
    assert err.document() == {
        "ietf-restconf:errors": {
            "error": [{"error-type": "application", "error-tag": "invalid-value", "error-path": "/a/b", "error-message": "mtu invalide"}]
        }
    }


def test_modules_state():
    doc = r.modules_state(
        [
            {"name": "sonic-port", "revision": "2019-07-01", "namespace": "ns-port"},
            {"name": "sonic-types", "revision": "", "namespace": "ns-types", "implemented": False},
            {"name": "builtin", "revision": "", "namespace": "ns", "downloadable": False},
        ],
        "https://h/restconf/yang",
    )
    by_name = {m["name"]: m for m in doc["module"]}
    assert by_name["sonic-port"]["schema"] == "https://h/restconf/yang/sonic-port@2019-07-01"
    assert by_name["sonic-port"]["conformance-type"] == "implement"
    assert by_name["sonic-types"]["conformance-type"] == "import" and by_name["sonic-types"]["revision"] == ""
    assert "schema" not in by_name["builtin"]
    assert len(doc["module-set-id"]) == 16
