#!/bin/sh

# ==============================================================================
# Continuous Process Killer & procd Respawn Verifier Script
# Logs PID transitions (before/after), respawn latency, and procd recovery status.
#
# Respawn timeouts are dynamically calculated based on:
#   ubus call service list -> (term_timeout + respawn.timeout + buffer)
#
# Usage:
#   ./process-kill-loop.sh --random
#   ./process-kill-loop.sh --all --interval 10
#   ./process-kill-loop.sh --specific hostapd,gobin,wgd --interval 10
#   ./process-kill-loop.sh --specific sensord-linux,configagent
#
# Background execution:
#   nohup ./process-kill-loop.sh --all --interval 10 > /dev/null 2>&1 &
# ==============================================================================

# Directory & Log Configuration
LOG_DIR="/root/process-kill-loop"
mkdir -p "$LOG_DIR"

INTERVAL=15                   # Default delay between test iterations (seconds)
MAX_SIZE_BYTES=20971520       # 20 MB in bytes (20 * 1024 * 1024)
MAX_LOG_NUM=30                # Maximum rotated files before creating a tar.gz archive
CURRENT_LOG="$LOG_DIR/process-kill-loop.log"
ITERATION=0

ALL_TARGETS="wl_evt_handler hostapd synch_agent gobin rrmagent configagent rsyslogd app_visibility wgd sensord-linux portald"

usage() {
    echo "=============================================================================="
    echo "Usage: $0 [MODE] [--interval <seconds>]"
    echo "=============================================================================="
    echo ""
    echo "Modes:"
    echo "  --random               Randomly select and kill one procd daemon per loop"
    echo "  --all                  Kill all tracked procd daemons in sequence per loop"
    echo "  --specific <p1,p2,..>  Kill only the specified comma-separated processes"
    echo ""
    echo "Options:"
    echo "  --interval <seconds>   Custom sleep interval between loops (Default: 15)"
    echo ""
    echo "Available processes (for copy-pasting into --specific):"
    echo "  wl_evt_handler   (Max Wait: 12s | term:5s,  respawn:5s)"
    echo "  hostapd          (Max Wait: 12s | term:5s,  respawn:5s)"
    echo "  synch_agent      (Max Wait: 12s | term:5s,  respawn:5s)"
    echo "  gobin            (Max Wait: 12s | term:5s,  respawn:5s)"
    echo "  rrmagent         (Max Wait: 12s | term:5s,  respawn:5s)"
    echo "  configagent      (Max Wait: 17s | term:5s,  respawn:10s)"
    echo "  rsyslogd         (Max Wait: 8s  | term:5s,  respawn:1s)"
    echo "  app_visibility   (Max Wait: 12s | term:5s,  respawn:5s)"
    echo "  wgd              (Max Wait: 12s | term:5s,  respawn:5s)"
    echo "  sensord-linux    (Max Wait: 42s | term:30s, respawn:10s)"
    echo "  portald          (Max Wait: 17s | term:5s,  respawn:10s)"
    echo ""
    echo "Examples:"
    echo "  $0 --all --interval 10"
    echo "  $0 --specific hostapd,gobin,wgd --interval 10"
    echo "  $0 --specific sensord-linux,configagent"
    echo "=============================================================================="
    exit 1
}

MODE=""
SPECIFIC_LIST=""
SPECIFIC_RAW=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --random)
            MODE="--random"
            shift
            ;;
        --all)
            MODE="--all"
            shift
            ;;
        --specific)
            MODE="--specific"
            if [ -z "$2" ] || echo "$2" | grep -q "^--"; then
                echo "Error: --specific requires a comma-separated list of processes."
                usage
            fi
            SPECIFIC_RAW="$2"
            SPECIFIC_LIST=$(echo "$2" | tr ',' ' ')
            shift 2
            ;;
        --interval)
            if [ -z "$2" ] || echo "$2" | grep -q "^--"; then
                echo "Error: --interval requires a numeric value in seconds."
                usage
            fi
            INTERVAL="$2"
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

if [ -z "$MODE" ]; then
    usage
fi

