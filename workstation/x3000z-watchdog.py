#!/usr/bin/env python3
"""Reboot the D-Link X3000Z when its wireless backhaul to the ZTE gateway drops.

Runs on a machine wired to the X3000Z: when the backhaul is down the ZTE
gateway becomes unreachable, but the X3000Z itself still answers on the LAN.

Usage:
  x3000z-watchdog              watch loop (systemd service)
  x3000z-watchdog --test       log in + one read-only call, no reboot
  x3000z-watchdog --reboot-now log in and reboot immediately
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ROUTER_UBUS_URL = "http://192.168.0.35/ubus"
GATEWAY_IP = "192.168.0.1"
NULL_SESSION = "0" * 32
AES_IV = b"poiewjhw49q35j4n"  # fixed IV hardcoded in the router's web UI (dLoad.js)
CHECK_INTERVAL_S = 60
FAILURES_BEFORE_REBOOT = 5
REBOOT_COOLDOWN_S = 15 * 60
HTTP_TIMEOUT_S = 10


class UbusError(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def gateway_reachable() -> bool:
    ping = subprocess.run(["ping", "-c", "3", "-W", "2", "-q", GATEWAY_IP], capture_output=True)
    return ping.returncode == 0


def ubus_call(session: str, obj: str, method: str, args: dict) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "call",
                       "params": [session, obj, method, args]}).encode()
    request = urllib.request.Request(ROUTER_UBUS_URL, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
        reply = json.load(response)
    result = reply.get("result")
    if not result or result[0] != 0:
        raise UbusError(f"{obj}.{method} failed: {reply}")
    return result[1] if len(result) > 1 else {}


def encrypt_password(password: str, rand_key: str) -> str:
    key_index, key_hex = rand_key[:32], rand_key[32:64]
    ciphertext = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-K", key_hex, "-iv", AES_IV.hex()],
        input=password.encode(), capture_output=True, check=True,
    ).stdout
    return key_index + ciphertext.hex()


def login(username: str, password: str) -> str:
    rand_key = ubus_call(NULL_SESSION, "routerd", "get_rand_key", {})["rand_key"]
    reply = ubus_call(NULL_SESSION, "routerd", "login",
                      {"username": username, "password": encrypt_password(password, rand_key)})
    session = reply.get("ubus_rpc_session")
    if not session:
        raise UbusError(f"login returned no session: {reply}")
    return session


def reboot_router(username: str, password: str) -> None:
    session = login(username, password)
    try:
        ubus_call(session, "system", "reboot", {})
    except OSError as error:  # the router can drop the connection as it goes down
        log(f"reboot sent, router stopped answering ({error})")
        return
    log("reboot accepted")


def watch(username: str, password: str) -> None:
    failures = 0
    down_since: float | None = None
    last_reboot = float("-inf")
    log(f"watching {GATEWAY_IP} every {CHECK_INTERVAL_S}s, reboot after {FAILURES_BEFORE_REBOOT} failures")
    while True:
        if gateway_reachable():
            if down_since is not None:
                log(f"gateway back after {(time.monotonic() - down_since) / 60:.1f} min")
            failures, down_since = 0, None
        else:
            failures += 1
            if down_since is None:
                down_since = time.monotonic()
                log("gateway unreachable, backhaul likely down")
            cooled_down = time.monotonic() - last_reboot >= REBOOT_COOLDOWN_S
            if failures >= FAILURES_BEFORE_REBOOT and cooled_down:
                log(f"down for {failures} checks, rebooting X3000Z")
                last_reboot = time.monotonic()
                try:
                    reboot_router(username, password)
                except Exception as error:  # a crash would restart the service and reset the cooldown
                    log(f"reboot failed: {type(error).__name__}: {error}")
        time.sleep(CHECK_INTERVAL_S)


def main() -> int:
    username = os.environ.get("X3000Z_USER", "Admin")
    password = os.environ.get("X3000Z_PASSWORD")
    if not password:
        log("X3000Z_PASSWORD is not set")
        return 1
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode == "--test":
        session = login(username, password)
        lan = ubus_call(session, "routerd", "lan_config_get", {})
        log(f"login ok, router LAN IP {lan.get('ipaddr')}")
    elif mode == "--reboot-now":
        reboot_router(username, password)
    elif mode is None:
        watch(username, password)
    else:
        log(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
