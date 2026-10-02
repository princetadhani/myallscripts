#!/bin/sh

# ============================================================
# KMEMLEAK ANALYSIS WORKFLOW
#
# Run script
#    ↓
# Scan all kmemleak logs
#    ↓
# Generate small summary
#    ↓
# Compare repeated/persistent objects
#    ↓
# Identify suspicious allocation paths/modules
#    ↓
# NO NEW SUSPICIOUS PATTERN → PASS / nothing to deep dive
#    ↓
# NEW/PERSISTENT PATTERN → DEEP DIVE THIS ONLY
#
# BusyBox compatible:
# - Does NOT require "comm"
# - Uses awk for state comparison
# ============================================================


LOG_DIR="/opt/pstore_logs/kmemleak"
PATTERN="kmemleak_*.log"

# Persistent state
STATE_DIR="$LOG_DIR/.state"

CURRENT_STATE="$STATE_DIR/current_objects"
PREVIOUS_STATE="$STATE_DIR/previous_objects"

NEW_FILE="$STATE_DIR/new_objects"
PERSISTENT_FILE="$STATE_DIR/persistent_objects"
REMOVED_FILE="$STATE_DIR/removed_objects"


# ------------------------------------------------------------
# Check log directory
# ------------------------------------------------------------

if [ ! -d "$LOG_DIR" ]; then
    echo "ERROR: Log directory not found: $LOG_DIR"
    exit 1
fi


# ------------------------------------------------------------
# Create state directory
# ------------------------------------------------------------

mkdir -p "$STATE_DIR"


# ------------------------------------------------------------
# Check that at least one log exists
# ------------------------------------------------------------

LOG_COUNT=$(ls "$LOG_DIR"/$PATTERN 2>/dev/null | wc -l)

if [ "$LOG_COUNT" -eq 0 ]; then
    echo "ERROR: No kmemleak logs found."
    echo "Expected: $LOG_DIR/$PATTERN"
    exit 1
fi


# ------------------------------------------------------------
# Helper:
# Read all kmemleak logs as ONE input stream.
#
# This avoids BusyBox grep producing:
# filename:count
# ------------------------------------------------------------

scan_logs()
{
    cat "$LOG_DIR"/$PATTERN 2>/dev/null
}


echo "============================================================"
echo "              KMEMLEAK STATEFUL SUMMARY"
echo "============================================================"
echo ""
echo "Log directory  : $LOG_DIR"
echo "Log files      : $LOG_COUNT"
echo "State directory: $STATE_DIR"
echo "Generated      : $(date)"
echo ""


# ------------------------------------------------------------
# 1. CURRENT SCAN
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "1. CURRENT SCAN"
echo "------------------------------------------------------------"


REPORT_COUNT=$(scan_logs | grep -i "unreferenced object" | wc -l)


# Extract unique object addresses.
#
# Example:
#
# unreferenced object 0xffffff8059d84600 (size 232):
#
# $3 = 0xffffff8059d84600
#

