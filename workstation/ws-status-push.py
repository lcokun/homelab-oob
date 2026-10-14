#!/usr/bin/env python3
"""Publish a workstation status snapshot to the homelab dashboard's ntfy topic.

Run by ws-status-push.timer at boot and every 30 minutes. The laptop dashboard
shows the latest snapshot; while the workstation is off, its last one goes stale.
"""
import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

CONFIG_PATH = Path("/etc/ws-status.conf")
NTFY_URL = "https://ntfy.sh"
HTTP_TIMEOUT_S = 15
SYSTEM_UNITS = ("x3000z-watchdog", "tailscaled", "docker")
USER_UNITS = {"sunshine": "app-dev.lizardbyte.app.Sunshine.service"}
GPU_FIELDS = ("util_pct", "temp_c", "mem_used_mib", "mem_total_mib", "power_w")
GPU_QUERY = "utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw"


def read_status_topic() -> str:
    for line in CONFIG_PATH.read_text().splitlines():
        key, _, value = line.partition("=")
        if key == "STATUS_TOPIC":
            return value
    raise KeyError(f"STATUS_TOPIC missing from {CONFIG_PATH}")


def run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


def unit_state(args: list[str]) -> str:
    # `systemctl is-active` exits non-zero for anything but "active"; the printed state is the answer.
    state = subprocess.run(args, capture_output=True, text=True, check=False).stdout.strip()
    return state or "unknown"


def read_uptime_s() -> int:
    return int(float(Path("/proc/uptime").read_text().split()[0]))


def read_load_1m() -> float:
    return float(Path("/proc/loadavg").read_text().split()[0])


def gpu_stats() -> dict[str, float]:
    values = run(["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"])
    return dict(zip(GPU_FIELDS, (float(value) for value in values.split(", "))))


def unit_states() -> dict[str, str]:
    states = {unit: unit_state(["systemctl", "is-active", unit]) for unit in SYSTEM_UNITS}
    for name, unit in USER_UNITS.items():
        states[name] = unit_state(["systemctl", "--user", "is-active", unit])
    return states


def running_containers() -> int:
    return len(run(["docker", "ps", "-q"]).splitlines())


def desktop_session_active() -> bool:
    # Sunshine needs a logged-in session on the physical seat; auto-login provides it.
    return any("seat0" in line for line in run(["loginctl", "list-sessions", "--no-legend"]).splitlines())


def build_snapshot() -> dict:
    disk = shutil.disk_usage("/")
    snapshot = {
        "src": "workstation",
        "t": int(time.time()),
        "uptime_s": read_uptime_s(),
        "load_1m": read_load_1m(),
        "disk_used_pct": round(100 * disk.used / disk.total, 1),
        "units": unit_states(),
        "desktop_session": desktop_session_active(),
    }
    try:
        snapshot["gpu"] = gpu_stats()
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as error:
        snapshot["gpu_error"] = str(error)
    try:
        snapshot["containers_running"] = running_containers()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        snapshot["containers_error"] = str(error)
    return snapshot


def publish(topic: str, snapshot: dict) -> None:
    body = json.dumps(snapshot, separators=(",", ":"), sort_keys=True).encode()
    request = urllib.request.Request(f"{NTFY_URL}/{topic}", data=body, method="POST")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S):
        pass


def main() -> None:
    snapshot = build_snapshot()
    publish(read_status_topic(), snapshot)
    print(json.dumps(snapshot, sort_keys=True))


if __name__ == "__main__":
    main()
