#!/bin/bash
# Auto-restart picam-motion if its capture loop hangs (process stays alive but
# stops logging). Installed 2026-05-31 after an 11h silent freeze.
LOG=/home/pi/picam-logs/motion.log
WLOG=/home/pi/picam-logs/watchdog.log
MAXAGE=150
now=$(date +%s)
if systemctl is-active --quiet picam-motion.service; then
  if [ -f "$LOG" ]; then
    age=$(( now - $(stat -c %Y "$LOG") ))
    if [ "$age" -gt "$MAXAGE" ]; then
      echo "$(date +%F\ %T) motion.log stale ${age}s -> restart" >> "$WLOG"
      systemctl restart picam-motion.service
    fi
  fi
else
  echo "$(date +%F\ %T) service not active -> start" >> "$WLOG"
  systemctl start picam-motion.service
fi
