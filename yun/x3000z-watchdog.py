#!/usr/bin/python
"""Reboot the D-Link X3000Z when its wireless backhaul to the ZTE gateway drops.

Python 2.7 port for the Arduino Yun (Linino / OpenWrt 12.09, no python3).
The Yun is wired straight into the X3000Z. While the backhaul is up it also
sends a heartbeat to healthchecks.io (which alerts when the room goes quiet),
logs the backhaul RSSI, and alerts when anything other than a mesh agent joins
the ZTE's backhaul network.

Usage:
  x3000z-watchdog.py              watch loop (started by /etc/init.d/x3000z-watchdog)
  x3000z-watchdog.py --test       log in + one read-only call, no reboot
  x3000z-watchdog.py --topology   print backhaul RSSI and unexpected backhaul stations
  x3000z-watchdog.py --reboot-now log in and reboot immediately
"""
import binascii
import collections
import httplib
import json
import os
import signal
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
HEARTBEAT_EVERY_CHECKS = 5
TOPOLOGY_EVERY_CHECKS = 10
HTTP_TIMEOUT_S = 10
LOG_PATH = "/mnt/sda1/x3000z-watchdog.log"  # SD card, survives reboots unlike logread
RSSI_LOG_PATH = "/mnt/sda1/backhaul-rssi.csv"
PID_PATH = "/var/run/x3000z-watchdog.pid"
NETWORK_ERRORS = (IOError, httplib.HTTPException)  # URLError and socket.timeout are IOErrors
# The Yun's own OpenSSL is too old for healthchecks.io and ntfy.sh, so HTTPS goes through the static curl.
CURL = ["/mnt/sda1/bin/curl", "-s", "-m", "20", "--retry", "2", "--cacert", "/mnt/sda1/bin/cacert.pem"]
NTFY_URL = "https://ntfy.sh"
CONTROLLER_ROLE = "01"  # EasyMesh "Device role" values in easymesh_topology
AGENT_ROLE = "02"
ETHERNET_BACKHAUL = "Ethernet"
UNMEASURED_RSSI = "0"  # what the X3000Z reports until link metrics come in after a (re)pair
# The first easymesh_topology after a quiet spell often lists only the X3000Z itself;
# the full table (with the ZTE and its backhaul stations) is there ~20 s later.
TOPOLOGY_SETTLE_S = 20

Config = collections.namedtuple("Config", "username password heartbeat_url alert_topic")


class RouterError(Exception):
    pass


class CurlError(Exception):
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


