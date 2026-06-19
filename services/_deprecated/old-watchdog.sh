#!/bin/bash
# ============================================
# PiCam Watchdog Script
# ============================================
# Checks health of camera services and restarts if needed.
# Run by picam-watchdog.timer every 5 minutes.

set -euo pipefail

LOG_FILE="/home/pi/picam-logs/watchdog.log"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [WATCHDOG] $1" >> "$LOG_FILE"
}

# Check if mediamtx is responding
check_stream() {
    if curl -sf http://127.0.0.1:9997/v3/paths/list > /dev/null 2>&1; then
        return 0
    fi
    return 1
}

# Check if motion detector process is running
check_motion() {
    if systemctl is-active --quiet picam-motion; then
        return 0
    fi
    return 1
}

# Check disk space (warn if < 500MB free)
check_disk() {
    local free_mb
    free_mb=$(df /home/pi --output=avail -BM | tail -1 | tr -d 'M ')
    if [ "$free_mb" -lt 500 ]; then
        log "WARNING: Low disk space: ${free_mb}MB free"
        # Trigger cleanup
        /home/pi/picam/scripts/storage-cleanup.sh
        return 1
    fi
    return 0
}

# Check CPU temperature (throttle warning > 80C)
check_temp() {
    local temp
    temp=$(vcgencmd measure_temp 2>/dev/null | grep -oP '[0-9.]+' || echo "0")
    local temp_int=${temp%.*}
    if [ "$temp_int" -gt 80 ]; then
        log "WARNING: High CPU temperature: ${temp}C"
        return 1
    fi
    return 0
}

# --- Run Checks ---
ISSUES=0

if ! check_stream; then
    log "Stream service DOWN - restarting picam-stream"
    systemctl restart picam-stream || true
    ISSUES=$((ISSUES + 1))
fi

if ! check_motion; then
    log "Motion service DOWN - restarting picam-motion"
    systemctl restart picam-motion || true
    ISSUES=$((ISSUES + 1))
fi

check_disk || true
check_temp || true

if [ "$ISSUES" -eq 0 ]; then
    log "All systems healthy"
fi
