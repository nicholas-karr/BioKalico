#!/bin/bash
#
# Host monitoring, run by cron. Local checks (disk, services, ports, Klipper
# state) run every time; network checks (certificates, HTTP, timers) run once
# per NETWORK_CHECK_INTERVAL. Host-specific settings are in
# ~/printer_data/config/monitoring.conf.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
CONFIG_FILE="${MONITORING_CONF:-$HOME/printer_data/config/monitoring.conf}"

# Self-install into cron on first run on a new host.
crontab -l 2>/dev/null | grep -qF "$SCRIPT_DIR/monitoring.sh" || (crontab -l 2>/dev/null; echo "*/15 * * * * $SCRIPT_DIR/monitoring.sh") | crontab -

if [ ! -f "$CONFIG_FILE" ]; then
    # Write a starting config guessed from this system. It is never
    # overwritten.
    mkdir -p "$(dirname "$CONFIG_FILE")"
    guessed_cert_domains=$(grep -ohP 'ssl_certificate\s+/etc/letsencrypt/live/\K[^/]+' \
        /etc/nginx/nginx.conf /etc/nginx/sites-enabled/* 2>/dev/null | sort -u)
    cert_domains_line="CERT_DOMAINS=()"
    if [ -n "$guessed_cert_domains" ]; then
        cert_domains_line="CERT_DOMAINS=($(printf '"%s" ' $guessed_cert_domains))"
    fi

    guessed_timers=$(systemctl list-unit-files --type=timer --no-legend 2>/dev/null | awk '{print $1}' | grep -i renew)
    timers_line="REQUIRED_TIMERS=()"
    if [ -n "$guessed_timers" ]; then
        timers_line="REQUIRED_TIMERS=($(printf '"%s" ' $guessed_timers))"
    fi

    # Watch every enabled, running service except per-session ones (getty@,
    # user@, session@), plus all per-user services and timers.
    user_services_guess=$(systemctl --user list-units --type=service --all --no-legend 2>/dev/null \
        | awk '{print $1}' | grep -vE '^(●|×)' | sed 's/^[^a-zA-Z]*//' \
        | while read -r unit; do
              [ -n "$unit" ] && printf '"%s" ' "$unit"
          done)
    user_timers_guess=$(systemctl --user list-timers --all --no-legend 2>/dev/null \
        | awk '{print $NF == "" ? "" : $(NF-1)}' | grep '\.timer$' \
        | while read -r unit; do printf '"%s" ' "$unit"; done)

    critical_services_guess=$(systemctl list-units --type=service --state=running --no-legend 2>/dev/null \
        | awk '{print $1}' | grep -vE '^(getty|user|session)@' \
        | while read -r unit; do
              systemctl is-enabled --quiet "$unit" 2>/dev/null && printf '"%s" ' "$unit"
          done)

    cat > "$CONFIG_FILE" <<EOF
# monitoring.sh config for $(hostname), generated $(date -Iseconds).
# Edit freely; it is never overwritten.

NOTIFY_METHOD="mail"
MAIL_TO="${SUDO_USER:-$USER}"
MAIL_SUBJECT="Monitoring alert on $(hostname)"
RELAY_URL=""
RELAY_TOKEN=""

# Leave DISK_PATHS empty to check every mounted volume, or list mount
# points to check only those.
DISK_THRESHOLD=90
DISK_PATHS=()

# Services that must be running and not crash-looping. RESTART_THRESHOLD is
# how many restarts between two runs counts as a crash loop.
CRITICAL_SERVICES=($critical_services_guess)
RESTART_THRESHOLD=3

# host:port entries that must accept a TCP connection. Catches a process
# that systemd reports as running but is hung.
PORT_CHECKS=()

# "<url>|<dotted.path>" entries naming a JSON field that is null when healthy
# and an error message otherwise, e.g. a projector's serial link status.
JSON_ERROR_CHECKS=()

# systemctl --user units, such as backups.
USER_SERVICES=($user_services_guess)
USER_TIMERS=($user_timers_guess)

CERT_MIN_DAYS=14
$cert_domains_line
HTTP_CHECKS=()
$timers_line

NETWORK_CHECK_INTERVAL=3600
STATE_DIR="\$HOME/.cache/monitoring"

