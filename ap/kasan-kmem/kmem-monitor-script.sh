#!/bin/sh
# Enable kmemleak scanning at startup
echo scan > /sys/kernel/debug/kmemleak

# Create a directory for logs
LOG_DIR="/opt/pstore_logs/kmemleak"
mkdir -p "$LOG_DIR"

# Continuous logging loop
while true; do
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    LOG_FILE="$LOG_DIR/kmemleak_$TIMESTAMP.log"
    cat /sys/kernel/debug/kmemleak > "$LOG_FILE"
    sleep 60  # Adjust sleep duration as needed
done