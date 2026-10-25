#!/usr/bin/python
"""Remote power control for the workstation over ntfy.sh, via the BMC's Redfish API.

Polls a private ntfy topic for commands and answers on a second topic. Every 30
minutes, and on a `report` command, it also publishes a status snapshot to a
third topic for the laptop dashboard. Outbound HTTPS only, through the static
curl on the SD card: the Yun's own OpenSSL is too old for both ntfy.sh and the BMC.

Commands: status | on | off (graceful) | forceoff | report
Local use: ws-remote.py <command>   or   ws-remote.py snapshot  (print, don't publish)
"""
import json
import os
import subprocess
import sys
import time

CURL = ["/mnt/sda1/bin/curl", "-s", "-m", "20", "--cacert", "/mnt/sda1/bin/cacert.pem"]
CONFIG_PATH = "/etc/ws-remote.conf"
BMC_NETRC = "/etc/bmc.netrc"
BMC_SYSTEM_URL = "https://192.168.69.33/redfish/v1/Systems/Self"
BMC_THERMAL_URL = "https://192.168.69.33/redfish/v1/Chassis/Self/Thermal"
BMC_OK_CODES = ("200", "202", "204")
RESET_TYPES = {"on": "On", "off": "GracefulShutdown", "forceoff": "ForceOff"}
NTFY_URL = "https://ntfy.sh"
NTFY_MAX_MESSAGE_BYTES = 4096  # bigger messages become attachments
POLL_INTERVAL_S = 30
FIRST_POLL_LOOKBACK = "%ds" % (POLL_INTERVAL_S + 15)
STATUS_INTERVAL_S = 30 * 60  # ntfy.sh allows 250 messages/day per IP, shared with the workstation
LOG_PATH = "/mnt/sda1/ws-remote.log"
WATCHDOG_LOG_PATH = "/mnt/sda1/room-watchdog.log"
WATCHDOG_SCRIPT = "room-watchdog.py"
RECENT_EVENTS = 8


class CurlError(Exception):
    pass


def log(message):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), message)
    print(line)
    subprocess.call(["logger", "-t", "ws-remote", message])
    try:
        with open(LOG_PATH, "a") as log_file:
            log_file.write(line + "\n")
    except IOError as error:
        subprocess.call(["logger", "-t", "ws-remote", "cannot write %s: %s" % (LOG_PATH, error)])


def read_config():
    config = {}
    with open(CONFIG_PATH) as config_file:
        for line in config_file:
            if "=" in line:
                key, value = line.strip().split("=", 1)
                config[key] = value
    return config


def curl(args, data=None):
    process = subprocess.Popen(CURL + args, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate(data)
    if process.returncode != 0:
        raise CurlError("curl exit %d: %s" % (process.returncode, err.strip()))
    return out


def bmc_auth(pubkey_pin):
    # The BMC's certificate is self-signed for "ami.com", so normal verification
    # can't work; -k skips it and the pinned public key takes its place.
    return ["-k", "--pinnedpubkey", "sha256//" + pubkey_pin, "--netrc-file", BMC_NETRC]


def bmc_power_state(pubkey_pin):
    reply = json.loads(curl(bmc_auth(pubkey_pin) + [BMC_SYSTEM_URL]))
    return reply.get("PowerState", "unknown")


def bmc_sensors(pubkey_pin):
    thermal = json.loads(curl(bmc_auth(pubkey_pin) + [BMC_THERMAL_URL]))
    # Only sensors that are wired up report a reading; the rest are "Absent".
    temps = dict((t["Name"], t["ReadingCelsius"]) for t in thermal.get("Temperatures", [])
                 if t.get("ReadingCelsius") is not None)
    fans = dict((f["Name"], f["Reading"]) for f in thermal.get("Fans", [])
                if f.get("Reading") is not None)
    return temps, fans


def bmc_reset(reset_type, pubkey_pin):
    body = json.dumps({"ResetType": reset_type})
    return curl(bmc_auth(pubkey_pin) + ["-H", "Content-Type: application/json",
                "-o", "/dev/null", "-w", "%{http_code}", "--data-binary", "@-",
                BMC_SYSTEM_URL + "/Actions/ComputerSystem.Reset"], body)


def run_command(command, pubkey_pin):
    if command == "status":
        return "workstation is %s" % bmc_power_state(pubkey_pin)
    reset_type = RESET_TYPES.get(command)
    if reset_type is None:
        return "unknown command %r (use: status, on, off, forceoff, report)" % command
    code = bmc_reset(reset_type, pubkey_pin)
    if code not in BMC_OK_CODES:
        return "%s failed: BMC returned HTTP %s" % (command, code)
    return "%s sent (%s)" % (command, reset_type)


def read_uptime_s():
    with open("/proc/uptime") as uptime_file:
        return int(float(uptime_file.read().split()[0]))


def read_available_mem_kb():
    # Kernel 3.3 has no MemAvailable; free + buffers + page cache is the equivalent.
    fields = {}
    with open("/proc/meminfo") as meminfo:
        for line in meminfo:
            name, value = line.split(":", 1)
            fields[name] = int(value.split()[0])
    return fields["MemFree"] + fields["Buffers"] + fields["Cached"]


def read_load_1m():
    with open("/proc/loadavg") as loadavg:
        return float(loadavg.read().split()[0])


def process_running(script_name):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid) as cmdline:
                if script_name in cmdline.read():
                    return True
        except IOError:  # the process exited while we were looking
            continue
    return False


