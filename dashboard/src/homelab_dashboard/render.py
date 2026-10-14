"""Turn snapshots into the Rich renderables shown in each panel."""
from datetime import datetime

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from .snapshots import WatchdogEvent, WorkstationSnapshot, YunSnapshot, format_duration

SNAPSHOT_STALE_S = 45 * 60  # both devices publish every 30 minutes
# Display order and labels. FSC_INDEX is the BMC's fan-control input, not a temperature, so it's left out.
SENSOR_LABELS = {
    "TEMP_CPU": "CPU",
    "TEMP_VR": "VRM",
    "TEMP_MB": "Board",
    "TEMP_M.2": "M.2 SSD",
    "TEMP_X710": "10G NIC",
    "TEMP_CARD_SIDE": "Card side",
}
HIDDEN_SENSORS = {"FSC_INDEX"}
TEMP_WARN_C = {"TEMP_CPU": 85, "TEMP_M.2": 70}
DEFAULT_TEMP_WARN_C = 75
UNIT_LABELS = {"x3000z-watchdog": "watchdog"}
EVENT_STYLES = (
    ("reboot failed", "bold red"),
    ("unreachable", "red"),
    ("rebooting", "yellow"),
    ("reboot sent", "yellow"),
    ("reboot accepted", "yellow"),
    ("back after", "green"),
)
DROP_MARKER = "unreachable"
REBOOT_MARKERS = ("rebooting X3000Z", "reboot sent", "reboot accepted")


def local_time(moment: datetime) -> str:
    return moment.astimezone().strftime("%d %b %H:%M")


def age_text(taken_at: datetime, now: datetime) -> Text:
    age_s = (now - taken_at).total_seconds()
    style = "yellow" if age_s > SNAPSHOT_STALE_S else "dim"
    return Text(f"{format_duration(age_s)} ago", style=style)


def key_value_table(rows: list[tuple[str, RenderableType]]) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for key, value in rows:
        table.add_row(key, value)
    return table


def power_text(power: str | None) -> Text:
    styles = {"On": "bold green", "Off": "bold red"}
    return Text(power or "unknown", style=styles.get(power or "", "yellow"))


def reachability_text(os_reachable: bool | None) -> Text:
    if os_reachable is None:
        return Text("checking…", style="dim")
    if os_reachable:
        return Text("reachable over Tailscale", style="green")
    return Text("not reachable over Tailscale", style="red")


def gpu_text(workstation: WorkstationSnapshot) -> Text:
    if workstation.gpu is None:
        return Text(workstation.gpu_error or "no data", style="red")
    gpu = workstation.gpu
    return Text(
        f"{gpu['util_pct']:.0f}% · {gpu['temp_c']:.0f}°C · "
        f"{gpu['mem_used_mib'] / 1024:.1f}/{gpu['mem_total_mib'] / 1024:.0f} GiB · {gpu['power_w']:.0f} W"
    )


def units_text(units: dict[str, str]) -> Text:
    text = Text()
    for unit, state in units.items():
        text.append("● ", style="green" if state == "active" else "red")
        text.append(f"{UNIT_LABELS.get(unit, unit)}  ")
    return text


def workstation_panel(
    workstation: WorkstationSnapshot | None, yun: YunSnapshot | None, os_reachable: bool | None, now: datetime
) -> RenderableType:
    power_row = Text.assemble(power_text(yun.power if yun else None), "  ", Text("per BMC", style="dim"))
    rows: list[tuple[str, RenderableType]] = [("Power", power_row), ("OS", reachability_text(os_reachable))]
    if workstation is None:
        rows.append(("Details", Text("no snapshot in the last 12h", style="dim")))
        return key_value_table(rows)
    powered_off = yun is not None and yun.power == "Off"
    details = [
        ("Snapshot", age_text(workstation.taken_at, now)),
        ("Uptime", Text(f"{format_duration(workstation.uptime_s)} · load {workstation.load_1m:.2f}")),
        ("GPU", gpu_text(workstation)),
        ("Disk /", Text(f"{workstation.disk_used_pct:.0f}% used")),
        ("Services", units_text(workstation.units)),
        (
            "Session",
            Text(
                f"desktop {'up' if workstation.desktop_session else 'DOWN'} · "
                f"{workstation.containers_running if workstation.containers_running is not None else '?'} containers"
            ),
        ),
    ]
    if powered_off:
        # Show what it looked like before it went off, but make clear it's history.
        details = [(key, Text(value.plain, style="dim")) for key, value in details]
        details[0] = ("Last seen", Text(f"{local_time(workstation.taken_at)} (powered off since)", style="dim"))
    return key_value_table(rows + details)


def temperature_text(name: str, celsius: float) -> Text:
    warn_at = TEMP_WARN_C.get(name, DEFAULT_TEMP_WARN_C)
    return Text(f"{celsius:.0f}°C", style="bold red" if celsius >= warn_at else "")


def sensors_panel(yun: YunSnapshot | None, now: datetime) -> RenderableType:
    if yun is None:
        return Text("no Yún snapshot yet", style="dim")
    if yun.bmc_error:
        return Text(f"BMC unreachable from the Yún: {yun.bmc_error}", style="red")
    known = [name for name in SENSOR_LABELS if name in yun.temps]
    unknown = sorted(name for name in yun.temps if name not in SENSOR_LABELS and name not in HIDDEN_SENSORS)
    rows: list[tuple[str, RenderableType]] = [
        (SENSOR_LABELS.get(name, name), temperature_text(name, yun.temps[name])) for name in known + unknown
    ]
    rows += [(name.replace("FAN", "Fan "), Text(f"{rpm} rpm")) for name, rpm in sorted(yun.fans.items())]
    rows.append(("As of", age_text(yun.taken_at, now)))
    return key_value_table(rows)


def event_text(event: WatchdogEvent) -> Text:
    style = next((style for marker, style in EVENT_STYLES if marker in event.text), "dim")
    return Text.assemble((local_time(event.at), "dim"), "  ", (event.text, style))


def last_event_matching(events: list[WatchdogEvent], markers: tuple[str, ...]) -> str:
    matches = [event for event in events if any(marker in event.text for marker in markers)]
    return local_time(matches[-1].at) if matches else "none in recent log"


def network_panel(yun: YunSnapshot | None) -> RenderableType:
    if yun is None:
        return Text("no Yún snapshot yet", style="dim")
    summary = key_value_table([
        ("Last drop", Text(last_event_matching(yun.events, (DROP_MARKER,)))),
        ("Last reboot", Text(last_event_matching(yun.events, REBOOT_MARKERS))),
    ])
    recent = [event_text(event) for event in reversed(yun.events)]
    return Group(summary, Text(""), Text("Watchdog log (newest first)", style="bold"), *recent)


def yun_panel(yun: YunSnapshot | None, now: datetime) -> RenderableType:
    if yun is None:
        return Text("no Yún snapshot yet", style="dim")
    watchdog = Text("running", style="green") if yun.watchdog_running else Text("NOT running", style="bold red")
    return key_value_table([
        ("Uptime", Text(format_duration(yun.uptime_s))),
        ("Memory", Text(f"{yun.mem_available_kb / 1024:.0f} MB available")),
        ("Load", Text(f"{yun.load_1m:.2f}")),
        ("Watchdog", watchdog),
        ("Snapshot", age_text(yun.taken_at, now)),
    ])
