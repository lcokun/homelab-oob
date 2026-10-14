"""Textual dashboard: four panels refreshed from ntfy; `r` asks the Yún for a fresh snapshot."""
import subprocess
import time
from datetime import datetime, timezone

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Grid
from textual.widgets import Footer, Header, Static

from . import render
from .config import Config, load_config
from .ntfy import fetch_messages, publish
from .snapshots import Snapshot, merge_latest

INITIAL_LOOKBACK = "12h"  # how far back ntfy.sh's message cache reaches
POLL_INTERVAL_S = 60
REPORT_WAIT_S = 75  # the Yún checks for commands every 30 s, then needs a few seconds for the BMC
REPORT_POLL_S = 5
TAILSCALE_PING_TIMEOUT = "5s"
PANELS = (
    ("workstation", "Workstation"),
    ("sensors", "BMC sensors"),
    ("network", "Router / backhaul"),
    ("yun", "Yún"),
)


def tailscale_reachable(host: str) -> bool | None:
    try:
        ping = subprocess.run(
            ["tailscale", "ping", "-c", "1", "--timeout", TAILSCALE_PING_TIMEOUT, host],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    return ping.returncode == 0


class HomelabApp(App):
    TITLE = "homelab"
    CSS = """
    Grid { grid-size: 2 2; grid-gutter: 1 2; padding: 0 1; height: 1fr; }
    Static.panel { border: round $accent; padding: 0 1; height: 100%; }
    #status { height: 1; padding: 0 2; color: $text-muted; }
    """
    BINDINGS = [("r", "report", "Fresh report"), ("q", "quit", "Quit")]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.latest: dict[str, Snapshot] = {}
        self.since = INITIAL_LOOKBACK
        self.os_reachable: bool | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Grid():
            for panel_id, _ in PANELS:
                yield Static(id=panel_id, classes="panel")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        for panel_id, title in PANELS:
            self.query_one(f"#{panel_id}", Static).border_title = title
        self.render_panels()
        self.poll_status()
        self.set_interval(POLL_INTERVAL_S, self.poll_status)

    def render_panels(self) -> None:
        now = datetime.now(timezone.utc)
        yun = self.latest.get("yun")
        workstation = self.latest.get("workstation")
        self.query_one("#workstation", Static).update(
            render.workstation_panel(workstation, yun, self.os_reachable, now)
        )
        self.query_one("#sensors", Static).update(render.sensors_panel(yun, now))
        self.query_one("#network", Static).update(render.network_panel(yun))
        self.query_one("#yun", Static).update(render.yun_panel(yun, now))

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def apply_update(self, latest: dict[str, Snapshot], since: str, os_reachable: bool | None, status: str) -> None:
        # Runs on the UI thread, so workers never mutate shared state themselves.
        self.latest, self.since, self.os_reachable = latest, since, os_reachable
        self.render_panels()
        self.set_status(status)

    def fetch_into_copy(self, since: str) -> tuple[dict[str, Snapshot], str, set[str], list[str]]:
        messages = fetch_messages(self.config.status_topic, since)
        latest = dict(self.latest)
        sources, errors = merge_latest(latest, [message.text for message in messages])
        newest_id = messages[-1].id if messages else self.since
        return latest, newest_id, sources, errors

    @staticmethod
    def describe(errors: list[str], fallback: str) -> str:
        return f"skipped {len(errors)} unreadable snapshot(s): {errors[-1]}" if errors else fallback

    @work(thread=True, exclusive=True, group="poll")
    def poll_status(self) -> None:
        try:
            latest, since, _, errors = self.fetch_into_copy(self.since)
        except OSError as error:
            self.call_from_thread(self.set_status, f"ntfy unreachable: {error}")
            return
        reachable = tailscale_reachable(self.config.tailscale_host)
        status = self.describe(errors, f"updated {datetime.now():%H:%M:%S} · r = ask the Yún for a fresh report")
        self.call_from_thread(self.apply_update, latest, since, reachable, status)

    def action_report(self) -> None:
        self.request_report()

    @work(thread=True, exclusive=True, group="report")
    def request_report(self) -> None:
        requested_at = int(time.time())
        try:
            publish(self.config.cmd_topic, "report")
        except OSError as error:
            self.call_from_thread(self.set_status, f"couldn't send the report request: {error}")
            return
        self.call_from_thread(self.set_status, "asked the Yún for a fresh report (usually under 40 s)…")
        deadline = time.monotonic() + REPORT_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(REPORT_POLL_S)
            try:
                # since=<unix time> returns only what arrived after the request.
                latest, _, sources, errors = self.fetch_into_copy(str(requested_at))
            except OSError as error:
                self.call_from_thread(self.set_status, f"ntfy unreachable while waiting: {error}")
                continue
            if "yun" in sources:
                reachable = tailscale_reachable(self.config.tailscale_host)
                waited = int(time.time()) - requested_at
                status = self.describe(errors, f"fresh report after {waited}s")
                self.call_from_thread(self.apply_update, latest, self.since, reachable, status)
                return
        self.call_from_thread(self.set_status, f"no fresh report after {REPORT_WAIT_S}s: is the Yún online?")


def main() -> None:
    HomelabApp(load_config()).run()