def curl(args, data=None):
    process = subprocess.Popen(CURL + args, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate(data)
    if process.returncode != 0:
        raise CurlError("curl exit %d: %s" % (process.returncode, err.strip()))
    return out


def send_heartbeat(url):
    curl(["-o", "/dev/null", url])


def publish_alert(topic, title, message, priority):
    curl(["-o", "/dev/null", "-H", "Title: " + title, "-H", "Priority: " + priority,
          "--data-binary", "@-", "%s/%s" % (NTFY_URL, topic)], message)


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


def logout(session):
    # The router caps concurrent web sessions; one left open per check would soon lock out every login.
    try:
        ubus_call(session, "session", "destroy", {})
    except NETWORK_ERRORS + (RouterError, ValueError) as error:
        log("logout failed: %s" % error)


def reboot_router(username, password):
    session = login(username, password)
    try:
        ubus_call(session, "system", "reboot", {})
    except NETWORK_ERRORS as error:  # the router can drop the connection as it goes down
        log("reboot sent, router stopped answering (%s)" % error)
        return
    log("reboot accepted")


def read_topology(username, password):
    session = login(username, password)
    try:
        topology = ubus_call(session, "routerd", "easymesh_topology", {})["topology information"]
        if not has_controller(topology):
            time.sleep(TOPOLOGY_SETTLE_S)
            topology = ubus_call(session, "routerd", "easymesh_topology", {})["topology information"]
        return topology
    finally:
        logout(session)


def has_controller(topology):
    return any(device.get("Device role") == CONTROLLER_ROLE for device in topology)


def backhaul_rssi(topology):
    for device in topology:
        if device.get("Device role") != AGENT_ROLE:
            continue
        for link in device.get("BH Info", []):
            if link.get("Backhaul Medium Type") != ETHERNET_BACKHAUL:
                return link.get("RSSI")
    return None


def agent_backhaul_macs(topology):
    return set(link.get("connected iface addr", "").lower()
               for device in topology if device.get("Device role") == AGENT_ROLE
               for link in device.get("BH Info", []))


def backhaul_intruders(topology):
    """MACs on the controller's backhaul BSS that are not a mesh agent's backhaul radio."""
    agents = agent_backhaul_macs(topology)
    intruders = set()
    for device in topology:
        if device.get("Device role") != CONTROLLER_ROLE:
            continue
        backhaul_bssids = set(iface.get("MAC address", "").lower()
                              for iface in device.get("BH Interface(AP)", []))
        for radio in device.get("Radio Info", []):
            for bss in radio.get("BSSINFO", []):
                if bss.get("BSSID", "").lower() not in backhaul_bssids:
                    continue
                for station in bss.get("connected sta info", []):
                    mac = station.get("STA MAC address", "").lower()
                    if mac not in agents:
                        intruders.add(mac)
    return intruders


def record_rssi(rssi):
    with open(RSSI_LOG_PATH, "a") as rssi_log:
        rssi_log.write("%d,%s\n" % (time.time(), rssi))


def check_topology(config, alerted_intruders):
    topology = read_topology(config.username, config.password)
    rssi = backhaul_rssi(topology)
    if rssi not in (None, UNMEASURED_RSSI):
        record_rssi(rssi)
    intruders = backhaul_intruders(topology)
    log("topology: rssi %s, %d mesh agent(s), %d unexpected backhaul station(s)%s"
        % (rssi, len(agent_backhaul_macs(topology)), len(intruders),
           "" if has_controller(topology) else " (incomplete: no controller entry)"))
    for mac in intruders - alerted_intruders:
        log("unexpected station %s on the ZTE backhaul network" % mac)
        if config.alert_topic:
            publish_alert(config.alert_topic, "Unknown device on the mesh backhaul",
                          "%s joined the ZTE's hidden backhaul network (passphrase 'password'). "
                          "It is not the X3000Z." % mac, "urgent")
        alerted_intruders.add(mac)


def format_duration(minutes):
    if minutes >= 90:
        return "%.1f h" % (minutes / 60)
    return "%.0f min" % minutes


def notify_recovery(config, minutes, reboot_sent, failed_reboots):
    if reboot_sent:
        cause = "after a watchdog reboot"
    else:
        cause = "without a watchdog reboot (on its own, or by hand); %d reboot attempts failed" % failed_reboots
    publish_alert(config.alert_topic, "Room network back",
                  "Backhaul was down for %s. Recovered %s." % (format_duration(minutes), cause), "default")


def is_due(checks, every):
    return (checks - 1) % every == 0  # the first check counts, so a restart reports straight away


def run_periodic_checks(config, checks, alerted_intruders):
    if config.heartbeat_url and is_due(checks, HEARTBEAT_EVERY_CHECKS):
        try:
            send_heartbeat(config.heartbeat_url)
        except CurlError as error:
            log("heartbeat failed: %s" % error)
    if is_due(checks, TOPOLOGY_EVERY_CHECKS):
        try:
            check_topology(config, alerted_intruders)
        except NETWORK_ERRORS + (RouterError, CurlError, ValueError, KeyError) as error:
            log("topology check failed: %s" % error)


def watch(config):
    checks = 0
    failures = 0
    down_since = None
    last_reboot = None
    reboot_sent = False
    failed_reboots = 0
    alerted_intruders = set()
    log("watching %s every %ds, reboot after %d failures"
        % (GATEWAY_IP, CHECK_INTERVAL_S, FAILURES_BEFORE_REBOOT))
    if not config.heartbeat_url or not config.alert_topic:
        log("HC_PING_URL or ALERT_TOPIC not set: heartbeat and alerts are partly off")
    while True:
        checks += 1
        if gateway_reachable():
            if down_since is not None:
                minutes = (time.time() - down_since) / 60
                log("gateway back after %.1f min" % minutes)
                if failures >= FAILURES_BEFORE_REBOOT and config.alert_topic:
                    try:
                        notify_recovery(config, minutes, reboot_sent, failed_reboots)
                    except CurlError as error:
                        log("recovery notice failed: %s" % error)
            failures, down_since, reboot_sent, failed_reboots = 0, None, False, 0
            run_periodic_checks(config, checks, alerted_intruders)
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
                    reboot_router(config.username, config.password)
                    reboot_sent = True
                except NETWORK_ERRORS + (RouterError, ValueError, KeyError) as error:
                    failed_reboots += 1
                    log("reboot failed: %s" % error)
        time.sleep(CHECK_INTERVAL_S)


def stop_on_sigterm(signum, frame):
    raise SystemExit(0)


def run_watch(config):
    # The process may inherit an ignored SIGTERM from whoever started it; this makes
    # `/etc/init.d/x3000z-watchdog stop` work regardless.
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    with open(PID_PATH, "w") as pid_file:
        pid_file.write("%d\n" % os.getpid())
    try:
        watch(config)
    finally:
        log("watchdog stopping")
        os.remove(PID_PATH)


def main():
    config = Config(username=os.environ.get("X3000Z_USER", "Admin"),
                    password=os.environ.get("X3000Z_PASSWORD"),
                    heartbeat_url=os.environ.get("HC_PING_URL"),
                    alert_topic=os.environ.get("ALERT_TOPIC"))
    if not config.password:
        log("X3000Z_PASSWORD is not set")
        return 1
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode == "--test":
        session = login(config.username, config.password)
        try:
            lan = ubus_call(session, "routerd", "lan_config_get", {})
        finally:
            logout(session)
        log("login ok, router LAN IP %s" % lan.get("ipaddr"))
    elif mode == "--topology":
        topology = read_topology(config.username, config.password)
        print("backhaul RSSI: %s" % backhaul_rssi(topology))
        print("mesh agents: %s" % ", ".join(sorted(agent_backhaul_macs(topology))))
        print("unexpected backhaul stations: %s" % (", ".join(sorted(backhaul_intruders(topology))) or "none"))
    elif mode == "--reboot-now":
        reboot_router(config.username, config.password)
    elif mode is None:
        run_watch(config)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