rotate_logs() {
    if [ -f "$CURRENT_LOG" ]; then
        FILE_SIZE=$(wc -c < "$CURRENT_LOG" 2>/dev/null || echo 0)
        if [ "$FILE_SIZE" -ge "$MAX_SIZE_BYTES" ]; then
            ROT_TS=$(date +"%Y%m%d_%H%M%S")
            ROTATED_COUNT=$(ls -1 "$LOG_DIR"/process-kill-loop.[0-9]*.log 2>/dev/null | grep -v "\.tar\.gz$" | wc -l)
            NEXT_NUM=$((ROTATED_COUNT + 1))

            echo "[$(date)] $CURRENT_LOG reached 20MB. Rotating to process-kill-loop.${NEXT_NUM}.log"
            mv "$CURRENT_LOG" "$LOG_DIR/process-kill-loop.${NEXT_NUM}.log"

            if [ "$NEXT_NUM" -ge "$MAX_LOG_NUM" ]; then
                ARCHIVE_NAME="$LOG_DIR/archive_batch_${ROT_TS}.tar.gz"
                echo "[$(date)] Reached $MAX_LOG_NUM log files. Creating archive: $ARCHIVE_NAME..."
                (
                    cd "$LOG_DIR" && \
                    tar -czf "$ARCHIVE_NAME" process-kill-loop.[0-9]*.log 2>/dev/null && \
                    rm -f process-kill-loop.[0-9]*.log
                )
            fi
        fi
    fi
}

# ------------------------------------------------------------------------------
# Dynamically get max wait time based on ubus service configs
# Total = term_timeout + respawn.timeout + 2s execution buffer
# ------------------------------------------------------------------------------
get_expected_timeout() {
    PROC_NAME="$1"
    case "$PROC_NAME" in
        sensord-linux)
            # term_timeout: 30, timeout: 10 -> 42s
            echo 42
            ;;
        configagent|portald)
            # term_timeout: 5, timeout: 10 -> 17s
            echo 17
            ;;
        rsyslogd|nl_agent)
            # term_timeout: 5, timeout: 1 -> 8s
            echo 8
            ;;
        wl_evt_handler|hostapd|synch_agent|gobin|rrmagent|app_visibility|wgd)
            # term_timeout: 5, timeout: 5 -> 12s
            echo 12
            ;;
        *)
            # Fallback default
            echo 15
            ;;
    esac
}

get_proc_pid() {
    PROC_NAME="$1"
    TARGET_PID=""

    case "$PROC_NAME" in
        hostapd)
            TARGET_PID=$(pgrep -f "^/sbin/hostapd" 2>/dev/null | head -n 1)
            ;;
        gobin)
            TARGET_PID=$(pgrep -f "^/opt/gobin" 2>/dev/null | head -n 1)
            ;;
        wgd)
            TARGET_PID=$(pgrep -f "^/opt/portal/sbin/wgd" 2>/dev/null | head -n 1)
            ;;
        portald)
            TARGET_PID=$(pgrep -f "^/opt/portal/sbin/portald" 2>/dev/null | head -n 1)
            ;;
        sensord-linux)
            TARGET_PID=$(pgrep -f "^/opt/sensor/sensord-linux" 2>/dev/null | head -n 1)
            ;;
        app_visibility)
            TARGET_PID=$(pgrep -f "^/opt/app_visibility/app_visibility" 2>/dev/null | head -n 1)
            ;;
        synch_agent)
            TARGET_PID=$(pgrep -f "^/opt/ap/synch_agent" 2>/dev/null | head -n 1)
            ;;
        wl_evt_handler)
            TARGET_PID=$(pgrep -f "^/sbin/wl_evt_handler" 2>/dev/null | head -n 1)
            ;;
        rrmagent)
            TARGET_PID=$(pgrep -f "^/sbin/rrmagent" 2>/dev/null | head -n 1)
            ;;
        configagent)
            TARGET_PID=$(pgrep -f "^/sbin/configagent" 2>/dev/null | head -n 1)
            ;;
        rsyslogd)
            TARGET_PID=$(pgrep -f "^/usr/sbin/rsyslogd" 2>/dev/null | head -n 1)
            ;;
        *)
            TARGET_PID=$(pidof "$PROC_NAME" 2>/dev/null | awk '{print $NF}')
            if [ -z "$TARGET_PID" ]; then
                TARGET_PID=$(pgrep -x "$PROC_NAME" 2>/dev/null | head -n 1)
            fi
            ;;
    esac

    echo "$TARGET_PID"
}

