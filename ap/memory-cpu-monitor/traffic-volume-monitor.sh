#!/bin/sh

# nohup ./traffic-volume-monitor.sh > /dev/null 2>&1 &
# ps | grep traffic-volume-monitor

INTERVAL=${1:-900}
IFACE="bond0"
CURRENT_LOG="/opt/pstore_logs/memory+cpu/traffic.log"

ITERATION=0

while true; do
    ITERATION=$((ITERATION + 1))
    TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

    START=$(date +%s)
    RX1=$(cat /sys/class/net/$IFACE/statistics/rx_bytes)
    TX1=$(cat /sys/class/net/$IFACE/statistics/tx_bytes)

    sleep "$INTERVAL"

    END=$(date +%s)
    RX2=$(cat /sys/class/net/$IFACE/statistics/rx_bytes)
    TX2=$(cat /sys/class/net/$IFACE/statistics/tx_bytes)

    RX=$((RX2 - RX1))
    TX=$((TX2 - TX1))
    TOTAL=$((RX + TX))

    # Convert bytes to GB (1 GB = 1,000,000,000 bytes)
    RX_GB=$(awk "BEGIN {printf \"%.2f\", $RX / 1000000000}")
    TX_GB=$(awk "BEGIN {printf \"%.2f\", $TX / 1000000000}")
    TOTAL_GB=$(awk "BEGIN {printf \"%.2f\", $TOTAL / 1000000000}")

    echo "iteration:${ITERATION}    timestamp:${TIMESTAMP}" >> "$CURRENT_LOG"
    echo "Start: $(date -d @$START)" >> "$CURRENT_LOG"
    echo "End  : $(date -d @$END)" >> "$CURRENT_LOG"
    echo "RX   : $RX bytes ($RX_GB GB)" >> "$CURRENT_LOG"
    echo "TX   : $TX bytes ($TX_GB GB)" >> "$CURRENT_LOG"
    echo "Total: $TOTAL bytes ($TOTAL_GB GB)" >> "$CURRENT_LOG"
    echo "" >> "$CURRENT_LOG"
    echo "------------------------------------------------------------------------------------------------------------------------" >> "$CURRENT_LOG"
    echo "" >> "$CURRENT_LOG"

    echo "[$(date)] iteration:${ITERATION} | RX: ${RX_GB} GB | TX: ${TX_GB} GB | Total: ${TOTAL_GB} GB -> $CURRENT_LOG"
done