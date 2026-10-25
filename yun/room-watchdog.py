#!/usr/bin/python
"""Recover the room's uplink when the router's Wi-Fi link to the ZTE gateway drops.

Python 2.7 for the Arduino Yun (Linino / OpenWrt 12.09, no python3).
The Yun is wired into the room router, a Xiaomi AX3000T on OpenWrt, whose Wi-Fi
client link to the ZTE carries all of the room's traffic. When the ZTE stops
answering, the watchdog restarts the router's uplink radio, and reboots the
router if that didn't help. If the Yun itself has lost its default route, it
brings its own wan back up instead and leaves the router alone.
While the link is up it also sends a heartbeat to
healthchecks.io (which alerts when the room goes quiet) and logs ping loss/RTT
and the uplink signal.

Usage:
  room-watchdog.py                 watch loop (started by /etc/init.d/room-watchdog)
  room-watchdog.py --test          log in and read the uplink, no changes
  room-watchdog.py --restart-wifi  restart the router's uplink radio now
  room-watchdog.py --reboot-now    reboot the router now
"""
import collections
import httplib
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib2

ROUTER_UBUS_URL = "http://192.168.69.1/ubus"
GATEWAY_IP = "192.168.0.1"  # the ZTE, reached through the router's uplink
NULL_SESSION = "0" * 32
# Sessions expire on their own, so the router login needs no logout permission.
SESSION_TIMEOUT_S = 60
UPLINK_DEVICE = "phy1-sta0"  # the router's Wi-Fi client interface to the ZTE
UPLINK_RADIO = "radio1"
WIFI_COMMAND = "/sbin/wifi"
WIFI_RESTART_PAUSE_S = 5
CHECK_INTERVAL_S = 60
FAILURES_BEFORE_RECOVERY = 5
RECOVERY_COOLDOWN_S = 10 * 60  # a router reboot takes ~3.5 min to bring the uplink back
HEARTBEAT_EVERY_CHECKS = 5
UPLINK_EVERY_CHECKS = 10
HTTP_TIMEOUT_S = 10
LOG_PATH = "/mnt/sda1/room-watchdog.log"  # SD card, survives reboots unlike logread
SIGNAL_LOG_PATH = "/mnt/sda1/uplink-signal.csv"  # epoch,signal_dbm (backhaul-rssi.csv is the X3000Z era)
LINK_LOG_PATH = "/mnt/sda1/backhaul.csv"  # epoch,loss_pct,avg_ms per check
PID_PATH = "/var/run/room-watchdog.pid"
ROUTE_TABLE_PATH = "/proc/net/route"
DEFAULT_DESTINATION = "00000000"  # 0.0.0.0 in /proc/net/route's hex notation
YUN_WAN_INTERFACE = "wan"
NETWORK_ERRORS = (IOError, httplib.HTTPException)  # URLError and socket.timeout are IOErrors
# The Yun's own OpenSSL is too old for healthchecks.io and ntfy.sh, so HTTPS goes through the static curl.
CURL = ["/mnt/sda1/bin/curl", "-s", "-m", "20", "--retry", "2", "--cacert", "/mnt/sda1/bin/cacert.pem"]
NTFY_URL = "https://ntfy.sh"
KBIT_PER_MBIT = 1000.0
# busybox ping summary: "3 packets transmitted, 3 packets received, 0% packet loss"
# and "round-trip min/avg/max = 1.960/2.663/4.011 ms" (absent when nothing came back)
LOSS_PATTERN = re.compile(r"(\d+)% packet loss")
AVG_RTT_PATTERN = re.compile(r"min/avg/max = [\d.]+/([\d.]+)/")
TOTAL_LOSS = 100

Config = collections.namedtuple("Config", "username password heartbeat_url alert_topic")
PingResult = collections.namedtuple("PingResult", "loss_pct avg_ms")  # avg_ms is "" with no reply


class RouterError(Exception):
    pass


class CurlError(Exception):
    pass


def log(message):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), message)
    print(line)
    subprocess.call(["logger", "-t", "room-watchdog", message])
    try:
        with open(LOG_PATH, "a") as log_file:
            log_file.write(line + "\n")
    except IOError as error:
        subprocess.call(["logger", "-t", "room-watchdog", "cannot write %s: %s" % (LOG_PATH, error)])


