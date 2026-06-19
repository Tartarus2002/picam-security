#!/bin/bash
# ============================================
# PiCam Storage Cleanup Script
# ============================================
# Runs via cron to keep storage usage in check.
#   1. Deletes ALL recordings older than MAX_AGE_DAYS (default 7)
#   2. Deletes oldest recordings when storage exceeds size limit
#
# Crontab entry (runs every hour):
#   0 * * * * /home/pi/picam/scripts/storage-cleanup.sh

set -euo pipefail

# Load config
CONFIG_FILE="/home/pi/picam/config/picam.conf"
STORAGE_DIR="/home/pi/picam-recordings"
MAX_STORAGE_GB=8
MAX_AGE_DAYS=7
LOG_FILE="/home/pi/picam-logs/cleanup.log"

if [ -f "$CONFIG_FILE" ]; then
    eval "$(grep -E '^(STORAGE_DIR|MAX_STORAGE_GB|MAX_AGE_DAYS|LOG_DIR)=' "$CONFIG_FILE" | sed 's/#.*//' | sed 's/^/export /')"
    LOG_FILE="${LOG_DIR:-/home/pi/picam-logs}/cleanup.log"
fi

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

get_usage_mb() {
    du -sm "$STORAGE_DIR" 2>/dev/null | awk '{print $1}'
}

# -------------------------------------------
# Pass 1: Delete files older than MAX_AGE_DAYS
# -------------------------------------------
OLD_COUNT=0
while IFS= read -r filepath; do
    [ -z "$filepath" ] && continue
    filesize=$(du -k "$filepath" 2>/dev/null | awk '{print $1}')
    rm -f "$filepath"
    log "Expired (>${MAX_AGE_DAYS}d): $(basename "$filepath") (${filesize}KB)"
    OLD_COUNT=$((OLD_COUNT + 1))
done < <(find "$STORAGE_DIR" -type f \( -name "*.jpg" -o -name "*.mp4" -o -name "*.json" \) -mtime +${MAX_AGE_DAYS} 2>/dev/null)

if [ "$OLD_COUNT" -gt 0 ]; then
    log "Expired cleanup: removed $OLD_COUNT files older than ${MAX_AGE_DAYS} days"
fi

# Remove empty directories left behind
find "$STORAGE_DIR" -mindepth 1 -type d -empty -delete 2>/dev/null || true

# -------------------------------------------
# Pass 2: Delete oldest files if over size limit
# -------------------------------------------
MAX_MB=$((MAX_STORAGE_GB * 1024))
CURRENT_MB=$(get_usage_mb)

if [ "$CURRENT_MB" -le "$MAX_MB" ]; then
    log "Storage OK: ${CURRENT_MB}MB / ${MAX_MB}MB"
    exit 0
fi

log "Storage cleanup needed: ${CURRENT_MB}MB / ${MAX_MB}MB"

# Target 80% of max
TARGET_MB=$((MAX_MB * 80 / 100))

# Delete oldest files first
find "$STORAGE_DIR" -type f \( -name "*.jpg" -o -name "*.mp4" -o -name "*.json" \) \
    -printf '%T+ %p\n' | sort | while IFS= read -r line; do

    CURRENT_MB=$(get_usage_mb)
    if [ "$CURRENT_MB" -le "$TARGET_MB" ]; then
        break
    fi

    filepath=$(echo "$line" | cut -d' ' -f2-)
    filesize=$(du -k "$filepath" 2>/dev/null | awk '{print $1}')
    rm -f "$filepath"
    log "Size limit: $(basename "$filepath") (${filesize}KB)"
done

FINAL_MB=$(get_usage_mb)
log "Cleanup complete: ${FINAL_MB}MB / ${MAX_MB}MB"