# Seconds before an ongoing problem's alert is sent again.
REMINDER_INTERVAL=14400
EOF
    echo "Generated $CONFIG_FILE (cert domains: ${guessed_cert_domains:-none guessed}; timers: ${guessed_timers:-none guessed})" >&2
fi
source "$CONFIG_FILE"

: "${NOTIFY_METHOD:=mail}"
: "${MAIL_TO:=$USER}"
: "${MAIL_SUBJECT:=Monitoring alert on $(hostname)}"
: "${RELAY_URL:=}"
: "${RELAY_TOKEN:=}"
: "${DISK_THRESHOLD:=90}"
: "${RESTART_THRESHOLD:=3}"
: "${CERT_MIN_DAYS:=14}"
: "${NETWORK_CHECK_INTERVAL:=3600}"
: "${STATE_DIR:=$HOME/.cache/monitoring}"
: "${REMINDER_INTERVAL:=14400}"

# Filesystem types skipped by the disk check.
PSEUDO_FSTYPES=" tmpfs devtmpfs proc sysfs cgroup cgroup2 overlay squashfs autofs mqueue debugfs tracefs fusectl configfs pstore bpf nsfs devpts binfmt_misc rpc_pipefs efivarfs hugetlbfs "

mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/last_network_check"

ALERTS=()
ALERT_KEYS=()

# $1 is a stable key (e.g. "service:klipper.service:failed") used to tell an
# ongoing problem from a new one, since the message text can change.
alert() {
    ALERT_KEYS+=("$1")
    ALERTS+=("$2")
}

send_alert() {
    # NOTIFY_METHOD in the config picks mail or the HTTP relay.
    local body doc_url
    doc_url="https://github.com/nicholas-karr/BioKalico/blob/main/biokalico_extras/monitoring_troubleshooting.md"
    body=$(printf '%s\n' "${ALERTS[@]}")
    body+=$'\n'"If you have terminal access, $doc_url walks through installing a coding agent and having it debug this for you."
    case "$NOTIFY_METHOD" in
        http_relay)
            local payload
            payload=$(MSG="$body" SUBJ="$MAIL_SUBJECT" python3 -c '
import json, os
print(json.dumps({"subject": os.environ["SUBJ"], "message": os.environ["MSG"]}))
')
            curl -sS --max-time 10 -X POST "$RELAY_URL" \
                -H "X-Alert-Token: $RELAY_TOKEN" \
                -H "Content-Type: application/json" \
                --data-binary "$payload"
            ;;
        *)
            printf '%s' "$body" | mail -s "$MAIL_SUBJECT" "$MAIL_TO"
            ;;
    esac
}

