"""Settings, read from the same private config file the `ws` command uses."""
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "ws-remote.conf"


@dataclass(frozen=True)
class Config:
    cmd_topic: str
    cmd_key: str
    status_topic: str
    tailscale_host: str


def load_config(path: Path = CONFIG_PATH) -> Config:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    missing = [key for key in ("CMD_TOPIC", "CMD_KEY", "STATUS_TOPIC", "WS_TAILSCALE_HOST") if key not in values]
    if missing:
        raise ValueError(f"{path} is missing {', '.join(missing)}")
    return Config(
        cmd_topic=values["CMD_TOPIC"],
        cmd_key=values["CMD_KEY"],
        status_topic=values["STATUS_TOPIC"],
        tailscale_host=values["WS_TAILSCALE_HOST"],
    )
