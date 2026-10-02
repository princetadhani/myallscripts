#!/bin/sh

# nohup ./memory-cpu-monitor.sh > /dev/null 2>&1 &
# ps | grep memory-cpu-monitor
# command to get total client count: for i in /sys/class/net/ath*; do wlanconfig "${i##*/}" list 2>/dev/null; done | awk '/^[0-9a-fA-F]{2}:/ {c++} END {print c+0}'
# Continuous Memory, CPU, and Client Monitoring Script
# Dumps wlanconfig, /proc/meminfo, /proc/slabinfo, and mpstat into rolling 20MB log files.
# Archives to tar.gz once 30 rotated log files accumulate.

# Copyright (c) 2026 Arista Networks, Inc. All rights reserved.
# Arista Networks, Inc. Confidential and Proprietary.
#

# Directory configuration
LOG_DIR="/opt/pstore_logs/memory+cpu"
mkdir -p "$LOG_DIR"

INTERVAL=120                  # 2 minutes in seconds
MAX_SIZE_BYTES=20971520       # 20 MB in bytes (20 * 1024 * 1024)
MAX_LOG_NUM=30                # Maximum rotated files before archiving
CURRENT_LOG="$LOG_DIR/memory-cpu-monitor.log"
ITERATION=0

echo "Starting AP Resource Monitoring..."
echo "Output Directory: $LOG_DIR"
echo "Monitoring Interval: $INTERVAL seconds"
echo "Rollover: 20 MB per file, up to 30 files before tar.gz archive creation."

# Function to handle 20 MB file rotation
rotate_logs() {
    if [ -f "$CURRENT_LOG" ]; then
        FILE_SIZE=$(wc -c < "$CURRENT_LOG" 2>/dev/null || echo 0)
        if [ "$FILE_SIZE" -ge "$MAX_SIZE_BYTES" ]; then
            TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
            
            # Count how many rotated logs exist (e.g. memory-cpu-monitor.1.log ...)
            ROTATED_COUNT=$(ls -1 "$LOG_DIR"/memory-cpu-monitor.[0-9]*.log 2>/dev/null | grep -v "\.tar\.gz$" | wc -l)
            NEXT_NUM=$((ROTATED_COUNT + 1))
            
            echo "[$(date)] $CURRENT_LOG reached 20MB. Rotating to memory-cpu-monitor.${NEXT_NUM}.log"
            mv "$CURRENT_LOG" "$LOG_DIR/memory-cpu-monitor.${NEXT_NUM}.log"

            # Check if 30 rotated log files have accumulated
            if [ "$NEXT_NUM" -ge "$MAX_LOG_NUM" ]; then
                ARCHIVE_NAME="$LOG_DIR/archive_batch_${TIMESTAMP}.tar.gz"
                echo "[$(date)] Reached $MAX_LOG_NUM log files. Creating archive: $ARCHIVE_NAME..."
                
                (
                    cd "$LOG_DIR" && \
                    tar -czf "$ARCHIVE_NAME" memory-cpu-monitor.[0-9]*.log 2>/dev/null && \
                    rm -f memory-cpu-monitor.[0-9]*.log
                )
                
                if [ -f "$ARCHIVE_NAME" ]; then
                    echo "[$(date)] Archive successfully created and old rotated logs removed."
                else
                    echo "[$(date)] ERROR: Failed to create tar archive!"
                fi
            fi
        fi
    fi
}

# Main Monitoring Loop
while true; do
    ITERATION=$((ITERATION + 1))
    TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

    # Check for rollover/archiving before writing next block
    rotate_logs

    # Append Header Block directly without "output:"
    echo "iteration:${ITERATION}    timestamp:${TIMESTAMP}" >> "$CURRENT_LOG"
    
    # 1. Client Count via wlanconfig
    echo "=== CLIENT COUNT ===" >> "$CURRENT_LOG"
    CLIENTS=$(for i in /sys/class/net/ath*; do wlanconfig "${i##*/}" list 2>/dev/null; done | awk '/^[0-9a-fA-F]{2}:/ {c++} END {print c+0}')
    echo "Total Associated Clients: $CLIENTS" >> "$CURRENT_LOG"
    echo "" >> "$CURRENT_LOG"

    # 2. Memory Info
    echo "=== /proc/meminfo ===" >> "$CURRENT_LOG"
    if [ -f /proc/meminfo ]; then
        cat /proc/meminfo >> "$CURRENT_LOG"
    else
        echo "ERROR: /proc/meminfo not found" >> "$CURRENT_LOG"
    fi
    echo "" >> "$CURRENT_LOG"

    # 3. Slab Info
    echo "=== /proc/slabinfo ===" >> "$CURRENT_LOG"
    if [ -f /proc/slabinfo ]; then
        cat /proc/slabinfo >> "$CURRENT_LOG"
    else
        echo "ERROR: /proc/slabinfo not found" >> "$CURRENT_LOG"
    fi
    echo "" >> "$CURRENT_LOG"

    # 4. CPU Usage via mpstat
    echo "=== mpstat -P ALL 2 1 ===" >> "$CURRENT_LOG"
    if command -v mpstat >/dev/null 2>&1; then
        mpstat -P ALL 2 1 >> "$CURRENT_LOG" 2>&1
    else
        echo "ERROR: mpstat command not found" >> "$CURRENT_LOG"
    fi
    echo "" >> "$CURRENT_LOG"
    echo "------------------------------------------------------------------------------------------------------------------------" >> "$CURRENT_LOG"
    echo "" >> "$CURRENT_LOG"

    # Console feedback for stdout
    echo "[$(date)] iteration:${ITERATION} timestamp:${TIMESTAMP} | Clients: $CLIENTS -> $CURRENT_LOG"

    # Wait 2 minutes before next iteration
    sleep "$INTERVAL"
done