#!/usr/bin/python
"""Reboot the D-Link X3000Z when its wireless backhaul to the ZTE gateway drops.

Python 2.7 port for the Arduino Yun (Linino / OpenWrt 12.09, no python3).
The Yun is wired straight into the X3000Z: when the backhaul is down the ZTE
gateway becomes unreachable, but the X3000Z itself still answers on the LAN.

Usage:
  x3000z-watchdog.py              watch loop (started by /etc/init.d/x3000z-watchdog)
  x3000z-watchdog.py --test       log in + one read-only call, no reboot
  x3000z-watchdog.py --reboot-now log in and reboot immediately
"""
import binascii
import httplib
import json
import os
import subprocess
import sys
import time
import urllib2

ROUTER_UBUS_URL = "http://192.168.0.35/ubus"
GATEWAY_IP = "192.168.0.1"
NULL_SESSION = "0" * 32
AES_IV = "poiewjhw49q35j4n"  # fixed IV hardcoded in the router's web UI (dLoad.js)
CHECK_INTERVAL_S = 60
FAILURES_BEFORE_REBOOT = 5
REBOOT_COOLDOWN_S = 15 * 60
HTTP_TIMEOUT_S = 10
LOG_PATH = "/mnt/sda1/x3000z-watchdog.log"  # SD card, survives reboots unlike logread
NETWORK_ERRORS = (IOError, httplib.HTTPException)  # URLError and socket.timeout are IOErrors


class RouterError(Exception):
    pass


def log(message):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), message)
    print(line)
    subprocess.call(["logger", "-t", "x3000z-watchdog", message])
    try:
        with open(LOG_PATH, "a") as log_file:
            log_file.write(line + "\n")
    except IOError as error:
        subprocess.call(["logger", "-t", "x3000z-watchdog", "cannot write %s: %s" % (LOG_PATH, error)])


def gateway_reachable():
    with open(os.devnull, "w") as devnull:
        ping = ["ping", "-c", "3", "-W", "2", "-q", GATEWAY_IP]
        return subprocess.call(ping, stdout=devnull, stderr=devnull) == 0


def ubus_call(session, obj, method, args):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "call",
                       "params": [session, obj, method, args]})
    request = urllib2.Request(ROUTER_UBUS_URL, body, {"Content-Type": "application/json"})
    response = urllib2.urlopen(request, timeout=HTTP_TIMEOUT_S)
    try:
        reply = json.load(response)
    finally:
        response.close()
    result = reply.get("result")
    if not result or result[0] != 0:
        raise RouterError("%s.%s failed: %s" % (obj, method, reply))
    return result[1] if len(result) > 1 else {}


def encrypt_password(password, rand_key):
    key_index, key_hex = rand_key[:32], rand_key[32:64]
    openssl = subprocess.Popen(
        ["openssl", "enc", "-aes-128-cbc", "-K", key_hex, "-iv", binascii.hexlify(AES_IV)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ciphertext, stderr = openssl.communicate(password)
    if openssl.returncode != 0:
        raise RouterError("openssl failed: %s" % stderr.strip())
    return key_index + binascii.hexlify(ciphertext)


def login(username, password):
    rand_key = ubus_call(NULL_SESSION, "routerd", "get_rand_key", {})["rand_key"]
    reply = ubus_call(NULL_SESSION, "routerd", "login",
                      {"username": username, "password": encrypt_password(password, rand_key)})
    session = reply.get("ubus_rpc_session")
    if not session:
        raise RouterError("login returned no session: %s" % reply)
    return session


def reboot_router(username, password):
    session = login(username, password)
    try:
        ubus_call(session, "system", "reboot", {})
    except NETWORK_ERRORS as error:  # the router can drop the connection as it goes down
        log("reboot sent, router stopped answering (%s)" % error)
        return
    log("reboot accepted")


def watch(username, password):
    failures = 0
    down_since = None
    last_reboot = None
    log("watching %s every %ds, reboot after %d failures"
        % (GATEWAY_IP, CHECK_INTERVAL_S, FAILURES_BEFORE_REBOOT))
    while True:
        if gateway_reachable():
            if down_since is not None:
                log("gateway back after %.1f min" % ((time.time() - down_since) / 60))
            failures, down_since = 0, None
        else:
            failures += 1
            if down_since is None:
                down_since = time.time()
                log("gateway unreachable, backhaul likely down")
            cooled_down = last_reboot is None or time.time() - last_reboot >= REBOOT_COOLDOWN_S
            if failures >= FAILURES_BEFORE_REBOOT and cooled_down:
                log("down for %d checks, rebooting X3000Z" % failures)
                last_reboot = time.time()
                try:
                    reboot_router(username, password)
                except NETWORK_ERRORS + (RouterError, ValueError, KeyError) as error:
                    log("reboot failed: %s" % error)
        time.sleep(CHECK_INTERVAL_S)


def main():
    username = os.environ.get("X3000Z_USER", "Admin")
    password = os.environ.get("X3000Z_PASSWORD")
    if not password:
        log("X3000Z_PASSWORD is not set")
        return 1
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode == "--test":
        session = login(username, password)
        lan = ubus_call(session, "routerd", "lan_config_get", {})
        log("login ok, router LAN IP %s" % lan.get("ipaddr"))
    elif mode == "--reboot-now":
        reboot_router(username, password)
    elif mode is None:
        watch(username, password)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
