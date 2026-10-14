import json
from datetime import datetime, timezone

import pytest

from homelab_dashboard.snapshots import (
    WorkstationSnapshot,
    YunSnapshot,
    format_duration,
    merge_latest,
    parse_event,
    parse_snapshot,
)

YUN_SNAPSHOT = {
    "src": "yun",
    "t": 1791974240,
    "power": "On",
    "temps": {"TEMP_CPU": 38, "TEMP_M.2": 57},
    "fans": {"FAN6": 800, "FAN7": 800},
    "yun": {"load_1m": 0.44, "mem_available_kb": 40296, "uptime_s": 6379, "watchdog_running": True},
    "events": [
        "2026-10-14 07:20:47 UTC reboot sent, router stopped answering (timed out)",
        "2026-10-14 08:58:20 UTC watching 192.168.0.1 every 60s, reboot after 5 failures",
    ],
}
WORKSTATION_SNAPSHOT = {
    "src": "workstation",
    "t": 1791974350,
    "uptime_s": 9869,
    "load_1m": 0.0,
    "disk_used_pct": 36.0,
    "units": {"docker": "active", "sunshine": "active"},
    "desktop_session": True,
    "gpu": {"util_pct": 0.0, "temp_c": 34.0, "mem_used_mib": 560.0, "mem_total_mib": 32760.0, "power_w": 3.81},
    "containers_running": 5,
}


def test_parses_a_yun_snapshot() -> None:
    source, snapshot = parse_snapshot(json.dumps(YUN_SNAPSHOT))

    assert source == "yun"
    assert isinstance(snapshot, YunSnapshot)
    assert snapshot.power == "On"
    assert snapshot.temps["TEMP_M.2"] == 57
    assert snapshot.events[0].at == datetime(2026, 10, 14, 7, 20, 47, tzinfo=timezone.utc)
    assert snapshot.events[0].text == "reboot sent, router stopped answering (timed out)"


def test_parses_a_workstation_snapshot() -> None:
    source, snapshot = parse_snapshot(json.dumps(WORKSTATION_SNAPSHOT))

    assert source == "workstation"
    assert isinstance(snapshot, WorkstationSnapshot)
    assert snapshot.gpu is not None and snapshot.gpu["temp_c"] == 34.0
    assert snapshot.containers_running == 5


def test_yun_snapshot_without_bmc_data_keeps_the_error() -> None:
    data = {key: value for key, value in YUN_SNAPSHOT.items() if key not in ("power", "temps", "fans")}
    data["bmc_error"] = "curl exit 90: "

    _, snapshot = parse_snapshot(json.dumps(data))

    assert isinstance(snapshot, YunSnapshot)
    assert snapshot.power is None
    assert snapshot.bmc_error == "curl exit 90: "


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown snapshot source"):
        parse_snapshot(json.dumps({"src": "toaster", "t": 0}))


def test_merge_keeps_the_newest_snapshot_per_source() -> None:
    older = dict(YUN_SNAPSHOT, t=1000, power="Off")
    newer = dict(YUN_SNAPSHOT, t=2000, power="On")
    latest: dict = {}

    sources, errors = merge_latest(latest, [json.dumps(newer), json.dumps(older), json.dumps(WORKSTATION_SNAPSHOT)])

    assert sources == {"yun", "workstation"}
    assert errors == []
    assert latest["yun"].power == "On"


def test_merge_reports_unreadable_messages_instead_of_failing() -> None:
    latest: dict = {}

    sources, errors = merge_latest(latest, ["not json", json.dumps({"src": "yun", "t": 1})])

    assert sources == set()
    assert len(errors) == 2
    assert latest == {}


def test_event_without_timestamp_keeps_the_whole_line() -> None:
    event = parse_event("cannot read watchdog log: [Errno 2] No such file")

    assert event.text == "cannot read watchdog log: [Errno 2] No such file"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(42, "42s"), (125, "2m"), (3 * 3600 + 5 * 60, "3h 5m"), (2 * 86400 + 4 * 3600, "2d 4h")],
)
def test_format_duration(seconds: int, expected: str) -> None:
    assert format_duration(seconds) == expected
