"""Minimal ntfy.sh client: poll a topic for cached messages, publish one."""
import json
import urllib.request
from dataclasses import dataclass

NTFY_URL = "https://ntfy.sh"
HTTP_TIMEOUT_S = 20


@dataclass(frozen=True)
class Message:
    id: str
    time: int
    text: str


def fetch_messages(topic: str, since: str) -> list[Message]:
    """`since` is a message id, a unix timestamp, or a duration such as "12h"."""
    url = f"{NTFY_URL}/{topic}/json?poll=1&since={since}"
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as response:
        lines = response.read().decode().splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    return [Message(e["id"], e["time"], e["message"]) for e in events if e.get("event") == "message"]


def publish(topic: str, text: str) -> None:
    request = urllib.request.Request(f"{NTFY_URL}/{topic}", data=text.encode(), method="POST")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S):
        pass