check_disk() {
    local pct fstype target
    local -a targets=() pcts=()

    while read -r pct fstype target; do
        [[ "$PSEUDO_FSTYPES" == *" $fstype "* ]] && continue
        if [ ${#DISK_PATHS[@]} -gt 0 ]; then
            local want match=0
            for want in "${DISK_PATHS[@]}"; do
                [ "$want" = "$target" ] && match=1 && break
            done
            [ "$match" -eq 0 ] && continue
        fi
        targets+=("$target")
        pcts+=("$pct")
    done < <(df -PT 2>/dev/null | tail -n +2 | awk '{ print $6, $2, $7 }')

    local i pct_num
    for i in "${!targets[@]}"; do
        pct_num="${pcts[$i]%\%}"
        if [ "$pct_num" -gt "$DISK_THRESHOLD" ]; then
            alert "disk:${targets[$i]}" "Disk usage on ${targets[$i]} is ${pct_num}% (threshold ${DISK_THRESHOLD}%)."
        fi
    done
}

check_cert_expiry() {
    local domain="$1" cert_end expiry_ts days_left
    cert_end=$(echo | openssl s_client -connect "$domain:443" -servername "$domain" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
    if [ -z "$cert_end" ]; then
        alert "cert:$domain" "Could not retrieve SSL cert for $domain (connection/handshake failure)."
        return
    fi
    expiry_ts=$(date -d "$cert_end" +%s)
    days_left=$(( (expiry_ts - $(date +%s)) / 86400 ))
    if [ "$days_left" -lt "$CERT_MIN_DAYS" ]; then
        alert "cert:$domain" "SSL cert for $domain expires in $days_left day(s) (on $cert_end)."
    fi
}

check_http() {
    local url="$1" err_file http_code curl_err
    err_file=$(mktemp)
    http_code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "$url" 2>"$err_file")
    curl_err=$(cat "$err_file")
    rm -f "$err_file"
    if [ "$http_code" = "000" ] || ! [[ "$http_code" =~ ^[23] ]]; then
        alert "http:$url" "$url unhealthy (http_code=${http_code:-none}${curl_err:+, error: $curl_err})."
    fi
}

check_user_service() {
    # systemctl --user units are not visible to check_service.
    local svc="$1" result
    if systemctl --user is-failed --quiet "$svc" 2>/dev/null; then
        result=$(systemctl --user show -p Result --value "$svc" 2>/dev/null)
        alert "user_service:$svc" "user unit $svc has failed on $(hostname): $result. Last log: $(journalctl --user -u "$svc" -n 1 --no-pager -o cat 2>/dev/null)."
    fi
}

check_user_timer() {
    local timer="$1"
    if ! systemctl --user is-active --quiet "$timer" 2>/dev/null; then
        alert "user_timer:$timer" "user timer $timer is not active on $(hostname); whatever it schedules is no longer running."
    fi
}

check_port() {
    # A hung service can still look active to systemd.
    local target="$1" host="${1%:*}" port="${1##*:}"
    if ! timeout 5 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
        alert "port:$target" "Nothing is accepting connections on $target ($(hostname)); the owning process may be running but wedged."
    fi
}

check_json_error() {
    # A service can be running while a device it uses is not, e.g. the
    # projector's serial adapter. $1 is "<url>|<dotted.path>"; the field's
    # value, if not empty, is the alert text.
    local spec="$1" url path body value
    url="${spec%%|*}"
    path="${spec#*|}"
    body=$(python3 "$SCRIPT_DIR/scripts/moonraker_api.py" --timeout 10 "$url" 2>/dev/null)
    if [ -z "$body" ]; then
        alert "json:$spec" "Could not query $url on $(hostname) to check $path; the endpoint returned nothing."
        return
    fi
    value=$(BODY="$body" FIELD="$path" python3 -c '
import json, os, sys
try:
    data = json.loads(os.environ["BODY"])
except ValueError:
    sys.exit(1)
for key in os.environ["FIELD"].split("."):
    if not isinstance(data, dict) or key not in data:
        sys.exit(0)
    data = data[key]
if data not in (None, "", False):
    print(data)
' 2>/dev/null)
    if [ $? -ne 0 ]; then
        alert "json:$spec" "Could not parse the response from $url on $(hostname) while checking $path."
        return
    fi
    if [ -n "$value" ]; then
        alert "json:$spec" "${path##*.} on $(hostname): $value"
    fi
}

check_klippy_state() {
    # Klipper stays running in "shutdown" or "error", so systemd doesn't
    # notice. auto_recovery may fix it, but gives up after a few tries.
    # An unreachable Moonraker is reported by HTTP_CHECKS instead.
    local body state message
    body=$(python3 "$SCRIPT_DIR/scripts/moonraker_api.py" --timeout 10 "http://127.0.0.1:7125/printer/info" 2>/dev/null)
    [ -z "$body" ] && return

    state=$(BODY="$body" python3 -c '
import json, os, sys
try:
    data = json.loads(os.environ["BODY"])
except ValueError:
    sys.exit(0)
print(data.get("result", {}).get("state", ""))
' 2>/dev/null)

    if [ "$state" = "shutdown" ] || [ "$state" = "error" ]; then
        message=$(BODY="$body" python3 -c '
import json, os
data = json.loads(os.environ["BODY"])
msg = (data.get("result", {}).get("state_message") or "").strip()
print(msg.splitlines()[0] if msg else "(no message)")
' 2>/dev/null)
        alert "klippy_state" "Klipper is in \"$state\" state on $(hostname): $message"
    fi
}

check_timer() {
    local timer="$1"
    if ! systemctl is-active --quiet "$timer"; then
        alert "timer:$timer:active" "$timer is not active on $(hostname)."
    fi
    if ! systemctl is-enabled --quiet "$timer"; then
        alert "timer:$timer:enabled" "$timer is not enabled (won't survive reboot) on $(hostname)."
    fi
}

check_service() {
    local svc="$1" restarts last_restarts delta restart_state

    if ! systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        alert "service:$svc:enabled" "$svc is not enabled (won't survive reboot) on $(hostname)."
    fi

    if systemctl is-failed --quiet "$svc"; then
        alert "service:$svc:failed" "$svc has failed on $(hostname): $(systemctl show -p Result --value "$svc" 2>/dev/null). Last log: $(journalctl -u "$svc" -n 1 --no-pager -o cat 2>/dev/null)."
        return
    fi

    if ! systemctl is-active --quiet "$svc"; then
        alert "service:$svc:running" "$svc is not running on $(hostname)."
        return
    fi

    # A crash-looping Restart=always service usually reads as active, so
    # compare its restart count with the last run.
    restarts=$(systemctl show -p NRestarts --value "$svc" 2>/dev/null)
    [[ "$restarts" =~ ^[0-9]+$ ]] || return
    restart_state="$STATE_DIR/restarts_$(printf '%s' "$svc" | tr -c 'A-Za-z0-9._-' '_')"
    last_restarts=0
    [ -f "$restart_state" ] && last_restarts=$(cat "$restart_state")
    printf '%s\n' "$restarts" > "$restart_state"

    # The counter resets on reboot or daemon-reload; treat a decrease as a
    # fresh count rather than reporting a negative delta.
    if [ "$restarts" -lt "$last_restarts" ]; then
        delta="$restarts"
    else
        delta=$(( restarts - last_restarts ))
    fi

    if [ "$delta" -ge "$RESTART_THRESHOLD" ]; then
        alert "service:$svc:crashloop" "$svc restarted $delta time(s) since the last check on $(hostname) (crash loop, $restarts total). Last log: $(journalctl -u "$svc" -n 1 --no-pager -o cat 2>/dev/null)."
    fi
}

# Needed for systemctl --user under cron.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

check_disk
for service in "${CRITICAL_SERVICES[@]}"; do check_service "$service"; done
for service in "${USER_SERVICES[@]}"; do check_user_service "$service"; done
for timer in "${USER_TIMERS[@]}"; do check_user_timer "$timer"; done
for listener in "${PORT_CHECKS[@]}"; do check_port "$listener"; done
for spec in "${JSON_ERROR_CHECKS[@]}"; do check_json_error "$spec"; done
check_klippy_state

now=$(date +%s)
last=0
[ -f "$STATE_FILE" ] && last=$(cat "$STATE_FILE")
if [ $(( now - last )) -ge "$NETWORK_CHECK_INTERVAL" ]; then
    for domain in "${CERT_DOMAINS[@]}"; do check_cert_expiry "$domain"; done
    for url in "${HTTP_CHECKS[@]}"; do check_http "$url"; done
    for timer in "${REQUIRED_TIMERS[@]}"; do check_timer "$timer"; done
    echo "$now" > "$STATE_FILE"
fi

# A new alert key is sent right away; an ongoing one only every
# REMINDER_INTERVAL. State for keys that no longer alert is removed, so a
# problem that comes back later counts as new.
ALERT_STATE_DIR="$STATE_DIR/alerts"
mkdir -p "$ALERT_STATE_DIR"

to_send=()
seen_state_files=()
for i in "${!ALERT_KEYS[@]}"; do
    key_state_file="$ALERT_STATE_DIR/$(printf '%s' "${ALERT_KEYS[$i]}" | tr -c 'A-Za-z0-9._-' '_')"
    seen_state_files+=("$key_state_file")
    last_sent=0
    [ -f "$key_state_file" ] && last_sent=$(cat "$key_state_file")
    if [ $(( now - last_sent )) -ge "$REMINDER_INTERVAL" ]; then
        to_send+=("${ALERTS[$i]}")
        echo "$now" > "$key_state_file"
    fi
done

for existing_state_file in "$ALERT_STATE_DIR"/*; do
    [ -e "$existing_state_file" ] || continue
    if ! printf '%s\n' "${seen_state_files[@]}" | grep -qxF "$existing_state_file"; then
        rm -f "$existing_state_file"
    fi
done

if [ ${#to_send[@]} -gt 0 ]; then
    ALERTS=("${to_send[@]}")
    send_alert
fi
