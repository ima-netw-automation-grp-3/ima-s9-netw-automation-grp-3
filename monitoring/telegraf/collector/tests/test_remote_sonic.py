"""Analyseurs purs du collecteur exécuté dans le routeur (remote/sonic_collect.py, Python 3.11)."""

import calendar
from datetime import datetime, timezone

from conftest import text
from labmon.remote import sonic_collect as sc

SUPERVISOR = """arp_update                       STOPPED   Not started
bgpd                             RUNNING   pid 707, uptime 0:17:07
orchagent                        RUNNING   pid 223, uptime 2 days, 3:04:05
start.sh                         EXITED    Nov 19 01:06 PM
syncd                            FATAL     Exited too quickly (process log may have details)
"""


def test_parse_supervisorctl():
    services = {s["name"]: s for s in sc.parse_supervisorctl(SUPERVISOR)}
    assert services["bgpd"]["pid"] == 707 and services["bgpd"]["uptime_s"] == 17 * 60 + 7
    assert services["orchagent"]["uptime_s"] == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
    assert "pid" not in services["start.sh"] and services["start.sh"]["state"] == "EXITED"
    assert services["syncd"]["state"] == "FATAL"


def test_parse_lanemap_and_version():
    assert sc.parse_lanemap("eth2:29,30,31,32\neth1:25,26,27,28\n") == {"29,30,31,32": "eth2", "25,26,27,28": "eth1"}
    version = sc.parse_version_yml("build_version: 'master.123'\nasic_type: vs\nlibs:\n  - a\n")
    assert version == {"build_version": "master.123", "asic_type": "vs", "libs": ""}


def test_parse_proc_stat_handles_spaces_in_name():
    stat = "42 (my proc) S 1 42 42 0 -1 4194560 100 0 0 0 150 50 0 0 20 0 7 0 1234 100000 2560 18446744073709551615"
    assert sc.parse_proc_stat(stat) == {"ppid": 1, "ticks": 200, "threads": 7, "rss_pages": 2560}


def test_table_of():
    assert sc.table_of("PORT|Ethernet0", "|") == "PORT"
    assert sc.table_of("ROUTE_TABLE:10.0.0.0/8", ":") == "ROUTE_TABLE"
    assert sc.table_of("ASIC_STATE:SAI_OBJECT_TYPE_PORT:oid:0x1", ":") == "ASIC_STATE:SAI_OBJECT_TYPE_PORT"
    assert sc.table_of("CONFIG_DB_INITIALIZED", "|") == "CONFIG_DB_INITIALIZED"


def test_program_of():
    assert sc.program_of("bgp#bgpd[707]:") == "bgpd"
    assert sc.program_of("#orchagent:") == "orchagent"
    assert sc.program_of("#lldp-syncd") == "lldp-syncd"
    assert sc.program_of("swss#orchagent:") == "orchagent"


def test_parse_real_syslog():
    parsed = [sc.parse_syslog_line(line) for line in text("syslog_sonic.txt").splitlines()]
    assert all(p is not None for p in parsed)
    stamp, severity, program, message = parsed[0]
    assert stamp == "Nov 19 13:48:49.339843" and severity == "INFO" and program == "supervisord"
    assert sc.parse_syslog_line("ligne libre sans format") is None


def test_syslog_epoch_infers_year():
    now = calendar.timegm((2026, 11, 19, 14, 0, 0))
    expected = calendar.timegm((2026, 11, 19, 13, 23, 59)) + 0.148617
    assert abs(sc.syslog_epoch("Nov 19 13:23:59.148617", now) - expected) < 1e-6
    # le 1er janvier, une ligne du 31 décembre date de l'année précédente
    jan = calendar.timegm((2027, 1, 1, 0, 5, 0))
    dec = sc.syslog_epoch("Dec 31 23:59:00", jan)
    assert datetime.fromtimestamp(dec, tz=timezone.utc).year == 2026
    assert sc.syslog_epoch("Nov  9 01:02:03", now) == calendar.timegm((2026, 11, 9, 1, 2, 3))


def test_summarize_routes_caps():
    data = {
        "default": {
            "10.0.0.0/8": [{"protocol": "static", "selected": True, "installed": True, "nexthops": [{"ip": "1.1.1.1", "active": True}]}],
            "10.1.0.0/16": [{"protocol": "bgp"}],
        },
        "Vrf1": {"192.0.2.0/24": [{"protocol": "connected", "nexthops": [{"interfaceName": "Ethernet8", "directlyConnected": True}]}]},
    }
    routes, truncated = sc.summarize_routes(data, 10)
    assert not truncated
    assert routes["Vrf1"]["192.0.2.0/24"][0]["nexthops"][0] == {
        "ip": None,
        "interface": "Ethernet8",
        "active": False,
        "fib": False,
        "connected": True,
    }
    routes, truncated = sc.summarize_routes(data, 2)
    assert truncated and sum(len(v) for v in routes.values()) == 2
