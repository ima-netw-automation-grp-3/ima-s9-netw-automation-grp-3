import math

from labmon.lineproto import Point, encode, generic_fields, ns, ns_us, num


def test_num_types():
    assert num(3) == 3.0
    assert num("42") == 42.0
    assert num("-1.5e3") == -1500.0
    assert num(True) == 1.0
    assert num("N/A") is None
    assert num("") is None
    assert num(float("nan")) is None
    assert num(math.inf) is None
    assert num(None) is None


def test_generic_fields_never_mix_types():
    out = generic_fields({"speed": "100000", "description": "client1", "fec": "N/A", "empty": "", "nested": {"a": 1}}, "cfg_")
    assert out == {"cfg_speed": 100000.0, "cfg_description_text": "client1", "cfg_fec_text": "N/A"}


def test_encode_escapes_and_types():
    p = Point(
        "lab test",
        {"node": "sonic 1", "path": "a,b=c", "empty": "", "none": None},
        {"n": 3, "flag": False, "s": 'il a dit "bonjour"\nfin', "bad": float("nan"), "back": "a\\b"},
        1_000,
    )
    line = encode(p)
    assert line == (
        'lab\\ test,node=sonic\\ 1,path=a\\,b\\=c '
        'back="a\\\\b",flag=0.0,n=3.0,s="il a dit \\"bonjour\\" fin" 1000'
    )


def test_encode_without_valid_field_is_skipped():
    assert encode(Point("m", {"a": "b"}, {"x": None, "y": float("inf")})) is None


def test_strings_are_truncated():
    line = encode(Point("m", {}, {"s": "x" * 5000}))
    assert len(line) < 1100


def test_timestamps():
    assert ns(1.2345678) == 1_235_000_000
    assert ns_us(1.2345678) == 1_234_568_000