def recent_watchdog_events():
    try:
        with open(WATCHDOG_LOG_PATH) as watchdog_log:
            return [line.rstrip("\n") for line in watchdog_log.readlines()[-RECENT_EVENTS:]]
    except IOError as error:
        return ["cannot read watchdog log: %s" % error]


def build_snapshot(pubkey_pin):
    snapshot = {
        "src": "yun",
        "t": int(time.time()),
        "yun": {
            "uptime_s": read_uptime_s(),
            "mem_available_kb": read_available_mem_kb(),
            "load_1m": read_load_1m(),
            "watchdog_running": process_running(WATCHDOG_SCRIPT),
        },
        "events": recent_watchdog_events(),
    }
    try:
        snapshot["power"] = bmc_power_state(pubkey_pin)
        snapshot["temps"], snapshot["fans"] = bmc_sensors(pubkey_pin)
    except (CurlError, ValueError, KeyError) as error:
        snapshot["bmc_error"] = str(error)
    return snapshot


def encode_snapshot(snapshot):
    body = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
    while len(body) > NTFY_MAX_MESSAGE_BYTES and snapshot["events"]:
        snapshot["events"].pop(0)  # oldest first
        body = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
    return body


def publish(topic, text):
    curl(["-o", "/dev/null", "--data-binary", "@-", "%s/%s" % (NTFY_URL, topic)], text)


def publish_snapshot(config):
    publish(config["STATUS_TOPIC"], encode_snapshot(build_snapshot(config["BMC_PUBKEY_PIN"])))


def fetch_messages(topic, since):
    out = curl(["%s/%s/json?poll=1&since=%s" % (NTFY_URL, topic, since)])
    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [event for event in events if event.get("event") == "message"]


def handle_message(config, command):
    if command == "report":
        publish_snapshot(config)
        return "report published"
    return run_command(command, config["BMC_PUBKEY_PIN"])


def serve(config):
    since = FIRST_POLL_LOOKBACK
    seen_ids = set()
    next_snapshot_at = 0
    log("polling ntfy every %ds, status every %ds" % (POLL_INTERVAL_S, STATUS_INTERVAL_S))
    while True:
        try:
            for message in fetch_messages(config["CMD_TOPIC"], since):
                since = message["id"]
                if message["id"] in seen_ids:
                    continue
                seen_ids.add(message["id"])
                command = message.get("message", "").strip().lower()
                log("command %r" % command)
                answer = handle_message(config, command)
                log(answer)
                publish(config["REPLY_TOPIC"], answer)
            if time.time() >= next_snapshot_at:
                publish_snapshot(config)
                next_snapshot_at = time.time() + STATUS_INTERVAL_S
        except (CurlError, ValueError, KeyError) as error:
            log("poll failed: %s" % error)
        time.sleep(POLL_INTERVAL_S)


def main():
    config = read_config()
    if sys.argv[1:] == ["snapshot"]:
        print(encode_snapshot(build_snapshot(config["BMC_PUBKEY_PIN"])))
        return 0
    if len(sys.argv) > 1:
        print(run_command(sys.argv[1], config["BMC_PUBKEY_PIN"]))
        return 0
    serve(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
