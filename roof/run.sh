#!/bin/bash
# launchd entry point. Waits for the address ALP binds to, because ZeroTier can come
# up after this at boot. Exits nonzero on timeout so launchd tries again.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
IP="$(sed -n "s/^ip_address *= *['\"]\([^'\"]*\)['\"].*/\1/p" device/config.toml | head -1)"
case "$IP" in
  ""|127.0.0.1|0.0.0.0) ;;
  *)
    for _ in $(seq 1 60); do
      /sbin/ifconfig | grep -q "inet $IP " && break
      /bin/sleep 5
    done
    /sbin/ifconfig | grep -q "inet $IP " || { echo "$IP is not up yet"; exit 1; }
    ;;
esac
exec "$REPO/.venv/bin/python" "$REPO/root_app.py"
