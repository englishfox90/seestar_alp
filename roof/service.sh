#!/bin/bash
# Run ALP (with roof automation) as a login service that survives a reboot.
#   roof/service.sh install | uninstall | start | stop | restart [--force] | status | logs
set -u
LABEL="com.englishfox90.seestar-alp"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
BIND="$(sed -n "s/^ip_address *= *['\"]\([^'\"]*\)['\"].*/\1/p" "$REPO/device/config.toml" | head -1)"
[ "$BIND" = "0.0.0.0" ] && BIND=127.0.0.1
ALPACA="http://${BIND:-127.0.0.1}:5555/api/v1/telescope/1/action"

# A system daemon so it starts at boot, with no login. The venv must use a native
# arm64 Python (/opt/homebrew): macOS gives launchd jobs running the Intel build in
# /usr/local "No route to host" when they try to reach the scope.
need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
  fi
}

write_plist() {
  local user home
  user="$(stat -f %Su "$REPO")"
  home="$(eval echo "~$user")"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>UserName</key><string>$user</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/roof/run.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict><key>HOME</key><string>$home</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$home/Library/Logs/seestar_alp.log</string>
  <key>StandardErrorPath</key><string>$home/Library/Logs/seestar_alp.log</string>
</dict>
</plist>
PLIST
  chown root:wheel "$PLIST"
  chmod 644 "$PLIST"
}

is_loaded() { launchctl print "system/$LABEL" >/dev/null 2>&1; }

remove_old_agent() {
  local user uid home
  user="$(stat -f %Su "$REPO")"
  uid="$(id -u "$user")"
  home="$(eval echo "~$user")"
  launchctl bootout "gui/$uid/$LABEL" 2>/dev/null
  rm -f "$home/Library/LaunchAgents/$LABEL.plist"
}

# Restarting ALP re-grabs master and ends whatever the scope is doing.
imaging_now() {
  curl -s -m 12 -X PUT "$ALPACA" --data-urlencode "Action=method_sync" \
    --data-urlencode 'Parameters={"method":"iscope_get_app_state"}' \
    --data-urlencode ClientID=77 --data-urlencode ClientTransactionID=1 \
  | /usr/bin/python3 -c '
import json,sys
try:
    r=json.load(sys.stdin)["Value"]["result"]
except Exception:
    sys.exit(1)
sys.exit(0 if r.get("View",{}).get("state")=="working" or r.get("ViewPlan",{}).get("state")=="working" else 1)'
}

guard() {
  if [ "${1:-}" != "--force" ] && imaging_now; then
    echo "The scope is imaging. Restarting ALP would end the session."
    echo "Stop it first, or pass --force."
    exit 1
  fi
}

case "${1:-}" in
  install)
    need_root "$@"
    guard "${2:-}"
    remove_old_agent
    write_plist
    is_loaded && launchctl bootout "system/$LABEL"
    launchctl bootstrap system "$PLIST" && echo "Installed; running now and at every boot. UI: http://${BIND:-localhost}:5432/roof"
    ;;
  uninstall)
    need_root "$@"
    guard "${2:-}"
    is_loaded && launchctl bootout "system/$LABEL"
    rm -f "$PLIST"
    echo "Removed."
    ;;
  start)
    need_root "$@"
    is_loaded && launchctl kickstart "system/$LABEL" || launchctl bootstrap system "$PLIST"
    ;;
  stop)
    need_root "$@"
    guard "${2:-}"
    launchctl bootout "system/$LABEL" && echo "Stopped (returns at next boot, or: roof/service.sh start)."
    ;;
  restart)
    need_root "$@"
    guard "${2:-}"
    launchctl kickstart -k "system/$LABEL" && echo "Restarted."
    ;;
  status)
    launchctl print "system/$LABEL" 2>/dev/null | grep -E "^\s+(state|pid|last exit code) =" | head -3 || true
    is_loaded || echo "Not loaded."
    lsof -nP -iTCP:5432 -sTCP:LISTEN 2>/dev/null | tail -n +2 | awk '{print "port 5432: " $1 " pid " $2}'
    ;;
  logs)
    tail -f "$REPO/roof_automation.log"
    ;;
  *)
    sed -n 2p "$0"
    exit 2
    ;;
esac
