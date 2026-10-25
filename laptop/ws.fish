function ws --description 'Workstation power via the Yún + BMC over ntfy: ws status|on|off|forceoff'
    set -l command $argv[1]
    if not contains -- "$command" status on off forceoff
        echo "usage: ws status|on|off|forceoff" >&2
        return 2
    end

    set -l conf ~/.config/ws-remote.conf
    set -l cmd_topic (string replace -f 'CMD_TOPIC=' '' <$conf)
    set -l reply_topic (string replace -f 'REPLY_TOPIC=' '' <$conf)
    set -l workstation_ts_ip (string replace -f 'WS_TAILSCALE_HOST=' '' <$conf)
    set -l reply_wait_s 90
    set -l boot_wait_s 360
    set -l poll_s 5
    set -l since (date +%s)

    # Same format as dashboard/src/homelab_dashboard/commands.py. Python reads the
    # key from the file itself so it never appears on a command line.
    set -l signed (python3 -c '
import hashlib, hmac, sys
command, timestamp, conf = sys.argv[1:]
key = next(line.split("=", 1)[1].strip() for line in open(conf) if line.startswith("CMD_KEY="))
payload = command + " " + timestamp
print(payload, hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest())
' $command $since $conf 2>/dev/null)
    or begin
        echo "ws: no CMD_KEY in $conf" >&2
        return 1
    end

    if not curl -sf -m 15 -o /dev/null -d "$signed" https://ntfy.sh/$cmd_topic
        echo "ws: could not reach ntfy.sh" >&2
        return 1
    end
    echo "sent '$command', waiting for the Yún (it polls every 30s)..."

    set -l replies
    for attempt in (seq (math $reply_wait_s / $poll_s))
        set replies (curl -s -m 15 "https://ntfy.sh/$reply_topic/json?poll=1&since=$since" | jq -r 'select(.event == "message") | .message')
        test (count $replies) -gt 0; and break
        sleep $poll_s
    end
    if test (count $replies) -eq 0
        echo "ws: no reply after "$reply_wait_s"s (Yún offline, or the room's uplink is down)" >&2
        return 1
    end
    printf '%s\n' $replies

    if test "$command" = status
        if tailscale ping -c 1 --timeout 5s $workstation_ts_ip >/dev/null 2>&1
            echo "OS is up (reachable over Tailscale)"
        else
            echo "OS not reachable over Tailscale (off, or still booting)"
        end
    else if test "$command" = on; and string match -q 'on sent*' -- $replies
        echo "waiting for it to boot (usually ~3 min)..."
        set -l boot_start (date +%s)
        while test (math (date +%s) - $boot_start) -lt $boot_wait_s
            if tailscale ping -c 1 --timeout 5s $workstation_ts_ip >/dev/null 2>&1
                echo "workstation up after "(math (date +%s) - $since)"s (reachable over Tailscale)"
                return 0
            end
            sleep $poll_s
        end
        echo "ws: powered on, but not reachable over Tailscale after "$boot_wait_s"s" >&2
        return 1
    end
end
