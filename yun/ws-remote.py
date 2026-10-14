#!/usr/bin/python
"""Remote power control for the workstation over ntfy.sh, via the BMC's Redfish API.

Polls a private ntfy topic for commands and answers on a second topic. Outbound
HTTPS only, through the static curl on the SD card: the Yun's own OpenSSL is too
old for both ntfy.sh and the BMC.

Commands: status | on | off (graceful) | forceoff
"""
import json
import subprocess
import sys
import time

CURL = ["/mnt/sda1/bin/curl", "-s", "-m", "20", "--cacert", "/mnt/sda1/bin/cacert.pem"]
CONFIG_PATH = "/etc/ws-remote.conf"
BMC_NETRC = "/etc/bmc.netrc"
BMC_SYSTEM_URL = "https://192.168.0.33/redfish/v1/Systems/Self"
BMC_OK_CODES = ("200", "202", "204")
RESET_TYPES = {"on": "On", "off": "GracefulShutdown", "forceoff": "ForceOff"}
NTFY_URL = "https://ntfy.sh"
POLL_INTERVAL_S = 30
FIRST_POLL_LOOKBACK = "%ds" % (POLL_INTERVAL_S + 15)
LOG_PATH = "/mnt/sda1/ws-remote.log"


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
        return "unknown command %r (use: status, on, off, forceoff)" % command
    code = bmc_reset(reset_type, pubkey_pin)
    if code not in BMC_OK_CODES:
        return "%s failed: BMC returned HTTP %s" % (command, code)
    return "%s sent (%s)" % (command, reset_type)


def fetch_messages(topic, since):
    out = curl(["%s/%s/json?poll=1&since=%s" % (NTFY_URL, topic, since)])
    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [event for event in events if event.get("event") == "message"]


def reply(topic, text):
    curl(["-o", "/dev/null", "--data-binary", "@-", "%s/%s" % (NTFY_URL, topic)], text)


def serve(cmd_topic, reply_topic, pubkey_pin):
    since = FIRST_POLL_LOOKBACK
    seen_ids = set()
    log("polling ntfy every %ds" % POLL_INTERVAL_S)
    while True:
        try:
            for message in fetch_messages(cmd_topic, since):
                since = message["id"]
                if message["id"] in seen_ids:
                    continue
                seen_ids.add(message["id"])
                command = message.get("message", "").strip().lower()
                log("command %r" % command)
                answer = run_command(command, pubkey_pin)
                log(answer)
                reply(reply_topic, answer)
        except (CurlError, ValueError, KeyError) as error:
            log("poll failed: %s" % error)
        time.sleep(POLL_INTERVAL_S)


def main():
    config = read_config()
    pubkey_pin = config["BMC_PUBKEY_PIN"]
    if len(sys.argv) > 1:
        print(run_command(sys.argv[1], pubkey_pin))
        return 0
    serve(config["CMD_TOPIC"], config["REPLY_TOPIC"], pubkey_pin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