scan_logs |
grep -i "unreferenced object" |
awk '
{
    addr=$3
    gsub(/\(.*/, "", addr)

    if (addr != "")
        print addr
}
' |
sort |
uniq > "$CURRENT_STATE"


UNIQUE_COUNT=$(wc -l < "$CURRENT_STATE")


echo "Unreferenced reports : $REPORT_COUNT"
echo "Unique objects       : $UNIQUE_COUNT"
echo ""


# ------------------------------------------------------------
# 2. FIRST RUN / BASELINE
# ------------------------------------------------------------

if [ ! -f "$PREVIOUS_STATE" ]; then

    echo "------------------------------------------------------------"
    echo "2. STATE COMPARISON"
    echo "------------------------------------------------------------"
    echo ""
    echo "No previous state found."
    echo "This run will be used as the BASELINE."
    echo ""

    cp "$CURRENT_STATE" "$PREVIOUS_STATE"

    echo "Baseline saved."
    echo ""
    echo "State stored in:"
    echo "  $PREVIOUS_STATE"
    echo ""

    echo "============================================================"
    echo "                    BASELINE CREATED"
    echo "============================================================"
    echo ""
    echo "Objects recorded : $UNIQUE_COUNT"
    echo ""
    echo "Run the script again after the next kmemleak scan."
    echo "The next run will compare against the baseline."
    echo ""
    echo "============================================================"

    exit 0
fi


# ------------------------------------------------------------
# 3. STATE COMPARISON
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "2. STATE COMPARISON"
echo "------------------------------------------------------------"


PREVIOUS_COUNT=$(wc -l < "$PREVIOUS_STATE")


# ------------------------------------------------------------
# NEW OBJECTS
#
# Present in CURRENT but NOT in PREVIOUS
# ------------------------------------------------------------

awk '
NR == FNR {
    previous[$1] = 1
    next
}

!($1 in previous) {
    print $1
}
' "$PREVIOUS_STATE" "$CURRENT_STATE" |
sort -u > "$NEW_FILE"


# ------------------------------------------------------------
# PERSISTENT OBJECTS
#
# Present in BOTH CURRENT and PREVIOUS
# ------------------------------------------------------------

awk '
NR == FNR {
    previous[$1] = 1
    next
}

($1 in previous) {
    print $1
}
' "$PREVIOUS_STATE" "$CURRENT_STATE" |
sort -u > "$PERSISTENT_FILE"


# ------------------------------------------------------------
# REMOVED OBJECTS
#
# Present in PREVIOUS but NOT in CURRENT
# ------------------------------------------------------------

awk '
NR == FNR {
    current[$1] = 1
    next
}

!($1 in current) {
    print $1
}
' "$CURRENT_STATE" "$PREVIOUS_STATE" |
sort -u > "$REMOVED_FILE"


NEW_COUNT=$(wc -l < "$NEW_FILE")
PERSISTENT_COUNT=$(wc -l < "$PERSISTENT_FILE")
REMOVED_COUNT=$(wc -l < "$REMOVED_FILE")


echo "Previous unique objects : $PREVIOUS_COUNT"
echo "Current unique objects  : $UNIQUE_COUNT"
echo "New objects             : $NEW_COUNT"
echo "Persistent objects      : $PERSISTENT_COUNT"
echo "Removed objects         : $REMOVED_COUNT"
echo ""


# ------------------------------------------------------------
# 4. NEW OBJECTS
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "3. NEW OBJECTS"
echo "------------------------------------------------------------"

if [ "$NEW_COUNT" -eq 0 ]; then
    echo "None."
else
    head -20 "$NEW_FILE"

    if [ "$NEW_COUNT" -gt 20 ]; then
        echo ""
        echo "... showing first 20 of $NEW_COUNT new objects"
    fi
fi

echo ""


# ------------------------------------------------------------
# 5. PERSISTENT OBJECTS
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "4. PERSISTENT OBJECTS"
echo "------------------------------------------------------------"

if [ "$PERSISTENT_COUNT" -eq 0 ]; then

    echo "None."

else

    echo "Persistent objects : $PERSISTENT_COUNT"
    echo ""
    echo "Top persistent objects:"
    echo ""

    scan_logs |
    grep -i "unreferenced object" |
    awk '
    {
        addr=$3
        gsub(/\(.*/, "", addr)

        if (addr != "")
            count[addr]++
    }

    END {
        for (addr in count)
            print count[addr], addr
    }' |
    sort -nr |
    head -20

fi

echo ""


# ------------------------------------------------------------
# 6. MOST REPEATED OBJECTS
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "5. MOST REPEATED OBJECTS"
echo "------------------------------------------------------------"

scan_logs |
grep -i "unreferenced object" |
awk '
{
    addr=$3
    gsub(/\(.*/, "", addr)

    if (addr != "")
        count[addr]++
}

END {
    for (addr in count)
        print count[addr], addr
}' |
sort -nr |
head -20

echo ""


# ------------------------------------------------------------
# 7. OBJECT SIZE SUMMARY
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "6. OBJECT SIZE SUMMARY"
echo "------------------------------------------------------------"

scan_logs |
grep -i "unreferenced object" |
awk '
{
    for (i=1; i<=NF; i++) {

        if ($i == "(size") {

            size=$(i+1)
            gsub(/[^0-9].*/, "", size)

            if (size != "")
                count[size]++
        }
    }
}

END {
    for (size in count)
        print count[size], size
}' |
sort -nr |
head -20

echo ""


# ------------------------------------------------------------
# 8. TOP COMM / PROCESS ENTRIES
#
# Extract only:
#
# comm "techsupport"
#
# instead of counting the whole line including changing age.
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "7. TOP COMM / PROCESS ENTRIES"
echo "------------------------------------------------------------"

scan_logs |
grep -i 'comm "' |
sed 's/.*comm "\([^"]*\)".*/\1/' |
sort |
uniq -c |
sort -nr |
head -20

echo ""


# ------------------------------------------------------------
# 9. FINAL RESULT
# ------------------------------------------------------------

echo "============================================================"
echo "                    FINAL RESULT"
echo "============================================================"
echo ""


if [ "$NEW_COUNT" -gt 0 ]; then

    echo "⚠ NEW KMEMLEAK OBJECTS DETECTED"
    echo ""
    echo "New addresses : $NEW_COUNT"
    echo ""
    echo "Next action:"
    echo "  1. Check NEW object allocation backtrace."
    echo "  2. Check object AGE."
    echo "  3. Check comm/process."
    echo "  4. Identify allocation function/module."
    echo "  5. Repeat the scan to check persistence."

elif [ "$PERSISTENT_COUNT" -gt 0 ]; then

    echo "⚠ PERSISTENT KMEMLEAK PATTERN"
    echo ""
    echo "No NEW addresses detected."
    echo "$PERSISTENT_COUNT existing objects remain unreferenced."
    echo ""
    echo "Next action:"
    echo "  Check object AGE and allocation BACKTRACE."
    echo "  Persistent objects with increasing age need investigation."

else

    echo "✓ NO NEW OR PERSISTENT KMEMLEAK OBJECTS"
    echo ""
    echo "No new or persistent unreferenced objects detected."

fi


# ------------------------------------------------------------
# 10. SAVE CURRENT STATE
# ------------------------------------------------------------

cp "$CURRENT_STATE" "$PREVIOUS_STATE"


echo ""
echo "------------------------------------------------------------"
echo "STATE SAVED"
echo "------------------------------------------------------------"
echo ""
echo "Previous state:"
echo "  $PREVIOUS_STATE"
echo ""
echo "Current state:"
echo "  $CURRENT_STATE"
echo ""

echo "============================================================"
echo "============================================================"