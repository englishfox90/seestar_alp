#!/bin/bash
# Run ALP (with roof automation) as a login service that survives a reboot.
#   roof/service.sh install | uninstall | start | stop | restart [--force] | status | logs
set -u
LABEL="com.englishfox90.seestar-alp"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/seestar_alp.log"
DOMAIN="gui/$(id -u)"
ALPACA="http://127.0.0.1:5555/api/v1/telescope/1/action"

write_plist() {
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO/.venv/bin/python</string>
    <string>$REPO/root_app.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
}

is_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

# Restarting ALP re-grabs master and ends whatever the scope is doing.
imaging_now() {
  curl -s -m 12 -X PUT "$ALPACA" --data-urlencode "Action=method_sync" \
    --data-urlencode 'Parameters={"method":"iscope_get_app_state"}' \
    --data-urlencode ClientID=77 --data-urlencode ClientTransactionID=1 \
  | python3 -c '
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
    mkdir -p "$(dirname "$PLIST")"
    write_plist
    is_loaded && launchctl bootout "$DOMAIN/$LABEL"
    launchctl bootstrap "$DOMAIN" "$PLIST" && echo "Installed; starts now and at every login. UI: http://localhost:5432/roof"
    ;;
  uninstall)
    guard "${2:-}"
    is_loaded && launchctl bootout "$DOMAIN/$LABEL"
    rm -f "$PLIST"
    echo "Removed."
    ;;
  start)
    is_loaded && launchctl kickstart "$DOMAIN/$LABEL" || launchctl bootstrap "$DOMAIN" "$PLIST"
    ;;
  stop)
    guard "${2:-}"
    launchctl bootout "$DOMAIN/$LABEL" && echo "Stopped (returns at next login, or: roof/service.sh start)."
    ;;
  restart)
    guard "${2:-}"
    launchctl kickstart -k "$DOMAIN/$LABEL" && echo "Restarted."
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "^\s+(state|pid|last exit code) =" || echo "Not loaded."
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