kill_and_verify() {
    PROC="$1"
    OLD_PID=$(get_proc_pid "$PROC")
    MAX_WAIT=$(get_expected_timeout "$PROC")

    echo "--- Target: $PROC ---" >> "$CURRENT_LOG"

    if [ -z "$OLD_PID" ]; then
        echo "Status       : FAILED (Process was not running before kill attempt)" >> "$CURRENT_LOG"
        echo "[$(date)] [$PROC] ERROR: Process is not currently active!"
        echo "" >> "$CURRENT_LOG"
        return 1
    fi

    echo "PID Before   : $OLD_PID" >> "$CURRENT_LOG"
    echo "Action       : Sending SIGTERM to PID $OLD_PID (Max Wait: ${MAX_WAIT}s)" >> "$CURRENT_LOG"

    kill "$OLD_PID" 2>/dev/null

    WAITED=0
    NEW_PID=""
    RESPAWNED=0

    while [ "$WAITED" -lt "$MAX_WAIT" ]; do
        sleep 1
        WAITED=$((WAITED + 1))
        CURRENT_PID=$(get_proc_pid "$PROC")

        if [ -n "$CURRENT_PID" ] && [ "$CURRENT_PID" != "$OLD_PID" ]; then
            NEW_PID="$CURRENT_PID"
            RESPAWNED=1
            break
        fi
    done

    if [ "$RESPAWNED" -eq 1 ]; then
        echo "PID After    : $NEW_PID" >> "$CURRENT_LOG"
        echo "procd Status : SUCCESS (Respawned in ${WAITED}s)" >> "$CURRENT_LOG"
        echo "[$(date)] [$PROC] OLD_PID: $OLD_PID -> NEW_PID: $NEW_PID (Respawned in ${WAITED}s: PASS)"
    else
        echo "PID After    : NONE" >> "$CURRENT_LOG"
        echo "procd Status : FAILED (Did not respawn within ${MAX_WAIT}s)" >> "$CURRENT_LOG"
        echo "[$(date)] [$PROC] OLD_PID: $OLD_PID -> FAILED TO RESPAWN within ${MAX_WAIT}s!"
    fi

    echo "" >> "$CURRENT_LOG"
}

if [ "$MODE" = "--specific" ]; then
    DISPLAY_MODE="--specific [$SPECIFIC_RAW]"
else
    DISPLAY_MODE="$MODE"
fi

echo "=================================================="
echo "Starting Process Killer & Verification Loop"
echo "Log Directory : $LOG_DIR"
echo "Log File      : $CURRENT_LOG"
echo "Mode          : $DISPLAY_MODE"
echo "Loop Interval : ${INTERVAL}s"
echo "Timeouts      : Dynamic based on ubus service parameters"
echo "=================================================="

while true; do
    ITERATION=$((ITERATION + 1))
    START_TS=$(date +"%Y-%m-%d %H:%M:%S")

    rotate_logs

    echo "================================================================================" >> "$CURRENT_LOG"
    echo "Iteration    : $ITERATION (Started: $START_TS)" >> "$CURRENT_LOG"
    echo "Execution Mode: $DISPLAY_MODE" >> "$CURRENT_LOG"
    echo "Loop Interval: ${INTERVAL}s" >> "$CURRENT_LOG"
    echo "================================================================================" >> "$CURRENT_LOG"

    case "$MODE" in
        --random)
            TARGET_COUNT=$(echo "$ALL_TARGETS" | awk '{print NF}')
            RAND_IDX=$(awk -v min=1 -v max="$TARGET_COUNT" 'BEGIN{srand(); print int(min+rand()*(max-min+1))}')
            SELECTED_PROC=$(echo "$ALL_TARGETS" | cut -d ' ' -f "$RAND_IDX")
            kill_and_verify "$SELECTED_PROC"
            ;;
        --all)
            for PROC in $ALL_TARGETS; do
                kill_and_verify "$PROC"
                sleep 2
            done
            ;;
        --specific)
            for PROC in $SPECIFIC_LIST; do
                kill_and_verify "$PROC"
                sleep 2
            done
            ;;
    esac

    END_TS=$(date +"%H:%M:%S")
    NEXT_TS=$(date -d "@$(( $(date +%s) + INTERVAL ))" +"%H:%M:%S" 2>/dev/null || echo "+${INTERVAL}s")

    echo "--------------------------------------------------------------------------------" >> "$CURRENT_LOG"
    echo "Iter-${ITERATION}: over at ${END_TS} | Waiting ${INTERVAL}s for next iteration (Next: ~${NEXT_TS})" >> "$CURRENT_LOG"
    echo "--------------------------------------------------------------------------------" >> "$CURRENT_LOG"
    echo "" >> "$CURRENT_LOG"

    echo "[$(date)] Iter-${ITERATION}: over at ${END_TS}. Waiting ${INTERVAL}s before next cycle (Next: ~${NEXT_TS})..."
    sleep "$INTERVAL"
done