def parse_ping(output):
    loss = LOSS_PATTERN.search(output)
    avg_rtt = AVG_RTT_PATTERN.search(output)
    # no summary at all (e.g. "Network is unreachable") counts as total loss
    return PingResult(int(loss.group(1)) if loss else TOTAL_LOSS, avg_rtt.group(1) if avg_rtt else "")


def ping_gateway():
    ping = subprocess.Popen(["ping", "-c", "3", "-W", "2", "-q", GATEWAY_IP],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return parse_ping(ping.communicate()[0])


def has_default_route():
    with open(ROUTE_TABLE_PATH) as routes:
        entries = routes.readlines()[1:]  # the first line is the column header
    return any(entry.split()[1] == DEFAULT_DESTINATION for entry in entries)


def restore_default_route():
    # This netifd drops the default route when udhcpc renews its lease, and only
    # bringing the interface up again restores it.
    log("no default route on the Yun, running ifup %s" % YUN_WAN_INTERFACE)
    code = subprocess.call(["ifup", YUN_WAN_INTERFACE])
    if code != 0:
        log("ifup %s exited %d" % (YUN_WAN_INTERFACE, code))


def record_link(result):
    with open(LINK_LOG_PATH, "a") as link_log:
        link_log.write("%d,%d,%s\n" % (time.time(), result.loss_pct, result.avg_ms))


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


def login(username, password):
    reply = ubus_call(NULL_SESSION, "session", "login",
                      {"username": username, "password": password, "timeout": SESSION_TIMEOUT_S})
    session = reply.get("ubus_rpc_session")
    if not session:
        raise RouterError("login returned no session: %s" % reply)
    return session


def run_wifi(session, action):
    # network.wireless would be the direct call, but it rejects the ubus_rpc_session
    # argument the web API adds to every call, so this runs /sbin/wifi like LuCI does.
    result = ubus_call(session, "file", "exec", {"command": WIFI_COMMAND, "params": [action, UPLINK_RADIO]})
    if result.get("code") != 0:
        raise RouterError("wifi %s %s exited %s: %s" % (action, UPLINK_RADIO, result.get("code"),
                                                        result.get("stderr", "").strip()))


def restart_uplink_radio(username, password):
    session = login(username, password)
    run_wifi(session, "down")
    time.sleep(WIFI_RESTART_PAUSE_S)
    run_wifi(session, "up")
    log("Wi-Fi restart sent")


def reboot_router(username, password):
    session = login(username, password)
    try:
        ubus_call(session, "system", "reboot", {})
    except NETWORK_ERRORS as error:  # the router can drop the connection as it goes down
        log("reboot sent, router stopped answering (%s)" % error)
        return
    log("reboot accepted")


def read_uplink(username, password):
    return ubus_call(login(username, password), "iwinfo", "info", {"device": UPLINK_DEVICE})


def describe_uplink(uplink):
    if "signal" not in uplink:
        return "uplink: not associated"
    return "uplink: %s ch%s, signal %s dBm, %.0f Mbit/s" % (
        uplink.get("ssid"), uplink.get("channel"), uplink["signal"],
        uplink.get("bitrate", 0) / KBIT_PER_MBIT)


def record_signal(signal_dbm):
    with open(SIGNAL_LOG_PATH, "a") as signal_log:
        signal_log.write("%d,%s\n" % (time.time(), signal_dbm))


def check_uplink(config):
    uplink = read_uplink(config.username, config.password)
    log(describe_uplink(uplink))
    if "signal" in uplink:
        record_signal(uplink["signal"])


def format_duration(minutes):
    if minutes >= 90:
        return "%.1f h" % (minutes / 60)
    return "%.0f min" % minutes


def notify_recovery(config, minutes, actions_sent, failed_actions):
    if actions_sent:
        cause = "after the watchdog's %s" % " and ".join(actions_sent)
    else:
        cause = "without watchdog action (on its own, or by hand); %d recovery attempts failed" % failed_actions
    publish_alert(config.alert_topic, "Room network back",
                  "Uplink was down for %s. Recovered %s." % (format_duration(minutes), cause), "default")


def is_due(checks, every):
    return (checks - 1) % every == 0  # the first check counts, so a restart reports straight away


def run_periodic_checks(config, checks):
    if config.heartbeat_url and is_due(checks, HEARTBEAT_EVERY_CHECKS):
        try:
            send_heartbeat(config.heartbeat_url)
        except CurlError as error:
            log("heartbeat failed: %s" % error)
    if is_due(checks, UPLINK_EVERY_CHECKS):
        try:
            check_uplink(config)
        except NETWORK_ERRORS + (RouterError, ValueError) as error:
            log("uplink check failed: %s" % error)


def attempt_recovery(config, failures, attempts):
    """Restart the uplink radio on the first attempt of an outage, reboot the router after that.

    Returns the name of the action sent; raises if the router refused or didn't answer.
    """
    if attempts == 0:
        log("down for %d checks, restarting the router's Wi-Fi" % failures)
        restart_uplink_radio(config.username, config.password)
        return "Wi-Fi restart"
    log("down for %d checks, rebooting the router" % failures)
    reboot_router(config.username, config.password)
    return "reboot"


def watch(config):
    checks = 0
    failures = 0
    down_since = None
    last_attempt = None
    attempts = 0
    actions_sent = []
    log("watching %s every %ds, recovery after %d failures"
        % (GATEWAY_IP, CHECK_INTERVAL_S, FAILURES_BEFORE_RECOVERY))
    if not config.heartbeat_url or not config.alert_topic:
        log("HC_PING_URL or ALERT_TOPIC not set: heartbeat and alerts are partly off")
    while True:
        checks += 1
        result = ping_gateway()
        try:
            record_link(result)
        except IOError as error:
            log("cannot write %s: %s" % (LINK_LOG_PATH, error))
        if result.loss_pct < TOTAL_LOSS:
            if down_since is not None:
                minutes = (time.time() - down_since) / 60
                log("gateway back after %.1f min" % minutes)
                if failures >= FAILURES_BEFORE_RECOVERY and config.alert_topic:
                    try:
                        notify_recovery(config, minutes, actions_sent, attempts - len(actions_sent))
                    except CurlError as error:
                        log("recovery notice failed: %s" % error)
            failures, down_since, attempts, actions_sent = 0, None, 0, []
            run_periodic_checks(config, checks)
        elif not has_default_route():
            # the fault is on the Yun's side, so it doesn't count against the router
            restore_default_route()
        else:
            failures += 1
            if down_since is None:
                down_since = time.time()
                log("gateway unreachable, uplink likely down")
            cooled_down = last_attempt is None or time.time() - last_attempt >= RECOVERY_COOLDOWN_S
            if failures >= FAILURES_BEFORE_RECOVERY and cooled_down:
                last_attempt = time.time()
                try:
                    actions_sent.append(attempt_recovery(config, failures, attempts))
                except NETWORK_ERRORS + (RouterError, ValueError) as error:
                    log("recovery failed: %s" % error)
                attempts += 1
        time.sleep(CHECK_INTERVAL_S)


def stop_on_sigterm(signum, frame):
    raise SystemExit(0)


def run_watch(config):
    # The process may inherit an ignored SIGTERM from whoever started it; this makes
    # `/etc/init.d/room-watchdog stop` work regardless.
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    with open(PID_PATH, "w") as pid_file:
        pid_file.write("%d\n" % os.getpid())
    try:
        watch(config)
    finally:
        log("watchdog stopping")
        os.remove(PID_PATH)


def main():
    config = Config(username=os.environ.get("ROUTER_USER", "yun"),
                    password=os.environ.get("ROUTER_PASSWORD"),
                    heartbeat_url=os.environ.get("HC_PING_URL"),
                    alert_topic=os.environ.get("ALERT_TOPIC"))
    if not config.password:
        log("ROUTER_PASSWORD is not set")
        return 1
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode == "--test":
        print(describe_uplink(read_uplink(config.username, config.password)))
    elif mode == "--restart-wifi":
        restart_uplink_radio(config.username, config.password)
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
