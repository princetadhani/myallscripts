#!/bin/sh

# KASAN Continuous Monitoring Script
# This script monitors kernel logs for KASAN warnings and saves them periodically
# Similar to kmemleak monitoring for kernel panic debugging

# Copyright (c) 2024 Arista Networks, Inc. All rights reserved.
# Arista Networks, Inc. Confidential and Proprietary.

# Create a directory for logs
LOG_DIR="/opt/pstore_logs/kasan"
mkdir -p "$LOG_DIR"

# Check if KASAN is enabled
echo "Checking if KASAN is enabled..."
KASAN_ENABLED=$(zcat /proc/config.gz | grep 'CONFIG_KASAN=y' || echo "")

if [ -z "$KASAN_ENABLED" ]; then
    echo "ERROR: KASAN is not enabled in this kernel build!"
    echo "Please rebuild with ENABLE_KASAN_DBG=TRUE ENABLE_MM_DEBUG=TRUE"
    exit 1
fi

echo "KASAN is enabled. Starting continuous monitoring..."
echo "Logs will be saved to: $LOG_DIR"
echo "Monitoring interval: 10 minutes (600 seconds)"
echo "Note: Logs will only be retained if a KASAN warning is detected."

# Counter for tracking scans
SCAN_COUNT=0

# Continuous logging loop
while true; do
    SCAN_COUNT=$((SCAN_COUNT + 1))
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    LOG_FILE="$LOG_DIR/kasan_scan_$TIMESTAMP.log"

    echo "========================================" > "$LOG_FILE"
    echo "KASAN Scan #$SCAN_COUNT - $TIMESTAMP" >> "$LOG_FILE"
    echo "========================================" >> "$LOG_FILE"
    echo "" >> "$LOG_FILE"

    # Check dmesg for KASAN warnings
    echo "--- DMESG KASAN WARNINGS ---" >> "$LOG_FILE"
    dmesg | grep -F "BUG: KASAN" -A 200 >> "$LOG_FILE" 2>&1 || echo "No KASAN warnings found in dmesg" >> "$LOG_FILE"
    echo "" >> "$LOG_FILE"

    # Check current kernel log
    echo "--- CURRENT KERNEL LOG (/var/log/kern.logs) ---" >> "$LOG_FILE"
    if [ -f /var/log/kern.logs ]; then
        cat /var/log/kern.logs | grep "BUG: KASAN" -A 150 >> "$LOG_FILE" 2>&1 || echo "No KASAN warnings in current kern.logs" >> "$LOG_FILE"
    else
        echo "/var/log/kern.logs not found" >> "$LOG_FILE"
    fi
    echo "" >> "$LOG_FILE"

    # Check compressed kernel logs
    echo "--- COMPRESSED KERNEL LOGS (/var/log/kern.logs*.gz) ---" >> "$LOG_FILE"
    FOUND_COMPRESSED=0
    for gz_file in /var/log/kern.logs*.gz; do
        if [ -f "$gz_file" ]; then
            FOUND_COMPRESSED=1
            echo "Checking $gz_file..." >> "$LOG_FILE"
            zcat "$gz_file" | grep "BUG: KASAN" -A 150 >> "$LOG_FILE" 2>&1 || echo "  No KASAN warnings in $gz_file" >> "$LOG_FILE"
        fi
    done

    if [ $FOUND_COMPRESSED -eq 0 ]; then
        echo "No compressed kernel log files found" >> "$LOG_FILE"
    fi
    echo "" >> "$LOG_FILE"

    # Summary
    KASAN_COUNT=$(grep -c "BUG: KASAN" "$LOG_FILE" 2>/dev/null || true)
    # Handle case where grep returns empty or non-numeric
    if [ -z "$KASAN_COUNT" ] || ! [ "$KASAN_COUNT" -eq "$KASAN_COUNT" ] 2>/dev/null; then
        KASAN_COUNT=0
    fi

    echo "========================================" >> "$LOG_FILE"
    echo "Scan completed at $(date)" >> "$LOG_FILE"
    echo "Total KASAN warnings found in this scan: $KASAN_COUNT" >> "$LOG_FILE"
    echo "========================================" >> "$LOG_FILE"

    # Decide whether to keep or discard the log
    if [ "$KASAN_COUNT" -eq 0 ]; then
        # Discard the log file if nothing was found to save disk space
        rm -f "$LOG_FILE"
        # Console output for tracking (if watching live or viewing nohup out)
        echo "[$(date)] Scan #$SCAN_COUNT completed - No KASAN warnings found. (Log discarded)"
    else
        # Keep the log and print alerts
        echo "[$(date)] Scan #$SCAN_COUNT completed - Found $KASAN_COUNT KASAN warning(s) - Log: $LOG_FILE"
        echo "!!! WARNING: KASAN ISSUES DETECTED !!! Check $LOG_FILE for details"
    fi

    # Sleep for 10 minutes (600 seconds)
    sleep 600
done