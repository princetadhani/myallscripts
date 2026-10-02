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
# ============================================================

LOG_DIR="/opt/pstore_logs/kmemleak"
PATTERN="kmemleak_*.log"

echo "============================================================"
echo "                 KMEMLEAK SUMMARY"
echo "============================================================"
echo "Log directory : $LOG_DIR"
echo "Log pattern   : $PATTERN"
echo "Generated     : $(date)"
echo

# ------------------------------------------------------------
# 1. Total kmemleak reports
# ------------------------------------------------------------

TOTAL=$(cat "$LOG_DIR"/$PATTERN 2>/dev/null \
    | grep -ic "unreferenced object")

echo "------------------------------------------------------------"
echo "1. TOTAL UNREFERENCED OBJECT REPORTS"
echo "------------------------------------------------------------"
echo "Total reports : $TOTAL"
echo

if [ "$TOTAL" -eq 0 ]; then
    echo "RESULT: No kmemleak unreferenced objects found."
    echo "============================================================"
    exit 0
fi

# ------------------------------------------------------------
# 2. Unique object addresses
# ------------------------------------------------------------

UNIQUE=$(cat "$LOG_DIR"/$PATTERN 2>/dev/null \
    | grep -i "unreferenced object" \
    | awk '{print $3}' \
    | sort -u \
    | wc -l)

echo "------------------------------------------------------------"
echo "2. UNIQUE OBJECT ADDRESSES"
echo "------------------------------------------------------------"
echo "Unique objects : $UNIQUE"
echo

# ------------------------------------------------------------
# 3. Persistent objects
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "3. MOST PERSISTENT OBJECTS"
echo "------------------------------------------------------------"
echo "Address                                      Reports"
echo "------------------------------------------------------------"

cat "$LOG_DIR"/$PATTERN 2>/dev/null \
    | grep -i "unreferenced object" \
    | awk '{print $3}' \
    | sort \
    | uniq -c \
    | sort -nr \
    | head -20

echo

# ------------------------------------------------------------
# 4. Allocation sizes
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "4. OBJECT SIZE SUMMARY"
echo "------------------------------------------------------------"

cat "$LOG_DIR"/$PATTERN 2>/dev/null \
    | grep -i "unreferenced object" \
    | sed -n 's/.*(size \([0-9]*\)).*/\1/p' \
    | sort \
    | uniq -c \
    | sort -nr \
    | head -15

echo

# ------------------------------------------------------------
# 5. Processes / kernel contexts
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "5. TOP COMM / PROCESS ENTRIES"
echo "------------------------------------------------------------"

cat "$LOG_DIR"/$PATTERN 2>/dev/null \
    | grep -i "comm " \
    | sort \
    | uniq -c \
    | sort -nr \
    | head -15

echo

# ------------------------------------------------------------
# 6. Allocation backtrace functions
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "6. TOP ALLOCATION FUNCTIONS"
echo "------------------------------------------------------------"

cat "$LOG_DIR"/$PATTERN 2>/dev/null \
    | grep -E "^[[:space:]]*[a-zA-Z0-9_]+[[:space:]]*\+" \
    | sed 's/^[[:space:]]*//' \
    | sed 's/+.*//' \
    | sort \
    | uniq -c \
    | sort -nr \
    | head -20

echo

# ------------------------------------------------------------
# 7. Oldest objects
# ------------------------------------------------------------

echo "------------------------------------------------------------"
echo "7. OLDEST KMEMLEAK OBJECTS"
echo "------------------------------------------------------------"

cat "$LOG_DIR"/$PATTERN 2>/dev/null \
    | grep -i "age " \
    | sort -nr -k6 \
    | head -10

echo

# ------------------------------------------------------------
# 8. Quick assessment
# ------------------------------------------------------------

echo "============================================================"
echo "                     QUICK ASSESSMENT"
echo "============================================================"

if [ "$TOTAL" -eq 0 ]; then

    echo "PASS: No unreferenced objects detected."

elif [ "$UNIQUE" -le 10 ]; then

    echo "ATTENTION: Small number of unique objects detected."
    echo "Action: Check whether the same addresses persist across scans."

else

    echo "ATTENTION: Multiple unique unreferenced objects detected."
    echo "Action: Review persistent objects and allocation backtraces."

fi

echo
echo "IMPORTANT:"
echo "Repeated reports do NOT automatically mean memory leaks."
echo "Persistent addresses + increasing age + allocation backtrace"
echo "are the important indicators."
echo
echo "For deep dive, inspect the suspicious object address with:"
echo
echo "grep -B1 -A25 <OBJECT_ADDRESS> $LOG_DIR/$PATTERN"
echo
echo "============================================================"