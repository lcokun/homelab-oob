import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console, RenderableType

from homelab_dashboard import render
from homelab_dashboard.config import load_config
from homelab_dashboard.snapshots import parse_snapshot

from .test_snapshots import WORKSTATION_SNAPSHOT, YUN_SNAPSHOT


def plain(renderable: RenderableType) -> str:
    console = Console(width=120, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def snapshot(data: dict):
    return parse_snapshot(json.dumps(data))[1]


def test_workstation_panel_shows_live_details_when_on() -> None:
    now = datetime.fromtimestamp(WORKSTATION_SNAPSHOT["t"], timezone.utc) + timedelta(minutes=5)

    text = plain(render.workstation_panel(snapshot(WORKSTATION_SNAPSHOT), snapshot(YUN_SNAPSHOT), True, now))

    assert "On" in text
    assert "reachable over Tailscale" in text
    assert "34°C" in text
    assert "5 containers" in text


def test_workstation_panel_marks_details_as_history_when_powered_off() -> None:
    now = datetime.fromtimestamp(WORKSTATION_SNAPSHOT["t"], timezone.utc) + timedelta(hours=3)
    yun_off = snapshot(dict(YUN_SNAPSHOT, power="Off"))

    text = plain(render.workstation_panel(snapshot(WORKSTATION_SNAPSHOT), yun_off, False, now))

    assert "Off" in text
    assert "Last seen" in text
    assert "powered off since" in text


def test_sensors_panel_flags_a_hot_ssd() -> None:
    hot = snapshot(dict(YUN_SNAPSHOT, temps={"TEMP_M.2": 75}))
    now = hot.taken_at

    renderable = render.sensors_panel(hot, now)

    assert "M.2 SSD" in plain(renderable)
    assert render.temperature_text("TEMP_M.2", 75).style == "bold red"
    assert render.temperature_text("TEMP_M.2", 57).style == ""


def test_network_panel_summarises_the_last_reboot() -> None:
    text = plain(render.network_panel(snapshot(YUN_SNAPSHOT)))

    assert "Last drop" in text and "none in recent log" in text
    assert "reboot sent" in text


def test_config_reads_the_ws_remote_file(tmp_path: Path) -> None:
    path = tmp_path / "ws-remote.conf"
    path.write_text("CMD_TOPIC=ws-a\nREPLY_TOPIC=ws-b\nSTATUS_TOPIC=ws-c\nWS_TAILSCALE_HOST=100.64.0.1\n")

    config = load_config(path)

    assert (config.cmd_topic, config.status_topic, config.tailscale_host) == ("ws-a", "ws-c", "100.64.0.1")


def test_config_names_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / "ws-remote.conf"
    path.write_text("CMD_TOPIC=ws-a\n")

    with pytest.raises(ValueError, match="STATUS_TOPIC, WS_TAILSCALE_HOST"):
        load_config(path)
