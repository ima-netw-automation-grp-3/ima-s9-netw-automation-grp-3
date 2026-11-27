from labmon.iperf import fmt_bps, parse_result

RESULT = {
    "start": {"timestamp": {"timesecs": 1_795_096_000}},
    "intervals": [
        {
            "streams": [{"rtt": 150, "bits_per_second": 5.0e9}],
            "sum": {"start": 0, "end": 1.0, "bytes": 625_000_000, "bits_per_second": 5.0e9, "retransmits": 0},
        },
        {
            "streams": [{"rtt": 250, "bits_per_second": 4.0e9}],
            "sum": {"start": 1.0, "end": 2.0, "bytes": 500_000_000, "bits_per_second": 4.0e9, "retransmits": 2},
        },
    ],
    "end": {
        "streams": [{"sender": {"mean_rtt": 200, "max_rtt": 300}}],
        "sum_sent": {"seconds": 2.0, "bits_per_second": 4.6e9, "retransmits": 2},
        "sum_received": {"seconds": 2.0, "bytes": 1_125_000_000, "bits_per_second": 4.5e9},
    },
}


def test_parse_result():
    tags = {"src": "client1", "dst": "client2", "test_id": "t1"}
    points, summary = parse_result(RESULT, tags, 1)
    intervals = [p for p in points if p.tags["sample"] == "interval"]
    assert [p.fields["bps"] for p in intervals] == [5.0e9, 4.0e9]
    assert intervals[0].fields["rtt_ms"] == 0.15 and intervals[1].fields["retransmits"] == 2
    assert intervals[1].ts_ns == (1_795_096_000 + 2) * 1_000_000_000
    assert summary["received_bps"] == 4.5e9 and summary["retransmits"] == 2
    assert summary["mean_rtt_ms"] == 0.2 and summary["max_rtt_ms"] == 0.3
    final = points[-1]
    assert final.tags["sample"] == "summary" and final.ts_ns == (1_795_096_000 + 2) * 1_000_000_000


def test_fmt_bps():
    assert fmt_bps(5.12e9) == "5,12 Gbit/s"
    assert fmt_bps(950e3) == "950,00 kbit/s"
    assert fmt_bps(12) == "12 bit/s"
