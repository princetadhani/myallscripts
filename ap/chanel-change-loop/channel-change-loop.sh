#!/bin/sh

# Run debug_acs sequentially for every monitor interface configured on the AP.
# This script intentionally uses only POSIX/BusyBox-compatible shell tools.

SLEEP_SECONDS=300
ITERATION=0
SEPARATOR="------------------------------------------------------------------------------------------------------------------------"

log()
{
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

get_mon_wifi()
{
    conf_file=$1

    # Read the first MON_WIFI assignment, case-insensitively.  Trim spaces,
    # CRLF characters, and optional single/double quotes around the value.
    awk '
        tolower($0) ~ /^[[:space:]]*mon_wifi[[:space:]]*=/ {
            value = $0
            sub(/^[^=]*=[[:space:]]*/, "", value)
            sub(/[[:space:]\r]*$/, "", value)
            if ((substr(value, 1, 1) == "\"" && substr(value, length(value), 1) == "\"") ||
                (substr(value, 1, 1) == "\047" && substr(value, length(value), 1) == "\047")) {
                value = substr(value, 2, length(value) - 2)
            }
            print value
            exit
        }
    ' "$conf_file"
}

print_radio_mapping()
{
    echo "=== RADIO MONITOR INTERFACE MAPPING ==="
    for mapping_file in /tmp/radio0/radio.conf /tmp/radio1/radio.conf /tmp/radio2/radio.conf
    do
        if [ ! -r "$mapping_file" ]; then
            echo "$mapping_file: ERROR: file is not readable"
            continue
        fi

        mapping_value=$(get_mon_wifi "$mapping_file")
        if [ -n "$mapping_value" ]; then
            echo "$mapping_file: MON_WIFI=$mapping_value"
        else
            echo "$mapping_file: MON_WIFI not found"
        fi
    done
    echo "$SEPARATOR"
    echo ""
}

echo "Starting channel-change loop..."
echo "Monitoring interval: ${SLEEP_SECONDS} seconds"
print_radio_mapping

while :
do
    ITERATION=$((ITERATION + 1))
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

    echo "iteration:${ITERATION}    timestamp:${TIMESTAMP}"
    echo "=== DEBUG ACS COMMANDS ==="

    for conf_file in /tmp/radio0/radio.conf /tmp/radio1/radio.conf /tmp/radio2/radio.conf
    do
        if [ ! -r "$conf_file" ]; then
            log "WARNING: cannot read $conf_file; skipping"
            continue
        fi

        mon_wifi=$(get_mon_wifi "$conf_file")
        if [ -z "$mon_wifi" ]; then
            log "WARNING: MON_WIFI is missing or empty in $conf_file; skipping"
            continue
        fi

        # Prevent malformed configuration values from becoming command input.
        case "$mon_wifi" in
            *[!A-Za-z0-9_.:-]*)
                log "WARNING: invalid MON_WIFI value '$mon_wifi' in $conf_file; skipping"
                continue
                ;;
        esac

        log "running: iwpriv $mon_wifi debug_acs 7"
        if command_output=$(iwpriv "$mon_wifi" debug_acs 7 2>&1); then
            if [ -n "$command_output" ]; then
                echo "$command_output"
            fi
            log "completed: $mon_wifi"
        else
            status=$?
            if [ -n "$command_output" ]; then
                echo "$command_output"
            fi
            log "ERROR: iwpriv failed for $mon_wifi (exit=$status)"
        fi
    done

    log "cycle complete; sleeping ${SLEEP_SECONDS}s"
    echo "$SEPARATOR"
    echo ""
    sleep "$SLEEP_SECONDS"
done
