"""Parse the status snapshots the Yún and the workstation publish to ntfy."""
import json
from dataclasses import dataclass
from datetime import datetime, timezone

WATCHDOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S UTC"
WATCHDOG_TIMESTAMP_LENGTH = len("2026-01-01 00:00:00 UTC")


@dataclass(frozen=True)
class WatchdogEvent:
    at: datetime
    text: str


@dataclass(frozen=True)
class YunSnapshot:
    taken_at: datetime
    power: str | None
    temps: dict[str, float]
    fans: dict[str, int]
    bmc_error: str | None
    uptime_s: int
    mem_available_kb: int
    load_1m: float
    watchdog_running: bool
    events: list[WatchdogEvent]


@dataclass(frozen=True)
class WorkstationSnapshot:
    taken_at: datetime
    uptime_s: int
    load_1m: float
    disk_used_pct: float
    units: dict[str, str]
    desktop_session: bool
    gpu: dict[str, float] | None
    gpu_error: str | None
    containers_running: int | None


Snapshot = YunSnapshot | WorkstationSnapshot


def parse_event(line: str) -> WatchdogEvent:
    stamp, text = line[:WATCHDOG_TIMESTAMP_LENGTH], line[WATCHDOG_TIMESTAMP_LENGTH:].strip()
    try:
        at = datetime.strptime(stamp, WATCHDOG_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        # Lines like "cannot read watchdog log: ..." carry no timestamp.
        return WatchdogEvent(at=datetime.fromtimestamp(0, timezone.utc), text=line)
    return WatchdogEvent(at=at, text=text)


def _taken_at(data: dict) -> datetime:
    return datetime.fromtimestamp(data["t"], timezone.utc)


def parse_yun(data: dict) -> YunSnapshot:
    yun = data["yun"]
    return YunSnapshot(
        taken_at=_taken_at(data),
        power=data.get("power"),
        temps=data.get("temps", {}),
        fans=data.get("fans", {}),
        bmc_error=data.get("bmc_error"),
        uptime_s=yun["uptime_s"],
        mem_available_kb=yun["mem_available_kb"],
        load_1m=yun["load_1m"],
        watchdog_running=yun["watchdog_running"],
        events=[parse_event(line) for line in data.get("events", [])],
    )


def parse_workstation(data: dict) -> WorkstationSnapshot:
    return WorkstationSnapshot(
        taken_at=_taken_at(data),
        uptime_s=data["uptime_s"],
        load_1m=data["load_1m"],
        disk_used_pct=data["disk_used_pct"],
        units=data["units"],
        desktop_session=data["desktop_session"],
        gpu=data.get("gpu"),
        gpu_error=data.get("gpu_error"),
        containers_running=data.get("containers_running"),
    )


PARSERS = {"yun": parse_yun, "workstation": parse_workstation}


def parse_snapshot(text: str) -> tuple[str, Snapshot]:
    data = json.loads(text)
    source = data.get("src")
    if source not in PARSERS:
        raise ValueError(f"unknown snapshot source {source!r}")
    return source, PARSERS[source](data)


def merge_latest(latest: dict[str, Snapshot], texts: list[str]) -> tuple[set[str], list[str]]:
    """Keep the newest snapshot per source in `latest`.

    Returns the sources seen in `texts` and a description of each message that didn't parse.
    """
    sources: set[str] = set()
    errors = []
    for text in texts:
        try:
            source, snapshot = parse_snapshot(text)
        except (ValueError, KeyError, TypeError) as error:
            errors.append(f"{type(error).__name__}: {error}")
            continue
        sources.add(source)
        current = latest.get(source)
        if current is None or snapshot.taken_at >= current.taken_at:
            latest[source] = snapshot
    return sources, errors


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"
